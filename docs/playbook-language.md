# Playbook language and job lifecycle (provisioning worker split, R1 6.9.0, R2 6.10.0)

## Description

Seven modules under `database_utils/utils/` hold the code that both the API
(backend-erp) and the provisioning worker need. They were moved here from
backend-erp in release R1 of the provisioning worker split. R1 only moves code.
Nothing changes behaviour and there is no schema change. (R2, 6.10.0, then
changed `finish_job` / `stamp_connectivity` and added the outbox: see
[R2](#r2-6100-the-outbox).) Each module was copied
verbatim, and only its imports changed. The exceptions are listed under
[What changed in the move](#what-changed-in-the-move).

## Goal

The worker is moving to its own repo (`InnovaTech-GT/provisioning-worker`,
ADR-011). Neither side may import the other, so everything they share has to
live in models-utils:

- **The playbook language.** The UI needs dry runs, template lint and
  `resolve_variables` synchronously, and preview and execution must come from
  one implementation. The worker's executor imports these same functions.
- **The job state machine.** A parked step's «Abortar» runs synchronously in
  the API, and the worker settles jobs. Both use one `finish_job`.
- **Connectivity-job construction and read-side NBI helpers.** The API's
  "test connection" and ACS administration use them, and so do the worker's
  sweep and tr069 driver.

Design and plan: workspace `docs/superpowers/specs/2026-10-15-provisioning-worker-split-design.md`
§4 and `docs/superpowers/plans/2026-10-15-provisioning-worker-split.md` P1.1.

## Modules

| Module | Moved from (backend-erp) | Public surface |
|---|---|---|
| `playbook_template.py` | `provisioning/renderer.py` (all of it) | `TOKEN_SHAPE`, `TemplateError`, `Expr`, `FILTERS`, `parse_body`, `find_variables`, `find_template_errors`, `render_string`, `render_value`, `resolve_variables` (`dry_run=` since 6.25.2), `input_problem` (6.25.2), `mask_secrets`; re-exports `input_key`, `INPUT_NAMESPACE`, `is_secret_name`, `_SECRET_HINTS` |
| `playbook_render.py` | the render half of `provisioning/executor.py`, plus all of `provisioning/redaction.py` and `provisioning/guided.py` | render: `_render_step`, `_leftover_tokens`, `_render_text`, `_render_regex` (RE2, imported lazily), `_threshold`, `_prepare_checks`, `RenderError` (alias `_RenderError`), `_render_target_item`; captures: `_gate_capture`, `merge_captures` (alias `_merge_captures`), `_placeholders`; manual: `is_secret_field`, `_render_manual`; redaction: `redact_secrets`; guided: `GUIDED`, `should_pause`, `render_preview`, `is_pause_entry`, `without_pause`; preview: `_shows_probe`, `dry_run_playbook`, `_NOT_SIMULATED`, `_NOT_SHIPPED_DRIVERS` |
| `provisioning_codes.py` | `Codes`, `NON_RETRYABLE_CODES` from `provisioning/errors.py` | the failure-code vocabulary. Stages, `RETRYABLE`, `is_transient` and `classify_connect_error` stay with the worker |
| `playbook_drivers.py` | `services/playbook_drivers.py`, without `reject_driver_mismatch` and fastapi | `DRIVER_NOT_ALLOWED_FOR_TARGET`, `EDITOR_DRIVERS`, `ALLOWED_BY_TIER`, `allowed_drivers`, `ping_probe`, `driver_options`, `mismatches` |
| `genieacs_nbi.py` | the helpers in `provisioning/drivers/genieacs.py` | `tenant_tag`, `nbi_base_url`, `_nbi_auth`, `nbi_client` (imports httpx lazily), `find_device_id`, `GENIEACS_NBI_TIMEOUT_SECONDS` |
| `connectivity.py` | `provisioning/worker.py` (connectivity section) | `DEFAULT_PROBE_COMMAND`, `_connectivity_definition`, `_get_or_create_connectivity_playbook`, `enqueue_connectivity_check` |
| `provisioning_jobs.py` | `provisioning/worker.py` | `finish_job` (was `_finish`), `write_device_action_log`, `stamp_connectivity`, `fail_parked` (`FollowOn` removed in 6.10.0) |

Additions to existing modules:

| Module | Added | From |
|---|---|---|
| `transport.py` | `company_dials_gateway(db, company_id)` | `provisioning/worker.py` |
| `provisioning_gates.py` | `claim_time_gate_blocked(db, company_id, device_type)` | `utils/provisioning_guards.py` |
| `provisioning_runs.py` | `run_captures(db, job)` (was the worker's `_run_captures`), `parked_step(run, job)` | `provisioning/worker.py`, `services/manual_steps.py` |

## What changed in the move

These are the only differences from the backend-erp source. The verbatim
check is an AST comparison that ignores docstrings:

- **`enqueue_connectivity_check`** no longer calls the backend's
  `enforce_provisioning_gates`, which raised `HTTPException(409)`. It runs
  `failure = gate_failure(...)`, and on a failure it raises the **existing**
  `ProvisioningGateError([failure])`. `errors[0]` is byte-identical to the old
  409 body. The backend maps it back with
  `except ProvisioningGateError as e: raise HTTPException(409, detail=e.errors[0])`,
  and the worker's sweep catches it instead of `HTTPException`.
- **`finish_job(db, job, status, error, *, probed)`** (R2, 6.10.0): its last
  follow-on emits a `JOB_FINISHED` `provisioning_event` with payload
  `{status, error, probed, playbook_name, client_service_id}`. The
  install_state recompute and the INSTALLED promotion are business logic and
  run in backend-erp's event consumer. The event comes after `advance_run`,
  so a final child's `RUN_CLOSED` (emitted by `close_run`) has the smaller id
  and the backend sees the run closed before it recomputes.
- **`stamp_connectivity(db, job)`** stamps `mgmt_last_check_at` /
  `mgmt_last_check_ok` only. It never changes `inventory_item.status`.
- **`fail_parked`** fails the PENDING entry and calls `finish_job`. It
  imports `parked_step` from `provisioning_runs` instead of backend
  `services.manual_steps`.
- **`re2` and `httpx` are imported inside the function** that needs them
  (`_render_regex`, `nbi_client`). models-utils installs neither at runtime,
  so a consumer that never renders a regex or dials the NBI, such as auth-erp,
  needs neither. Both rendering consumers already pin them.
  `requirements-dev.txt` adds `google-re2` for the tests.
- `RenderError` and `merge_captures` were made public. The old names
  `_RenderError` and `_merge_captures` stay as aliases.

`finish_job` keeps the order of the old `_finish`:

1. The job's columns are written and flushed.
2. If `probed`, `device_action_log` runs, then `connectivity_stamp`.
3. `advance_run` runs.
4. Then the `JOB_FINISHED` event is emitted.

Each follow-on runs in its own SAVEPOINT, so a failing one is logged and rolled
back alone and the outcome is kept. The caller commits.

## R2 (6.10.0): the outbox

`utils/provisioning_events.py` holds `emit_event(db, kind, *, job=None,
run=None, company_id=None, payload=None)` and the kinds `RUN_CLOSED`,
`MANUAL_WAITING`, `JOB_FINISHED`, `JOB_UPDATED`, `INSTALL_STATE_STALE`.
`emit_event` derives `company_id` / `run_id` / `job_id` from the job or run,
adds the `provisioning_event` row and flushes; it never commits, so the event
commits or rolls back with the state change. `close_run` emits `RUN_CLOSED`
(payload `{status, error_code}`) for a non-dry run; `RUN_CLOSED_LISTENERS`
is gone. backend-erp drains the table in `id` order behind one advisory lock
and runs the handlers.

## 6.13.0: the `connection_request` driver

`connection_request` is a playbook driver for a step that summons a CPE: the
worker sends it a CWMP connection request from inside the tenant's network
(the ACS on Railway cannot reach CPEs on a private LAN). The CPE then opens a
session with the ACS at once, and GenieACS runs every task queued for it.

- `PLAYBOOK_DRIVERS`, `NETWORK_DRIVERS` and `NO_UNDO_DRIVERS` include it (a
  summon changes nothing to undo, so it is resend-safe and needs no rollback).
- `REQUEST_DRIVERS` (`http`, `tr069`, `connection_request`) are the drivers
  whose payload is a `request` dict. `playbook_render` renders those value by
  value, and the step validator requires `request` for `http` and
  `connection_request`.
- `ALLOWED_BY_TIER["EDGE"]` allows it. It is not in `EDITOR_DRIVERS`: playbooks
  normally use the tr069 request flag `summon` instead, which runs the same
  summon inside the tr069 step (provisioning-worker `drivers/connection_request.py`).

The request shape is defined by the worker driver, not validated here.

## 6.25.2: playbook inputs

A playbook's `variables` are the inputs a caller supplies (`{{input.<key>}}`),
for example the customer's new Wi-Fi password from the bot.

**Declaring an input.** `PlaybookVariable` is `{key, label, type, required,
default, min_length, max_length}`. `min_length` / `max_length` are optional,
TEXT only, `1 <= min_length <= max_length <= 64` (64 is backend's SAFE_TEXT
cap). A WPA2 passphrase is declared with `min_length: 8, max_length: 63`.
Because they are modelled, both survive the playbook POST/PATCH `model_dump`.

**Judging a value.** `input_problem(var, value)` (pure, in
`playbook_template`) returns `MISSING_INPUT` when the value is `None`, `""`
or its dry-run stand-in and the variable is required with no default;
`INVALID_INPUT` when the text is shorter than `min_length` or longer than
`max_length`; otherwise `None`. backend-erp's `clean_input` calls the same
function, so the bot and run creation agree.

**Live runs are refused at run creation.** `_plan_checks` runs
`input_problem` over every declared variable on the plan's snapshotted
definitions, judged on the run's shared frame (`extra_variables` included,
bare or `input.`-namespaced keys). On a problem it raises
`ResolutionError(code, ..., errors=[{code, key, playbook_id, detail}])`, one
entry per key, `code` = the first entry's. This happens before the ACS
registration ensure, so nothing is written. `create_run` passes its shared
frame; `preflight_run(..., provided=)` takes the caller's inputs (a live
preflight without them refuses a plan that declares a required input).
Callers map it to 422 like any resolution failure. A standalone job (no run)
still fails in the worker, where `resolve_variables` reports the key missing.

**Dry runs get stand-ins.** `resolve_variables(definition, provided,
dry_run=True)` fills a missing required TEXT input (type unset counts as
TEXT) with `«input.<key>»`, which has no braces, so the leftover-token guard
passes. NUMBER and BOOLEAN inputs stay missing, so they need a `default` to be
simulated. `dry_run_playbook` and the worker's executor pass `dry_run`; a dry
run never refuses on inputs. In a live call a provided value equal to its
stand-in counts as missing, so a stand-in can never reach a device.

**Masking is name-based.** A variable's value is masked (`***`) in
`ProvisioningJobOut.variables`, in `log.variables` and redacted from rendered
steps only when its key contains `password`, `secret`, `token`, `key` or
`credential` (`is_secret_name`). A customer secret must be named that way
(`wifi_password`, not `wifi_clave`). There is no declared `sensitive` flag.

## Tests

| File | Covers |
|---|---|
| `tests/test_template_filters.py` | The renderer half of backend's test: filters, security, static/runtime errors, the guard, the hash-locked shared fixture `tests/fixtures/playbook_filters.json` (byte-identical to backend-erp's and frontend-erp's copies) |
| `tests/test_namespaced_variables.py` | The doc-33 grammar, `input.*`, the leftover-token guard; 6.25.2: dry-run stand-ins, the live stand-in backstop, `dry_run_playbook` on the CGG WIFI_PASSWORD definition, the `input_problem` table |
| `tests/test_provisioning_run_v2.py` (inputs) | 6.25.2: a live `create_run` missing / with an invalid input raises `MISSING_INPUT` / `INVALID_INPUT` with no run or job rows; a valid input lands in `frames.shared`; a dry run needs none; `preflight_run(provided=)` |
| `tests/test_playbook_schema_v2.py` (inputs) | 6.25.2: `min_length`/`max_length` refusals, the WIFI_PASSWORD variable keeps both through `model_dump`, `ProvisioningJobOut.variables` masked |
| `tests/test_redaction.py` | `redact_secrets`. The executor-integration and `secret_safe_audit` cases stay in backend-erp |
| `tests/test_playbook_drivers.py` | The tier matrix, `ping_probe`, `driver_options`, `mismatches`. The 422 wrapper cases stay in backend-erp |
| `tests/test_connectivity.py` | A gate failure raises `ProvisioningGateError` with `errors[0]` equal to the old 409 body and creates no job; device-dial vs gateway-dial playbook selection; get-or-create reuse |
| `tests/test_finish_job.py` | Follow-on order (… `advance_run`, then the event); a failing follow-on is rolled back alone (the outcome and the later follow-ons survive); `stamp_connectivity` never touches `status` |
| `tests/test_provisioning_events.py` | `emit_event` id derivation, refusals, no commit; `close_run` emits once, a dry run emits nothing; `finish_job` emits `JOB_FINISHED` after the final child's `RUN_CLOSED` (id order) with the full payload; a connectivity job through `finish_job` leaves an IN_STOCK core IN_STOCK |
| `tests/pg/test_pw1_pg.py` | The migration on Postgres: bigserial ids, defaults, the partial index, the company FK, idempotent downgrade then upgrade |

## Connections to Other Components

- **backend-erp**: in P1.2 its old module paths (`provisioning/renderer.py`,
  `guided.py`, `redaction.py`, `services/playbook_drivers.py` and others)
  become re-export shims over these modules. Its routers import from here
  directly.
- **provisioning-worker** (from P4): the executor, the drivers and the worker
  loop import these modules. The worker must never import backend-erp.
