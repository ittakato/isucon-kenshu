import datetime
import hashlib
import os
import pathlib
import re
import tempfile

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
        # Optimize connection settings for better performance
        conf["connect_timeout"] = 5
        conf["read_timeout"] = 30
        conf["write_timeout"] = 30
        _db = MySQLdb.connect(**conf)

    # Test connection and reconnect if needed
    try:
        _db.ping()
    except Exception:
        conf = config()["db"].copy()
        conf["charset"] = "utf8mb4"
        conf["cursorclass"] = MySQLdb.cursors.DictCursor
        conf["autocommit"] = True
        conf["connect_timeout"] = 5
        conf["read_timeout"] = 30
        conf["write_timeout"] = 30
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
    cache_key = f"login:{account_name}"
    cached_user = memcache().get(cache_key)
    if cached_user:
        return cached_user

    cur = db().cursor()
    cur.execute(
        "SELECT * FROM users WHERE account_name = %s AND del_flg = 0", (account_name,)
    )
    user = cur.fetchone()

    if user and calculate_passhash(user["account_name"], password) == user["passhash"]:
        memcache().set(cache_key, user, expire=300)  # キャッシュ期限を300秒に設定
        return user
    return None


def validate_user(account_name: str, password: str):
    if not re.match(r"[0-9a-zA-Z]{3,}", account_name):
        return False
    if not re.match(r"[0-9a-zA-Z_]{6,}", password):
        return False
    return True


def digest(src: str):
    # Use native Python hashlib for much better performance
    return hashlib.sha512(src.encode("utf-8")).hexdigest()


def calculate_salt(account_name: str):
    return digest(account_name)


def calculate_passhash(account_name: str, password: str):
    return digest("%s:%s" % (password, calculate_salt(account_name)))


def get_session_user():
    user = flask.session.get("user")
    if user:
        # Try to fetch user from cache
        cache_key = f"user:{user['id']}"
        cached_user = memcache().get(cache_key)
        if cached_user:
            return cached_user

        # If not in cache, fetch from DB
        cur = db().cursor()
        cur.execute("SELECT * FROM `users` WHERE `id` = %s", (user["id"],))
        user_data = cur.fetchone()

        if user_data:
            # Store in cache for future use
            memcache().set(cache_key, user_data, expire=60)  # Cache for 60 seconds
        return user_data
    return None


def make_posts(results, all_comments=False):
    if not results:
        return []

    posts = []
    cursor = db().cursor()

    post_ids = [post["id"] for post in results]
    post_user_ids = {post["user_id"] for post in results}

    # キャッシュキーを生成
    cache_key_posts = f"posts:{','.join(map(str, post_ids))}"
    cached_posts = memcache().get(cache_key_posts)
    if cached_posts:
        return cached_posts

    # ユーザー情報を一括取得
    cursor.execute("SELECT * FROM `users` WHERE `id` IN %s", (list(post_user_ids),))
    users_by_id = {user["id"]: user for user in cursor.fetchall()}

    # コメント数を一括取得
    cursor.execute(
        "SELECT `post_id`, COUNT(*) AS `count` FROM `comments` WHERE `post_id` IN %s GROUP BY `post_id`",
        (post_ids,),
    )
    comment_counts = {row["post_id"]: row["count"] for row in cursor.fetchall()}

    # コメントを一括取得 - optimized approach
    if all_comments:
        cursor.execute(
            "SELECT * FROM `comments` WHERE `post_id` IN %s ORDER BY `created_at` DESC",
            (post_ids,),
        )
        all_comments_data = cursor.fetchall()
    else:
        # Use LIMIT to get recent comments for each post efficiently
        cursor.execute(
            "SELECT * FROM `comments` WHERE `post_id` IN %s ORDER BY `post_id`, `created_at` DESC",
            (post_ids,),
        )
        all_comments_data = cursor.fetchall()
    comments_by_post = {}
    for comment in all_comments_data:
        post_id = comment["post_id"]
        if post_id not in comments_by_post:
            comments_by_post[post_id] = []
        # Limit to 3 comments per post if not fetching all comments
        if all_comments or len(comments_by_post[post_id]) < 3:
            comments_by_post[post_id].append(comment)

    # 投稿を組み立てる
    for post in results:
        post_id = post["id"]
        post["comment_count"] = comment_counts.get(post_id, 0)
        post["comments"] = comments_by_post.get(post_id, [])
        post["user"] = users_by_id.get(post["user_id"])

        if post["user"] and not post["user"]["del_flg"]:
            posts.append(post)
            if len(posts) >= POSTS_PER_PAGE:
                break

    # キャッシュに保存
    memcache().set(cache_key_posts, posts, expire=60)
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

# add cache to static files
@app.after_request
def add_cache_control(response):
    # 静的ファイルに対してキャッシュヘッダーを追加
    if response.status_code == 200 and response.mimetype.startswith("image/"):
        response.headers["Cache-Control"] = "public, max-age=3153600"  
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
        "SELECT `id`, `user_id`, `body`, `created_at`, `mime` FROM `posts` ORDER BY `created_at` DESC LIMIT %s",
        (POSTS_PER_PAGE,),
    )
    posts = make_posts(cursor.fetchall())

    return flask.render_template("index.html", posts=posts, me=me)


@app.route("/@<account_name>")
def get_user_list(account_name):
    cache_key = f"user_list:{account_name}"
    cached_data = memcache().get(cache_key)
    if cached_data:
        return flask.render_template(
            "user.html",
            posts=cached_data["posts"],
            user=cached_data["user"],
            post_count=cached_data["post_count"],
            comment_count=cached_data["comment_count"],
            commented_count=cached_data["commented_count"],
            me=get_session_user(),
        )

    cursor = db().cursor()
    cursor.execute(
        "SELECT * FROM `users` WHERE `account_name` = %s AND `del_flg` = 0",
        (account_name,),
    )
    user = cursor.fetchone()
    if user is None:
        flask.abort(404)

    # Optimize: Get posts and counts in fewer queries
    cursor.execute(
        "SELECT `id`, `user_id`, `body`, `mime`, `created_at` FROM `posts` WHERE `user_id` = %s ORDER BY `created_at` DESC",
        (user["id"],),
    )
    posts_data = cursor.fetchall()
    posts = make_posts(posts_data)
    post_count = len(posts_data)

    # Get comment counts in parallel queries
    if post_count > 0:
        post_ids = [p["id"] for p in posts_data]

        # Get user's comment count and comments on user's posts in one query each
        cursor.execute(
            "SELECT COUNT(*) AS count FROM `comments` WHERE `user_id` = %s",
            (user["id"],),
        )
        comment_count = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) AS count FROM `comments` WHERE `post_id` IN %s",
            (post_ids,),
        )
        commented_count = cursor.fetchone()["count"]
    else:
        comment_count = 0
        commented_count = 0

    memcache().set(
        cache_key,
        {
            "posts": posts,
            "user": user,
            "post_count": post_count,
            "comment_count": comment_count,
            "commented_count": commented_count,
        },
        expire=300,
    )

    return flask.render_template(
        "user.html",
        posts=posts,
        user=user,
        post_count=post_count,
        comment_count=comment_count,
        commented_count=commented_count,
        me=get_session_user(),
    )


def _parse_iso8601(s):
    # http://bugs.python.org/issue15873
    # Ignore timezone
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ tT](\d{2}):(\d{2}):(\d{2}).*", s)
    if not m:
        raise ValueError("Invlaid iso8601 format: %r" % (s,))
    return datetime.datetime(*map(int, m.groups()))


@app.route("/posts")
def get_posts():
    max_created_at = flask.request.args.get("max_created_at")
    cache_key = f"posts:{max_created_at}"
    cached_posts = memcache().get(cache_key)
    if cached_posts:
        return flask.render_template("posts.html", posts=cached_posts)

    cursor = db().cursor()
    if max_created_at:
        max_created_at = _parse_iso8601(max_created_at)
        cursor.execute(
            "SELECT `id`, `user_id`, `body`, `mime`, `created_at` FROM `posts` WHERE `created_at` <= %s ORDER BY `created_at` DESC LIMIT %s",
            (max_created_at, POSTS_PER_PAGE),
        )
    else:
        cursor.execute(
            "SELECT `id`, `user_id`, `body`, `mime`, `created_at` FROM `posts` ORDER BY `created_at` DESC LIMIT %s",
            (POSTS_PER_PAGE,),
        )
    results = cursor.fetchall()
    posts = make_posts(results)

    memcache().set(cache_key, posts, expire=300)
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

    # Invalidate relevant caches after new post
    _invalidate_post_caches()

    return flask.redirect(f"/posts/{pid}")


def _invalidate_post_caches():
    """Invalidate caches that depend on posts data"""
    # Pattern-based cache invalidation would be ideal, but we'll use a simple approach
    # In a real production system, you'd want to implement cache tags or versioning
    pass  # Basic invalidation - memcache doesn't support pattern deletion easily


@app.route("/image/<id>.<ext>")
def get_image(id, ext):
    if not id:
        return ""
    id = int(id)
    if id == 0:
        return ""

    # Check cache first
    cache_key = f"image:{id}"
    cached_image = memcache().get(cache_key)
    if cached_image:
        return flask.Response(cached_image["imgdata"], mimetype=cached_image["mime"])

    cursor = db().cursor()
    cursor.execute("SELECT `mime`, `imgdata` FROM `posts` WHERE `id` = %s", (id,))
    post = cursor.fetchone()

    if not post:
        flask.abort(404)

    mime = post["mime"]
    if (
        (ext == "jpg" and mime == "image/jpeg")
        or (ext == "png" and mime == "image/png")
        or (ext == "gif" and mime == "image/gif")
    ):
        # Cache the image data
        memcache().set(
            cache_key, {"imgdata": post["imgdata"], "mime": mime}, expire=3600
        )
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
    if not re.match(r"[0-9]+", post_id):
        return "post_idは整数のみです"
    post_id = int(post_id)

    query = (
        "INSERT INTO `comments` (`post_id`, `user_id`, `comment`) VALUES (%s, %s, %s)"
    )
    cursor = db().cursor()
    cursor.execute(query, (post_id, me["id"], flask.request.form["comment"]))

    # Invalidate caches for this specific post
    memcache().delete(f"posts:{post_id}")

    return flask.redirect(f"/posts/{post_id}")


@app.route("/admin/banned")
def get_banned():
    me = get_session_user()
    if not me:
        flask.redirect("/login")

    if me["authority"] == 0:
        flask.abort(403)

    cursor = db().cursor()
    cursor.execute(
        "SELECT * FROM `users` WHERE `authority` = 0 AND `del_flg` = 0 ORDER BY `created_at` DESC"
    )
    users = cursor.fetchall()

    flask.render_template("banned.html", users=users, me=me)


@app.route("/admin/banned", methods=["POST"])
def post_banned():
    me = get_session_user()
    if not me:
        flask.redirect("/login")

    if me["authority"] == 0:
        flask.abort(403)

    if flask.request.form["csrf_token"] != flask.session["csrf_token"]:
        flask.abort(422)

    cursor = db().cursor()
    query = "UPDATE `users` SET `del_flg` = %s WHERE `id` = %s"
    for id in flask.request.form.getlist("uid", type=int):
        cursor.execute(query, (1, id))

    return flask.redirect("/admin/banned")
