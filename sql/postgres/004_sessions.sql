-- Persistent sign-in ("stay signed in after refresh").
-- The browser keeps a random token in a cookie; only its SHA-256 hash is stored.
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL DEFAULT app_user_id() REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS user_sessions_user ON user_sessions (user_id);
ALTER TABLE user_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_sessions_own ON user_sessions;
CREATE POLICY user_sessions_own ON user_sessions FOR ALL TO nse_app
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());
GRANT SELECT, INSERT, DELETE ON user_sessions TO nse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions TO nse_service;

-- Resume a session from its token hash (no user context yet). Expired or idle
-- sessions, and sessions of inactive/pending users, are never resumed.
CREATE OR REPLACE FUNCTION auth_resume(p_hash text, p_idle_hours int)
RETURNS TABLE (id uuid, email text, name text, role text, must_change_password boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    DELETE FROM user_sessions
    WHERE expires_at < now() OR last_seen < now() - make_interval(hours => p_idle_hours);
    RETURN QUERY
    UPDATE user_sessions s SET last_seen = now()
    FROM users u
    WHERE s.token_hash = p_hash AND u.id = s.user_id AND u.is_active AND NOT u.signup_pending
    RETURNING u.id, u.email, u.name, u.role, u.must_change_password;
END $$;
REVOKE ALL ON FUNCTION auth_resume(text, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_resume(text, int) TO nse_app;

-- Changing/resetting a password or deactivating an account signs out every
-- device of that user (enforced in the database, not only in the app).
CREATE OR REPLACE FUNCTION users_revoke_sessions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.password_hash IS DISTINCT FROM OLD.password_hash OR (OLD.is_active AND NOT NEW.is_active) THEN
        DELETE FROM user_sessions WHERE user_id = NEW.id;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS users_revoke_sessions ON users;
CREATE TRIGGER users_revoke_sessions AFTER UPDATE OF password_hash, is_active ON users
    FOR EACH ROW EXECUTE FUNCTION users_revoke_sessions();

INSERT INTO schema_version (version) VALUES (4) ON CONFLICT DO NOTHING;
