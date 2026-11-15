"""Operator SIM cards and the Operators page (v1.0.4 slice 4).

A SIM card is one physical SIM/card account of a company (MTN or Syriatel).
The companies themselves stay the ``operators`` configuration rows with their
services and USSD settings; SIM cards never change them.

Visibility (enforced in every query here, not just in the page):

* admins see every SIM card, active or archived;
* a seller sees active *shared* SIM cards and *their own* private SIM cards
  (active or archived). Anything else answers 404 on every route, exactly
  like an id that does not exist, so its existence is not revealed.

Management:

* a seller adds private SIM cards for themselves, or shared ones;
* an admin adds private SIM cards for any active account, or shared ones;
* the owner of a private SIM card, or an admin, edits its name/phone,
  archives and restores it; only an admin edits, archives or restores a shared
  SIM card, and only an admin changes owner or visibility;
* the company of a SIM card never changes; SIM cards are archived, never
  deleted (there is no DELETE route).

Money is out of scope for this slice: SIM cards have no balance. Sales,
top-ups and refunds keep using each company's shop credit
(``operator_balance``), which the Operators page shows as such.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, case, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

if __package__:
    from . import auth
    from .database import (
        SIM_NAME_MAX_LENGTH, Operator, OperatorBalance, OperatorTopup, SimCard, User, get_db, utc_now,
    )
    from .validation import validate_syrian_phone
else:  # pragma: no cover - flat module layout (PyInstaller)
    import auth
    from database import (
        SIM_NAME_MAX_LENGTH, Operator, OperatorBalance, OperatorTopup, SimCard, User, get_db, utc_now,
    )
    from validation import validate_syrian_phone

NO_STORE = {"Cache-Control": "no-store"}
#: Companies are always presented in this order (MTN first, Syriatel second).
COMPANY_ORDER = ("mtn", "syriatel")

NOT_FOUND = "SIM card not found"
NOT_ALLOWED = "You are not allowed to change this SIM card"
ADMIN_ONLY_ASSIGNMENT = "Only an admin can change who owns or can use a SIM card"
OWN_ONLY = "You can only add private SIM cards for yourself"
OWNER_REQUIRED = "Choose the active account that owns this private SIM card"
SHARED_HAS_NO_OWNER = "A shared SIM card has no owner"
ARCHIVED = "Restore this SIM card before editing it"
UNKNOWN_COMPANY = "Unknown company"


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not 1 <= len(cleaned) <= SIM_NAME_MAX_LENGTH:
        raise ValueError(f"Display name must be 1-{SIM_NAME_MAX_LENGTH} characters")
    return cleaned


def _clean_phone(value: str | None) -> str | None:
    return None if value is None else validate_syrian_phone(value)


class SimCreate(BaseModel):
    """Unknown fields (money, creator, ...) are refused with 422."""

    model_config = ConfigDict(extra="forbid")

    operator_code: str = Field(min_length=1, max_length=50)
    display_name: str = Field(max_length=4 * SIM_NAME_MAX_LENGTH)
    phone: str = Field(max_length=32)
    visibility: Literal["private", "shared"]
    owner_user_id: int | None = Field(default=None, ge=1, le=9223372036854775807)

    @field_validator("display_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _clean_name(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _clean_phone(value)


class SimUpdate(BaseModel):
    """Only the fields sent are changed; the company can never change."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=4 * SIM_NAME_MAX_LENGTH)
    phone: str | None = Field(default=None, max_length=32)
    visibility: Literal["private", "shared"] | None = None
    owner_user_id: int | None = Field(default=None, ge=1, le=9223372036854775807)

    @field_validator("display_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _clean_name(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return _clean_phone(value)


class SimOut(BaseModel):
    id: int
    operator_code: str
    operator_name: str
    display_name: str
    phone: str
    visibility: Literal["private", "shared"]
    owner_user_id: int | None
    owner_username: str | None
    status: Literal["active", "archived"]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_edit: bool
    can_archive: bool
    can_restore: bool


def _context(request: Request) -> auth.AuthContext:
    context = getattr(request.state, "auth", None)
    if context is None:  # the app-wide gate always sets it; fail closed anyway
        raise HTTPException(status_code=401, detail="Authentication required")
    return context


Context = Annotated[auth.AuthContext, Depends(_context)]
Owner = aliased(User)


def _visible(context: auth.AuthContext):
    """SQL filter: the SIM cards this account may see at all."""
    if context.role == "admin":
        return true()
    return or_(
        and_(SimCard.is_shared.is_(True), SimCard.archived_at.is_(None)),
        and_(SimCard.is_shared.is_(False), SimCard.owner_user_id == context.user_id),
    )


def _may_manage(context: auth.AuthContext, is_shared: bool, owner_user_id: int | None) -> bool:
    return context.role == "admin" or (not is_shared and owner_user_id == context.user_id)


def _select_sims(context: auth.AuthContext):
    company_rank = case(
        *((Operator.code == code, rank) for rank, code in enumerate(COMPANY_ORDER)), else_=len(COMPANY_ORDER)
    )
    return (
        select(SimCard, Operator.code, Operator.name, Owner.username)
        .join(Operator, Operator.id == SimCard.operator_id)
        .outerjoin(Owner, Owner.id == SimCard.owner_user_id)
        .where(_visible(context))
        .order_by(company_rank, SimCard.archived_at.is_not(None), func.lower(SimCard.display_name), SimCard.id)
    )


def _out(row, context: auth.AuthContext) -> SimOut:
    sim, operator_code, operator_name, owner_username = row
    manage = _may_manage(context, sim.is_shared, sim.owner_user_id)
    archived = sim.archived_at is not None
    return SimOut(
        id=sim.id,
        operator_code=operator_code,
        operator_name=operator_name,
        display_name=sim.display_name,
        phone=sim.phone,
        visibility="shared" if sim.is_shared else "private",
        owner_user_id=sim.owner_user_id,
        owner_username=owner_username,
        status="archived" if archived else "active",
        archived_at=sim.archived_at,
        created_at=sim.created_at,
        updated_at=sim.updated_at,
        can_edit=manage and not archived,
        can_archive=manage and not archived,
        can_restore=manage and archived,
    )


async def _visible_sim(db: AsyncSession, context: auth.AuthContext, sim_id: int):
    row = (await db.execute(
        _select_sims(context).where(SimCard.id == sim_id).execution_options(populate_existing=True)
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return row


async def _require_active_account(db: AsyncSession, user_id: int | None) -> int:
    if user_id is None:
        raise HTTPException(status_code=400, detail=OWNER_REQUIRED)
    found = await db.scalar(select(User.id).where(User.id == user_id, User.is_active.is_(True)))
    if found is None:
        raise HTTPException(status_code=400, detail=OWNER_REQUIRED)
    return found


async def _shop_credit(db: AsyncSession) -> dict[str, dict]:
    """Each company's shop credit (the balance POS sales are charged to)."""
    rows = (await db.execute(
        select(
            Operator.code,
            Operator.name,
            func.coalesce(OperatorBalance.balance_cents, 0).label("balance_cents"),
            func.max(OperatorTopup.created_at).label("last_topup_at"),
        )
        .outerjoin(OperatorBalance, OperatorBalance.operator_id == Operator.id)
        .outerjoin(OperatorTopup, OperatorTopup.operator_id == Operator.id)
        .group_by(Operator.id)
    )).all()
    return {
        row.code: {
            "code": row.code,
            "name": row.name,
            "balance_cents": int(row.balance_cents or 0),
            "last_topup_at": row.last_topup_at,
        }
        for row in rows
    }


def create_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    # --- pages -------------------------------------------------------------

    @router.get("/topup", include_in_schema=False)
    async def old_topup_page(lang: str = "en") -> RedirectResponse:
        """The Top-up page became the Operators page (shop credit lives there)."""
        return RedirectResponse("/operators?lang=ar" if lang == "ar" else "/operators", status_code=303)

    @router.get("/operators", response_class=HTMLResponse, include_in_schema=False)
    async def operators_page(
        request: Request, context: Context, db: AsyncSession = Depends(get_db), lang: str = "en"
    ) -> Response:
        language = "ar" if lang == "ar" else "en"
        credit = await _shop_credit(db)
        sims = [_out(row, context) for row in (await db.execute(_select_sims(context))).all()]
        companies = []
        for code in COMPANY_ORDER:
            if code not in credit:
                continue
            mine = [sim for sim in sims if sim.operator_code == code]
            companies.append({
                **credit[code],
                "active_sims": [sim for sim in mine if sim.status == "active"],
                "archived_sims": [sim for sim in mine if sim.status == "archived"],
            })
        owners = []
        if context.role == "admin":
            owners = (await db.execute(
                select(User.id, User.username, User.role)
                .where(User.is_active.is_(True))
                .order_by(func.lower(User.username))
            )).all()
        return templates.TemplateResponse(
            request=request,
            name="operators.html",
            context={
                "lang": language,
                "nav_page": "operators",
                "companies": companies,
                "owners": owners,
                "current_user_id": context.user_id,
                "viewer_is_admin": context.role == "admin",
            },
            headers=NO_STORE,
        )

    # --- API ---------------------------------------------------------------

    @router.get("/api/sims", response_model=list[SimOut])
    async def list_sims(
        response: Response,
        context: Context,
        db: AsyncSession = Depends(get_db),
        include_archived: bool = False,
        operator_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    ) -> list[SimOut]:
        statement = _select_sims(context)
        if not include_archived:
            statement = statement.where(SimCard.archived_at.is_(None))
        if operator_code is not None:
            statement = statement.where(Operator.code == operator_code)
        response.headers.update(NO_STORE)
        return [_out(row, context) for row in (await db.execute(statement)).all()]

    @router.get("/api/sims/{sim_id}", response_model=SimOut)
    async def get_sim(
        sim_id: Annotated[int, Field(ge=1)], response: Response, context: Context,
        db: AsyncSession = Depends(get_db),
    ) -> SimOut:
        response.headers.update(NO_STORE)
        return _out(await _visible_sim(db, context, sim_id), context)

    @router.post("/api/sims", response_model=SimOut, status_code=201)
    async def create_sim(
        data: SimCreate, response: Response, context: Context, db: AsyncSession = Depends(get_db)
    ) -> SimOut:
        operator_id = await db.scalar(
            select(Operator.id).where(Operator.code == data.operator_code, Operator.is_active.is_(True))
        )
        if operator_id is None:
            raise HTTPException(status_code=400, detail=UNKNOWN_COMPANY)
        if data.visibility == "shared":
            if data.owner_user_id is not None:
                raise HTTPException(status_code=400, detail=SHARED_HAS_NO_OWNER)
            owner_id = None
        elif context.role != "admin":
            if data.owner_user_id not in (None, context.user_id):
                raise HTTPException(status_code=403, detail=OWN_ONLY)
            owner_id = context.user_id
        else:
            owner_id = await _require_active_account(db, data.owner_user_id)

        now = utc_now()
        sim = SimCard(
            operator_id=operator_id,
            display_name=data.display_name,
            phone=data.phone,
            owner_user_id=owner_id,
            is_shared=data.visibility == "shared",
            created_by_user_id=context.user_id,
            created_at=now,
            updated_at=now,
        )
        db.add(sim)
        await db.commit()
        response.headers.update(NO_STORE)
        return _out(await _visible_sim(db, context, sim.id), context)

    @router.patch("/api/sims/{sim_id}", response_model=SimOut)
    async def update_sim(
        sim_id: Annotated[int, Field(ge=1)], data: SimUpdate, response: Response, context: Context,
        db: AsyncSession = Depends(get_db),
    ) -> SimOut:
        sim = (await _visible_sim(db, context, sim_id))[0]
        if not _may_manage(context, sim.is_shared, sim.owner_user_id):
            raise HTTPException(status_code=403, detail=NOT_ALLOWED)
        sent = data.model_fields_set
        if sent & {"visibility", "owner_user_id"} and context.role != "admin":
            raise HTTPException(status_code=403, detail=ADMIN_ONLY_ASSIGNMENT)
        if sim.archived_at is not None:
            raise HTTPException(status_code=409, detail=ARCHIVED)
        for field in ("display_name", "phone"):
            if field in sent and getattr(data, field) is None:
                raise HTTPException(status_code=422, detail=f"{field} cannot be empty")

        values: dict = {}
        if "display_name" in sent:
            values["display_name"] = data.display_name
        if "phone" in sent:
            values["phone"] = data.phone
        if sent & {"visibility", "owner_user_id"}:
            shared = sim.is_shared if "visibility" not in sent or data.visibility is None else data.visibility == "shared"
            if shared:
                if data.owner_user_id is not None:
                    raise HTTPException(status_code=400, detail=SHARED_HAS_NO_OWNER)
                values.update(is_shared=True, owner_user_id=None)
            else:
                owner = data.owner_user_id if "owner_user_id" in sent else (None if sim.is_shared else sim.owner_user_id)
                if "owner_user_id" in sent or sim.is_shared:
                    owner = await _require_active_account(db, owner)
                values.update(is_shared=False, owner_user_id=owner)
        if values:
            values["updated_at"] = utc_now()
            # Guarded on "still active" so a concurrent archive is never undone.
            result = await db.execute(
                update(SimCard).where(SimCard.id == sim.id, SimCard.archived_at.is_(None)).values(**values)
            )
            if result.rowcount != 1:
                await db.rollback()
                raise HTTPException(status_code=409, detail=ARCHIVED)
            await db.commit()
        response.headers.update(NO_STORE)
        return _out(await _visible_sim(db, context, sim.id), context)

    async def _set_archived(sim_id: int, archive: bool, context: auth.AuthContext, db: AsyncSession) -> SimOut:
        sim = (await _visible_sim(db, context, sim_id))[0]
        if not _may_manage(context, sim.is_shared, sim.owner_user_id):
            raise HTTPException(status_code=403, detail=NOT_ALLOWED)
        now = utc_now()
        state = SimCard.archived_at.is_(None) if archive else SimCard.archived_at.is_not(None)
        await db.execute(
            update(SimCard).where(SimCard.id == sim.id, state)
            .values(archived_at=now if archive else None, updated_at=now)
        )
        await db.commit()  # repeating an archive/restore is a harmless no-op
        return _out(await _visible_sim(db, context, sim.id), context)

    @router.post("/api/sims/{sim_id}/archive", response_model=SimOut)
    async def archive_sim(
        sim_id: Annotated[int, Field(ge=1)], response: Response, context: Context,
        db: AsyncSession = Depends(get_db),
    ) -> SimOut:
        response.headers.update(NO_STORE)
        return await _set_archived(sim_id, True, context, db)

    @router.post("/api/sims/{sim_id}/restore", response_model=SimOut)
    async def restore_sim(
        sim_id: Annotated[int, Field(ge=1)], response: Response, context: Context,
        db: AsyncSession = Depends(get_db),
    ) -> SimOut:
        response.headers.update(NO_STORE)
        return await _set_archived(sim_id, False, context, db)

    return router
