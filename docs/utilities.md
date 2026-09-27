# Utilities

## Description

Shared utility modules in `database_utils/utils/` (24 modules) plus the
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
| `provisioning_runs.py` | Opens and advances a multi-device `ProvisioningRun` (Cycle 10, doc 35 §5) — see below |
| `transport.py` | Transport resolver, `resolve_endpoint()` + `default_outbound_access()` (2026-08-13, doc 34 canon R23 rewrite; `vpn` branch 2026-09-25) — see below |
| `jwt_utils.py` | HS256 JWT create/decode. Env: `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE` (minutes, default 1440), `REFRESH_TOKEN_EXPIRE`. **Fails fast if `SECRET_KEY` is unset when `ENVIRONMENT=production`**; dev fallback otherwise |
| `permission_utils.py` | `PermissionChecker` and require-permission FastAPI dependencies |
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
  `None`). `position` is a **fact about the resolved path, never an addressing
  mechanism** — nothing templates it.
- `ResolvedProvisioning` — `path` (every node, passives included, so an operator
  can see that a splitter was considered and deliberately skipped rather than
  wondering where it went), `steps` (the subset that will be configured),
  `shared_variables`, `device_variables` (`item_id → that node's device.* frame`).

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
| `service_plan.<field\|param>` | plan fields plus the plan's tenant-authored rows (`plan`- and `service`-scoped alike — the author writes `{{service_plan.<key>}}` either way) |
| `client.<attr>` | built-in subscriber fields plus the tenant's own client custom fields (built-ins win a clash) |
| `service.<attr>` | the `client_service` itself |
| `input.<key>` | author-declared playbook variables; the namespace is applied at REFERENCE time by `input_key()`, the declared key stays bare |

`DEVICE_ATTRIBUTES = ("item_id", "serial", "mac", "type", "category",
"category_tier", "mgmt_host", "mgmt_port", "depth")` — one tuple shared by all
three device namespaces, built by `build_device_frame(node, prefix)`. `depth` is
hops from the CPE (`cpe.depth == 0`).

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
> `| filter` suffix (`_FILTER_SUFFIX`). A namespace that is emitted but not
> listed in the pattern does not raise, does not warn, and does not fail a test
> that is not looking for it — it quietly turns a hard resolution error into a
> partial run that half-configures a paying customer. Add a namespace here in the
> same commit you add it anywhere else;
> `test_device_variable_pattern_matches_the_new_namespaces` exists solely to
> catch that omission.

### `provisioning_runs.py`

| Function | Behaviour |
|---|---|
| `run_idempotency_key(client_service_id, purpose, dry_run)` | `path-{service_id}-{purpose_lower}[-dry]` — deliberately mirrors the shape backend-erp's manual endpoint has always used, so a run and a legacy standalone job never collide in the same namespace |
| `find_in_flight_run(db, company_id, key)` | Dedupe lookup over `IN_FLIGHT = (QUEUED, RUNNING, PENDING_INFORM)`. That tuple **must** mirror the predicate on `uq_provisioning_run_company_idem`; if they disagree, the dedupe check and the unique index disagree and one of them starts raising `IntegrityError` |
| `create_run(db, client_service, purpose, dry_run, triggered_by, ..., resolution=None)` | Resolves (or accepts an already-resolved `ResolvedProvisioning`, which the manual endpoint passes so it can 422 with the error list before touching anything), snapshots `path`/`plan`/`frames`, opens the run, and queues **only its first child**. Resolution happens exactly once per run |
| `advance_run(db, job) -> ProvisioningJob \| None` | Called when a job reaches a terminal state. A standalone job (`run_id` NULL) is a **no-op** — ACS reboots and connectivity probes must keep behaving exactly as they did. Any non-SUCCEEDED status stops the run and the run takes that status (continuing to the OLT after the CPE step failed would leave the network configured for a subscriber whose own device is not). On success it queues the next child; when the plan is exhausted the run goes SUCCEEDED, stamps `finished_at`, and — for a non-dry-run ACTIVATION only — clears `client_service.path_changed_at` |

Child jobs derive their idempotency key as `{run_key}#{position}` (so each child
is still individually unique under `uq_provisioning_job_company_idem`), carry
`device_lock_key = "{company_id}:item:{item_id}"`, and get their `variables` from
`shared | device[item_id]`.

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

## Related packages

| Path | Purpose |
|---|---|
| `dependencies/db.py` | `get_db` FastAPI session dependency (rollback + close) |
| `dependencies/audit.py` | `get_client_ip` (proxy-aware) |
| `middleware/logging_middleware.py` | `LoggingMiddleware` — request-ID + JWT-context + duration ASGI middleware |
| `constants/roles.py` | `Roles` ADMIN/MANAGER/SALES/USER |

## Connections to Other Components

- **auth-erp** and **backend-erp** import these utilities directly
- **JWT utilities**: auth-erp issues tokens; both backends validate with the
  shared `SECRET_KEY`
- **Workflow engine**: fired by backend-erp after CRM/ISP entity mutations
- **Audit utilities**: called by mutation endpoints in both services

## Environment Variables

- `SECRET_KEY`, `ENVIRONMENT`, `ACCESS_TOKEN_EXPIRE`, `REFRESH_TOKEN_EXPIRE` — `jwt_utils.py`
- `POSTGRES_*` / `DATABASE_URL` / `DB_URL` — anything touching the DB (via `database.py`)
- `EMAIL_PROVIDER`, `SMTP_USE_TLS` — email service (see [email-service.md](email-service.md))
