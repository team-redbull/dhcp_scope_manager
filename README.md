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

**Where the API runs is a config choice, not a fixed property.** `DHCP_TRANSPORT`
selects it:

- `psrp` (production) — the API runs on **Linux**, as a container on OpenShift, and
  sends PowerShell text to the Windows DHCP server over WinRM. No PowerShell is
  installed on the API host and none executes there; the cmdlets run in a runspace on
  the DHCP server and return JSON. **Nothing this project ships runs on the Windows
  box** — that is the whole reason WinRM is in the picture.
- `local` (default) — the API runs *on* the Windows DHCP server and invokes
  `powershell.exe` as a subprocess. This is what [test-env/](test-env/) deploys and
  what the default exists for; it is not the production topology.

The trade-off between the two is analysed in
[docs/api-host-architecture.md](docs/api-host-architecture.md).

A disposable Windows DHCP test environment for exercising the scope lifecycle
against real PowerShell lives in [test-env/](test-env/).

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
    scopes.py                path address and body validation (validate_scope, validate_scope_request)
  models/
    __init__.py              Re-exports all model types
    scope.py                 DhcpScopePayload — canonical request/response model
    failover.py              DhcpFailover — failover relationship configuration
    exclusion.py             DhcpExclusion — exclusion range
    list_response.py         DhcpScopeListResponse / DhcpScopeListError — GET /scopes response
    test_run.py              TestRunRequest / TestTarget / TestRun — the test-runner contract
  routers/
    __init__.py              Aggregates all sub-routers into a single router — main.py imports only this
    scopes.py                DHCP scope endpoints (POST/GET/PUT/DELETE /api/v1/scopes/{scope})
    health.py                /healthz runtime capability check
    testrunner.py            On-demand test execution (POST/GET /api/v1/test-runs)
  services/
    dhcp_service.py          Runtime guard (OS / PowerShell / DHCP cmdlets check)
    ps_executor.py           Async PowerShell command runner with timeout/error handling
    ps_parsers.py            Single-process GET script builder and PowerShell JSON normalization
    scope_service.py         Core scope lifecycle logic (create / get / update / delete)
    test_runner.py           Runs the pytest suite in a subprocess with a scrubbed environment
  utils/
    decorators.py            Async-aware lightweight logging decorator for service calls
    ip_utils.py              IP integer conversion and TimeSpan parsing helpers
    locks.py                 Async per-scope lock manager for serialized mutations

scripts/
  validate_changed_clusters.py  CI entry point — detects changed files via git diff, resolves
                                affected clusters (inheritance-aware), validates only those clusters
  validate_dhcp_values.py       Full validator — walks sites/ structure, validates folder layout,
                                YAML content, and DHCP business rules on the merged inheritance chain
  requirements.txt              Minimal CI dependencies (pydantic, PyYAML)

.github/
  workflows/
    test.yml                   CI — hermetic suite. Reusable: called by build.yml
    build.yml                  CI — runs test.yml, then builds and pushes the image to GHCR

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
  test_ps_transport.py           Local subprocess and PSRP transports (pypsrp faked — no network)
  test_windows_error_classification.py  Error classification against real captured Windows DHCP output
  test_diff.py                   Diff-based update logic
  test_dhcp_service.py           Runtime environment guard behavior
  test_parity.py                 GET/PUT parity — the main guard against Crossplane reconciliation loops
  test_edge_cases.py             Edge cases and boundary conditions
  test_security.py               PowerShell escaping and response sanitization
  test_service_unit.py           Focused scope_service create/get/delete/list behavior
  test_testrunner.py             Test-runner guards: env scrubbing, host refusal, redaction
  test_validate_dhcp_values.py        CI validator — structure, YAML checks, filtering, deep merge
  test_validate_changed_clusters.py   Changed-file detection, inheritance resolution, git integration
  integration/                   Live suite — real app, real PSRP, real DHCP server. Skipped
                                 unless DHCP_IT=1; see its README.md
```

## Runtime Requirements

The DHCP server is always Windows. What varies is where the API runs, selected
by `DHCP_TRANSPORT`.

### `DHCP_TRANSPORT=local` (default)

API and DHCP server on the same Windows host:

- Python 3.12+
- Windows host (native Windows, **not** Linux / macOS / WSL)
- `powershell.exe` on PATH
- DHCP PowerShell cmdlets (`Get-DhcpServerv4Scope`, etc.)
  - The DHCP Server role installed on this same host:
    `Install-WindowsFeature -Name DHCP -IncludeManagementTools`
  - Cmdlets are invoked without `-ComputerName`, so the service always manages
    the **local** DHCP server. RSAT on an administrative host is not sufficient —
    it provides the cmdlets but no local DHCP service for them to act on.

This is what `test-env/` deploys.

### `DHCP_TRANSPORT=psrp`

API on Linux, DHCP server on a separate Windows host:

- Python 3.12+ on any OS (Linux in production)
- `pypsrp` installed (already in `requirements.txt`)
- Network reachability to `DHCP_SERVER_HOST` on `WINRM_PORT` (5986/HTTPS —
  a single port, unlike the dynamic RPC range that `-ComputerName` needs)
- WinRM enabled on the DHCP server, with the DHCP Server role installed **there**
- Kerberos: a working `krb5.conf`, a keytab or credential cache, correct SPNs,
  and clock skew within tolerance

No PowerShell runs on the API host. Script text is sent to the Windows server,
which executes it in a real Windows runspace and returns a JSON string — so the
`DhcpServer` module is never needed locally. See
[docs/api-host-architecture.md](docs/api-host-architecture.md) for the full
rationale.

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

### Transport

| Variable                            | Default   | Description                                                                       |
| ----------------------------------- | --------- | --------------------------------------------------------------------------------- |
| `DHCP_TRANSPORT`                    | `local`   | `local` = spawn `powershell.exe` here. `psrp` = run cmdlets on a remote Windows host. |
| `DHCP_SERVER_HOST`                  | _(empty)_ | Target DHCP server. **Required** when `DHCP_TRANSPORT=psrp`.                       |
| `WINRM_PORT`                        | `5986`    | WinRM port.                                                                       |
| `WINRM_USE_SSL`                     | `true`    | Use HTTPS for WinRM.                                                              |
| `WINRM_AUTH`                        | `kerberos`| `kerberos`, `ntlm` or `credssp`. Managing failover requires `credssp` — see [Security](#security). |
| `WINRM_USERNAME`                    | _(empty)_ | Required for `ntlm` / `credssp`. Optional for `kerberos` (an explicit principal).  |
| `WINRM_PASSWORD`                    | _(empty)_ | Required for `ntlm` / `credssp`. Leave unset with Kerberos — no password is stored.|
| `WINRM_CERT_VALIDATION`             | `true`    | Validate the WinRM TLS certificate.                                               |
| `WINRM_CONNECTION_TIMEOUT_SECONDS`  | `30`      | WinRM connection timeout.                                                         |
| `WINRM_RUNSPACE_MAX_IDLE_SECONDS`   | `60`      | Reopen a pooled WinRM session rather than reuse it after this long idle.          |

**Pooled sessions expire on purpose.** The API caches open WinRM runspaces, but
the *server* decides how long its half lives: HTTP.SYS reaps an idle connection
after 120s by default, a GPO may shorten WinRM's own `IdleTimeout`, and a CredSSP
security context has its own lifetime. Whichever expires first leaves this side
holding a session the server has forgotten, and the next command on it fails —
under CredSSP with `Server did not response with a CredSSP token after step
Credential exchange`. The signature is unmistakable: **the first request after an
idle gap fails, the identical retry succeeds, and it then works until traffic
pauses again**, on every verb including `GET`.

`WINRM_RUNSPACE_MAX_IDLE_SECONDS` is the client giving up on a session before the
server does, so it must stay *below* the shortest server-side timeout. If the
"failed after Ns idle, retrying" warning keeps appearing, lower it to under the
`N` it reports. The transport also retries once on a fresh session, which covers
the timeouts this setting cannot predict.

`DHCP_SERVER_HOST` is deliberately a single explicit host, not a DNS alias or
load balancer. Under failover either peer can answer a read, and replication lag
or drift on the partner would make `GET` differ from the desired `PUT` body —
Crossplane would then issue corrective `PUT`s in a loop. See
[Reconciliation Contract](#reconciliation-contract).

Misconfiguration fails at startup, not per request: `DHCP_TRANSPORT=psrp`
without `DHCP_SERVER_HOST`, or `WINRM_AUTH=ntlm` without credentials, raises
immediately.

A `.env` file in the repo root is also supported.

## Run the API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Container

```bash
docker build -t dhcp-scope-manager .
docker run -p 8080:8080 \
  -e DHCP_TRANSPORT=psrp -e DHCP_SERVER_HOST=<windows-dhcp-host> \
  -e WINRM_AUTH=ntlm -e WINRM_USERNAME=Administrator \
  -e WINRM_PASSWORD=<password> -e WINRM_CERT_VALIDATION=false \
  dhcp-scope-manager
```

The image is Linux-only and contains no PowerShell — it is built for
`DHCP_TRANSPORT=psrp`, where the cmdlets execute on the remote Windows host.
Kerberos client libraries are included so switching `WINRM_AUTH` to `kerberos`
is a config change rather than a rebuild.

It runs as an arbitrary UID with GID 0, which is what OpenShift's `restricted-v2`
SCC assigns — so it does **not** depend on the `USER` in the Dockerfile being the
UID it actually gets.

### Deployment

Pushes are built and published to `ghcr.io/team-redbull/dhcp-scope-manager` by
the org-wide reusable workflow (`team-redbull/.github`), which also bumps the
image tag in the chart repo.

| | |
|---|---|
| Chart | `team-redbull/helm-charts-dhcp-scope-manager` |
| Argo app | `gitops/services/prod/dhcp-scope-manager` in `redbull-platform` |
| Namespace | `dhcp-scope-manager` |

The chart deliberately keeps the WinRM password in a Secret rather than in
values; see the chart's README.

## API Endpoints

Base path: `/api/v1`

`{scope}` is always the IPv4 network address of the scope (e.g. `10.20.30.0`), and it is
the **only** place that address appears in a request. It is identity rather than state, so
the request body carries no `scope` (or `network`) field — a body that includes one is
rejected with `422`. `GET /api/v1/scopes` is the exception: list items have no URL of their
own, so each carries a leading `scope` field to identify itself.

All `/api/v1/scopes*` endpoints share two implicit checks that run before the handler:

- **Auth** — rejects requests when `DHCP_API_TOKEN` is set and the token is missing or wrong. Returns `401`. `/healthz` is deliberately outside this check, because it backs the readiness probe and a kubelet cannot send a token.
- **Environment guard** — rejects requests when DHCP automation cannot run. Under `local` that means the wrong OS, missing PowerShell, or no DHCP cmdlets; under `psrp` it means `pypsrp` missing, WinRM unreachable or unauthenticated, or no DHCP cmdlets on the target host. Returns `503`.

There is one more group of routes, used only for on-demand test execution — see
[Air-gapped environments](#air-gapped-environments):

| Verb   | Path                       | Description                                                        |
| ------ | -------------------------- | ------------------------------------------------------------------ |
| `POST` | `/api/v1/test-runs`        | Run the whole suite against the DHCP server named in the body → `202` + `runId` |
| `GET`  | `/api/v1/test-runs/{id}`   | Status, exit code and redacted output of one run                    |
| `GET`  | `/api/v1/test-runs`        | Recent runs, summaries only                                         |

These take **auth but not the environment guard**: they drive a DHCP server named
in the request rather than this deployment's own, so a release that manages no
scopes (and returns `503` from every scope route) can still run tests.

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
- `error.details` contains sanitized structured context such as `scope`, validation errors, or DHCP environment `reason`.

Raw PowerShell commands, stack traces, and full internal stderr are not returned to clients. Backend logs use safe context such as request path, `scope`, operation name, return code, and sanitized stderr previews.

Common error codes:

| HTTP Status | Error Code                     | Meaning                                                        |
| ----------- | ------------------------------ | -------------------------------------------------------------- |
| `400`       | `INVALID_SCOPE`                | `{scope}` is not a valid IPv4 address                          |
| `401`       | `UNAUTHORIZED`                 | Missing or invalid bearer token                                |
| `404`       | `SCOPE_NOT_FOUND`              | DHCP scope does not exist                                      |
| `409`       | `DHCP_CONFLICT`                | Windows DHCP reported an unsafe already-exists/in-use conflict |
| `409`       | `IMMUTABLE_FIELD`              | `subnetMask` differs from the scope on the server — not changeable in place |
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
- Per-scope assembly errors (invalid data, missing DNS option, unrecognized field format) are caught individually. The broken scope is added to `errors` with its `scope` and a description; all other scopes are returned normally in `scopes`.

```json
{
  "scopes": [{ "scope": "10.20.30.0", "scopeName": "...", "...": "..." }],
  "errors": [
    {
      "scope": "10.20.31.0",
      "error": "No DNS servers configured for this scope"
    }
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

### `POST /api/v1/scopes/{scope}`

Creates the scope if it does not exist. If it already exists, POST converges the
**whole** desired state by running the same diff `PUT` runs. Idempotent — never
fails because the scope is already present, and a POST of state that already
matches issues no cmdlets at all.

> **POST on an existing scope converges every field.** The response body always
> describes what the server now holds, which for an existing scope is what the
> request asked for.
>
> This was not always true. POST used to skip `Add-DhcpServerv4Scope` when the
> scope existed — the only cmdlet on the create path that writes `scopeName`,
> `leaseDurationDays`, `description`, `startRange` and `endRange` — so those five
> fields were silently discarded, exclusions could be added but never removed,
> and a `gateway` of `null` did not clear DHCP option 3. The call still returned
> `200`, echoing the *old* state, so a caller had no way to tell the difference
> between "converged" and "ignored".
>
> Sharing one convergence routine with `PUT` means the two verbs cannot drift
> apart: whichever one reaches an existing scope converges it identically.
> Retry-after-partial-create is strictly safer than before — the retry now
> repairs whatever the failed attempt left half-written, instead of leaving it.
>
> Two consequences worth knowing:
>
> - A POST that changes `subnetMask` on an existing scope now returns `409`
>   `IMMUTABLE_FIELD`, exactly as `PUT` does. Windows has no in-place mask change
>   (see the `PUT` section), and silently ignoring the field is what this change
>   set out to remove.
> - POST is still the *create* path in GitOps terms. Crossplane only issues it
>   after a `GET` returns `404`; ordinary drift is caught by `GET` and repaired
>   with `PUT`.

| Status | Body                                                               | When                                                                        |
| ------ | ------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `200`  | `DhcpScopeBody`                                                    | Scope created or already present and converged                              |
| `400`  | Standard error body with `INVALID_SCOPE`                           | `{scope}` is not a valid IPv4 address                                       |
| `401`  | Standard error body with `UNAUTHORIZED`                            | Bad or missing bearer token                                                 |
| `409`  | Standard error body with `DHCP_CONFLICT`                           | Unsafe existing/in-use DHCP state                                           |
| `422`  | Standard error body with `VALIDATION_ERROR`                        | Request body fails Pydantic field constraints                               |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`               | PowerShell cmdlet failed                                                    |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE`            | Host cannot run DHCP automation                                             |
| `504`  | Standard error body with `POWERSHELL_TIMEOUT`                      | PowerShell command timed out                                                |

---

### `GET /api/v1/scopes/{scope}`

Returns the current canonical state of the scope. When Crossplane sees a `404` here it issues `POST` to create the scope.

The backend builds a single script that runs all required DHCP cmdlets in-process (one PowerShell process):

1. `Get-DhcpServerv4Scope -ScopeId ...`
2. `Get-DhcpServerv4OptionValue -ScopeId ...`
3. `Get-DhcpServerv4ExclusionRange -ScopeId ...`
4. `Get-DhcpServerv4Failover -ScopeId ...`

`options` and `exclusions` are array-wrapped in PowerShell so single-result output does not collapse into an object. Missing exclusions become `[]`, missing failover becomes `null`, missing scope becomes `404 SCOPE_NOT_FOUND`. Any other cmdlet failure is re-thrown rather than silently returning empty state. The path address is validated as an IPv4 address and inserted through a central PowerShell single-quote literal helper.

| Status | Body                                                    | When                                                       |
| ------ | ------------------------------------------------------- | ---------------------------------------------------------- |
| `200`  | `DhcpScopeBody`                                         | Scope found                                                |
| `400`  | Standard error body with `INVALID_SCOPE`                | `{scope}` is not a valid IPv4 address                      |
| `401`  | Standard error body with `UNAUTHORIZED`                 | Bad or missing bearer token                                |
| `404`  | Standard error body with `SCOPE_NOT_FOUND`              | Scope does not exist on the DHCP server                    |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`    | PowerShell cmdlet failed for a reason other than not-found |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE` | Host cannot run DHCP automation                            |
| `504`  | Standard error body with `POWERSHELL_TIMEOUT`           | PowerShell command timed out                               |

---

### `PUT /api/v1/scopes/{scope}`

Diff-based convergence — compares the current scope state to the desired payload and issues only the PowerShell cmdlets needed to reconcile the difference.

| Changed fields                                                            | PowerShell cmdlet                                                                            |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `startRange`, `endRange`                                                  | `Set-DhcpServerv4Scope` — written first, in its own call, routed through the union (see below) |
| `scopeName`, `leaseDurationDays`, `description`                           | `Set-DhcpServerv4Scope`                                                                      |
| `subnetMask`                                                              | none — rejected with `409 IMMUTABLE_FIELD` (see below)                                        |
| `gateway`, `dnsServers`, `dnsDomain`                                      | `Set-DhcpServerv4OptionValue`; `gateway: null` removes DHCP option 3                         |
| `nextServer`, `bootFile`                                                  | `Set-DhcpServerv4OptionValue -OptionId 66/67`; clearing both removes the options              |
| Exclusions added                                                          | `Add-DhcpServerv4ExclusionRange`                                                             |
| Exclusions removed                                                        | `Remove-DhcpServerv4ExclusionRange`                                                          |
| Failover added / changed / removed                                        | `Add-DhcpServerv4Failover` / `Set-DhcpServerv4Failover` / `Remove-DhcpServerv4FailoverScope` |

**Why the range is written separately, and first.** `Set-DhcpServerv4Scope` applies the
name/lease/description half of a combined call even when it then rejects the range,
which would leave the scope matching neither the old nor the new desired state. Keeping
the range in its own call, ordered first, means a refused range aborts before anything
else has been written.

**Why a range change can take two calls.** Windows accepts a new range only when it is a
superset or a subset of the one the scope already holds. A mixed change — one edge moving
out while the other moves in — or a disjoint move is refused with "Failed to set IP
address range to a scope" (`DHCP 20023`). The service widens to the union of the current
and desired ranges first, then narrows to the desired range; each step is a superset or a
subset by construction. A pure widening or narrowing collapses to one call, so the common
case costs nothing extra. The intermediate union is briefly leasable — for a mixed change
every union address already belongs to the current or desired range, so only a fully
disjoint move exposes new addresses, and only for one cmdlet call. Deactivating the scope
first is not an alternative: range writes fail outright on an Inactive scope.

**Why `subnetMask` is refused.** `Set-DhcpServerv4Scope` has no `-SubnetMask` parameter;
changing a mask requires deleting and recreating the scope, which drops every active lease
on that subnet. A PUT that changes it returns `409 IMMUTABLE_FIELD` naming both the
observed and requested masks. Returning `200` without applying it would hide the drift and
make Crossplane re-send the same PUT on every reconcile loop. Creating a *new* scope with
any valid mask works normally — only the update path is affected.

| Status | Body                                                               | When                                                                        |
| ------ | ------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `200`  | `DhcpScopeBody`                                                    | Scope updated (or already at desired state — no-op)                         |
| `400`  | Standard error body with `INVALID_SCOPE`                           | `{scope}` is not a valid IPv4 address                                       |
| `401`  | Standard error body with `UNAUTHORIZED`                            | Bad or missing bearer token                                                 |
| `404`  | Standard error body with `SCOPE_NOT_FOUND`                         | Scope does not exist — Crossplane responds by issuing `POST`                |
| `409`  | Standard error body with `DHCP_CONFLICT`                           | Unsafe existing/in-use DHCP state                                           |
| `409`  | Standard error body with `IMMUTABLE_FIELD`                         | `subnetMask` differs from the scope on the server                           |
| `422`  | Standard error body with `VALIDATION_ERROR`                        | Request body fails Pydantic field constraints                               |
| `500`  | Standard error body with `POWERSHELL_COMMAND_FAILED`               | PowerShell cmdlet failed                                                    |
| `503`  | Standard error body with `DHCP_ENVIRONMENT_UNAVAILABLE`            | Host cannot run DHCP automation                                             |
| `504`  | Standard error body with `POWERSHELL_TIMEOUT`                      | PowerShell command timed out                                                |

---

### `DELETE /api/v1/scopes/{scope}`

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
| `400`  | Standard error body with `INVALID_SCOPE`                | `{scope}` is not a valid IPv4 address                  |
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

The POST/PUT request body and the single-scope GET response, identical by
construction — both are `DhcpScopeBody`. The scope address is absent on purpose:
it is identity, carried in the URL only (see [API Endpoints](#api-endpoints)).

```json
{
  "scopeName": "cluster-a-workers",
  "subnetMask": "255.255.255.0",
  "startRange": "10.20.30.50",
  "endRange": "10.20.30.200",
  "leaseDurationDays": 8,
  "description": "",
  "gateway": "10.20.30.1",
  "dnsServers": ["10.10.1.5", "10.10.1.6"],
  "dnsDomain": "lab.local",
  "nextServer": "boot.lab.local",
  "bootFile": "snponly.efi",
  "exclusions": [{ "startAddress": "10.20.30.1", "endAddress": "10.20.30.10" }],
  "failover": null
}
```

`GET /api/v1/scopes` returns these same objects with a leading `scope` field, since a
list item has no URL of its own to identify it:

```json
{ "scope": "10.20.30.0", "scopeName": "cluster-a-workers", "...": "..." }
```

- Field order is intentional and tested — GET response and PUT body are the same model.
- `failover` is either `null` or a full failover object (no partial objects).
- Exclusions are always returned sorted by IP (ascending). Values files must match this order.
- `dnsServers` must contain at least one IPv4 address. If GET observes a managed scope without DNS servers, the backend treats that as invalid managed state instead of returning a pretend-valid payload.
- `subnetMask` and `gateway` are **derived when omitted** — `255.255.255.0` and the subnet's `.254` address respectively. Writing a value, including `null` or `""`, is always honoured as written:

  | `gateway` | Result |
  | --------- | ------ |
  | key absent | derived `.254` (e.g. `10.20.30.254`) |
  | `null` or `""` | no DHCP option 3; GET returns `null` |
  | an IPv4 address | that address |

  A `subnetMask` other than `255.255.255.0` with no explicit `gateway` is rejected — there is no `.254` convention to fall back on outside a /24. The chart applies the same rule when it renders, so the PUT body and the GET response always agree.
- **Gateway-in-range guard**: if `gateway` is set to an IP inside `[startRange, endRange]` and is not covered by an exclusion, the request is rejected with `422 VALIDATION_ERROR`. An unexcluded gateway inside the distribution pool would be leased to a client, causing a network outage.
- DNS server order is preserved exactly (primary/secondary semantics — never sorted).
- `description` defaults to `""` (never `null`).
- `nextServer` / `bootFile` (DHCP options 66/67) are the PXE pair: option 66 names the boot server, option 67 the boot file. Both default to `""`, meaning the option is not set on the scope — that is the ordinary case for scopes whose hosts do not network-boot.

  **They are optional but both-or-nothing**: a request setting one without the other is rejected with `422 VALIDATION_ERROR`, because a boot server with no boot file (or the reverse) leaves a host silently unbootable. Both keys are always present in the body regardless — `""` is the concrete "not set" state GET reports back, so carrying them keeps the body and the GET response identical. In values files these come from the `pxe.server` / `pxe.bootfile` keys; see [docs/dhcp_values.md](docs/dhcp_values.md).

  Per-architecture boot files (BIOS vs UEFI) need Windows DHCP policies matching option 93 and are not modelled — a scope carries one 66/67 pair.

## Failover Model

Supported modes: `HotStandby`, `LoadBalance`

| Mode          | Required fields      | Normalized fields                                 |
| ------------- | -------------------- | ------------------------------------------------- |
| `HotStandby`  | `serverRole`         | `loadBalancePercent` → `0`                        |
| `LoadBalance` | `loadBalancePercent` | `serverRole` → `"Active"`, `reservePercent` → `0` |

Normalization in both the chart's template and the Pydantic model prevents GET/PUT drift
when values include cross-mode fields.

## The Crossplane Request CR

**Rendered by a chart in another repo** — `team-redbull/helm-charts-hostedclusters-setup`.
This repo is the DHCP API; it does not carry the chart, because one values file has to
create both the hosted cluster and its DHCP scope, so the template belongs with the
cluster's chart rather than with the service it calls.

What matters on this side is the contract the rendered CR holds the API to. Crossplane
compares the `GET` response against `payload.body` roughly every 60 seconds and PUTs on
any difference, so these properties are load-bearing here even though the file that
produces them lives elsewhere:

- **Object name** comes only from `dhcp_values.network` (`dhcp-scope-10-20-30-0`).
  Changing `scopeName` does not create a new CR or delete the live scope.
- **The scope address is in `payload.baseUrl`, never in `payload.body`** — identity is
  the URL, state is the body. That is what makes the body and the GET response directly
  comparable.
- **`payload.body` is a JSON string**, written field by field so the canonical field
  order in [Canonical Payload Shape](#canonical-payload-shape) survives — piping a map
  through `toJson` would sort the keys and break it.
- **Derived defaults are resolved at render time**, not passed through as omissions.
  `subnetMask` → `255.255.255.0`, `gateway` → the subnet's `.254`, `scopeName` → the
  hosted cluster's own name. GET reports the concrete value the DHCP server holds, so a
  body that said `null` would diff forever. A non-/24 mask with no explicit gateway
  fails the render.
- **`scopeName` comes from the cluster's values-file name.** One values file is one
  hosted cluster is one DHCP scope, so `<cluster>.yaml` already carries the name; writing
  it in the file is only an override. Helm has no notion of which file a value came from,
  so the ApplicationSet injects it as a `clusterName` Helm parameter — deliberately a
  chart-owned key rather than `--set dhcp_values.scopeName`, since parameters outrank
  `valueFiles` and would make an explicit value impossible to honour.
- **The template is gated on `dhcp_values.network`**, the one key only a cluster's own
  file sets, so a cluster with no DHCP block renders nothing instead of failing. It
  cannot be gated on `scopeName` any more — that now resolves for every cluster.
- **Bearer token** renders as `Authorization: "Bearer {{ name:namespace:key }}"`, which
  provider-http resolves against the live Secret at reconcile time, keeping the token out
  of git. All three of `dhcp_api.tokenSecretRef.{name,namespace,key}` or none.

The chart repo's `tests/test_render_parity.py` asserts the rendered body equals the
payload shape below; this repo's `tests/test_parity.py` asserts `GET` returns it. Neither
imports the other — both pin the documented shape — so **changing the payload shape means
changing it in both repos**, and a local test run here will not catch the other half.

## HTTP Response Codes

Quick reference — see the per-endpoint tables above for the exact set each route can return.

| Code  | Meaning               | Error code examples                                                                  |
| ----- | --------------------- | ------------------------------------------------------------------------------------ |
| `200` | OK                    | Success response: `DhcpScopeBody`, `DhcpScopeListResponse`, or `{"status": "ok"}`   |
| `204` | No Content            | Success response with empty body — DELETE only                                       |
| `400` | Bad Request           | `INVALID_SCOPE`                                                                      |
| `401` | Unauthorized          | `UNAUTHORIZED`                                                                       |
| `404` | Not Found             | `SCOPE_NOT_FOUND` — GET and PUT only                                                 |
| `409` | Conflict              | `DHCP_CONFLICT`, `IMMUTABLE_FIELD` — PUT only                                        |
| `422` | Unprocessable Entity  | `VALIDATION_ERROR`                                                                   |
| `500` | Internal Server Error | `POWERSHELL_COMMAND_FAILED`, `INTERNAL_ERROR`                                        |
| `503` | Service Unavailable   | `DHCP_ENVIRONMENT_UNAVAILABLE`                                                       |
| `504` | Gateway Timeout       | `POWERSHELL_TIMEOUT`                                                                 |

## Reconciliation Contract

Crossplane reconciles every ~60 seconds: GET current state → compare to desired PUT body → issue PUT on any diff.

Rules that must hold to prevent infinite reconciliation loops:

- GET response must be identical to the desired PUT body when no change is intended.
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
sites/configValues.yaml
  → sites/<site>/values.yaml
    → sites/<site>/mces/<mce>/values.yaml
      → sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml
```

`configValues.yaml` is a real merge layer, not just a file that must exist: a cluster may
inherit `leaseDurationDays`, `dns.servers` or a `failover` relationship from it, and the
validator merges it first so those are not reported as missing.

### What it validates

| Layer          | Checks                                                                                                                                                   |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Global         | `sites/` exists; `configValues.yaml` present and valid YAML (may be empty)                                                                               |
| Site           | Directory has `values.yaml` (valid YAML, **may be empty**) and `mces/`                                                                                   |
| MCE            | Directory has `values.yaml` (valid YAML, **may be empty**) and `hostedClusters/`                                                                         |
| Hosted cluster | `.yaml`/`.yml` extension; valid YAML; **must not be empty**                                                                                              |
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

The `.gitlab-ci.yml` at the repo root runs on every MR that touches a YAML file inside `sites/`.
It uses `validate_changed_clusters.py` so only the clusters affected by the diff are validated —
not the entire repo:

```yaml
validate_dhcp_values:
  stage: validate
  image: python:3.12-slim
  before_script:
    - pip install --quiet -r scripts/requirements.txt
    - git fetch origin $CI_MERGE_REQUEST_TARGET_BRANCH_NAME
  script:
    - >
      python3 scripts/validate_changed_clusters.py
      --base-ref origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME
      --sites-dir sites
      --verbose
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes:
        - sites/**/*.yaml
```

The `git fetch` is required because GitLab MR pipelines use a shallow clone — without it,
`git diff origin/<target>...HEAD` has nothing to compare against.

**What gets validated per change:**

| Changed file                                      | Clusters validated          |
| ------------------------------------------------- | --------------------------- |
| `sites/<site>/mces/<mce>/hostedClusters/<c>.yaml` | Only `<c>`                  |
| `sites/<site>/mces/<mce>/values.yaml`             | All clusters under `<mce>`  |
| `sites/<site>/values.yaml`                        | All clusters under `<site>` |
| `sites/configValues.yaml`                         | Full repo scan              |

To run the same check locally against `main`:

```bash
python3 scripts/validate_changed_clusters.py --base-ref origin/main --verbose
```

Dependencies are installed with `pip install -r scripts/requirements.txt` (pydantic + PyYAML).

## Security and Safety

- Bearer token auth via `DHCP_API_TOKEN` — optional; disabled when unset. `/healthz` is
  the one exemption: it is the readiness probe, and a kubelet probe cannot send a token
- Runtime environment guard rejects all scope operations on non-Windows / non-DHCP hosts
- `-ErrorAction Stop` on every PowerShell command
- PowerShell stderr is sanitized before returning to clients and before logging previews
- Structured JSON logs include safe fields on every entry: `scope` (auto-extracted from the call when the function accepts it), `operation` (function name), `duration_ms`, `status` (`ok`/`error`), `relationship_name`, `returncode`, `stderr_preview`, and `error_code`

## Debugging Errors

From Crossplane events:

1. Read `error.code` first. It is stable and safe to use for automation.
2. Use `error.message` for the short human explanation.
3. Use `error.details` for safe context such as `scope`, body validation fields, or DHCP environment `reason`.

From backend logs:

- `AppError` entries mean the request failed in an expected, client-safe way.
- `RequestValidationError` entries include sanitized validation fields and messages, not raw input values.
- `PowerShellError` entries include return code, operation name, `scope` when available, and sanitized stderr preview.
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

The repository virtualenv is preferred because the system Python may not have runtime dependencies such as `pydantic-settings` installed.

The suite has two tiers, and the split is load-bearing.

### Hermetic suite — `tests/`

No DHCP server, no network, runs anywhere in seconds. Covers:

- Endpoint contracts and HTTP status codes
- Async runtime behavior, subprocess timeout handling, and concurrency limits
- Pydantic schema validation (IPs, subnet consistency, range ordering, failover mode enforcement)
- Single-process GET script construction, PowerShell output parsing, and normalization
- Diff-based update semantics (only changed sections trigger cmdlets)
- Runtime environment guard behavior
- GET/PUT parity contract — the main guard against Crossplane reconciliation loops
- Security checks for PowerShell escaping and response sanitization
- CI validator: all validators, YAML deep-merge, cluster discovery (old and new layouts), JSON reporter

It mocks the PowerShell transport end to end, which is what makes it hermetic and
also what bounds it: it proves which cmdlet *strings* the service builds, never
what Windows does with them.

### Live suite — `tests/integration/`

The real ASGI app over a real PSRP connection to a Windows DHCP server. **Skipped
unless `DHCP_IT=1`** — nothing happens without the opt-in. It owns scope
`10.77.88.0` alone and deletes it before and after every test, so it is
order-independent and safe to re-run after a failure.

It exists because anything that depends on real server behaviour is invisible to
the mocked suite: the range-transition rules Windows enforces, whether a cleared
option 66/67 is actually removed, what a cmdlet really persists. Two shipped bugs
lived in exactly that blind spot — POST silently discarding half its payload on an
existing scope, and an under-specified PUT wiping PXE options and exclusions.

Setup and connection recipes: [tests/integration/README.md](tests/integration/README.md).

```bash
DHCP_IT=1 DHCP_TRANSPORT=psrp DHCP_SERVER_HOST=... pytest tests/integration -v
```

### CI

`.github/workflows/test.yml` runs the hermetic suite: Python 3.12 (matching the
Dockerfile, so a green run means green in the image that ships), then `pytest -q`.

**It is a reusable workflow with no trigger of its own.** `build.yml` calls it and
declares `needs: test` on the image build, which does two things at once:

- **A red suite produces no image.** The GHCR push is gated on the tests, not run
  alongside them.
- **The suite runs once per commit.** `build.yml` already fires on push to every
  branch, so a `pull_request:` trigger here would run everything a second time on
  the same SHA. Check runs attach to a commit, not to an event, so the push-triggered
  run is the one a PR displays — coverage is unchanged. The exception is fork PRs,
  where no push event reaches this repo; add `pull_request:` back to `test.yml` if
  the repo ever accepts them.

`workflow_dispatch` is kept so the suite can still be run by hand from the Actions
tab without pushing.

The job needs no toolchain beyond Python. Chart rendering is tested in
`team-redbull/helm-charts-hostedclusters-setup`, which runs its own workflow with
`helm` installed — see [The Crossplane Request CR](#the-crossplane-request-cr).

Dependencies are installed from both `requirements.txt` and
`scripts/requirements.txt`. The second is not redundant: `tests/test_validate_*.py`
import the validator scripts, which need PyYAML, and that reaches the environment
today only as a transitive dependency of `uvicorn[standard]`.

The live suite is deliberately **not** run in CI. It needs a Windows DHCP server
to write to, and the test environment is brought up on demand rather than kept
running, so the job would fail for reasons that have nothing to do with the change
under test. `DHCP_IT` is never set in the workflow.

`.gitlab-ci.yml` at the repo root is a separate pipeline for the *values* repo (see
[GitLab CI](#gitlab-ci) under DHCP Values Validation). It validates YAML under
`sites/`, which this repository does not contain.

### Air-gapped environments

There is no CI on the air-gapped side, and no package index to install a test
runner from. Instead **the deployed API runs its own suite on request**: the image
already installs `pytest` from `requirements.txt` and carries `tests/`, so nothing
extra has to cross the air gap.

Deploy a second release of the chart whose only job is running tests:

```bash
helm install dhcp-scope-manager-tests . -f values-test.yaml -n dhcp-scope-manager
oc port-forward svc/dhcp-scope-manager-tests 8080:8080 -n dhcp-scope-manager &

# One token for both releases — the test release reads the production release's
# Secret rather than minting a second credential, which is why it installs into
# the same namespace.
TOKEN=$(oc get secret dhcp-scope-manager-api-token -n dhcp-scope-manager \
          -o jsonpath='{.data.api-token}' | base64 -d)
```

The release name differs from production's, so every resource resolves to
`dhcp-scope-manager-tests` and nothing collides. Install the production release
first: the test pod mounts its Secret and will not start without it.

Start a run. **The DHCP server under test is named in the request, never stored in
the chart** — so no values file can aim the destructive live suite anywhere:

```bash
curl -sX POST localhost:8080/api/v1/test-runs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "target": {
          "dhcpServerHost": "dhcp-test.lab.local",
          "winrmUsername": "svc-dhcp-test",
          "winrmPassword": "...",
          "winrmAuth": "ntlm",
          "winrmCertValidation": false
        }
      }'
# → 202 {"runId":"a1b2c3d4e5f6","status":"running", ...}

curl -s localhost:8080/api/v1/test-runs/a1b2c3d4e5f6 -H "Authorization: Bearer $TOKEN"
# → {"status":"passed","exitCode":0,"output":"721 passed, 30 passed in 41.2s", ...}
```

`202` and polling rather than one blocking call: a live run takes minutes, far
longer than an HTTP request should be held open.

**Every run is the whole suite** — the mocked tests and `tests/integration`
together. There is no suite selector: with one, a green run meant two different
things depending on a field nobody looked at afterwards, and the cheap option
verified nothing about the server the release was pointed at. `target` is
therefore **required**, and a request without one is `422` rather than a run that
quietly skips the live half (`tests/integration` self-skips unless `DHCP_IT` is
set, which only a target sets). A body still carrying `suite` is rejected by
`extra="forbid"`, not ignored.

#### What stops this being dangerous

| Guard | Effect |
| --- | --- |
| Environment is rebuilt, not inherited | The subprocess gets no `DHCP_*`/`WINRM_*` from the pod, so a mistyped target cannot silently fall back to the server this release manages |
| Protected hosts refused | A target matching `dhcp.serverHost`, or any `testRunner.denyHosts` entry, returns `422 TEST_TARGET_REFUSED` before anything starts |
| Token required | Generated into a Secret by the chart; the app treats an empty token as auth disabled, so the chart never leaves one empty |
| Credentials not persisted | `winrmPassword` is a `SecretStr` held only for the subprocess — never in the run registry, a response, or a log |
| Output redacted | Captured pytest output has the run's secrets stripped before it is returned |
| One run at a time | The suite owns a single scope; concurrent runs would delete each other's state |

The live suite creates and deletes exactly one scope, `10.77.88.0`, and touches
nothing else.

#### Why a separate release

The route exists on every deployment of this image, including production. Running
it there would put a full pytest run — and its memory spike — inside the pod
Crossplane reconciles every hosted cluster's scope through. `values-test.yaml`
gives it its own pod, its own limits, and no DHCP credentials at rest.

That release sets `dhcp.transport: local`, so its own `/api/v1/scopes/*` routes
return `503`. That is correct: it exists to run tests, not to manage DHCP. Its
readiness probe is TCP rather than `/healthz` for the same reason.

**Failover still cannot be tested on a single DHCP server.** Windows failover is a
relationship between two *distinct* servers — `Add-DhcpServerv4Failover` refuses a
partner that is the local server. The failover logic is covered by 73 hermetic
tests; what stays unverifiable without a partner is the CredSSP double hop and
replication itself.

## Operational Notes

- The **DHCP server** is always Windows. The **API** is Windows-only under
  `DHCP_TRANSPORT=local`; under `psrp` it runs on Linux and needs no PowerShell,
  no DHCP cmdlets and no Windows anything.
- Under `local` only, requests to scope endpoints from Linux / macOS / WSL return a
  structured `503` with a `reason` field. Under `psrp` the OS check does not run —
  the guard validates the WinRM target instead
  ([app/services/dhcp_service.py](app/services/dhcp_service.py)).
- `/healthz` is always safe to call regardless of OS or transport.
- Scope deletion is fail-safe: failover is detached before scope removal to prevent orphaned relationships.
- If failover detach fails, the delete is retried on the next Crossplane reconciliation cycle.
