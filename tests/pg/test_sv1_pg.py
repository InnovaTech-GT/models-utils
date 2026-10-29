"""sv1_service_entity backfill on a real Postgres: downgrade to pa2, seed a
realistic catalog (two groups, mixed automation, categories union, a NULL
group, an INSTALLATION plan), upgrade to sv1, assert, then leave the DB at head.
Needs a throwaway DB (name contains "test") already at head: it migrates it."""
import os
import subprocess
import uuid

import pytest
import sqlalchemy as sa

URL = os.getenv("PG_TEST_URL", "")
pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not URL or "test" not in URL.rsplit("/", 1)[-1], reason="PG_TEST_URL (…test…) not set"),
]


def _alembic(*args):
    subprocess.run(["alembic", *args], check=True, env={**os.environ, "DB_URL": URL},
                   cwd=os.path.join(os.path.dirname(__file__), "..", ".."))


def test_backfill_two_groups_mixed_automation_categories_union():
    _alembic("downgrade", "pa2_micronode_category")
    e = sa.create_engine(URL)
    co, onu, mic = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids = {k: uuid.uuid4() for k in "abcdef"}
    try:
        with e.begin() as c:
            c.execute(sa.text("INSERT INTO company (id, created_at, name, tier_id) SELECT :i, now(), :n, id FROM tier LIMIT 1"),
                      {"i": co, "n": f"sv1-{co}"})
            for i, k in ((onu, "SV1ONU"), (mic, "SV1MIC")):
                c.execute(sa.text("INSERT INTO device_category (id, created_at, updated_at, key, name) VALUES (:i, now(), now(), :k, :k)"),
                          {"i": i, "k": f"{k}-{i.hex[:6]}"})
            # (key, group, type, kind, automation, categories)
            rows = [("a", "Internet GPON", "FIBER", "SERVICE", True, [onu]),
                    ("b", "Internet GPON", "FIBER", "SERVICE", False, [mic]),
                    ("c", "Internet GPON", "WIRELESS", "SERVICE", False, []),
                    ("d", "Cable", "CABLE", "SERVICE", False, [onu]),
                    ("e", None, "DSL", "SERVICE", False, []),
                    ("f", None, "FIBER", "INSTALLATION", True, [])]
            for k, g, t, kind, auto, cats in rows:
                c.execute(sa.text("INSERT INTO service_plan (id, created_at, updated_at, name, plan_type, price, is_active, "
                                  "company_id, service_group, kind, network_automation) VALUES "
                                  "(:i, now(), now(), :k, :t, 1, true, :co, :g, :kind, :a)"),
                          {"i": ids[k], "k": k, "t": t, "co": co, "g": g, "kind": kind, "a": auto})
                for cat in cats:
                    c.execute(sa.text("INSERT INTO service_plan_device_category VALUES (:p, :c)"), {"p": ids[k], "c": cat})
        _alembic("upgrade", "head")
        with e.begin() as c:
            svc = {r.name: r for r in c.execute(sa.text(
                "SELECT s.id, s.name, s.service_type::text AS stype, s.network_automation AS auto FROM service s WHERE company_id = :co"),
                {"co": co})}
            assert set(svc) == {"Internet GPON", "Cable", "DSL"}
            g = svc["Internet GPON"]
            assert (g.stype, g.auto) == ("FIBER", True)  # mode + bool_or
            assert (svc["Cable"].stype, svc["Cable"].auto) == ("CABLE", False)
            cats = set(c.execute(sa.text("SELECT device_category_id FROM service_device_category WHERE service_id = :s"),
                                 {"s": g.id}).scalars())
            assert cats == {onu, mic}  # union
            plans = {r.name: r for r in c.execute(sa.text(
                "SELECT name, service_id, service_group, plan_type::text AS ptype FROM service_plan WHERE company_id = :co"), {"co": co})}
            assert {plans[k].service_id for k in "abc"} == {g.id}
            assert plans["c"].ptype == "WIRELESS"  # plan_type untouched
            assert plans["d"].service_group == "Cable" and plans["e"].service_id == svc["DSL"].id
            assert plans["f"].service_id is None  # INSTALLATION: no service
            # idempotent re-run of the data steps is covered by IF NOT EXISTS / service_id IS NULL
    finally:
        with e.begin() as c:
            c.execute(sa.text("DELETE FROM service_plan WHERE company_id = :co"), {"co": co})
            c.execute(sa.text("DELETE FROM service WHERE company_id = :co"), {"co": co})
            c.execute(sa.text("DELETE FROM company WHERE id = :co"), {"co": co})
            c.execute(sa.text("DELETE FROM device_category WHERE id IN (:a, :b)"), {"a": onu, "b": mic})
        e.dispose()
        _alembic("upgrade", "head")
