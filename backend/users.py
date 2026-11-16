"""Account management (v1.0.4 slice 2): admins create and look after sellers.

* Only admins reach any of this (``auth.require_admin``); the app-wide gate
  has already authenticated the caller, checked CSRF and any pending
  password change.
* Admins create *seller* accounts only; there is no way to add roles or
  promote/demote accounts.
* Usernames are stored normalised exactly as sign-in normalises them, so each
  person has one distinct login whatever case or spacing they type.
* Every new or reset account gets a generated temporary password that is
  returned once in the ``201``/``200`` response (``Cache-Control: no-store``),
  stored only as a hash, never logged, and must be changed at first sign-in.
* Accounts are deactivated, never deleted. Deactivation and password resets
  end the account's sessions at once. The final active admin can never be
  deactivated, and admins cannot deactivate themselves.
* Reactivation policy: the account keeps its current password and any pending
  forced change; because its sessions were revoked, it must sign in again.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from . import auth
    from .database import LoginAttempt, User, UserSession, get_db, utc_now
else:  # pragma: no cover - flat module layout (PyInstaller)
    import auth
    from database import LoginAttempt, User, UserSession, get_db, utc_now


#: Unambiguous symbols (no 0/O, 1/I): 32 symbols, 5 bits each.
TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
#: 16 symbols x 5 bits = 80 bits of entropy, shown as XXXX-XXXX-XXXX-XXXX.
TEMP_PASSWORD_SYMBOLS = 16
_TEMP_PASSWORD_GROUP = 4

#: Normalised usernames: 3-64 letters (any script), digits, ``.``, ``_``,
#: ``-`` or ``@``. Whitespace, slashes and other punctuation are refused.
USERNAME_PATTERN = re.compile(r"[\w.@-]{3,64}")

NO_STORE = {"Cache-Control": "no-store"}


def generate_temporary_password() -> str:
    symbols = "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(TEMP_PASSWORD_SYMBOLS))
    return "-".join(
        symbols[index:index + _TEMP_PASSWORD_GROUP]
        for index in range(0, TEMP_PASSWORD_SYMBOLS, _TEMP_PASSWORD_GROUP)
    )


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=256)

    @field_validator("username")
    @classmethod
    def normalise_username(cls, value: str) -> str:
        normalised = auth.normalize_username(value)
        if not USERNAME_PATTERN.fullmatch(normalised):
            raise ValueError(
                "Username must be 3-64 letters, digits, '.', '_', '-' or '@', without spaces"
            )
        return normalised


class UserStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: StrictBool


class UserOut(BaseModel):
    """The only account fields ever returned: no hashes, no secrets."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    is_active: bool
    must_change_password: bool
    created_at: datetime


AdminContext = Annotated[auth.AuthContext, Depends(auth.require_admin)]


async def would_remove_last_admin(db: AsyncSession, user: User) -> bool:
    """True when deactivating ``user`` would leave no active admin."""
    if user.role != "admin" or not user.is_active:
        return False
    others = await db.scalar(
        select(func.count()).select_from(User).where(
            User.role == "admin", User.is_active.is_(True), User.id != user.id
        )
    )
    return not others


async def _get_user_or_404(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _end_sessions(db: AsyncSession, user_id: int) -> None:
    await db.execute(delete(UserSession).where(UserSession.user_id == user_id))


def _issued(user: User, temporary: str, status_code: int) -> JSONResponse:
    payload = {"user": UserOut.model_validate(user).model_dump(mode="json"), "temporary_password": temporary}
    return JSONResponse(payload, status_code=status_code, headers=NO_STORE)


def create_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/users", response_class=HTMLResponse, include_in_schema=False)
    async def users_page(request: Request, db: AsyncSession = Depends(get_db), lang: str = "en") -> Response:
        language = "ar" if lang == "ar" else "en"
        context = {"lang": language, "nav_page": "users"}
        if not auth.is_admin(request):
            return templates.TemplateResponse(
                request=request, name="users.html", status_code=403,
                context={**context, "forbidden": True}, headers=NO_STORE,
            )
        accounts = (await db.scalars(select(User).order_by(User.id))).all()
        return templates.TemplateResponse(
            request=request,
            name="users.html",
            context={
                **context,
                "forbidden": False,
                "accounts": [UserOut.model_validate(account) for account in accounts],
                "current_user_id": request.state.auth.user_id,
            },
            headers=NO_STORE,
        )

    @router.get("/api/users", response_model=list[UserOut])
    async def list_users(_: AdminContext, db: AsyncSession = Depends(get_db)) -> list[User]:
        return list((await db.scalars(select(User).order_by(User.id))).all())

    @router.post("/api/users", status_code=201, response_model=None)
    async def create_seller(data: UserCreate, _: AdminContext, db: AsyncSession = Depends(get_db)) -> JSONResponse:
        if await db.scalar(select(User.id).where(User.username == data.username)) is not None:
            raise HTTPException(status_code=409, detail="Username is already taken")
        temporary = generate_temporary_password()
        user = User(
            username=data.username,
            password_hash=auth.hash_password(temporary),
            role="seller",
            is_active=True,
            must_change_password=True,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:  # a concurrent create took the name first
            await db.rollback()
            raise HTTPException(status_code=409, detail="Username is already taken") from None
        await db.refresh(user)
        return _issued(user, temporary, 201)

    @router.patch("/api/users/{user_id}", response_model=UserOut)
    async def set_user_active(
        user_id: int, data: UserStatusUpdate, actor: AdminContext, db: AsyncSession = Depends(get_db),
    ) -> User:
        user = await _get_user_or_404(db, user_id)
        if data.is_active:
            user.is_active = True
            await db.commit()
            return user
        if user.id == actor.user_id:
            raise HTTPException(status_code=409, detail="You cannot deactivate your own account")
        if await would_remove_last_admin(db, user):
            raise HTTPException(status_code=409, detail="The last active admin cannot be deactivated")
        # Single conditional statement, so two concurrent requests can never
        # deactivate the final active admins between them.
        remaining_admins = (
            select(func.count()).select_from(User)
            .where(User.role == "admin", User.is_active.is_(True), User.id != user_id)
            .scalar_subquery()
        )
        result = await db.execute(
            update(User)
            .where(User.id == user_id)
            .where((User.role != "admin") | (remaining_admins >= 1))
            .values(is_active=False, updated_at=utc_now())
        )
        if result.rowcount != 1:
            await db.rollback()
            raise HTTPException(status_code=409, detail="The last active admin cannot be deactivated")
        await _end_sessions(db, user_id)
        await db.commit()
        await db.refresh(user)
        return user

    @router.post("/api/users/{user_id}/reset-password", response_model=None)
    async def reset_seller_password(user_id: int, _: AdminContext, db: AsyncSession = Depends(get_db)) -> JSONResponse:
        user = await _get_user_or_404(db, user_id)
        if user.role != "seller":
            raise HTTPException(
                status_code=409,
                detail="Only seller passwords can be reset; admins change their own password",
            )
        temporary = generate_temporary_password()
        user.password_hash = auth.hash_password(temporary)
        user.must_change_password = True
        user.password_changed_at = utc_now()
        await _end_sessions(db, user.id)
        # Lift any sign-in lockout on this username so the new password is usable.
        await db.execute(delete(LoginAttempt).where(
            LoginAttempt.scope == "username", LoginAttempt.key_hash == auth.username_attempt_key(user.username)
        ))
        await db.commit()
        await db.refresh(user)
        return _issued(user, temporary, 200)

    return router
