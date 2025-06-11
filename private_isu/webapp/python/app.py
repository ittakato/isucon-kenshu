import datetime
import hashlib
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
from collections import defaultdict

import flask
import MySQLdb.cursors
from flask_session import Session
from jinja2 import pass_eval_context
from markupsafe import Markup, escape
from pymemcache.client.base import Client as MemcacheClient

UPLOAD_LIMIT = 10 * 1024 * 1024  # 10mb
POSTS_PER_PAGE = 20


_config = None


def config():
    global _config
    if _config is None:
        _config = {
            "db": {
                "host": os.environ.get("ISUCONP_DB_HOST", "localhost"),
                "port": int(os.environ.get("ISUCONP_DB_PORT", "3306")),
                "user": os.environ.get("ISUCONP_DB_USER", "root"),
                "db": os.environ.get("ISUCONP_DB_NAME", "isuconp"),
            },
            "memcache": {
                "address": os.environ.get(
                    "ISUCONP_MEMCACHED_ADDRESS", "127.0.0.1:11211"
                ),
            },
        }
        password = os.environ.get("ISUCONP_DB_PASSWORD")
        if password:
            _config["db"]["passwd"] = password
    return _config


_db = None


def db():
    global _db
    if _db is None:
        conf = config()["db"].copy()
        conf["charset"] = "utf8mb4"
        conf["cursorclass"] = MySQLdb.cursors.DictCursor
        conf["autocommit"] = True
        _db = MySQLdb.connect(**conf)
    return _db


def db_initialize():
    cur = db().cursor()
    sqls = [
        "DELETE FROM users WHERE id > 1000",
        "DELETE FROM posts WHERE id > 10000",
        "DELETE FROM comments WHERE id > 100000",
        "UPDATE users SET del_flg = 0",
        "UPDATE users SET del_flg = 1 WHERE id % 50 = 0",
    ]
    for q in sqls:
        cur.execute(q)


_mcclient = None


def memcache():
    global _mcclient
    if _mcclient is None:
        conf = config()["memcache"]
        _mcclient = MemcacheClient(
            conf["address"], no_delay=True, default_noreply=False
        )
    return _mcclient


def try_login(account_name, password):
    cur = db().cursor()
    cur.execute(
        "SELECT * FROM users WHERE account_name = %s AND del_flg = 0", (account_name,)
    )
    user = cur.fetchone()

    if user and calculate_passhash(user["account_name"], password) == user["passhash"]:
        return user
    return None


def validate_user(account_name: str, password: str):
    if not re.match(r"[0-9a-zA-Z]{3,}", account_name):
        return False
    if not re.match(r"[0-9a-zA-Z_]{6,}", password):
        return False
    return True


def digest(src: str) -> str:
    h = hashlib.sha512()
    h.update(src.encode("utf-8"))
    return h.hexdigest()


def calculate_salt(account_name: str):
    return digest(account_name)


def calculate_passhash(account_name: str, password: str):
    return digest("%s:%s" % (password, calculate_salt(account_name)))


def get_session_user():
    user = flask.session.get("user")
    if user:
        cur = db().cursor()
        cur.execute("SELECT * FROM `users` WHERE `id` = %s", (user["id"],))
        return cur.fetchone()
    return None


BATCH_META_SQL = """
  SELECT
    p.id           AS post_id,
    u.id           AS user_id,
    u.account_name AS account_name,
    u.del_flg      AS user_del_flg,
    COUNT(c.id)    AS comment_count
  FROM posts p
  JOIN users u
    ON u.id = p.user_id
   AND u.del_flg = 0
  LEFT JOIN comments c
    ON c.post_id = p.id
  WHERE p.id IN %s
  GROUP BY p.id, u.id, u.account_name, u.del_flg
"""

BATCH_COMMENTS_SQL = """
  SELECT
    c.post_id,
    c.id,
    c.comment,
    c.user_id,
    u.account_name   AS commenter_name,
    u.del_flg        AS commenter_del_flg,
    c.created_at
  FROM comments c
  JOIN users u
    ON u.id = c.user_id
  WHERE c.post_id IN %s
  ORDER BY c.created_at DESC
"""


def make_posts(raw_posts):
    if not raw_posts:
        return []

    post_ids = [p["id"] for p in raw_posts]
    cursor = db().cursor()

    # 1) batch author + count
    cursor.execute(BATCH_META_SQL, (post_ids,))
    meta_by_post = {
        row["post_id"]: {
            "user": {
                "id": row["user_id"],
                "account_name": row["account_name"],
                "del_flg": row["user_del_flg"],
            },
            "comment_count": row["comment_count"],
        }
        for row in cursor.fetchall()
    }

    # 2) batch fetch comments
    cursor.execute(BATCH_COMMENTS_SQL, (post_ids,))
    comments_by_post = defaultdict(list)
    for cm in cursor.fetchall():
        comments_by_post[cm["post_id"]].append(
            {
                "id": cm["id"],
                "comment": cm["comment"],
                "user": {
                    "account_name": cm["commenter_name"],
                    "del_flg": cm["commenter_del_flg"],
                },
                "created_at": cm["created_at"],
            }
        )

    # 3) build final list
    posts = []
    for post in raw_posts:
        meta = meta_by_post.get(post["id"])
        if not meta:
            continue  # skip deleted author or missing meta

        # attach author & count
        post["user"] = meta["user"]
        post["comment_count"] = meta["comment_count"]

        # take the 3 most recent comments in chronological order
        cms = comments_by_post.get(post["id"], [])
        cms.reverse()  # now oldest→newest
        post["comments"] = cms[-3:]

        # only include if author isn’t deleted
        if post["user"]["del_flg"] == 0:
            posts.append(post)

        if len(posts) >= POSTS_PER_PAGE:
            break
    return posts


# app setup
static_path = pathlib.Path(__file__).resolve().parent.parent / "public"
app = flask.Flask(__name__, static_folder=str(static_path), static_url_path="")
# app.debug = True

# Flask-Session
app.config["SESSION_TYPE"] = "memcached"
app.config["SESSION_MEMCACHED"] = memcache()
Session(app)


@app.template_global()
def image_url(post):
    ext = ""
    mime = post["mime"]
    if mime == "image/jpeg":
        ext = ".jpg"
    elif mime == "image/png":
        ext = ".png"
    elif mime == "image/gif":
        ext = ".gif"

    return "/image/%s%s" % (post["id"], ext)


# http://flask.pocoo.org/snippets/28/
_paragraph_re = re.compile(r"(?:\r\n|\r|\n){2,}")


@app.template_filter()
@pass_eval_context
def nl2br(eval_ctx, value):
    result = "\n\n".join(
        "<p>%s</p>" % p.replace("\n", "<br>\n")
        for p in _paragraph_re.split(escape(value))
    )
    if eval_ctx.autoescape:
        result = Markup(result)
    return result


# Cache-Control header for static files
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "public, max-age=31536000"
    return response


# endpoints


@app.route("/initialize")
def get_initialize():
    db_initialize()
    return ""


@app.route("/login")
def get_login():
    if get_session_user():
        return flask.redirect("/")
    return flask.render_template("login.html", me=None)


@app.route("/login", methods=["POST"])
def post_login():
    if get_session_user():
        return flask.redirect("/")

    user = try_login(flask.request.form["account_name"], flask.request.form["password"])
    if user:
        flask.session["user"] = {"id": user["id"]}
        flask.session["csrf_token"] = os.urandom(8).hex()
        return flask.redirect("/")

    flask.flash("アカウント名かパスワードが間違っています")
    return flask.redirect("/login")


@app.route("/register")
def get_register():
    if get_session_user():
        return flask.redirect("/")
    return flask.render_template("register.html", me=None)


@app.route("/register", methods=["POST"])
def post_register():
    if get_session_user():
        return flask.redirect("/")

    account_name = flask.request.form["account_name"]
    password = flask.request.form["password"]
    if not validate_user(account_name, password):
        flask.flash(
            "アカウント名は3文字以上、パスワードは6文字以上である必要があります"
        )
        return flask.redirect("/register")

    cursor = db().cursor()
    cursor.execute("SELECT 1 FROM users WHERE `account_name` = %s", (account_name,))
    user = cursor.fetchone()
    if user:
        flask.flash("アカウント名がすでに使われています")
        return flask.redirect("/register")

    query = "INSERT INTO `users` (`account_name`, `passhash`) VALUES (%s, %s)"
    cursor.execute(query, (account_name, calculate_passhash(account_name, password)))

    flask.session["user"] = {"id": cursor.lastrowid}
    flask.session["csrf_token"] = os.urandom(8).hex()
    return flask.redirect("/")


@app.route("/logout")
def get_logout():
    flask.session.clear()
    return flask.redirect("/")


@app.route("/")
def get_index():
    me = get_session_user()

    cursor = db().cursor()
    cursor.execute(
        "SELECT `id`, `user_id`, `body`, `created_at`, `mime` FROM `posts` ORDER BY `created_at` DESC"
    )
    posts = make_posts(cursor.fetchall())

    return flask.render_template("index.html", posts=posts, me=me)


@app.route("/@<account_name>")
def get_user_list(account_name):
    cursor = db().cursor()

    cursor.execute(
        "SELECT * FROM `users` WHERE `account_name` = %s AND `del_flg` = 0",
        (account_name,),
    )
    user = cursor.fetchone()
    if user is None:
        flask.abort(404)

    cursor.execute(
        "SELECT `id`, `user_id`, `body`, `mime`, `created_at` FROM `posts` WHERE `user_id` = %s ORDER BY `created_at` DESC",
        (user["id"],),
    )
    posts = make_posts(cursor.fetchall())

    cursor.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM `posts` WHERE `user_id` = %s) AS post_count,
            (SELECT COUNT(*) FROM `comments` WHERE `user_id` = %s) AS comment_count,
            (SELECT COUNT(*) FROM `comments` WHERE `post_id` IN (SELECT `id` FROM `posts` WHERE `user_id` = %s)) AS commented_count
        """,
        (user["id"], user["id"], user["id"]),
    )
    counts = cursor.fetchone()

    me = get_session_user()

    return flask.render_template(
        "user.html",
        posts=posts,
        user=user,
        post_count=counts["post_count"],
        comment_count=counts["comment_count"],
        commented_count=counts["commented_count"],
        me=me,
    )


def _parse_iso8601(s):
    # http://bugs.python.org/issue15873
    # Ignore timezone
    try:
        # Slice the string to its first 19 characters (the YYYY-MM-DDTHH:MM:SS part)
        # and pass it to the highly optimized fromisoformat parser.
        return datetime.datetime.fromisoformat(s[:19])
    except (ValueError, TypeError, IndexError):
        # Catch potential errors for invalid or too-short strings
        # and raise a ValueError consistent with the original function.
        raise ValueError(f"Invalid iso8601 format: {s!r}")


@app.route("/posts")
def get_posts():
    cursor = db().cursor()
    max_created_at = flask.request.args.get("max_created_at") or None
    if max_created_at:
        max_created_at = _parse_iso8601(max_created_at)
        cursor.execute(
            "SELECT `id`, `user_id`, `body`, `mime`, `created_at` FROM `posts` WHERE `created_at` <= %s ORDER BY `created_at` DESC",
            (max_created_at,),
        )
    else:
        cursor.execute(
            "SELECT `id`, `user_id`, `body`, `mime`, `created_at` FROM `posts` ORDER BY `created_at` DESC"
        )
    results = cursor.fetchall()
    posts = make_posts(results)
    return flask.render_template("posts.html", posts=posts)


@app.route("/posts/<id>")
def get_posts_id(id):
    cursor = db().cursor()

    cursor.execute("SELECT * FROM `posts` WHERE `id` = %s", (id,))
    posts = make_posts(cursor.fetchall(), all_comments=True)
    if not posts:
        flask.abort(404)

    me = get_session_user()
    return flask.render_template("post.html", post=posts[0], me=me)


@app.route("/", methods=["POST"])
def post_index():
    me = get_session_user()
    if not me:
        return flask.redirect("/login")

    if flask.request.form["csrf_token"] != flask.session["csrf_token"]:
        flask.abort(422)

    file = flask.request.files.get("file")
    if not file:
        flask.flash("画像が必要です")
        return flask.redirect("/")

    # 投稿のContent-Typeからファイルのタイプを決定する
    mime = file.mimetype
    if mime not in ("image/jpeg", "image/png", "image/gif"):
        flask.flash("投稿できる画像形式はjpgとpngとgifだけです")
        return flask.redirect("/")

    with tempfile.TemporaryFile() as tempf:
        file.save(tempf)
        tempf.flush()

        if tempf.tell() > UPLOAD_LIMIT:
            flask.flash("ファイルサイズが大きすぎます")
            return flask.redirect("/")

        tempf.seek(0)
        imgdata = tempf.read()

    query = "INSERT INTO `posts` (`user_id`, `mime`, `imgdata`, `body`) VALUES (%s,%s,%s,%s)"
    cursor = db().cursor()
    cursor.execute(query, (me["id"], mime, imgdata, flask.request.form.get("body")))
    pid = cursor.lastrowid
    return flask.redirect("/posts/%d" % pid)


@app.route("/image/<id>.<ext>")
def get_image(id, ext):
    if not id:
        return ""
    id = int(id)
    if id == 0:
        return ""

    cursor = db().cursor()
    cursor.execute("SELECT * FROM `posts` WHERE `id` = %s", (id,))
    post = cursor.fetchone()

    mime = post["mime"]
    if (
        ext == "jpg"
        and mime == "image/jpeg"
        or ext == "png"
        and mime == "image/png"
        or ext == "gif"
        and mime == "image/gif"
    ):
        return flask.Response(post["imgdata"], mimetype=mime)

    flask.abort(404)


@app.route("/comment", methods=["POST"])
def post_comment():
    me = get_session_user()
    if not me:
        return flask.redirect("/login")

    if flask.request.form["csrf_token"] != flask.session["csrf_token"]:
        flask.abort(422)

    post_id = flask.request.form["post_id"]
    if not post_id.isdigit():
        return "post_idは整数のみです"
    post_id = int(post_id)

    query = (
        "INSERT INTO `comments` (`post_id`, `user_id`, `comment`) VALUES (%s, %s, %s)"
    )
    cursor = db().cursor()
    cursor.execute(query, (post_id, me["id"], flask.request.form["comment"]))

    return flask.redirect("/posts/%d" % post_id)


@app.route("/admin/banned")
def get_banned():
    me = get_session_user()
    if not me:
        return flask.redirect("/login")

    if me["authority"] == 0:
        flask.abort(403)

    cursor = db().cursor()
    cursor.execute(
        "SELECT * FROM `users` WHERE `authority` = 0 AND `del_flg` = 0 ORDER BY `created_at` DESC"
    )
    users = cursor.fetchall()

    return flask.render_template("banned.html", users=users, me=me)


@app.route("/admin/banned", methods=["POST"])
def post_banned():
    me = get_session_user()
    if not me:
        return flask.redirect("/login")

    if me["authority"] == 0:
        flask.abort(403)

    if flask.request.form["csrf_token"] != flask.session["csrf_token"]:
        flask.abort(422)

    cursor = db().cursor()
    query = "UPDATE `users` SET `del_flg` = %s WHERE `id` = %s"
    for id in flask.request.form.getlist("uid", type=int):
        cursor.execute(query, (1, id))

    return flask.redirect("/admin/banned")
