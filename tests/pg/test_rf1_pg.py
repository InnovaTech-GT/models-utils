"""rf1_referral_programs on a real Postgres at head (doc 69 §5): the ON DELETE
rules keep the referral history a company delete should keep, every CHECK /
UNIQUE / index of §5 exists by name on the migrated schema (the SQLite suite
only sees create_all), they refuse bad rows, and pre-rf1 rows read 0 in the new
money columns. Each test runs in one rolled-back transaction."""
import os
import uuid

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

# table -> (CHECK names, UNIQUE constraint names, index names) added by rf1
EXPECTED = {
    "referral": ({"ck_referral_not_self"}, {"uq_referral_referred_company_id"},
                 {"ix_referral_referrer_company_id"}),
    "referral_reward": ({"ck_referral_reward_kind", "ck_referral_reward_status", "ck_referral_reward_amount_nonneg",
                         "ck_referral_reward_remaining", "ck_referral_reward_clawed_nonneg"},
                        {"uq_referral_reward_referral_kind"}, {"ix_referral_reward_company_status_expires"}),
    "billing_reversal": ({"ck_billing_reversal_kind"}, {"uq_billing_reversal_gateway_id"}, set()),
    "billing_card": (set(), set(), {"ix_billing_card_company_id", "ix_billing_card_card_key"}),
    "company_identity": ({"ck_company_identity_kind"}, {"uq_company_identity_company_kind_value"},
                         {"ix_company_identity_company_id", "ix_company_identity_kind_value"}),
    "referral_identity": ({"ck_referral_identity_kind"}, {"uq_referral_identity_referral_kind_value"}, set()),
}
# existing tables: only the rf1 names must be present
EXPECTED_EXISTING = {
    "company": (set(), {"uq_company_referral_code"}, set()),
    "subscription": ({"ck_subscription_balance_due_nonneg"}, set(), set()),
    "billing_invoice": ({f"ck_billing_invoice_{c}_nonneg" for c in
                         ("credit_cents", "previous_balance_cents", "carried_out_cents", "refunded_cents")},
                        set(), {"ix_billing_invoice_recurrente_payment_id"}),
}


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
    engine.dispose()


def _x(conn, sql, **params):
    return conn.execute(sa.text(sql), params)


def _refused(conn, fn):
    with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
        fn()


def _company(conn, code=None):
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id, referral_code) "
             "SELECT :id, now(), :n, id, :code FROM tier LIMIT 1", id=co, n=f"rf1-{co}", code=code)
    return co


def _invoice(conn, co, extra_cols="", extra_vals=""):
    sub, inv = uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO subscription (id, created_at, updated_at, status, billing_type, current_period_start, "
             "current_period_end, cancel_at_period_end, company_id, tier_id) "
             "SELECT :id, now(), now(), 'ACTIVE', 'MANUAL', now(), now() + interval '30 days', false, :co, tier_id "
             "FROM company WHERE id = :co", id=sub, co=co)
    _x(conn, f"INSERT INTO billing_invoice (id, created_at, invoice_number, invoice_date, due_date, subtotal, tax, "
             f"total, status, subscription_id{extra_cols}) VALUES (:id, now(), :num, now(), now(), 80000, 0, 80000, "
             f"'PAID', :sub{extra_vals})", id=inv, num=f"U-{inv.hex[:10]}", sub=sub)
    return sub, inv


def _referral(conn, referrer, referred):
    rid = uuid.uuid4()
    _x(conn, "INSERT INTO referral (id, created_at, referrer_company_id, referred_company_id, code, referred_nit, "
             "referred_email, referred_phone, nit_seen_before) VALUES (:id, now(), :a, :b, 'K7QX2M', '576937K', "
             "'a@example.com', '50255551234', false)", id=rid, a=referrer, b=referred)
    return rid


def _reward(conn, rid, co, kind="FREE_MONTH", status="PENDING_APPROVAL", amount=None, remaining=0, **links):
    wid = uuid.uuid4()
    _x(conn, "INSERT INTO referral_reward (id, created_at, updated_at, referral_id, company_id, kind, status, "
             "amount_cents, remaining_cents, source_invoice_id, unlock_invoice_id, reversal_id) "
             "VALUES (:id, now(), now(), :r, :co, :k, :s, :a, :rem, :src, :unl, :rev)",
       id=wid, r=rid, co=co, k=kind, s=status, a=amount, rem=remaining,
       src=links.get("src"), unl=links.get("unl"), rev=links.get("rev"))
    return wid


def _world(conn):
    referrer, referred = _company(conn), _company(conn)
    rid = _referral(conn, referrer, referred)
    free = _reward(conn, rid, referred)
    credit = _reward(conn, rid, referrer, kind="REFERRER_CREDIT", amount=80000)
    return referrer, referred, rid, free, credit


def _names(conn, table):
    checks = set(_x(conn, "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) "
                          "AND contype = 'c'", t=table).scalars())
    uniques = set(_x(conn, "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) "
                           "AND contype = 'u'", t=table).scalars())
    idx = set(_x(conn, "SELECT indexname FROM pg_indexes WHERE tablename = :t", t=table).scalars())
    return checks, uniques, idx


def test_head_has_rf1_constraints_and_indexes(conn):
    for table, (checks, uniques, idx) in EXPECTED.items():
        got_c, got_u, got_i = _names(conn, table)
        assert got_c == checks, table
        assert got_u == uniques, table
        assert got_i == idx | uniques | {f"{table}_pkey"}, table
    for table, (checks, uniques, idx) in EXPECTED_EXISTING.items():
        got_c, got_u, got_i = _names(conn, table)
        assert checks <= got_c and uniques <= got_u and idx <= got_i, table


def test_company_delete_keeps_history(conn):
    referrer, referred, rid, free, credit = _world(conn)
    ident = uuid.uuid4()
    _x(conn, "INSERT INTO company_identity (id, created_at, company_id, kind, value) "
             "VALUES (:id, now(), :co, 'NIT', '576937K')", id=ident, co=referred)
    _x(conn, "INSERT INTO billing_card (id, created_at, company_id, payment_method_id, card_key) "
             "VALUES (:id, now(), :co, 'pm_1', 'visa:4242:12/30')", id=uuid.uuid4(), co=referred)
    _x(conn, "DELETE FROM company WHERE id = :co", co=referred)
    row = _x(conn, "SELECT referrer_company_id, referred_company_id FROM referral WHERE id = :r", r=rid).one()
    assert tuple(row) == (referrer, None)
    assert _x(conn, "SELECT company_id FROM company_identity WHERE id = :i", i=ident).scalars().all() == [None]
    assert _x(conn, "SELECT count(*) FROM billing_card WHERE company_id = :co", co=referred).scalar() == 0
    assert _x(conn, "SELECT id FROM referral_reward WHERE referral_id = :r", r=rid).scalars().all() == [credit]

    _x(conn, "DELETE FROM company WHERE id = :co", co=referrer)
    row = _x(conn, "SELECT referrer_company_id, referred_company_id FROM referral WHERE id = :r", r=rid).one()
    assert tuple(row) == (None, None)
    assert _x(conn, "SELECT count(*) FROM referral_reward WHERE referral_id = :r", r=rid).scalar() == 0


def test_referral_delete_cascades_rewards_and_identity(conn):
    _, _, rid, _, _ = _world(conn)
    _x(conn, "INSERT INTO referral_identity (id, created_at, referral_id, kind, value) "
             "VALUES (:id, now(), :r, 'CARD', 'visa:4242:12/30')", id=uuid.uuid4(), r=rid)
    _x(conn, "DELETE FROM referral WHERE id = :r", r=rid)
    assert _x(conn, "SELECT count(*) FROM referral_reward WHERE referral_id = :r", r=rid).scalar() == 0
    assert _x(conn, "SELECT count(*) FROM referral_identity WHERE referral_id = :r", r=rid).scalar() == 0


def test_invoice_delete_nulls_reward_and_reversal_links(conn):
    referrer, referred = _company(conn), _company(conn)
    _, inv = _invoice(conn, referrer)
    rid = _referral(conn, referrer, referred)
    wid = _reward(conn, rid, referrer, kind="REFERRER_CREDIT", amount=100, src=inv, unl=inv)
    rev = uuid.uuid4()
    _x(conn, "INSERT INTO billing_reversal (id, created_at, updated_at, kind, gateway_id, invoice_id) "
             "VALUES (:id, now(), now(), 'REFUND', :g, :inv)", id=rev, g=f"re_{rev.hex[:8]}", inv=inv)
    _x(conn, "DELETE FROM billing_invoice WHERE id = :i", i=inv)
    assert tuple(_x(conn, "SELECT source_invoice_id, unlock_invoice_id FROM referral_reward WHERE id = :w",
                    w=wid).one()) == (None, None)
    assert _x(conn, "SELECT invoice_id FROM billing_reversal WHERE id = :r", r=rev).scalar() is None


def test_reward_reversal_fk_set_null(conn):
    referrer, referred = _company(conn), _company(conn)
    rid = _referral(conn, referrer, referred)
    rev = uuid.uuid4()
    _x(conn, "INSERT INTO billing_reversal (id, created_at, updated_at, kind, gateway_id) "
             "VALUES (:id, now(), now(), 'DISPUTE', :g)", id=rev, g=f"di_{rev.hex[:8]}")
    wid = _reward(conn, rid, referrer, kind="REFERRER_CREDIT", status="REVERSED", amount=100, rev=rev)
    _x(conn, "DELETE FROM billing_reversal WHERE id = :r", r=rev)
    assert _x(conn, "SELECT reversal_id FROM referral_reward WHERE id = :w", w=wid).scalar() is None


def test_rf1_constraints_refuse_on_migrated_db(conn):
    referrer, referred, rid, _, _ = _world(conn)
    _refused(conn, lambda: _reward(conn, rid, referred))                      # 2nd FREE_MONTH
    _refused(conn, lambda: _referral(conn, _company(conn), referred))         # referred twice
    _refused(conn, lambda: _referral(conn, referrer, referrer))               # self
    _company(conn, code="K7QX2M")
    _refused(conn, lambda: _company(conn, code="K7QX2M"))
    other = _referral(conn, referrer, _company(conn))
    _refused(conn, lambda: _reward(conn, other, referrer, kind="REFERRER_CREDIT", amount=100, remaining=101))
    _refused(conn, lambda: _reward(conn, other, referrer, kind="REFERRER_CREDIT", remaining=1))
    _refused(conn, lambda: _reward(conn, other, referrer, status="BOGUS"))
    _refused(conn, lambda: _reward(conn, other, referrer, kind="X"))
    sub, inv = _invoice(conn, referrer)
    _refused(conn, lambda: _x(conn, "UPDATE subscription SET balance_due_cents = -1 WHERE id = :s", s=sub))
    _refused(conn, lambda: _x(conn, "UPDATE billing_invoice SET credit_cents = -1 WHERE id = :i", i=inv))
    _refused(conn, lambda: _x(conn, "INSERT INTO company_identity (id, created_at, company_id, kind, value) "
                                    "VALUES (:id, now(), :co, 'CARD', 'x')", id=uuid.uuid4(), co=referrer))
    _refused(conn, lambda: _x(conn, "INSERT INTO billing_reversal (id, created_at, updated_at, kind, gateway_id) "
                                    "VALUES (:id, now(), now(), 'X', 're_bad')", id=uuid.uuid4()))


def test_existing_rows_get_zero_in_new_columns(conn):
    rf1 = load("versions/rf1_referral_programs.py", "rf1_referral_programs")
    with Operations.context(MigrationContext.configure(conn)):
        rf1.downgrade()
        rf1.downgrade()  # idempotent
        assert _x(conn, "SELECT to_regclass('referral')").scalar() is None
        co = uuid.uuid4()
        _x(conn, "INSERT INTO company (id, created_at, name, tier_id) SELECT :id, now(), :n, id FROM tier LIMIT 1",
           id=co, n=f"rf1-{co}")
        sub, inv = _invoice(conn, co)
        rf1.upgrade()
        rf1.upgrade()  # idempotent
    assert tuple(_x(conn, "SELECT balance_due_cents, partner_since FROM subscription WHERE id = :s",
                    s=sub).one()) == (0, None)
    assert tuple(_x(conn, "SELECT credit_cents, previous_balance_cents, carried_out_cents, refunded_cents, "
                          "credit_detail, recurrente_payment_id FROM billing_invoice WHERE id = :i",
                    i=inv).one()) == (0, 0, 0, 0, None, None)
    assert _x(conn, "SELECT referral_code FROM company WHERE id = :c", c=co).scalar() is None
    assert _x(conn, "SELECT to_regclass('referral_reward')").scalar() == "referral_reward"
