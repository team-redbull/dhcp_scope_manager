# DHCP Scope Manager

A production-oriented FastAPI service for managing Windows DHCP IPv4 scopes through PowerShell, designed for GitOps reconciliation with Crossplane `provider-http`.

## What This Project Does

This repository connects declarative cluster configuration to real DHCP server state:

1. A values file in Git defines the desired DHCP scope configuration.
2. Helm renders a Crossplane `Request` resource from those values.
3. Crossplane reconciles by calling this API (`GET` / `POST` / `PUT` / `DELETE`).
4. The API executes Windows DHCP PowerShell cmdlets.
5. Current DHCP state is normalized back into a canonical shape so GET equals the desired PUT body.

The user only ever edits values files. The backend is not a manual entry point.

## Architecture

```text
Git (values files — desired state)
  → Helm (renders Crossplane Request CR)
    → Crossplane provider-http (reconciliation engine)
      → FastAPI DHCP backend (validate + normalize + execute)
        → PowerShell cmdlets
          → Windows DHCP Server
```

## Repository Layout

```text
app/
  main.py                    FastAPI app bootstrap
  config.py                  Env-based settings (auth, bind address, logging, PowerShell limits)
  logging_config.py          JSON structured logging
  errors.py                  Project error classes and stable machine-readable error codes
  exception_handlers.py      Global exception → standard JSON error response mapping
  dependencies/
    auth.py                  Bearer token verification (verify_token dependency)
    dhcp.py                  DHCP runtime environment guard dependency
    scopes.py                scope_id and request body validation (validate_scope_id, validate_scope_request)
  models/
    __init__.py              Re-exports all model types
    scope.py                 DhcpScopePayload — canonical request/response model
    failover.py              DhcpFailover — failover relationship configuration
    exclusion.py             DhcpExclusion — exclusion range
    list_response.py         DhcpScopeListResponse / DhcpScopeListError — GET /scopes response
  routers/
    __init__.py              Aggregates all sub-routers into a single router — main.py imports only this
    scopes.py                DHCP scope endpoints (POST/GET/PUT/DELETE /api/v1/scopes/{scope_id})
    health.py                /healthz runtime capability check
  services/
    dhcp_service.py          Runtime guard (OS / PowerShell / DHCP cmdlets check)
    ps_executor.py           Async PowerShell command runner with timeout/error handling
    ps_parsers.py            Single-process GET script builder and PowerShell JSON normalization
    scope_service.py         Core scope lifecycle logic (create / get / update / delete)
  utils/
    decorators.py            Async-aware lightweight logging decorator for service calls
    ip_utils.py              IP integer conversion and TimeSpan parsing helpers
    locks.py                 Async per-scope lock manager for serialized mutations

helm/
  Chart.yaml
  values.yaml                Reference values file with all supported fields documented
  templates/
    dhcp-scope-request.yaml  Crossplane Request CR — all verbs (POST/GET/PUT/DELETE) on /{network}
    _dhcp-helpers.tpl        Canonical payload rendering for provider-http

scripts/
  validate_dhcp_values.py    CI validator — walks sites/ structure, validates folder layout,
                             YAML content, and DHCP business rules on the merged inheritance chain
  requirements.txt           Minimal CI dependencies (pydantic, PyYAML)

tests/
  conftest.py
  test_async_runtime.py          Async subprocess execution, locks, timeout, and concurrency basics
  test_concurrency_stress.py     High-concurrency observe/write workload behavior
  test_decorators_and_locks.py   log_call and ScopeLockManager unit tests
  test_endpoints.py              HTTP endpoint contracts and status codes
  test_models.py                 Pydantic field ordering and serialization
  test_validation.py             IP validation, subnet consistency, failover mode enforcement
  test_parsers.py                Single-process GET parsing, normalization, and injection safety
  test_ps_executor_unit.py       Focused ps_executor command construction and sanitization tests
  test_diff.py                   Diff-based update logic
  test_dhcp_service.py           Runtime environment guard behavior
  test_parity.py                 GET/PUT parity — the main guard against Crossplane reconciliation loops
  test_edge_cases.py             Edge cases and boundary conditions
  test_helm.py                   Helm-rendered Crossplane Request contract
  test_security.py               PowerShell escaping and response sanitization
  test_service_unit.py           Focused scope_service create/get/delete/list behavior
  test_validate_dhcp_values.py   CI validator — structure, YAML checks, filtering, deep merge
```

## Runtime Requirements

The API itself requires:

- Python 3.12+
- Windows host (native Windows, **not** Linux / macOS / WSL)
- `powershell.exe` on PATH
- DHCP PowerShell cmdlets (`Get-DhcpServerv4Scope`, etc.)
  - Windows Server: DHCP Server role or RSAT DHCP tools
  - Windows client: RSAT DHCP tools

The CI validation scripts require only Python 3.12+ and can run on any OS.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

| Variable                               | Default   | Description                                                   |
| -------------------------------------- | --------- | ------------------------------------------------------------- |
| `DHCP_API_TOKEN`                       | _(empty)_ | Bearer token for auth. When unset, auth is disabled entirely. |
| `HOST`                                 | `0.0.0.0` | Bind address                                                  |
| `PORT`                                 | `8080`    | Bind port                                                     |
| `LOG_LEVEL`                            | `INFO`    | Log level                                                     |
| `POWERSHELL_COMMAND_TIMEOUT_SECONDS`   | `60`      | Timeout for DHCP PowerShell operations.                       |
| `POWERSHELL_ENV_CHECK_TIMEOUT_SECONDS` | `15`      | Timeout for PowerShell startup/cmdlet availability checks.    |
| `POWERSHELL_MAX_CONCURRENCY`           | `10`      | Maximum concurrent PowerShell commands across all requests.   |

A `.env` file in the repo root is also supported.

## Run the API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## API Endpoints

Base path: `/api/v1`

`scope_id` is always the IPv4 network address of the scope (e.g. `10.20.30.0`).

All `/api/v1/scopes*` endpoints share two implicit checks that run before the handler:

- **Auth** — rejects requests when `DHCP_API_TOKEN` is set and the token is missing or wrong. Returns `401`.
- **Environment guard** — rejects requests when the host cannot execute DHCP automation (wrong OS, missing PowerShell, no DHCP cmdlets). Returns `503`.

Scope APIs use a real async execution path:

```text
async FastAPI route
  → async service function
  → async PowerShell executor
  → asyncio.create_subprocess_exec()
  → awaited stdout/stderr result
```

PowerShell execution is globally bounded by `POWERSHELL_MAX_CONCURRENCY`. Mutating operations (`POST`, `PUT`, `DELETE`) also take an async per-scope lock, so two writes for `10.20.30.0` are serialized while writes for different scopes can run concurrently up to the global limit.

## Error Response Format

All API errors use the same envelope:

```json
{
  "error": {
    "code": "SCOPE_NOT_FOUND",
    "message": "DHCP scope 10.20.30.0 was not found",
    "details": {}
  }
}
```

- `error.code` is stable and machine-readable for Crossplane events and automation.
- `error.message` is human-readable and safe to expose.
- `error.details` contains sanitized structured context such as `scopeId`, `network`, validation errors, or DHCP environment `reason`.

Raw PowerShell commands, stack traces, and full internal stderr are not returned to clients. Backend logs use safe context such as request path, `scope_id`, operation name, return code, and sanitized stderr previews.

Common error codes:

| HTTP Status | Error Code                     | Meaning                                                        |
| ----------- | ------------------------------ | -------------------------------------------------------------- |
| `400`       | `INVALID_SCOPE_ID`             | `scope_id` is not a valid IPv4 address                         |
| `400`       | `SCOPE_ID_MISMATCH`            | Path `scope_id` does not match body `network`                  |
| `401`       | `UNAUTHORIZED`                 | Missing or invalid bearer token                                |
| `404`       | `SCOPE_NOT_FOUND`              | DHCP scope does not exist                                      |
| `409`       | `DHCP_CONFLICT`                | Windows DHCP reported an unsafe already-exists/in-use conflict |
| `422`       | `VALIDATION_ERROR`             | Request body failed FastAPI/Pydantic validation                |
| `500`       | `POWERSHELL_COMMAND_FAILED`    | PowerShell failed unexpectedly                                 |
| `500`       | `INTERNAL_ERROR`               | Unexpected Python/backend bug                                  |
| `503`       | `DHCP_ENVIRONMENT_UNAVAILABLE` | Backend host cannot run DHCP automation                        |
| `504`       | `POWERSHELL_TIMEOUT`           | PowerShell command timed out                                   |

Validation errors include compact field entries:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": {
      "errors": [
        {
          "field": "body.startRange",
          "message": "Input should be a valid IPv4 address",
          "type": "ip_v4_address"
        }
      ]
    }
  }
}
```

---

### `GET /api/v1/scopes`

Returns all scopes sorted by network address (ascending). Uses **one PowerShell process** for the entire fleet — the backend builds a single script that loops through all scopes, runs the required DHCP cmdlets in-process, and emits one JSON array. This is O(1) PowerShell processes regardless of fleet size.

**Partial-result semantics** — the response is always `200` and always contains both a `scopes` list and an `errors` list:

- PowerShell-level failures (connection refused, permission denied, etc.) propagate as `500` — the entire list is unavailable.
- Per-scope assembly errors (invalid data, missing DNS option, unrecognized field format) are caught individually. The broken scope is added to `errors` with its `scopeId` and a description; all other scopes are returned normally in `scopes`.

```json
{
  "scopes": [
    { "scopeName": "...", "network": "10.20.30.0", "..." : "..." }
  ],
  "errors": [
    { "scopeId": "10.20.31.0", "error": "No DNS servers configured for this scope" }
  ]
}
```

| Status | Body                                                    | When                                                              |
| ------ | ------------------------------------------------------- | ----------------------------------------------------------------- |
| `200`  | `DhcpScopeListResponse`                                 | Success — `scopes` may be empty; `errors` lists any broken scopes |
| `401`  | Standard error body with `UNAUTHORIZED`                 | Bad or missing bearer token                                       |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`    | PowerShell-level failure — entire list unavailable                |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE` | Host cannot run DHCP automation                                   |

---

### `POST /api/v1/scopes/{scope_id}`

Creates the scope if it does not exist, then converges all options, exclusions, and failover to the desired state. Idempotent — never fails if the scope already exists.

| Status | Body                                                               | When                                                                        |
| ------ | ------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `200`  | `DhcpScopePayload`                                                 | Scope created or already present and converged                              |
| `400`  | Standard error body with `INVALID_SCOPE_ID` or `SCOPE_ID_MISMATCH` | `scope_id` is not a valid IPv4 address, or path `scope_id` ≠ body `network` |
| `401`  | Standard error body with `UNAUTHORIZED`                            | Bad or missing bearer token                                                 |
| `409`  | Standard error body with `DHCP_CONFLICT`                           | Unsafe existing/in-use DHCP state                                           |
| `422`  | Standard error body with `VALIDATION_ERROR`                        | Request body fails Pydantic field constraints                               |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`               | PowerShell cmdlet failed                                                    |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE`            | Host cannot run DHCP automation                                             |
| `504`  | Standard error body with `POWERSHELL_TIMEOUT`                      | PowerShell command timed out                                                |

---

### `GET /api/v1/scopes/{scope_id}`

Returns the current canonical state of the scope. When Crossplane sees a `404` here it issues `POST` to create the scope.

The backend builds a single script that runs all required DHCP cmdlets in-process (one PowerShell process):

1. `Get-DhcpServerv4Scope -ScopeId ...`
2. `Get-DhcpServerv4OptionValue -ScopeId ...`
3. `Get-DhcpServerv4ExclusionRange -ScopeId ...`
4. `Get-DhcpServerv4Failover -ScopeId ...`

`options` and `exclusions` are array-wrapped in PowerShell so single-result output does not collapse into an object. Missing exclusions become `[]`, missing failover becomes `null`, missing scope becomes `404 SCOPE_NOT_FOUND`. Any other cmdlet failure is re-thrown rather than silently returning empty state. `scope_id` is validated as an IPv4 address and inserted through a central PowerShell single-quote literal helper.

| Status | Body                                                    | When                                                       |
| ------ | ------------------------------------------------------- | ---------------------------------------------------------- |
| `200`  | `DhcpScopePayload`                                      | Scope found                                                |
| `400`  | Standard error body with `INVALID_SCOPE_ID`             | `scope_id` is not a valid IPv4 address                     |
| `401`  | Standard error body with `UNAUTHORIZED`                 | Bad or missing bearer token                                |
| `404`  | Standard error body with `SCOPE_NOT_FOUND`              | Scope does not exist on the DHCP server                    |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`    | PowerShell cmdlet failed for a reason other than not-found |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE` | Host cannot run DHCP automation                            |
| `504`  | Standard error body with `POWERSHELL_TIMEOUT`           | PowerShell command timed out                               |

---

### `PUT /api/v1/scopes/{scope_id}`

Diff-based convergence — compares the current scope state to the desired payload and issues only the PowerShell cmdlets needed to reconcile the difference.

| Changed fields                                                            | PowerShell cmdlet                                                                            |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `scopeName`, `leaseDurationDays`, `description`, `startRange`, `endRange` | `Set-DhcpServerv4Scope`                                                                      |
| `gateway`, `dnsServers`, `dnsDomain`                                      | `Set-DhcpServerv4OptionValue`; `gateway: null` removes DHCP option 3                         |
| Exclusions added                                                          | `Add-DhcpServerv4ExclusionRange`                                                             |
| Exclusions removed                                                        | `Remove-DhcpServerv4ExclusionRange`                                                          |
| Failover added / changed / removed                                        | `Add-DhcpServerv4Failover` / `Set-DhcpServerv4Failover` / `Remove-DhcpServerv4FailoverScope` |

| Status | Body                                                               | When                                                                        |
| ------ | ------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `200`  | `DhcpScopePayload`                                                 | Scope updated (or already at desired state — no-op)                         |
| `400`  | Standard error body with `INVALID_SCOPE_ID` or `SCOPE_ID_MISMATCH` | `scope_id` is not a valid IPv4 address, or path `scope_id` ≠ body `network` |
| `401`  | Standard error body with `UNAUTHORIZED`                            | Bad or missing bearer token                                                 |
| `404`  | Standard error body with `SCOPE_NOT_FOUND`                         | Scope does not exist — Crossplane responds by issuing `POST`                |
| `409`  | Standard error body with `DHCP_CONFLICT`                           | Unsafe existing/in-use DHCP state                                           |
| `422`  | Standard error body with `VALIDATION_ERROR`                        | Request body fails Pydantic field constraints                               |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`               | PowerShell cmdlet failed                                                    |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE`            | Host cannot run DHCP automation                                             |
| `504`  | Standard error body with `POWERSHELL_TIMEOUT`                      | PowerShell command timed out                                                |

---

### `DELETE /api/v1/scopes/{scope_id}`

Deletes the scope and cleans up its failover relationship and exclusion ranges. Idempotent — returns `204` even if the scope does not exist.

Deletion order:

1. Remove scope from failover relationship (`Remove-DhcpServerv4FailoverScope`)
2. Remove failover relationship if now empty (`Remove-DhcpServerv4Failover`)
3. Remove each exclusion range
4. Remove the scope (`Remove-DhcpServerv4Scope -Force`)

If failover detach fails, the delete propagates a `500` so Crossplane retries on the next cycle rather than removing the CR while the scope remains on the server.

| Status | Body                                                    | When                                                   |
| ------ | ------------------------------------------------------- | ------------------------------------------------------ |
| `204`  | _(empty)_                                               | Scope deleted, or scope did not exist                  |
| `400`  | Standard error body with `INVALID_SCOPE_ID`             | `scope_id` is not a valid IPv4 address                 |
| `401`  | Standard error body with `UNAUTHORIZED`                 | Bad or missing bearer token                            |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`    | PowerShell cmdlet failed (e.g. failover detach failed) |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE` | Host cannot run DHCP automation                        |
| `504`  | Standard error body with `POWERSHELL_TIMEOUT`           | PowerShell command timed out                           |

---

### `GET /healthz`

Checks that the runtime environment can execute DHCP automation:

1. Native Windows OS (not WSL / Linux / macOS)
2. `powershell.exe` present and executable
3. DHCP cmdlets available (`Get-DhcpServerv4Scope` discoverable)

Protected by auth (like all endpoints). Does **not** check the DHCP environment dependency before running — it is the check itself, so it always returns a structured response rather than a plain 503.

Environment validation is async-safe and cached per process. A successful check is cached for the process lifetime, so repeated requests do not re-run the PowerShell environment checks. Failed checks are cached briefly and retried after the negative-cache TTL so transient PowerShell startup failures can recover without restarting the backend.

| Status | Body                                                    | When                        |
| ------ | ------------------------------------------------------- | --------------------------- |
| `200`  | `{"status": "ok"}`                                      | All checks pass             |
| `401`  | Standard error body with `UNAUTHORIZED`                 | Bad or missing bearer token |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE` | Any runtime check fails     |

`reason` values: `unsupported_os`, `wsl_detected`, `powershell_not_found`, `powershell_exec_failed`, `dhcp_cmdlets_unavailable`.

## Canonical Payload Shape

```json
{
  "scopeName": "cluster-a-workers",
  "network": "10.20.30.0",
  "subnetMask": "255.255.255.0",
  "startRange": "10.20.30.50",
  "endRange": "10.20.30.200",
  "leaseDurationDays": 8,
  "description": "",
  "gateway": "10.20.30.1",
  "dnsServers": ["10.10.1.5", "10.10.1.6"],
  "dnsDomain": "lab.local",
  "exclusions": [{ "startAddress": "10.20.30.1", "endAddress": "10.20.30.10" }],
  "failover": null
}
```

- Field order is intentional and tested — Crossplane byte-compares GET response to PUT body.
- `failover` is either `null` or a full failover object (no partial objects).
- Exclusions are always returned sorted by IP (ascending). Values files must match this order.
- `dnsServers` must contain at least one IPv4 address. If GET observes a managed scope without DNS servers, the backend treats that as invalid managed state instead of returning a pretend-valid payload.
- `gateway` is optional. In values files, use `gateway: ""` when you do not want DHCP option 3; the API also accepts `null`/omitted and GET returns `null` when absent.
- **Gateway-in-range guard**: if `gateway` is set to an IP inside `[startRange, endRange]` and is not covered by an exclusion, the request is rejected with `422 VALIDATION_ERROR`. An unexcluded gateway inside the distribution pool would be leased to a client, causing a network outage.
- DNS server order is preserved exactly (primary/secondary semantics — never sorted).
- `description` defaults to `""` (never `null`).

## Failover Model

Supported modes: `HotStandby`, `LoadBalance`

| Mode          | Required fields      | Normalized fields                                 |
| ------------- | -------------------- | ------------------------------------------------- |
| `HotStandby`  | `serverRole`         | `loadBalancePercent` → `0`                        |
| `LoadBalance` | `loadBalancePercent` | `serverRole` → `"Active"`, `reservePercent` → `0` |

Normalization at both the Helm template layer and the Pydantic model layer prevents GET/PUT drift
when values include cross-mode fields.

## Helm Chart

The chart under `helm/` renders a single Crossplane `Request` CR.

Key behaviors:

- **Crossplane object name** is based only on `dhcp_values.network` (`dhcp-scope-10-20-30-0`).
  Changing `scopeName` does **not** create a new Crossplane CR or delete the live scope.
- **Required fields** — strict DHCP payload validation is enforced by the backend/Pydantic model
  and the optional CI validator, not by large Helm `required()` blocks. The chart keeps only the
  minimal existing render-time checks needed to form the Request URL/name.
- **Optional defaults** — `description`, `gateway`, and `dns.domain` can be written as `""`,
  `exclusions` renders as `[]`, and disabled failover renders as `null`.
- **`providerConfigRef.name`** is configurable via `crossplane.providerConfigName`
  (defaults to `dhcp-http`).

Reference chart render (single file):

```bash
helm template dhcp-request ./helm -f ./helm/values.yaml
```

Production render (three-layer merge — later file wins):

```bash
helm template dhcp-scope-hc-workers ./helm \
  -f sites/site-a/values.yaml \
  -f sites/site-a/mce-1/values.yaml \
  -f sites/site-a/mce-1/hc-workers.yaml
```

## HTTP Response Codes

Quick reference — see the per-endpoint tables above for the exact set each route can return.

| Code  | Meaning               | Error code examples                                                                    |
| ----- | --------------------- | -------------------------------------------------------------------------------------- |
| `200` | OK                    | Success response: `DhcpScopePayload`, `DhcpScopeListResponse`, or `{"status": "ok"}` |
| `204` | No Content            | Success response with empty body — DELETE only                                         |
| `400` | Bad Request           | `INVALID_SCOPE_ID`, `SCOPE_ID_MISMATCH`                                                |
| `401` | Unauthorized          | `UNAUTHORIZED`                                                                         |
| `404` | Not Found             | `SCOPE_NOT_FOUND` — GET and PUT only                                                   |
| `409` | Conflict              | `DHCP_CONFLICT`                                                                        |
| `422` | Unprocessable Entity  | `VALIDATION_ERROR`                                                                     |
| `500` | Internal Server Error | `POWERSHELL_COMMAND_FAILED`, `INTERNAL_ERROR`                                          |
| `503` | Service Unavailable   | `DHCP_ENVIRONMENT_UNAVAILABLE`                                                         |
| `504` | Gateway Timeout       | `POWERSHELL_TIMEOUT`                                                                   |

## Reconciliation Contract

Crossplane reconciles every ~60 seconds: GET current state → compare to desired PUT body → issue PUT on any diff.

Rules that must hold to prevent infinite reconciliation loops:

- GET response must be byte-identical to the desired PUT body when no change is intended.
- No hidden defaults or transformations inside the API.
- Exclusions in values files **must** be in ascending IP numerical order — the API always returns them sorted.
- DNS server order must match exactly — the API preserves insertion order, never sorts.
- **Gateway must not be inside the distribution range without an exclusion** — the backend rejects this at validation time (`422`) so it never reaches the DHCP server.

**Removing failover with layered values files:** use `failover: null` — not `failover: {}`.
Helm deep-merges `{}` with the parent map, leaving failover intact. Only `null` removes it.

## DHCP Values Validation

`scripts/validate_dhcp_values.py` is a self-contained validator. It checks the `sites/` folder
structure and the DHCP business rules on every hosted-cluster values file before anything reaches
Crossplane.

### Required folder structure

```text
sites/
├── configValues.yaml              ← global config (required, must not be empty)
├── telAviv/
│   ├── values.yaml                ← site-level defaults
│   └── mces/
│       ├── prep-mce-tlv-a/
│       │   ├── values.yaml        ← MCE-level overrides
│       │   └── hostedClusters/
│       │       ├── prep-tlv-gpu.yaml
│       │       └── prod-tlv-generic.yaml
│       └── prod-mce-tlv-b/
│           ├── values.yaml
│           └── hostedClusters/
│               └── ...
├── newYork/
│   ├── values.yaml
│   └── mces/
│       └── ...
```

Inheritance chain per cluster (last file wins — mirrors Helm `-f` merge semantics):

```
sites/<site>/values.yaml
  → sites/<site>/mces/<mce>/values.yaml
    → sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml
```

### What it validates

| Layer          | Checks                                                                                  |
| -------------- | --------------------------------------------------------------------------------------- |
| Global         | `sites/` exists; `configValues.yaml` present and valid YAML (may be empty)             |
| Site           | Directory has `values.yaml` (valid YAML, **may be empty**) and `mces/`                 |
| MCE            | Directory has `values.yaml` (valid YAML, **may be empty**) and `hostedClusters/`       |
| Hosted cluster | `.yaml`/`.yml` extension; valid YAML; **must not be empty**                            |
| DHCP content   | Required fields, IP validity, subnet consistency, range ordering, exclusion overlaps, gateway-in-range guard, failover mode fields, exclusion sort order |

### Local usage

```bash
pip install -r scripts/requirements.txt

# Validate everything under sites/
python3 scripts/validate_dhcp_values.py

# Explicit sites directory
python3 scripts/validate_dhcp_values.py --sites-dir sites

# Filter to one site, MCE, or cluster
python3 scripts/validate_dhcp_values.py --site telAviv
python3 scripts/validate_dhcp_values.py --mce prep-mce-tlv-a
python3 scripts/validate_dhcp_values.py --cluster prep-tlv-gpu
python3 scripts/validate_dhcp_values.py --cluster prep-tlv-gpu.yaml  # full filename also works

# Extra flags
python3 scripts/validate_dhcp_values.py --verbose
python3 scripts/validate_dhcp_values.py --fail-fast
python3 scripts/validate_dhcp_values.py --format json
```

Exit codes: `0` = pass, `1` = validation errors, `2` = script/IO error.

### GitLab CI

The `.gitlab-ci.yml` at the repo root runs the validation job on every MR that touches a YAML
file inside `sites/`:

```yaml
validate_dhcp_values:
  stage: validate
  image: python:3.12-slim
  before_script:
    - pip install -r scripts/requirements.txt
  script:
    - python3 scripts/validate_dhcp_values.py --sites-dir sites
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes:
        - sites/**/*.yaml
```

Dependencies are installed with `pip install -r scripts/requirements.txt` (pydantic + PyYAML).

## Security and Safety

- Bearer token auth via `DHCP_API_TOKEN` — optional; disabled when unset
- Runtime environment guard rejects all scope operations on non-Windows / non-DHCP hosts
- `-ErrorAction Stop` on every PowerShell command
- PowerShell stderr is sanitized before returning to clients and before logging previews
- Structured JSON logs include safe fields on every entry: `scope_id` (auto-extracted from the call when the function accepts it), `operation` (function name), `duration_ms`, `status` (`ok`/`error`), `relationship_name`, `returncode`, `stderr_preview`, and `error_code`

## Debugging Errors

From Crossplane events:

1. Read `error.code` first. It is stable and safe to use for automation.
2. Use `error.message` for the short human explanation.
3. Use `error.details` for safe context such as `scopeId`, body validation fields, or DHCP environment `reason`.

From backend logs:

- `AppError` entries mean the request failed in an expected, client-safe way.
- `RequestValidationError` entries include sanitized validation fields and messages, not raw input values.
- `PowerShellError` entries include return code, operation name, `scope_id` when available, and sanitized stderr preview.
- `DhcpEnvironmentError` entries include the full internal environment failure detail.
- `INTERNAL_ERROR` responses mean an unexpected Python exception reached the fallback handler; inspect backend logs for the request path and timestamp.

Crossplane-specific behavior:

- `GET` missing scope returns `404 SCOPE_NOT_FOUND`, which lets provider-http create it.
- `PUT` missing scope returns `404 SCOPE_NOT_FOUND`, which exposes drift instead of silently writing to the wrong object.
- `DELETE` missing scope returns `204 No Content`; deletes are intentionally idempotent.
- Delete failures after partial cleanup return an error so Crossplane retries rather than removing the CR while DHCP state remains.

## Testing

```bash
.venv/bin/python -m pytest -v
```

Test coverage includes:

- Endpoint contracts and HTTP status codes
- Async runtime behavior, subprocess timeout handling, and concurrency limits
- Pydantic schema validation (IPs, subnet consistency, range ordering, failover mode enforcement)
- Single-process GET script construction, PowerShell output parsing, and normalization
- Diff-based update semantics (only changed sections trigger cmdlets)
- Runtime environment guard behavior
- GET/PUT parity contract — the main guard against Crossplane reconciliation loops
- Helm-rendered Crossplane Request contract
- Security checks for PowerShell escaping and response sanitization
- CI validator: all validators, YAML deep-merge, cluster discovery (old and new layouts), JSON reporter

The repository virtualenv is preferred because the system Python may not have runtime dependencies such as `pydantic-settings` installed.

## Operational Notes

- This service must run on a Windows host with DHCP cmdlets available.
- Linux / macOS / WSL requests to scope endpoints return a structured `503` with a `reason` field.
- `/healthz` is always safe to call regardless of OS.
- Scope deletion is fail-safe: failover is detached before scope removal to prevent orphaned relationships.
- If failover detach fails, the delete is retried on the next Crossplane reconciliation cycle.
