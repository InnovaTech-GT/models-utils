import uuid
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database_utils.database import Base
from database_utils.models.auth import (
    User, Company, Tier, EmailVerificationToken, PasswordResetToken
)
from database_utils.utils.timezone_utils import now_gt
from database_utils.utils.password import hash_password

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine)

# Scope create_all/drop_all to only the tables this test actually needs.
# Base.metadata now includes Postgres-only DDL from unrelated models (e.g.
# isp.py's TopologyPlaybook CHECK constraint uses the PG regex operator `~`,
# Cycle 3 revision c3a_topology_purpose) that sqlite cannot parse — this test
# only exercises User/Company/Tier/*Token, so it has no business creating the
# rest of the schema.
_TEST_TABLES = [
    Tier.__table__, Company.__table__, User.__table__,
    EmailVerificationToken.__table__, PasswordResetToken.__table__,
]


def _make_user(db):
    tier = Tier(name=f"T-{uuid.uuid4().hex[:8]}", price=0.0)
    db.add(tier)
    db.flush()
    company = Company(name=f"C-{uuid.uuid4().hex[:8]}", tier_id=tier.id)
    db.add(company)
    db.flush()
    user = User(
        name="Jane Doe",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        age=30,
        password_hash=hash_password("secret123"),
        company_id=company.id,
    )
    db.add(user)
    db.flush()
    return user


def test_user_defaults_to_email_unverified():
    Base.metadata.create_all(bind=engine, tables=_TEST_TABLES)
    db = TestingSession()
    try:
        user = _make_user(db)
        db.commit()
        assert user.email_verified is False
    finally:
        Base.metadata.drop_all(bind=engine, tables=_TEST_TABLES)
        db.close()


def test_email_verification_token_roundtrip():
    Base.metadata.create_all(bind=engine, tables=_TEST_TABLES)
    db = TestingSession()
    try:
        user = _make_user(db)
        token = EmailVerificationToken(
            user_id=user.id,
            token="abc123",
            expires_at=now_gt() + timedelta(hours=24),
        )
        db.add(token)
        db.commit()
        fetched = db.query(EmailVerificationToken).filter_by(token="abc123").first()
        assert fetched is not None
        assert fetched.used_at is None
        assert fetched.user_id == user.id
    finally:
        Base.metadata.drop_all(bind=engine, tables=_TEST_TABLES)
        db.close()


def test_password_reset_token_roundtrip():
    Base.metadata.create_all(bind=engine, tables=_TEST_TABLES)
    db = TestingSession()
    try:
        user = _make_user(db)
        token = PasswordResetToken(
            user_id=user.id,
            token="xyz789",
            expires_at=now_gt() + timedelta(hours=1),
        )
        db.add(token)
        db.commit()
        fetched = db.query(PasswordResetToken).filter_by(token="xyz789").first()
        assert fetched is not None
        assert fetched.used_at is None
    finally:
        Base.metadata.drop_all(bind=engine, tables=_TEST_TABLES)
        db.close()
