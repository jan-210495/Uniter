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

Money (v1.0.4 slice 4.1):

* every SIM card has its own balance (``sim_balances``, 0 until funded) and an
  append-only audit (``sim_ledger``). It is funded only by an audited per-SIM
  top-up (``POST /api/sims/{id}/topup``, idempotent by ``topup_key``);
* a POS sale is charged to the SIM card the seller selects (see ``main.py``);
  each account may save one default SIM card per company;
* the per-company shop credit of earlier versions (``operator_balance``) is the
  *legacy company credit*: historical and read-only. Nothing here reads it into
  a SIM, splits it, or charges it.

"Usable" means: active (not archived) and visible to the account, i.e. any
active SIM card for an admin, or an active shared SIM card / the seller's own
active private SIM card for a seller. Only usable SIM cards can be topped up,
saved as a default or charged by a sale.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import and_, case, delete, func, or_, select, true, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

if __package__:
    from . import auth
    from .database import (
        MAX_MINOR_UNITS, SIM_NAME_MAX_LENGTH, Operator, OperatorBalance, OperatorTopup, SimBalance, SimCard,
        SimDefault, SimLedger, User, get_db, utc_now,
    )
    from .validation import validate_syrian_phone
else:  # pragma: no cover - flat module layout (PyInstaller)
    import auth
    from database import (
        MAX_MINOR_UNITS, SIM_NAME_MAX_LENGTH, Operator, OperatorBalance, OperatorTopup, SimBalance, SimCard,
        SimDefault, SimLedger, User, get_db, utc_now,
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
ARCHIVED_TOPUP = "Restore this SIM card before topping it up"
TOPUP_KEY_REUSED = "This top-up key already belongs to a different top-up"
TOPUP_OVERFLOW = "This top-up would exceed the largest balance that can be stored"
#: One answer for every SIM card a sale or default may not use (missing,
#: another seller's private SIM, archived, other company), so the response
#: never reveals which of these it was.
SIM_UNAVAILABLE = "This SIM card is not available for this sale"
SIM_REQUIRED = "Select a SIM card for this company"
INSUFFICIENT_SIM_BALANCE = "Insufficient SIM balance"
KEY_PATTERN = r"^[A-Za-z0-9_-]+$"


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


class SimTopupCreate(BaseModel):
    """Credit loaded onto one SIM card; the key makes a repeated submit harmless."""

    model_config = ConfigDict(extra="forbid")

    amount_cents: StrictInt = Field(gt=0, le=MAX_MINOR_UNITS)
    topup_key: str = Field(min_length=16, max_length=64, pattern=KEY_PATTERN)


class SimTopupOut(BaseModel):
    sim_id: int
    amount_cents: int
    balance_cents: int
    reference: str


class DefaultSimUpdate(BaseModel):
    """Save (or clear, with ``sim_id: null``) the caller's own default SIM card."""

    model_config = ConfigDict(extra="forbid")

    operator_code: str = Field(min_length=1, max_length=50)
    sim_id: StrictInt | None = Field(default=None, ge=1, le=MAX_MINOR_UNITS)


class DefaultSimOut(BaseModel):
    operator_code: str
    default_sim_id: int | None


class PosSim(BaseModel):
    id: int
    display_name: str
    phone: str
    visibility: Literal["private", "shared"]
    balance_cents: int


class PosCompany(BaseModel):
    operator_code: str
    operator_name: str
    sims: list[PosSim]
    default_sim_id: int | None


class PosSims(BaseModel):
    companies: list[PosCompany]


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
    #: Current per-SIM balance in minor units (0 until the first top-up).
    balance_cents: int = 0
    #: Whether this is the caller's own default SIM card for its company.
    is_default: bool = False
    #: Whether the caller may top it up / save it as a default / sell from it.
    can_use: bool = False


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


def usable(context: auth.AuthContext):
    """SQL filter: the SIM cards this account may charge, fund or default to."""
    return and_(SimCard.archived_at.is_(None), _visible(context))


def _may_manage(context: auth.AuthContext, is_shared: bool, owner_user_id: int | None) -> bool:
    return context.role == "admin" or (not is_shared and owner_user_id == context.user_id)


def _select_sims(context: auth.AuthContext):
    company_rank = case(
        *((Operator.code == code, rank) for rank, code in enumerate(COMPANY_ORDER)), else_=len(COMPANY_ORDER)
    )
    return (
        select(
            SimCard, Operator.code, Operator.name, Owner.username,
            func.coalesce(SimBalance.balance_cents, 0).label("balance_cents"),
            SimDefault.id.is_not(None).label("is_default"),
        )
        .join(Operator, Operator.id == SimCard.operator_id)
        .outerjoin(Owner, Owner.id == SimCard.owner_user_id)
        .outerjoin(SimBalance, SimBalance.sim_card_id == SimCard.id)
        .outerjoin(SimDefault, and_(
            SimDefault.sim_card_id == SimCard.id,
            SimDefault.user_id == context.user_id,
            SimDefault.operator_id == SimCard.operator_id,
        ))
        .where(_visible(context))
        .order_by(company_rank, SimCard.archived_at.is_not(None), func.lower(SimCard.display_name), SimCard.id)
    )


def _out(row, context: auth.AuthContext) -> SimOut:
    sim, operator_code, operator_name, owner_username, balance_cents, is_default = row
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
        balance_cents=int(balance_cents or 0),
        # A default is only ever shown while it is still allowed.
        is_default=bool(is_default) and not archived,
        # Visible + active == usable (sellers only see SIMs they may use).
        can_use=not archived,
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


async def usable_sim_id(
    db: AsyncSession, context: auth.AuthContext, sim_id: int | None, operator_id: int
) -> int:
    """Return ``sim_id`` if this account may charge it for ``operator_id``.

    Raises 400 ``SIM_REQUIRED`` when no SIM card was chosen, and 400
    ``SIM_UNAVAILABLE`` for every SIM card it may not use (missing, another
    seller's private SIM, archived, other company): one answer, no oracle.
    """
    if sim_id is None:
        raise HTTPException(status_code=400, detail=SIM_REQUIRED)
    found = await db.scalar(
        select(SimCard.id).where(SimCard.id == sim_id, SimCard.operator_id == operator_id, usable(context))
    )
    if found is None:
        raise HTTPException(status_code=400, detail=SIM_UNAVAILABLE)
    return found


async def sim_balance(db: AsyncSession, sim_id: int) -> int:
    value = await db.scalar(select(SimBalance.balance_cents).where(SimBalance.sim_card_id == sim_id))
    return int(value or 0)


async def _ensure_balance_row(db: AsyncSession, sim_id: int) -> None:
    """A SIM card without a balance row has 0; materialise it before a change."""
    await db.execute(
        sqlite_insert(SimBalance)
        .values(sim_card_id=sim_id, balance_cents=0, updated_at=utc_now())
        .on_conflict_do_nothing(index_elements=[SimBalance.sim_card_id])
    )


async def debit_for_sale(
    db: AsyncSession, context: auth.AuthContext, sim_id: int, operator_id: int, amount_cents: int
) -> int | None:
    """Atomically take ``amount_cents`` off a usable SIM card; return the new balance.

    One conditional UPDATE: it only applies while the balance covers the
    amount *and* the SIM card is still usable by this account for this company
    (so a concurrent archive or visibility change is honoured). Returns
    ``None`` when nothing was debited; the caller rolls back.
    """
    await _ensure_balance_row(db, sim_id)
    still_usable = select(SimCard.id).where(
        SimCard.id == sim_id, SimCard.operator_id == operator_id, usable(context)
    ).exists()
    result = await db.execute(
        update(SimBalance)
        .where(
            SimBalance.sim_card_id == sim_id,
            func.typeof(SimBalance.balance_cents) == "integer",
            SimBalance.balance_cents >= amount_cents,
            still_usable,
        )
        .values(balance_cents=SimBalance.balance_cents - amount_cents, updated_at=utc_now())
        .returning(SimBalance.balance_cents)
    )
    return result.scalar_one_or_none()


async def credit_refund(db: AsyncSession, sim_id: int, amount_cents: int) -> int | None:
    """Return ``amount_cents`` to a SIM card (archived or not); new balance or ``None``.

    The balance row must already exist (the refunded sale debited it): a
    missing or malformed row fails closed instead of being recreated at 0.
    """
    result = await db.execute(
        update(SimBalance)
        .where(
            SimBalance.sim_card_id == sim_id,
            func.typeof(SimBalance.balance_cents) == "integer",
            SimBalance.balance_cents >= 0,
            SimBalance.balance_cents <= MAX_MINOR_UNITS - amount_cents,
        )
        .values(balance_cents=SimBalance.balance_cents + amount_cents, updated_at=utc_now())
        .returning(SimBalance.balance_cents)
    )
    return result.scalar_one_or_none()


async def pos_companies(db: AsyncSession, context: auth.AuthContext) -> PosSims:
    """Per company, the SIM cards this account may sell from, and its default."""
    sims = (await db.execute(_select_sims(context).where(SimCard.archived_at.is_(None)))).all()
    operators = {row.code: row for row in (await db.execute(
        select(Operator.id, Operator.code, Operator.name).where(Operator.code.in_(COMPANY_ORDER))
    )).all()}
    companies = []
    for code in COMPANY_ORDER:
        operator = operators.get(code)
        if operator is None:
            continue
        mine = [row for row in sims if row[1] == code]
        companies.append(PosCompany(
            operator_code=code,
            operator_name=operator.name,
            sims=[
                PosSim(
                    id=row[0].id,
                    display_name=row[0].display_name,
                    phone=row[0].phone,
                    visibility="shared" if row[0].is_shared else "private",
                    balance_cents=int(row.balance_cents or 0),
                )
                for row in mine
            ],
            # Re-checked on every read: only a default that is still usable.
            default_sim_id=next((row[0].id for row in mine if row.is_default), None),
        ))
    return PosSims(companies=companies)


async def _forget_disallowed_defaults(db: AsyncSession, sim_id: int) -> None:
    """Drop the defaults on ``sim_id`` that their accounts may no longer use."""
    sim = await db.get(SimCard, sim_id, populate_existing=True)
    if sim is None:
        return
    statement = delete(SimDefault).where(SimDefault.sim_card_id == sim_id)
    if sim.archived_at is None:
        if sim.is_shared:
            return
        admins = select(User.id).where(User.role == "admin")
        statement = statement.where(
            SimDefault.user_id != sim.owner_user_id, SimDefault.user_id.not_in(admins)
        )
    await db.execute(statement)


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
            if sent & {"visibility", "owner_user_id"}:
                # Same transaction: nobody keeps a default they may not use.
                await _forget_disallowed_defaults(db, sim.id)
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
        if archive:
            # An archived SIM card is nobody's default (a restore never brings
            # a default back).
            await db.execute(delete(SimDefault).where(SimDefault.sim_card_id == sim.id))
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

    # --- per-SIM money (slice 4.1) ----------------------------------------

    @router.post("/api/sims/{sim_id}/topup", response_model=SimTopupOut)
    async def topup_sim(
        sim_id: Annotated[int, Field(ge=1)], data: SimTopupCreate, response: Response, context: Context,
        db: AsyncSession = Depends(get_db),
    ) -> SimTopupOut:
        """Load credit onto one SIM card, with an append-only audit row.

        Only a SIM card the caller may use (visible and active). Replaying the
        same ``topup_key`` answers the original top-up and credits nothing;
        reusing it for a different top-up is refused. The balance and its
        ledger row change together or not at all.
        """
        response.headers.update(NO_STORE)

        async def replay() -> SimTopupOut | None:
            entry = await db.scalar(
                select(SimLedger).where(SimLedger.idempotency_key == data.topup_key)
                .execution_options(populate_existing=True)
            )
            if entry is None:
                return None
            if entry.kind != "topup" or entry.sim_card_id != sim_id or entry.amount_cents != data.amount_cents:
                raise HTTPException(status_code=409, detail=TOPUP_KEY_REUSED)
            return SimTopupOut(sim_id=entry.sim_card_id, amount_cents=entry.amount_cents,
                               balance_cents=entry.balance_after_cents, reference=entry.reference)

        sim = (await _visible_sim(db, context, sim_id))[0]
        previous = await replay()
        if previous is not None:
            return previous
        if sim.archived_at is not None:
            raise HTTPException(status_code=409, detail=ARCHIVED_TOPUP)
        sim_id = sim.id  # a plain value: ORM attributes expire on rollback
        try:
            await _ensure_balance_row(db, sim_id)
            result = await db.execute(
                update(SimBalance)
                .where(
                    SimBalance.sim_card_id == sim_id,
                    func.typeof(SimBalance.balance_cents) == "integer",
                    SimBalance.balance_cents >= 0,
                    SimBalance.balance_cents <= MAX_MINOR_UNITS - data.amount_cents,
                    select(SimCard.id).where(SimCard.id == sim_id, usable(context)).exists(),
                )
                .values(balance_cents=SimBalance.balance_cents + data.amount_cents, updated_at=utc_now())
                .returning(SimBalance.balance_cents)
            )
            new_balance = result.scalar_one_or_none()
            if new_balance is None:
                await db.rollback()
                still_usable = await db.scalar(select(SimCard.id).where(SimCard.id == sim_id, usable(context)))
                raise HTTPException(status_code=409, detail=TOPUP_OVERFLOW if still_usable else ARCHIVED_TOPUP)
            reference = f"SIMTOPUP-{uuid4().hex}"
            db.add(SimLedger(
                sim_card_id=sim_id,
                kind="topup",
                amount_cents=data.amount_cents,
                balance_after_cents=new_balance,
                idempotency_key=data.topup_key,
                reference=reference,
                performed_by_user_id=context.user_id,
                created_at=utc_now(),
            ))
            await db.commit()
        except IntegrityError:
            await db.rollback()
            previous = await replay()  # a concurrent submit with the same key won
            if previous is not None:
                return previous
            raise HTTPException(status_code=409, detail="Could not record the top-up; retry") from None
        return SimTopupOut(sim_id=sim_id, amount_cents=data.amount_cents, balance_cents=new_balance,
                           reference=reference)

    @router.get("/api/pos/sims", response_model=PosSims)
    async def list_pos_sims(response: Response, context: Context, db: AsyncSession = Depends(get_db)) -> PosSims:
        response.headers.update(NO_STORE)
        return await pos_companies(db, context)

    @router.put("/api/pos/default-sim", response_model=DefaultSimOut)
    async def save_default_sim(
        data: DefaultSimUpdate, response: Response, context: Context, db: AsyncSession = Depends(get_db)
    ) -> DefaultSimOut:
        """Save or clear the caller's own default SIM card for one company."""
        response.headers.update(NO_STORE)
        operator_id = await db.scalar(
            select(Operator.id).where(Operator.code == data.operator_code, Operator.code.in_(COMPANY_ORDER))
        )
        if operator_id is None:
            raise HTTPException(status_code=400, detail=UNKNOWN_COMPANY)
        if data.sim_id is None:
            await db.execute(delete(SimDefault).where(
                SimDefault.user_id == context.user_id, SimDefault.operator_id == operator_id
            ))
            await db.commit()
            return DefaultSimOut(operator_code=data.operator_code, default_sim_id=None)
        sim_id = await usable_sim_id(db, context, data.sim_id, operator_id)
        await db.execute(
            sqlite_insert(SimDefault)
            .values(user_id=context.user_id, operator_id=operator_id, sim_card_id=sim_id, updated_at=utc_now())
            .on_conflict_do_update(
                index_elements=[SimDefault.user_id, SimDefault.operator_id],
                set_={"sim_card_id": sim_id, "updated_at": utc_now()},
            )
        )
        await db.commit()
        return DefaultSimOut(operator_code=data.operator_code, default_sim_id=sim_id)

    return router
