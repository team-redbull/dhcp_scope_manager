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
- POST on an existing scope converges the **whole** desired state, by delegating to
  `_converge_scope` — the same diff PUT runs (§6). It must not converge only part of
  the payload: returning 200 while silently discarding fields is indistinguishable
  from success, and a caller cannot detect it. (It previously skipped
  `Add-DhcpServerv4Scope`, the only cmdlet carrying `scopeName` /
  `leaseDurationDays` / `description` / `startRange` / `endRange`, and so dropped
  all five plus exclusion removals and gateway clears.)
- Sharing one convergence routine is what keeps POST and PUT from drifting apart.
  `_converge_scope` assumes the caller holds `scope_locks.lock(scope)` —
  ScopeLockManager hands out plain `asyncio.Lock`s, which are not reentrant, so it
  must never acquire the lock itself.
- Idempotency is a property of the *diff*, not of skipping work: a POST of state
  that already matches issues no cmdlets. A POST that changes `subnetMask` on an
  existing scope returns 409, exactly as PUT does.
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

**Derived defaults** (`subnetMask`, `gateway`, `scopeName`, `failover.relationshipName`,
`startRange` + `endRange` — the only six)

- Omitting the key derives it: `subnetMask` → `255.255.255.0`, `gateway` → the subnet's `.254`,
  `scopeName` → the hosted cluster's own name, `relationshipName` → `<scopeName>-failover`,
  the range → the subnet's `.1`–`.253`
- Writing `null` or `""` is honoured as written — for `gateway` that means no DHCP option 3.
  `scopeName`, `relationshipName` and the range bounds are the exceptions: `""` counts as
  omitted and derives, matching Helm's `default`, because neither a nameless scope nor one
  distributing no addresses is a state anything can hold
- Any mask other than `255.255.255.0` with no explicit gateway is a hard error, not a guess.
  Same for the range: the `.1`–`.253` convention only holds for a /24
- **The range derives as a pair.** One bound without the other is rejected (422 / CI error),
  like the PXE pair — completing half a range from a subnet-wide default would put a
  deliberate edge opposite an implied one
- **`.253`, not `.254`, and that is load-bearing.** The derived gateway is `.254`; a range
  ending there would sit on top of it, and `gateway_not_in_distribution_range` would 422
  every values file that omitted both. Stopping one address short is what lets the minimal
  file — a bare `network:` — resolve to a valid scope
- **The derived bounds ignore the exclusion list.** Exclusions already carve holes inside a
  Windows range, so `.1`–`.253` minus the exclusions *is* "every address that is not
  excluded". Moving the bounds to fit the exclusions would couple two derivations that have
  to stay byte-identical across three implementations, one of them in Go templates
- Deriving the range is **fail-open by construction** — it widens the pool to the whole /24
  rather than the narrow band a values file used to state. The guard that keeps it honest is
  `gateway_not_in_distribution_range`: an explicit gateway at `.1` that used to sit safely
  below `startRange` is now inside the pool and is refused until an exclusion covers it. Do
  not soften that check to make the derivation more convenient
- `relationshipName` only applies when a `failover` block is present; `failover: null` stays
  `null`. Windows caps the name at 64 chars, so a `scopeName` long enough to overflow that
  fails the render and CI rather than being truncated — which caps a *derived* `scopeName`,
  and so a cluster file name, at 55
- Resolved identically in **three** places — the Pydantic model and the CI validator here,
  and the chart's `_dhcp-helpers.tpl` in `helm-charts-hostedclusters-setup`. The duplication
  is deliberate: GET reports the concrete value the DHCP server holds, so the rendered PUT
  body must carry it too or the drift check in §9 never converges. Change one, change all
  three — and one of the three is in another repo, so it will not fail your local tests.
- `scopeName` and `relationshipName` are the exception to "three places": the API model keeps
  both **required**, because the API only ever receives a resolved value. Only the chart and
  the CI validator derive them. The range is *not* an exception — it derives from the scope
  address, which the API has in the URL, so all three resolve it.

**`scopeName` comes from the cluster's file name**

- One values file *is* one hosted cluster *is* one DHCP scope, so `<cluster>.yaml` already
  carries the name. Repeating it inside the file could only ever be redundant or wrong
- An explicit `scopeName` anywhere in the merge chain still wins, so nothing already written
  had to change when this landed
- **Helm has no notion of which file a value came from** — it merges the four `valueFiles`
  into one flat `.Values`. So the name cannot be derived inside the chart from the values
  alone: `hcAppset.yaml` injects it as a `clusterName` Helm *parameter*, from the same
  `.path.filename | trimSuffix` expression that names the Application. The CI validator has
  the path in hand and uses `cluster_path.stem` instead
- It must be a chart-owned key, **not** `--set dhcp_values.scopeName`: helm parameters
  outrank `valueFiles`, so injected there an explicit `scopeName` in a values file could
  never win
- `clusterName` is chart-owned like `dhcp_api` and `crossplane` — it never appears in a
  values file, so `_REQUIRED_PATHS` must never demand it

**`dns.extraServers`** — the append-able half of `dns.servers`

- Helm deep-merges mappings but *replaces* lists, so a site file writing `dns.servers`
  discards the globals from `sites/configValues.yaml` instead of extending them
- `extraServers` is a separate key so the merge keeps both; the chart concatenates
  `servers` then `extraServers`, and that order is the wire order
- It never reaches the API as a distinct field — the payload carries one flat `dnsServers`
  list. The CI validator must validate the *joined* list or the site's own resolvers go
  unchecked

**Immutable on an existing scope** (`subnetMask`)

- Windows cannot change a scope's mask in place — the scope must be deleted and recreated
- A PUT that changes it is refused with 409, never silently accepted (see §6)
- On POST for a *new* scope the mask is applied normally; only the update path is affected

**Reconciliation safety**

- No hidden defaults inside the API beyond the two derived above; everything else comes from Helm / Git values
- API is stateless **for every scope route**. The one exception is the test runner
  (`/api/v1/test-runs`), which keeps a bounded in-memory registry so a run started by
  `POST` can be polled by `GET`. That is why a release people actually post runs to must
  be single-replica — a second pod has never heard of the run id.

## 3. GitOps Values Hierarchy

Merge order (last value wins), matching the `valueFiles` list Argo CD hands to Helm
(`argocd-platform/hostedClusters/templates/hcAppset.yaml`):

```
the chart's values.yaml                               (chart defaults — implicit base)
  → sites/configValues.yaml                           (global)
    → sites/{site}/values.yaml
      → sites/{site}/mces/{mce}/values.yaml
        → sites/{site}/mces/{mce}/hostedClusters/{cluster}.yaml
```

- Helm performs all merging — the API receives a fully resolved payload
- **`dhcp_values` is the only key the values repo owns.** `dhcp_api`, `crossplane` and
  `clusterName` are chart-owned: one API per cluster is a platform constant, not per-cluster
  config, and `clusterName` is injected by the ApplicationSet (§2).
  `_REQUIRED_PATHS` in the CI validator must therefore never demand them — that script
  walks the values repo, which never contains them.
- **The cluster file's own name is load-bearing, not just a label.** `scopeName` derives from
  it (§2), so renaming a file renames the scope. That is already a destructive operation for
  a different reason — the Argo Application is named after the file too, so a `git mv`
  deletes and recreates the Application, and with it the Request and the live scope. See the
  risk table in `argocd-platform/README.md`.
- All IPs in values files must be absolute (no offsets)
- `failover: null` removes an inherited failover — `failover: {}` does NOT (Helm deep-merges `{}`)

## 4. Helm Chart Behavior

**The chart is not in this repo.** The templates and their rendering tests live in
`team-redbull/helm-charts-hostedclusters-setup` — this repo is the DHCP API only.
What follows is the *contract* the API is written against, not a description of
files here; §9 does not make sense without it. Changing any of it means changing
that chart, and its `tests/test_render_parity.py` is what proves the rendered body
still matches the payload shape in §5.

- **Crossplane object name** — based only on `dhcp_values.network`: `dhcp-scope-{network-dashed}`. Changing `scopeName` does NOT create a new CR.
- **Request URL** — `dhcp_values.network` is baked into `payload.baseUrl` at template time (`{dhcp_api.url}/api/v1/scopes/{network}`); all four mappings then use `(.payload.baseUrl)`. The address is deliberately absent from `payload.body`. Note `network` remains a required **values file** key — only the rendered request body drops it.
- **`payload.body` is a JSON *string*, not a mapping** — provider-http types the field as a string and the API server rejects an object outright (`must be of type string`). `dhcp.payload` therefore emits JSON text, written field by field rather than piped through `toJson`, because Go marshals a map with its keys sorted and that would destroy the canonical field order in §5. The mappings parse it back with jq (`.payload.body`).
- **Bearer token via a header placeholder** — `Authorization: "Bearer {{ name:namespace:key }}"`, resolved by provider-http against the live Secret at reconcile time and stored only in placeholder form in `spec`, `status.requestDetails` and its logs. **Not** `secretInjectionConfigs`: that field runs the opposite direction, extracting HTTP *response* fields into a Secret. `dhcp_api.tokenSecretRef.{name,key}` are both required or the header is omitted entirely — a half-resolved placeholder would be sent as literal text. `namespace` defaults to the CR's own namespace and is written in by the chart: provider-http parses the placeholder with a regex demanding exactly three literal segments and never passes the CR's namespace in, so it cannot be inherited at reconcile time. A placeholder that fails that regex is **left in the header as literal text with no error raised**, so a wrong namespace surfaces as a 401 naming nothing.
- **The Request is the namespaced kind** — `http.m.crossplane.io/v1alpha2`, Crossplane v2's `.m.` group. provider-http ships two separate Request CRDs, not two versions of one: the legacy `http.crossplane.io/v1alpha2` is `scope: Cluster`. Consequences: `metadata.namespace` is always rendered (an object without one lands in `default`); `providerConfigRef` requires **both** `kind` and `name`, and references a `ClusterProviderConfig`/`ProviderConfig` **in the `.m.` group** — the legacy ProviderConfig of the same name cannot satisfy it; and `spec.deletionPolicy` does not exist on this kind, so writing it is silently pruned by the CRD and deletion behaviour comes from `managementPolicies` instead.
- **Required fields** — `helm template` fails hard if missing: `dhcp_values.network`, `startRange`, `endRange`, `leaseDurationDays`, `dns.servers`, `dhcp_api.url`
- **Derived fields** — `subnetMask`, `gateway` and `scopeName` may be omitted; the chart resolves them (`dhcp.defaultGateway` and `dhcp.scopeName` in `_dhcp-helpers.tpl`) rather than passing the omission through, so the rendered body always carries concrete values. A non-/24 mask with no gateway fails the render. `scopeName` resolves from the `clusterName` parameter (§2); with neither that nor an explicit value the render fails rather than silently skipping — a skip is what the old gate did, and it hid the misconfiguration.
- **The chart's `values.yaml` is the base of every merge** — a key set there cannot be unset by a site or cluster file. That is why it ships no `dhcp_values` at all: a worked example there would give every hosted cluster the same `scopeName` and `network`, colliding on one Request object name.
- **The whole template is gated on `dhcp_values.network`**, so a cluster with no DHCP block renders nothing. Three things make that the right key and not a workaround:
  - It is the scope's identity (§5) and the only `dhcp_values` key a cluster's *own* file ever sets. `sites/configValues.yaml` gives every cluster a `dhcp_values` block (lease, DNS, failover defaults), so inheriting one is not opting in — having a subnet is.
  - It was gated on `scopeName` until that gained a default. Now that `scopeName` resolves for every cluster, leaving the gate there would be the same as deleting it.
  - Deleting it is not an option: the air-gapped copy of this chart also carries the `HostedCluster` / `NodePool`, so an ungated Request would make the `required` on `network` fail the **whole** render for a cluster that never asked for a scope.
  - Gated on `hasKey`, not truthiness, so an absent `network` (no scope wanted) and `network: ""` (a malformed scope) stay different: the first renders nothing, the second reaches the `required` guard instead of disappearing silently.
- **ProviderConfig** — configurable via `crossplane.providerConfigName` (default: `dhcp-http`) and `crossplane.providerConfigKind` (default: `ClusterProviderConfig`). Both must name an object in the `.m.` group.
- **CR namespace** — `crossplane.namespace`, defaulting to `hcp-<clusterName>`. Resolved once in `dhcp.crNamespace` because it is used twice — as the CR's own namespace and as the default namespace for the token placeholder — and the two must not disagree. Unresolvable is a hard render failure, not a rendered `hcp-`: `clusterName` defaults to `""`, so the derived form would otherwise produce a garbage namespace for any render that skipped the appset's `--set`.

## 5. API Contract

| Verb     | Path                     | Description                                            |
| -------- | ------------------------ | ------------------------------------------------------ |
| `POST`   | `/api/v1/scopes/{scope}` | Create or ensure scope (idempotent)                    |
| `GET`    | `/api/v1/scopes/{scope}` | Current canonical state (404 → Crossplane issues POST) |
| `PUT`    | `/api/v1/scopes/{scope}` | Diff-based update                                      |
| `DELETE` | `/api/v1/scopes/{scope}` | Delete scope (idempotent — 204 even if not found)      |
| `GET`    | `/api/v1/scopes`         | List all scopes, sorted by scope address               |
| `GET`    | `/healthz`               | Runtime capability check                               |
| `POST`   | `/api/v1/test-runs`      | Run this service's whole suite (§12) — 202 + `runId`   |
| `GET`    | `/api/v1/test-runs/{id}` | Run status, exit code, redacted output                 |
| `GET`    | `/api/v1/test-runs`      | Recent runs, summaries only                            |

The `test-runs` routes take auth but **not** the DHCP environment guard: they drive a
server named in the request, not this deployment's own, so a release returning 503 from
every scope route can still run tests.

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

PUT is **diff-based convergence** in what it *writes* — only changed sections trigger
PowerShell — but the body it diffs against is a **full replacement**, not a patch. An
omitted optional field resolves to its default and is then applied: omitting `dnsDomain`,
`nextServer` / `bootFile` or `exclusions` clears them on the server, and omitting the
range **widens the pool to the derived `.1`–`.253`** rather than leaving it alone. That is
deliberate and load-bearing for GitOps — if omission meant "leave alone", deleting a line
from a values file could never remove anything, and Git would stop being authoritative. It
is also a footgun for anyone calling the API by hand; `tests/integration/` pins it so it
cannot change unnoticed. Required fields are `scopeName`, `leaseDurationDays`,
`dnsServers`.

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

**`/healthz` is the one route with no `verify_token`.** It is the Deployment's
readiness probe, and a kubelet probe cannot send an Authorization header — it has no
way to read a Secret. Behind auth it returned 401 to every probe, so the pod never
became ready and never joined its Service; that is not theoretical, it is what the
first token-generating release did. The response is a bare `{"status": "ok"}` or an
error, so an anonymous caller learns only whether the DHCP server is reachable.
Do not "fix" this by moving readiness to `tcpSocket`: that asks whether the process
is up rather than whether this pod can reach the DHCP server, and would keep a pod
that can serve nothing in the Service endpoints. Pinned by
`test_healthz_needs_no_token_even_when_auth_is_on`.

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

Validates: IP format, subnet consistency, range ordering, range given as a pair (or omitted and derived), gateway in subnet, exclusions in subnet, failover mode fields.

## 12. Testing Requirements

- `GET == PUT` roundtrip equality: `assert get_scope(id).model_dump() == put_payload.model_dump()`
- Idempotent POST (scope exists) and DELETE (scope absent)
- Exclusion sorting, failover `null`/object consistency, subnet validation

**The unit suite mocks the PowerShell transport end to end.** It proves which cmdlet
*strings* the service builds, never what Windows does with them. Anything that depends
on real server behaviour — range transition rules, option removal, what a cmdlet
actually persists — is invisible to it. Both the POST convergence bug and the silent
field reset in an under-specified PUT lived in exactly that blind spot.

`tests/integration/` closes it: the real ASGI app over a real PSRP connection, skipped
unless `DHCP_IT=1`. It owns scope `10.77.88.0` alone and deletes it before and after
every test. Setup is in `tests/integration/README.md`. Add a case there whenever a
change depends on how Windows responds, not just on the command text.

**Cover *changing* a field, not only clearing it.** They are different code paths: the
diff decides whether to issue a cmdlet at all, so a diff that wrongly concludes
"unchanged" issues nothing, returns 200, and leaves the server stale — which a
reset-to-default test cannot detect. Every option in §6 needs both.

**Chart rendering is not tested here.** The Crossplane Request templates live in
`team-redbull/helm-charts-hostedclusters-setup` with their own suite, including the
parity test asserting the rendered body matches §5. This repo asserts the other half —
that GET returns that shape. Neither imports the other, so a change to the payload
shape has to be made in both, deliberately.

Its async fixtures must use `@pytest_asyncio.fixture`: the repo sets no `asyncio_mode`,
so pytest-asyncio runs strict, where a plain `@pytest.fixture` on an async generator is
never awaited and its body silently does not run.

**Air-gapped runs.** There is no CI and no package index on that side, so the deployed
API runs its own suite: `POST /api/v1/test-runs` forks pytest in a subprocess. This works
only because `requirements.txt` carries pytest / pytest-asyncio / httpx and the Dockerfile
does `COPY tests/` — keep both, or the endpoint has nothing to run.

Four rules that are load-bearing, all covered by `tests/test_testrunner.py`:

- **A run is always the whole suite.** There is no `suite` selector on the request — the
  subprocess gets no `--ignore` and no path argument, so pytest collects the mocked tests
  and `tests/integration` together. Selecting a subset meant a passing run could mean two
  different things, and the cheap subset verified nothing about the server the release was
  aimed at. `target` is consequently **required**: `tests/integration` self-skips unless
  `DHCP_IT` is set, and only a target sets it, so a run without one would be "everything"
  in name only. A body still carrying `suite` is rejected by `extra="forbid"`.
- **The DHCP server under test comes from the request body, never from settings.** The
  subprocess environment is built by *stripping* every `DHCP_*`/`WINRM_*` key and setting
  only what the request supplied. A target that could fall back to this deployment's own
  server would make the live tests — which create and delete real scopes — a loaded gun.
- **A target matching `settings.DHCP_SERVER_HOST` or `TEST_RUNNER_DENY_HOSTS` is refused
  with 422** before anything starts.
- **Captured pytest output is redacted** before it is returned or logged. `--tb=line`
  because a full traceback can echo locals, and a WinRM connection repr carries the
  credential.

Live failover is still not testable: Windows refuses a failover relationship whose partner
is the local server, so a single test server cannot exercise it at all.

## 13. Implementation Checklist

Before any code change: Does this break GET/PUT symmetry? Is it idempotent? Does this belong in Helm? Will Crossplane reconcile correctly? Is output deterministic? If any "no" → fix before merging.
