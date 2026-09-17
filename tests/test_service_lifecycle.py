"""Unit tests for the service-lifecycle status machine (founder decisions 3/5).

Covers: PURPOSE_TO_STATUS (including ACTIVATION -> None), ALLOWED_TRANSITIONS,
and purpose_allowed_for_status across every (purpose, status) pair plus the
tenant-custom-purpose fallthrough.
"""
import pytest

from database_utils.models.isp import (
    ALLOWED_TRANSITIONS,
    CANONICAL_PLAYBOOK_PURPOSES,
    ClientServiceStatus,
    PURPOSE_ACTIVATION,
    PURPOSE_DEPROVISION,
    PURPOSE_REACTIVATION,
    PURPOSE_SUSPENSION,
    PURPOSE_TO_STATUS,
    purpose_allowed_for_status,
)

PENDING = ClientServiceStatus.PENDING_INSTALL
ACTIVE = ClientServiceStatus.ACTIVE
SUSPENDED = ClientServiceStatus.SUSPENDED
CANCELLED = ClientServiceStatus.CANCELLED


# --- constants ---

def test_activation_maps_to_no_status_write():
    """Founder decision 5: activation ENQUEUES ONLY — recompute_install_state
    flips PENDING_INSTALL -> ACTIVE after the job succeeds."""
    assert PURPOSE_TO_STATUS[PURPOSE_ACTIVATION] is None


def test_purpose_to_status_covers_every_canonical_purpose():
    assert set(PURPOSE_TO_STATUS) == set(CANONICAL_PLAYBOOK_PURPOSES)


def test_purpose_to_status_targets():
    assert PURPOSE_TO_STATUS[PURPOSE_SUSPENSION] == SUSPENDED
    assert PURPOSE_TO_STATUS[PURPOSE_REACTIVATION] == ACTIVE
    assert PURPOSE_TO_STATUS[PURPOSE_DEPROVISION] == CANCELLED


def test_allowed_transitions_shape():
    assert ALLOWED_TRANSITIONS[PENDING] == {ACTIVE, CANCELLED}
    assert ALLOWED_TRANSITIONS[ACTIVE] == {SUSPENDED, CANCELLED}
    assert ALLOWED_TRANSITIONS[SUSPENDED] == {ACTIVE, CANCELLED}


def test_cancelled_is_terminal():
    assert ALLOWED_TRANSITIONS[CANCELLED] == set()


# --- purpose_allowed_for_status: full matrix ---

@pytest.mark.parametrize("purpose,status,expected", [
    # ACTIVATION is legal ONLY from PENDING_INSTALL (no Activate button on an
    # already-active service; reviving a suspended one is REACTIVATION's job).
    (PURPOSE_ACTIVATION, PENDING, True),
    (PURPOSE_ACTIVATION, ACTIVE, False),
    (PURPOSE_ACTIVATION, SUSPENDED, False),
    (PURPOSE_ACTIVATION, CANCELLED, False),
    # SUSPENSION -> SUSPENDED: only ACTIVE allows it.
    (PURPOSE_SUSPENSION, PENDING, False),
    (PURPOSE_SUSPENSION, ACTIVE, True),
    (PURPOSE_SUSPENSION, SUSPENDED, False),
    (PURPOSE_SUSPENSION, CANCELLED, False),
    # REACTIVATION -> ACTIVE: the inverse of SUSPENSION, so SUSPENDED ONLY.
    # PENDING_INSTALL -> ACTIVE is a legal edge of the machine, but it belongs
    # to ACTIVATION (written by recompute_install_state after the job lands),
    # NOT to a reactivation status write.
    (PURPOSE_REACTIVATION, PENDING, False),
    (PURPOSE_REACTIVATION, ACTIVE, False),
    (PURPOSE_REACTIVATION, SUSPENDED, True),
    (PURPOSE_REACTIVATION, CANCELLED, False),
    # DEPROVISION -> CANCELLED: legal from every non-terminal status.
    (PURPOSE_DEPROVISION, PENDING, True),
    (PURPOSE_DEPROVISION, ACTIVE, True),
    (PURPOSE_DEPROVISION, SUSPENDED, True),
    (PURPOSE_DEPROVISION, CANCELLED, False),
])
def test_purpose_allowed_matrix(purpose, status, expected):
    assert purpose_allowed_for_status(purpose, status) is expected


def test_reactivation_is_suspended_only_regression():
    """Regression (service-lifecycle adversarial review, BLOCKER).

    REACTIVATION used to fall through to the generic ALLOWED_TRANSITIONS
    lookup. Its target is ACTIVE and ACTIVE is a legal transition out of
    PENDING_INSTALL, so a brand-new PENDING_INSTALL service on a topology with
    a REACTIVATION playbook showed a "Reactivate" button; pressing it wrote
    status=ACTIVE + activation_date + billing_status=ACTIVE +
    next_generation_date, i.e. it started billing a subscriber whose install
    never happened and whose CPE was never linked. It also disagreed with the
    legacy POST /{id}/reactivate endpoint, which 400s on the same input.

    REACTIVATION is the inverse of SUSPENSION: SUSPENDED and nothing else.
    """
    assert purpose_allowed_for_status(PURPOSE_REACTIVATION, SUSPENDED) is True
    for illegal in (PENDING, ACTIVE, CANCELLED):
        assert purpose_allowed_for_status(PURPOSE_REACTIVATION, illegal) is False


def test_reactivation_and_suspension_are_exact_inverses():
    """Whatever status SUSPENSION can leave, REACTIVATION can return to, and
    vice versa — the pair must never drift apart."""
    for status in (PENDING, ACTIVE, SUSPENDED, CANCELLED):
        suspendable = purpose_allowed_for_status(PURPOSE_SUSPENSION, status)
        reactivatable = purpose_allowed_for_status(PURPOSE_REACTIVATION, status)
        assert not (suspendable and reactivatable)
    assert purpose_allowed_for_status(PURPOSE_SUSPENSION, ACTIVE) is True
    assert purpose_allowed_for_status(PURPOSE_REACTIVATION, SUSPENDED) is True


def test_accepts_status_as_plain_string():
    assert purpose_allowed_for_status(PURPOSE_SUSPENSION, "ACTIVE") is True
    assert purpose_allowed_for_status(PURPOSE_SUSPENSION, "CANCELLED") is False


def test_accepts_unnormalized_purpose():
    assert purpose_allowed_for_status(" suspension ", ACTIVE) is True


def test_unknown_status_is_not_allowed_and_does_not_raise():
    assert purpose_allowed_for_status(PURPOSE_SUSPENSION, "NOT_A_STATUS") is False


def test_none_purpose_is_not_allowed():
    assert purpose_allowed_for_status(None, ACTIVE) is False


# --- custom (tenant-defined) purposes ---

@pytest.mark.parametrize("status", [PENDING, ACTIVE, SUSPENDED, CANCELLED])
def test_custom_purpose_falls_through_to_allowed(status):
    """Purposes are tenant-extensible free strings. A custom one is
    enqueue-only — it writes no status, so there is no transition to police,
    and it must never raise."""
    assert purpose_allowed_for_status("FIRMWARE_UPGRADE", status) is True


def test_custom_purpose_normalized_form_also_falls_through():
    assert purpose_allowed_for_status("firmware-upgrade", ACTIVE) is True


def test_custom_purpose_does_not_leak_into_purpose_to_status():
    assert "FIRMWARE_UPGRADE" not in PURPOSE_TO_STATUS
