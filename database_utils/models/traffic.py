"""sm1_snmp_traffic_metrics: per-subscriber traffic measured by SNMP.

The snmp-poller service reads the dynamic simple queue RouterOS creates for
every PPPoE session (MIKROTIK-MIB mtxrQueueSimpleTable, queue name
``<pppoe-USER>``), turns the byte counters into bit rates and sends one
5-minute summary per service. ``client_service.pppoe_username`` is what joins a
queue to its service.

Lab-validated facts (2026-11-07, RouterOS CHR 7.16): ``mtxrQueueSimpleBytesIn``
is the subscriber's DOWNLOAD and ``mtxrQueueSimpleBytesOut`` the UPLOAD; on a
reconnect the queue keeps its name but gets a new index and its counters
restart from zero. Nothing here stores a raw counter, only rates.

Rates are bits per second. ``down_*`` is subscriber download, ``up_*`` upload.

``traffic_5m`` is RANGE-partitioned by ``bucket_start`` (one partition per
Guatemala day) in the sm1 revision only — SQLite ``create_all`` (every
consumer's test suite) builds it as a plain table. Partitions are created and
dropped through ``utils.traffic_partitions``. The CHECK constraints also live
only in the revision (house style).
"""
import uuid

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from database_utils.database import Base

from ..utils.timezone_utils import now_gt

TRAFFIC_BUCKET_SECONDS = 300
TRAFFIC_DIRECTIONS = ("DOWN", "UP")

# Defaults for company.traffic_settings (schemas.company.TrafficSettings). The
# meeting's example values; every tenant can change them.
TRAFFIC_SETTINGS_DEFAULTS = {
    "saturation_pct": 90,
    "saturation_minutes_per_day": 30,
    "retention_5m_days": 7,
    "retention_1h_days": 90,
    "retention_1d_days": 730,
}


def _service_fk():
    return mapped_column(
        Uuid, ForeignKey("client_service.id", ondelete="CASCADE"), primary_key=True
    )


def _company_fk():
    return mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )


class Traffic5m(Base):
    """One row per service and 5-minute bucket (bucket_start aligned to :00,
    :05, ...). ``samples`` = 1-minute rates that went into the bucket (a
    reconnect or a missed poll gives fewer than 5)."""
    __tablename__ = "traffic_5m"
    __table_args__ = (
        Index("ix_traffic_5m_company_bucket", "company_id", "bucket_start"),
    )

    client_service_id: Mapped[uuid.UUID] = _service_fk()
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = _company_fk()
    down_avg_bps = Column(BigInteger, nullable=False)
    down_max_bps = Column(BigInteger, nullable=False)
    up_avg_bps = Column(BigInteger, nullable=False)
    up_max_bps = Column(BigInteger, nullable=False)
    down_dropped = Column(BigInteger, nullable=False, default=0)
    up_dropped = Column(BigInteger, nullable=False, default=0)
    samples = Column(SmallInteger, nullable=False)


class Traffic1h(Base):
    """Hourly rollup of traffic_5m (built by the daily job)."""
    __tablename__ = "traffic_1h"
    __table_args__ = (
        Index("ix_traffic_1h_company_bucket", "company_id", "bucket_start"),
    )

    client_service_id: Mapped[uuid.UUID] = _service_fk()
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = _company_fk()
    down_avg_bps = Column(BigInteger, nullable=False)
    down_max_bps = Column(BigInteger, nullable=False)
    down_p95_bps = Column(BigInteger, nullable=False)
    up_avg_bps = Column(BigInteger, nullable=False)
    up_max_bps = Column(BigInteger, nullable=False)
    up_p95_bps = Column(BigInteger, nullable=False)
    down_dropped = Column(BigInteger, nullable=False, default=0)
    up_dropped = Column(BigInteger, nullable=False, default=0)
    samples = Column(SmallInteger, nullable=False)


class Traffic1d(Base):
    """Daily rollup (Guatemala day). ``down/up_saturated_minutes`` = minutes of
    5-minute buckets whose average was over the tenant's saturation_pct of the
    plan speed at rollup time."""
    __tablename__ = "traffic_1d"
    __table_args__ = (
        Index("ix_traffic_1d_company_day", "company_id", "day"),
    )

    client_service_id: Mapped[uuid.UUID] = _service_fk()
    day = Column(Date, primary_key=True)
    company_id: Mapped[uuid.UUID] = _company_fk()
    down_avg_bps = Column(BigInteger, nullable=False)
    down_max_bps = Column(BigInteger, nullable=False)
    down_p95_bps = Column(BigInteger, nullable=False)
    up_avg_bps = Column(BigInteger, nullable=False)
    up_max_bps = Column(BigInteger, nullable=False)
    up_p95_bps = Column(BigInteger, nullable=False)
    down_saturated_minutes = Column(Integer, nullable=False, default=0)
    up_saturated_minutes = Column(Integer, nullable=False, default=0)
    samples = Column(Integer, nullable=False)


class SaturationEvent(Base):
    """A day on which a service met the tenant's saturation rule in one
    direction. Plan speed and threshold are snapshotted: the plan or the rule
    can change later and the event must still read the same."""
    __tablename__ = "saturation_event"
    __table_args__ = (
        Index("uq_saturation_event", "client_service_id", "day", "direction", unique=True),
        Index("ix_saturation_event_company_day", "company_id", "day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    company_id: Mapped[uuid.UUID] = _company_fk()
    client_service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client_service.id", ondelete="CASCADE"), nullable=False
    )
    day = Column(Date, nullable=False)
    direction = Column(String(4), nullable=False)
    plan_mbps = Column(Integer, nullable=False)
    threshold_pct = Column(SmallInteger, nullable=False)
    saturated_minutes = Column(Integer, nullable=False)
    peak_pct = Column(SmallInteger, nullable=False)
