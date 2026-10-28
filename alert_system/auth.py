"""Password hashing (scrypt, stdlib) and login/admin helpers.

Hashes: "scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>" (16-byte random salt).
Login failures are counted in the database; 5 in a row lock the account for
15 minutes (auth_record_login). Unknown emails take the same time as wrong
passwords (a dummy hash is checked) so accounts cannot be enumerated.
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

from .pgdb import anon_tx, user_tx

N, R, P, DKLEN = 2 ** 14, 8, 1, 32
MIN_LENGTH = 10


class AuthError(Exception):
    pass


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    b64 = base64.b64encode
    return f"scrypt${N}${R}${P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password, stored):
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc, base64.b64decode(digest))


_DUMMY = hash_password(secrets.token_urlsafe(16))


def check_strength(password):
    if len(password) < MIN_LENGTH:
        raise AuthError(f"password must be at least {MIN_LENGTH} characters")
    if password.isdigit() or password.isalpha():
        raise AuthError("password must mix letters with digits or symbols")


def temp_password():
    return secrets.token_urlsafe(9) + "7"


def authenticate(settings, email, password):
    """Return {'id','role','must_change_password'} or raise AuthError."""
    email = (email or "").strip().lower()
    with anon_tx(settings) as conn:
        row = conn.execute("SELECT * FROM auth_get_user(%s)", (email,)).fetchone()
        if row is None:
            verify_password(password, _DUMMY)
            raise AuthError("invalid email or password")
        if row["locked_until"] and row["locked_until"] > datetime.now(timezone.utc):
            raise AuthError("too many failed attempts; account locked for 15 minutes")
        pw_ok = verify_password(password, row["password_hash"])
        ok = pw_ok and row["is_active"]
        conn.execute("SELECT auth_record_login(%s, %s)", (row["id"], ok))
    if pw_ok and row["signup_pending"]:
        # only someone who knows the password learns the account is pending
        raise AuthError("your sign-up is awaiting admin approval; you will be able to sign in once approved")
    if not ok:
        raise AuthError("invalid email or password")
    return {"id": row["id"], "role": row["role"], "must_change_password": row["must_change_password"]}


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_DAYS = 7          # absolute lifetime of a "stay signed in" session
SESSION_IDLE_HOURS = 12   # ends after this long without any activity


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(settings, user_id):
    """New persistent session for the browser cookie. Returns the raw token
    (only its hash is stored, so a database leak cannot be replayed)."""
    token = secrets.token_urlsafe(32)
    with user_tx(settings, user_id) as conn:
        conn.execute("INSERT INTO user_sessions (token_hash, expires_at) "
                     "VALUES (%s, now() + make_interval(days => %s))", (_token_hash(token), SESSION_DAYS))
    return token


def resume_session(settings, token):
    """User dict for a valid cookie token, else None (expired, idle, revoked,
    deactivated or pending accounts are never resumed)."""
    if not token or len(token) > 200:
        return None
    with anon_tx(settings) as conn:
        row = conn.execute("SELECT * FROM auth_resume(%s, %s)", (_token_hash(token), SESSION_IDLE_HOURS)).fetchone()
    return dict(row) if row else None


def end_session(settings, user_id, token):
    if token:
        with user_tx(settings, user_id) as conn:
            conn.execute("DELETE FROM user_sessions WHERE token_hash=%s", (_token_hash(token),))


def signup(settings, email, name, password):
    """Self sign-up. Returns 'pending' (admin approval needed) or 'active'.
    Raises AuthError for invalid input, duplicates, rate limits or when closed."""
    if settings.signup_mode == "closed":
        raise AuthError("sign-up is closed; ask the administrator for an invitation")
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise AuthError("enter a valid email address")
    if not (name or "").strip():
        raise AuthError("enter your name")
    check_strength(password)
    import psycopg
    hint = f"Open {settings.public_url} -> Users." if settings.public_url else ""
    try:
        with anon_tx(settings) as conn:
            return conn.execute("SELECT auth_signup(%s, %s, %s, %s, %s, %s) AS r",
                                (email, name.strip(), hash_password(password), settings.signup_mode == "open",
                                 settings.report.email_max_attempts, hint)).fetchone()["r"]
    except psycopg.errors.UniqueViolation as exc:
        raise AuthError("an account with this email already exists. Use the Sign in tab with its password - "
                        "for an account created by the administrator that is the temporary password they gave "
                        "you - or ask the administrator for a password reset") from exc
    except psycopg.errors.RaiseException as exc:
        raise AuthError(str(exc).split("\n")[0]) from exc


def change_password(settings, user_id, current, new):
    check_strength(new)
    with user_tx(settings, user_id) as conn:
        email = conn.execute("SELECT email FROM users WHERE id = app_user_id()").fetchone()["email"]
    authenticate(settings, email, current)
    with user_tx(settings, user_id) as conn:
        conn.execute("UPDATE users SET password_hash=%s, must_change_password=false WHERE id = app_user_id()",
                     (hash_password(new),))


def admin_create_user(settings, admin_id, email, name, role="member"):
    """Returns the one-time temporary password for the new user."""
    pw = temp_password()
    with user_tx(settings, admin_id) as conn:
        conn.execute("SELECT admin_create_user(%s, %s, %s, %s)", (email.strip().lower(), name, hash_password(pw), role))
    return pw


def admin_reset_password(settings, admin_id, user_id):
    pw = temp_password()
    with user_tx(settings, admin_id) as conn:
        conn.execute("SELECT admin_update_user(%s, NULL, NULL, %s)", (user_id, hash_password(pw)))
    return pw
