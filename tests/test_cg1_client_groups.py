"""cg1: client groups (doc 68 §1.4) — the ClientGroup model, client.client_group_id
with its company-scoped viewonly relationship, the widened dunning_run.mode CHECK
and the client schemas."""
import uuid

import pytest
from conftest import CO_A, CO_B
from pydantic import ValidationError

from database_utils.models import Client, ClientGroup, DunningRun
from database_utils.schemas.client import ClientCreate, ClientGroupRef, ClientOut, ClientUpdate


def test_client_group_table_and_constraints():
    t = ClientGroup.__table__
    assert set(t.c.keys()) == {"id", "company_id", "name", "color", "auto_suspension_mode",
                               "min_overdue_orders", "created_at", "updated_at"}
    assert {"uq_client_group_company_name", "ck_client_group_color", "ck_client_group_mode",
            "ck_client_group_min"} <= {c.name for c in t.constraints}
    assert t.c.auto_suspension_mode.nullable and t.c.min_overdue_orders.nullable and not t.c.color.nullable


def test_client_column_and_run_mode_check():
    col = Client.__table__.c.client_group_id
    assert col.nullable and next(iter(col.foreign_keys)).ondelete == "SET NULL"
    assert "ix_client_client_group" in {i.name for i in Client.__table__.indexes}
    ck = next(c for c in DunningRun.__table__.constraints if c.name == "ck_dunning_run_mode")
    assert "'OFF'" in str(ck.sqltext)


def test_relationship_ignores_another_tenants_group(db):
    own = ClientGroup(company_id=CO_A, name="Gobierno")
    foreign = ClientGroup(company_id=CO_B, name="Ajeno")
    db.add_all([own, foreign])
    db.flush()
    assert own.color == "grey"
    mine = Client(company_id=CO_A, name="a", client_group_id=own.id)
    smuggled = Client(company_id=CO_A, name="b", client_group_id=foreign.id)
    db.add_all([mine, smuggled])
    db.commit()
    db.expire_all()
    assert mine.client_group.name == "Gobierno"
    assert smuggled.client_group is None


def test_client_schemas_carry_the_group():
    assert ClientUpdate(client_group_id=None).model_dump(exclude_unset=True) == {"client_group_id": None}
    assert "client_group_id" in ClientCreate.model_fields and "client_group" in ClientOut.model_fields
    ref = ClientGroupRef(id=uuid.uuid4(), name="x", color="blue")
    assert ref.color == "blue"
    with pytest.raises(ValidationError):
        ClientGroupRef(id=uuid.uuid4(), name="x", color="#ff0000")
