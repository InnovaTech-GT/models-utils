"""ng1/ng2 structural guarantees (doc 35 §8).

These are static assertions about the revision files themselves. The behavioural
proof — that the chain applies cleanly against a real production dump and is
byte-identical on a second run — is the rehearsal, not a unit test.
"""

import importlib.util
import pathlib

VERSIONS = pathlib.Path(__file__).parent.parent / "alembic" / "versions"
NG1 = VERSIONS / "ng1_network_graph.py"
NG2 = VERSIONS / "ng2_topology_drop.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _upgrade_body(path) -> str:
    body = path.read_text()
    return body.split("def upgrade")[1].split("def downgrade")[0]


# ----------------------------------------------------------------------- ng1

def test_ng1_revision_id_is_within_the_alembic_limit():
    mod = _load(NG1, "ng1")
    assert mod.revision == "ng1_network_graph"
    assert len(mod.revision) <= 32
    assert mod.down_revision == "lc2_retire_susp_react"


def test_ng1_drops_nothing():
    """The additive half must be reviewable as 'what appears', with no surprises."""
    upgrade = _upgrade_body(NG1)
    for forbidden in ("drop_table", "drop_column", "DROP TABLE", "DROP COLUMN"):
        assert forbidden not in upgrade, f"ng1 must be additive, found {forbidden}"


def test_ng1_installs_both_guards():
    body = NG1.read_text()
    assert "trg_inventory_item_graph_guard" in body
    assert "trg_inventory_item_detach_guard" in body


def test_ng1_guard_rejects_cycles_cross_tenant_parents_and_depth():
    body = NG1.read_text()
    for code in ("NETWORK_GRAPH_CYCLE", "NETWORK_GRAPH_CROSS_TENANT",
                 "NETWORK_GRAPH_TOO_DEEP", "NETWORK_GRAPH_SELF_PARENT",
                 "NETWORK_GRAPH_PARENT_DETACHED", "NETWORK_GRAPH_HAS_CHILDREN"):
        assert code in body, f"{code} not raised by the guard"


def test_ng1_depth_bound_matches_the_traversal_module():
    from database_utils.utils.network_graph import MAX_PATH_DEPTH
    mod = _load(NG1, "ng1_depth")
    assert mod.MAX_PATH_DEPTH == MAX_PATH_DEPTH, (
        "the trigger and the CTE must agree, or traversal raises PATH_TOO_DEEP "
        "on paths the database accepted"
    )


def test_ng1_recursive_cte_is_bounded():
    """An unbounded recursion over a cycle does not error, it hangs.

    Asserted against the RENDERED plpgsql, not the source text: the bound is
    interpolated from MAX_PATH_DEPTH, and a test reading the f-string would pass
    even if the constant were removed.
    """
    mod = _load(NG1, "ng1_cte")
    assert f"anc.depth < {mod.MAX_PATH_DEPTH}" in mod._GRAPH_GUARD_FN


# ----------------------------------------------------------------------- ng2

def test_ng2_revision_chain():
    mod = _load(NG2, "ng2")
    assert mod.revision == "ng2_topology_drop"
    assert len(mod.revision) <= 32
    assert mod.down_revision == "ng1_network_graph"


def test_every_retired_token_is_guarded():
    mod = _load(NG2, "ng2_tokens")
    assert set(mod.RETIRED_TOKENS) == {
        "chain[", "edge_devices[", "core_devices[", "RETIRED_ALIAS",
        "target_position",
    }


def test_guards_run_before_any_drop():
    upgrade = _upgrade_body(NG2)
    assert upgrade.index("_assert_no_retired_syntax") < upgrade.index("drop_table")
    assert upgrade.index("_assert_every_service_has_a_cpe") < upgrade.index("drop_table")


def test_columns_are_dropped_before_tables():
    """A referencing FK would otherwise block DROP TABLE topology."""
    upgrade = _upgrade_body(NG2)
    assert upgrade.index("drop_column") < upgrade.index("drop_table")


def test_the_workflow_rewrite_is_predicate_guarded_for_idempotency():
    body = NG2.read_text()
    assert "use_topology" in body
    assert "LIKE '%use_topology%'" in body


def test_ng2_is_explicitly_irreversible():
    mod = _load(NG2, "ng2_down")
    try:
        mod.downgrade()
    except NotImplementedError as exc:
        assert "not reversible" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("downgrade must refuse rather than lose the plant")
