"""Daily partitions of traffic_5m (sm1).

traffic_5m is RANGE-partitioned by bucket_start, one partition per Guatemala
day named ``traffic_5m_YYYYMMDD``. There is no DEFAULT partition on purpose: a
row landing in one would block creating that day's partition later. So the
writer (backend-erp's ingest) calls ``ensure_traffic_5m_partitions`` for the
bucket's day before inserting, and the daily job keeps a few days ahead.

The helpers are no-ops on anything but PostgreSQL, and ``ensure`` also on a
plain (``create_all``) traffic_5m, so callers need no checks and the test
suites keep working. They never commit. CREATE/DROP take ACCESS EXCLUSIVE on
traffic_5m, so each DDL runs under a 5 s lock_timeout (restored afterwards):
a long dashboard read makes the DDL fail fast instead of queueing every ingest
behind it.
"""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import text

from .timezone_utils import GUATEMALA_TZ

PARENT = "traffic_5m"
_PREFIX = PARENT + "_"
DDL_LOCK_TIMEOUT = "5s"


def guatemala_day(value: date | datetime) -> date:
    """The Guatemala day a value falls on. A ``date`` is taken as is; an aware
    ``datetime`` is converted to America/Guatemala first, and a naive one is
    read as UTC (the database and the API speak UTC). So a 23:00 UTC bucket
    lands on that day's 17:00 local, and 02:00 UTC on the previous local day."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(GUATEMALA_TZ).date()
    return value


@contextmanager
def _ddl_lock_timeout(bind):
    """SET LOCAL lock_timeout for the DDL, then put the previous value back
    (SET LOCAL would otherwise last until the caller's commit)."""
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'"))
    try:
        yield
    finally:
        bind.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": previous})


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


def _exists(bind, name: str) -> bool:
    return bind.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar()


def ensure_traffic_5m_partitions(bind, start: date | datetime, days: int = 1) -> list[str]:
    """Create the partitions for ``start``'s Guatemala day and the following
    ``days - 1`` days if missing. ``start`` may be a date or a datetime (a
    bucket_start in UTC is fine, see ``guatemala_day``). Returns the names it
    ensured (empty off PostgreSQL). Only a missing partition takes the lock."""
    if not _is_pg(bind) or days < 1 or not _is_partitioned(bind):
        return []
    first = guatemala_day(start)
    names = []
    for i in range(days):
        d = first + timedelta(days=i)
        name = partition_name(d)
        if not _exists(bind, name):
            with _ddl_lock_timeout(bind):
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


def drop_traffic_5m_partitions_before(bind, day: date | datetime) -> list[str]:
    """DROP every partition older than ``day`` (the archive must already have
    been written). Dropping a whole partition is instant and leaves no bloat,
    unlike a bulk DELETE."""
    day = guatemala_day(day)
    dropped = []
    for d in list_traffic_5m_partitions(bind):
        if d < day:
            name = partition_name(d)
            with _ddl_lock_timeout(bind):
                bind.execute(text(f"DROP TABLE IF EXISTS {name}"))
            dropped.append(name)
    return dropped
