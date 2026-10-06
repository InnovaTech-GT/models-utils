"""Refresh-token store (bug-fix/refresh-token-reuse): jti claim + auth_refresh_token."""
import uuid
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database_utils.database import Base
from database_utils.models.auth import Company, RefreshToken, Tier, User
from database_utils.utils.jwt_utils import create_refresh_token, decode_token
from database_utils.utils.password import hash_password
from database_utils.utils.timezone_utils import now_gt

USER = SimpleNamespace(id=uuid.uuid4(), roles=[], company_id=uuid.uuid4(), is_super_admin=False)


def test_refresh_token_carries_unique_jti():
    a = decode_token(create_refresh_token(USER))
    b = decode_token(create_refresh_token(USER))
    assert a["jti"] and a["jti"] != b["jti"]


def test_refresh_token_explicit_jti():
    assert decode_token(create_refresh_token(USER, jti="abc"))["jti"] == "abc"


def test_refresh_token_row_roundtrip():
    engine = create_engine("sqlite:///:memory:")
    tables = [Tier.__table__, Company.__table__, User.__table__, RefreshToken.__table__]
    Base.metadata.create_all(engine, tables=tables)
    db = sessionmaker(bind=engine)()
    tier = Tier(name="T", price=0.0)
    db.add(tier)
    db.flush()
    company = Company(name="C", tier_id=tier.id)
    db.add(company)
    db.flush()
    user = User(name="U", email="u@example.com", age=1, password_hash=hash_password("x"),
                company_id=company.id)
    db.add(user)
    db.flush()
    fam = uuid.uuid4()
    db.add(RefreshToken(jti="j1", family_id=fam, user_id=user.id, company_id=company.id,
                        client_type="mobile", expires_at=now_gt() + timedelta(days=1)))
    db.commit()
    row = db.get(RefreshToken, "j1")
    assert row.family_id == fam and row.issued_at is not None
    assert row.rotated_at is None and row.replaced_by is None and row.revoked_at is None
    db.close()
