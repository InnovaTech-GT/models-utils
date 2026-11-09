"""Daily partitions of traffic_5m (sm1).

traffic_5m is RANGE-partitioned by bucket_start, one partition per Guatemala
day named ``traffic_5m_YYYYMMDD``. There is no DEFAULT partition on purpose: a
row landing in one would block creating that day's partition later. So the
writer (backend-erp's ingest) calls ``ensure_traffic_5m_partitions`` for the
bucket's day before inserting, and the daily job keeps a few days ahead.

The helpers are no-ops on anything but PostgreSQL, and ``ensure`` also on a
plain (``create_all``) traffic_5m, so callers need no checks and the test
suites keep working. They never commit.
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import text

from .timezone_utils import GUATEMALA_TZ

PARENT = "traffic_5m"
_PREFIX = PARENT + "_"


def partition_name(day: date) -> str:
    return f"{_PREFIX}{day:%Y%m%d}"


def _bound(day: date) -> str:
    return datetime.combine(day, time(0), GUATEMALA_TZ).isoformat()


def _is_pg(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _is_partitioned(bind) -> bool:
    """True when traffic_5m is the partitioned table of the sm1 revision. A
    schema built with ``create_all`` (the consumers' Postgres test lanes) has
    a plain table, and there is nothing to partition."""
    return bind.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relname = :t AND relkind = 'p')"
    ), {"t": PARENT}).scalar()


def ensure_traffic_5m_partitions(bind, start: date, days: int = 1) -> list[str]:
    """Create the partitions for ``start`` and the following ``days - 1`` days
    if missing. Returns the names it ensured (empty off PostgreSQL)."""
    if not _is_pg(bind) or days < 1 or not _is_partitioned(bind):
        return []
    names = []
    for i in range(days):
        d = start + timedelta(days=i)
        name = partition_name(d)
        bind.execute(text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {PARENT} "
            f"FOR VALUES FROM ('{_bound(d)}') TO ('{_bound(d + timedelta(days=1))}')"
        ))
        names.append(name)
    return names


def list_traffic_5m_partitions(bind) -> list[date]:
    """Days that currently have a partition, oldest first."""
    if not _is_pg(bind):
        return []
    rows = bind.execute(text(
        "SELECT c.relname FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "WHERE p.relname = :parent"
    ), {"parent": PARENT}).scalars()
    days = []
    for name in rows:
        suffix = name[len(_PREFIX):] if name.startswith(_PREFIX) else ""
        if len(suffix) == 8 and suffix.isdigit():
            days.append(date(int(suffix[:4]), int(suffix[4:6]), int(suffix[6:])))
    return sorted(days)


def drop_traffic_5m_partitions_before(bind, day: date) -> list[str]:
    """DROP every partition older than ``day`` (the archive must already have
    been written). Dropping a whole partition is instant and leaves no bloat,
    unlike a bulk DELETE."""
    dropped = []
    for d in list_traffic_5m_partitions(bind):
        if d < day:
            name = partition_name(d)
            bind.execute(text(f"DROP TABLE IF EXISTS {name}"))
            dropped.append(name)
    return dropped
