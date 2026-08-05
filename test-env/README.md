# Windows DHCP test environment

Windows Server 2022 EC2 instances running the **DHCP Server role**, so the
PowerShell code paths run against a real `DhcpServer` module instead of mocks.

- **primary** — the box the API drives, over PSRP or co-located.
- **partner** — a second DHCP server, created only when
  `enable_failover_partner = true`, so `Add-DhcpServerv4Failover` has somewhere
  real to point. It never runs the API.

These are disposable test boxes. They are not a model for the production
air-gapped deployment, which has real leases and Active Directory.

## Why Windows, and where the API runs

The `DhcpServer` module is a wrapper over the Windows DHCP service and does not
exist for `pwsh` on Linux, so the cmdlets have to execute on Windows no matter
what. What is configurable is where the *API* sits, via `DHCP_TRANSPORT`
([`app/config.py`](../app/config.py)):

| `install_api` | Transport | Layout                                                       |
| ------------- | --------- | ------------------------------------------------------------ |
| `false` (default) | `psrp` | API on Linux/OpenShift, driving this host over WinRM         |
| `true`        | `local`   | API co-located here, `powershell.exe` as a local subprocess   |

The default matches production. It also introduces the one limitation that
matters for failover — see below.

## What testing costs: nothing per scope

A DHCP scope is a row in the Windows DHCP database (`dhcp.mdb`).
`Add-DhcpServerv4Scope` writes configuration and nothing else — **AWS never sees
it**. No VPC subnet is created, no addresses are allocated, no ENIs, no charge.
A scope does not need to correspond to a network that exists.

So you can create scopes on `10.20.30.0/24`, `192.168.50.0/24`,
`172.16.99.0/24`, and a hundred more simultaneously, and the bill is identical
to having none. Cost is the instance and its disk, full stop.

## Prerequisites

```bash
aws configure set region <region>     # required, no default in this config
aws sts get-caller-identity           # must succeed
```

## Deploy

```bash
cd test-env

cat > terraform.tfvars <<EOF
region       = "eu-central-1"
allowed_cidr = "$(curl -s https://checkip.amazonaws.com)/32"

# Second DHCP server, for the failover paths. Doubles the cost — leave it off
# unless you are exercising them.
enable_failover_partner = true
EOF

terraform init
terraform apply
```

Toggling `enable_failover_partner` later adds or removes the partner and its
security group rules only; the primary is not rebuilt.

First boot takes roughly 5–8 minutes per box: Windows boots, installs the DHCP
role, sets the Administrator password, and configures the WinRM HTTPS listener.
With `install_api = true` add another 4 minutes or so for Git, Python 3.12, the
clone and the pip install, after which the bootstrap polls its own `/healthz`
before declaring success. Both boxes bootstrap in parallel.

Confirm each finished by checking for `C:\bootstrap-complete.txt`:

```bash
terraform output -raw bootstrap_log_command | sh
```

```bash
curl -s "$(terraform output -raw api_base_url)/healthz"
```

## Exercising the routes across segments

```bash
API=$(terraform output -raw api_base_url)

create() {  # create() <network> <name>
  curl -s -X POST "$API/api/v1/scopes/$1" -H 'Content-Type: application/json' -d "{
    \"scopeName\": \"$2\",
    \"subnetMask\": \"255.255.255.0\",
    \"startRange\": \"$${1%.0}.50\",
    \"endRange\": \"$${1%.0}.200\",
    \"leaseDurationDays\": 8,
    \"description\": \"\",
    \"gateway\": \"$${1%.0}.1\",
    \"dnsServers\": [\"10.10.1.5\", \"10.10.1.6\"],
    \"dnsDomain\": \"lab.local\",
    \"exclusions\": [{\"startAddress\": \"$${1%.0}.1\", \"endAddress\": \"$${1%.0}.10\"}],
    \"failover\": null
  }"
}

create 10.20.30.0  cluster-a
create 192.168.50.0 cluster-b
create 172.16.99.0  cluster-c

create 10.20.30.0  cluster-a            # idempotent: must not fail (§2)
curl -s "$API/api/v1/scopes"            # sorted by scope address; items carry "scope" (§5)
curl -s "$API/api/v1/scopes/10.20.30.0" # must round-trip the POST body (§9)
curl -i -X DELETE "$API/api/v1/scopes/10.20.30.0"
curl -i -X DELETE "$API/api/v1/scopes/10.20.30.0"  # 204 again (§2)
```

What this catches that the 585 mock-based tests do not: real `ConvertTo-Json`
output, where .NET `IPAddress` properties serialize as
`{"IPAddressToString": ...}` dicts — the `_extract_ip_str()` contract in
`CLAUDE.md` §8 — plus real cmdlet error text and real idempotency behaviour.

## Failover

`enable_failover_partner = true` launches a second Windows DHCP server, so the
failover paths in `CLAUDE.md` §6 and §7 run against real cmdlets. What the
environment provides:

| Piece                       | How                                                            |
| --------------------------- | -------------------------------------------------------------- |
| A partner DHCP server       | Same bootstrap as the primary, `install_api` forced off         |
| A knowable `partnerServer`  | `partner_private_ip`, pinned (default `10.100.1.11`)            |
| Failover protocol           | TCP 647 between the two, source is the security group itself    |
| Remote configuration        | TCP 135 + 49152-65535, the RPC path `Add-DhcpServerv4Failover` uses |
| Credentials across the pair | The same local `Administrator` password on both boxes           |

That last row is the part that has no Active Directory to lean on. With no
domain, the only credential NTLM can carry between two workgroup servers is a
**mirrored local account** — same username, same password, both ends. Both boxes
get `admin_password`, and both set `LocalAccountTokenFilterPolicy = 1` so the
remote local account gets a full admin token instead of a UAC-filtered one.

This was verified on the built environment, from the primary against the
partner — an explicit credential over a DCOM CIM session authenticates and the
partner's DHCP database is readable remotely:

```powershell
$cred = Get-Credential Administrator     # the admin_password
$cs   = New-CimSession -ComputerName 10.100.1.11 -Credential $cred `
            -SessionOption (New-CimSessionOption -Protocol Dcom)
Get-DhcpServerv4Scope -CimSession $cs
```

So the ports and the credential model are both sound. What is *not* sound is
supplying that credential to `Add-DhcpServerv4Failover`, which has nowhere to
take it — see below.

### The double hop, and why `WINRM_AUTH=credssp` exists

All three failover cmdlets configure **both** servers: they write the local
relationship and reach the partner over RPC, as the calling user. Inside a PSRP
session that is a second hop, and neither NTLM nor plain Kerberos delegates — the
session authenticated with a network logon, which holds no reusable credential to
present to the partner. The local half succeeds and the partner half fails.

Measured on these two boxes, same commands, two logon types:

| Command | network logon | delegated credential |
| ------- | ------------- | -------------------- |
| `Add-DhcpServerv4Failover` | fails | OK |
| `Add-DhcpServerv4FailoverScope` | `Failed to update failover relationship ... on server <partner>` | OK |
| `Invoke-DhcpServerv4FailoverReplication` | `Failed to get superscope information on <partner>` | OK |

`WINRM_AUTH=credssp` is what supplies the delegated credential, and it is the
supported path — proven end to end against this environment, where the API
attached a new scope to the relationship and it replicated to the partner:

```
identity            : ec2amaz-e3mal2i\administrator
ADD FAILOVER SCOPE  : OK
REPLICATE TO PARTNER: OK
relationship holds  : 10.20.30.0, 10.20.31.0, 10.20.32.0
```

Only the box the API **connects to** needs CredSSP enabled. The partner is the
target of the delegated credential, not a CredSSP endpoint — it ran with
`CredSSP: false` throughout the test above. The bootstrap enables it on both
anyway, so either box can serve as the API's target.

Two things to know when reproducing this by hand:

**1. An interactive session on the primary also works.** An interactive or batch
logon holds the credential, so the hop succeeds as the mirrored `Administrator`.
Useful for exercising the PowerShell in §6/§7 without involving the API.

```bash
./rdp-tunnel.sh          # RDP to 127.0.0.1 as Administrator
```

```powershell
Add-DhcpServerv4Failover -Name 'dhcp-scope-test-failover' `
    -PartnerServer '10.100.1.11' -ScopeId 10.20.30.0 `
    -ServerRole Active -ReservePercent 5 `
    -MaxClientLeadTime (New-TimeSpan -Minutes 60) -Force

Get-DhcpServerv4Failover
Get-DhcpServerv4Failover -ComputerName 10.100.1.11   # the partner's own view
```

Then let the API read it back, which is the half that matters for
reconciliation — `GET` must return a `failover` object that a `PUT` can send
straight back (§9):

```bash
curl -s "$API/api/v1/scopes/10.20.30.0" | jq .failover
```

**Answered here, and it matters for §9 parity:** Windows stores `partnerServer`
**verbatim as given**. Created with `-PartnerServer 10.100.1.11`, the primary
reports exactly `10.100.1.11`, so a values file pinning the address round-trips
and Crossplane converges. It is not normalized to a resolved host name.

The partner's own view is not symmetric — it names the primary by NetBIOS
name (`EC2AMAZ-E3MAL2I`), not by address. Irrelevant while the API targets the
Active server, but it is why `DHCP_SERVER_HOST` must be one explicit host rather
than an alias that could resolve to either peer: the same relationship reports a
different `partnerServer` depending on which end answers.

**2. `aws ssm start-session` will not work for this.** Session Manager runs as
`SYSTEM`, whose outbound identity is the machine account — meaningless to a
workgroup partner. It fails identically to the un-delegated PSRP hop, which makes
it easy to misdiagnose as a firewall problem. Use RDP, or a scheduled task
registered with `-User Administrator -Password`, which gets a batch logon that
does carry the credential.

### Driving it through the API over CredSSP

The API connects to the primary and needs no interactive session:

```
DHCP_TRANSPORT=psrp
WINRM_AUTH=credssp
WINRM_USERNAME=Administrator
WINRM_PASSWORD=<admin_password>
WINRM_CERT_VALIDATION=false
```

One wrinkle when testing from a workstation: the HTTPS listener is created with
`Hostname` set to the Elastic IP, so WinRM rejects a request whose `Host:` header
says `127.0.0.1` with `HTTP 400 - Invalid Hostname`. The SSM tunnel therefore
does **not** work against the HTTPS listener; connect to the Elastic IP directly,
which needs this workstation's address in `winrm_allowed_cidrs` for the duration.
The HTTP listener EC2Launch creates has an empty `Hostname` and does not care.

**CredSSP does not require TLS transport.** Verified here against the HTTP
listener on 5985 with `AllowUnencrypted = false` — the failover cmdlets ran and
replicated exactly as they did over 5986:

```
WINRM_PORT=5985
WINRM_USE_SSL=false
```

That combination looks wrong and is not: CredSSP negotiates its own TLS channel
and encrypts at the message level, which is what satisfies WinRM's
`AllowUnencrypted = false`. It matters because a hardened site may publish only
an HTTP listener by GPO, leaving no HTTPS endpoint to point `WINRM_USE_SSL=true`
at. `WINRM_CERT_VALIDATION` is then irrelevant, and no CA bundle has to be baked
into the API image.

### Two things that look broken and are not

Both verified on this environment, from the primary against the partner:

```powershell
Test-NetConnection 10.100.1.11 -Port 135    # True  — RPC endpoint mapper
Test-NetConnection 10.100.1.11 -Port 647    # False — before any relationship
Test-Connection    10.100.1.11              # False — always, see below
```

**Port 647 is closed until a relationship exists.** The DHCP server binds it
only once it has a failover partner to talk to; `Get-NetTCPConnection -LocalPort
647` returns nothing on a freshly built box. So a closed 647 before you run
`Add-DhcpServerv4Failover` is the expected state, not a blocked security group.
Check 135 instead — that is the port the cmdlet needs, and it answers from boot.

**Remote WMI/CIM to the partner fails with "The RPC server is unavailable"**
even though port 135 answers. The endpoint mapper is open, but Windows Firewall
leaves the *WMI* DCOM rules disabled by default, so the dynamic port it hands
back is refused. This is unrelated to failover — the DHCP role opens its own RPC
rules and the failover path does not go through WMI — but it will mislead you if
you reach for `Get-CimInstance` as a connectivity check. Enable it on the
partner if you want remote diagnostics (not applied by the bootstrap, so a
rebuilt partner loses it):

```powershell
Enable-NetFirewallRule -DisplayGroup 'Windows Management Instrumentation (WMI)'
```

**Ping fails between the two boxes even though the security group allows it.**
Windows Firewall drops inbound ICMP echo by default, and nothing in the DHCP
role changes that. Reaching for `ping` first here produces a false negative on a
perfectly healthy pair. Enable it explicitly if you want it:

```powershell
Enable-NetFirewallRule -Name 'FPS-ICMP4-ERQ-In'
```

### Teardown of the pair

`Remove-DhcpServerv4FailoverScope` then `Remove-DhcpServerv4Failover` (§7) both
reach the partner too, so they carry the same constraint. A `DELETE` issued over
PSRP against a scope in a relationship will abort at the detach step — which is
the documented behaviour (it retries next cycle), but here it retries forever.

## Iterating on code

The instance clones from the public GitHub repo, so it runs what is **pushed**,
not your local working tree. After pushing:

```bash
aws ssm start-session --target "$(terraform output -raw instance_id)"
```

```powershell
C:\refresh-app.ps1     # git pull + pip install + restart the API task
```

To rebuild from scratch instead, `terraform apply` after any bootstrap change
replaces the instance (`user_data_replace_on_change = true`).

## Troubleshooting

Two ways in, neither needing a key pair or an inbound port: SSM Session Manager
for a shell, and an SSM port-forwarding tunnel for the DHCP management console
(`dhcpmgmt.msc`).

```bash
./rdp-tunnel.sh                        # then RDP to 127.0.0.1
./winrm-tunnel.sh                      # then PSRP to https://127.0.0.1:15986

TARGET=partner ./rdp-tunnel.sh 3390    # same, against the failover partner
TARGET=partner ./winrm-tunnel.sh 15987
```

A failover relationship is only half visible from either end — the primary shows
the relationship it created, the partner shows the scope it received — so
diagnosing one usually means opening both consoles.

Both need `session-manager-plugin` (`brew install --cask session-manager-plugin`).

### Why the tunnel instead of an inbound RDP rule

`rdp_allowed_cidr` defaults to `""`, so **no inbound 3389 rule exists**. Pinning
a workstation address there is what makes RDP fail intermittently: the address
comes from your ISP and rotates on its own, and once it does, the rule names
someone else's address. The client reports

```
We couldn't connect to the remote PC ... Error code: 0x204
```

which reads as though the instance is down, when it is running fine and merely
unreachable from wherever you now are. Note this is **not** caused by stopping
and starting the instance — the Elastic IP reattaches and the server's address
never changes. It is your end that moved.

The tunnel is built on the instance's outbound SSM channel, so no rule has to
know your address and it works from any network. Set `rdp_allowed_cidr` only if
you deliberately want direct exposure.

`winrm_allowed_cidrs` still pins the cluster's NAT egress address, which is
correct — a NAT gateway address is stable, and the cluster cannot open a tunnel.
Only the *workstation* entry was the fragile one, and `winrm-tunnel.sh` replaces
it.

| Path                        | Contents                                     |
| --------------------------- | -------------------------------------------- |
| `C:\bootstrap.log`          | Full bootstrap transcript                    |
| `C:\bootstrap-complete.txt` | Present only on success                      |
| `C:\bootstrap-FAILED.txt`   | Present only on failure, holds the exception |
| `C:\api.log`                | uvicorn stdout/stderr                        |

```powershell
Get-ScheduledTaskInfo -TaskName DhcpScopeApi
Get-DhcpServerv4Scope                     # what the API is actually managing
Get-Service dhcpserver
```

## Cost and teardown

`t3.small` Windows runs roughly **$0.04/hour** (region-dependent). Nothing here
is free-tier eligible — Windows AMIs never are. `enable_failover_partner = true`
doubles every line below, since the partner is the same instance type with the
same disk and its own Elastic IP.

Stop the instances between sessions. Compute and the Windows licence both stop
billing; the disks and the Elastic IPs do not:

```bash
terraform output -raw stop_command    # then start_command to resume
```

`stop_command` covers both boxes deliberately. Parking only one leaves the
relationship in `COMMUNICATION-INTERRUPTED`, which reads as a broken
configuration rather than a stopped instance.

| Parked (stopped), per instance  | Cost         |
| ------------------------------- | ------------ |
| 50 GB gp3 root volume           | ~$4.00/month |
| Elastic IP                      | ~$3.60/month |
| Instance hours, Windows licence | $0           |

The Elastic IP is what makes stop/start cheap in *time*: the address, the WinRM
certificate subject, and `DHCP_SERVER_HOST` all survive a restart, so resuming
is a start command rather than a rebuild. AWS bills every public IPv4 at
$0.005/hour, so while the box runs the EIP costs exactly what the auto-assigned
address did; the ~$3.60/month is only the parked case.

`use_spot = true` cuts the hourly rate roughly 60–70%, at the risk of AWS
reclaiming the box on two minutes' notice. It also removes stopping as an
option: the config requests a `one-time` Spot instance, and those can only be
terminated.

```bash
terraform destroy       # releases the Elastic IP too
```

Terminating loses everything on the disk — every scope lives in the Windows DHCP
database there. Rebuilding is a single `terraform apply` (~8–12 min); the
`terraform.tfvars`, the state file, and the IAM setup from `iam-bootstrap.sh` all
survive a destroy.
