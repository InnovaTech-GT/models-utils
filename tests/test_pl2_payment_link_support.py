"""pl2: user.is_system (machine accounts never take a tier seat) and the
PAYMENT_LINK owner type for a transfer receipt. The CHECK and the enum label
on a real database: tests/pg/test_pl2_pg.py."""
import inspect
import uuid

from conftest import CO_A
from pydantic import BaseModel

import database_utils.schemas.user as user_schemas
from database_utils.models import User
from database_utils.models.crm import UploadedFileOwnerType
from database_utils.utils.tier_limits import _count_resource


def _user(db, **kw):
    user = User(name="u", email=f"{uuid.uuid4().hex}@test.gt", age=30, password_hash="!",
                company_id=CO_A, **kw)
    db.add(user)
    db.commit()
    return user


def test_payment_link_owner_type():
    assert UploadedFileOwnerType.PAYMENT_LINK.value == "PAYMENT_LINK"
    assert UploadedFileOwnerType.PAYMENT.value == "PAYMENT"


def test_is_system_defaults_to_false(db):
    assert _user(db).is_system is False


def test_system_and_inactive_users_take_no_seat(db):
    _user(db)
    _user(db)
    _user(db, is_system=True)
    _user(db, active=False)
    assert _count_resource(db, CO_A, "users") == 2


def test_no_user_schema_can_set_is_system():
    for name, obj in vars(user_schemas).items():
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj.__module__ == user_schemas.__name__:
            assert "is_system" not in obj.model_fields, name
