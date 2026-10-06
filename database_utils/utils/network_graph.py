"""Traversal of the per-company network graph (doc 35 §3).

The plant is a strict tree of `inventory_item` rows joined by `parent_id`. This
module is the only place that walks it.

Two invariants are load-bearing and appear in every query here:

1. `company_id` is filtered in BOTH the anchor and the recursive term. A tree
   that spans tenants cannot be produced through the API — the DB trigger
   `trg_inventory_item_graph_guard` rejects a cross-company parent — but a
   traversal that only filtered the anchor would still happily follow such an
   edge if one ever appeared through a direct DB write. Filtering both terms
   makes cross-tenant traversal impossible rather than merely unlikely.

2. Every recursion is bounded by MAX_PATH_DEPTH. The trigger already rejects
   cycles, so the bound is a backstop rather than the primary guard — but an
   unbounded recursive CTE over a cycle does not error, it hangs, and that is
   the worst possible failure mode for a query on the provisioning hot path.

Written against SQLAlchemy Core's recursive-CTE API rather than raw `text()` so
the same code runs on Postgres and on the in-memory SQLite database the unit
tests build with `create_all`.
"""

from __future__ import annotations

import uuid
from typing import List

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from database_utils.models.isp import InventoryItem, InventoryItemPort, NetworkLink

# Kept in sync with the same constant inside trg_inventory_item_graph_guard
# (revision ng1_network_graph). If these ever disagree, the trigger wins and the
# traversal starts raising PATH_TOO_DEEP on paths the DB happily accepted.
MAX_PATH_DEPTH = 32


class GraphError(Exception):
    """A traversal could not be completed. `code` is stable and API-facing."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _ancestor_cte(item_id: uuid.UUID, company_id: uuid.UUID):
    """`item_id` at depth 0, then each ancestor, one level per step.

    The anchor requires `network_attached`. A detached item is warehouse stock,
    not a place in the plant, and returning a one-element "path" for it made two
    callers disagree: the resolver refused with CPE_NOT_ATTACHED while the path
    panel happily rendered a single-node path. One of them had to be wrong; the
    resolver was right.
    """
    ii = InventoryItem.__table__
    anchor = (
        sa.select(ii.c.id, ii.c.parent_id, sa.literal(0).label("depth"))
        .where(
            ii.c.id == item_id,
            ii.c.company_id == company_id,
            ii.c.network_attached.is_(True),
        )
        .cte("graph_ancestors", recursive=True)
    )
    step = (
        sa.select(ii.c.id, ii.c.parent_id, (anchor.c.depth + 1).label("depth"))
        .join(anchor, ii.c.id == anchor.c.parent_id)
        .where(ii.c.company_id == company_id, anchor.c.depth < MAX_PATH_DEPTH)
    )
    return anchor.union_all(step)


def _descendant_cte(item_id: uuid.UUID, company_id: uuid.UUID):
    """`item_id` at depth 0, then everything behind it."""
    ii = InventoryItem.__table__
    anchor = (
        sa.select(ii.c.id, sa.literal(0).label("depth"))
        .where(ii.c.id == item_id, ii.c.company_id == company_id)
        .cte("graph_descendants", recursive=True)
    )
    step = (
        sa.select(ii.c.id, (anchor.c.depth + 1).label("depth"))
        .join(anchor, ii.c.parent_id == anchor.c.id)
        .where(ii.c.company_id == company_id, anchor.c.depth < MAX_PATH_DEPTH)
    )
    return anchor.union_all(step)


def _ordered_items(db: Session, ids) -> List[InventoryItem]:
    """Re-hydrate ORM objects preserving the order the CTE produced.

    The CTE returns ids; a plain `IN (...)` query returns them in whatever order
    the planner likes. Path order is load-bearing (doc 35 §3.1), so it is
    re-imposed here rather than trusted.
    """
    ids = list(ids)
    if not ids:
        return []
    by_id = {
        item.id: item
        for item in db.execute(
            sa.select(InventoryItem).where(InventoryItem.id.in_(ids))
        ).scalars()
    }
    return [by_id[i] for i in ids if i in by_id]


def resolve_path(
    db: Session, item_id: uuid.UUID, company_id: uuid.UUID
) -> List[InventoryItem]:
    """The configuration path for a node: itself, then every ancestor.

    Ordered LEAF -> ROOT (doc 35 §3.1) — the subscriber's own device first, the
    core last. Devices are configured from the subscriber outward.

    Returns [] for an unknown item, or for one belonging to another company.
    Raises GraphError("PATH_TOO_DEEP") past MAX_PATH_DEPTH.
    """
    cte = _ancestor_cte(item_id, company_id)
    rows = db.execute(sa.select(cte.c.id, cte.c.depth).order_by(cte.c.depth)).all()
    if len(rows) > MAX_PATH_DEPTH:
        raise GraphError(
            "PATH_TOO_DEEP",
            f"the path from {item_id} exceeds the maximum depth of {MAX_PATH_DEPTH}",
        )
    return _ordered_items(db, (r.id for r in rows))


def descendants(
    db: Session, item_id: uuid.UUID, company_id: uuid.UUID
) -> List[InventoryItem]:
    """Everything behind a node, excluding the node itself.

    Impact analysis: "who is affected if I re-parent or take down this OLT?"
    Ordered by depth, nearest first.
    """
    cte = _descendant_cte(item_id, company_id)
    rows = db.execute(
        sa.select(cte.c.id, cte.c.depth).where(cte.c.depth > 0).order_by(cte.c.depth)
    ).all()
    return _ordered_items(db, (r.id for r in rows))


def would_create_cycle(
    db: Session,
    item_id: uuid.UUID,
    new_parent_id: uuid.UUID,
    company_id: uuid.UUID,
) -> bool:
    """True if parenting `item_id` under `new_parent_id` closes a loop.

    Mirrors the DB trigger so the API can answer 422 with a readable message
    instead of surfacing a raised Postgres exception. The trigger remains the
    guarantee; this is the courtesy. Both must stay in agreement — a check here
    that the trigger does not make would let a race through.
    """
    if item_id == new_parent_id:
        return True
    cte = _ancestor_cte(new_parent_id, company_id)
    hit = db.execute(sa.select(cte.c.id).where(cte.c.id == item_id).limit(1)).first()
    return hit is not None


def child_count(db: Session, item_id: uuid.UUID, company_id: uuid.UUID) -> int:
    """Immediate children only. Used by the detach guard and the tree UI."""
    return (
        db.execute(
            sa.select(sa.func.count())
            .select_from(InventoryItem.__table__)
            .where(
                InventoryItem.parent_id == item_id,
                InventoryItem.company_id == company_id,
            )
        ).scalar_one()
    )


def assert_links_consistent(db: Session) -> None:
    """Assert every network_link agrees with the tree it describes.

    On Postgres the deferred triggers of revision pt1_port_topology enforce
    `inventory_item[down].parent_id = up_item_id` at COMMIT, and the composite
    FKs pin each port to its item and tenant. SQLite `create_all` schemas have
    neither (triggers never live in metadata; SQLite does not enforce FKs by
    default), so graph tests in this repo and in backend-erp call this after
    every write instead. Raises AssertionError naming each bad link.
    """
    up = aliased(InventoryItemPort)
    down = aliased(InventoryItemPort)
    rows = db.execute(
        sa.select(NetworkLink, InventoryItem.parent_id, InventoryItem.company_id,
                  up.item_id, up.company_id, down.item_id, down.company_id)
        .outerjoin(InventoryItem, InventoryItem.id == NetworkLink.down_item_id)
        .outerjoin(up, up.id == NetworkLink.up_port_id)
        .outerjoin(down, down.id == NetworkLink.down_port_id)
    ).all()
    problems = []
    for link, parent_id, item_co, up_item, up_co, down_item, down_co in rows:
        where = f"link {link.id} ({link.down_item_id} -> {link.up_item_id})"
        if parent_id != link.up_item_id:
            problems.append(f"{where}: parent_id is {parent_id} (NETWORK_LINK_PARENT_MISMATCH)")
        if item_co != link.company_id:
            problems.append(f"{where}: down item belongs to company {item_co}")
        if (up_item, up_co) != (link.up_item_id, link.company_id):
            problems.append(f"{where}: up port belongs to item {up_item} / company {up_co}")
        if link.down_port_id is not None and (down_item, down_co) != (
                link.down_item_id, link.company_id):
            problems.append(f"{where}: down port belongs to item {down_item} / company {down_co}")
    assert not problems, "inconsistent network links:\n" + "\n".join(problems)
