"""Admin-set passwords need 8 characters, like invitation/reset/signup
(security audit #94). Placeholder values only."""
import pytest
from pydantic import ValidationError

from database_utils.schemas.user import SuperAdminCreate, UserCreate, UserUpdate

_BASE = {"name": "n", "email": "a@b.gt", "age": 30}


@pytest.mark.parametrize("cls", [UserCreate, SuperAdminCreate])
def test_seven_chars_refused(cls):
    with pytest.raises(ValidationError):
        cls(password="1234567", **_BASE)
    assert cls(password="12345678", **_BASE)


def test_update_seven_chars_refused():
    with pytest.raises(ValidationError):
        UserUpdate(password="1234567")
    assert UserUpdate(password="12345678").password == "12345678"
    assert UserUpdate().password is None
