# Utilities

## Description

Shared utility modules in `database_utils/utils/` (28 modules) plus the
supporting `dependencies/` and `middleware/` packages. The two largest —
the workflow engine and provisioning resolution — have their own page:
[workflow-engine.md](workflow-engine.md).

## Goal

Eliminate duplication across auth-erp and backend-erp by centralizing common
patterns, and host all logic the workflow engine needs (backends import this
library, never the reverse).

## Utility Modules (in `database_utils/utils/`)

| Module | Purpose |
|------|---------|
| `workflow_engine.py` (58 KB) | Trigger matching + async DAG execution (`check_workflow_triggers`, `execute_workflow`, `execute_step`) — see [workflow-engine.md](workflow-engine.md) |
| `network_graph.py` | The **only** place the company plant tree is walked (Cycle 10, doc 35 §3) — see below |
| `provisioning_resolution.py` | Resolves a service's configuration path, its per-node playbooks and its variable frames (moved down from backend-erp in Cycle 3; **rewritten in Cycle 10** to traverse the graph instead of matching a topology chain) — see below |
| `provisioning_runs.py` | Opens and advances a multi-device `ProvisioningRun` (Cycle 10, doc 35 §5; **engine v2**, doc 42: phase-major plan, whole-run rollback, `close_run` + `RUN_CLOSED_LISTENERS`, revert, rollback retry, per-run secrets) — see below |
| `playbook_expr.py` | Declared integer arithmetic for a playbook's `computed` block (doc 40 §3.3.3, ADR-006 amendment) and the shared `is_secret_name`/`_SECRET_HINTS` (moved from backend-erp's renderer, which re-exports them) — see below |
| `transport.py` | Transport resolver, `resolve_endpoint()` + `default_outbound_access()` (2026-08-13, doc 34 canon R23 rewrite; `vpn` branch 2026-09-25) — see below |
| `jwt_utils.py` | HS256 JWT create/decode. Env: `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE` (minutes, default 1440), `REFRESH_TOKEN_EXPIRE` (**seconds**, default 604800; dev/prod set 2592000 = 30 days), `MOBILE_ACCESS_TOKEN_EXPIRE` (minutes, default 60). Every token carries a `type` claim: `create_access_token(usuario, expires_minutes=None, sid=None)` → `"access"` (+ `sid` = the refresh `family_id` when auth-erp issues a pair — see `sessions.py`); `create_refresh_token(usuario, client_type="web", jti=None)` → `"refresh"` + `cl` (`"m"`/`"w"`, so `/refresh` keeps the client's TTL) + `jti` (the given one, else a fresh uuid4 hex — keys the `auth_refresh_token` row auth-erp records for rotation reuse detection). `is_refresh_payload(p)` also recognises legacy tokens (no `type`, no `roles`). `get_current_user` and `require_permission` reject refresh tokens (401 `Invalid token type`); `require_permission` also returns 403 `Account has been deactivated` for an inactive user. Both reject an access token whose session was revoked with 401 `SESSION_REVOKED` (`sessions.check_session`). `decode_token` never logs the payload. **Fails fast if `SECRET_KEY` is unset when `ENVIRONMENT=production`**; dev fallback otherwise |
| `permission_utils.py` | `PermissionChecker` and require-permission FastAPI dependencies |
| `sessions.py` | Access-token revocation (bug-fix/access-token-revocation). `check_session(db, payload)` — called by `get_current_user` and `require_permission` (so also backend-erp's `require_all_permissions`/`require_any_permission`) — raises 401 `{"code": "SESSION_REVOKED"}` when the token's `sid` family has any `auth_refresh_token` row with `revoked_at` set (logout, refresh reuse, user deactivated, password changed/reset). `is_session_revoked(db, sid)`: one lookup on the indexed `family_id` (no migration needed), cached per process — a revoked verdict until the cache clears, a live verdict for `SESSION_CACHE_SECONDS` (30). `revoke_user_sessions(db, user_id=… \| company_id=…)` revokes every family of a user/company (caller commits) and clears this process's cache. **Revocation latency: immediate in the revoking process, ≤ 30 s in every other process/replica** (no Redis — neither backend has a shared client in models-utils). **Legacy access tokens without `sid` are accepted until they expire** (≤ 24 h web / 60 min mobile after deploy) so a deploy logs nobody out; a malformed `sid` is rejected |
| `audit_utils.py` | `log_create_operation` / `log_update_operation` / `log_delete_operation` / `log_custom_operation` helpers writing `AuditLog` rows |
| `ssrf.py` | `validate_url_no_ssrf` blocklist — shared by the integration-test endpoint and workflow `HTTP_REQUEST` steps (SEC-6) |
| `workflow_fields.py` | Trigger-context variable/field handling for workflow steps |
| `tier_limits.py` | Tier resource-cap enforcement |
| `pagination_utils.py` | Pagination helpers returning `PaginatedResponse[T]` |
| `timezone_utils.py` | `now_gt()` / `today_gt()` — America/Guatemala (UTC-6, no DST) |
| `token_utils.py` | Token generation/validation helpers (email verification, password reset) |
| `password.py` | bcrypt password hashing/verification |
| `order_typing.py` | Order type/classification helpers |
| `json_utils.py` | JSON serialization helpers |
| `email_templates.py` | Jinja2 rendering of the email templates (see [email-service.md](email-service.md)) |
| `error_handling.py` | `handle_exceptions` — wraps an async handler, re-raises `HTTPException`, converts anything else to a 500. Logs argument **types and keyword names only, never values**: every decorated handler receives its request body and the Capa 3 bodies carry plaintext secrets |
| `exception_handlers.py` | Standardized FastAPI exception handlers |
| `logging_utils.py` | Loguru structured JSON logging setup |
| `telemetry_utils.py` | `get_tracer`, `set_request_span_attributes` — OTEL **API only**; SDK/exporter configured by the consuming services |
| `router_factory.py` | FastAPI router factory helpers |

## The provisioning trio (Cycle 10, doc 35)

These three modules are read together: `network_graph` produces the path,
`provisioning_resolution` turns it into playbooks and variables, and
`provisioning_runs` executes it as one child job per device. All three live here
rather than in backend-erp because the workflow engine's `ENQUEUE_PROVISIONING`
step calls them and the import direction is strictly downward.

### `network_graph.py`

The plant is a strict tree of `inventory_item` rows joined by `parent_id`, and
this module is the only place that walks it. Written against SQLAlchemy Core's
recursive-CTE API rather than raw `text()`, so the same code runs on Postgres and
on the in-memory SQLite the unit tests build with `create_all`.

| Name | Behaviour |
|---|---|
| `MAX_PATH_DEPTH = 32` | Hand-kept in sync with the same constant inside `trg_inventory_item_graph_guard` (revision `ng1_network_graph`). If they disagree the trigger wins, and traversal starts raising `PATH_TOO_DEEP` on paths the DB happily accepted |
| `GraphError(code, detail)` | `code` is stable and API-facing (`PATH_TOO_DEEP`) |
| `resolve_path(db, item_id, company_id) -> list[InventoryItem]` | The node itself, then every ancestor, ordered **leaf → root**. Returns `[]` for an unknown item or one belonging to another company; raises `GraphError("PATH_TOO_DEEP")` past the bound |
| `descendants(db, item_id, company_id)` | Everything behind a node, excluding the node itself, ordered by depth (nearest first). Impact analysis: "who is affected if I re-parent or take down this OLT?" |
| `would_create_cycle(db, item_id, new_parent_id, company_id) -> bool` | Service-layer pre-check mirroring the DB trigger, so the API can answer 422 with a readable message instead of surfacing a raised Postgres exception. The trigger remains the guarantee; this is the courtesy, and the two must stay in agreement |
| `child_count(db, item_id, company_id) -> int` | Immediate children only — the detach guard and the tree UI |
| `assert_links_consistent(db)` | (doc 40) Test helper: raises `AssertionError` naming every `network_link` whose down item's `parent_id` is not its `up_item_id` (the `NETWORK_LINK_PARENT_MISMATCH` the pt1 deferred triggers enforce on Postgres), or whose ports/items belong to another item or tenant (the composite FKs). SQLite `create_all` schemas have neither, so graph tests here and in backend-erp call it after each write |

Two invariants are load-bearing and appear in every query here:

1. **`company_id` is filtered in BOTH the anchor and the recursive term.** A
   cross-tenant tree cannot be produced through the API (the trigger rejects a
   cross-company parent), but a traversal that only filtered its anchor would
   still happily follow such an edge if one ever appeared through a direct DB
   write. Filtering both terms makes cross-tenant traversal impossible rather
   than merely unlikely — and `test_network_graph_traversal.py` proves it by
   writing an illegal edge with raw SQL, past both the service layer and the
   trigger.
2. **Every recursion is bounded by `MAX_PATH_DEPTH`.** An unbounded recursive CTE
   over a cycle does not error, it hangs — the worst possible failure mode for a
   query on the provisioning hot path.

`_ordered_items` re-hydrates the ORM objects in the order the CTE produced: the
CTE returns ids, and a plain `IN (...)` re-query returns them in whatever order
the planner likes. Path order is load-bearing, so it is re-imposed, not trusted.

### `provisioning_resolution.py`

`resolve_provisioning(db, client_service, purpose=PURPOSE_ACTIVATION)` starts at
the service's CPE and walks to the root:

1. `client_service.cpe_item_id` unset → **`CPE_NOT_SET`**
2. that CPE not attached to the graph → **`CPE_NOT_ATTACHED`**
3. `path = resolve_path(cpe)`, leaf → root (a `GraphError` is re-raised as a
   `ResolutionError` carrying the same code)
4. a node whose category `is_passive` contributes nothing but stays on `path`
5. every other node resolves **node override → device-type default → none**
   (`resolve_playbook_for`, one helper so the resolver, the API path preview and
   the node detail endpoint share one lookup semantics)
6. an active node with no playbook for the purpose is reported
   **`PLAYBOOK_NOT_BOUND`** — fatal for `ACTIVATION` unconditionally, and for
   other purposes only when some playbook on the path actually reads a
   device-derived variable (the pre-existing amendment-4 rule). A bound playbook
   that is inactive or belongs to another company raises **`PLAYBOOK_INACTIVE`**;
   a per-service parameter that is blank *and* referenced by a playbook on the
   path raises `RESOLUTION_FAILED` with `MISSING_SERVICE_PARAM` errors.
7. **Resolution-time refusal (doc 40 §3.3.2).** For every step node, the
   playbook's `computed` block is evaluated (`playbook_expr.evaluate_all`) and
   every resolver-owned token the executor renders must have a value — in
   **every phase, rollback included** (engine v2, doc 42 §9.4: a run must not
   start if its undo cannot render): templates, http/tr069 `request`,
   `target_item_id`, step guards, validation strings, capture regexes and
   thresholds, and every output value. `capture.*` / `secret.*` are outside
   `RESOLVER_NAMESPACES` and skipped (checked at save time instead). For any purpose other than
   ACTIVATION, every port fact the step reads must also match the last live
   activation. Everything found is raised once as `RESOLUTION_FAILED` with the
   whole list (see below). This applies to **every** purpose.

Runs execute on frames frozen at creation, so before step 7 a missing OLT port
failed the OLT step *after* another device had already been configured; now
nothing touches a device first.

Step 6 preserves the fatality posture exactly: ACTIVATION fails visibly (a
half-provisioned install is worse than a refused one), while a SUSPENSION whose
OLT happens to have no suspend playbook still suspends whatever it can.

**Retired with the chain, and they cannot occur any more:** `MISSING_DEVICE`,
`AMBIGUOUS_DEVICE`, `PINNED_DEVICE_UNAVAILABLE`, `TOPOLOGY_NOT_SET`,
`TOPOLOGY_INACTIVE`, `PURPOSE_NOT_CONFIGURED`. Every node on the path *is* a
concrete device, so there is nothing left to match or disambiguate — which
deletes the single most common class of provisioning failure in the old system.

Returns two dataclasses:

- `ResolvedNode` — `position` (hop count from the CPE, 0-based leaf → root),
  `item_id`, `serial_number`, `mac_address`, `device_type_id`,
  `device_type_name`, `category_key`, `category_tier`, `mgmt_host`, `mgmt_port`,
  `is_passive`, `playbook_id`, `playbook_source` (`"node"` | `"device_type"` |
  `None`), and since doc 40: `label` (`inventory_item.label`), `path_role`
  (the device type's), `out_slot`/`out_port`/`out_port_name` (see below) and
  `playbook_version` (the bound playbook's `version`, steps only). `position`
  is a **fact about the resolved path, never an addressing mechanism** —
  nothing templates it.
- `ResolvedProvisioning` — `path` (every node, passives included, so an operator
  can see that a splitter was considered and deliberately skipped rather than
  wondering where it went), `steps` (the subset that will be configured),
  `shared_variables`, `device_variables` (`item_id → that node's device.* frame`),
  `ambiguous_roles` (`role → [item_id, …]` for path roles held by more than one
  node — those get no frame).

**Two dicts, deliberately.** `device.*` means "the box this playbook is running
on", so it differs per child job; a single flat dict cannot express that. The
path-scoped half is identical for every node and is resolved once. Each child
job's `variables` column is written as
`shared_variables | device_variables[item_id]`, so the executor and the renderer
still receive exactly one flat dict and their contract is untouched.

#### The variable namespace

| Namespace | Contents |
|---|---|
| `device.<attr>` | the device **this playbook is running on** |
| `cpe.<attr>` | the subscriber edge device that triggered the run (the leaf, always `path[0]`) |
| `path.<category_key>.<attr>` | any node on **this run's** path, named by its device-category key; **nearest-to-the-CPE wins** if a role repeats. Passives are addressable too (a playbook may legitimately want the splitter's serial for a description field) |
| `path.<path_role>.<attr>` | (doc 40) the node whose device type carries this per-company `path_role` (`mufa_principal`), emitted **only when exactly one node on the path holds it** — a repeated role is collected in `ambiguous_roles` instead, because "nearest wins" would silently pick the wrong splitter. A role whose name is already a category frame on the path raises **`ROLE_SHADOWS_CATEGORY`** rather than overwrite it — whatever the number of nodes holding it |
| `computed.<key>` | (doc 40) a playbook's declared integer arithmetic. Evaluated here for refusal and again by backend-erp's renderer; never stored in the frames |
| `service_plan.<field\|param>` | plan fields plus the plan's tenant-authored rows (`plan`- and `service`-scoped alike — the author writes `{{service_plan.<key>}}` either way) |
| `client.<attr>` | built-in subscriber fields plus the tenant's own client custom fields (built-ins win a clash) |
| `service.<attr>` | the `client_service` itself |
| `input.<key>` | author-declared playbook variables; the namespace is applied at REFERENCE time by `input_key()`, the declared key stays bare |

`DEVICE_ATTRIBUTES = ("item_id", "serial", "mac", "type", "category",
"category_tier", "mgmt_host", "mgmt_port", "depth")` — one tuple shared by all
three device namespaces, built by `build_device_frame(node, prefix)`. `depth` is
hops from the CPE (`cpe.depth == 0`).

`PORT_ATTRIBUTES = ("out_slot", "out_port", "out_port_name")` (doc 40 §3.3.1) —
emitted by `build_device_frame` **only when known** (ints for slot/number, the
port's name as a string, `out_slot` only when the port has a slot). An absent key
is not `""`: the renderer tests presence as `vars[name] is not None`, so an empty
string would render `slot  link` and fail open. A frame's keys are therefore
always ⊇ `DEVICE_ATTRIBUTES` and ⊆ `DEVICE_ATTRIBUTES ∪ PORT_ATTRIBUTES` (the pin
test). `out_*` of `path[i]` is the port on `path[i]` that `path[i-1]` hangs off,
read from `path[i-1]`'s `network_link` in **one company-scoped query** joined to
the upstream ports, and used **only if** `link.up_item_id == path[i].item_id` —
a reparent committed between the path read and the link read must not lend a
node another device's port. The CPE never has `out_*`; an unported edge (parent
but no link) simply has no keys. `out_port_name` is the template (factory) name
(`ether2`), so RouterOS templates use `[find default-name=…]`.

#### Resolution-time refusal errors (doc 40 §3.3.2)

Namespaces checked are the resolver's own: `device`, `cpe`, `path`,
`service_plan`, `client`, `service`, `computed`. A token whose body has a
`| default:` filter (regex `\|\s*default\s*:`, not a substring test, run after
quoted filter arguments are stripped so `replace:"|default:","x"` does not count
— 4.5.2, review F2) is skipped;
`input.*` keeps the renderer's required/default rule; bare legacy tokens are
skipped. A **malformed** construct — a token-shaped body whose head does not
parse (`{{path.ROUTER.serial}}`, `{{ not a token }}`) or a residual `{{`
outside any token shape — is `UNRESOLVED_TOKEN` `reason: malformed` (`token` is
the raw construct, plus `item_id`) whatever its namespace or `| default:`,
mirroring the executor's leftover guard, which fails the step on it anyway. A
missing value is explained as one of:

| Code | Fields | When |
|---|---|---|
| `ROLE_AMBIGUOUS` | `token`, `role`, `item_ids` | `path.<role>.*` for a role held by more than one node |
| `PORT_NOT_RECORDED` | `token`, `item_id`, `label`, `position`, `reason: no_link\|no_slot` | a port attribute of a node above the CPE (`no_slot`: the port is known but has no slot) |
| `UNRESOLVED_TOKEN` | `token`, `reason: not_on_path\|missing_value\|malformed` | anything else (`not_on_path`: no node holds that `path.<segment>`) |

Plus `COMPUTE_*` errors from `evaluate_all` (with `key` and the step's
`item_id`; an entry skipped for a missing operand is reported once, through its
operand), and, for non-ACTIVATION purposes, **`PATH_CHANGED_SINCE_ACTIVATION`**
(`token`, `was`, `now`, `item_id`): a port attribute a step reads (directly or
as a computed operand) whose value differs from the frames of the service's last
**SUCCEEDED, non-dry ACTIVATION** run. No baseline run, or a baseline that never
had that key (activated before doc 40, or the port was not recorded then), means
no check for that fact. A fact cleared since activation (`now: null`) *is*
drift, even behind `| default`. This keeps a SUSPENSION or DEPROVISION from
addressing another subscriber's ONU id after a port correction, without blocking
on unrelated edits. Identical errors from two playbooks are de-duplicated.

**Retired outright, with no compatibility shim:** `chain[n].*`,
`edge_devices[n].*`, `core_devices[n].*`, the `position` attribute, and
`PlaybookStep.target_position`. `ng2_topology_drop` refuses to run over any
playbook whose definition still contains them.

Addressing is by **category**, not device-type slug and not relative hop.
Category is the stable semantic role ("OLT") on a curated, platform-global table
with a unique immutable key; a device-type slug is the hardware ("Huawei MA5800")
and would break every playbook on a vendor swap. Relative hops (`parent.parent.*`)
break the instant a splitter is inserted mid-path — the exact positional
fragility this cycle exists to remove — and have no downward form, so a
core-router playbook could never name the CPE. Addressing is *path*-relative
rather than *upstream*-relative for the same reason.

`variables` remains a **flat** dict whose keys are the whole dotted strings:
`path.olt.serial` is a key, not a walk. A nested dict under a namespace prefix
deliberately does **not** satisfy a dotted token — allowing it would be attribute
access by the back door (ADR-006).

> ⚠️ **Both token regexes in this module FAIL OPEN.**
> `_DEVICE_VARIABLE_PATTERN` now recognizes `device|cpe|path.<category>` and
> `_playbook_references_token` matches a single token; each tolerates the doc-34
> `| filter` suffix (`_FILTER_SUFFIX`). Since doc 40 both also read the
> `computed` block: an operand in a device namespace makes the playbook
> device-referencing, a token used only as an operand counts as referenced, and
> a block that does not parse counts as referencing everything. Path roles and
> the port attributes are single segments, so the regex itself did not change. A namespace that is emitted but not
> listed in the pattern does not raise, does not warn, and does not fail a test
> that is not looking for it — it quietly turns a hard resolution error into a
> partial run that half-configures a paying customer. Add a namespace here in the
> same commit you add it anywhere else;
> `test_device_variable_pattern_matches_the_new_namespaces` exists solely to
> catch that omission.

### `provisioning_runs.py`

| Function | Behaviour |
|---|---|
| `run_idempotency_key(client_service_id, purpose, dry_run=False)` | **The single shared run key** (provisioning-concurrency fix): `deprovision-{service_id}` for DEPROVISION (the bare string the retired `service-removal` template composes, so a still-active copy dedupes against the native run), otherwise `path-provision-{service_id}-{purpose_lower}`; `-dry` appended for a dry run. `/provision`, backend-erp's lifecycle hooks and the workflow engine's default all use it, so they dedupe against each other |
| `find_in_flight_run(db, company_id, key)` | Dedupe lookup over `IN_FLIGHT = (QUEUED, RUNNING, PENDING_INFORM)`. That tuple **must** mirror the predicate on `uq_provisioning_run_company_idem`; if they disagree, the dedupe check and the unique index disagree and one of them starts raising `IntegrityError` |
| `order_for_purpose(steps, purpose)` | Engine v2 (doc 42 §5). Build order = core bottom-up (leaf → root without the CPE), **CPE last** (founder 9.A); `TEARDOWN_PURPOSES` (SUSPENSION, DEPROVISION) run it reversed (CPE first, OLT last). Custom purposes use build order. `resolve_path`, `ResolvedProvisioning.steps` and `run.path` stay leaf → root |
| `create_run(db, client_service, purpose, dry_run, triggered_by, ..., resolution=None)` | Resolves (or accepts an already-resolved `ResolvedProvisioning`, which the manual endpoint passes so it can 422 with the error list before touching anything), opens the run, and queues **only its first child**. Resolution happens exactly once per run. **Engine v2:** loads each bound playbook and snapshots its **normalized** definition in `frames["definitions"][playbook_id]` (children execute the snapshot, never the live row); builds a **phase-major** `plan` — every device's PRECONDITIONS, then CONFIGURATION, then VERIFICATION, each in `order_for_purpose`, one entry per (device, phase) with steps: `{item_id, playbook_id, playbook_version, category_key, device_label, phase, steps: [{name, label}], probe?}`. `probe: true` marks the implicit preflight session step `__session__` (doc 42 §6.4: a device whose configuration/verification uses ssh/telnet, that is not the CPE in build order, and whose preconditions do not already start with ssh/telnet). Raises `ResolutionError` `OUTPUT_KEY_CONFLICT` (two devices publish one output key) / `SECRET_SPEC_CONFLICT` (one secret key, two specs). A **non-dry** plan that declares `secrets` generates each once (`secrets.choice` over `SECRET_ALPHABET`) and stores them envelope-encrypted (`crypto.encrypt_secret(json, company_id, run.id)` → `secrets_ciphertext`/`secrets_dek_wrapped`/`secrets_kek_id`) **before** any row is inserted; a missing KEK raises `SECRETS_KEY_UNAVAILABLE`. A **dry run** also plans every device's ROLLBACK (reverse configuration order) and generates nothing. Each child gets `phase` and `max_attempts = PHASE_MAX_ATTEMPTS[phase] (3, ROLLBACK 5) × len(job_steps)` |
| `create_or_get_run(db, client_service, purpose, dry_run, idempotency_key=None, **create_run_kwargs) -> (run, created)` | `create_run` deduped on the run key (`idempotency_key` or `run_idempotency_key(...)`): an in-flight run with that key comes back with `created=False`. The INSERT runs in a **SAVEPOINT**, so losing a race to a concurrent producer (`uq_provisioning_run_company_idem`) rolls back only the savepoint and returns the winner — the caller's session stays usable. Any other `IntegrityError` (no winner found) and every other error propagate. **Re-run (doc 42 §7.7, founder Q10):** both idempotency indexes are partial over the in-flight statuses, so once a run is terminal the same key opens a **new** run (fresh resolution, snapshot, secrets, empty outputs); a run still in ROLLBACK is in flight and is returned instead |
| `advance_run(db, job) -> ProvisioningJob \| None` | Called when a job reaches a terminal state. Locks the run row (`SELECT … FOR UPDATE`, `populate_existing`; lock order everywhere is job row, then run row) and is a **no-op** when the run is already terminal, when `job` is still in flight, or when a later child already exists. A standalone job (`run_id` NULL) is a no-op. Then (doc 42 §6.3) it merges `job.log.outputs` into `run.outputs` (last writer per `(item_id, key)`, on every terminal child, success or not) and applies the table: **PRECONDITIONS** failed → FAILED/`PRECONDITION_FAILED` with `error = "<device> · <step label>: <display>"`, cancelled → CANCELLED/`CANCELLED`, **never a rollback**; **CONFIGURATION/VERIFICATION** failed or cancelled → `error_code` `CONFIGURATION_FAILED`/`VERIFICATION_FAILED`/`CANCELLED`, and the outcome follows the **rollback set** (devices whose CONFIGURATION child has a non-empty `ran_steps`): empty → FAILED (or CANCELLED), devices untouched; only `NO_ROLLBACK_DEFINED` devices and no entry → FAILED/`ROLLBACK_INCOMPLETE`; otherwise ROLLBACK entries are appended and the first is queued (`phase = ROLLBACK`); **ROLLBACK** any outcome → next rollback entry (rollback continues past failures); after the last one, ROLLED_BACK when the **latest ROLLBACK child per item** SUCCEEDED and no device is `NO_ROLLBACK_DEFINED`, else FAILED/`ROLLBACK_INCOMPLETE` (`error` = the original cause + a JSON list of failed steps, one `device_action_log` row `rollback_incomplete` per failed device). Forward success → next entry; the last → SUCCEEDED (+ `path_changed_at` cleared for ACTIVATION). A **dry run** never branches: SUCCEEDED only if every child succeeded, and then stamps `last_dry_run_version` on each plan playbook **only if its live version still equals the planned one**. A plan entry with no `phase` (a run opened before engine v2) keeps the pre-v2 rule: any non-success stops the run with that status |
| `close_run(db, run, status, error_code=None, error=None)` | **The only writer of a terminal run status** (doc 42 §6.5): sets `status`, `finished_at` and, when given, `error_code`/`error`; for a **non-dry** run then calls every `RUN_CLOSED_LISTENERS` function with `(db, run)`, each in its own SAVEPOINT — a raising listener is logged and rolled back alone, the outcome is kept. backend-erp's `provisioning/run_events.py` registers the listener that classifies `RUN_SUCCEEDED` / `RUN_FAILED` / `RUN_NEEDS_ATTENTION` / `RUN_ROLLBACK_INCOMPLETE` for SP2 |
| `ran_steps(job) -> [name]` | Configuration steps that may have changed the device (doc 42 §7.3), from the **last step-log entry per name**: SUCCEEDED or FAILED at stage `command` (or an unknown stage) ran; SKIPPED and FAILED at stage `connect`/`render` did not; `log.interrupted_step` (the name backend-erp's `_finish` keeps after a crash) ran; `__session__` never counts |
| `append_rollback(db, run, items=None) -> [entry]` | The one builder of ROLLBACK entries (doc 42 §7.6): per CONFIGURATION child, reverse `run_position`, with a non-empty `ran_steps`, one entry `{…, phase: "ROLLBACK", steps: [{name, label, skip?}], ran_steps}` from the snapshot's `rollback` (a step whose `undoes` did not run is pre-marked `skip: true`; a device whose every step would be skipped gets no entry). A device that changed something (not only simulator/ping) but has no `rollback` is recorded `NO_ROLLBACK_DEFINED` in `frames["rollback"]`. `plan` is reassigned (the forward plan stays a stable prefix). `items` limits it to those item ids |
| `revert_run(db, run) -> ProvisioningJob \| None` | "Revertir" (doc 42 §7.6): `RunNotRevertible` (409 `RUN_NOT_REVERTIBLE`, `.reason`) unless the run is non-dry, SUCCEEDED, v2 (`phase` set), with no other in-flight run on the service and no later non-dry run. Then `error_code = REVERTED`, RUNNING, phase ROLLBACK, `append_rollback`, first entry queued — or closed at once (ROLLED_BACK, or ROLLBACK_INCOMPLETE for a `NO_ROLLBACK_DEFINED` device) when nothing needs undoing |
| `retry_rollback(db, run) -> ProvisioningJob \| None` | "Reintentar reversión": only a non-dry v2 FAILED/`ROLLBACK_INCOMPLETE` run with no in-flight run on its key. Re-appends rollback entries for the items whose latest ROLLBACK child did not succeed (or never ran), restores the original cause into `error_code`/`error`, and queues the first; a success ends ROLLED_BACK with that cause. `NO_ROLLBACK_DEFINED` devices are not retried |
| `decrypt_run_secrets(run) -> {key: value}` | Decrypts the run's generated secrets (`{}` when none). Never log or persist the result |
| `repair_stranded_runs(db, limit=100) -> int` | Backstop, called by the worker's reaper: an in-flight run quiet for > 30 s (`STRANDED_RUN_GRACE`) with **no in-flight child**, taken `FOR UPDATE SKIP LOCKED`. Per run, in its own savepoint, it re-reads the last child: still in flight → skip; none → queue child 0 (an empty plan goes straight to SUCCEEDED); terminal → `advance_run`. Quiet longer than `STRANDED_RUN_MAX_AGE` (1 h, from the last child's `finished_at`, else the run's `updated_at`) it is **phase-aware** (doc 42 §8.4): PRECONDITIONS, a legacy or a dry run → closed FAILED/`STRANDED_RUN_EXPIRED`; CONFIGURATION/VERIFICATION → **enters ROLLBACK** with `STRANDED_RUN_EXPIRED` (the safe direction); ROLLBACK → keeps advancing (closed `ROLLBACK_INCOMPLETE` if it has no child). Returns the number repaired; the caller commits |

Callers (backend-erp, provisioning-concurrency fix): `POST /client-services/{id}/provision`
and the lifecycle hooks (`services/client_service_lifecycle.enqueue_lifecycle_run`)
both open runs through `create_or_get_run` with `run_idempotency_key`
(`/provision` answers `created=False` with 409 `PROVISIONING_ALREADY_QUEUED` +
`run_id`; the lifecycle hook returns the winner's id); the workflow engine's
ENQUEUE_PROVISIONING does too. `POST /provisioning/jobs/{id}/cancel` expires the
CAS-cancelled child before `advance_run`, because `advance_run` no-ops while the
job still looks in flight; `POST /provisioning/jobs/{id}/retry` refuses a run
child (409 `RUN_CHILD_NOT_RETRYABLE`): `advance_run` never resurrects a terminal
run, so a re-queued child would execute outside it. The real-Postgres races
(claim, fence, journal, reaper, producers) are covered by backend-erp's
`tests/pg/test_provisioning_concurrency.py`; this repo's
`tests/pg/test_provisioning_runs_pg.py` covers the module itself.

Child jobs derive their idempotency key as `{run_key}#{position}` (so each child
is still individually unique under `uq_provisioning_job_company_idem`), are
inserted with **`device_lock_key` NULL**, and get their `variables` from
`shared | device[item_id]`. **Producers never lock:** only backend-erp's worker
claim writes the device lock (never for a dry run). Writing it at INSERT made the
commit that recorded child N's outcome fail whenever child N+1's device was busy
with another run or a probe (the 2026-10-06 incident); now a busy device just
means the worker's claim skips that child until the device frees.

**What this module does not do:** it does not evaluate the provisioning gates
(kill switch, dry-run gate). Those live in backend-erp and are called by its
routers before `create_run`, exactly as they are today — and the workflow-engine
path still does not call them (see [limitations.md](limitations.md)).

### `transport.py` (transport resolution, 2026-08-13; transport axis `tr1_transport_axis`, 2026-09-26)

The one place that turns an `InventoryItem` plus its tenant's
`ProvisioningSettings` row into the address a driver actually dials. Lives here
(not in backend-erp) so `cli.py`, `ping.py`, and any future TCP driver share one
implementation instead of three drifting copies.

The configuration is **two orthogonal fields**, not one cross-product enum
(`tr1_transport_axis` replaced `network_access.mode` with them):

```
dial_target   'device' | 'gateway'    whose address do we dial
proxy_kind    'none'   | 'socks5'     is there a hop, and of what sort
```

| Name | Behaviour |
|---|---|
| `ResolvedEndpoint` | Frozen dataclass: `host`, `port`, `proxy` (SOCKS5 `host:port` when `proxy_kind='socks5'`, else `None`), `dial_target` (`'device'`/`'gateway'`) |
| `company_provisioning_settings(db, company_id)` | The tenant's `provisioning_settings` singleton, or `None` when it has never been created (canon C6 — absence means provisioning DISABLED). **Public on purpose**, and the successor to `default_outbound_access`: backend-erp's `cli.py` driver and the provisioning worker each carried a byte-identical private copy of the old `network_access` lookup, each docstring claiming to be the canonical one; they import this instead |
| `resolve_endpoint(db, item, company_id, default_port, settings=None)` | Returns `(endpoint, None)` or `(None, error_code)`. Reads the company's one `provisioning_settings` row (or the caller-supplied `settings`) — no longest-prefix match and no per-device override. The multi-row, per-CIDR `mgmt_subnets` resolver the old table existed for was never implemented and is **abandoned, not deferred** |

Resolution is the three lines in the module docstring:

```
host  = gateway_host if dial_target == 'gateway' else item.mgmt_host
port  = item.nat_port if dial_target == 'gateway' else (item.mgmt_port or default_port)
proxy = proxy_address if proxy_kind == 'socks5' else None
```

- `dial_target == 'gateway'`: the target is always `(settings.gateway_host, item.nat_port)`, **never** `item.mgmt_host`. A missing `gateway_host` or `nat_port` → `NAT_MAPPING_NOT_SET`.
- `dial_target == 'device'`: `(item.mgmt_host, item.mgmt_port or default_port)`. A blank `mgmt_host` → `MGMT_HOST_NOT_SET`.
- `proxy_kind == 'socks5'`: `settings.proxy_address` is the hop, on either dial target. Blank/NULL → `PROXY_NOT_PROVISIONED`. The hub TECHNOLOGY is not recorded and is none of the resolver's business — a Railway-internal ZeroTier/Pylon proxy, an external WireGuard-hub VPS and a future Tailscale exit node are the same thing here, which is why the old `PYLON_NOT_PROVISIONED`/`VPN_NOT_PROVISIONED` pair collapsed into one code.
- **No `provisioning_settings` row** → `device` + `none`, the legitimate public-IP case and the pre-existing default. Not a bypass: a tenant with no row also has provisioning DISABLED (canon C6), so no job reaches a driver.
- `settings` supplied by the caller (skipping the internal query) is rejected with `TRANSPORT_UNAVAILABLE` if `settings.company_id != company_id` — a cross-tenant guard, since nothing else here re-validates a caller-supplied row.

The four combinations, and the operator scenario each is:

| `dial_target` | `proxy_kind` | scenario | old `mode` |
|---|---|---|---|
| `device` | `none` | the devices have public IPs | `direct` |
| `gateway` | `none` | NAT + port map to a public IP | `nat_public` |
| `gateway` | `socks5` | NAT + port map reached via ZeroTier | `nat_zt` |
| `device` | `socks5` | a hub with managed routes into the LAN: WireGuard, ZeroTier or any other | `vpn` — and the ZeroTier variant of this row had **no** `mode` value at all, which is why the axis was split |

Error-code vocabulary `resolve_endpoint` can return (spec N12):

| Code | When |
|---|---|
| `NAT_MAPPING_NOT_SET` | `dial_target == 'gateway'` and `gateway_host` or `item.nat_port` is missing |
| `PROXY_NOT_PROVISIONED` | `proxy_kind == 'socks5'` and `proxy_address` is blank/NULL (replaces both `PYLON_NOT_PROVISIONED` and `VPN_NOT_PROVISIONED`) |
| `MGMT_HOST_NOT_SET` | `dial_target == 'device'` and `item.mgmt_host` is empty |
| `TRANSPORT_UNAVAILABLE` | a caller-supplied `settings` row belongs to a different `company_id` |

Three invariants documented in the module docstring:

1. The `InventoryItem` is **never mutated** — `mgmt_host`/`mgmt_port` always
   describe the device, never the path to it (doc 34 §1.3).
2. It **fails closed** (doc 34 canon R23, rewritten). `proxy_kind` is
   LOAD-BEARING here and deliberately not collapsed into
   `proxy_address IS NOT NULL`: `device` with no proxy is the legitimate public-IP
   case, so without a stored intent the resolver could not tell "no hop needed"
   from "a hub is intended but its address is missing", and the second would
   silently dial an RFC1918 address from the Railway container. A blank
   `proxy_address` under `socks5` is a HARD ERROR, never a fallthrough.
   `ck_provisioning_settings_proxy_address` only demands NOT NULL, so `''`
   commits and `PROXY_NOT_PROVISIONED` stays reachable.
3. Absence of a settings row is a defined state, not an accident (point 4 above).

What the resolver still cannot know is whether a hub's route actually reaches
`mgmt_host`: a populated `proxy_address` and a populated `mgmt_host` resolve
successfully either way, and only the driver's connect attempt settles it.
Callers must surface any returned error code as a step failure. See
`tests/test_transport_resolver.py` and `tests/test_transport_axis.py`.

## `playbook_expr.py` (doc 40 §3.3.3)

A playbook may declare `computed: [{"key", "expr", "min"?, "max"?}]`
(`schemas/playbook.py` `ComputedVar`); templates read the results by plain
lookup as `{{computed.<key>}}`. The renderer stays a dictionary lookup.

- **Grammar:** `expr := term (("+"|"-") term)*`, `term := unary (("*"|"/"|"%") unary)*`,
  `unary := "-" unary | atom`, `atom := INT | NAME | "(" expr ")"`, `INT` 1–9 digits,
  `NAME` = a namespace in {`device`, `cpe`, `path`, `service_plan`, `client`,
  `service`, `computed`} plus dotted segments. `input.*` is not an operand (the
  resolver cannot see author variables). A hand-written tokenizer and recursive
  descent return tuples; no `eval`, `ast`, `compile` or `format`
  (`test_no_dynamic_evaluation_in_the_source`).
- **Limits:** ≤ 16 entries, ≤ 256 characters, ≤ 64 tokens, paren depth ≤ 8; keys
  `^[a-z][a-z0-9_]{0,31}$`, unique, not secret-named; an entry reads only earlier
  `computed.*` keys.
- **Semantics:** operands are `int` (not `bool`) or strings that `fullmatch`
  `-?[0-9]{1,9}`; `/` truncates toward zero and `%` is `a − b·trunc(a/b)`, so the
  TypeScript mirror (`frontend-erp/lib/playbookExpr.ts`) is exact; every operand,
  intermediate and result satisfies |x| ≤ 2³¹−1.
- **API:** `parse(expr)` → tuple tree (raises `ExprError(code, detail)`, a
  `ValueError`), `names(tree)`, `evaluate_all(computed, variables) -> (values,
  missing, errors)` with `values = {"computed.<key>": int}`, `missing` = operand
  names absent/None, `errors = [{"code", "key", "detail"}]`. An entry with a
  missing operand, or reading an earlier failed entry, is skipped without a second
  error. Codes: `COMPUTE_SYNTAX`, `COMPUTE_LIMIT`, `COMPUTE_NAME`,
  `COMPUTE_SECRET`, `COMPUTE_TYPE`, `COMPUTE_OVERFLOW`, `COMPUTE_DIV_ZERO`,
  `COMPUTE_RANGE`.
  `evaluate_all` never raises on a malformed **stored** block (4.5.2, review F3):
  a non-list block, or an entry without string `key`/`expr`, is `COMPUTE_SYNTAX`;
  a non-int `min`/`max` is `COMPUTE_TYPE`.
- **Pinned by** the hash-locked `tests/fixtures/playbook_expr.json` (copied to
  `frontend-erp/lib/__fixtures__/`): changing it means changing both
  implementations and both hash pins.

## Related packages

| Path | Purpose |
|---|---|
| `dependencies/db.py` | `get_db` FastAPI session dependency (rollback + close) |
| `dependencies/audit.py` | `get_client_ip` (proxy-aware) |
| `middleware/logging_middleware.py` | `LoggingMiddleware` — request-ID + JWT-context + duration ASGI middleware |
| `constants/roles.py` | `Roles` ADMIN/VIEWER/COLLECTOR/TECHNICIAN |

## Connections to Other Components

- **auth-erp** and **backend-erp** import these utilities directly
- **JWT utilities**: auth-erp issues tokens; both backends validate with the
  shared `SECRET_KEY`
- **Workflow engine**: fired by backend-erp after CRM/ISP entity mutations
- **Audit utilities**: called by mutation endpoints in both services

## Environment Variables

- `SECRET_KEY`, `ENVIRONMENT`, `ACCESS_TOKEN_EXPIRE` (min), `REFRESH_TOKEN_EXPIRE` (s), `MOBILE_ACCESS_TOKEN_EXPIRE` (min) — `jwt_utils.py`
- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — anything touching the DB (via `database.py`)
- `EMAIL_PROVIDER`, `SMTP_USE_TLS` — email service (see [email-service.md](email-service.md))
