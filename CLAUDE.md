# DHCP Backend — Engineering Contract

## 1. Purpose

DHCP scope lifecycle (create, read, update, delete) for OpenShift hosted clusters via GitOps.
**The user only ever edits values files. The backend is not a manual entry point.**

| Layer      | Role                                                    |
| ---------- | ------------------------------------------------------- |
| Git        | Source of truth (values files)                          |
| Helm       | Merge site → MCE → cluster values, render Crossplane CR |
| Crossplane | Reconciliation engine — decides POST / PUT / DELETE     |
| FastAPI    | Validate, normalize, execute                            |
| PowerShell | Apply changes to Windows DHCP Server                    |

## 2. Core Invariants (never break these)

**API shape consistency**

- GET response must exactly match PUT / POST payload shape
- This is the prerequisite for Crossplane reconciliation correctness

**Idempotency**

- POST must not fail if scope already exists — converge to desired state
- DELETE must not fail if scope does not exist

**Deterministic data**

- Exclusions: always absolute IPs (not offsets), always sorted ascending by IP
- Failover: `null` stays `null`; object stays object
- DNS servers: order preserved exactly (primary/secondary semantics — never sorted)
- `description`: always `""`, never `null`
- `nextServer` / `bootFile`: always `""`, never `null` — `""` is the concrete "option not set" state

**Paired fields** (`nextServer` + `bootFile` — DHCP options 66/67)

- Optional, but **both-or-nothing**: setting one without the other is rejected (422 / CI error)
- A boot server with no boot file leaves a host silently unbootable, so half a pair is never written
- Enforced in **two** places — the Pydantic model and the CI validator. Change one, change both.
- Per-architecture boot files (BIOS vs UEFI) need Windows DHCP *policies* matching option 93
  and are deliberately **not** modelled here

**Derived defaults** (`subnetMask`, `gateway` — the only two)

- Omitting the key derives it: `subnetMask` → `255.255.255.0`, `gateway` → the subnet's `.254`
- Writing `null` or `""` is honoured as written — for `gateway` that means no DHCP option 3
- Any mask other than `255.255.255.0` with no explicit gateway is a hard error, not a guess
- Resolved identically in **three** places — Helm, the Pydantic model, and the CI validator.
  The duplication is deliberate: GET reports the concrete value the DHCP server holds, so
  the rendered PUT body must carry it too or the byte-compare in §9 never converges. Change
  one, change all three.

**Reconciliation safety**

- No hidden defaults inside the API beyond the two derived above; everything else comes from Helm / Git values
- API is stateless

## 3. GitOps Values Hierarchy

Merge order (last value wins):

```
sites/{site}/config.yaml  →  sites/{site}/mce/{mce}/config.yaml  →  sites/{site}/mce/{mce}/hosted-cluster/{c}.yaml
```

- Helm performs all merging — the API receives a fully resolved payload
- All IPs in values files must be absolute (no offsets)
- `failover: null` removes an inherited failover — `failover: {}` does NOT (Helm deep-merges `{}`)

## 4. Helm Chart Behavior

- **Crossplane object name** — based only on `dhcp_values.network`: `dhcp-scope-{network-dashed}`. Changing `scopeName` does NOT create a new CR.
- **Request URL** — `dhcp_values.network` is baked into `payload.baseUrl` at template time (`{apiServer.url}/api/v1/scopes/{network}`); all four mappings then use `(.payload.baseUrl)`. The address is deliberately absent from `payload.body`. Note `network` remains a required **values file** key — only the rendered request body drops it.
- **Required fields** — `helm template` fails hard if missing: `dhcp_values.network`, `scopeName`, `startRange`, `endRange`, `leaseDurationDays`, `dns.servers`, `apiServer.url`
- **Derived fields** — `subnetMask` and `gateway` may be omitted; the chart resolves them (`dhcp.defaultGateway` in `_dhcp-helpers.tpl`) rather than passing the omission through, so the rendered body always carries concrete values. A non-/24 mask with no gateway fails the render.
- **`helm/values.yaml` is the base of every merge** — a key set there cannot be unset by a site or cluster file, which is why `subnetMask`/`gateway` ship absent from it
- **ProviderConfig** — configurable via `crossplane.providerConfigName` (default: `dhcp-http`)

## 5. API Contract

| Verb     | Path                     | Description                                            |
| -------- | ------------------------ | ------------------------------------------------------ |
| `POST`   | `/api/v1/scopes/{scope}` | Create or ensure scope (idempotent)                    |
| `GET`    | `/api/v1/scopes/{scope}` | Current canonical state (404 → Crossplane issues POST) |
| `PUT`    | `/api/v1/scopes/{scope}` | Diff-based update                                      |
| `DELETE` | `/api/v1/scopes/{scope}` | Delete scope (idempotent — 204 even if not found)      |
| `GET`    | `/api/v1/scopes`         | List all scopes, sorted by scope address               |
| `GET`    | `/healthz`               | Runtime capability check                               |

`{scope}` = IPv4 network address — **the sole identifier of the resource**.

**The scope address appears in the URL only, never in the body.** It is immutable
identity, not state, so duplicating it in the payload would create two sources of
truth that could disagree. The body model is `extra="forbid"`, so a request that
carries `scope` (or the old `network`) in its body is rejected with 422 rather than
one source silently winning.

Internally the API composes the path address with the body into `DhcpScopePayload`
so cross-field validation (subnet consistency) still sees every field. That model is
serialized directly only by `GET /api/v1/scopes`, where each item needs to say which
scope it is — list items are the body shape plus a leading `scope` field.

### Canonical Payload Shape (field order is intentional and tested — do not reorder)

POST/PUT request body, and the GET response for a single scope — identical by construction:

```json
{
  "scopeName": "cluster-a",
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

`GET /api/v1/scopes` returns the same objects with a leading `scope` field:

```json
{
  "scopes": [{ "scope": "10.20.30.0", "scopeName": "cluster-a", "...": "..." }],
  "errors": [{ "scope": "10.40.0.0", "error": "..." }]
}
```

## 6. Update Semantics (PUT)

PUT is **diff-based convergence**, not full replace. Only changed sections trigger PowerShell:

| Section      | Changed when                       | PowerShell cmdlet                            |
| ------------ | ---------------------------------- | -------------------------------------------- |
| Scope params | name / lease / description / range | `Set-DhcpServerv4Scope`                      |
| Options      | gateway / DNS servers / domain     | `Set-DhcpServerv4OptionValue`                |
| Boot options | `nextServer` / `bootFile`          | `Set-` / `Remove-DhcpServerv4OptionValue` (ids 66, 67) |
| Exclusions   | set difference (add/remove)        | `Add-` / `Remove-DhcpServerv4ExclusionRange` |
| Failover     | any failover field                 | add / remove / update relationship           |

Boot options are diffed in their own block, not folded into Options: they need separate
cmdlet calls either way, and a shared flag would make a PXE-only change rewrite the DNS
options (and vice versa) on every reconciliation.

Failover changes requiring recreate: mode change, `relationshipName`, `partnerServer`, or (HotStandby only) `serverRole`.

## 7. Delete Semantics

Order is critical:

1. `Remove-DhcpServerv4FailoverScope` (if in failover relationship)
2. `Remove-DhcpServerv4Failover` (if relationship now empty)
3. `Remove-DhcpServerv4ExclusionRange` for each exclusion
4. `Remove-DhcpServerv4Scope`

If failover detach fails, delete aborts and retries on next Crossplane cycle.

## 8. PowerShell Execution Rules

Always: `-ErrorAction Stop`, `ConvertTo-Json -Depth 10`, `-Force` where applicable, `run_ps()` wrapper, `_ps_str()` to escape user strings.
Never: return raw PS output to clients, swallow errors, execute partial operations without error handling.

**Named parameters vs `-OptionId`**: `Set-DhcpServerv4OptionValue` exposes named parameters only for options 3/6/15 (`-Router`, `-DnsServer`, `-DnsDomain`), which is why those three merge into a single call. Options 66/67 have none and `-OptionId` takes one id per invocation, so the PXE pair costs one call per option. Clearing them needs an explicit `Remove-DhcpServerv4OptionValue` — Windows keeps an option value that merely stopped being written, same as the router option.

**IP address fields in PS output**: `ConvertTo-Json -Depth 10` serializes .NET `IPAddress` properties (scope `SubnetMask`/`StartRange`/`EndRange`/`ScopeId`, exclusion `StartRange`/`EndRange`, option values) as dicts with an `IPAddressToString` key, not plain strings. Always use `_extract_ip_str()` from `ps_parsers` to extract these values — never call `str()` directly on them. Violating this produces "only decimal digits permitted in address" errors at `IPv4Address()` construction time.

## 9. Reconciliation

GET → 404: POST. GET body differs from desired: PUT. CR deleted: DELETE.
GET must serialize to byte-identical output as the desired PUT body when no change is intended. Both are `DhcpScopeBody`, so they cannot drift apart by construction.

## 10. Security

- Bearer token auth via `DHCP_API_TOKEN` — optional; disabled when unset
- Secrets never logged; PowerShell stderr sanitized before returning to clients
- All inputs validated at API boundary (Pydantic + subnet consistency checks). Subnet consistency spans path and body, so it runs when the two are composed in `validate_scope_request`; failures there are re-raised as `RequestValidationError` to return 422, not 500.

## 11. CI Validation

```bash
# CI — validate only clusters affected by the current git diff (inheritance-aware)
python3 scripts/validate_changed_clusters.py --base-ref origin/main

# Manual — validate a specific cluster, MCE, or site
python3 scripts/validate_dhcp_values.py --site telAviv --mce prep-mce-tlv-a --cluster prep-tlv-gpu

# Manual — validate the full repo
python3 scripts/validate_dhcp_values.py
```

Changed-file resolution:

- `hostedClusters/<c>.yaml` changed → validate only `<c>`
- `mces/<mce>/values.yaml` changed → validate all clusters under `<mce>`
- `<site>/values.yaml` changed → validate all clusters under `<site>`
- `sites/configValues.yaml` changed → full repo scan

Validates: IP format, subnet consistency, range ordering, gateway in subnet, exclusions in subnet, failover mode fields.

## 12. Testing Requirements

- `GET == PUT` roundtrip equality: `assert get_scope(id).model_dump() == put_payload.model_dump()`
- Idempotent POST (scope exists) and DELETE (scope absent)
- Exclusion sorting, failover `null`/object consistency, subnet validation

## 13. Implementation Checklist

Before any code change: Does this break GET/PUT symmetry? Is it idempotent? Does this belong in Helm? Will Crossplane reconcile correctly? Is output deterministic? If any "no" → fix before merging.
