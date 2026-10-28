-- Self sign-up with admin approval.
-- signup_pending = true: account requested, cannot sign in until an admin approves.
ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_pending boolean NOT NULL DEFAULT false;
GRANT SELECT (signup_pending) ON users TO nse_app;

-- auth_get_user gains signup_pending (return type change -> drop first)
DROP FUNCTION IF EXISTS auth_get_user(text);
CREATE FUNCTION auth_get_user(p_email text)
RETURNS TABLE (id uuid, password_hash text, is_active boolean, locked_until timestamptz,
               role text, must_change_password boolean, signup_pending boolean)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT id, password_hash, is_active, locked_until, role, must_change_password, signup_pending
    FROM users WHERE lower(email) = lower(p_email)
$$;

-- Called by the dashboard role WITHOUT a user context (sign-up form).
-- p_open = true  -> account active immediately (SIGNUP_MODE=open)
-- p_open = false -> pending until an admin approves (SIGNUP_MODE=approval)
-- Admins are emailed about every new request. Max 20 sign-ups per hour overall.
CREATE OR REPLACE FUNCTION auth_signup(p_email text, p_name text, p_hash text, p_open boolean,
                                       p_max_attempts int, p_dashboard_hint text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE new_id uuid; adm record;
BEGIN
    IF (SELECT count(*) FROM users WHERE created_at > now() - interval '1 hour') >= 20 THEN
        RAISE EXCEPTION 'too many sign-ups right now; please try again later';
    END IF;
    IF EXISTS (SELECT 1 FROM users WHERE lower(email) = lower(p_email)) THEN
        RAISE EXCEPTION 'an account with this email already exists' USING ERRCODE = '23505';
    END IF;
    INSERT INTO users (email, name, password_hash, role, is_active, must_change_password, signup_pending)
    VALUES (lower(p_email), left(p_name, 100), p_hash, 'member', p_open, false, NOT p_open)
    RETURNING id INTO new_id;
    FOR adm IN SELECT id, email FROM users WHERE role = 'admin' AND is_active LOOP
        INSERT INTO notification_outbox (user_id, group_key, channel, recipient, subject, body, status, max_attempts)
        VALUES (adm.id, 'signup:' || new_id, 'email', adm.email,
                CASE WHEN p_open THEN 'New user signed up: ' ELSE 'Sign-up awaiting your approval: ' END || lower(p_email),
                'Name: ' || left(p_name, 100) || E'\nEmail: ' || lower(p_email) || E'\n\n' ||
                CASE WHEN p_open THEN 'The account is active.'
                     ELSE 'Approve or reject it on the dashboard Users page. ' || coalesce(p_dashboard_hint, '') END,
                'PENDING', p_max_attempts)
        ON CONFLICT DO NOTHING;
    END LOOP;
    RETURN CASE WHEN p_open THEN 'active' ELSE 'pending' END;
END $$;

-- Admin approval / rejection of pending sign-ups.
CREATE OR REPLACE FUNCTION admin_review_signup(p_id uuid, p_approve boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT is_admin() THEN RAISE EXCEPTION 'admin only' USING ERRCODE = '42501'; END IF;
    IF p_approve THEN
        UPDATE users SET is_active = true, signup_pending = false WHERE id = p_id AND signup_pending;
    ELSE
        DELETE FROM users WHERE id = p_id AND signup_pending;
    END IF;
END $$;

-- admin_list_users gains signup_pending
DROP FUNCTION IF EXISTS admin_list_users();
CREATE FUNCTION admin_list_users()
RETURNS TABLE (id uuid, email text, name text, role text, is_active boolean, signup_pending boolean,
               must_change_password boolean, failed_logins int, locked_until timestamptz, created_at timestamptz,
               last_login_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT is_admin() THEN RAISE EXCEPTION 'admin only' USING ERRCODE = '42501'; END IF;
    RETURN QUERY SELECT u.id, u.email, u.name, u.role, u.is_active, u.signup_pending, u.must_change_password,
                        u.failed_logins, u.locked_until, u.created_at, u.last_login_at
                 FROM users u ORDER BY u.signup_pending DESC, u.created_at;
END $$;

-- An activated-by-admin account is no longer pending either.
CREATE OR REPLACE FUNCTION admin_update_user(p_id uuid, p_active boolean, p_role text, p_hash text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT is_admin() THEN RAISE EXCEPTION 'admin only' USING ERRCODE = '42501'; END IF;
    IF p_id = app_user_id() AND (p_active IS FALSE OR p_role = 'member') THEN
        RAISE EXCEPTION 'you cannot deactivate or demote yourself';
    END IF;
    UPDATE users SET is_active = COALESCE(p_active, is_active), role = COALESCE(p_role, role),
                     signup_pending = CASE WHEN p_active THEN false ELSE signup_pending END,
                     password_hash = COALESCE(p_hash, password_hash),
                     must_change_password = (p_hash IS NOT NULL) OR must_change_password,
                     failed_logins = CASE WHEN p_hash IS NOT NULL THEN 0 ELSE failed_logins END,
                     locked_until = CASE WHEN p_hash IS NOT NULL THEN NULL ELSE locked_until END
    WHERE id = p_id;
END $$;

REVOKE ALL ON FUNCTION auth_get_user(text), auth_signup(text, text, text, boolean, int, text),
    admin_review_signup(uuid, boolean), admin_list_users() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_get_user(text), auth_signup(text, text, text, boolean, int, text),
    admin_review_signup(uuid, boolean), admin_list_users() TO nse_app;

INSERT INTO schema_version (version) VALUES (3) ON CONFLICT DO NOTHING;
