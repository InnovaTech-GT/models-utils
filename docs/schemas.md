# Pydantic Schemas

## Description

Pydantic v2 request/response schemas for all models — 37 modules in
`database_utils/schemas/`, shared between auth-erp and backend-erp to keep API
contracts consistent (frontend-erp consumes the resulting JSON shapes via the
backend proxies).

## Goal

Validate API inputs and serialize API outputs with a single shared schema
definition across services.

## Schema Modules (in `database_utils/schemas/`)

One module per entity. `schemas/__init__.py` star-imports all modules and runs
`model_rebuild()` to resolve circular Order/billing_due forward references.

| Domain | Modules |
|---|---|
| Auth / tenancy | `user`, `company`, `role`, `permission`, `invitation`, `notification`, `audit_log`, `requests` (Login + flat company-only Signup), `email_verification`, `password_reset` |
| SaaS billing | `tier`, `subscription`, `payment_method`, `billing_invoice` — rb1 extends `tier` and `subscription` (see below) |
| CRM | `client`, `custom_field`, `order`, `order_item`, `payment`, `invoice`, `billing_due` (cron due-billing + generation/gap DTOs), `task`, `task_template`, `integration` |
| ISP | `service_plan`, `client_service`, `inventory`, `playbook`, `device_category`, `insight` (Cycle 4; v2 since 1.33.0) |
| Network config (Cycle 5) | `acs_registration`, `device_credential`, `provisioning_settings` (the transport axis + ACS config live here since `tr1_transport_axis`) |
| Workflow | `workflow` |
| Generic | `pagination` — `PaginatedResponse[T]` wrapper |

Two modules have been deleted over the life of this repo, and the distinction
matters when reading `__init__.py`:

- `schemas/network.py` — deleted with the free-form network-graph removal
- `schemas/network_access.py` — deleted by `tr1_transport_axis` with the
  `network_access` table; its two surviving fields (`acs_base_url`,
  `acs_auth_required`) are on `schemas/provisioning_settings.py`
  (Cycle 2 `c2d_graph_removal`); a comment in `__init__.py` still records it.
- `schemas/topology.py` — deleted in **Cycle 10** (doc 35) together with the
  `Topology` / `TopologyDeviceType` / `TopologyPlaybook` models. Its one
  still-needed export, **`normalize_purpose`**, moved to
  [`schemas/playbook.py`](#cycle-10-network-graph-doc-35--schema-changes) —
  purposes now key playbook *bindings*, not topologies.

## Connections to Other Components

- **auth-erp** and **backend-erp** import schemas directly from this package
- **Models** ([auth-models.md](auth-models.md), [crm-models.md](crm-models.md),
  [isp-models.md](isp-models.md), [workflow-models.md](workflow-models.md)):
  schemas mirror model fields
- **frontend-erp**: no direct dependency; its API responses are shaped by
  these schemas via the backends

## Key Implementation Details

- `Out` schemas use `from_attributes=True` for ORM compatibility
- Sensitive fields are excluded from `Out` schemas (e.g. `password_hash`);
  integration `credentials` are returned masked (`IntegrationOut.from_orm_masked`,
  `api_key`/`token`/`password` → `***`)
- `integration` (fg1): `IntegrationCreate` gains `provider:
  Optional[Literal["WHATSAPP_BUSINESS"]] = None` and `enabled: bool = True`;
  `IntegrationUpdate` gains both as Optional (None = unchanged — so `provider`
  can't be cleared via PATCH); `IntegrationOut` exposes `provider`/`enabled`
- `PaginatedResponse[T]`: generic paginated wrapper
- `order_item.product_id` is gone (`ld1_legacy_drop`); `OrderItemBase.service_plan_id`
  is required
- UUID fields serialize as strings in JSON responses

### Cycle 7 (core config, doc 25) — extensions to existing modules

- `device_category`: `tier` on Base/Update with a normalizing validator
  (strip/upper, must be in `DEVICE_CATEGORY_TIERS`, empty → None) so the DB
  CHECK never fires as a raw 500
- `inventory`: `DeviceType*.cli_platform` (free string); `InventoryItem*`
  mgmt surface — `mgmt_host`/`mgmt_port`/`cli_protocol` (normalizing validator
  against `CLI_PROTOCOLS`) PATCHable via the existing inventory update; the
  worker-stamped `mgmt_last_check_at`/`mgmt_last_check_ok` appear **only** on
  `InventoryItemOut` (read-only)
- ~~`topology`~~: the pinned-chain write shapes (`TopologyChainEntryIn/Out`,
  `TopologyCreate`/`TopologyUpdate`) went with the module in Cycle 10
- `playbook`: `PLAYBOOK_DRIVERS` gains `ping`; `PlaybookStep.target_item_id`
  (and the same on `PlaybookPrecondition`) — an inventory_item id or a
  `{{variable}}` rendered by the executor, declared so it round-trips through
  `model_dump()` instead of being silently dropped
- `client_service`: `ClientServiceOut.install_state`/`installed_at` — read-only
  (deliberately absent from `ClientServiceUpdate`; written only by backend-erp's
  `recompute_install_state`)

### Brownfield adoption (doc 30) — `client_service` schema changes

- `ClientServiceOut` gains `adopted_at`/`adopted_by_user_id`/`adoption_note`
  (Out-only, never on Create/Update — `migration_source` precedent) and
  `activation_evidence` (Out-only, backend-COMPUTED — not a DB column; values
  from `isp.ACTIVATION_EVIDENCE_VALUES`: `'provisioned'` | `'attested'` |
  `None`; populated only on list/detail/adopt/un-adopt responses — `None`
  elsewhere means "not computed", not "no evidence")
- `ClientServiceAdoptIn` — `POST /client-services/{id}/adopt` body: `note`
  required non-empty (stripping validator), `installed_at` optional historical
  install date (applied only while the service's `installed_at` is NULL). The
  service-lifecycle cycle's `topology_id` field was removed again in Cycle 10 —
  where the CPE sits in the plant is stated by attaching the node, not by the
  attestation. Inherited by `ClientServiceAdoptBulkItem`, so the bulk campaign
  path accepts the same shape
- `ClientServiceAdoptBulkItem` (AdoptIn + `client_service_id`) and
  `ClientServiceAdoptBulkIn` (`items`, 1–500) — `POST /client-services/adopt-bulk`
  body
- `ClientServiceAdoptBulkRowResult` (`status` `'adopted'`|`'error'`; `error`
  `'NOT_FOUND'`|`'ALREADY_ADOPTED'`) and `ClientServiceAdoptBulkOut`
  (`results` + `adopted_count`/`error_count`) — the bulk response (per-row,
  never all-or-nothing)

### Client codes and payment day (4.5.0 `cc1`, 4.2.0 `pd1`) — `client` schema changes

- `ClientCreate.code` / `ClientUpdate.code` (`Optional[str]`): trimmed and
  uppercased by `utils/client_code.normalize_client_code` (`^[A-Z0-9-]{1,16}$`,
  else `ValueError`); `None`/blank means "generate one" on create and "leave
  unchanged" on update. `ClientOut.code` is typed optional but the column is NOT
  NULL, so a read always carries it. Per-company uniqueness
  (case-insensitive) is the DB's `uq_client_company_code`; callers retry on it.
- `ClientBase`/`ClientUpdate.payment_day`: optional, 1..31.

### Client install-field removal (doc 31) — `client` schema changes

- `ClientBase`/`ClientUpdate` (and thus `ClientCreate`/`ClientOut`) drop
  `installation_status`/`installation_date` — the columns and the
  `InstallationStatus` enum were removed by `cf1_drop_client_install_fields`
  (install truth is `client_service.install_state`)
- `ClientOut` gains `services_total`/`services_installed` (`int`, default
  `0`) — the services-summary rollup, Out-only and backend-COMPUTED by
  backend-erp's clients list/detail endpoints from `client_service` rows
  (`install_state='INSTALLED'` for the second count); never stored, never on
  Create/Update (`activation_evidence` precedent)

### Recurrente tenant billing (rb1) — `tier` / `subscription` schema changes

- `SubscriptionOut` gains `recurrente_subscription_id`/`card_last4`/`card_brand`
  (Optional, mirror the rb1 columns)
- `TierOut` gains `recurrente_product_id`/`recurrente_price_id`/
  `recurrente_price_yearly_id` (Optional — admin-facing)
- `TierPublic` gains `purchasable: bool = False` — stamped by the endpoint from
  `recurrente_price_id` presence; the raw price id is never exposed publicly

### Cycle 8 (network UX, doc 26) — `playbook` schema changes

- `PlaybookBase` drops `target_vendor` and `target_category`; `PlaybookCreate`
  drops them from its optional overrides too. **This half stands.**
- `PlaybookOut` drops `target_category_id`. Cycle 8 also added
  `topology_id: Optional[UUID]` and `PlaybookStep.target_position`; **both were
  removed again in Cycle 10** — see below.
- `PlaybookDefinition` is otherwise unchanged (steps still carry `target_item_id`)

### Cycle 10 (network graph, doc 35) — schema changes

`schemas/topology.py` is **deleted** along with its models.

`schemas/playbook.py`:

- **hosts `normalize_purpose(v)`** now — strip → upper → replace `' '`/`'-'`
  with `'_'` → regex-validate against `PLAYBOOK_PURPOSE_PATTERN` (renamed from
  `TOPOLOGY_PURPOSE_PATTERN`, `models/isp.py`). Deliberately module-level and
  importable: the provision endpoint body schema
  (`ClientServiceProvisionIn`), the binding endpoints and the engine's
  `ENQUEUE_PROVISIONING` config path all share this exact normalization, so a
  tenant typing `'Activation'` or `'activation '` always matches the seeded
  `ACTIVATION` binding.
- `PlaybookBase` carries **no ownership field at all** — a playbook row is again
  a plain company-scoped library entry. `topology_id` is gone from `PlaybookOut`;
  ownership lives in the `device_type_playbook` / `inventory_item_playbook`
  tables ([network-models.md](network-models.md)).
- `PlaybookStep.target_position` is **deleted**. A playbook binds to one device
  type and therefore runs on exactly one device, so there is no chain slot left
  to address: the executor defaults the step target to `{{device.item_id}}` and
  `target_item_id` remains the power-user override.

`schemas/client_service.py`:

- `ClientServiceBase` drops `topology_id` and gains the **two network inputs**:
  - `cpe_item_id: Optional[UUID]` — the subscriber's edge device. Nullable,
    because a brownfield service attested from the field legitimately has no
    equipment record.
  - `cpe_parent_id: Optional[UUID]` — **write-only**. It attaches the CPE under
    that node in the same request so the two inputs land together or not at all,
    but it is a property of the *item*, not of the service, and is never echoed
    back on `ClientServiceOut`.
- `ClientServiceUpdate` swaps `topology_id` for the same two fields.
- `ClientServiceOut` gains `path_changed_at: Optional[datetime]` —
  machine-written, never accepted on an Update schema.
- `ClientServiceAdoptIn` **drops `topology_id`**: attestation records that a
  service was *already installed*, while where its CPE sits in the plant is a
  separate physical fact stated by attaching the node.

> **Known drift — two vestigial topology surfaces remain.**
> `ServicePlanBase`/`ServicePlanUpdate.default_topology_id` is still declared in
> `schemas/service_plan.py`, and `utils/workflow_fields.py` still lists
> `client_service.topology_id` (`fk_to: "topology"`) as a trigger-context field.
> The backing **column and table are gone** (`ng2_topology_drop`), so the first
> is an accepted-but-ignored request field that can never round-trip and the
> second is a trigger field that can never match. Nothing reads them; they are
> inert rather than dangerous, but they are not intended and should be removed in
> a follow-up. Recorded here so the wiki does not claim a cleanliness the code
> does not have.
>
> No Pydantic schema exists for `ProvisioningRun` — backend-erp shapes the
> `/automations/runs` response itself.

### Port-level topology (4.4.0, doc 40, revision `pt1_port_topology`)

`schemas/inventory.py`:

- `PortTemplateGroup` (`name`, `slots?`, `start` = 1, `count`, `medium`,
  `direction`), `PortSpec`, `expand_port_template(groups)` (accepts models or
  the raw stored dicts), `validate_port_template` (list rules; `[]` → `None`).
  See [network-models.md](network-models.md) for every limit.
- `DeviceTypeCreate`/`DeviceTypeUpdate`/`DeviceTypeOut` gain `port_template` and
  `path_role`. A template on a lot type is a 422
  `PORT_TEMPLATE_REQUIRES_SERIALIZED` (on Update only when both fields are sent;
  the backend checks a lone template against the stored flag). `path_role` goes
  through `normalize_path_role` (strip, blank → `None`, `PATH_ROLE_PATTERN`, not
  secret-named); `path_role_shadows_category(db, role)` is the DB check behind
  the backend's `PATH_ROLE_SHADOWS_CATEGORY`. On Update an explicit `null`
  clears the template (`model_fields_set`).
- No port/link response schemas here: backend-erp owns `PortOut`/`UplinkOut`
  (`schemas/network_graph.py`).

`schemas/playbook.py`:

- `ComputedVar` (`key`, `expr`, `min?`, `max?` as strict ints) and
  `PlaybookDefinition.computed: List[ComputedVar] = []`. Declared because the
  library routes store `model_dump()`, which drops unknown keys. The validator
  parses every `expr` (`utils/playbook_expr.py`), refuses secret-named keys and
  operands, `input.*`, forward/self references, more than 16 entries, and any
  `{{computed.x}}` token (templates, requests, preconditions, `on_failure`,
  rollback) whose key is not declared — matched on each raw step string's
  token bodies with the renderer's `[ \t]*` head rule, never on a JSON dump
  (which escapes a tab).
  Since 4.5.2 (review F1) a `computed` token must be exactly
  `{{computed.<key>}}` (plus filters): `{{computed.onu.y}}` or
  `{{computed[0].x}}` is refused with `COMPUTE_NAME`, because `evaluate_all`
  never produces such a name and only a caller-supplied value could fill it.

### Engine v2 (6.1.0, doc 42, revision `pe1_playbook_phases`) — `playbook` format

`schemas/playbook.py` — one playbook is still one purpose on one device type;
the definition now holds the phases at the top level (the founder's layout):

| Key | Meaning |
|---|---|
| `variables`, `computed` | unchanged (`input.*`, `computed.*`) |
| `session` | `PlaybookSession`, ssh/telnet only: `enable` (`command` "enable", `password_prompt` "ssword", `enabled_prompt` "#" — the password is a `CLI_ENABLE` credential), `config_command` (required by a `config_mode` step), `exit_command` "exit", `error_patterns` (None = platform default in backend-erp; a match = `COMMAND_REJECTED`), `busy_patterns` (a match = `DEVICE_BUSY`) |
| `secrets` | `[PlaybookSecret{key, length 8..63 (12)}]`, generated once per RUN, read as `{{secret.<key>}}` |
| `preconditions` / `configuration` / `verification` / `rollback` | `[PlaybookStep]`; `configuration` ≥ 1 step; names unique across all four; `__session__` reserved |
| `outputs` | `[PlaybookOutput{key, label, value, unit?, audience ⊆ {technician, office}, shareable, sensitive}]`, ≤ 16 |

`PlaybookStep` gains `label` (≤ 80, static) / `hint` (≤ 200, static),
`idempotent` (was silently dropped — bug §3.2.2), `config_mode`, `capture`
(`PlaybookCapture{key, regex with exactly one group, type number|text, label,
unit, min/max (number, literal or ONE token), equals (text)}`, ≤ 8 per step,
≤ 32 per playbook, never secret-named), `wait_until`
(`PlaybookWaitUntil{tries 2..30, interval_seconds 1..60}`, tries × interval
≤ 600) and `undoes`. `on_failure` is **retired**. `PlaybookStepValidation` gains
`expect_regex` / `expect_not_regex` (every string is rendered before
comparing).

**Save-time rules** (`PlaybookDefinition.validate_definition`; the error code is
the prefix of the message): `PHASE_FIELD_NOT_ALLOWED` (`undoes` outside
rollback, `capture` in rollback, `wait_until` outside preconditions/verification
except on a tr069 configuration step, `config_mode` outside ssh/telnet
configuration/rollback, the step guard `precondition` and `idempotent` outside
configuration), `CONFIG_COMMAND_REQUIRED`, `UNDOES_UNKNOWN_STEP`,
`CAPTURE_UNDECLARED` (a step reads only captures of EARLIER steps, in
preconditions → configuration → verification order; rollback and outputs may
read any), `CAPTURE_SECRET_NAME`, `REGEX_UNSUPPORTED` (`check_regex`: ≤ 256
chars, no lookaround, backreference or named group, compiled with tokens as a
literal; escaped text such as `\(?=` or `\\1` is not mistaken for a
construct; a leading `(?i)`/`(?m)`/`(?s)` is fine — backend-erp's `re2.compile`
is the authority), `SECRET_UNDECLARED`, `OUTPUT_SECRET_MIXED` (a secret output
is exactly `{{secret.<key>}}`; it is always `sensitive`, refused as false),
`OUTPUT_SHARE_AUDIENCE` (`shareable` needs `technician`), and the `computed`
checks across every phase, validation/threshold strings and output values.
No `extra="forbid"` (the editor round-trips unmodelled keys).

**Legacy shape.** `normalize_definition(d)` (a `mode="before"` validator, so
every save stores v2; also used by every reader) converts `{steps, rollback}`:
`configuration = steps` (on_failure stripped); per-step `on_failure` becomes
rollback steps with `undoes` in reverse order, else the legacy `rollback` is
kept without `undoes`; rollback names that collide get a ` (rollback)` suffix.
Precedence: `configuration` wins over an empty or identical `steps` (the
mirror round-tripping); differing `steps` next to `configuration` is
`LEGACY_STEPS_CONFLICT`. `PlaybookDefinition.steps` is never stored;
`PlaybookOut.definition` is `PlaybookDefinitionOut`, which returns a read-only
`steps` mirror (= `configuration`) for the pre-doc-48 editor (removed in `pe2`).

Helpers: `job_steps(definition, phase, probe=False)` (a phase's list, the
`__session__` probe prepended; `phase=None` = standalone, preconditions +
configuration + verification flattened), `shared_device_wait_errors(definition)`
(`WAIT_TOO_LONG_FOR_SHARED_DEVICE` above 120 s — the router calls it for a
non-CPE binding), `playbook_warnings(definition, category_tier=, purpose=)`
(`ROLLBACK_EMPTY`, `ROLLBACK_WITHOUT_UNDOES`, `ENABLE_WITHOUT_SESSION`,
`NOT_RESEND_SAFE`, `CPE_NETWORK_PRECONDITION` — warnings, never errors),
`is_resend_safe(step)`, `output_secret_ref(value)`; constants `PHASE_KEYS`,
`SESSION_PROBE_STEP`, `SECRET_ALPHABET`, `SHARED_DEVICE_WAIT_MAX_SECONDS`.

### Insights v2 (1.33.0, revision `iv1_insights_v2`) — `insight` schema changes

- **`InsightChartSpec` is deleted.** Chart `spec` is an opaque `Dict[str, Any]`.
  backend-erp owns query-spec v2 (`insights/spec.py`), validates it on every
  write, and stores the normalized dump. Reads never re-validate, so a stale
  spec cannot 500 a dashboard GET.
- `viz: Optional[Dict[str, Any]]` on `InsightChartBase`/`InsightChartUpdate`,
  and `default_time_range: Optional[Dict[str, Any]]` on
  `InsightDashboardBase`/`InsightDashboardUpdate`. Both are opaque, and
  backend-erp validates them.
- `ordering` left `InsightChartBase`. It is `Optional[int] = None` on
  `InsightChartCreate` (None means the backend assigns `max+1`, or the list index
  for inline charts), optional on `InsightChartUpdate`, and a required `int` on
  `InsightChartOut`.
- PATCH semantics depend on `exclude_unset`: an explicit `viz: null` or
  `default_time_range: null` is distinguishable from "not sent", and clears
  the value. Pinned by `tests/test_insight_schemas_v2.py`.
- The read wrappers that add `accessible` (`InsightChartView`,
  `InsightDashboardView`) live in backend-erp, not here.

### Mobile integration (mi1/mi2, 2026-09-29)

- `payment.py`: `PaymentCreate` / `FullPaymentCreate` gain
  `idempotency_key: Optional[str]` (max 64). `PaymentOut` gains
  `allocation_id` and `cash_session_id`.
- `inventory.py`: `InventoryItemUpdate` gains `latitude` (−90..90),
  `longitude` (−180..180), `gps_precision_m` (≥0); `InventoryItemOut` returns
  them. Warehouse Create/Update/Out gain `latitude`/`longitude`.
- `company.py`: `BankAccount` (`holder`, `bank`, `account`, `type`,
  `currency` default GTQ) and `MobileSettings` (`bank_account`,
  `collector_daily_goal`, `technician_daily_goal`), both `extra="forbid"`.
  `CompanyUpdate` and `CompanyOut` gain `mobile_settings`.
- `requests.py`: `LoginRequest.client_type: Literal["web","mobile"] = "web"`.

### Auth overhaul — request-schema changes (no DB migration)

Company-only signup with locale-aware transactional email:

- `requests.py`: `SignupCompanyRequest` is now **flat and minimal** —
  `company_name` (2..255), `name` (2..255), `email` (EmailStr), `password`
  (min 8), `locale` (`Literal["es","en"]`, default `"es"`). The old nested
  `{company: CompanyCreate, user: UserCreate}` shape and **`SignupUserRequest`
  are deleted** (users are invitation-only; no self-serve user signup).
- `email_verification.py`: `ResendConfirmationRequest` gains `locale`
  (es/en, default es).
- `password_reset.py`: `PasswordResetRequestSchema` gains `locale`;
  `PasswordResetConfirmSchema.new_password` min length raised 6 → 8 (all
  password minimums aligned at 8).
- `invitation.py`: `InvitationCreate` gains `locale` (used for the invitation
  email language); `InvitationAccept` is now `{token, name, password}` with
  `password` min length 8 — the `age` field was **removed** (the accept
  handler passes `age=0` explicitly; no model change).

The `locale` values feed the localized email templates/subjects — see
[email-service.md](email-service.md).

## Environment Variables

None — schemas are pure Python/Pydantic.
