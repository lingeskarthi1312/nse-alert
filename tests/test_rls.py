"""Row-level security + authentication, against the real PostgreSQL test
database (nse_app_test). These tests prove the DATABASE refuses cross-user
access even when application code asks for it."""
from datetime import date

import psycopg
import pytest

from alert_system.auth import (AuthError, admin_create_user, admin_reset_password, authenticate, change_password,
                               check_strength, hash_password, verify_password)
from alert_system.pgdb import anon_tx, service_tx, user_tx


def make_user(s, email, role="member", password="Password-123", active=True):
    with service_tx(s) as c:
        return c.execute("INSERT INTO users (email, name, password_hash, role, is_active, must_change_password) "
                         "VALUES (%s, %s, %s, %s, %s, false) RETURNING id",
                         (email, email.split("@")[0], hash_password(password), role, active)).fetchone()["id"]


def seed(s, uid, symbol):
    with user_tx(s, uid) as c:
        c.execute("INSERT INTO holdings (symbol, buy_date, buy_price, quantity) VALUES (%s, '2026-10-01', 100, 1)",
                  (symbol,))
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold) VALUES (%s,'Daily','Below or equal',5)",
                  (symbol,))
        c.execute("INSERT INTO watchlist (symbol) VALUES (%s)", (symbol,))
    with service_tx(s) as c:
        c.execute("INSERT INTO alert_history (user_id, symbol, metric, condition, threshold, triggered_value) "
                  "VALUES (%s, %s, 'Daily', 'Below or equal', 5, -6)", (uid, symbol))
        c.execute("INSERT INTO notification_outbox (user_id, group_key, channel, recipient, body, status, max_attempts) "
                  "VALUES (%s, %s, 'email', 'x@example.com', 'b', 'SENT', 3)", (uid, f"k-{symbol}"))
        c.execute("INSERT INTO delivery_markers (user_id, kind, ref, status) VALUES (%s, 'mtd_rankings', %s, 'SENT')",
                  (uid, symbol))


@pytest.fixture
def two_users(pg_settings):
    s = pg_settings()
    a, b = make_user(s, "alice@example.com", "admin"), make_user(s, "bob@example.com")
    seed(s, a, "ALICE.NS")
    seed(s, b, "BOB.NS")
    return s, a, b


TABLES = ("holdings", "user_alerts", "watchlist", "alert_history", "notification_outbox", "delivery_markers")


def test_each_user_sees_only_own_rows_in_every_table(two_users):
    s, a, b = two_users
    for uid, own in ((a, "ALICE.NS"), (b, "BOB.NS")):
        with user_tx(s, uid) as c:
            for t in TABLES:
                rows = c.execute(f"SELECT user_id FROM {t}").fetchall()  # no WHERE clause on purpose
                assert rows and {r["user_id"] for r in rows} == {uid}, t
            assert [r["symbol"] for r in c.execute("SELECT symbol FROM holdings")] == [own]
            users = c.execute("SELECT id, email FROM users").fetchall()
            assert [u["id"] for u in users] == [uid]


def test_no_user_context_sees_nothing(two_users):
    s, _, _ = two_users
    with anon_tx(s) as c:
        for t in TABLES:
            assert c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] == 0


def test_cannot_write_rows_for_another_user(two_users):
    s, a, b = two_users
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with user_tx(s, a) as c:
            c.execute("INSERT INTO holdings (user_id, symbol, buy_date, buy_price, quantity) "
                      "VALUES (%s, 'X.NS', '2026-10-01', 1, 1)", (b,))
    with user_tx(s, a) as c:
        assert c.execute("UPDATE holdings SET quantity = 999 WHERE symbol = 'BOB.NS'").rowcount == 0
        assert c.execute("DELETE FROM user_alerts WHERE symbol = 'BOB.NS'").rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(s, a) as c:  # moving own row to someone else is also refused
            c.execute("UPDATE holdings SET user_id = %s WHERE symbol = 'ALICE.NS'", (b,))
    with service_tx(s) as c:
        assert c.execute("SELECT quantity FROM holdings WHERE symbol='BOB.NS'").fetchone()["quantity"] == 1


def test_insert_defaults_to_current_user(two_users):
    s, a, _ = two_users
    with user_tx(s, a) as c:
        c.execute("INSERT INTO holdings (symbol, buy_date, buy_price, quantity) VALUES ('NEW.NS', %s, 5, 2)",
                  (date(2026, 10, 2),))
    with service_tx(s) as c:
        assert c.execute("SELECT user_id FROM holdings WHERE symbol='NEW.NS'").fetchone()["user_id"] == a


def test_password_hashes_and_privileged_columns_not_readable(two_users):
    s, a, _ = two_users
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(s, a) as c:
            c.execute("SELECT password_hash FROM users").fetchall()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(s, a) as c:
            c.execute("UPDATE users SET role = 'admin' WHERE id = app_user_id()")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(s, a) as c:  # dashboard role cannot fabricate alert history
            c.execute("INSERT INTO alert_history (user_id, symbol, metric, condition, threshold) "
                      "VALUES (app_user_id(), 'X', 'Daily', 'Below or equal', 1)")


def test_admin_functions_require_admin(two_users):
    s, a, b = two_users
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="admin only"):
        with user_tx(s, b) as c:
            c.execute("SELECT * FROM admin_list_users()").fetchall()
    with user_tx(s, a) as c:
        assert {u["email"] for u in c.execute("SELECT * FROM admin_list_users()")} == {"alice@example.com",
                                                                                      "bob@example.com"}
    with pytest.raises(psycopg.errors.RaiseException, match="yourself"):
        with user_tx(s, a) as c:
            c.execute("SELECT admin_update_user(%s, false, NULL, NULL)", (a,))


def test_service_role_sees_all_users(two_users):
    s, a, b = two_users
    with service_tx(s) as c:
        assert {r["user_id"] for r in c.execute("SELECT user_id FROM holdings")} == {a, b}


def test_authentication_flow_and_lockout(two_users):
    s, a, b = two_users
    assert authenticate(s, "ALICE@example.com", "Password-123")["id"] == a  # email case-insensitive
    with pytest.raises(AuthError, match="invalid"):
        authenticate(s, "nobody@example.com", "whatever-123")
    for _ in range(5):
        with pytest.raises(AuthError, match="invalid"):
            authenticate(s, "bob@example.com", "wrong-password-1")
    with pytest.raises(AuthError, match="locked"):
        authenticate(s, "bob@example.com", "Password-123")  # correct password, but locked


def test_inactive_user_cannot_log_in(pg_settings):
    s = pg_settings()
    make_user(s, "gone@example.com", active=False)
    with pytest.raises(AuthError, match="invalid"):
        authenticate(s, "gone@example.com", "Password-123")


def test_admin_invite_reset_and_change_password(two_users):
    s, a, _ = two_users
    temp = admin_create_user(s, a, "carol@example.com", "Carol")
    user = authenticate(s, "carol@example.com", temp)
    assert user["must_change_password"] is True
    change_password(s, user["id"], temp, "New-secret-42")
    assert authenticate(s, "carol@example.com", "New-secret-42")["must_change_password"] is False
    temp2 = admin_reset_password(s, a, user["id"])
    assert authenticate(s, "carol@example.com", temp2)["must_change_password"] is True
    with pytest.raises(AuthError):
        authenticate(s, "carol@example.com", "New-secret-42")


def test_password_hashing_and_policy():
    h = hash_password("Correct-horse-9")
    assert h.startswith("scrypt$") and verify_password("Correct-horse-9", h)
    assert not verify_password("wrong", h) and not verify_password("x", "garbage")
    assert hash_password("same-pass-1") != hash_password("same-pass-1")  # salted
    for bad in ("short1", "onlyletterslong", "1234567890123"):
        with pytest.raises(AuthError):
            check_strength(bad)
    check_strength("letters-and-123")


def test_dashboard_cannot_email_arbitrary_addresses(two_users):
    s, a, _ = two_users
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(s, a) as c:
            c.execute("INSERT INTO notification_outbox (user_id, group_key, channel, recipient, body, status, "
                      "max_attempts) VALUES (app_user_id(), 'x', 'email', 'victim@example.com', 'spam', 'PENDING', 1)")
    with user_tx(s, a) as c:
        c.execute("SELECT request_email('r1', 'Report', 'body', '<p>b</p>', 3)")
    with service_tx(s) as c:
        row = c.execute("SELECT recipient, user_id FROM notification_outbox WHERE group_key='manual:r1'").fetchone()
    assert row["recipient"] == "alice@example.com" and row["user_id"] == a  # always the caller's own address
    with pytest.raises(psycopg.errors.RaiseException, match="rate limit"):
        for i in range(11):
            with user_tx(s, a) as c:
                c.execute("SELECT request_email(%s, 's', 'b', NULL, 3)", (f"x{i}",))


def test_signup_requires_approval_and_notifies_admins(pg_settings):
    from alert_system.auth import signup
    s = pg_settings(SIGNUP_MODE="approval")
    admin = make_user(s, "boss@example.com", "admin")
    assert signup(s, "New.User@Example.com", "New User", "Fresh-pass-42") == "pending"
    with pytest.raises(AuthError, match="awaiting admin approval"):
        authenticate(s, "new.user@example.com", "Fresh-pass-42")
    with pytest.raises(AuthError, match="invalid email or password"):  # wrong password reveals nothing
        authenticate(s, "new.user@example.com", "Wrong-pass-42")
    with pytest.raises(AuthError, match="already exists"):
        signup(s, "new.user@example.com", "Again", "Fresh-pass-42")
    with service_tx(s) as c:
        note = c.execute("SELECT recipient, subject FROM notification_outbox WHERE group_key LIKE 'signup:%'").fetchone()
        new_id = c.execute("SELECT id FROM users WHERE email='new.user@example.com'").fetchone()["id"]
    assert note["recipient"] == "boss@example.com" and "awaiting your approval" in note["subject"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(s, new_id) as c:  # a pending user cannot approve themselves
            c.execute("SELECT admin_review_signup(%s, true)", (new_id,))
    with user_tx(s, admin) as c:
        c.execute("SELECT admin_review_signup(%s, true)", (new_id,))
    assert authenticate(s, "new.user@example.com", "Fresh-pass-42")["role"] == "member"


def test_signup_reject_open_and_closed_modes(pg_settings):
    from alert_system.auth import signup
    s = pg_settings(SIGNUP_MODE="approval")
    admin = make_user(s, "boss@example.com", "admin")
    signup(s, "spam@example.com", "Spam", "Spammy-pass-1")
    with service_tx(s) as c:
        sid = c.execute("SELECT id FROM users WHERE email='spam@example.com'").fetchone()["id"]
    with user_tx(s, admin) as c:
        c.execute("SELECT admin_review_signup(%s, false)", (sid,))
    with service_tx(s) as c:
        assert c.execute("SELECT count(*) AS n FROM users WHERE email='spam@example.com'").fetchone()["n"] == 0
    s_open = pg_settings(SIGNUP_MODE="open")
    assert signup(s_open, "open@example.com", "Open", "Open-pass-123") == "active"
    assert authenticate(s_open, "open@example.com", "Open-pass-123")["role"] == "member"  # never admin
    s_closed = pg_settings(SIGNUP_MODE="closed")
    with pytest.raises(AuthError, match="closed"):
        signup(s_closed, "x@example.com", "X", "Closed-pass-1")
    for bad_email, bad_pw in (("not-an-email", "Valid-pass-123"), ("ok@example.com", "short")):
        with pytest.raises(AuthError):
            signup(s_open, bad_email, "Name", bad_pw)


def test_persistent_sessions_resume_isolate_and_revoke(two_users):
    from alert_system.auth import create_session, end_session, resume_session
    s, a, b = two_users
    tok_a, tok_b = create_session(s, a), create_session(s, b)
    assert resume_session(s, tok_a)["id"] == a and resume_session(s, tok_b)["id"] == b
    assert resume_session(s, "forged-token") is None and resume_session(s, "") is None
    with service_tx(s) as c:  # only hashes are stored, never the token itself
        stored = {r["token_hash"] for r in c.execute("SELECT token_hash FROM user_sessions")}
    assert tok_a not in stored and len(stored) == 2
    with user_tx(s, a) as c:  # RLS: a user sees and can delete only their own sessions
        assert {r["user_id"] for r in c.execute("SELECT user_id FROM user_sessions")} == {a}
        assert c.execute("DELETE FROM user_sessions WHERE user_id = %s", (b,)).rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_tx(s, a) as c:  # cannot mint a session for someone else
            c.execute("INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES ('x', %s, now() + interval '1 day')", (b,))
    change_password(s, b, "Password-123", "Brand-new-pass-9")  # password change signs out every device
    assert resume_session(s, tok_b) is None
    with service_tx(s) as c:
        c.execute("UPDATE users SET is_active=false WHERE id=%s", (a,))  # deactivation too
    assert resume_session(s, tok_a) is None
    with service_tx(s) as c:
        c.execute("UPDATE users SET is_active=true WHERE id=%s", (a,))
    tok = create_session(s, a)
    end_session(s, a, tok)  # sign out
    assert resume_session(s, tok) is None


def test_expired_and_idle_sessions_not_resumed(two_users):
    from alert_system.auth import create_session, resume_session
    s, a, _ = two_users
    old, idle = create_session(s, a), create_session(s, a)
    with service_tx(s) as c:
        from alert_system.auth import _token_hash
        c.execute("UPDATE user_sessions SET expires_at = now() - interval '1 minute' WHERE token_hash=%s", (_token_hash(old),))
        c.execute("UPDATE user_sessions SET last_seen = now() - interval '13 hours' WHERE token_hash=%s", (_token_hash(idle),))
    assert resume_session(s, old) is None and resume_session(s, idle) is None
