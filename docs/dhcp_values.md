# dhcp_values Reference

This document describes how to write the `dhcp_values` block in a Helm values file for a hosted cluster. The block is the single source of truth for a DHCP scope — Crossplane reads it, compares it to the live DHCP server state, and converges.

---

## Values File Hierarchy

Values files live in a separate repository (`day1` / `platform-config`). Helm merges them in this
order (last value wins), which is exactly the `valueFiles` list Argo CD builds in `hcAppset.yaml`:

```
sites/configValues.yaml                                       # global defaults
  → sites/{site}/values.yaml                                  # site defaults
    → sites/{site}/mces/{mce}/values.yaml                     # MCE overrides
      → sites/{site}/mces/{mce}/hostedClusters/{cluster}.yaml # cluster-specific values
```

The chart's own `values.yaml` (in `team-redbull/helm-charts-hostedclusters-setup`) sits
underneath all of them as an implicit base. It carries
`dhcp_api` and `crossplane` — which the values repo never sets, one API per cluster being a
platform constant — and ships `dhcp_values` commented out, so nothing here is inherited by
accident.

You only need to set the fields that differ from the layer above. Required fields must appear
somewhere in the chain.

---

## Full Example

```yaml
dhcp_values:
  scopeName: "cluster-a-workers"
  network: "10.20.30.0"
  subnetMask: "255.255.255.0" # optional; this is the default, so it can be omitted
  startRange: "10.20.30.11"
  endRange: "10.20.30.240"
  leaseDurationDays: 8
  description: "DHCP scope for cluster-a"
  gateway: "10.20.30.1" # optional; omit to derive 10.20.30.254, or set "" for no router option

  dns:
    servers:
      - "10.50.1.5"
      - "10.50.1.6"
    domain: "cluster-a.lab.local"

  pxe: # optional; both keys or neither (see "PXE boot options" below)
    server: "10.50.1.20"
    bootfile: "snponly.efi"

  exclusions:
    - startAddress: "10.20.30.1"
      endAddress: "10.20.30.10"
    - startAddress: "10.20.30.241"
      endAddress: "10.20.30.254"

  failover:
    partnerServer: "dhcp02.lab.local"
    relationshipName: "cluster-a-failover"
    mode: "HotStandby"
    serverRole: "Active"
    reservePercent: 5
    maxClientLeadTimeMinutes: 60
```

---

## Fields

### Required fields

| Field               | Type         | Constraints                                     | Description                                                          |
| ------------------- | ------------ | ----------------------------------------------- | -------------------------------------------------------------------- |
| `scopeName`         | string       | 1–256 chars, not blank                          | Display name for the scope on the DHCP server                        |
| `network`           | IPv4         | must be exact network address                   | Scope ID — used in all PowerShell cmdlets and the Crossplane CR name |
| `startRange`        | IPv4         | in subnet, not network/broadcast                | First IP in the DHCP distribution range                              |
| `endRange`          | IPv4         | in subnet, not network/broadcast, >= startRange | Last IP in the DHCP distribution range                               |
| `leaseDurationDays` | integer      | 1–3650                                          | Lease duration sent to clients                                       |
| `dns.servers`       | list of IPv4 | at least one required                           | DNS servers sent to clients (DHCP option 6)                          |

### Optional fields

| Field         | Type           | Default             | Description                                                                |
| ------------- | -------------- | ------------------- | -------------------------------------------------------------------------- |
| `description` | string         | `""`                | Free-text scope description. `null` and omitting are both treated as `""`. |
| `subnetMask`  | IPv4           | `255.255.255.0`     | Contiguous mask; combined with `network` must form a valid subnet. `null` and `""` also mean the default — there is no "no subnet mask". |
| `gateway`     | IPv4 or `""`   | the subnet's `.254` | Default gateway/router option. **Omit it** to derive `.254`; write `""` or `null` for no option 3. When set, must be in the subnet. If inside `[startRange, endRange]`, must be covered by an exclusion. |
| `dns.domain`  | string         | `""`                | Can be omitted or set to `""` if no domain suffix is needed.               |
| `pxe.server`  | string         | `""`                | PXE boot server host name or IP (DHCP option 66). Required if `pxe.bootfile` is set (see section below). |
| `pxe.bootfile`| string         | `""`                | PXE boot file path or URL (DHCP option 67). Required if `pxe.server` is set (see section below). |
| `exclusions`  | list           | `[]`                | IP ranges excluded from distribution (see section below).                  |
| `failover`    | object or null | `null`              | Failover configuration (see section below). `null` = no failover.          |

---

## Derived defaults: `subnetMask` and `gateway`

These two are the only fields the stack fills in for you. **Omitting the key is what
triggers the default** — anything you actually write is honoured as written.

```yaml
# The common case: a /24 whose router is at .254. Write neither key.
dhcp_values:
  network: "10.20.30.0"
  # subnetMask -> 255.255.255.0
  # gateway    -> 10.20.30.254
```

| What you write        | What the scope gets      |
| --------------------- | ------------------------ |
| neither key           | `255.255.255.0`, `.254`  |
| `gateway: ""`         | no DHCP option 3         |
| `gateway: null`       | no DHCP option 3         |
| `gateway: "10.20.30.1"` | that address           |

A `subnetMask` other than `255.255.255.0` **requires** an explicit `gateway`. The `.254`
convention only holds for a /24, so rather than guessing, `helm template` fails and the
API returns `422`:

```yaml
# Rejected — no gateway to derive
subnetMask: "255.255.0.0"

# Fine
subnetMask: "255.255.0.0"
gateway: "10.20.0.1"
```

The rule is applied in three places — the Helm chart, the API's Pydantic model, and the
CI validator — so a values file that passes CI renders and applies identically. The chart
resolves the value at render time rather than letting the API do it alone, because
Crossplane checks the GET response against the rendered body: GET reports the concrete
address the DHCP server holds, so a body that said `null` would diff on every cycle and
trigger a PUT every 60 seconds forever.

Note that the chart's `values.yaml` is the base of every Helm merge, so a key set there cannot be
unset by a site or cluster file. Both keys ship absent from it for that reason.

---

## Setting optional values

For optional scalar fields, use `""` when you do not want to set a value:

```yaml
description: ""  # no scope description
gateway: ""      # no router/default gateway option
dns:
  domain: ""     # no DNS search domain
```

For `description` and `dns.domain`, `null`, a bare empty YAML value, and omitting the key are also accepted. Use `""` as the canonical form because it is explicit and unambiguous in all YAML parsers.

`gateway` is the exception: omitting it does **not** mean "unset", it means "derive the
subnet's `.254`" (see [Derived defaults](#derived-defaults-subnetmask-and-gateway)). Write
`gateway: ""` when you genuinely want no router option — that is the only form that says so
without ambiguity.

For optional non-scalar fields, use the field's natural empty value:

```yaml
exclusions: []  # no exclusion ranges
failover: null  # no failover relationship
```

Do not use `failover: ""` or `failover: {}`. `failover` is an object, and Helm deep-merges `{}` with inherited values, so any inherited failover values survive.

---

## DNS servers

DNS server order matters. The first entry is the primary DNS server, the second is secondary. The API preserves order exactly as written — it does not sort.

```yaml
dns:
  servers:
    - "10.50.1.5" # primary
    - "10.50.1.6" # secondary
  domain: "lab.local"
```

If the order in `values.yaml` does not match what the DHCP server has stored, Crossplane will issue a single PUT to reorder the DHCP server's option 6 value to match `values.yaml` — after that PUT, GET and desired state agree and reconciliation goes quiet again. This is harmless but still worth avoiding: when adopting a scope that was previously configured manually or by another tool, matching the existing order in `values.yaml` up front avoids an unnecessary write.

At least one DNS server is required by the backend model. `dns.servers: []` is rejected with `422 VALIDATION_ERROR`, and a live DHCP scope observed with no DNS option is treated as invalid managed state.

---

## PXE boot options

For hosts that network-boot rather than booting from local media, the `pxe` block sets the two classic PXE options:

| Key            | DHCP option | Meaning                                                                        |
| -------------- | ----------- | ------------------------------------------------------------------------------ |
| `pxe.server`   | 66          | Host name or IP of the TFTP/HTTP server the client fetches its bootloader from |
| `pxe.bootfile` | 67          | Path (or full URL) of the boot file on that server                             |

Option 66 says *where*, option 67 says *what*.

```yaml
pxe:
  server: "10.50.1.20" # host name or IP — option 66 is a string field, both are valid
  bootfile: "snponly.efi" # or "http://boot.lab.local/ipxe.efi" for iPXE HTTP chainloading
```

### Both keys or neither

The block is entirely optional — omit it and options 66/67 are simply not set on the scope, which is the ordinary case. But **setting one key without the other is a hard error**, rejected by CI at PR time and by the API with `422 VALIDATION_ERROR`:

```yaml
# REJECTED — a boot server with no boot file leaves the host unbootable
pxe:
  server: "10.50.1.20"
```

There are exactly three legal states:

| Values file                     | Rendered body                          | DHCP server           |
| ------------------------------- | -------------------------------------- | --------------------- |
| no `pxe` block                  | `nextServer: ""`, `bootFile: ""`       | options 66/67 not set |
| both `server` and `bootfile`    | both carry the values                  | options 66/67 set     |
| only one of the two             | render succeeds, **CI and API reject** | —                     |

Both keys always appear in the rendered request body (as `""` when unset). GET reports `""` for an option the server does not have, so carrying the keys keeps the rendered body and the GET response identical rather than merely compatible.

Removing a `pxe` block from a values file that previously had one clears options 66 and 67 from the scope on the next reconciliation.

### What this does not cover

Per-architecture boot files — BIOS clients needing `undionly.kpxe` while UEFI clients need `ipxe.efi` — cannot be expressed here. That requires Windows DHCP **policies** matching option 93 (client architecture), which this API does not manage. A scope carries exactly one 66/67 pair.

Values are written to the DHCP server as-is; neither the API nor CI verifies that the boot server is reachable or that the file exists.

---

## Exclusions

Exclusions define IP ranges within the scope that are NOT distributed to clients (e.g. reserved for static assignments).

```yaml
exclusions:
  - startAddress: "10.20.30.1"
    endAddress: "10.20.30.10"
  - startAddress: "10.20.30.241"
    endAddress: "10.20.30.254"
```

Rules enforced by the API and CI validator:

- `endAddress` must be >= `startAddress` within each exclusion.
- All addresses must be within the scope subnet.
- No duplicate ranges (identical start+end pair).
- No overlapping ranges (ranges must not share any IP).
- **List must be in ascending IP numerical order.** The API always returns exclusions sorted by startAddress. If your values file has a different order, Crossplane will detect a mismatch and PUT every 60 seconds. Always list exclusions in ascending IP order.

Exclusions may cover addresses anywhere within the subnet — they do not need to fall inside `[startRange, endRange]`. Addresses outside the distribution range are never leased regardless, but including them as exclusions (e.g. the gateway or an infrastructure range below `startRange`) is valid.

**Gateway-in-range rule:** if `gateway` is inside `[startRange, endRange]`, it **must** be covered by an exclusion. Without one, the backend rejects the configuration with `422 VALIDATION_ERROR` to prevent the DHCP server from leasing the gateway IP to a client.

```yaml
# Gateway inside the distribution range — must be excluded:
startRange: "10.20.30.100"
endRange: "10.20.30.200"
gateway: "10.20.30.100"
exclusions:
  - startAddress: "10.20.30.100"
    endAddress: "10.20.30.100"

# Common pattern — gateway is below startRange, no exclusion needed:
startRange: "10.20.30.11"
endRange: "10.20.30.240"
gateway: "10.20.30.1"   # below startRange, never leased
```

To exclude no IPs, omit the key or set `exclusions: []`.

---

## Failover

Failover synchronizes the scope with a partner DHCP server so clients can get leases if the primary server is unavailable.

Two modes are supported:

### HotStandby

One server is active, the other is standby. The standby server only responds if the primary is unreachable.

```yaml
failover:
  partnerServer: "dhcp02.lab.local"
  relationshipName: "cluster-a-failover"
  mode: "HotStandby"
  serverRole: "Active" # role of THIS server: "Active" or "Standby"
  reservePercent: 5 # % of IPs reserved for the standby server (0–100)
  maxClientLeadTimeMinutes: 60
```

`serverRole` is required for HotStandby. `loadBalancePercent` is not used and can be omitted.

### LoadBalance

Both servers share the load. Each server responds to a configured percentage of requests.

```yaml
failover:
  partnerServer: "dhcp02.lab.local"
  relationshipName: "cluster-a-failover"
  mode: "LoadBalance"
  loadBalancePercent: 50 # % of requests handled by THIS server (0–100)
  maxClientLeadTimeMinutes: 60
```

`loadBalancePercent` is required for LoadBalance. `serverRole` and `reservePercent` are not used and can be omitted.

### Failover field reference

| Field                      | Type    | Required for | Constraints                       | Description                                                   |
| -------------------------- | ------- | ------------ | --------------------------------- | ------------------------------------------------------------- |
| `partnerServer`            | string  | both modes   | 1–255 chars                       | FQDN of the partner DHCP server                               |
| `relationshipName`         | string  | both modes   | 1–64 chars                        | Unique name for this failover relationship on the DHCP server |
| `mode`                     | string  | both modes   | `"HotStandby"` or `"LoadBalance"` | Failover mode                                                 |
| `serverRole`               | string  | HotStandby   | `"Active"` or `"Standby"`         | Role of THIS server in HotStandby mode                        |
| `reservePercent`           | integer | —            | 0–100, default 0                  | % of IPs reserved for standby (HotStandby only)               |
| `loadBalancePercent`       | integer | LoadBalance  | 0–100                             | % of requests handled by THIS server (LoadBalance only)       |
| `maxClientLeadTimeMinutes` | integer | both modes   | 1–1440                            | Max client lead time in minutes (up to 24 hours)              |

### Changing failover

Certain changes require the failover relationship to be removed and recreated:

- Changing `mode`
- Changing `relationshipName`
- Changing `partnerServer`
- Changing `serverRole` (HotStandby only)

Other changes (`reservePercent`, `loadBalancePercent`, `maxClientLeadTimeMinutes`) update in-place with `Set-DhcpServerv4Failover`.

### Removing failover

Set `failover: null` (or omit the key). Do not use `failover: {}` — Helm deep-merges an empty object, which leaves any inherited failover configuration intact.

---

The validator checks: IP format, subnet consistency, startRange/endRange ordering, gateway in subnet when set, gateway not inside `[startRange, endRange]` without a covering exclusion, exclusions in subnet, network/broadcast address rejection, no overlapping exclusions, and failover mode-specific required fields. `gateway: ""` is accepted and means DHCP option 3 is unset.

---

## Crossplane reconciliation

Crossplane polls the DHCP API every ~60 seconds per scope:

| Situation                  | Action                           |
| -------------------------- | -------------------------------- |
| Scope does not exist       | POST (create)                    |
| Scope differs from desired | PUT (update only changed fields) |
| CR deleted from Kubernetes | DELETE                           |

For reconciliation to be stable (no perpetual PUT loops), the GET response from the API must exactly match the rendered Helm payload. Common sources of drift:

- DNS server order in values file differs from DHCP server order.
- Exclusions not listed in ascending IP order.
- `description: null` in values (Helm renders `""`, API normalizes to `""` — safe).
- `failover: {}` instead of `failover: null` when disabling failover.
