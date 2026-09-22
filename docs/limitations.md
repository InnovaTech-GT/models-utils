# Limitations, TODOs, Known Debt

## Transitional / rollback-window debt

- **Dual-write rollback window is still open** (Cycle 2 entity merge):
  - `ClientService` still dual-writes into the legacy `recurring_order` table
    (see the comment near `isp.py:256`).
  - `order_item.product_id` is deprecated but still honored
    (`schemas/order_item.py`).
  - Bridge-less legacy products are treated as `SERVICE` by the workflow
    engine (`workflow_engine.py`, catalog-merge handling).
  - Removal awaits a post-production bake period.
- **Legacy models retained**: `Product`, `RecurringOrder`/`RecurringOrderItem`
  are kept for the transition and because `cron-erp` still consumes
  RecurringOrder for recurring order generation.
- **`tier_change_request` table retained, model dropped**: the manual
  tier-change approval workflow (`TierChangeRequest` model, its routers, and
  its frontend UI) was removed — superseded by Recurrente self-serve
  checkout/cancel. The table itself is still physically present; its drop is
  a separate, later destructive-change release (all consuming code already
  removed from every service — this is purely the drop-after-prod rule).

## The network graph (Cycle 10, doc 35 §10) — shipped limitations

These are known and accepted, not oversights. They are the price of the model
chosen in doc 35, and each is a thing a real carrier can walk into.

- **Single parent only.** `inventory_item.parent_id` is one nullable self-FK and
  the DB trigger enforces a strict tree, so **redundant paths, protected rings
  and dual-homed aggregation cannot be modelled**. This is doc 07's original
  position and is correct for PON — physical signal paths in PON *are* trees —
  but it is wrong for a metro-ethernet core, and a carrier with a protected ring
  will notice. Adding a second parent is not a column change: it invalidates
  "the path" as a single ordered list, which the resolver, the run `plan`, the
  `path.<category>` namespace and the leaf→root execution order all assume.
- **No automatic chain → graph backfill.** `ng2_topology_drop` **raises** rather
  than repairing when it finds a `client_service` with a `topology_id` and no
  `cpe_item_id`. A chain names device *types*; a graph names device *instances*
  and the physical edges between them. Deriving one from the other would mean
  inventing parent relationships — asserting that this ONT hangs off that
  splitter when nothing in the database says so — which is exactly the kind of
  "helpful" migration discovered six months later when a technician is sent to
  the wrong pole. Any tenant that built topologies on `develop` re-declares its
  plant once by hand. Acceptable because no tenant has.
- **`path.<category>` picks the nearest node to the CPE** when a role repeats on
  one path. That is unambiguous by construction — a tree gives a total order
  along a path — but it means a playbook **cannot address the *second* OLT
  above it**. There is no `path.olt[1]`, deliberately: reintroducing an index
  would reintroduce exactly the positional fragility this cycle removed.
- **The graph is provisioning truth, not physical truth.** Nothing verifies that
  the modelled parent matches the fibre actually plugged in. A wrong `parent_id`
  produces a confidently wrong configuration path, and neither the trigger nor
  the resolver can detect it. Re-parenting is also never auto-provisioning: it
  stamps `client_service.path_changed_at` and surfaces a chip, and an operator
  confirms — so a stale path can persist indefinitely if nobody acts on it.
- **Purely civil-works elements cannot be nodes.** A hand-hole or a pole with no
  equipment record has no `inventory_item` to attach. Tracked passive gear
  (splitters, splice closures) already is an `InventoryItem` and works fine; if
  untracked structural nodes are ever needed the answer is a device category for
  them, not a second table.
- **Pre-existing and deliberately untouched:**
  `workflow_engine._execute_enqueue_provisioning_path` (like the
  `_execute_enqueue_provisioning` it replaced) **still never calls
  `enforce_provisioning_gates`**, so automation-triggered runs bypass the
  dry-run gate and the tenant kill switch — those gates live in backend-erp and
  are only invoked by its routers. Cycle 10 moved this code but did not fix it:
  fixing it here would change automation behaviour mid-cycle, silently. Filed
  (doc 33 "Known gap", doc 35 §10), not smuggled in.

## Migrations

- **`c1e_install_actions` is irreversible** — it uses
  `ALTER TYPE ... ADD VALUE`, which PostgreSQL cannot undo; there is no
  working downgrade. Any claim that "all migrations are reversible" is wrong.
- **`ng2_topology_drop` is irreversible by design** — its `downgrade()` raises
  `NotImplementedError`. A network graph cannot be turned back into a set of
  named device-type chains: the chains carried per-topology playbook bindings and
  pinned positions the graph does not encode. Restore from a backup taken before
  the release.
- **Two vestigial topology surfaces survive.**
  `ServicePlanBase`/`ServicePlanUpdate.default_topology_id` and
  `workflow_fields.py`'s `client_service.topology_id` (`fk_to: "topology"`)
  are still declared even though the columns and the `topology` table are gone.
  They are inert — an accepted-but-ignored request field and a trigger field
  that can never match — but they are drift, and they should be removed in a
  follow-up.

## CI / packaging

- **ruff is advisory only** in CI (`continue-on-error: true`) — lint failures
  do not block merges.
- `setup.cfg` still carries the placeholder `author_email = you@example.com`.
- `CHANGELOG.md` has a documented gap: 0.7.0 → 1.10.0 releases were not
  recorded per-version (see the note at the top of that file).
- **Naming mismatch** (repo `models-utils`, package `database-utils`, module
  `database_utils`) is historical and now documented, but still a recurring
  source of confusion.

## Platform-level accepted debt (ADR-009)

- Real device drivers (ssh / telnet / snmp / tr069) are tracked TODOs. They
  will land in backend-erp's provisioning worker; the **resolution logic**
  (graph path → per-node playbook → variable frames) lives here. The network
  config layer design (platform docs 21/23 — cloud-side drivers, shared
  GenieACS, netmiko) is design-only and not implemented in this repo.

## Architectural constraint (by design, but worth knowing)

- Because backends may never be imported by this library, business logic the
  workflow engine needs keeps migrating *down* into models-utils (e.g.
  `provisioning_resolution.py` moved here in Cycle 3). Expect this "models"
  library to keep accumulating engine-adjacent logic — see
  [architecture.md](architecture.md).
