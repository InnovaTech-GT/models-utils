"""
`client_service.network_managed` from run history (doc 71 §9.2, nm1).

`backfill_value` is the nm1 backfill predicate for one service, in SQLAlchemy.
backend-erp calls it when a Revertir closes an ACTIVATION (doc 71 §3.4), so a
revert of a re-ACTIVATION keeps `true` when an earlier one still stands. The
revision keeps its own frozen SQL copy (`BACKFILL_SQL`); a test runs both on
the same fixtures.
"""
from __future__ import annotations

from sqlalchemy import and_, exists, not_, select
from sqlalchemy.orm import aliased

from ..models.isp import (
    PURPOSE_ACTIVATION, PURPOSE_BOOST, PURPOSE_CPE_SWAP, PURPOSE_DEPROVISION,
    PURPOSE_REACTIVATION, PURPOSE_SUSPENSION, ProvisioningJobStatus, ProvisioningRun,
)

# A successful live run of any of these needs Uplink's scheme on the device:
# ACTIVATION / CPE_SWAP configured it; SUSPENSION / REACTIVATION / BOOST found
# the PPPoE user, so it exists in the scheme.
MANAGED_EVIDENCE_PURPOSES = (
    PURPOSE_ACTIVATION, PURPOSE_CPE_SWAP, PURPOSE_SUSPENSION, PURPOSE_REACTIVATION, PURPOSE_BOOST,
)


def backfill_value(db, service_id) -> bool:
    """True when the service has a successful non-dry evidence run with no
    later successful non-dry DEPROVISION. A reverted run is ROLLED_BACK, so it
    never counts."""
    r, d = aliased(ProvisioningRun), aliased(ProvisioningRun)
    ok = ProvisioningJobStatus.SUCCEEDED
    later_deprovision = exists().where(
        d.client_service_id == service_id, not_(d.dry_run), d.status == ok,
        d.purpose == PURPOSE_DEPROVISION, d.created_at > r.created_at,
    )
    evidence = exists().where(and_(
        r.client_service_id == service_id, not_(r.dry_run), r.status == ok,
        r.purpose.in_(MANAGED_EVIDENCE_PURPOSES), not_(later_deprovision),
    ))
    return bool(db.scalar(select(evidence)))
