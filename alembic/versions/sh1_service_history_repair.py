"""Data repair: reconstruct client-service history for plan changes the ISP adoption missed

Revision ID: sh1_service_history_repair
Revises: cr1_cash_review
Create Date: 2026-10-04

Problem (investigated 2026-10-03, reviewed by the product owner in
service-history-fix-plan.xlsx): 184 RECURRING orders created Jul–Dec 2025, before
the platform's audit log starts, carry a line item that names the client's
CURRENT plan and price, while `order.total_cents` and the payments hold what was
really charged. The adoption import attached every client to a single service
(their plan at go-live), so clients who changed plans before go-live have their
older months hanging off the newer service; `c1b_backfill` then priced those
lines from the current product. Totals and payments were always right.

Repair, per client service whose RECURRING history shows the price switching
from older value(s) to the current plan's price exactly once and never back
("clean switch"):

  1. For every run of consecutive months at an older price, INSERT a historical
     client_service on the company's single monthly SERVICE plan with that price
     (installation-kind plans excluded), status/billing_status CANCELLED,
     activation_date/created_at = the run's first order (the original service's
     activation for the first run), cancelled_at = the next run's first order,
     recurrence_end = the run's LAST order (so `detect_missing_periods` expects
     exactly the periods it billed, not the one the next service billed),
     migration_source 'sh1'.
  2. Re-point that run's orders to it and set their single line item's plan,
     price (= order total / quantity) and name.
  3. Move the current service's activation_date AND created_at to its first
     order at the current price — `detect_missing_periods` anchors on
     created_at, so leaving it would make the moved months look unbilled.

Services that are not a clean switch (a one-off odd month, a first-month
discount, or an old price that matches no unique plan) keep their orders; only
the mismatched line's price is set to the amount charged.

Tidy (approved for all matches): a service left status ACTIVE with
billing_status INACTIVE that a later non-cancelled service of the same client
replaced, and whose last order precedes that service's start, is cancelled at
the replacement's start — matching client_service_lifecycle's CANCELLED side
effects (cancelled_at, billing CANCELLED, next_generation_date NULL) plus
recurrence_end = its last billed order (gap detection must not expect the
months the replacement billed). Overlapping pairs (possibly two real services) are skipped.

Only order_item and client_service rows change; order totals and payments are
never touched. Idempotent: a second run finds no mismatched line and no
ACTIVE/INACTIVE replaced service. Downgrade is not supported (data repair).
"""
from collections import defaultdict
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "sh1_service_history_repair"
down_revision: Union[str, Sequence[str], None] = "cr1_cash_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MIGRATION_SOURCE = "sh1"


def _plan_for_price(plans_by_price, company_id, price_cents):
    cands = plans_by_price.get((company_id, price_cents), [])
    if len(cands) == 1:
        return cands[0]
    active = [p for p in cands if p["is_active"]]
    return active[0] if len(active) == 1 else None


def _tidy_replaced_services(conn) -> tuple:
    rows = conn.execute(text("""
        SELECT cs.id,
               (SELECT max(o.created_at) FROM "order" o
                 WHERE o.client_service_id = cs.id AND o.order_type = 'RECURRING'
                   AND o.status <> 'CANCELLED') AS last_order,
               COALESCE(
                 (SELECT min(o.created_at) FROM "order" o
                   WHERE o.client_service_id = nx.id AND o.order_type = 'RECURRING'
                     AND o.status <> 'CANCELLED'),
                 nx.activation_date, nx.created_at) AS switch_at
        FROM client_service cs
        JOIN LATERAL (
            SELECT n.* FROM client_service n
            WHERE n.client_id = cs.client_id AND n.id <> cs.id
              AND n.created_at > cs.created_at AND n.status <> 'CANCELLED'
            ORDER BY n.created_at LIMIT 1) nx ON true
        WHERE cs.status = 'ACTIVE' AND cs.billing_status = 'INACTIVE'
    """)).mappings().all()
    cancelled = skipped = 0
    for r in rows:
        if r["last_order"] is not None and r["last_order"] >= r["switch_at"]:
            skipped += 1
            continue
        conn.execute(text("""
            UPDATE client_service SET status = 'CANCELLED', billing_status = 'CANCELLED',
                   cancelled_at = :at, recurrence_end = COALESCE(:last, :at),
                   next_generation_date = NULL, updated_at = NOW()
            WHERE id = :id
        """), {"id": r["id"], "at": r["switch_at"], "last": r["last_order"]})
        cancelled += 1
    return cancelled, skipped


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET lock_timeout = '5s'"))

    # Tidy first: it compares created_at between a client's services, and the
    # repair below moves current services' created_at.
    tidied, tidy_skipped = _tidy_replaced_services(conn)

    plans_by_price = defaultdict(list)
    plan_by_id = {}
    for p in conn.execute(text(
        "SELECT id, company_id, name, price_cents, kind::text AS kind, is_active FROM service_plan"
    )).mappings():
        plan_by_id[p["id"]] = p
        if p["kind"] == "SERVICE":
            plans_by_price[(p["company_id"], p["price_cents"])].append(p)

    affected = [r[0] for r in conn.execute(text("""
        SELECT DISTINCT o.client_service_id FROM "order" o
        WHERE o.order_type = 'RECURRING' AND o.status <> 'CANCELLED' AND o.client_service_id IS NOT NULL
          AND o.total_cents IS DISTINCT FROM
              (SELECT COALESCE(sum(oi.unit_price_cents * oi.quantity), 0) FROM order_item oi WHERE oi.order_id = o.id)
    """))]

    created = moved = line_fixed = shifted = 0
    for sid in affected:
        svc = conn.execute(text(
            "SELECT * FROM client_service WHERE id = :id"), {"id": sid}).mappings().one()
        cur_plan = plan_by_id.get(svc["service_plan_id"])
        if cur_plan is None:
            continue
        cur_price = cur_plan["price_cents"]
        orders = conn.execute(text("""
            SELECT o.id, o.created_at, o.total_cents,
                   (SELECT count(*) FROM order_item oi WHERE oi.order_id = o.id) AS n_items,
                   (SELECT oi.id FROM order_item oi WHERE oi.order_id = o.id LIMIT 1) AS item_id,
                   (SELECT oi.quantity FROM order_item oi WHERE oi.order_id = o.id LIMIT 1) AS quantity,
                   (SELECT oi.unit_price_cents FROM order_item oi WHERE oi.order_id = o.id LIMIT 1) AS unit_price_cents
            FROM "order" o
            WHERE o.client_service_id = :sid AND o.order_type = 'RECURRING' AND o.status <> 'CANCELLED'
            ORDER BY o.created_at
        """), {"sid": sid}).mappings().all()
        multi_line = any(o["n_items"] != 1 for o in orders)

        first_cur = next((i for i, o in enumerate(orders) if o["total_cents"] == cur_price), None)
        clean = (not multi_line and first_cur is not None and first_cur > 0
                 and all(o["total_cents"] == cur_price for o in orders[first_cur:]))
        runs = []
        if clean:
            for o in orders[:first_cur]:
                if runs and runs[-1]["price"] == o["total_cents"]:
                    runs[-1]["orders"].append(o)
                else:
                    runs.append({"price": o["total_cents"], "orders": [o]})
            for run in runs:
                run["plan"] = _plan_for_price(plans_by_price, svc["company_id"], run["price"])
            clean = all(run["plan"] is not None for run in runs)

        if not clean:
            for o in orders:
                if o["n_items"] != 1:
                    continue
                qty = max(o["quantity"] or 1, 1)
                if o["total_cents"] != o["unit_price_cents"] * qty:
                    conn.execute(text("UPDATE order_item SET unit_price_cents = :p WHERE id = :id"),
                                 {"p": o["total_cents"] // qty, "id": o["item_id"]})
                    line_fixed += 1
            continue

        boundaries = [run["orders"][0]["created_at"] for run in runs] + [orders[first_cur]["created_at"]]
        for k, run in enumerate(runs):
            start = boundaries[k]
            if k == 0 and svc["activation_date"] is not None and svc["activation_date"] <= start:
                start = svc["activation_date"]
            end = boundaries[k + 1]
            hp = run["plan"]
            hid = conn.execute(text("""
                INSERT INTO client_service (
                    id, created_at, updated_at, status, activation_date, cancelled_at, notes,
                    company_id, client_id, service_plan_id, recurrence, recurrence_end,
                    next_generation_date, last_generated_at, billing_status, quantity,
                    migration_source, install_state, installed_at)
                VALUES (
                    gen_random_uuid(), :start, NOW(), 'CANCELLED', :start, :end, :notes,
                    :company_id, :client_id, :plan_id, :recurrence, :last_gen,
                    NULL, :last_gen, 'CANCELLED', :quantity,
                    :src, :install_state, :installed_at)
                RETURNING id
            """), {
                "start": start, "end": end,
                "notes": f"Historical service reconstructed by {revision}: "
                         f"client was on {hp['name']} until {end.date()} (plan change before go-live).",
                "company_id": svc["company_id"], "client_id": svc["client_id"], "plan_id": hp["id"],
                "recurrence": svc["recurrence"], "last_gen": run["orders"][-1]["created_at"],
                "quantity": svc["quantity"], "src": MIGRATION_SOURCE,
                "install_state": svc["install_state"],
                "installed_at": start if svc["install_state"] == "INSTALLED" else None,
            }).scalar_one()
            created += 1
            for o in run["orders"]:
                qty = max(o["quantity"] or 1, 1)
                conn.execute(text('UPDATE "order" SET client_service_id = :h WHERE id = :id'),
                             {"h": hid, "id": o["id"]})
                conn.execute(text("""
                    UPDATE order_item SET service_plan_id = :plan, unit_price_cents = :p, product_name = :name
                    WHERE id = :id
                """), {"plan": hp["id"], "p": o["total_cents"] // qty, "name": hp["name"], "id": o["item_id"]})
                moved += 1
                line_fixed += 1

        new_start = orders[first_cur]["created_at"]
        conn.execute(text("""
            UPDATE client_service SET activation_date = :d, created_at = :d, updated_at = NOW() WHERE id = :id
        """), {"d": new_start, "id": sid})
        shifted += 1

    remaining = conn.execute(text("""
        SELECT count(*) FROM "order" o
        WHERE o.order_type = 'RECURRING' AND o.status <> 'CANCELLED'
          AND (SELECT count(*) FROM order_item oi WHERE oi.order_id = o.id) = 1
          AND o.total_cents IS DISTINCT FROM
              (SELECT COALESCE(sum(oi.unit_price_cents * oi.quantity), 0) FROM order_item oi WHERE oi.order_id = o.id)
    """)).scalar()

    print(f"[sh1] tidy: cancelled {tidied} replaced service(s), skipped {tidy_skipped} overlapping")
    print(f"[sh1] created {created} historical service(s), moved {moved} order(s), "
          f"fixed {line_fixed} line item(s), shifted start of {shifted} current service(s)")
    if remaining:
        raise RuntimeError(f"[sh1] {remaining} single-line RECURRING order(s) still mismatch their line item")


def downgrade() -> None:
    # Data repair: the pre-repair state (wrong line prices, services attached
    # to the wrong plan period) is not worth restoring and cannot be derived.
    raise NotImplementedError("sh1_service_history_repair is a one-way data repair")
