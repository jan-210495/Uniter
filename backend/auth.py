"""Authentication foundation: accounts, server-side sessions, CSRF, throttling.

Design notes
------------
* Passwords are hashed with salted ``scrypt`` (stdlib); plaintext is never
  stored, logged or echoed back.
* Sessions are server-side rows. The browser only holds a random 256-bit
  token; the database stores its SHA-256 digest. Nothing is signed, so there
  is no signing secret to ship, configure or leak.
* Every session has its own CSRF token: HMAC-SHA256 keyed by the session
  token, so it is unforgeable without the HttpOnly cookie, rotates with the
  session and is never stored or logged. It is required on every
  state-changing request: as the ``X-CSRF-Token`` header for
  the app's ``fetch`` calls, or as the ``csrf_token`` field for HTML forms. The
  sign-in form, which runs before a session exists, uses a double-submit
  cookie. A cross-origin ``Origin`` header is always rejected.
* A single app-wide dependency (:func:`authorize_request`) classifies every
  routed request, so new routes are protected by default.
* There is no shared default credential. A database with no account and no
  initialisation marker shows a one-time ``/setup`` page where the first
  person to open the app chooses the ``admin`` password. Setup runs under a
  write lock (``BEGIN IMMEDIATE``) and records ``INITIALIZED_MARKER`` in
  ``schema_migrations`` in the same transaction as the account, so it happens
  exactly once and can never be reopened. An initialised database without an
  active admin fails closed (503) instead of reopening setup; recovery needs
  local access to the database (see the README).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from .database import LoginAttempt, User, UserSession, get_db
else:  # pragma: no cover - flat module layout (PyInstaller)
    from database import LoginAttempt, User, UserSession, get_db


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

SESSION_COOKIE = "uniter_session"
LOGIN_CSRF_COOKIE = "uniter_login_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"

SESSION_IDLE_TIMEOUT = timedelta(hours=4)
SESSION_ABSOLUTE_LIFETIME = timedelta(hours=12)
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)
LOGIN_FORM_LIFETIME = timedelta(hours=1)

THROTTLE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_USERNAME = 5
MAX_FAILURES_PER_ADDRESS = 20

USERNAME_MAX_LENGTH = 150
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256
#: Upper bound on submitted credentials; longer input is rejected unhashed.
_SUBMITTED_SECRET_MAX_LENGTH = 1024
_FORM_BODY_MAX_BYTES = 16 * 1024

#: Username of the account created by first-run setup (fixed in this slice).
INITIAL_ADMIN_USERNAME = "admin"
AUTH_SCHEMA_VERSION = "v1.0.4-auth-foundation"
#: Recorded when the database is initialised: by ``/setup`` (with the first
#: account, atomically) or at startup when accounts already exist. Once
#: present, setup never opens again, even if every account later disappears.
INITIALIZED_MARKER = "v1.0.4-auth-initialized"
#: Written by earlier v1.0.4 review builds that created a bootstrap account.
#: Only read: such a database counts as initialised and keeps its accounts.
LEGACY_BOOTSTRAP_MARKER = "v1.0.4-auth-bootstrap-admin"
_INITIALISED_MARKERS = (INITIALIZED_MARKER, LEGACY_BOOTSTRAP_MARKER)

#: First-run states, see :func:`setup_state`.
SETUP_REQUIRED = "setup-required"
SETUP_READY = "ready"
SETUP_LOCKED = "locked"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Routed paths reachable without a session. ``/static`` is a mounted
#: sub-application and never passes through route dependencies.
PUBLIC_PATHS = frozenset({"/", "/api/health", "/manifest.webmanifest", "/login", "/logout", "/setup"})
#: Paths a session that must change its password may still use.
PENDING_CHANGE_PATHS = frozenset({"/change-password"})

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; every stored value is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _csrf_for(session_token: str) -> str:
    mac = hmac.new(session_token.encode("utf-8"), b"uniter-csrf-v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64encode(salt)}${_b64encode(derived)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        n_value, r_value, p_value = int(n), int(r), int(p)
        if scheme != "scrypt" or not (2 <= n_value <= 2 ** 20 and 1 <= r_value <= 32 and 1 <= p_value <= 16):
            return False
        salt_bytes, expected_bytes = _b64decode(salt), _b64decode(expected)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt_bytes, n=n_value, r=r_value, p=p_value,
            dklen=len(expected_bytes), maxmem=256 * 1024 * 1024,
        )
    except (AttributeError, ValueError, TypeError, binascii.Error, MemoryError):
        return False
    return hmac.compare_digest(derived, expected_bytes)


_dummy_hash: str | None = None


def _burn_password_check(password: str) -> None:
    """Spend the same work as a real check so timing does not reveal accounts."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


def normalize_username(raw: str) -> str:
    return unicodedata.normalize("NFKC", raw).strip().casefold()


def new_password_problem(username: str, new: str, confirm: str) -> str | None:
    """Message key for an unacceptable new password (length, match, username)."""
    if len(new) < PASSWORD_MIN_LENGTH:
        return "too_short"
    if len(new) > PASSWORD_MAX_LENGTH:
        return "too_long"
    if new != confirm:
        return "mismatch"
    if normalize_username(new) == username:
        return "too_weak"
    return None


def password_problem(user: User, current: str, new: str, confirm: str) -> str | None:
    """Return a message key for an unacceptable new password, else ``None``."""
    problem = new_password_problem(user.username, new, confirm)
    if problem in {"too_short", "too_long", "mismatch"}:
        return problem
    if new == current:
        return "unchanged"
    return problem


# ---------------------------------------------------------------------------
# Schema, initialisation state and one-time setup
# ---------------------------------------------------------------------------

_AUTH_TABLES = (User.__table__, UserSession.__table__, LoginAttempt.__table__)
_SCHEMA_MIGRATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations "
    "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)


async def _has_marker(connection, versions=_INITIALISED_MARKERS) -> bool:
    has_table = (await connection.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ))).scalar() is not None
    if not has_table:
        return False
    placeholders = ", ".join(f":v{index}" for index in range(len(versions)))
    return (await connection.execute(
        text(f"SELECT 1 FROM schema_migrations WHERE version IN ({placeholders}) LIMIT 1"),
        {f"v{index}": version for index, version in enumerate(versions)},
    )).scalar() is not None


async def migrate_auth_schema(target_engine) -> bool:
    """Install the auth tables under a write lock; never create an account.

    Follows the guarded-migration contract of the other startup steps: an
    uninitialised file (no ``transactions`` table) is left untouched, and the
    whole step commits atomically or not at all. A database that already has
    accounts (or was bootstrapped by an earlier v1.0.4 build) is recorded as
    initialised, so first-run setup can never open on it.
    """
    async with target_engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            tables = set((await connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).scalars())
            if "transactions" not in tables:
                await connection.rollback()
                return False
            changed = not {table.name for table in _AUTH_TABLES} <= tables
            await connection.run_sync(
                lambda sync: User.metadata.create_all(sync, tables=list(_AUTH_TABLES), checkfirst=True)
            )
            await connection.exec_driver_sql(_SCHEMA_MIGRATIONS_DDL)
            await connection.execute(
                text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"),
                {"version": AUTH_SCHEMA_VERSION},
            )
            if not await _has_marker(connection, (INITIALIZED_MARKER,)):
                accounts = (await connection.execute(select(func.count()).select_from(User))).scalar_one()
                if accounts or await _has_marker(connection, (LEGACY_BOOTSTRAP_MARKER,)):
                    await connection.execute(
                        text("INSERT INTO schema_migrations(version) VALUES (:version)"),
                        {"version": INITIALIZED_MARKER},
                    )
                    changed = True
            await connection.commit()
            return changed
        except Exception:
            await connection.rollback()
            raise


async def setup_state(db: AsyncSession) -> str:
    """Classify the database for first-run handling (read-only).

    * ``SETUP_READY`` — at least one active admin exists;
    * ``SETUP_REQUIRED`` — no account and no initialisation marker: ``/setup``;
    * ``SETUP_LOCKED`` — initialised (accounts or a marker) but no active admin:
      fail closed; setup is never reopened as an unauthenticated takeover.
    """
    active_admin = await db.scalar(
        select(User.id).where(User.role == "admin", User.is_active.is_(True)).limit(1)
    )
    if active_admin is not None:
        return SETUP_READY
    if await db.scalar(select(User.id).limit(1)) is not None:
        return SETUP_LOCKED
    connection = await db.connection()
    return SETUP_LOCKED if await _has_marker(connection) else SETUP_REQUIRED


async def initialize_admin(target_engine, password_hash: str) -> str | None:
    """Create the first admin and its session atomically, exactly once.

    Runs under ``BEGIN IMMEDIATE`` and re-checks inside the lock, so of two
    concurrent submissions exactly one succeeds. Returns the new session
    token, or ``None`` (writing nothing) when the database is already
    initialised. Only the given hash is stored; the password never reaches
    this function.
    """
    async with target_engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            accounts = (await connection.execute(select(func.count()).select_from(User))).scalar_one()
            if accounts or await _has_marker(connection):
                await connection.rollback()
                return None
            now = utc_now()
            result = await connection.execute(User.__table__.insert().values(
                username=INITIAL_ADMIN_USERNAME,
                password_hash=password_hash,
                role="admin",
                is_active=True,
                must_change_password=False,
                password_changed_at=now,
                created_at=now,
                updated_at=now,
            ))
            user_id = result.inserted_primary_key[0]
            await connection.exec_driver_sql(_SCHEMA_MIGRATIONS_DDL)
            await connection.execute(
                text("INSERT INTO schema_migrations(version) VALUES (:version)"),
                {"version": INITIALIZED_MARKER},
            )
            token = secrets.token_urlsafe(32)
            await connection.execute(UserSession.__table__.insert().values(
                token_hash=_digest(token),
                user_id=user_id,
                created_at=now,
                last_seen_at=now,
                expires_at=now + SESSION_ABSOLUTE_LIFETIME,
            ))
            await connection.commit()
            return token
        except Exception:
            await connection.rollback()
            raise


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthContext:
    """Plain, detached values for the authenticated request."""

    session_id: int
    user_id: int
    username: str
    role: str
    must_change_password: bool
    csrf_token: str


def _session_alive(record: UserSession, now: datetime) -> bool:
    return _aware(record.expires_at) > now and _aware(record.last_seen_at) + SESSION_IDLE_TIMEOUT > now


async def _purge_expired_sessions(db: AsyncSession, now: datetime) -> None:
    await db.execute(delete(UserSession).where(or_(
        UserSession.expires_at <= now,
        UserSession.last_seen_at <= now - SESSION_IDLE_TIMEOUT,
    )))


async def _start_session(db: AsyncSession, user: User) -> str:
    """Create a fresh session (new id, hence new CSRF token); the caller commits."""
    token = secrets.token_urlsafe(32)
    now = utc_now()
    db.add(UserSession(
        token_hash=_digest(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + SESSION_ABSOLUTE_LIFETIME,
    ))
    return token


async def load_session(db: AsyncSession, token: str | None) -> AuthContext | None:
    if not token or len(token) > 128:
        return None
    row = (await db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == _digest(token))
    )).first()
    now = utc_now()
    if row is None:
        return None
    record, user = row
    if not _session_alive(record, now) or not user.is_active:
        await db.execute(delete(UserSession).where(UserSession.id == record.id))
        await db.commit()
        return None
    context = AuthContext(
        session_id=record.id,
        user_id=user.id,
        username=user.username,
        role=user.role,
        must_change_password=bool(user.must_change_password),
        csrf_token=_csrf_for(token),
    )
    if _aware(record.last_seen_at) + SESSION_TOUCH_INTERVAL <= now:
        try:
            await db.execute(
                update(UserSession).where(UserSession.id == record.id).values(last_seen_at=now)
            )
            await db.commit()
        except OperationalError:  # a busy database must not fail the request
            await db.rollback()
    return context


def cookie_secure(request: Request) -> bool:
    """``Secure`` follows the scheme unless explicitly configured.

    The desktop app serves plain HTTP on the local network, where a ``Secure``
    cookie would never be sent back; HTTPS deployments (directly or through a
    proxy that uvicorn trusts) get ``Secure`` automatically.
    """
    configured = os.environ.get("UNITER_COOKIE_SECURE", "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    return request.url.scheme == "https"


def _set_cookie(response: Response, request: Request, name: str, value: str, max_age: timedelta) -> None:
    response.set_cookie(
        name, value, max_age=int(max_age.total_seconds()), path="/",
        secure=cookie_secure(request), httponly=True, samesite="lax",
    )


def _clear_cookie(response: Response, request: Request, name: str) -> None:
    response.delete_cookie(name, path="/", secure=cookie_secure(request), httponly=True, samesite="lax")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def _tokens_match(supplied: str | None, expected: str | None) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _form_token(request: Request, *, rotate: bool = False) -> str:
    """Double-submit token for the pre-session forms (sign-in and setup)."""
    existing = request.cookies.get(LOGIN_CSRF_COOKIE)
    if existing and 32 <= len(existing) <= 64 and not rotate:
        return existing
    return secrets.token_urlsafe(32)


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # non-browser client or same-origin navigation; the token still applies
    host = request.headers.get("host", "")
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return bool(parts.netloc) and parts.netloc.lower() == host.lower()


async def read_form(request: Request) -> dict[str, str]:
    """Parse a URL-encoded body once (bounded); other bodies yield ``{}``."""
    cached = getattr(request.state, "auth_form", None)
    if cached is not None:
        return cached
    form: dict[str, str] = {}
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        body = await request.body()
        if len(body) > _FORM_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=20)
        form = {key: values[0] for key, values in parsed.items()}
    request.state.auth_form = form
    return form


async def verify_csrf(request: Request, expected: str) -> None:
    if not _origin_allowed(request):
        raise HTTPException(status_code=403, detail="CSRF check failed")
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied:
        supplied = (await read_form(request)).get(CSRF_FIELD)
    if not _tokens_match(supplied, expected):
        raise HTTPException(status_code=403, detail="CSRF check failed")


def csrf_token_for(request: Request) -> str:
    """Jinja helper: the CSRF token of the request's session, if any."""
    context = getattr(request.state, "auth", None)
    return context.csrf_token if isinstance(context, AuthContext) else ""


def is_admin(request: Request) -> bool:
    """Jinja/route helper: whether the request's session belongs to an admin.

    The role comes from the database on every request (``load_session``), so a
    role or activation change takes effect on the very next request.
    """
    context = getattr(request.state, "auth", None)
    return isinstance(context, AuthContext) and context.role == "admin"


async def require_admin(request: Request) -> AuthContext:
    """Route dependency: refuse anyone but an admin, before the body is used.

    Runs after the app-wide gate, so the caller is already authenticated, has
    passed CSRF and has no pending password change. The refusal is identical
    whatever the target resource, so it reveals nothing about it.
    """
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    return request.state.auth


# ---------------------------------------------------------------------------
# Request authorisation
# ---------------------------------------------------------------------------

class AuthRedirect(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


async def auth_redirect_handler(request: Request, exc: AuthRedirect) -> Response:
    return RedirectResponse(exc.location, status_code=303, headers={"Cache-Control": "no-store"})


def _language(request: Request, form: dict[str, str] | None = None) -> str:
    value = (form or {}).get("lang") or request.query_params.get("lang")
    return "ar" if value == "ar" else "en"


def _with_lang(path: str, lang: str) -> str:
    return f"{path}?lang=ar" if lang == "ar" else path


def safe_next(value: str | None) -> str | None:
    """Accept only local absolute paths for post-login redirects."""
    if not value or len(value) > 2048 or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path in {"/login", "/logout"}:
        return None
    return value


def _login_location(request: Request) -> str:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    params = {"next": target}
    if _language(request) == "ar":
        params["lang"] = "ar"
    return f"/login?{urlencode(params)}"


async def authorize_request(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    """App-wide gate: every routed request is classified before it runs."""
    path = request.url.path
    if path in PUBLIC_PATHS:
        return
    unsafe = request.method not in SAFE_METHODS
    api = path.startswith("/api/")
    state = await setup_state(db)
    if state != SETUP_READY:
        await db.commit()
        if state == SETUP_LOCKED:
            # Initialised but no active admin: fail closed for everyone, even
            # existing sessions, until access is recovered locally.
            if api or unsafe:
                raise HTTPException(status_code=503, detail="Service unavailable")
            raise AuthRedirect(_with_lang("/setup", _language(request)))
        # Before setup no account exists, so nobody can be signed in.
        if api or unsafe:
            raise HTTPException(status_code=401, detail="Authentication required")
        raise AuthRedirect(_with_lang("/setup", _language(request)))
    context = await load_session(db, request.cookies.get(SESSION_COOKIE))
    # End the gate's read transaction so the route starts on a clean session.
    await db.commit()
    if context is None:
        if api or unsafe:
            raise HTTPException(status_code=401, detail="Authentication required")
        raise AuthRedirect(_login_location(request))
    request.state.auth = context
    if unsafe:
        await verify_csrf(request, context.csrf_token)
    if context.must_change_password and path not in PENDING_CHANGE_PATHS:
        if api or unsafe:
            raise HTTPException(status_code=403, detail="Password change required")
        raise AuthRedirect(_with_lang("/change-password", _language(request)))


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

def username_attempt_key(username: str) -> str:
    """Digest under which failed sign-ins for a normalised username are counted."""
    return _digest(f"username:{username}")


def _attempt_keys(username: str, address: str) -> tuple[str, str]:
    return username_attempt_key(username), _digest(f"address:{address}")


async def _is_throttled(db: AsyncSession, username_key: str, address_key: str, now: datetime) -> bool:
    since = now - THROTTLE_WINDOW
    counts = dict((await db.execute(
        select(LoginAttempt.scope, func.count())
        .where(LoginAttempt.attempted_at > since)
        .where(or_(
            (LoginAttempt.scope == "username") & (LoginAttempt.key_hash == username_key),
            (LoginAttempt.scope == "address") & (LoginAttempt.key_hash == address_key),
        ))
        .group_by(LoginAttempt.scope)
    )).all())
    return (
        counts.get("username", 0) >= MAX_FAILURES_PER_USERNAME
        or counts.get("address", 0) >= MAX_FAILURES_PER_ADDRESS
    )


async def _record_failure(db: AsyncSession, username_key: str, address_key: str, now: datetime) -> None:
    await db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at <= now - THROTTLE_WINDOW))
    db.add_all([
        LoginAttempt(scope="username", key_hash=username_key, attempted_at=now),
        LoginAttempt(scope="address", key_hash=address_key, attempted_at=now),
    ])


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

LOGIN_MESSAGES = {
    "invalid": ("Incorrect username or password.", "اسم المستخدم أو كلمة المرور غير صحيحة."),
    "throttled": (
        "Too many failed sign-in attempts. Please wait a few minutes and try again.",
        "محاولات تسجيل دخول فاشلة كثيرة. يرجى الانتظار بضع دقائق ثم المحاولة مجدداً.",
    ),
    "expired": (
        "This sign-in form expired. Please try again.",
        "انتهت صلاحية نموذج تسجيل الدخول. يرجى المحاولة مجدداً.",
    ),
}

PASSWORD_MESSAGES = {
    "wrong_current": ("The current password is incorrect.", "كلمة المرور الحالية غير صحيحة."),
    "too_short": (
        f"The new password must be at least {PASSWORD_MIN_LENGTH} characters.",
        f"يجب ألا تقل كلمة المرور الجديدة عن {PASSWORD_MIN_LENGTH} أحرف.",
    ),
    "too_long": (
        f"The new password must be at most {PASSWORD_MAX_LENGTH} characters.",
        f"يجب ألا تزيد كلمة المرور الجديدة عن {PASSWORD_MAX_LENGTH} حرفاً.",
    ),
    "mismatch": ("The new passwords do not match.", "كلمتا المرور الجديدتان غير متطابقتين."),
    "unchanged": (
        "The new password must be different from the current one.",
        "يجب أن تختلف كلمة المرور الجديدة عن الحالية.",
    ),
    "too_weak": (
        "Choose a password that is not the username.",
        "اختر كلمة مرور مختلفة عن اسم المستخدم.",
    ),
    "throttled": LOGIN_MESSAGES["throttled"],
}

SETUP_MESSAGES = {
    "expired": (
        "This setup form expired. Please try again.",
        "انتهت صلاحية نموذج الإعداد. يرجى المحاولة مجدداً.",
    ),
    "too_short": PASSWORD_MESSAGES["too_short"],
    "too_long": PASSWORD_MESSAGES["too_long"],
    "mismatch": PASSWORD_MESSAGES["mismatch"],
    "too_weak": PASSWORD_MESSAGES["too_weak"],
}


def _message(table: dict[str, tuple[str, str]], key: str | None, lang: str) -> str | None:
    if key is None:
        return None
    english, arabic = table[key]
    return arabic if lang == "ar" else english


def create_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def render_login(
        request: Request, lang: str, *, status: int = 200, error: str | None = None,
        username: str = "", next_path: str | None = None,
    ) -> Response:
        token = _form_token(request, rotate=error == "expired")
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=status,
            context={
                "lang": lang,
                "csrf_token": token,
                "next_path": next_path or "",
                "username": username[:USERNAME_MAX_LENGTH],
                "error": _message(LOGIN_MESSAGES, error, lang),
            },
            headers={"Cache-Control": "no-store"},
        )
        _set_cookie(response, request, LOGIN_CSRF_COOKIE, token, LOGIN_FORM_LIFETIME)
        return response

    def render_setup(
        request: Request, lang: str, *, mode: str = "form", status: int = 200, error: str | None = None,
    ) -> Response:
        """``mode``: ``form`` (choose the password), ``done`` (already set up)
        or ``locked`` (initialised without an active admin; fail closed)."""
        token = _form_token(request, rotate=error == "expired") if mode == "form" else ""
        response = templates.TemplateResponse(
            request=request,
            name="setup.html",
            status_code=status,
            context={
                "lang": lang,
                "mode": mode,
                "csrf_token": token,
                "username": INITIAL_ADMIN_USERNAME,
                "error": _message(SETUP_MESSAGES, error, lang),
                "min_length": PASSWORD_MIN_LENGTH,
                "max_length": PASSWORD_MAX_LENGTH,
            },
            headers={"Cache-Control": "no-store"},
        )
        if mode == "form":
            _set_cookie(response, request, LOGIN_CSRF_COOKIE, token, LOGIN_FORM_LIFETIME)
        return response

    async def first_run_redirect(request: Request, db: AsyncSession, lang: str) -> Response | None:
        """Send every sign-in to ``/setup`` until the database is ready."""
        state = await setup_state(db)
        await db.commit()
        if state == SETUP_READY:
            return None
        return RedirectResponse(_with_lang("/setup", lang), status_code=303,
                                headers={"Cache-Control": "no-store"})

    def render_change(
        request: Request, context: AuthContext, lang: str, *, status: int = 200, error: str | None = None,
    ) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            status_code=status,
            context={
                "lang": lang,
                "csrf_token": context.csrf_token,
                "forced": context.must_change_password,
                "username": context.username,
                "error": _message(PASSWORD_MESSAGES, error, lang),
                "min_length": PASSWORD_MIN_LENGTH,
            },
            headers={"Cache-Control": "no-store"},
        )

    def signed_in_redirect(request: Request, token: str, location: str) -> Response:
        response = RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})
        _set_cookie(response, request, SESSION_COOKIE, token, SESSION_ABSOLUTE_LIFETIME)
        _clear_cookie(response, request, LOGIN_CSRF_COOKIE)
        return response

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        lang = _language(request)
        if (redirect := await first_run_redirect(request, db, lang)) is not None:
            return redirect
        next_path = safe_next(request.query_params.get("next"))
        context = await load_session(db, request.cookies.get(SESSION_COOKIE))
        if context is not None:
            location = (
                _with_lang("/change-password", lang) if context.must_change_password
                else next_path or _with_lang("/pos", lang)
            )
            return RedirectResponse(location, status_code=303)
        return render_login(request, lang, next_path=next_path)

    @router.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        form = await read_form(request)
        lang = _language(request, form)
        if (redirect := await first_run_redirect(request, db, lang)) is not None:
            return redirect  # nothing to sign in to yet (or locked): never processed
        next_path = safe_next(form.get("next"))
        raw_username = form.get("username", "")
        if not _origin_allowed(request) or not _tokens_match(
            form.get(CSRF_FIELD), request.cookies.get(LOGIN_CSRF_COOKIE)
        ):
            return render_login(request, lang, status=403, error="expired",
                                username=raw_username, next_path=next_path)

        username = normalize_username(raw_username)[:USERNAME_MAX_LENGTH]
        password = form.get("password", "")
        username_key, address_key = _attempt_keys(username, _client_address(request))
        now = utc_now()
        if await _is_throttled(db, username_key, address_key, now):
            await db.rollback()
            return render_login(request, lang, status=429, error="throttled",
                                username=raw_username, next_path=next_path)

        user = None
        if username and len(raw_username) <= USERNAME_MAX_LENGTH * 4:
            user = await db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
        if len(password) > _SUBMITTED_SECRET_MAX_LENGTH:
            password = ""
            user = None
        if len(password) < PASSWORD_MIN_LENGTH:
            # Every password the app accepts meets the minimum, so a shorter
            # one can only be the retired shared bootstrap default of an
            # earlier review build: refuse it like any wrong password.
            password = ""
        if user is not None and password and verify_password(password, user.password_hash):
            await db.execute(delete(LoginAttempt).where(
                LoginAttempt.scope == "username", LoginAttempt.key_hash == username_key
            ))
            presented = request.cookies.get(SESSION_COOKIE)
            if presented and len(presented) <= 128:  # never keep a pre-login session id
                await db.execute(delete(UserSession).where(UserSession.token_hash == _digest(presented)))
            await _purge_expired_sessions(db, now)
            token = await _start_session(db, user)
            location = (
                _with_lang("/change-password", lang) if user.must_change_password
                else next_path or _with_lang("/pos", lang)
            )
            await db.commit()
            return signed_in_redirect(request, token, location)

        if user is None or not password:
            _burn_password_check(password)
        await _record_failure(db, username_key, address_key, now)
        await db.commit()
        return render_login(request, lang, status=401, error="invalid",
                            username=raw_username, next_path=next_path)

    @router.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        lang = _language(request)
        state = await setup_state(db)
        await db.commit()
        if state == SETUP_READY:
            return RedirectResponse(_with_lang("/login", lang), status_code=303,
                                    headers={"Cache-Control": "no-store"})
        if state == SETUP_LOCKED:
            return render_setup(request, lang, mode="locked", status=503)
        return render_setup(request, lang)

    @router.post("/setup", response_class=HTMLResponse)
    async def setup_submit(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        form = await read_form(request)
        lang = _language(request, form)
        state = await setup_state(db)
        await db.commit()
        if state == SETUP_READY:
            return render_setup(request, lang, mode="done", status=409)
        if state == SETUP_LOCKED:
            return render_setup(request, lang, mode="locked", status=503)
        if not _origin_allowed(request) or not _tokens_match(
            form.get(CSRF_FIELD), request.cookies.get(LOGIN_CSRF_COOKIE)
        ):
            return render_setup(request, lang, status=403, error="expired")

        new = form.get("new_password", "")
        confirm = form.get("confirm_password", "")
        problem = new_password_problem(INITIAL_ADMIN_USERNAME, new, confirm)
        if problem is not None:
            return render_setup(request, lang, status=400, error=problem)

        # Hash outside the write lock; the lock itself is held only for the
        # re-check and the three inserts.
        token = await initialize_admin(db.bind, hash_password(new))
        if token is None:  # another submission initialised the database first
            return render_setup(request, lang, mode="done", status=409)
        return signed_in_redirect(request, token, _with_lang("/pos", lang))

    @router.post("/logout")
    async def logout(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        form = await read_form(request)
        lang = _language(request, form)
        context = await load_session(db, request.cookies.get(SESSION_COOKIE))
        if context is not None:
            await verify_csrf(request, context.csrf_token)
            await db.execute(delete(UserSession).where(UserSession.id == context.session_id))
            await db.commit()
        response = RedirectResponse(_with_lang("/login", lang), status_code=303,
                                    headers={"Cache-Control": "no-store"})
        _clear_cookie(response, request, SESSION_COOKIE)
        return response

    @router.get("/change-password", response_class=HTMLResponse)
    async def change_password_page(request: Request) -> Response:
        return render_change(request, request.state.auth, _language(request))

    @router.post("/change-password", response_class=HTMLResponse)
    async def change_password_submit(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
        # The gate already proved the session and its CSRF token.
        context: AuthContext = request.state.auth
        form = await read_form(request)
        lang = _language(request, form)
        current = form.get("current_password", "")
        new = form.get("new_password", "")
        confirm = form.get("confirm_password", "")
        user = await db.get(User, context.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Authentication required")

        username_key, address_key = _attempt_keys(user.username, _client_address(request))
        now = utc_now()
        if await _is_throttled(db, username_key, address_key, now):
            await db.rollback()
            return render_change(request, context, lang, status=429, error="throttled")
        if len(current) > _SUBMITTED_SECRET_MAX_LENGTH or not verify_password(current, user.password_hash):
            await _record_failure(db, username_key, address_key, now)
            await db.commit()
            return render_change(request, context, lang, status=400, error="wrong_current")
        problem = password_problem(user, current, new, confirm)
        if problem is not None:
            await db.rollback()
            return render_change(request, context, lang, status=400, error=problem)

        user.password_hash = hash_password(new)
        user.must_change_password = False
        user.password_changed_at = now
        # Every existing session of this account ends, including this one.
        await db.execute(delete(UserSession).where(UserSession.user_id == user.id))
        token = await _start_session(db, user)
        await db.commit()
        return signed_in_redirect(request, token, _with_lang("/pos", lang))

    return router


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    """Register auth routes, the redirect handler and template helpers."""
    templates.env.globals["csrf_token_for"] = csrf_token_for
    templates.env.globals["is_admin"] = is_admin
    app.add_exception_handler(AuthRedirect, auth_redirect_handler)
    app.include_router(create_router(templates))
