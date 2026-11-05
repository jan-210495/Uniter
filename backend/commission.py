from decimal import Decimal, InvalidOperation, localcontext
from typing import TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

if __package__:
    from .database import get_setting
else:
    from database import get_setting

DEFAULT_COMMISSION_RATE = Decimal("0.05")


class CommissionResult(TypedDict):
    amount_cents: int
    commission_cents: int
    net_cents: int
    total_cents: int
    rate: str


def calculate_commission(amount_cents: int, rate: Decimal) -> CommissionResult:
    """Calculate commission in cents using Python's nearest-even rounding."""
    if amount_cents < 0:
        raise ValueError("Amount must not be negative")
    if not rate.is_finite() or not Decimal(0) <= rate <= Decimal(1):
        raise ValueError("Commission rate must be finite and between 0 and 1")
    with localcontext() as context:
        context.prec = max(context.prec, len(str(amount_cents)) + len(rate.as_tuple().digits) + 2)
        commission_cents = round(Decimal(amount_cents) * rate)
    return {
        "amount_cents": amount_cents,
        "commission_cents": commission_cents,
        # Retain the legacy net field for existing API clients.
        "net_cents": amount_cents - commission_cents,
        "total_cents": amount_cents + commission_cents,
        "rate": str(rate),
    }


async def get_commission_for_transaction(
    session: AsyncSession, amount_cents: int
) -> CommissionResult:
    stored_rate = await get_setting(session, "commission_rate")
    try:
        rate = DEFAULT_COMMISSION_RATE if stored_rate is None else Decimal(stored_rate)
    except InvalidOperation as exc:
        raise ValueError("Stored commission rate is not a valid decimal") from exc
    return calculate_commission(amount_cents, rate)
