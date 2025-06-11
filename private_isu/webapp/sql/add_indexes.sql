-- Add performance indexes for the private_isu application

-- Index for posts table - frequently ordered by created_at DESC
CREATE INDEX idx_posts_created_at ON posts(created_at DESC);

-- Index for posts by user_id (for user pages)
CREATE INDEX idx_posts_user_id ON posts(user_id);

-- Index for comments by post_id (for fetching comments per post)
CREATE INDEX idx_comments_post_id ON comments(post_id);

-- Index for comments by user_id (for user comment counts)
CREATE INDEX idx_comments_user_id ON comments(user_id);

-- Compound index for comments by post_id and created_at (for ordering)
CREATE INDEX idx_comments_post_created ON comments(post_id, created_at DESC);

-- Index for users by del_flg (for filtering active users)
CREATE INDEX idx_users_del_flg ON users(del_flg);

-- Index for users by authority and del_flg (for admin queries)
CREATE INDEX idx_users_authority_del_flg ON users(authority, del_flg);
