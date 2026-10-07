"""Client short codes (cc1): a small, human-friendly, per-company unique id.

Legacy clients keep the id their old system gave them (`[LEGACY_ID:CO0648]`,
backfilled by revision cc1_client_code); every other client gets a random
6-character code at creation, which the office may override.

The alphabet drops 0/O/1/I/L so a code read aloud or off a label is never
ambiguous. 31^6 ≈ 887M codes, so a per-company collision is vanishingly rare;
the unique index is still the arbiter and callers retry on it.

The SAME alphabet and length are hand-kept in the cc1 migration's
`client_code_generate()` SQL function (the DB default that covers writers
which predate this column).
"""
import re
import secrets

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6
CODE_MAX_LENGTH = 16
CODE_PATTERN = re.compile(r"^[A-Z0-9-]{1,16}$")


def generate_client_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_client_code(value: str) -> str:
    """Trim + uppercase an operator-supplied code; raise ValueError if invalid."""
    code = (value or "").strip().upper()
    if not CODE_PATTERN.match(code):
        raise ValueError(
            "code must be 1-16 characters: letters A-Z, digits 0-9 and '-'"
        )
    return code
