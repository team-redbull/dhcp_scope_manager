# Where the API Runs: Host Architecture and the Linux/PSRP Question

**Status:** Implemented on `refactor/linux-api` as of 2026-07-28. Option C is
available behind `DHCP_TRANSPORT=psrp`; `local` remains the default, so the
co-located test environment is unaffected. The Kerberos spike in §9 has **not**
been run — do that before switching production over.
**Date:** 2026-07-27 (analysis), 2026-07-28 (implementation)
**Scope:** Where the FastAPI service should run in production, and whether it
can run on Linux.

---

## 1. Summary

| Question                                          | Answer                                                  |
| ------------------------------------------------- | ------------------------------------------------------- |
| Can the API run on Linux today?                   | **No.** It shells out to local `powershell.exe`.        |
| Should the API be co-located with a DHCP server?  | **No, in production.** Failover makes it incoherent.    |
| Can Linux drive Windows DHCP over WinRM/PSRP?     | **Yes.** This is a common, proven pattern.              |
| Recommendation for production                     | **Linux + PSRP**, after a Kerberos spike.               |
| Recommendation for the test environment           | **Keep co-located.** Simpler, and it constrains nothing. |

The single biggest technical risk in a Linux migration — PSRP object
deserialization — **does not apply to this codebase**, because every command
already returns a JSON string. See §6.

---

## 2. Current state

The service can only manage the DHCP server on its own host, and that host must
be Windows.

| Fact                                        | Evidence                                  |
| ------------------------------------------- | ----------------------------------------- |
| Spawns local `powershell.exe` as subprocess | `app/services/ps_executor.py:94`          |
| Startup env check does the same             | `app/services/dhcp_service.py:120`        |
| No `-ComputerName` anywhere in the codebase | verified by grep across `app/` and `tests/` |
| Single execution entrypoint                 | `run_ps()` at `app/services/ps_executor.py:47` |
| Callers routed through it                   | 21 in `scope_service.py`, 1 in `ps_parsers.py` |

Consequences:

- The `DhcpServer` PowerShell module is a CIM/CDXML wrapper over the Windows
  DHCP service. It does not exist on Linux, and `pwsh` on Linux cannot provide
  it.
- RSAT on a Windows administrative host is **not** sufficient either: it
  supplies the cmdlets, but with no `-ComputerName` they act on a local DHCP
  service that is not there. The startup check would pass and every operation
  would then fail or act on nothing. (An incorrect claim to the contrary was
  removed from `dhcp_service.py` and `README.md` on 2026-07-27.)

Crossplane already reaches the API over the network — `apiServer.url` in
`helm/values.yaml`, default `https://dhcp-api.lab.local` — so the API is
already a network service. Its host location is a free architectural choice.

---

## 3. Why co-location is wrong in production

Production runs at least two DHCP servers in a failover relationship. If the
API runs on DHCP-A:

1. **Peer symmetry is broken.** Two servers designed as equals no longer are;
   DHCP-A becomes special in a way the failover design does not account for.
2. **The control plane becomes less available than the data plane.** DHCP-A
   fails → DHCP keeps serving addresses via failover, but Crossplane can no
   longer reconcile anything until the API host is rebuilt. The management
   plane should not be the weakest link in managing a highly-available service.
3. **Blast radius.** A uvicorn crash-loop, memory leak, or bad deploy lands on
   a production DHCP server. Every API patch means touching DHCP.

None of this applies to the disposable test environment in `test-env/`, where
co-location is the simplest correct choice.

---

## 4. The "Linux can't do this" misconception

This belief is half right, and the wrong half is the one people act on.

**True.** Linux cannot run the `DhcpServer` module. Installing `pwsh` on RHEL
and running `Get-DhcpServerv4Scope` fails. The module requires Windows.

**False.** That this still applies when using WinRM/PSRP.

With PSRP, **no PowerShell executes on Linux.** Linux is a protocol client:

```
Linux (FastAPI + pypsrp)                    Windows DHCP server
        |                                             |
        |  HTTPS/5986, script text  ----------------> |
        |                             Windows PowerShell runspace
        |                             DhcpServer module loaded
        |                             ConvertTo-Json -Depth 10
        |  <-------------------------  JSON string    |
```

The Windows host executes the cmdlets in a real Windows runspace. Linux never
needs the module because Linux never runs the cmdlet. It sends text and parses
a JSON string.

**Existence proof:** Ansible's entire Windows story is this pattern — Linux
control node, `pypsrp`/`pywinrm`, managing Windows hosts including DHCP, at
scale, for years.

**Where the confusion comes from:** doing it via `pwsh` *on Linux*
(`Invoke-Command -ComputerName` from Linux PowerShell) genuinely is unreliable.
Cross-platform pwsh remoting over WinRM has real gaps and Microsoft steers
users toward SSH remoting. The robust path is a Python library that implements
the protocol directly and never involves `pwsh` at all — which suits a service
that is already Python.

---

## 5. Options considered

### A. Co-located on a DHCP server (current design)

- **Pros:** no remoting, no auth plumbing, works today, zero code change.
- **Cons:** all of §3. Windows host in an otherwise Linux estate. Correct for
  the test box, wrong for production.

### B. Windows administrative host + `-ComputerName`

Cmdlets run on a dedicated Windows host and target the DHCP server remotely.

- **Pros:** smaller change than PSRP — thread a target host through `run_ps`,
  no new transport. Kerberos is implicit in a domain.
- **Cons:** the `DhcpServer` module's `-ComputerName` uses **RPC/DCOM with
  dynamic high ports**, which is difficult to get through a segmented or
  air-gapped firewall review. Still adds a Windows VM to a Linux estate.

### C. Linux + PSRP over WinRM — **recommended**

FastAPI on Linux, `pypsrp` to the DHCP server, cmdlets execute Windows-side.

- **Pros:** matches the existing Linux estate (patching, monitoring, log
  shipping, deployment tooling all become uniform). **One port, 5986/HTTPS** —
  far easier through segmentation than option B's dynamic RPC range.
  Containerizable alongside the rest of the platform.
- **Cons:** Kerberos plumbing (§7.1), air-gap vendoring (§7.2), connection
  pooling (§7.3).

---

## 6. Why this codebase is unusually well suited to PSRP

The classic PSRP migration hazard is that remoting returns **deserialized**
objects (`Deserialized.System.Object`) whose behaviour differs from local ones.
Ported code that touches object properties or methods tends to break subtly.

**That hazard cannot apply here.** Every command already terminates in
`ConvertTo-Json` and the result is parsed as a **string**:

- default append in `ps_executor.py:81`
- explicit `-Depth 10 -Compress` in `ps_parsers.py:170`, `ps_parsers.py:226`,
  `scope_service.py:424`

Strings survive PSRP serialization intact. Therefore the entire parsing layer
is untouched by a transport change:

- `_extract_ip_str()` and the `IPAddressToString` handling (CLAUDE.md §8)
- TimeSpan parsing, single-element array collapse (`ps_parsers.py:121`)
- failover `null`/object consistency
- exclusion sorting and all canonical-shape guarantees

The change is confined to the body of `run_ps()` and the startup checks.

---

## 7. Real costs and risks

### 7.1 Kerberos — the main one

The work is not the transport, it is authentication. Required on the Linux
host: `krb5.conf`, a keytab or credential cache, correct SPNs, DNS resolution
to the KDC, clock skew within tolerance. This is the most common source of
pain and the reason for the spike in §9.

NTLM works as a fallback but means storing a password — avoid if Kerberos is
achievable. Certificate auth is a third option.

### 7.2 Air-gapped dependency vendoring

`pypsrp` and its transitive dependencies must be mirrored into the internal
index or baked into the image. As of 2026-07-27 PyPI shows `pypsrp` at
**0.9.1** and `pywinrm` at **0.5.0**. Note `pypsrp` is pre-1.0 — pin it
explicitly and deliberately.

### 7.3 Connection pooling breaks a current assumption

Per-request PSRP session setup is slow, so a runspace pool is required. This
conflicts with the comment at `ps_parsers.py:16-17`:

> Inline rather than defined once server-side because each `run_ps` call is a
> fresh pwsh process with no shared state.

With a pooled runspace, state *can* persist between calls. Either keep helpers
inlined per call (simplest, preserves current semantics) or move them to a
session-initialisation script — but do so deliberately. Also revisit how
`POWERSHELL_MAX_CONCURRENCY` (`app/config.py`) maps onto pool size; today it
bounds concurrent processes, and it would come to bound concurrent runspaces.

### 7.4 Timeout and error semantics

`POWERSHELL_COMMAND_TIMEOUT_SECONDS` currently kills a local process
(`ps_executor.py:107-116`). Under PSRP this must become a protocol-level
operation timeout plus session teardown. `PowerShellError` /
`PowerShellTimeoutError` should keep their existing shapes so the exception
handlers and the sanitisation in `errors.py` are unaffected.

---

## 8. Open design question: which DHCP server is targeted?

**This is independent of Linux vs Windows and must be answered in either
design.** The codebase currently has no concept of a target host.

With failover, configuration is written to one server and the relationship
replicates it to the partner. So:

- **Writes** go to a designated primary.
- **Reads must come from a deterministically chosen server.** If GET can be
  served by either peer, transient replication lag or manual drift on the
  partner will produce a GET body that differs from the desired PUT body, and
  Crossplane will issue a corrective PUT in a loop. That directly violates the
  byte-identical GET/PUT requirement in CLAUDE.md §9.

Make the target an explicit configuration value, not an accident of DNS or
load balancing. Decide and document the behaviour when the primary is
unreachable — failing closed (503, Crossplane retries next cycle) is likely
correct and consistent with the existing delete semantics in CLAUDE.md §7.

---

## 9. Spike before committing

Roughly one day. Do this before any production decision.

**Setup:** one Linux host, `pypsrp`, one Windows DHCP server, Kerberos auth.

**Test:**

```python
# Success = parseable JSON with the expected fields, from Linux.
Get-DhcpServerv4Scope | ConvertTo-Json -Depth 10 -Compress
```

**Acceptance criteria:**

1. Kerberos authenticates without a stored password.
2. Returned payload parses as JSON and `IPAddress` fields appear as
   `{"IPAddressToString": ...}` dicts — i.e. `_extract_ip_str()` still applies
   unchanged.
3. A write cmdlet (`Add-DhcpServerv4Scope`) succeeds and is visible on the
   failover partner.
4. An induced cmdlet error surfaces as a non-zero result with usable stderr
   text, so `PowerShellError` semantics can be preserved.
5. Measured latency of a pooled call is acceptable versus the current local
   subprocess.

**If Kerberos fights you**, that is the signal to reconsider — option B
remains available and the analysis above still holds.

---

## 10. What was implemented

Confined surface, as predicted. Everything else stayed put.

| Change                                       | Location                        |
| -------------------------------------------- | ------------------------------- |
| Transport abstraction + local subprocess impl | `app/services/ps_transport.py` (new) |
| PSRP transport and runspace pool              | `app/services/psrp_pool.py` (new)    |
| `run_ps` delegates execution to the transport | `app/services/ps_executor.py`        |
| Transport-aware environment checks            | `app/services/dhcp_service.py`       |
| Target host + auth settings, validated at startup | `app/config.py`                  |
| Pool teardown on shutdown                     | `app/main.py` (lifespan)             |
| Pin `pypsrp==0.9.1` (lazy import)             | `requirements.txt`                   |

Rather than replacing the subprocess call, `run_ps` now calls
`get_transport().execute(...)`. Both transports return a `PsResult`
(`returncode`, `stdout`, `stderr`) and both raise `asyncio.TimeoutError` on
timeout, so the JSON parsing, error classification, logging, and
`PowerShellError` mapping in `run_ps` are shared verbatim. PSRP has no process
exit code, so an error stream is normalised to `returncode=1` — which keeps
`is_not_found_error()` / `is_already_exists_error()` matching on the same text.

**Explicitly unchanged:** `ps_parsers.py`, `scope_service.py`, all models, all
routers, the canonical payload shape, and every invariant in CLAUDE.md §2.

**Test result:** all 585 pre-existing tests passed **unmodified**, confirming
the abstraction boundary did not leak. 38 new tests cover transport selection,
settings validation, connection arguments, PSRP result mapping, pool
reuse/discard/timeout behaviour, the remote environment check, and `run_ps`
end-to-end over PSRP — including that `IPAddressToString` dicts still reach
`_extract_ip_str()` intact. Total: 623.

### Known limitations

- **Not yet run against a real Windows host.** The pypsrp interaction is
  covered only by fakes. The §9 spike remains the gate for production.
- **`asyncio.wait_for` cannot cancel a blocked worker thread.** On timeout the
  runspace is discarded rather than reused and closed in the background, so the
  caller sees the timeout immediately; the underlying thread may linger until
  WinRM I/O unblocks.
- **§7.3 resolved conservatively:** helpers stay inlined per call, so pooled
  runspaces share no state and current semantics are preserved exactly. Pool
  size is bounded implicitly by `POWERSHELL_MAX_CONCURRENCY` via the existing
  semaphore in `run_ps`.
- **§8 resolved:** `DHCP_SERVER_HOST` is a single explicit host. Behaviour when
  it is unreachable is fail-closed (503), consistent with delete semantics.

---

## 11. Relationship to the test environment

`test-env/` deploys a single Windows Server 2022 Core EC2 instance running both
the DHCP role and the API. That co-location is deliberate and remains correct
regardless of the production decision: it validates scope lifecycle logic
against real PowerShell, real `ConvertTo-Json` output, and real cmdlet errors —
none of which changes under PSRP, per §6.

DHCP scopes are rows in the Windows DHCP database. Creating them costs nothing
in AWS and requires no corresponding real network, so any number of segments
can be exercised on one small instance.

Failover paths (CLAUDE.md §6, §7) are **not** covered by that environment — they
need a second DHCP server and Active Directory. They remain mock-only until the
spike environment in §9 exists, which would also be the natural place to test
them.
