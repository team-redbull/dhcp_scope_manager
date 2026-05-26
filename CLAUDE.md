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

**Reconciliation safety**

- No hidden defaults inside the API; all defaults come from Helm / Git values
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
- **Required fields** — `helm template` fails hard if missing: `dhcp_values.network`, `scopeName`, `subnetMask`, `startRange`, `endRange`, `leaseDurationDays`, `dns.servers`, `apiServer.url`
- **ProviderConfig** — configurable via `crossplane.providerConfigName` (default: `dhcp-http`)

## 5. API Contract

| Verb     | Path                        | Description                                            |
| -------- | --------------------------- | ------------------------------------------------------ |
| `POST`   | `/api/v1/scopes/{scope_id}` | Create or ensure scope (idempotent)                    |
| `GET`    | `/api/v1/scopes/{scope_id}` | Current canonical state (404 → Crossplane issues POST) |
| `PUT`    | `/api/v1/scopes/{scope_id}` | Diff-based update                                      |
| `DELETE` | `/api/v1/scopes/{scope_id}` | Delete scope (idempotent — 204 even if not found)      |
| `GET`    | `/api/v1/scopes`            | List all scopes, sorted by network address             |
| `GET`    | `/healthz`                  | Runtime capability check                               |

`scope_id` = IPv4 network address. Path and body `network` must match on POST/PUT.

### Canonical Payload Shape (field order is intentional and tested — do not reorder)

```json
{
  "scopeName": "cluster-a",
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

## 6. Update Semantics (PUT)

PUT is **diff-based convergence**, not full replace. Only changed sections trigger PowerShell:

| Section      | Changed when                       | PowerShell cmdlet                            |
| ------------ | ---------------------------------- | -------------------------------------------- |
| Scope params | name / lease / description / range | `Set-DhcpServerv4Scope`                      |
| Options      | gateway / DNS servers / domain     | `Set-DhcpServerv4OptionValue`                |
| Exclusions   | set difference (add/remove)        | `Add-` / `Remove-DhcpServerv4ExclusionRange` |
| Failover     | any failover field                 | add / remove / update relationship           |

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

**IP address fields in PS output**: `ConvertTo-Json -Depth 10` serializes .NET `IPAddress` properties (scope `SubnetMask`/`StartRange`/`EndRange`/`ScopeId`, exclusion `StartRange`/`EndRange`, option values) as dicts with an `IPAddressToString` key, not plain strings. Always use `_extract_ip_str()` from `ps_parsers` to extract these values — never call `str()` directly on them. Violating this produces "only decimal digits permitted in address" errors at `IPv4Address()` construction time.

## 9. Reconciliation

GET → 404: POST. GET body differs from desired: PUT. CR deleted: DELETE.
GET must serialize to byte-identical output as the desired PUT body when no change is intended.

## 10. Security

- Bearer token auth via `DHCP_API_TOKEN` — optional; disabled when unset
- Secrets never logged; PowerShell stderr sanitized before returning to clients
- All inputs validated at API boundary (Pydantic + subnet consistency checks)

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
