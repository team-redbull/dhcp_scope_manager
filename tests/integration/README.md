# Live DHCP integration tests

These run the real API against a real Windows DHCP server — no mocked
PowerShell. They are **skipped by default**; nothing happens unless you set
`DHCP_IT=1`.

The rest of the suite mocks the transport, so it verifies which cmdlet strings
the service builds and never what Windows does with them. Two bugs lived behind
that seam: POST silently discarding half its payload on an existing scope, and an
under-specified PUT wiping PXE options and exclusions. Both are now covered here.

## What they touch

One scope: `10.77.88.0/24`, created and deleted by each test.
`Add-DhcpServerv4Scope` writes a row in `dhcp.mdb` and nothing else — no
addresses are allocated and no network has to exist — so the address cannot
collide with real infrastructure. Each test deletes the scope on the way in and
on the way out, so the suite is order-independent and safe to re-run after a
failure.

They will not touch any other scope on the server.

## Running them against the EC2 test environment

The WinRM HTTPS listener binds hostname `<elastic-ip>`, so a tunnelled
`https://127.0.0.1` is rejected by HTTP.SYS with `400 Invalid Hostname`. Two ways
around it — pick one.

### A. Direct to the Elastic IP (matches production transport)

Requires your address in `winrm_allowed_cidrs` in `terraform.tfvars`.

```bash
cd test-env
export DHCP_IT=1
export DHCP_TRANSPORT=psrp
export DHCP_SERVER_HOST="$(terraform output -raw public_ip)"
export WINRM_PORT=5986
export WINRM_USE_SSL=true
export WINRM_AUTH=ntlm          # credssp if the test exercises failover — see CLAUDE.md §10
export WINRM_USERNAME=Administrator
export WINRM_PASSWORD="$(terraform output -raw admin_password)"
export WINRM_CERT_VALIDATION=false
cd .. && pytest tests/integration -v
```

### B. Over the SSM tunnel (no security-group entry needed)

The HTTP listener on 5985 has no hostname binding, so it accepts a tunnelled
request — but **the local port must be 5985 too**, because HTTP.SYS matches the
port in the `Host` header against its `+:5985` reservation. Connect by *name*
(`localhost`), not by IP: a raw-IP `Host` header is refused.

NTLM applies message-level encryption over HTTP, and the SSM tunnel is itself TLS
end-to-end, so nothing crosses the network in the clear. This is a convenience for
a disposable test box; production uses HTTPS as in option A.

```bash
aws ssm start-session --target "$(cd test-env && terraform output -raw instance_id)" \
  --region us-east-1 --document-name AWS-StartPortForwardingSession \
  --parameters "portNumber=5985,localPortNumber=5985"
```

Then, in a second shell:

```bash
export DHCP_IT=1
export DHCP_TRANSPORT=psrp
export DHCP_SERVER_HOST=localhost   # not 127.0.0.1 — HTTP.SYS rejects a raw-IP Host header
export WINRM_PORT=5985
export WINRM_USE_SSL=false
export WINRM_AUTH=ntlm
export WINRM_USERNAME=Administrator
export WINRM_PASSWORD="$(cd test-env && terraform output -raw admin_password)"
pytest tests/integration -v
```

## Failover

No failover coverage here yet. `Add-DhcpServerv4Failover` acts on the *partner*
as the calling user, so it needs `WINRM_AUTH=credssp` and
`enable_failover_partner = true` (CLAUDE.md §10). Worth adding once the CredSSP
path is exercised routinely.
