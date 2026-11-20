from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import sys

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import func, literal, or_, select, union_all, update
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from . import auth, sims, users
    from .commission import get_commission_for_transaction
    from .database import TRANSACTION_STATUSES, MAX_MINOR_UNITS, Operator, Service, SimCard, SimLedger, Transaction, User, OperatorTopup, OperatorBalance, ProfitLedger, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting, upgrade_existing_database
    from .seed import DEFAULT_MTN_DOMAIN, DEFAULT_SYRIATEL_DOMAIN, generate_ussd
    from .validation import validate_syrian_phone
else:
    import auth
    import sims
    import users
    from commission import get_commission_for_transaction
    from database import TRANSACTION_STATUSES, MAX_MINOR_UNITS, Operator, Service, SimCard, SimLedger, Transaction, User, OperatorTopup, OperatorBalance, ProfitLedger, CashRegister, CurrencyDenomination, get_db, get_setting, set_setting, upgrade_existing_database
    from seed import DEFAULT_MTN_DOMAIN, DEFAULT_SYRIATEL_DOMAIN, generate_ussd
    from validation import validate_syrian_phone

# Every routed request passes the auth gate (see ``auth.authorize_request``);
# the interactive API docs are disabled so no schema is served unauthenticated.
app = FastAPI(
    title="Uniter",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(auth.authorize_request)],
)

# PyInstaller --onefile extracts templates to sys._MEIPASS at runtime.
_template_dir = Path(__file__).resolve().parent / "templates"
if getattr(sys, "_MEIPASS", None):
    _meipass_templates = Path(sys._MEIPASS) / "backend" / "templates"
    if _meipass_templates.is_dir():
        _template_dir = _meipass_templates
templates = Jinja2Templates(directory=str(_template_dir))
auth.install(app, templates)
app.include_router(users.create_router(templates))
app.include_router(sims.create_router(templates))
_static_dir = _template_dir.parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_app_manifest() -> FileResponse:
    return FileResponse(_static_dir / "manifest.webmanifest", media_type="application/manifest+json")

Code = Annotated[str, Field(min_length=1, max_length=50)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
#: The admin-only settings: commission, both USSD domains and the timezone.
#: Every other setting and workflow stays available to sellers.
ADMIN_ONLY = [Depends(auth.require_admin)]

# Display timezone: the stored default and the options offered on /settings.
DEFAULT_TIMEZONE = "Asia/Damascus"
TIMEZONE_OPTIONS = (
    "UTC",
    "Asia/Damascus",
    "Europe/Damascus",
    "Europe/Zurich",
    "Europe/London",
    "America/New_York",
    "Asia/Dubai",
    "Asia/Riyadh",
)


class OperatorBalanceResponse(BaseModel):
    operator_code: str
    operator_name: str
    balance_cents: int


class OperatorTopupCreate(BaseModel):
    operator_code: Code
    amount_cents: int = Field(gt=0, le=9223372036854775807)


class OperatorTopupResponse(BaseModel):
    operator_code: str
    amount_cents: int
    new_balance_cents: int
    reference: str


@app.on_event("startup")
async def _upgrade_schema_on_startup() -> None:
    """Upgrade an existing database before the first request is served.

    Guarded and data-preserving; a fresh install is untouched because the file
    does not exist yet. See ``database.upgrade_existing_database``.
    """
    await upgrade_existing_database()


def normalize_customer_name(name: str | None) -> str | None:
    """Normalize the optional per-sale name snapshot; blank names stay absent."""
    cleaned = (name or "").strip()
    return cleaned or None


class TransactionRequest(BaseModel):
    """Sale details shared by the preview and the commit call.

    ``customer_name`` is an optional transaction snapshot, not a customer identity.
    """

    customer_phone: str
    operator_code: Code
    service_code: Code
    amount_cents: int = Field(ge=0, le=9223372036854775807)
    customer_name: str | None = Field(default=None, max_length=255)
    #: The SIM card the sale is charged to (v1.0.4 slice 4.1). Required by the
    #: route: a sale without one is refused, never charged to legacy credit.
    sim_card_id: StrictInt | None = Field(default=None, ge=1, le=MAX_MINOR_UNITS)

    @field_validator("customer_phone")
    @classmethod
    def validate_customer_phone(cls, phone: str) -> str:
        return validate_syrian_phone(phone)

    @field_validator("customer_name")
    @classmethod
    def validate_customer_name(cls, name: str | None) -> str | None:
        return normalize_customer_name(name)


class TransactionCreate(TransactionRequest):
    """Commit a sale exactly once: ``commit_key`` is the idempotency key.

    Unknown fields are rejected (422) rather than ignored: the performer is
    always the signed-in account of the server-side session, so a body that
    tries to name one (``user_id``, ``performed_by_user_id``, ``seller_id``,
    ``created_by``, ...) is refused before anything is written.
    """

    model_config = ConfigDict(extra="forbid")

    commit_key: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class TransactionPreviewResponse(BaseModel):
    """A rendered USSD code plus the key that will commit it."""

    ussd_string: str
    commit_key: str


class CommissionSettingUpdate(BaseModel):
    rate: Decimal = Field(ge=0, le=1, allow_inf_nan=False)


class DomainSettingUpdate(BaseModel):
    """USSD domain code: non-empty digits only, at most 20 characters."""

    domain: str = Field(min_length=1, max_length=20, pattern=r"^\d+$")


class TimezoneSettingUpdate(BaseModel):
    """Display timezone: a non-empty zone name, at most 64 characters."""

    timezone: str = Field(min_length=1, max_length=64)


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    ussd_string: str
    status: str
    amount_cents: int
    created_at: datetime
    #: The account that committed the sale; ``None`` for legacy rows.
    performed_by_user_id: int | None = None
    #: The SIM card the sale was charged to; ``None`` for legacy rows, which
    #: were charged to the company's legacy credit.
    sim_card_id: int | None = None
    customer_name: str | None = None


class TransactionStatusUpdate(BaseModel):
    """A requested status change for an existing transaction.

    Arbitrary strings are rejected during request validation; the only
    supported transition (successful -> failed) is enforced by the route.
    """

    status: Literal["successful", "failed"]


class TransactionListItem(BaseModel):
    """A transaction enriched with customer, operator and service labels.

    The optional name is the stored snapshot,
    never a live customer label or a phone substituted for a name.
    """

    id: int
    reference: str
    ussd_string: str
    status: str
    amount_cents: int
    created_at: datetime
    customer_phone: str
    customer_name: str | None
    operator_code: str
    operator_name: str
    service_code: str
    service_name: str
    #: Who committed the sale (id and username only); both are ``None`` for
    #: rows recorded before v1.0.4, which are never back-filled.
    performed_by_user_id: int | None
    performed_by_username: str | None
    #: ``"legacy"``: recorded before per-SIM balances and charged to the
    #: company's legacy credit (no SIM card). ``"sim"``: charged to a SIM card.
    funding: Literal["sim", "legacy"] = "legacy"
    #: The SIM card, only when the viewer may see it (never another seller's
    #: private SIM card); ``None`` otherwise and for legacy rows.
    sim_card_id: int | None = None
    sim_display_name: str | None = None


class SearchTransactionItem(BaseModel):
    """Transaction fields shown in global search results."""

    id: int
    reference: str
    amount_cents: int
    status: str
    customer_phone: str
    customer_name: str | None
    operator_code: str
    operator_name: str


class SearchOperatorItem(BaseModel):
    """Operator fields shown in global search results."""

    code: str
    name: str
    balance_cents: int


class SearchResults(BaseModel):
    """Global search response: transactions and operators."""

    transactions: list[SearchTransactionItem]
    operators: list[SearchOperatorItem]


class StatsTotals(BaseModel):
    """Window-wide profit and transaction-volume totals."""

    profit_cents: int
    commission_cents: int
    volume_cents: int
    transaction_count: int


class StatsOperatorRow(BaseModel):
    """Per-operator profit and transaction volume inside the stats window."""

    operator_code: str
    operator_name: str
    profit_cents: int
    commission_cents: int
    volume_cents: int
    transaction_count: int


class StatsStatusRow(BaseModel):
    """Transaction count and volume for one status value."""

    status: str
    count: int
    volume_cents: int


class StatsDailyRow(BaseModel):
    """One calendar day of the daily series (zero-filled)."""

    date: str
    profit_cents: int
    volume_cents: int
    transaction_count: int


class StatsSummary(BaseModel):
    """Response of GET /api/stats/summary."""

    period_days: int
    operator_code: str | None
    totals: StatsTotals
    by_operator: list[StatsOperatorRow]
    by_status: list[StatsStatusRow]
    daily: list[StatsDailyRow]


class CashRegisterCreate(BaseModel):
    opening_balance_cents: int = Field(ge=0, default=0)


class CashRegisterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opening_balance_cents: int
    closing_balance_cents: int | None = None
    status: str
    opened_at: datetime
    closed_at: datetime | None = None


class RegisterSummary(BaseModel):
    opening_balance_cents: int
    cash_collected_cents: int
    closing_balance_cents: int | None
    transaction_count: int
class CurrencyDenominationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scale_name: str
    multiplier: int
    is_active: bool


class SetDenomination(BaseModel):
    scale_name: str



@app.get("/")
def root() -> RedirectResponse:
    """The homepage always redirects to the point of sale."""
    return RedirectResponse(url="/pos", status_code=302)


@app.get("/pos", response_class=HTMLResponse)
async def pos_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    language = lang if lang in {"en", "ar"} else "en"
    denom = await db.scalar(select(CurrencyDenomination).where(CurrencyDenomination.is_active == True))
    # Only the SIM cards this account may use (with balances and its default):
    # another seller's private SIM card never reaches the page.
    pos_sims = (await sims.pos_companies(db, _auth_context(request))).model_dump()
    return templates.TemplateResponse(
        request=request,
        name="pos.html",
        context={"lang": language, "currency_denomination": denom, "pos_sims": pos_sims},
        headers={"Cache-Control": "no-store"},
    )

@app.get("/transactions", response_class=HTMLResponse)
async def transactions_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    language = lang if lang in {"en", "ar"} else "en"
    # "Performed by" choices: every account, admins and sellers, active or
    # deactivated (their history still exists). Only id and username leave the
    # database; the filter itself is applied by /api/transactions.
    performers = [
        {"id": row.id, "username": row.username}
        for row in (await db.execute(select(User.id, User.username).order_by(User.username, User.id))).all()
    ]
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={"lang": language, "nav_page": "transactions", "performers": performers},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: DbSession, lang: str = "en") -> HTMLResponse:
    """Settings page: commission rate, active currency denomination and the
    read-only operator/service code structure, rendered server-side."""
    language = lang if lang in {"en", "ar"} else "en"
    operators = (
        await db.scalars(
            select(Operator).options(selectinload(Operator.services)).order_by(Operator.code)
        )
    ).all()
    denominations = (
        await db.scalars(select(CurrencyDenomination).order_by(CurrencyDenomination.id))
    ).all()
    active_denomination = next((d for d in denominations if d.is_active), None)
    mtn_domain = await get_setting(db, "MTN_DOMAIN", DEFAULT_MTN_DOMAIN)
    syriatel_domain = await get_setting(db, "SYRIATEL_DOMAIN", DEFAULT_SYRIATEL_DOMAIN)
    timezone = await get_setting(db, "timezone", DEFAULT_TIMEZONE)
    assert mtn_domain is not None and syriatel_domain is not None and timezone is not None
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "lang": language,
            "nav_page": "settings",
            "operators": operators,
            "denominations": denominations,
            "active_denomination": active_denomination,
            "mtn_domain": mtn_domain,
            "syriatel_domain": syriatel_domain,
            "timezones": TIMEZONE_OPTIONS,
            "timezone": timezone,
            # Display only; the PUT endpoints enforce the same rule server-side.
            "can_manage_settings": auth.is_admin(request),
        },
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, lang: str = "en") -> HTMLResponse:
    """Stats dashboard: profit and transaction volume over time."""
    language = lang if lang in {"en", "ar"} else "en"
    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={"lang": language, "nav_page": "stats"},
    )


@app.get("/operators/{operator_code}", response_class=HTMLResponse)
async def operator_page(
    request: Request, db: DbSession, operator_code: str, lang: str = "en"
) -> HTMLResponse:
    """Operator subpage: balance, latest transactions and a top-up link."""
    language = lang if lang in {"en", "ar"} else "en"
    operator = await db.scalar(select(Operator).where(Operator.code == operator_code))
    if operator is None or not operator.is_active:
        raise HTTPException(status_code=404, detail="Operator not found")
    balance = await db.scalar(
        select(OperatorBalance.balance_cents).where(OperatorBalance.operator_id == operator.id)
    )
    rows = await db.execute(
        _transaction_list_statement()
        .where(Operator.code == operator_code)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(20)
    )
    transactions = _list_items(rows.all(), _auth_context(request))
    return templates.TemplateResponse(
        request=request,
        name="operator.html",
        context={
            "lang": language,
            "operator": operator,
            "balance_cents": int(balance or 0),
            "transactions": transactions,
        },
    )


@app.get("/api/search", response_model=SearchResults)
async def global_search(
    db: DbSession,
    q: Annotated[str | None, Query(max_length=255)] = None,
) -> SearchResults:
    """Global search across transactions and operators.

    Matches transactions by reference, snapshot name or phone fragment,
    and operators by code or name fragment. Each
    section is limited to 10 results; an empty query returns empty sections.
    """
    query = (q or "").strip()
    if not query:
        return SearchResults(transactions=[], operators=[])
    like = f"%{query}%"

    transaction_rows = await db.execute(
        select(
            Transaction.id,
            Transaction.reference,
            Transaction.amount_cents,
            Transaction.status,
            Transaction.customer_phone.label("customer_phone"),
            Transaction.customer_name.label("customer_name"),
            Operator.code.label("operator_code"),
            Operator.name.label("operator_name"),
        )
        .select_from(Transaction)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(
            or_(
                Transaction.reference.ilike(like),
                Transaction.customer_phone.ilike(like),
                Transaction.customer_name.ilike(like),
            ),
            Transaction.deleted_at.is_(None),
        )
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(10)
    )
    transactions = [SearchTransactionItem(**row._mapping) for row in transaction_rows.all()]

    operator_rows = await db.execute(
        select(
            Operator.code,
            Operator.name,
            func.coalesce(OperatorBalance.balance_cents, 0).label("balance_cents"),
        )
        .outerjoin(OperatorBalance, OperatorBalance.operator_id == Operator.id)
        .where(or_(Operator.code.ilike(like), Operator.name.ilike(like)))
        .order_by(Operator.code)
        .limit(10)
    )
    operators = [SearchOperatorItem(**row._mapping) for row in operator_rows.all()]

    return SearchResults(transactions=transactions, operators=operators)


@app.get("/api/stats/summary", response_model=StatsSummary)
async def stats_summary(
    db: DbSession,
    period: Annotated[int, Query(ge=1, le=90)] = 7,
    operator_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
) -> StatsSummary:
    """Profit and transaction-volume statistics over the last ``period`` days.

    Profit and commission come from the ProfitLedger, volume and counts from
    Transaction; both cover the same UTC window and are optionally restricted
    to one operator. The daily series is zero-filled so every calendar day in
    the window has a row, oldest first.

    Removed (tombstoned) transactions are excluded from both sides, so profit
    and volume always describe the same sales. Failed rows still count in the
    by-status breakdown, because the sale happened; removal is a reporting
    tombstone and never reverses money.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=period)

    profit_conditions = [
        ProfitLedger.created_at >= window_start,
        Transaction.deleted_at.is_(None),
    ]
    transaction_conditions = [
        Transaction.created_at >= window_start,
        Transaction.deleted_at.is_(None),
    ]
    if operator_code is not None:
        profit_conditions.append(ProfitLedger.operator_code == operator_code)
        transaction_conditions.append(Operator.code == operator_code)

    # Profit per operator (the ledger carries the operator code directly).
    profit_rows = await db.execute(
        select(
            ProfitLedger.operator_code,
            func.coalesce(func.sum(ProfitLedger.profit_cents), 0).label("profit_cents"),
            func.coalesce(func.sum(ProfitLedger.commission_cents), 0).label("commission_cents"),
        )
        .select_from(ProfitLedger)
        .join(Transaction, ProfitLedger.transaction_id == Transaction.id)
        .where(*profit_conditions)
        .group_by(ProfitLedger.operator_code)
    )
    profit_by_operator = {
        row.operator_code: (int(row.profit_cents), int(row.commission_cents))
        for row in profit_rows.all()
    }

    # Volume and count per operator; transactions reach the operator via services.
    volume_rows = await db.execute(
        select(
            Operator.code,
            func.count(Transaction.id).label("transaction_count"),
            func.coalesce(func.sum(Transaction.amount_cents), 0).label("volume_cents"),
        )
        .select_from(Transaction)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(*transaction_conditions)
        .group_by(Operator.code)
    )
    volume_by_operator = {
        row.code: (int(row.transaction_count), int(row.volume_cents)) for row in volume_rows.all()
    }

    status_rows = await db.execute(
        select(
            Transaction.status,
            func.count(Transaction.id).label("count"),
            func.coalesce(func.sum(Transaction.amount_cents), 0).label("volume_cents"),
        )
        .select_from(Transaction)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(*transaction_conditions)
        .group_by(Transaction.status)
    )
    status_counts = {row.status: (int(row.count), int(row.volume_cents)) for row in status_rows.all()}

    profit_day_rows = await db.execute(
        select(
            func.date(ProfitLedger.created_at).label("day"),
            func.coalesce(func.sum(ProfitLedger.profit_cents), 0).label("profit_cents"),
        )
        .select_from(ProfitLedger)
        .join(Transaction, ProfitLedger.transaction_id == Transaction.id)
        .where(*profit_conditions)
        .group_by(func.date(ProfitLedger.created_at))
    )
    profit_by_day = {str(row.day): int(row.profit_cents) for row in profit_day_rows.all()}

    volume_day_rows = await db.execute(
        select(
            func.date(Transaction.created_at).label("day"),
            func.count(Transaction.id).label("transaction_count"),
            func.coalesce(func.sum(Transaction.amount_cents), 0).label("volume_cents"),
        )
        .select_from(Transaction)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(*transaction_conditions)
        .group_by(func.date(Transaction.created_at))
    )
    volume_by_day = {
        str(row.day): (int(row.transaction_count), int(row.volume_cents))
        for row in volume_day_rows.all()
    }

    # Every operator appears in the breakdown, zeroed when idle inside the window.
    operator_statement = select(Operator.code, Operator.name).order_by(Operator.code)
    if operator_code is not None:
        operator_statement = operator_statement.where(Operator.code == operator_code)
    operator_rows = await db.execute(operator_statement)
    by_operator = [
        StatsOperatorRow(
            operator_code=row.code,
            operator_name=row.name,
            profit_cents=profit_by_operator.get(row.code, (0, 0))[0],
            commission_cents=profit_by_operator.get(row.code, (0, 0))[1],
            volume_cents=volume_by_operator.get(row.code, (0, 0))[1],
            transaction_count=volume_by_operator.get(row.code, (0, 0))[0],
        )
        for row in operator_rows.all()
    ]
    by_operator.sort(key=lambda item: (-item.profit_cents, item.operator_code))

    totals = StatsTotals(
        profit_cents=sum(profit for profit, _ in profit_by_operator.values()),
        commission_cents=sum(commission for _, commission in profit_by_operator.values()),
        volume_cents=sum(volume for _, volume in volume_by_operator.values()),
        transaction_count=sum(count for count, _ in volume_by_operator.values()),
    )
    by_status = [
        StatsStatusRow(
            status=status,
            count=status_counts.get(status, (0, 0))[0],
            volume_cents=status_counts.get(status, (0, 0))[1],
        )
        for status in TRANSACTION_STATUSES
    ]

    today = now.date()
    daily: list[StatsDailyRow] = []
    for offset in range(period - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        count, volume = volume_by_day.get(day, (0, 0))
        daily.append(
            StatsDailyRow(
                date=day,
                profit_cents=profit_by_day.get(day, 0),
                volume_cents=volume,
                transaction_count=count,
            )
        )

    return StatsSummary(
        period_days=period,
        operator_code=operator_code,
        totals=totals,
        by_operator=by_operator,
        by_status=by_status,
        daily=daily,
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/settings/commission")
async def read_commission_setting(db: DbSession) -> dict[str, str]:
    rate = await get_setting(db, "commission_rate", "0.05")
    assert rate is not None
    return {"commission_rate": rate}


@app.put("/api/settings/commission", dependencies=ADMIN_ONLY)
async def update_commission_setting(data: CommissionSettingUpdate, db: DbSession) -> dict[str, str]:
    setting = await set_setting(db, "commission_rate", str(data.rate))
    value = setting.value
    await db.commit()
    return {"commission_rate": value}


@app.get("/api/settings/mtn-domain")
async def read_mtn_domain_setting(db: DbSession) -> dict[str, str]:
    """Current MTN USSD domain code (default 65477 when not stored)."""
    domain = await get_setting(db, "MTN_DOMAIN", DEFAULT_MTN_DOMAIN)
    assert domain is not None
    return {"mtn_domain": domain}


@app.put("/api/settings/mtn-domain", dependencies=ADMIN_ONLY)
async def update_mtn_domain_setting(data: DomainSettingUpdate, db: DbSession) -> dict[str, str]:
    """Store the MTN USSD domain code consumed by generate_ussd."""
    setting = await set_setting(db, "MTN_DOMAIN", data.domain)
    value = setting.value
    await db.commit()
    return {"mtn_domain": value}


@app.get("/api/settings/syriatel-domain")
async def read_syriatel_domain_setting(db: DbSession) -> dict[str, str]:
    """Current Syriatel USSD domain code (default 67858 when not stored)."""
    domain = await get_setting(db, "SYRIATEL_DOMAIN", DEFAULT_SYRIATEL_DOMAIN)
    assert domain is not None
    return {"syriatel_domain": domain}


@app.put("/api/settings/syriatel-domain", dependencies=ADMIN_ONLY)
async def update_syriatel_domain_setting(data: DomainSettingUpdate, db: DbSession) -> dict[str, str]:
    """Store the Syriatel USSD domain code consumed by generate_ussd."""
    setting = await set_setting(db, "SYRIATEL_DOMAIN", data.domain)
    value = setting.value
    await db.commit()
    return {"syriatel_domain": value}


@app.get("/api/settings/timezone")
async def read_timezone_setting(db: DbSession) -> dict[str, str]:
    """Current display timezone (default Asia/Damascus when not stored)."""
    timezone = await get_setting(db, "timezone", DEFAULT_TIMEZONE)
    assert timezone is not None
    return {"timezone": timezone}


@app.put("/api/settings/timezone", dependencies=ADMIN_ONLY)
async def update_timezone_setting(data: TimezoneSettingUpdate, db: DbSession) -> dict[str, str]:
    """Store the display timezone shown in the header and used for times."""
    setting = await set_setting(db, "timezone", data.timezone)
    value = setting.value
    await db.commit()
    return {"timezone": value}


@app.get("/api/commission/calculate")
async def commission_calculation(
    db: DbSession,
    amount_cents: Annotated[int, Query(ge=0, le=9223372036854775807)],
    operator_code: Annotated[str, Query(min_length=1, max_length=50)],
    service_code: Annotated[str, Query(min_length=1, max_length=50)],
) -> dict[str, int | str]:
    operator = await db.scalar(select(Operator).where(Operator.code == operator_code))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    service = await db.scalar(
        select(Service).where(Service.operator_id == operator.id, Service.code == service_code)
    )
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found for operator")
    try:
        return await get_commission_for_transaction(db, amount_cents)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid stored commission rate") from exc


@app.get("/api/operators/balance", response_model=list[OperatorBalanceResponse])
async def list_operator_balances(db: DbSession) -> list[OperatorBalanceResponse]:
    """Return the prepaid credit held with every operator (0 when no row exists)."""
    rows = await db.execute(
        select(
            Operator.code,
            Operator.name,
            func.coalesce(OperatorBalance.balance_cents, 0).label("balance_cents"),
        )
        .outerjoin(OperatorBalance, OperatorBalance.operator_id == Operator.id)
        .order_by(Operator.code)
    )
    return [
        OperatorBalanceResponse(
            operator_code=row.code,
            operator_name=row.name,
            balance_cents=int(row.balance_cents or 0),
        )
        for row in rows.all()
    ]


@app.post("/api/operators/topup", response_model=OperatorTopupResponse)
async def topup_operator_balance(
    data: OperatorTopupCreate, db: DbSession
) -> OperatorTopupResponse:
    """Legacy company credit is read-only since per-SIM balances (slice 4.1).

    New money is loaded onto a SIM card (``POST /api/sims/{id}/topup``). The
    company pool keeps its historical balance and top-up audit untouched until
    the shop decides how to migrate it, so no new credit is added here.
    """
    operator = await db.scalar(select(Operator).where(Operator.code == data.operator_code))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    raise HTTPException(status_code=409, detail=LEGACY_CREDIT_READ_ONLY)

async def _resolve_sale_configuration(
    db: AsyncSession, operator_code: str, service_code: str
) -> tuple[Operator, Service]:
    """Return the active operator/service pair or raise the usual 404/400."""
    operator = await db.scalar(select(Operator).where(Operator.code == operator_code))
    if operator is None:
        raise HTTPException(status_code=404, detail="Operator not found")
    service = await db.scalar(
        select(Service).where(
            Service.operator_id == operator.id, Service.code == service_code
        )
    )
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found for operator")
    if not operator.is_active or not service.is_active:
        raise HTTPException(status_code=400, detail="Operator or service is inactive")
    return operator, service


#: Reported for a legacy top-up attempt (slice 4.1).
LEGACY_CREDIT_READ_ONLY = "Legacy company credit is read-only; top up a SIM card instead"
#: Reported when a legacy sale (no SIM card) is marked failed: its money came
#: from the company's legacy credit and no SIM card may be guessed for it.
LEGACY_SALE_HAS_NO_SIM = (
    "This sale was charged to the legacy company credit; it has no SIM card to refund"
)


def _auth_context(request: Request) -> auth.AuthContext:
    context = getattr(request.state, "auth", None)
    if context is None:  # the app-wide gate always sets it; fail closed anyway
        raise HTTPException(status_code=401, detail="Authentication required")
    return context


async def _require_sim_for_sale(
    db: AsyncSession, request: Request, operator_id: int, sim_card_id: int | None, amount_cents: int
) -> int:
    """The usable SIM card that will fund this sale, with enough balance.

    There is no fallback: without a usable SIM card of this company the sale is
    refused, and the legacy company credit is never charged.
    """
    sim_id = await sims.usable_sim_id(db, _auth_context(request), sim_card_id, operator_id)
    if await sims.sim_balance(db, sim_id) < amount_cents:
        raise HTTPException(status_code=400, detail=sims.INSUFFICIENT_SIM_BALANCE)
    return sim_id


async def _committed_transaction(
    db: AsyncSession, data: TransactionCreate
) -> Transaction | None:
    """Return the sale already stored for this commit key, if any.

    The key identifies one deliberate sale, so replaying the request returns
    that same row (whatever its current status) instead of charging again. A
    key reused for different details is rejected rather than silently answered.
    """
    row = (
        await db.execute(
            select(
                Transaction,
                Transaction.customer_phone.label("customer_phone"),
                Operator.code.label("operator_code"),
                Service.code.label("service_code"),
            )
            .select_from(Transaction)
            .join(Service, Transaction.service_id == Service.id)
            .join(Operator, Service.operator_id == Operator.id)
            .where(Transaction.idempotency_key == data.commit_key)
        )
    ).first()
    if row is None:
        return None
    transaction = row[0]
    # The SIM card is deliberately not compared: a replay answers the stored
    # sale with the SIM card it was charged to, even if another one is sent.
    same_sale = (
        row.customer_phone == data.customer_phone
        and row.operator_code == data.operator_code
        and row.service_code == data.service_code
        and transaction.amount_cents == data.amount_cents
    )
    if not same_sale:
        raise HTTPException(
            status_code=409,
            detail="This commit key already belongs to a different sale",
        )
    return transaction


@app.post("/api/transactions/preview", response_model=TransactionPreviewResponse)
async def preview_transaction(
    request: Request, data: TransactionRequest, db: DbSession
) -> TransactionPreviewResponse:
    """Render the USSD code for a sale without persisting anything.

    Generating a code is not a sale: no transaction row, no operator debit and
    no commission entry are created here. The returned ``commit_key`` is what
    the commit call uses to make the seller's tap idempotent.
    """
    operator, _service = await _resolve_sale_configuration(
        db, data.operator_code, data.service_code
    )
    await _require_sim_for_sale(db, request, operator.id, data.sim_card_id, data.amount_cents)
    try:
        ussd_string = await generate_ussd(
            db, data.operator_code, data.service_code, data.customer_phone, data.amount_cents
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TransactionPreviewResponse(ussd_string=ussd_string, commit_key=uuid4().hex)


def _session_user_id(request: Request) -> int:
    """The signed-in account of the server-side session (never the request)."""
    context = getattr(request.state, "auth", None)
    if context is None:  # the app-wide gate always sets it; fail closed anyway
        raise HTTPException(status_code=401, detail="Authentication required")
    return context.user_id


@app.post("/api/transactions", response_model=TransactionResponse)
async def create_transaction(
    request: Request, data: TransactionCreate, db: DbSession
) -> Transaction:
    """Commit a sale: persist it as successful and apply accounting once.

    The seller's copy/dialer tap is the commit event. The operator balance is
    debited with a single conditional UPDATE and the transaction row plus its
    profit ledger row are written in the same database transaction, so a sale
    is either fully recorded or not recorded at all. Replaying the same
    ``commit_key`` returns the stored sale without touching the ledger again.

    The optional normalized name is stored on this sale only. No customer
    record is searched, created, modified or linked.

    The performer is the session's account. A replay returns the stored row
    unchanged, so it keeps the account that originally committed it.
    """
    performer_id = _session_user_id(request)
    replay = await _committed_transaction(db, data)
    if replay is not None:
        return replay

    operator, service = await _resolve_sale_configuration(
        db, data.operator_code, data.service_code
    )
    # Plain values: ORM attributes expire on rollback and must not lazy-load.
    operator_id, operator_code = operator.id, operator.code
    try:
        sim_id = await _require_sim_for_sale(db, request, operator_id, data.sim_card_id, data.amount_cents)
    except HTTPException as exc:
        if exc.detail == sims.INSUFFICIENT_SIM_BALANCE:
            # A same-key request may have committed (and spent the balance)
            # since the replay check above: answer that sale, not "insufficient".
            replay = await _committed_transaction(db, data)
            if replay is not None:
                return replay
        raise
    try:
        ussd_string = await generate_ussd(
            db, data.operator_code, data.service_code, data.customer_phone, data.amount_cents
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        commission = await get_commission_for_transaction(db, data.amount_cents)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid stored commission rate") from exc
    commission_cents = int(commission["commission_cents"])

    transaction: Transaction | None = None
    try:
        # Atomic, non-negative debit of the selected SIM card only: two
        # concurrent commits can never overdraw it, and the legacy company
        # credit is never touched.
        balance_after = await sims.debit_for_sale(
            db, _auth_context(request), sim_id, operator_id, data.amount_cents
        )
        if balance_after is None:
            await db.rollback()
            # The losing request of a same-key race: the winner may have
            # committed (and spent the balance) since the first replay check.
            replay = await _committed_transaction(db, data)
            if replay is not None:
                return replay
            await _require_sim_for_sale(db, request, operator_id, sim_id, data.amount_cents)
            raise HTTPException(status_code=400, detail=sims.INSUFFICIENT_SIM_BALANCE)

        transaction = Transaction(
            customer_name=data.customer_name,
            service_id=service.id,
            amount_cents=data.amount_cents,
            status="successful",
            ussd_string=ussd_string,
            reference=f"TXN-{uuid4().hex}",
            idempotency_key=data.commit_key,
            # The charged number is on every row, linked or not, so history and
            # filters can identify the sale without a customer record.
            customer_phone=data.customer_phone,
            performed_by_user_id=performer_id,
            sim_card_id=sim_id,
        )
        db.add(transaction)
        await db.flush()  # assigns transaction.id for the profit ledger row
        db.add(
            ProfitLedger(
                transaction_id=transaction.id,
                operator_code=operator_code,
                amount_cents=data.amount_cents,
                commission_cents=commission_cents,
                profit_cents=commission_cents,
            )
        )
        db.add(
            SimLedger(
                sim_card_id=sim_id,
                kind="sale",
                amount_cents=data.amount_cents,
                balance_after_cents=balance_after,
                transaction_id=transaction.id,
                reference=f"SALE-{transaction.reference}",
                performed_by_user_id=performer_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # A concurrent commit with the same key won the race: answer with it
        # instead of charging again. Nothing from this attempt survives.
        replay = await _committed_transaction(db, data)
        if replay is not None:
            return replay
        raise HTTPException(
            status_code=409, detail="Could not commit the sale; retry with the same commit key"
        ) from None
    await db.refresh(transaction)
    return transaction


@app.patch("/api/transactions/{transaction_id}/status", response_model=TransactionResponse)
async def update_transaction_status(
    request: Request,
    transaction_id: Annotated[int, Field(ge=1)],
    data: TransactionStatusUpdate,
    db: DbSession,
) -> Transaction:
    """Mark a successful sale as failed.

    The seller records the real-world outcome from the Transactions page. Only
    ``successful -> failed`` is allowed, the guard lives in the UPDATE itself
    (so replays and races cannot apply twice). The SIM card the sale was
    charged to receives exactly the stored original debit (even if it has been
    archived since), with a unique append-only ``refund`` ledger row in the
    same commit. A legacy sale has no SIM card: it is refused unchanged rather
    than refunded to a guessed one. Commission/profit history and the
    performer are never reversed or edited.
    """
    performer_id = _session_user_id(request)
    if data.status != "failed":
        raise HTTPException(status_code=409, detail="Transactions can only be marked failed")

    try:
        result = await db.execute(
            update(Transaction)
            .where(Transaction.id == transaction_id, Transaction.status == "successful")
            .values(status="failed")
        )
        if result.rowcount != 1:
            current = await db.scalar(
                select(Transaction.status).where(Transaction.id == transaction_id)
            )
            if current is None:
                raise HTTPException(status_code=404, detail="Transaction not found")
            raise HTTPException(status_code=409, detail="Transaction is already failed")
        transaction = await db.get(Transaction, transaction_id, populate_existing=True)
        if transaction is None:
            raise HTTPException(status_code=409, detail="Original debit could not be resolved")
        if transaction.sim_card_id is None:
            raise HTTPException(status_code=409, detail=LEGACY_SALE_HAS_NO_SIM)
        debit = await db.scalar(
            select(SimLedger).where(SimLedger.transaction_id == transaction.id, SimLedger.kind == "sale")
        )
        if debit is None or debit.sim_card_id != transaction.sim_card_id:
            raise HTTPException(status_code=409, detail="Original SIM debit could not be resolved")
        # The sale's own accounting must still be intact (fail closed, as before):
        # without its profit ledger row the original sale cannot be trusted.
        recorded = await db.scalar(
            select(func.count()).select_from(ProfitLedger).where(ProfitLedger.transaction_id == transaction.id)
        )
        if recorded != 1:
            raise HTTPException(status_code=409, detail="Original sale accounting could not be resolved")
        amount = debit.amount_cents
        if type(amount) is not int or not 0 <= amount <= MAX_MINOR_UNITS or amount != transaction.amount_cents:
            raise HTTPException(status_code=409, detail="Stored transaction amount is invalid")
        balance_after = await sims.credit_refund(db, debit.sim_card_id, amount)
        if balance_after is None:
            raise HTTPException(status_code=409, detail="Original SIM balance could not be credited")
        db.add(SimLedger(
            sim_card_id=debit.sim_card_id,
            kind="refund",
            amount_cents=amount,
            balance_after_cents=balance_after,
            transaction_id=transaction.id,
            reference=f"REFUND-{transaction.reference}",
            performed_by_user_id=performer_id,
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        return transaction
    except Exception:
        await db.rollback()
        raise


@app.delete("/api/transactions/{transaction_id}", status_code=204, response_class=Response)
async def remove_failed_transaction(
    transaction_id: Annotated[int, Field(ge=1)], db: DbSession
) -> Response:
    """Remove a failed transaction from the reporting views (tombstone).

    Only failed rows can be removed, and removal never refunds: the row keeps
    its status with ``deleted_at`` set, and its profit ledger row and the
    refund audit stay untouched so the audit trail survives. Deleting again
    is safe and returns the same 204.
    """
    result = await db.execute(
        update(Transaction)
        .where(
            Transaction.id == transaction_id,
            Transaction.status == "failed",
            Transaction.deleted_at.is_(None),
        )
        .values(deleted_at=datetime.now(timezone.utc))
    )
    if result.rowcount == 1:
        await db.commit()
        return Response(status_code=204)

    row = (
        await db.execute(
            select(Transaction.status, Transaction.deleted_at).where(
                Transaction.id == transaction_id
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if row.status != "failed":
        raise HTTPException(status_code=409, detail="Only failed transactions can be removed")
    return Response(status_code=204)  # already removed: repeating is safe


#: Columns every transaction view shows, with the customer labels resolved for
#: linked rows and for one-time (unlinked) sales alike. An unlinked sale keeps
#: its own phone and has no name — the number is never presented as one.
_TRANSACTION_LABELS = (
    Transaction.id,
    Transaction.reference,
    Transaction.ussd_string,
    Transaction.status,
    Transaction.amount_cents,
    Transaction.created_at,
    Transaction.customer_phone.label("customer_phone"),
    Transaction.customer_name.label("customer_name"),
    Operator.code.label("operator_code"),
    Operator.name.label("operator_name"),
    Service.code.label("service_code"),
    Service.name.label("service_name"),
    Transaction.performed_by_user_id.label("performed_by_user_id"),
    User.username.label("performed_by_username"),
    Transaction.sim_card_id.label("sim_card_id"),
    SimCard.display_name.label("sim_display_name"),
    SimCard.is_shared.label("sim_is_shared"),
    SimCard.owner_user_id.label("sim_owner_user_id"),
    SimCard.archived_at.label("sim_archived_at"),
)


def _list_items(rows, context: auth.AuthContext) -> list[TransactionListItem]:
    """Build list items; a SIM card is only named to accounts that may see it.

    Visibility mirrors the SIM directory: admins see every SIM card; a seller
    sees active shared SIM cards and their own private ones. Any other SIM
    sale is shown as "charged to a SIM card" without its id or name.
    """
    items = []
    for row in rows:
        values = dict(row._mapping)
        sim_id = values.pop("sim_card_id")
        name = values.pop("sim_display_name")
        shared = values.pop("sim_is_shared")
        owner = values.pop("sim_owner_user_id")
        archived = values.pop("sim_archived_at")
        visible = sim_id is not None and (
            context.role == "admin"
            or (bool(shared) and archived is None)
            or (not shared and owner == context.user_id)
        )
        items.append(TransactionListItem(
            **values,
            funding="legacy" if sim_id is None else "sim",
            sim_card_id=sim_id if visible else None,
            sim_display_name=name if visible else None,
        ))
    return items


def _transaction_list_statement():
    """Live sales with operator/service labels and optional performer/SIM labels.

    Removed rows remain on disk for audit but are hidden from reports. Legacy
    rows without a performer or SIM remain visible through the outer joins.
    """
    return (
        select(*_TRANSACTION_LABELS)
        .select_from(Transaction)
        .outerjoin(User, Transaction.performed_by_user_id == User.id)
        .outerjoin(SimCard, Transaction.sim_card_id == SimCard.id)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(Transaction.deleted_at.is_(None))
    )


@app.get("/api/transactions", response_model=list[TransactionListItem])
async def list_transactions(
    request: Request,
    db: DbSession,
    operator_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    service_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    status: Annotated[str | None, Query(pattern="^(successful|failed)$")] = None,
    customer_phone: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    performed_by: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
) -> list[TransactionListItem]:
    """List transactions newest-first, optionally filtered by operator code,
    service code, status, a case-insensitive customer phone fragment, or the
    id of the account that performed the sale.

    Filters that match nothing (including an unknown performer id) return an
    empty list; a performer value that is not a positive integer is a 422.
    ``performed_by`` is a view filter, not an access boundary: every signed-in
    account sees every transaction. Legacy rows (no performer) are only listed
    when no performer is chosen."""
    statement = _transaction_list_statement()
    if operator_code is not None:
        statement = statement.where(Operator.code == operator_code)
    if service_code is not None:
        statement = statement.where(Service.code == service_code)
    if status is not None:
        statement = statement.where(Transaction.status == status)
    if customer_phone is not None:
        statement = statement.where(
            Transaction.customer_phone.ilike(
                f"%{customer_phone}%"
            )
        )
    if performed_by is not None:
        statement = statement.where(Transaction.performed_by_user_id == performed_by)
    rows = await db.execute(statement.order_by(Transaction.created_at.desc(), Transaction.id.desc()))
    return _list_items(rows.all(), _auth_context(request))


class LedgerSaleItem(TransactionListItem):
    record_type: Literal["sale"] = "sale"
    source: Literal["transaction"] = "transaction"


class LedgerTopupItem(BaseModel):
    record_type: Literal["topup"] = "topup"
    source: Literal["operator_topup", "sim_topup"]
    id: int
    reference: str
    created_at: datetime
    amount_cents: int
    status: Literal["successful"] = "successful"
    operator_code: str
    operator_name: str
    sim_card_id: int | None = None
    sim_display_name: str | None = None
    performed_by_user_id: int | None = None
    performed_by_username: str | None = None


class TransactionLedgerPage(BaseModel):
    items: list[Annotated[LedgerSaleItem | LedgerTopupItem, Field(discriminator="record_type")]]
    total: int
    limit: int
    offset: int


@app.get("/api/transaction-ledger", response_model=TransactionLedgerPage)
async def transaction_ledger(
    request: Request,
    db: DbSession,
    record_type: Literal["all", "sale", "topup"] = "all",
    operator_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    service_code: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    performed_by: Annotated[int | None, Query(ge=1, le=MAX_MINOR_UNITS)] = None,
    customer_phone: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    status: Literal["successful", "failed"] = "successful",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_MINOR_UNITS)] = 0,
) -> TransactionLedgerPage:
    """Successful sales and persisted top-ups; read-only, not a revenue report.

    SQL UNION ALL is ordered by (created_at DESC, source DESC, id DESC), then
    paginated. IDs are source-local: (source, id) identifies a ledger record.
    Operator filters both types. Sale-only filters require record_type=sale
    (422 otherwise), never silently remove top-ups from an all-types view.
    status=failed returns an empty page: failed sales/attempts are never listed.
    The original /api/transactions remains the sales-management API.
    Offset pages describe current history, not a frozen snapshot across writes.
    """
    if record_type != "sale" and any(value is not None for value in (service_code, performed_by, customer_phone)):
        raise HTTPException(status_code=422, detail="Sale-only filters require record_type=sale")
    if status == "failed":
        return TransactionLedgerPage(items=[], total=0, limit=limit, offset=offset)

    sales = (
        select(literal("transaction").label("source"), Transaction.id, Transaction.created_at)
        .join(Service, Transaction.service_id == Service.id)
        .join(Operator, Service.operator_id == Operator.id)
        .where(Transaction.status == "successful", Transaction.deleted_at.is_(None))
    )
    operator_topups = select(
        literal("operator_topup").label("source"), OperatorTopup.id, OperatorTopup.created_at
    ).join(Operator, OperatorTopup.operator_id == Operator.id)
    sim_topups = select(
        literal("sim_topup").label("source"), SimLedger.id, SimLedger.created_at
    ).join(SimCard, SimLedger.sim_card_id == SimCard.id).join(
        Operator, SimCard.operator_id == Operator.id
    ).where(SimLedger.kind == "topup")
    if operator_code is not None:
        sales = sales.where(Operator.code == operator_code)
        operator_topups = operator_topups.where(Operator.code == operator_code)
        sim_topups = sim_topups.where(Operator.code == operator_code)
    if service_code is not None:
        sales = sales.where(Service.code == service_code)
    if performed_by is not None:
        sales = sales.where(Transaction.performed_by_user_id == performed_by)
    if customer_phone is not None:
        sales = sales.where(Transaction.customer_phone.ilike(f"%{customer_phone}%"))
    sources = [sales] if record_type == "sale" else [operator_topups, sim_topups]
    if record_type == "all":
        sources.insert(0, sales)
    combined = union_all(*sources).subquery()
    total = await db.scalar(select(func.count()).select_from(combined))
    keys = (await db.execute(select(combined).order_by(
        combined.c.created_at.desc(), combined.c.source.desc(), combined.c.id.desc()
    ).limit(limit).offset(offset))).all()
    context = _auth_context(request)
    items = {}
    sale_ids = [row.id for row in keys if row.source == "transaction"]
    if sale_ids:
        rows = await db.execute(_transaction_list_statement().where(Transaction.id.in_(sale_ids)))
        for sale in _list_items(rows.all(), context):
            items[("transaction", sale.id)] = LedgerSaleItem(**sale.model_dump())
    operator_ids = [row.id for row in keys if row.source == "operator_topup"]
    if operator_ids:
        rows = await db.execute(select(
            OperatorTopup, Operator.code, Operator.name
        ).join(Operator, OperatorTopup.operator_id == Operator.id).where(OperatorTopup.id.in_(operator_ids)))
        for entry, code, name in rows:
            items[("operator_topup", entry.id)] = LedgerTopupItem(
                source="operator_topup", id=entry.id, reference=entry.reference,
                amount_cents=entry.amount_cents, created_at=entry.created_at,
                operator_code=code, operator_name=name,
            )
    sim_ids = [row.id for row in keys if row.source == "sim_topup"]
    if sim_ids:
        rows = await db.execute(select(
            SimLedger, SimCard, Operator.code, Operator.name, User.username
        ).join(SimCard, SimLedger.sim_card_id == SimCard.id).join(
            Operator, SimCard.operator_id == Operator.id
        ).join(User, SimLedger.performed_by_user_id == User.id).where(SimLedger.id.in_(sim_ids)))
        for entry, sim, code, name, username in rows:
            visible = context.role == "admin" or (sim.is_shared and sim.archived_at is None) or (
                not sim.is_shared and sim.owner_user_id == context.user_id
            )
            items[("sim_topup", entry.id)] = LedgerTopupItem(
                source="sim_topup", id=entry.id, reference=entry.reference,
                amount_cents=entry.amount_cents, created_at=entry.created_at,
                operator_code=code, operator_name=name,
                sim_card_id=sim.id if visible else None,
                sim_display_name=sim.display_name if visible else None,
                performed_by_user_id=entry.performed_by_user_id, performed_by_username=username,
            )
    return TransactionLedgerPage(
        items=[items[(row.source, row.id)] for row in keys],
        total=total, limit=limit, offset=offset,
    )


@app.get("/api/currency/denomination", response_model=CurrencyDenominationResponse)
async def get_active_denomination(db: DbSession) -> CurrencyDenomination:
    """Return the active currency denomination."""
    denom = await db.scalar(
        select(CurrencyDenomination).where(CurrencyDenomination.is_active == True)
    )
    if denom is None:
        raise HTTPException(status_code=404, detail="No active currency denomination found")
    return denom


@app.put("/api/currency/denomination", response_model=CurrencyDenominationResponse)
async def set_active_denomination(data: SetDenomination, db: DbSession) -> CurrencyDenomination:
    """Set active denomination (deactivate all others)."""
    denom = await db.scalar(
        select(CurrencyDenomination).where(CurrencyDenomination.scale_name == data.scale_name)
    )
    if denom is None:
        raise HTTPException(status_code=404, detail="Currency denomination not found")

    # Deactivate all others
    await db.execute(
        select(CurrencyDenomination).where(CurrencyDenomination.is_active == True)
    )
    all_denoms = await db.scalars(select(CurrencyDenomination).where(CurrencyDenomination.is_active == True))
    for d in all_denoms:
        d.is_active = False
        await db.flush()

    denom.is_active = True
    await db.commit()
    await db.refresh(denom)

    return denom


def format_amount(amount_cents: int, denomination: CurrencyDenomination) -> str:
    """Format amount in Syrian Pounds with ل.س symbol."""
    multiplier = denomination.multiplier if denomination else 1
    value = amount_cents / multiplier
    return f"{value:,.2f} ل.س"


# --- Cash Register Endpoints ---
# --- Cash Register Endpoints ---


@app.post("/api/register/open", response_model=CashRegisterResponse)
async def open_register(
    data: CashRegisterCreate,
    db: DbSession,
) -> CashRegister:
    """Open a new register. Fail if one is already open."""
    # Check if a register is already open
    existing = await db.scalar(
        select(CashRegister).where(CashRegister.status == "open")
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A register is already open")

    register = CashRegister(
        opening_balance_cents=data.opening_balance_cents,
        status="open",
        opened_at=datetime.now(timezone.utc),
        closed_at=None,
    )
    db.add(register)
    await db.commit()
    await db.refresh(register)

    return register


@app.post("/api/register/close", response_model=CashRegisterResponse)
async def close_register(
    db: DbSession,
    register_id: int,
) -> CashRegister:
    """Close the current open register, compute closing balance."""
    register = await db.get(CashRegister, register_id)
    if register is None:
        raise HTTPException(status_code=404, detail="Register not found")

    if register.status != "open":
        raise HTTPException(status_code=400, detail="Can only close an open register")

    # Cash collected: the amount of every committed (successful) transaction in
    # the register window. Failed and removed rows never count as cash; a
    # removal is a reporting tombstone and reverses nothing.
    transactions_result = await db.scalars(
        select(Transaction)
        .where(
            Transaction.created_at >= register.opened_at,
            Transaction.status == "successful",
            Transaction.deleted_at.is_(None),
        )
    )
    transactions = transactions_result.all()
    cash_collected = sum(t.amount_cents for t in transactions)

    register.closing_balance_cents = register.opening_balance_cents + cash_collected
    register.status = "closed"
    register.closed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(register)

    return register


@app.get("/api/register/summary", response_model=RegisterSummary)
async def get_register_summary(
    db: DbSession,
) -> RegisterSummary:
    """Return summary for current/open register, or last closed one."""
    # Try to find open register first
    register = await db.scalar(
        select(CashRegister).where(CashRegister.status == "open")
    )

    if register is None:
        # Fall back to most recent closed register
        register = await db.scalar(
            select(CashRegister).where(CashRegister.status == "closed").order_by(CashRegister.closed_at.desc())
        )

    if register is None:
        raise HTTPException(status_code=404, detail="No register found")

    # Cash collected: the amount of every committed (successful) transaction in
    # the register window. Failed and removed rows never count as cash; a
    # removal is a reporting tombstone and reverses nothing.
    transactions_result = await db.scalars(
        select(Transaction)
        .where(
            Transaction.created_at >= register.opened_at,
            Transaction.status == "successful",
            Transaction.deleted_at.is_(None),
        )
    )
    transactions = transactions_result.all()
    cash_collected = sum(t.amount_cents for t in transactions)

    closing_balance = register.opening_balance_cents + cash_collected

    return RegisterSummary(
        opening_balance_cents=register.opening_balance_cents,
        cash_collected_cents=cash_collected,
        closing_balance_cents=closing_balance if register.status == "closed" else None,
        transaction_count=len(transactions),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=56123)
