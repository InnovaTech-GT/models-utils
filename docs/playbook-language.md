# Playbook language and job lifecycle (provisioning worker split, R1, 6.9.0)

## Description

Seven modules under `database_utils/utils/` hold the code that both the API
(backend-erp) and the provisioning worker need. They were moved here from
backend-erp in release R1 of the provisioning worker split. R1 only moves code.
Nothing changes behaviour and there is no schema change. Each module was copied
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
| `playbook_template.py` | `provisioning/renderer.py` (all of it) | `TOKEN_SHAPE`, `TemplateError`, `Expr`, `FILTERS`, `parse_body`, `find_variables`, `find_template_errors`, `render_string`, `render_value`, `resolve_variables`, `mask_secrets`; re-exports `input_key`, `INPUT_NAMESPACE`, `is_secret_name`, `_SECRET_HINTS` |
| `playbook_render.py` | the render half of `provisioning/executor.py`, plus all of `provisioning/redaction.py` and `provisioning/guided.py` | render: `_render_step`, `_leftover_tokens`, `_render_text`, `_render_regex` (RE2, imported lazily), `_threshold`, `_prepare_checks`, `RenderError` (alias `_RenderError`), `_render_target_item`; captures: `_gate_capture`, `merge_captures` (alias `_merge_captures`), `_placeholders`; manual: `is_secret_field`, `_render_manual`; redaction: `redact_secrets`; guided: `GUIDED`, `should_pause`, `render_preview`, `is_pause_entry`, `without_pause`; preview: `_shows_probe`, `dry_run_playbook`, `_NOT_SIMULATED`, `_NOT_SHIPPED_DRIVERS` |
| `provisioning_codes.py` | `Codes`, `NON_RETRYABLE_CODES` from `provisioning/errors.py` | the failure-code vocabulary. Stages, `RETRYABLE`, `is_transient` and `classify_connect_error` stay with the worker |
| `playbook_drivers.py` | `services/playbook_drivers.py`, without `reject_driver_mismatch` and fastapi | `DRIVER_NOT_ALLOWED_FOR_TARGET`, `EDITOR_DRIVERS`, `ALLOWED_BY_TIER`, `allowed_drivers`, `ping_probe`, `driver_options`, `mismatches` |
| `genieacs_nbi.py` | the helpers in `provisioning/drivers/genieacs.py` | `tenant_tag`, `nbi_base_url`, `_nbi_auth`, `nbi_client` (imports httpx lazily), `find_device_id`, `GENIEACS_NBI_TIMEOUT_SECONDS` |
| `connectivity.py` | `provisioning/worker.py` (connectivity section) | `DEFAULT_PROBE_COMMAND`, `_connectivity_definition`, `_get_or_create_connectivity_playbook`, `enqueue_connectivity_check` |
| `provisioning_jobs.py` | `provisioning/worker.py` | `finish_job` (was `_finish`), `write_device_action_log`, `stamp_connectivity`, `fail_parked`, `FollowOn` |

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
- **`finish_job`** takes `extra_follow_ons: Iterable[(name, fn(db, job))]`.
  They run after `advance_run`, each in its own SAVEPOINT. In R1 the worker
  passes its install_state recompute here. That recompute is business logic
  and stays in backend-erp. It must run after `advance_run`, because it reads
  the run status.
- **`stamp_connectivity(db, job, *, promote=True)`**: with `promote=False` it
  skips the IN_STOCK/RESERVED → INSTALLED promotion. R1 callers keep the
  default, so behaviour is unchanged.
- **`fail_parked`** passes `extra_follow_ons` through to `finish_job`. It
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
4. Then each `extra_follow_ons` entry runs.

Each follow-on runs in its own SAVEPOINT, so a failing one is logged and rolled
back alone and the outcome is kept. The caller commits.

## R2 (next release, P2)

R2 removes both seams. `finish_job` drops `extra_follow_ons` and emits a
`JOB_FINISHED` `provisioning_event`. `stamp_connectivity` drops `promote`, and
the backend's event consumer does the promotion.

## Tests

| File | Covers |
|---|---|
| `tests/test_template_filters.py` | The renderer half of backend's test: filters, security, static/runtime errors, the guard, the hash-locked shared fixture `tests/fixtures/playbook_filters.json` (byte-identical to backend-erp's and frontend-erp's copies) |
| `tests/test_namespaced_variables.py` | The doc-33 grammar, `input.*`, the leftover-token guard |
| `tests/test_redaction.py` | `redact_secrets`. The executor-integration and `secret_safe_audit` cases stay in backend-erp |
| `tests/test_playbook_drivers.py` | The tier matrix, `ping_probe`, `driver_options`, `mismatches`. The 422 wrapper cases stay in backend-erp |
| `tests/test_connectivity.py` | A gate failure raises `ProvisioningGateError` with `errors[0]` equal to the old 409 body and creates no job; device-dial vs gateway-dial playbook selection; get-or-create reuse |
| `tests/test_finish_job.py` | Follow-on order; a failing follow-on is rolled back alone (the outcome and the later follow-ons survive); `stamp_connectivity(promote=)` |

## Connections to Other Components

- **backend-erp**: in P1.2 its old module paths (`provisioning/renderer.py`,
  `guided.py`, `redaction.py`, `services/playbook_drivers.py` and others)
  become re-export shims over these modules. Its routers import from here
  directly.
- **provisioning-worker** (from P4): the executor, the drivers and the worker
  loop import these modules. The worker must never import backend-erp.
