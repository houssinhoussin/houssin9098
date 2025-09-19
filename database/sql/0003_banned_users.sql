-- 0003_banned_users.sql
CREATE TABLE IF NOT EXISTS banned_users (
  user_id BIGINT PRIMARY KEY,
  reason TEXT,
  banned_by BIGINT NOT NULL,
  banned_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_banned_until ON banned_users(banned_until);
