# Limitations, TODOs, Known Debt

## Transitional / rollback-window debt

- **Dual-write rollback window: RESOLVED by `ld1_legacy_drop` (4.0.0).** The
  legacy `product`, `recurring_order`, `recurring_order_item`, `task_state` and
  `workflow_template` tables, their FK columns and the `products.*` /
  `recurring_orders.*` / `task_states.*` / `workflow_templates.*` permissions are
  gone; unbridged ACTIVE recurring orders were migrated into `client_service`.
  The migration is irreversible (see [migrations.md](migrations.md)).
- **`tier_change_request` table retained, model dropped**: the manual
  tier-change approval workflow (`TierChangeRequest` model, its routers, and
  its frontend UI) was removed — superseded by Recurrente self-serve
  checkout/cancel. The table itself is still physically present; its drop is
  a separate, later destructive-change release (all consuming code already
  removed from every service — this is purely the drop-after-prod rule).

## Transport and Capa 3 (2026-09-25, axis 2026-09-27) — shipped limitations

- **No hub with managed routes exists yet.** The `dial_target='device'` +
  `proxy_kind='socks5'` path is complete and unit tested, but nothing has ever
  dialled through a real one. An EXTERNAL `proxy_address` (a VPS, as opposed to a
  Railway-internal ZeroTier/Pylon service) carries a security prerequisite that is
  a prerequisite, not a code change: `microsocks` has no auth by default, so the
  listener must be firewalled to Railway's egress or anyone who learns the address
  has a route into the tenant LAN. The transport axis does NOT record which
  technology the hop is, so nothing in code can tell an internal address from an
  external one — this stays an operator rule. See
  [network-models.md](network-models.md).
- **`provisioning_settings.acs_base_url` has no writer.** It survived the fold
  because an installer needs a URL to type into a CPE, but it is read-only by
  construction (absent from `ProvisioningSettingsUpdate`) and nothing in code reads
  it either. Until something sets it — a seeded platform default, or a deliberate
  super-admin-only write path — it is a column that will read NULL for every
  tenant that did not already have one on `network_access`.
- **Nothing expires a credential rotation window.** The window is
  `cwmp_credential_id` + `cwmp_pending_credential_id` and closing it is an
  explicit commit or abort from the router. There is no reaper: a window left
  open stays open, and both secrets keep authenticating.
- **The per-CIDR / `mgmt_subnets` transport idea is DEAD, not deferred.**
  `network_access` was multi-row solely to support longest-prefix resolution over
  a JSON list of CIDRs, which was never implemented. `tr1_transport_axis` dropped
  the column, the table and the idea. The transport is tenant-wide; do not
  reintroduce per-address paths, and read any older document that describes them
  as history.
- **The Capa 3 gate fails OPEN.** An EXT fault or timeout, or an absent
  `cwmp.auth` document, is ALLOW. Deliberate — the alternative drops every CPE
  of every tenant during a backend blip — but it means a backend outage silently
  disables the gate rather than announcing itself.
- **Pre-existing `oui = ''` rows are left alone, but no new one can be written.**
  `ac1`'s partial UNIQUE covers `oui IS NULL` only, and a NULL row and a `''` row
  for the same serial are distinct under `uq_acs_registration_identity` too — so
  `('','SN1')` plus `(NULL,'SN1')` both committed and two tenants could claim one
  serial, defeating the index. `_normalize_oui` now returns `None` for a blank
  value, so every write path lands on NULL and the index covers it. No data
  conversion ships: converting existing `''` rows would collide with the index
  ac1 has just built. Legacy `''` rows stay readable and are tolerated by
  backend-erp's `_no_oui_filter()`; run
  `select serial_number, count(*) from acs_device_registration where oui is null or oui = '' group by 1 having count(*) > 1`
  before arming a tenant.
- **A 500 no longer logs handler arguments.** `handle_exceptions` used to write
  `args`/`kwargs` to loguru, which put `DeviceCredentialCreate.secret` and
  `RotationStartRequest.secret` — a tenant's whole-fleet CWMP password — in the
  logs on any unexpected error. It now logs argument *types* and keyword *names*
  only, so a 500 inside a secret-carrying handler is less diagnosable from the log
  alone; reproduce it against a scratch DB instead.

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
- **Closed in 6.4.0 (doc 43 §5.6):** the workflow engine's
  `ENQUEUE_PROVISIONING` used to bypass the kill switch, the device-type opt-out
  and the dry-run gate. The gates now live in `utils/provisioning_gates.py`; a
  live `create_run` refuses with `ProvisioningGateError` (mode A turns it into a
  FAILED step) and mode B checks `gate_failure` before its insert. A legacy
  automation that reaches a playbook not dry-run at its current version now
  fails visibly instead of running.
- **The system-playbook dry-run exemption is matched by name.** `playbook`
  has no `is_system` flag, so `gate_failure` exempts any playbook whose name is
  in `SYSTEM_PLAYBOOK_NAMES` — including a tenant's own playbook created or
  renamed to one (e.g. `huawei_onu_activate` on a tenant never seeded with it).
  Carried over verbatim from backend-erp; it matters more now that this is the
  only gate for every run producer. Fix: base the exemption on something a
  tenant cannot set (an `is_system` column, or the seed's creator), and have
  backend-erp's playbook create/rename reject names in `SYSTEM_PLAYBOOK_NAMES`.

## Provisioning runs (5.1.0, provisioning concurrency) — shipped limitations

- **Stranded runs are repaired by polling, not by the settle.** If `advance_run`
  fails inside the worker's settle (it runs in a savepoint so the job's outcome
  still commits), the run sits with no in-flight child until the reaper calls
  `repair_stranded_runs` (> 30 s quiet). A run quiet for more than
  `STRANDED_RUN_MAX_AGE` (1 h) is not advanced forward from a plan resolved
  long ago: in PRECONDITIONS it is closed FAILED (`STRANDED_RUN_EXPIRED`), in
  CONFIGURATION/VERIFICATION it enters ROLLBACK (engine v2, doc 42 §8.4). The
  one exception is a run whose final forward entry already succeeded: nothing
  is left to configure, so it is finished SUCCEEDED instead of rolled back.
- **No run-level cancel.** Cancelling a RUNNING child is per job; a run cannot be
  cancelled as a unit. A failed or cancelled child is never retried on its own
  (backend-erp `RUN_CHILD_NOT_RETRYABLE`): re-provision opens a new run.
- **The device lock is taken at claim, from the child's `inventory_item_id`.**
  A playbook step that targets a different device is not locked (backend-erp
  logs `TARGET_NOT_LOCKED`, warn-only); deriving the lock from step targets is
  tracked separately (R15).
- **`IN_FLIGHT` is hand-synced** with the predicates of
  `uq_provisioning_run_company_idem` / `uq_provisioning_job_company_idem`; if they
  drift, `create_or_get_run` and the index disagree. SQLite builds both indexes
  **without** their predicate, so a test that re-opens a run on the same key
  must recreate them partial (`tests/test_provisioning_run_v2.py` does).

## Engine v2 (6.3.0, doc 42) — shipped limitations

- **Run events are an in-process listener list** (`RUN_CLOSED_LISTENERS`), not
  an outbox table. A process that closes runs without importing backend-erp's
  `provisioning/run_events.py` emits nothing; backend-erp's test that both entry
  points import it is the guard. The workflow engine (cron-erp) only OPENS runs.
- **Stored legacy definitions are normalized on read**, not rewritten: the
  legacy shape, the read-only `steps` mirror and `_connectivity_definition`'s
  legacy shape all go in a later `pe2` cleanup.
- **The shared-device `wait_until` ceiling (120 s) needs the binding**, so the
  schema enforces only the 600 s cap; `shared_device_wait_errors()` is called by
  backend-erp's router.
- **No run-level deadline** (doc 42 Q9): a stranded forward run rolls back only
  after `STRANDED_RUN_MAX_AGE` (1 h).
- **Rollback children have no claim priority** over forward work, and a
  `NO_ROLLBACK_DEFINED` device always ends a rollback `ROLLBACK_INCOMPLETE`
  (an empty rollback is a save-time warning, not an error, doc 42 Q13).
- **Secrets need the KEK in every process that opens such a run**
  (`CREDENTIALS_KEKS` / `CREDENTIALS_ACTIVE_KEK_ID`); plans without `secrets`
  never touch crypto.

## Port labels (doc 40 C8) — transitional

- `inventory_item.parent_port` / `uplink_port` are **unmapped but still in the
  DB** (`pt2_unmap_port_labels`, 5.0.0, develop only). The column drop
  `pt3_drop_port_labels` (C8b) must not run against a database until every
  backend deployed on it runs C8a — on prod that means after the v1.0.1
  promotion.

## Migrations

- **`c1e_install_actions` is irreversible** — it uses
  `ALTER TYPE ... ADD VALUE`, which PostgreSQL cannot undo; there is no
  working downgrade. Any claim that "all migrations are reversible" is wrong.
- **`ng2_topology_drop` is irreversible by design** — its `downgrade()` raises
  `NotImplementedError`. A network graph cannot be turned back into a set of
  named device-type chains: the chains carried per-topology playbook bindings and
  pinned positions the graph does not encode. Restore from a backup taken before
  the release.
- **`ld1_legacy_drop` and `sh1_service_history_repair` are one-way** — both
  `downgrade()`s raise `NotImplementedError`. Prod's pre-v1.0.0 dump
  (`backups/prod-pre-v1.0.0-20261006T033428Z.dump`) is the rollback path.
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
- `CHANGELOG.md` has gaps: 0.7.0 → 1.11.1 and most 1.x releases between 1.11.1
  and 1.33.0 were not recorded per-version.
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
