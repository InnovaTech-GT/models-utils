"""dm1_device_metrics: tenant-defined SNMP metrics per device (doc 75).

A ``DeviceMetric`` names one OID (a scalar or a table column) that the
snmp-poller reads every minute on every device it applies to: every device
of ``device_type_id``, or the single ``inventory_item_id`` (exactly one is
set). ``GAUGE`` values are stored as read, ``COUNTER`` values as a
per-second rate; ``scale`` is applied last. ``label_oid`` names each table
row (ifName, a MikroTik gauge name, hrStorageDescr); without it the row's
OID suffix is its name. ``instances`` keeps only those rows (empty = all).
``subtract_from_oid``: the stored value is ``that - value`` ("free = size -
used").

Values are bucketed like traffic (sm1): 5-minute rows from the poller, then
hourly and daily rollups built by the daily job. ``instance`` is the row
name, ``''`` for a scalar; an OLT table keyed by ONU would store one series
per ONU here without a schema change.

No partitions (unlike traffic_5m): the volume is a few thousand rows per
tenant and day. The CHECK constraints live only in the revision (house
style).
"""
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
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

DEVICE_METRIC_KINDS = ("GAUGE", "COUNTER")
DEVICE_METRIC_LIMIT_PER_COMPANY = 50
DEVICE_METRIC_MAX_INSTANCES = 64


class DeviceMetric(Base):
    __tablename__ = "device_metric"
    __table_args__ = (
        Index("uq_device_metric_company_name", "company_id", "name", unique=True),
        Index("ix_device_metric_device_type", "device_type_id"),
        Index("ix_device_metric_item", "inventory_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(80), nullable=False)
    unit = Column(String(16), nullable=True)
    oid = Column(String(200), nullable=False)
    kind = Column(String(8), nullable=False, default="GAUGE")
    label_oid = Column(String(200), nullable=True)
    instances = Column(JSON, nullable=False, default=list)
    subtract_from_oid = Column(String(200), nullable=True)
    scale = Column(Float, nullable=False, default=1.0)
    device_type_id = mapped_column(
        Uuid, ForeignKey("device_type.id", ondelete="CASCADE"), nullable=True
    )
    inventory_item_id = mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="CASCADE"), nullable=True
    )
    preset = Column(String(32), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    # {item_id: {"at", "instances", "sample": [{"instance", "value"}], "error"}},
    # written by backend-erp on every ingest (the settings card's preview).
    last_poll = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now_gt)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now_gt, onupdate=now_gt)


def _metric_fk():
    return mapped_column(
        Uuid, ForeignKey("device_metric.id", ondelete="CASCADE"), primary_key=True
    )


def _item_fk():
    return mapped_column(
        Uuid, ForeignKey("inventory_item.id", ondelete="CASCADE"), primary_key=True
    )


def _company_fk():
    return mapped_column(
        Uuid, ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )


class DeviceMetric5m(Base):
    """One row per (metric, device, instance) and 5-minute bucket."""
    __tablename__ = "device_metric_5m"
    __table_args__ = (
        Index("ix_device_metric_5m_company_bucket", "company_id", "bucket_start"),
    )

    metric_id: Mapped[uuid.UUID] = _metric_fk()
    inventory_item_id: Mapped[uuid.UUID] = _item_fk()
    instance = Column(String(128), primary_key=True, default="")
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = _company_fk()
    value_avg = Column(Float, nullable=False)
    value_min = Column(Float, nullable=False)
    value_max = Column(Float, nullable=False)
    samples = Column(SmallInteger, nullable=False)


class DeviceMetric1h(Base):
    """Hourly rollup of device_metric_5m (daily job)."""
    __tablename__ = "device_metric_1h"
    __table_args__ = (
        Index("ix_device_metric_1h_company_bucket", "company_id", "bucket_start"),
    )

    metric_id: Mapped[uuid.UUID] = _metric_fk()
    inventory_item_id: Mapped[uuid.UUID] = _item_fk()
    instance = Column(String(128), primary_key=True, default="")
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = _company_fk()
    value_avg = Column(Float, nullable=False)
    value_min = Column(Float, nullable=False)
    value_max = Column(Float, nullable=False)
    samples = Column(SmallInteger, nullable=False)


class DeviceMetric1d(Base):
    """Daily rollup (Guatemala day)."""
    __tablename__ = "device_metric_1d"
    __table_args__ = (
        Index("ix_device_metric_1d_company_day", "company_id", "day"),
    )

    metric_id: Mapped[uuid.UUID] = _metric_fk()
    inventory_item_id: Mapped[uuid.UUID] = _item_fk()
    instance = Column(String(128), primary_key=True, default="")
    day = Column(Date, primary_key=True)
    company_id: Mapped[uuid.UUID] = _company_fk()
    value_avg = Column(Float, nullable=False)
    value_min = Column(Float, nullable=False)
    value_max = Column(Float, nullable=False)
    samples = Column(Integer, nullable=False)
