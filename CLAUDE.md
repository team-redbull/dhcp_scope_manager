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

- POST must not fail if scope already exists — it is the create path, and must be
  safe to retry after a partially-failed create
- POST converges options, PXE and exclusion *additions* on an existing scope, but
  **not** `scopeName` / `leaseDurationDays` / `description` / `startRange` /
  `endRange`, exclusion removals, or clearing a `gateway`. Those live only in
  `Add-DhcpServerv4Scope`, which the already-exists path skips. Converging an
  existing scope is PUT's job (§6) — Crossplane only POSTs after a `GET` 404, so
  it reaches an existing scope only when retrying its own create, where those
  fields were already written correctly. Documented in the README's POST section.
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
  the rendered PUT body must carry it too or the drift check in §9 never converges. Change
  one, change all three.

**Immutable on an existing scope** (`subnetMask`)

- Windows cannot change a scope's mask in place — the scope must be deleted and recreated
- A PUT that changes it is refused with 409, never silently accepted (see §6)
- On POST for a *new* scope the mask is applied normally; only the update path is affected

**Reconciliation safety**

- No hidden defaults inside the API beyond the two derived above; everything else comes from Helm / Git values
- API is stateless

## 3. GitOps Values Hierarchy

Merge order (last value wins), matching the `valueFiles` list Argo CD hands to Helm
(`argocd-platform/hostedClusters/templates/hcAppset.yaml`):

```
helm/values.yaml                                      (chart defaults — implicit base)
  → sites/configValues.yaml                           (global)
    → sites/{site}/values.yaml
      → sites/{site}/mces/{mce}/values.yaml
        → sites/{site}/mces/{mce}/hostedClusters/{cluster}.yaml
```

- Helm performs all merging — the API receives a fully resolved payload
- **`dhcp_values` is the only key the values repo owns.** `dhcp_api` and `crossplane` are
  chart-owned (`helm/values.yaml`): one API per cluster is a platform constant, not per-cluster
  config. `_REQUIRED_PATHS` in the CI validator must therefore never demand them — that script
  walks the values repo, which never contains them.
- All IPs in values files must be absolute (no offsets)
- `failover: null` removes an inherited failover — `failover: {}` does NOT (Helm deep-merges `{}`)

## 4. Helm Chart Behavior

- **Crossplane object name** — based only on `dhcp_values.network`: `dhcp-scope-{network-dashed}`. Changing `scopeName` does NOT create a new CR.
- **Request URL** — `dhcp_values.network` is baked into `payload.baseUrl` at template time (`{dhcp_api.url}/api/v1/scopes/{network}`); all four mappings then use `(.payload.baseUrl)`. The address is deliberately absent from `payload.body`. Note `network` remains a required **values file** key — only the rendered request body drops it.
- **`payload.body` is a JSON *string*, not a mapping** — provider-http types the field as a string and the API server rejects an object outright (`must be of type string`). `dhcp.payload` therefore emits JSON text, written field by field rather than piped through `toJson`, because Go marshals a map with its keys sorted and that would destroy the canonical field order in §5. The mappings parse it back with jq (`.payload.body`).
- **Bearer token via a header placeholder** — `Authorization: "Bearer {{ name:namespace:key }}"`, resolved by provider-http against the live Secret at reconcile time and stored only in placeholder form in `spec`, `status.requestDetails` and its logs. **Not** `secretInjectionConfigs`: that field runs the opposite direction, extracting HTTP *response* fields into a Secret. All three of `dhcp_api.tokenSecretRef.{name,namespace,key}` are required or the header is omitted entirely — a half-resolved placeholder would be sent as literal text.
- **Required fields** — `helm template` fails hard if missing: `dhcp_values.network`, `scopeName`, `startRange`, `endRange`, `leaseDurationDays`, `dns.servers`, `dhcp_api.url`
- **Derived fields** — `subnetMask` and `gateway` may be omitted; the chart resolves them (`dhcp.defaultGateway` in `_dhcp-helpers.tpl`) rather than passing the omission through, so the rendered body always carries concrete values. A non-/24 mask with no gateway fails the render.
- **`helm/values.yaml` is the base of every merge** — a key set there cannot be unset by a site or cluster file. That is why it ships `dhcp_values` entirely commented out: a worked example there would give every hosted cluster the same `scopeName` and `network`, colliding on one Request object name. The whole template is gated on `dhcp_values.scopeName`, so a cluster with no DHCP block renders nothing.
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
| Scope range  | `startRange` / `endRange`          | `Set-DhcpServerv4Scope` (1–2 calls, see below) |
| Scope params | name / lease / description         | `Set-DhcpServerv4Scope`                      |
| Options      | gateway / DNS servers / domain     | `Set-DhcpServerv4OptionValue`                |
| Boot options | `nextServer` / `bootFile`          | `Set-` / `Remove-DhcpServerv4OptionValue` (ids 66, 67) |
| Exclusions   | set difference (add/remove)        | `Add-` / `Remove-DhcpServerv4ExclusionRange` |
| Failover     | any failover field                 | add / remove / update relationship           |
| `subnetMask` | any change                         | **none — rejected with 409** (see below)     |

**Range and params are separate calls, range first.** `Set-DhcpServerv4Scope` applies
the name/lease/description half of a combined call even when it goes on to reject the
range, leaving the scope matching neither the old nor the new desired state. Writing
the range first means a refused range aborts before anything else is touched.

**Range changes route through the union.** Windows accepts a new range only when it is
a superset or a subset of the one the scope already holds; a mixed change (one edge
moving out while the other moves in) or a disjoint move is refused with "Failed to set
IP address range to a scope" (`DHCP 20023`). `_range_transition_steps` therefore widens
to the union of current and desired first, then narrows to desired — each step is a
superset or subset by construction. Pure widenings and narrowings collapse to a single
call, so the common case costs nothing extra. The intermediate union is briefly
leasable; for a mixed change every union address already belongs to current or desired,
so only a fully disjoint move exposes new addresses. Deactivating the scope first is
not an alternative — range writes fail outright on an Inactive scope.

**`subnetMask` is immutable on an existing scope.** `Set-DhcpServerv4Scope` has no
`-SubnetMask` parameter; changing a mask requires deleting and recreating the scope,
which drops every active lease on that subnet. A PUT that changes it is rejected with
409 `IMMUTABLE_FIELD` naming both values, rather than returning 200 without applying it
— a silent no-op hides the drift and makes Crossplane re-send the same PUT forever.

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

**How provider-http actually decides.** `expectedResponseCheck: DEFAULT` (what the chart uses)
holds the scope up to date when the GET response **contains** the desired body — jq containment,
not byte equality. Practical consequences:

- Every field the body *does* carry must match the response exactly, including list order. Two
  exclusions in a different order are a different value, so a values file that lists them out of
  ascending IP order makes Crossplane PUT on every poll, forever. Same for `dnsServers`.
- A field the body *omits* is not checked at all. So the derived-default rule (`subnetMask`,
  `gateway`) is what actually matters: a body that said `gateway: null` against a server holding
  `10.20.30.254` fails containment and re-PUTs forever, which is why the chart resolves them
  rather than passing the omission through.
- Removal uses `isRemovedCheck: DEFAULT` — the scope counts as gone when the OBSERVE GET returns
  404, which the API does.

Keeping GET byte-identical to the desired PUT body is stricter than provider-http requires, and
still the right internal invariant: both are `DhcpScopeBody`, so they cannot drift apart by
construction, and equality is far easier to test than containment.

## 10. Security

- Bearer token auth via `DHCP_API_TOKEN` — optional; disabled when unset
- Secrets never logged; PowerShell stderr sanitized before returning to clients

**WinRM auth and the failover double hop.** `WINRM_AUTH` is `kerberos | ntlm | credssp`.
The failover cmdlets in §6/§7 (`Add-DhcpServerv4Failover`,
`Add-DhcpServerv4FailoverScope`, `Invoke-DhcpServerv4FailoverReplication`) act on the
*partner* as the calling user. A `kerberos` or `ntlm` WinRM session is a network logon
holding no credential to present there, so all three fail against the partner while the
local half appears to succeed — verified on real servers, where the identical cmdlet
fails under a network logon and succeeds under one carrying a credential. **Only
`credssp` delegates a credential**, so it is required wherever the API manages failover.
Kerberos can do it too, but only with delegation configured in AD (RBCD), which is
directory configuration rather than anything this codebase controls.

Consequences to respect:

- Only the server the API *connects to* needs CredSSP enabled
  (`Enable-WSManCredSSP -Role Server`). The partner is the target of the delegated
  credential, not a CredSSP endpoint.
- The credential becomes recoverable on the DHCP server. Use a dedicated account holding
  only `DHCP Administrators` **on both servers** — never a Domain Admin.
- Accounts in AD's `Protected Users` group cannot delegate at all, and `Domain Admins` is
  commonly nested inside it. Check transitive membership before choosing an account.
- `scripts/credssp-precheck.ps1` (read-only) reports whether policy would block CredSSP
  on a target server.
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
