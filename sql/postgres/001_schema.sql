-- NSE alert system: multi-user application database with ROW LEVEL SECURITY.
-- Run as the owner role (nse_owner) inside database nse_app:
--   python -m alert_system db-migrate
--
-- Roles (created by db-setup):
--   nse_owner   owns all objects; used only for migrations/admin CLI.
--   nse_app     dashboard; NOBYPASSRLS -> can only ever see/modify rows of the
--               user whose id is in the transaction setting app.user_id.
--   nse_service background scheduler/jobs; BYPASSRLS (must process every
--               user's rules and send every user's emails).
-- Market data (prices, rankings, recommendations) stays in ClickHouse and is
-- shared, read-only reference data - it contains no per-user information.

CREATE TABLE IF NOT EXISTS schema_version (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

-- The current application user for this transaction (NULL if unset).
CREATE OR REPLACE FUNCTION app_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

CREATE TABLE IF NOT EXISTS users (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email                text NOT NULL CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    name                 text NOT NULL DEFAULT '',
    password_hash        text NOT NULL,
    role                 text NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    is_active            boolean NOT NULL DEFAULT true,
    must_change_password boolean NOT NULL DEFAULT true,
    failed_logins        int NOT NULL DEFAULT 0,
    locked_until         timestamptz,
    prefs                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_login_at        timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users (lower(email));

CREATE TABLE IF NOT EXISTS holdings (
    id         bigserial PRIMARY KEY,
    user_id    uuid NOT NULL DEFAULT app_user_id() REFERENCES users(id) ON DELETE CASCADE,
    symbol     text NOT NULL,
    buy_date   date NOT NULL,
    buy_price  double precision NOT NULL CHECK (buy_price > 0),
    quantity   double precision NOT NULL CHECK (quantity > 0),
    notes      text NOT NULL DEFAULT '',
    status     text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SOLD')),
    sell_date  date,
    sell_price double precision,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS holdings_user ON holdings (user_id, status);

CREATE TABLE IF NOT EXISTS user_alerts (
    id         bigserial PRIMARY KEY,
    user_id    uuid NOT NULL DEFAULT app_user_id() REFERENCES users(id) ON DELETE CASCADE,
    symbol     text NOT NULL,
    metric     text NOT NULL CHECK (metric IN ('Daily', 'Weekly', 'Monthly')),
    condition  text NOT NULL CHECK (condition IN ('Above or equal', 'Below or equal')),
    threshold  double precision NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_alerts_user ON user_alerts (user_id, enabled);

CREATE TABLE IF NOT EXISTS watchlist (
    user_id  uuid NOT NULL DEFAULT app_user_id() REFERENCES users(id) ON DELETE CASCADE,
    symbol   text NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS alert_history (
    id              bigserial PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol          text NOT NULL,
    metric          text NOT NULL,
    condition       text NOT NULL,
    threshold       double precision NOT NULL,
    triggered_value double precision,
    triggered_at    timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'RESET')),
    trigger_price   double precision,
    trigger_session date
);
CREATE INDEX IF NOT EXISTS alert_history_active ON alert_history (user_id, symbol, metric, condition, threshold, status);
CREATE INDEX IF NOT EXISTS alert_history_time ON alert_history (user_id, triggered_at);

-- Email/WhatsApp outbox = delivery audit + dedup state. user_id NULL = system
-- (ops) message, invisible to every dashboard user.
CREATE TABLE IF NOT EXISTS notification_outbox (
    id                   bigserial PRIMARY KEY,
    user_id              uuid REFERENCES users(id) ON DELETE CASCADE,
    group_key            text NOT NULL,
    channel              text NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    recipient            text NOT NULL,
    subject              text,
    body                 text NOT NULL,
    body_html            text,
    dedup_key            text,
    status               text NOT NULL CHECK (status IN ('PENDING','SENDING','SENT','RETRY_SCHEDULED','FAILED')),
    attempts             int NOT NULL DEFAULT 0,
    max_attempts         int NOT NULL,
    next_attempt_at      timestamptz NOT NULL DEFAULT now(),
    send_mode            text,
    parts_sent           int NOT NULL DEFAULT 0,
    provider_message_ids text,
    last_error           text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    sent_at              timestamptz,
    UNIQUE (group_key, channel, recipient)
);
CREATE INDEX IF NOT EXISTS outbox_due ON notification_outbox (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS outbox_dedup ON notification_outbox (channel, recipient, dedup_key, created_at);
CREATE INDEX IF NOT EXISTS outbox_user ON notification_outbox (user_id, created_at);

-- Per-user "already delivered" markers (e.g. kind='mtd_rankings', ref=snapshot date).
CREATE TABLE IF NOT EXISTS delivery_markers (
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       text NOT NULL,
    ref        text NOT NULL,
    status     text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind, ref)
);

-- ---------------------------------------------------------------- RLS ---
ALTER TABLE users               ENABLE ROW LEVEL SECURITY;
ALTER TABLE holdings            ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_alerts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE watchlist           ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_history       ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE delivery_markers    ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS users_self ON users;
CREATE POLICY users_self ON users FOR ALL TO nse_app
    USING (id = app_user_id()) WITH CHECK (id = app_user_id());
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['holdings', 'user_alerts', 'watchlist', 'alert_history',
                             'notification_outbox', 'delivery_markers'] LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I_own ON %I', t, t);
        EXECUTE format('CREATE POLICY %I_own ON %I FOR ALL TO nse_app '
                       'USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id())', t, t);
    END LOOP;
END $$;

-- ------------------------------------------------------------- grants ---
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nse_app, nse_service;
-- dashboard: own rows only (RLS); cannot read password hashes of anyone (column grants)
GRANT SELECT (id, email, name, role, is_active, must_change_password, prefs, created_at, last_login_at)
    ON users TO nse_app;
GRANT UPDATE (name, password_hash, must_change_password, prefs) ON users TO nse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON holdings, user_alerts, watchlist TO nse_app;
GRANT SELECT ON alert_history, delivery_markers TO nse_app;
-- No direct INSERT: RLS checks user_id but not the recipient, so a user could
-- otherwise send mail to any address. Use request_email() (own address only).
REVOKE INSERT ON notification_outbox FROM nse_app;
GRANT SELECT ON notification_outbox TO nse_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO nse_app;
-- background service: everything (BYPASSRLS role)
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO nse_service;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO nse_service;

-- --------------------------------------------- auth / admin functions ---
-- Login lookup before any user context exists. Returns the hash only to
-- verify a password; the dashboard role has no direct access to the column.
CREATE OR REPLACE FUNCTION auth_get_user(p_email text)
RETURNS TABLE (id uuid, password_hash text, is_active boolean, locked_until timestamptz,
               role text, must_change_password boolean)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT id, password_hash, is_active, locked_until, role, must_change_password
    FROM users WHERE lower(email) = lower(p_email)
$$;

-- Record a login attempt: 5 consecutive failures lock the account 15 min.
CREATE OR REPLACE FUNCTION auth_record_login(p_id uuid, p_success boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF p_success THEN
        UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = now() WHERE id = p_id;
    ELSE
        UPDATE users SET failed_logins = failed_logins + 1,
               locked_until = CASE WHEN failed_logins + 1 >= 5 THEN now() + interval '15 minutes' END
        WHERE id = p_id;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION is_admin() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM users WHERE id = app_user_id() AND role = 'admin' AND is_active)
$$;

CREATE OR REPLACE FUNCTION admin_list_users()
RETURNS TABLE (id uuid, email text, name text, role text, is_active boolean, must_change_password boolean,
               failed_logins int, locked_until timestamptz, created_at timestamptz, last_login_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT is_admin() THEN RAISE EXCEPTION 'admin only' USING ERRCODE = '42501'; END IF;
    RETURN QUERY SELECT u.id, u.email, u.name, u.role, u.is_active, u.must_change_password, u.failed_logins,
                        u.locked_until, u.created_at, u.last_login_at FROM users u ORDER BY u.created_at;
END $$;

CREATE OR REPLACE FUNCTION admin_create_user(p_email text, p_name text, p_hash text, p_role text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE new_id uuid;
BEGIN
    IF NOT is_admin() THEN RAISE EXCEPTION 'admin only' USING ERRCODE = '42501'; END IF;
    INSERT INTO users (email, name, password_hash, role) VALUES (lower(p_email), p_name, p_hash, p_role)
    RETURNING users.id INTO new_id;
    RETURN new_id;
END $$;

CREATE OR REPLACE FUNCTION admin_update_user(p_id uuid, p_active boolean, p_role text, p_hash text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT is_admin() THEN RAISE EXCEPTION 'admin only' USING ERRCODE = '42501'; END IF;
    IF p_id = app_user_id() AND (p_active IS FALSE OR p_role = 'member') THEN
        RAISE EXCEPTION 'you cannot deactivate or demote yourself';
    END IF;
    UPDATE users SET is_active = COALESCE(p_active, is_active), role = COALESCE(p_role, role),
                     password_hash = COALESCE(p_hash, password_hash),
                     must_change_password = (p_hash IS NOT NULL) OR must_change_password,
                     failed_logins = CASE WHEN p_hash IS NOT NULL THEN 0 ELSE failed_logins END,
                     locked_until = CASE WHEN p_hash IS NOT NULL THEN NULL ELSE locked_until END
    WHERE id = p_id;
END $$;

-- Queue an email to the CALLER's own address (dashboard "send report now").
-- Max 10 requests per user per hour.
CREATE OR REPLACE FUNCTION request_email(p_group text, p_subject text, p_body text, p_html text, p_max_attempts int)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE me users%ROWTYPE; new_id bigint;
BEGIN
    SELECT * INTO me FROM users WHERE id = app_user_id() AND is_active;
    IF NOT FOUND THEN RAISE EXCEPTION 'not logged in' USING ERRCODE = '42501'; END IF;
    IF (SELECT count(*) FROM notification_outbox WHERE user_id = me.id AND group_key LIKE 'manual:%'
          AND created_at > now() - interval '1 hour') >= 10 THEN
        RAISE EXCEPTION 'rate limit: at most 10 manual emails per hour';
    END IF;
    INSERT INTO notification_outbox (user_id, group_key, channel, recipient, subject, body, body_html, status, max_attempts)
    VALUES (me.id, 'manual:' || p_group, 'email', me.email, p_subject, p_body, p_html, 'PENDING', p_max_attempts)
    ON CONFLICT DO NOTHING RETURNING id INTO new_id;
    RETURN new_id;
END $$;

REVOKE ALL ON FUNCTION auth_get_user(text), auth_record_login(uuid, boolean), is_admin(), admin_list_users(),
    admin_create_user(text, text, text, text), admin_update_user(uuid, boolean, text, text),
    request_email(text, text, text, text, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_get_user(text), auth_record_login(uuid, boolean), is_admin(), admin_list_users(),
    admin_create_user(text, text, text, text), admin_update_user(uuid, boolean, text, text),
    request_email(text, text, text, text, int) TO nse_app;
GRANT EXECUTE ON FUNCTION app_user_id() TO nse_app, nse_service;

INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
