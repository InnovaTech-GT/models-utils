"""namespaced playbook variables + provisioning-params rows (doc 33)

Founder decision 2026-07-20: playbook template variables get a namespace and
the flat names are RETIRED, not aliased. `device1_serial` becomes
`edge_devices[0].serial`, the plan's parameters move under `service_plan.*`,
and author-declared variables move under `input.*`. Nothing bare survives, so
a mistyped namespace can no longer resolve silently to an unrelated value.

This rewrite is deliberately destructive — playbooks are not yet in production
(founder confirmed), so there is no back-compat alias layer to maintain. Two
data migrations:

1. `playbook.definition` — every {{token}} is rewritten through the name map
   below. Definitions are executable device configuration, so a rewritten
   playbook has its `last_dry_run_version` cleared: machine-edited config must
   be re-simulated before it is allowed near hardware again (canon C7 gate).
   `version` is deliberately NOT bumped — bumping it and clearing the stamp
   both re-gate the playbook, and bumping would make the idempotency check
   below (byte-identical on re-run) impossible to assert.

2. `service_plan.provisioning_params` — the flat {"vlan": 110} dict becomes
   [{"key": "vlan", "value": 110, "description": null}] so the UI can carry a
   human explanation per parameter. Insertion order is preserved.

Workflow action_config `variables` are also rewritten: their keys are author
input and now travel namespaced (`input.*`).

Idempotent: re-running finds no legacy tokens and leaves every row
byte-identical.

Revision ID: pv1_namespaced_variables
Revises: tk1_new_installation_v4
"""
from __future__ import annotations

import json
import re

from alembic import op
import sqlalchemy as sa

revision = 'pv1_namespaced_variables'
down_revision = 'tk1_new_installation_v4'
branch_labels = None
depends_on = None


# Legacy flat name -> new namespaced name. Positional device names are handled
# separately below because their tier index cannot be derived from the name.
_SCALAR_MAP = {
    'client_service_id': 'service.id',
    'service_plan_id': 'service_plan.id',
    'download_mbps': 'service_plan.download_mbps',
    'upload_mbps': 'service_plan.upload_mbps',
}

# device{i}_<attr> — i is the 1-based CHAIN position, which maps to the hidden
# absolute-position namespace. It cannot be turned into a tier index here: the
# tier depends on the topology's chain, which this rewrite does not resolve.
# `chain[n]` is exactly equivalent and keeps every existing template correct.
_DEVICE_RE = re.compile(r'device(\d+)_(item_id|serial|mac|type|category_tier)')

# Unique-category aliases (onu_serial, cpe_router_serial, olt_mac, ...). These
# are RETIRED with no mechanical replacement: the alias depended on a category
# appearing exactly once in the chain, which this migration cannot evaluate.
# They are rewritten to an obviously-broken marker so the post-migration check
# and the executor's unresolved-token guard both catch them loudly, rather
# than leaving a token that looks fine and resolves to nothing.
_ALIAS_RE = re.compile(r'([a-z][a-z0-9_]*)_(serial|mac)$')

_TOKEN_RE = re.compile(r'\{\{\s*([A-Za-z0-9_.\[\]]+)\s*\}\}')


def _map_name(name: str) -> str:
    """Legacy variable name -> namespaced name. Unknown//already-namespaced
    names pass through untouched."""
    if '.' in name or '[' in name:
        return name  # already namespaced
    if name in _SCALAR_MAP:
        return _SCALAR_MAP[name]
    m = _DEVICE_RE.fullmatch(name)
    if m:
        return f'chain[{int(m.group(1))}].{m.group(2)}'
    if _ALIAS_RE.fullmatch(name):
        # Retired alias — deliberately left unresolvable and greppable.
        return f'RETIRED_ALIAS.{name}'
    # Everything else is an author-declared variable.
    return f'input.{name}'


def _rewrite_tokens(blob: str) -> str:
    return _TOKEN_RE.sub(lambda m: '{{' + _map_name(m.group(1)) + '}}', blob)


def _rewrite_json(value):
    """Rewrite {{tokens}} inside any JSON structure, preserving shape."""
    if value is None:
        return None
    return json.loads(_rewrite_tokens(json.dumps(value)))


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Playbook definitions -------------------------------------------------
    rows = conn.execute(sa.text(
        "SELECT id, definition, last_dry_run_version FROM playbook WHERE definition IS NOT NULL"
    )).fetchall()
    for row in rows:
        definition = row[1]
        if isinstance(definition, str):
            definition = json.loads(definition)
        rewritten = _rewrite_json(definition)
        if rewritten == definition:
            continue  # idempotent: nothing to do, leave the row untouched
        conn.execute(
            sa.text(
                "UPDATE playbook SET definition = CAST(:d AS JSON), "
                "last_dry_run_version = NULL WHERE id = :id"
            ),
            {"d": json.dumps(rewritten), "id": str(row[0])},
        )

    # 2. Service-plan provisioning params: dict -> rows ------------------------
    plans = conn.execute(sa.text(
        "SELECT id, provisioning_params FROM service_plan WHERE provisioning_params IS NOT NULL"
    )).fetchall()
    for plan_id, params in plans:
        if isinstance(params, str):
            params = json.loads(params)
        if not isinstance(params, dict):
            continue  # already the list shape — idempotent
        rows_out = [
            {"key": str(k), "value": v, "description": None} for k, v in params.items()
        ]
        conn.execute(
            sa.text("UPDATE service_plan SET provisioning_params = CAST(:p AS JSON) WHERE id = :id"),
            {"p": json.dumps(rows_out), "id": str(plan_id)},
        )

    # 3. Workflow action_config variables -------------------------------------
    # Keys are author input; values may themselves be {{trigger.*}} templates,
    # which belong to a DIFFERENT namespace and must not be rewritten.
    for table in ("workflow_step", "workflow_template"):
        exists = conn.execute(sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
        ), {"t": table}).fetchone()
        if not exists:
            continue
        column = "action_config" if table == "workflow_step" else "definition"
        has_col = conn.execute(sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ), {"t": table, "c": column}).fetchone()
        if not has_col:
            continue
        for row_id, config in conn.execute(sa.text(
            f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
        )).fetchall():
            if isinstance(config, str):
                config = json.loads(config)
            updated = _namespace_action_variables(config)
            if updated == config:
                continue
            conn.execute(
                sa.text(f"UPDATE {table} SET {column} = CAST(:c AS JSON) WHERE id = :id"),
                {"c": json.dumps(updated), "id": str(row_id)},
            )


def _namespace_action_variables(node):
    """Recursively prefix the KEYS of any `variables` mapping with `input.`,
    leaving the values (which may be {{trigger.*}} templates) untouched."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "variables" and isinstance(value, dict):
                out[key] = {
                    (k if k.startswith("input.") else f"input.{k}"): v
                    for k, v in value.items()
                }
            else:
                out[key] = _namespace_action_variables(value)
        return out
    if isinstance(node, list):
        return [_namespace_action_variables(v) for v in node]
    return node


def downgrade() -> None:
    # Not reversible: the flat namespace was ambiguous by construction (that is
    # why it was replaced), so `service_plan.vlan` cannot be mapped back to a
    # unique legacy name, and the retired category aliases cannot be recovered
    # at all. Restore from a dump if this must be undone.
    raise NotImplementedError(
        "pv1_namespaced_variables is not reversible — restore from a backup"
    )
