"""
Real user accounts (Phase 17, master directive numbering: Authentication
& User Workspace) -- separate from, and additive to, auth.py's existing
pre-shared-key access control. auth.py decides *whether a request is let
in at all*; this decides *whose workspace a request acts on* (history,
preferences, usage, saved jobs, documents_converter/api/app.py's
/api/v1/account/* and /api/v1/jobs/{id}/save endpoints). Neither replaces
the other -- an unauthenticated or API-key-only caller keeps working
exactly as before, with no account attached to its jobs; see
JobRecord.user_id's own docstring (models.py) for how that stays
optional end to end.

Server-side session cookies, not a JWT -- a deliberate, explicitly-made
choice for this phase (this project's own dual audience, a browser page
and script/API callers, made either reasonable; a real login UI tipped
it toward cookies). Sessions are real rows (UserSession), not
self-contained tokens: only a SHA-256 hash of the raw cookie value is
ever stored, the same reasoning as never storing a plaintext password --
a database read shouldn't by itself hand over a usable session.

A cookie sent automatically by the browser on every request needs real
CSRF protection, unlike a bearer token a script has to attach by hand.
This module uses a signed double-submit cookie: at login/signup, the raw
session token and a second value -- HMAC-SHA256(SESSION_SECRET, raw
session token) -- are set as two separate cookies, only the first
HttpOnly. A same-origin page's JS can read the second (the browser's
Same-Origin Policy stops a cross-site attacker from reading it, even
though the attacker's forged request would still carry the *first*
cookie automatically) and must echo it back as the X-CSRF-Token header
on every mutating request; get_current_user() below recomputes the HMAC
from the presented session cookie and compares. No extra server-side
storage needed for the CSRF half -- it's derived, not stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, Response

from . import config
from .db import SessionLocal
from .models import User, UserSession

SESSION_COOKIE_NAME = "session_id"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"

# argon2id (the library's default) -- OWASP's current first recommendation
# for new password hashing, ahead of bcrypt: resistant to GPU/ASIC
# cracking in a way a fixed-cost algorithm designed in the 1990s isn't.
_hasher = PasswordHasher()

# A fixed, real argon2 hash of an arbitrary string (not any real
# account's password -- generated once, hardcoded) -- see login()'s own
# comment for why a nonexistent-email lookup still verifies against
# something real instead of short-circuiting immediately.
_DUMMY_PASSWORD_HASH = _hasher.hash("no-such-account-placeholder")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class InvalidEmailError(Exception):
    pass


class WeakPasswordError(Exception):
    pass


def _validate_email(email: str) -> str:
    """Normalizes to lowercase (so "A@B.com" and "a@b.com" are the same
    account) and does a minimal, deliberately non-exhaustive format
    check -- this phase never sends a verification email, so a perfect
    RFC-5322 validator would buy real complexity for very little actual
    benefit."""
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise InvalidEmailError("Not a valid email address.")
    return email


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise WeakPasswordError("Password must be at least 8 characters.")


@dataclass
class Account:
    """In-memory view of one user row, handed back to callers -- same
    reasoning as jobs.py's Job dataclass: app.py's code is written
    against this shape, not the SQLAlchemy model directly."""

    id: str
    email: str
    created_at: float
    preferences: dict = field(default_factory=dict)

    @classmethod
    def _from_record(cls, record: User) -> Account:
        try:
            preferences = json.loads(record.preferences_json)
        except (json.JSONDecodeError, TypeError):
            preferences = {}
        return cls(
            id=record.id, email=record.email, created_at=record.created_at, preferences=preferences
        )


# --------------------------------------------------------------------------
# Storage: signup/login/sessions/preferences
# --------------------------------------------------------------------------


class AccountStore:
    def signup(self, email: str, password: str) -> Account:
        email = _validate_email(email)
        _validate_password(password)
        with SessionLocal() as session:
            if session.query(User).filter(User.email == email).first() is not None:
                raise EmailAlreadyRegisteredError("An account with this email already exists.")
            record = User(
                id=secrets.token_hex(16),
                email=email,
                password_hash=_hasher.hash(password),
                created_at=time.time(),
                preferences_json="{}",
            )
            session.add(record)
            session.commit()
            return Account._from_record(record)

    def login(self, email: str, password: str) -> Account:
        """Raises InvalidCredentialsError for either a nonexistent email
        or a wrong password -- deliberately the same error either way,
        so a response never confirms whether a given email is
        registered at all."""
        email = _validate_email(email)
        with SessionLocal() as session:
            record = session.query(User).filter(User.email == email).first()
            # Always verify against *some* hash, real or a fixed dummy,
            # so a nonexistent email still pays the same real argon2
            # cost a wrong password does -- a login attempt's timing
            # shouldn't itself reveal whether an email is registered.
            password_hash = record.password_hash if record is not None else _DUMMY_PASSWORD_HASH
            try:
                _hasher.verify(password_hash, password)
            except VerifyMismatchError:
                raise InvalidCredentialsError("Incorrect email or password.") from None
            if record is None:
                # Unreachable in practice (the dummy hash above never
                # verifies for a real password) -- kept as a
                # defense-in-depth guard, not the real rejection path.
                raise InvalidCredentialsError("Incorrect email or password.")
            return Account._from_record(record)

    def get_account(self, user_id: str) -> Account | None:
        with SessionLocal() as session:
            record = session.get(User, user_id)
            return Account._from_record(record) if record is not None else None

    def set_preferences(self, user_id: str, preferences: dict) -> dict:
        with SessionLocal() as session:
            record = session.get(User, user_id)
            if record is None:
                raise ValueError(f"No such account: {user_id}")
            record.preferences_json = json.dumps(preferences)
            session.commit()
            return preferences

    def create_session(self, user_id: str) -> str:
        """Returns the raw session token (the only time it's ever
        available in full -- only its hash is stored). The caller sets
        this as the session cookie's value."""
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = time.time()
        with SessionLocal() as session:
            session.add(
                UserSession(
                    id=secrets.token_hex(16),
                    user_id=user_id,
                    token_hash=token_hash,
                    created_at=now,
                    expires_at=now + config.SESSION_MAX_AGE_SECONDS,
                    last_seen_at=now,
                )
            )
            session.commit()
        return raw_token

    def get_account_for_session(self, raw_token: str) -> Account | None:
        """Validates a raw session-cookie value: looks it up by hash,
        checks it hasn't expired, and touches last_seen_at. Returns None
        for anything invalid (unknown, expired) rather than
        distinguishing why -- a caller only ever needs "is this a valid
        session right now."""
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = time.time()
        with SessionLocal() as session:
            record = session.query(UserSession).filter(UserSession.token_hash == token_hash).first()
            if record is None:
                return None
            if record.expires_at < now:
                session.delete(record)
                session.commit()
                return None
            record.last_seen_at = now
            session.commit()
            user_record = session.get(User, record.user_id)
            return Account._from_record(user_record) if user_record is not None else None

    def delete_session(self, raw_token: str) -> None:
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        with SessionLocal() as session:
            session.query(UserSession).filter(UserSession.token_hash == token_hash).delete()
            session.commit()


accounts = AccountStore()


# --------------------------------------------------------------------------
# Cookies + CSRF
# --------------------------------------------------------------------------


def _compute_csrf_token(raw_session_token: str) -> str:
    return hmac.new(
        config.SESSION_SECRET.encode("utf-8"), raw_session_token.encode("ascii"), hashlib.sha256
    ).hexdigest()


def set_session_cookies(response: Response, raw_session_token: str) -> None:
    # secure=True would make the browser refuse to send this cookie over
    # plain http -- correct for a real deployment (always terminate TLS
    # in front of it), but this project's own local/dev default runs
    # over plain http on localhost, same tradeoff config.ENVIRONMENT
    # already governs for _check_startup_config's API_KEYS guard.
    secure = config.ENVIRONMENT == "production"
    max_age = int(config.SESSION_MAX_AGE_SECONDS)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        _compute_csrf_token(raw_session_token),
        max_age=max_age,
        httponly=False,  # deliberately JS-readable -- see module docstring
        secure=secure,
        samesite="lax",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_current_user(request: Request) -> Account:
    """
    Required-login dependency for /api/v1/account/* and
    /api/v1/jobs/{id}/save -- 401 with no valid session cookie, 403 on a
    mutating request whose X-CSRF-Token header doesn't match what the
    session cookie's own HMAC says it should be (see module docstring).
    GET/HEAD/OPTIONS never need the CSRF check -- they're expected to
    have no side effects in the first place.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Not logged in.")
    account = accounts.get_account_for_session(raw_token)
    if account is None:
        raise HTTPException(status_code=401, detail="Session is invalid or has expired.")

    if request.method not in _SAFE_METHODS:
        presented_csrf = request.headers.get(CSRF_HEADER_NAME, "")
        expected_csrf = _compute_csrf_token(raw_token)
        if not presented_csrf or not secrets.compare_digest(presented_csrf, expected_csrf):
            raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")

    return account


def get_current_user_optional(request: Request) -> Account | None:
    """
    Used only by POST /api/v1/jobs and /api/v1/batch, to optionally tag
    a new job/batch with whoever is logged in -- never required, and
    deliberately does NOT enforce the CSRF check get_current_user()
    does: the worst case of skipping it here is a job created without
    its owner correctly identified (falls back to anonymous, exactly
    like today), never a privileged action, since job/batch creation
    was already open to any caller regardless of login state. Enforcing
    CSRF here would only convert a forged cross-site job submission
    (already possible today, unauthenticated) into a *rejected* request
    instead of an *anonymous* one -- not a real security improvement,
    just an inconsistency with every other unauthenticated endpoint.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None
    return accounts.get_account_for_session(raw_token)
