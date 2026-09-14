"""
Order type derivation (Cycle 2 D2, doc 18-cycle2-design.md amendment 9).

`Order.order_type` stays a stored column (D2) but becomes DERIVED, never
user-supplied — `OrderUpdate` already excludes it via `extra='forbid'`
(schemas/order.py). One function is the single source of truth so
backend-erp's `orders.py:create_order`, `workflow_engine.py`'s CREATE_ORDER
action, and the client_service billing generation service can never drift
from each other on the derivation rule.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def derive_order_type(
    item_kinds: Iterable[str],
    *,
    generated_from_billing: bool = False,
    explicit_order_type: Optional[str] = None,
):
    """Derive the OrderType for a new order.

    Rule (amendment 9 — supersedes the original appendix text "derive
    instead of defaulting", which dropped the caller's explicit config signal
    entirely and would have broken installed v2 `new-installation` templates
    whose fee product/plan doesn't reliably infer to INSTALLATION):

    1. `generated_from_billing=True` (the recurring/client_service billing
       generation path) -> RECURRING, always — never overridden by item
       kinds or explicit config.
    2. Otherwise: INSTALLATION if ANY item kind == 'INSTALLATION', OR the
       caller passed `explicit_order_type == 'INSTALLATION'` — the engine
       keeps honoring the step's configured order_type as an input so
       `uq_order_installation_per_service` and the INSTALLATION dedupe
       precheck keep protecting installed workflows regardless of how the
       configured fee catalog item classifies.
    3. Otherwise: ONE_SHOT.

    A disagreement between the config and the kind-derivation is logged
    (never raised) — the explicit config signal always wins per amendment 9.

    Returns the `database_utils.models.crm.OrderType` enum member. Imports
    are lazy so this module has zero import-time coupling to the ORM layer.
    """
    from database_utils.models.crm import OrderType
    from database_utils.models.isp import CatalogKind

    if generated_from_billing:
        return OrderType.RECURRING

    kinds = {str(k) for k in item_kinds if k}
    kind_says_installation = CatalogKind.INSTALLATION.value in kinds
    config_says_installation = (
        (explicit_order_type or "").upper() == OrderType.INSTALLATION.value
    )

    # Only a genuine disagreement is loggable: an explicit config value was
    # actually given (not just absent/None) AND it disagrees with what the
    # resolved item kinds imply. Absent config is the normal single-signal
    # case (most callers, e.g. backend-erp's plain create_order, never pass
    # explicit_order_type at all) and must never be logged as a conflict.
    if explicit_order_type and kind_says_installation != config_says_installation:
        logger.warning(
            "derive_order_type: kind-derivation (INSTALLATION=%s) and "
            "explicit config order_type (%r) disagree — explicit config wins",
            kind_says_installation, explicit_order_type,
        )

    if kind_says_installation or config_says_installation:
        return OrderType.INSTALLATION

    return OrderType.ONE_SHOT
