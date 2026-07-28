# Windows DHCP test environment

A single Windows Server 2022 **Core** EC2 instance running both the **DHCP
Server role** and **this API**, so the PowerShell code paths run against a real
`DhcpServer` module instead of mocks.

This is a disposable test box. It is not a model for the production air-gapped
deployment, which has real leases and failover.

## Why one instance, and why Windows

[`app/services/ps_executor.py`](../app/services/ps_executor.py) spawns
`powershell.exe` as a local subprocess — no `-ComputerName`, no WinRM, no remote
transport anywhere in the codebase. Every cmdlet therefore targets localhost, so
the API and the DHCP role must be co-located, and the host must be Windows: the
`DhcpServer` module is a wrapper over the Windows DHCP service and does not
exist for `pwsh` on Linux. Running the API on Linux would mean adding a
WinRM/PSRP transport, which is a feature, not a config change.

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
EOF

terraform init
terraform apply
```

First boot takes roughly 8–12 minutes: Windows boots, installs the DHCP role,
Git, and Python 3.12, clones the repo, and starts uvicorn. The bootstrap polls
its own `/healthz` before declaring success.

```bash
curl -s "$(terraform output -raw api_base_url)/healthz"
```

## Exercising the routes across segments

```bash
API=$(terraform output -raw api_base_url)

create() {  # create() <network> <name>
  curl -s -X POST "$API/api/v1/scopes/$1" -H 'Content-Type: application/json' -d "{
    \"scopeName\": \"$2\",
    \"network\": \"$1\",
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
curl -s "$API/api/v1/scopes"            # sorted by network address (§5)
curl -s "$API/api/v1/scopes/10.20.30.0" # must round-trip the POST body (§9)
curl -i -X DELETE "$API/api/v1/scopes/10.20.30.0"
curl -i -X DELETE "$API/api/v1/scopes/10.20.30.0"  # 204 again (§2)
```

What this catches that the 585 mock-based tests do not: real `ConvertTo-Json`
output, where .NET `IPAddress` properties serialize as
`{"IPAddressToString": ...}` dicts — the `_extract_ip_str()` contract in
`CLAUDE.md` §8 — plus real cmdlet error text and real idempotency behaviour.

## Not covered: failover

`CLAUDE.md` §6 and §7 depend on `Add-DhcpServerv4Failover` against a partner
server, which needs a second DHCP server and, in practice, Active Directory —
failover between workgroup machines is unreliable to configure. Those paths stay
mock-only here. A second instance plus a domain controller is a meaningfully
larger and more expensive build.

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

Access is via SSM Session Manager — no RDP, no key pair, no inbound admin port.
Server Core has no GUI, so this is the only shell.

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

`t3.small` Windows runs roughly **$0.03/hour** (region-dependent), plus about
**$2.40/month** for the 30 GB gp3 volume. Nothing here is free-tier eligible —
Windows AMIs never are.

The instance is the entire cost, so stop it between sessions and pay only for
the disk:

```bash
terraform output -raw stop_command    # then start_command to resume
```

`use_spot = true` cuts the hourly rate roughly 60–70%, at the risk of AWS
reclaiming the box on two minutes' notice.

```bash
terraform destroy
```
