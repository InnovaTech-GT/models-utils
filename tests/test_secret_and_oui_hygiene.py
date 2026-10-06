"""Two review fixes on top of the vpn/kind/Capa-3 cycle.

1. `_normalize_oui("")` used to return `""`, which split the "registered
   without an OUI" key space into NULL and '' — and `ac1`'s
   `uq_acs_registration_serial_no_oui` is partial on `oui IS NULL`, so two
   tenants could still both claim one serial (verified on a scratch Postgres:
   `('','SNSPLIT')` and `(NULL,'SNSPLIT')` both committed). Blank must
   normalize to None so the index covers every no-OUI row.

2. `handle_exceptions` logged `f"{args = } :: {kwargs = }"`, so any 500 inside a
   handler dumped its request body — including `DeviceCredentialCreate.secret`
   and `RotationStartRequest.secret`, i.e. a tenant's whole-fleet CWMP password
   — into loguru.
"""
from database_utils.schemas.acs_registration import (
    AcsRegistrationCreate,
    AcsRegistrationUpdate,
    _normalize_oui,
)


def test_blank_oui_normalizes_to_none_not_empty_string():
    assert _normalize_oui("") is None
    assert _normalize_oui("   ") is None
    assert _normalize_oui(None) is None
    assert _normalize_oui(" aabbcc ") == "AABBCC"


def test_no_write_path_can_persist_an_empty_string_oui():
    assert AcsRegistrationCreate(serial_number="sn1", oui="").oui is None
    assert AcsRegistrationUpdate(oui="").oui is None


def test_handle_exceptions_never_logs_argument_values():
    """The decorator's source must not interpolate args/kwargs themselves."""
    import inspect

    from database_utils.utils import error_handling

    src = inspect.getsource(error_handling.handle_exceptions)
    assert "{args = }" not in src
    assert "{kwargs = }" not in src
    # The surviving log line may name types and keyword KEYS only.
    assert "type(a).__name__" in src
    assert "sorted(kwargs)" in src
