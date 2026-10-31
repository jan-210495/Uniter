import re


_ALLOWED_PREFIXES = ("093", "094", "095", "096", "099")
_ERROR = (
    "Invalid Syrian mobile number: expected 10 digits starting with "
    "093, 094, 095, 096, or 099"
)


def validate_syrian_phone(phone: str) -> str:
    """Return a Syrian mobile number in national format, or raise ValueError."""
    normalized = re.sub(r"[\s-]+", "", phone)
    if normalized.startswith("+963"):
        normalized = "0" + normalized[4:]
    elif normalized.startswith("00963"):
        normalized = "0" + normalized[5:]

    if not re.fullmatch(r"09[0-9]{8}", normalized) or not normalized.startswith(_ALLOWED_PREFIXES):
        raise ValueError(_ERROR)
    return normalized


def normalize_phone(phone: str) -> str:
    """Normalize a Syrian mobile number using the same validation rules."""
    return validate_syrian_phone(phone)
