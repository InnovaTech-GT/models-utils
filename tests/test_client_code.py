"""cc1: client short codes — generator, normalization, schemas, model default."""
import uuid

import pytest
from pydantic import ValidationError

from database_utils.models.crm import Client
from database_utils.schemas.client import ClientCreate, ClientUpdate
from database_utils.utils.client_code import (
    CODE_ALPHABET, CODE_LENGTH, generate_client_code, normalize_client_code,
)


def test_generated_codes_use_the_unambiguous_alphabet():
    for _ in range(200):
        code = generate_client_code()
        assert len(code) == CODE_LENGTH
        assert set(code) <= set(CODE_ALPHABET)
    assert not set("01OIL") & set(CODE_ALPHABET)


@pytest.mark.parametrize("raw,expected", [(" co0648 ", "CO0648"), ("c0971", "C0971"), ("ab-12", "AB-12")])
def test_normalize_trims_and_uppercases(raw, expected):
    assert normalize_client_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "CO 0648", "CO_0648", "ñ1", "X" * 17, "a\nb"])
def test_normalize_rejects_bad_codes(raw):
    with pytest.raises(ValueError):
        normalize_client_code(raw)


def test_schemas_normalize_and_treat_blank_as_unset():
    assert ClientCreate(name="A", tax_id=None, address=None, phone=None, email=None,
                        contact=None, observations=None, code=" co1 ").code == "CO1"
    assert ClientCreate(name="A", tax_id=None, address=None, phone=None, email=None,
                        contact=None, observations=None, code="  ").code is None
    assert ClientUpdate(code="x-9").code == "X-9"
    assert ClientUpdate().code is None
    with pytest.raises(ValidationError):
        ClientUpdate(code="bad code")


def test_model_default_fills_a_code(db):
    client = Client(id=uuid.uuid4(), name="Ana", company_id=uuid.uuid4())
    db.add(client)
    db.flush()
    assert len(client.code) == CODE_LENGTH


def test_migration_carries_the_same_alphabet():
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "alembic/versions/cc1_client_code.py").read_text()
    assert f"'{CODE_ALPHABET}'" in src
    assert "FOR i IN 1..%d LOOP" % CODE_LENGTH in src
