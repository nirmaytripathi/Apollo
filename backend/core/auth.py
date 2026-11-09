"""Accounts for the web app: register, log in, and JWT session tokens.

- Passwords are hashed with scrypt (Python's hashlib; no extra dependency) and a random salt per user.
  The plain password is never stored or logged.
- Users live in the `users` table of the app database (db.py: Neon Postgres via DATABASE_URL, or local SQLite).
- A session is a JWT (HS256, signed with config.JWT_SECRET) that expires after JWT_TTL_HOURS. api.py
  puts it in an HttpOnly cookie, so page scripts cannot read it and the live event stream and the
  contradiction-map iframe are authenticated without exposing the token in URLs.
- Failed logins are rate-limited per email (LOGIN_MAX_FAILURES in LOGIN_WINDOW_SECONDS).
"""

import hashlib
import hmac
import re
import secrets
import threading
import time
import jwt
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from core import config, db

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
MIN_PASSWORD_LENGTH = 8
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

# Used only when JWT_SECRET is not set: valid until the process exits.
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)
_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


class AuthError(Exception):
    """A user-facing problem: `status` is the HTTP status, `message` is safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def secret() -> str:
    return config.JWT_SECRET or _EPHEMERAL_SECRET


# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


# ---------------------------------------------------------------- user store


def _public(row) -> dict:
    return {"id": str(row.id), "email": row.email, "name": row.name, "created_at": row.created_at}


def _find(**where):
    (column, value), = where.items()
    with db.engine().connect() as conn:
        return conn.execute(select(db.users).where(getattr(db.users.c, column) == value)).first()


def register(name: str, email: str, password: str) -> dict:
    name, email = name.strip(), email.strip().lower()
    if not 1 <= len(name) <= 80:
        raise AuthError(422, "Enter your name (up to 80 characters).")
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError(422, "Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(422, f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 256:
        raise AuthError(422, "Password is too long.")
    exists = AuthError(409, "An account with this email already exists. Sign in instead.")
    if _find(email=email):
        raise exists
    try:
        with db.engine().begin() as conn:
            new_id = conn.execute(
                insert(db.users).values(email=email, name=name, password_hash=hash_password(password),
                                        created_at=int(time.time() * 1000)).returning(db.users.c.id)
            ).scalar_one()
    except IntegrityError:  # registered by a concurrent request a moment ago (unique email)
        raise exists from None
    return _public(_find(id=new_id))


def login(email: str, password: str) -> dict:
    email = email.strip().lower()
    now = time.time()
    with _lock:
        recent = [t for t in _failures.get(email, []) if now - t < LOGIN_WINDOW_SECONDS]
        _failures[email] = recent
    if len(recent) >= LOGIN_MAX_FAILURES:
        raise AuthError(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
    row = _find(email=email)
    # Same message whether the email or the password is wrong, so emails cannot be probed.
    if row is None or not verify_password(password, row.password_hash):
        with _lock:
            _failures.setdefault(email, []).append(now)
        raise AuthError(401, "Incorrect email or password.")
    with _lock:
        _failures.pop(email, None)
    return _public(row)


def get_user(user_id: str) -> dict | None:
    try:
        row = _find(id=int(user_id))
    except ValueError:
        return None
    return _public(row) if row else None


# ---------------------------------------------------------------- tokens


def create_token(user: dict) -> str:
    now = int(time.time())
    claims = {"sub": user["id"], "email": user["email"], "name": user["name"], "iat": now,
              "exp": now + config.JWT_TTL_HOURS * 3600, "iss": "apollo"}
    return jwt.encode(claims, secret(), algorithm="HS256")


def user_from_token(token: str) -> dict | None:
    """The user a valid, unexpired token belongs to, or None. The account must still exist."""
    try:
        claims = jwt.decode(token, secret(), algorithms=["HS256"], issuer="apollo", options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    return get_user(str(claims["sub"]))
