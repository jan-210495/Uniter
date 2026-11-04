import pytest

from backend.validation import normalize_phone, validate_syrian_phone


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("0933123456", "0933123456"),
        ("+963933123456", "0933123456"),
        ("00963933123456", "0933123456"),
        ("  +963 933-123-456  ", "0933123456"),
        ("0944123456", "0944123456"),
        ("0955123456", "0955123456"),
        ("0966123456", "0966123456"),
        ("0987123456", "0987123456"),
        ("0999123456", "0999123456"),
    ],
)
def test_valid_syrian_mobile_numbers(phone: str, expected: str) -> None:
    assert validate_syrian_phone(phone) == expected
    assert normalize_phone(phone) == expected


@pytest.mark.parametrize(
    "phone",
    [
        "", "  ", "093312345", "09331234567",
        "0923123456", "0922123456", "0913123456", "0911123456",
        "0901123456", "0977123456",
        "+964933123456", "+9630933123456", "009630933123456",
        "963933123456", "09331234#6", "09331234*6", "093312345a", "٠٩٣٣١٢٣٤٥٦",
    ],
)
def test_invalid_syrian_mobile_numbers(phone: str) -> None:
    with pytest.raises(ValueError, match="Invalid Syrian mobile number"):
        validate_syrian_phone(phone)
    with pytest.raises(ValueError, match="Invalid Syrian mobile number"):
        normalize_phone(phone)
