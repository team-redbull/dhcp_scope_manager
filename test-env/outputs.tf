output "instance_id" {
  description = <<-EOT
    EC2 instance ID of the primary — the box the API drives. Consumed by the
    tunnel scripts, which reach the partner through partner_instance_id instead.
  EOT
  value       = aws_instance.dhcp["primary"].id
}

output "public_ip" {
  description = "Elastic IP of the Windows DHCP server. Stable across stop/start."
  value       = aws_eip.this["primary"].public_ip
}

output "region" {
  description = "Region the environment is deployed in. Consumed by the tunnel scripts."
  value       = var.region
}

output "admin_password" {
  description = "Local Administrator password, for RDP and for NTLM over WinRM."
  value       = var.admin_password
  sensitive   = true
}

output "rdp_target" {
  description = <<-EOT
    Where to point a Remote Desktop client. Log in as Administrator with
    admin_password, then run dhcpmgmt.msc for the DHCP console.
    On macOS: Windows App (formerly Microsoft Remote Desktop) from the App Store.

    With rdp_allowed_cidr unset (the default) there is no inbound 3389 rule and
    the target is the local end of the SSM tunnel — start it with ./rdp-tunnel.sh.
  EOT
  value       = var.rdp_allowed_cidr == "" ? "127.0.0.1 (run ./rdp-tunnel.sh first)" : "${aws_eip.this["primary"].public_ip}:3389"
}

output "rdp_tunnel_command" {
  description = <<-EOT
    Open the RDP tunnel. Works from any network without a security group change,
    which direct RDP does not: a pinned workstation CIDR goes stale whenever the
    ISP reassigns your address, and the resulting failure (client error 0x204)
    looks like the instance is down when it is only unreachable from where you
    happen to be sitting.
  EOT
  value       = "./rdp-tunnel.sh"
}

output "winrm_tunnel_command" {
  description = "Open a WinRM/5986 tunnel for testing the PSRP path from this workstation."
  value       = "./winrm-tunnel.sh"
}

output "winrm_endpoint" {
  description = "WinRM HTTPS endpoint the API connects to over PSRP."
  value       = "https://${aws_eip.this["primary"].public_ip}:5986"
}

output "api_env" {
  description = <<-EOT
    Environment for the Linux-hosted API. Certificate validation is off because
    the listener uses a self-signed certificate — correct for a throwaway box,
    not for production.
  EOT
  value = join("\n", [
    "DHCP_TRANSPORT=psrp",
    "DHCP_SERVER_HOST=${aws_eip.this["primary"].public_ip}",
    "WINRM_PORT=5986",
    "WINRM_USE_SSL=true",
    "WINRM_AUTH=ntlm",
    "WINRM_USERNAME=Administrator",
    "WINRM_PASSWORD=<admin_password>",
    "WINRM_CERT_VALIDATION=false",
  ])
}

output "api_base_url" {
  description = "Base URL of the co-located API. Only meaningful when install_api = true."
  value       = var.install_api ? "http://${aws_eip.this["primary"].public_ip}:${var.api_port}" : "n/a — install_api = false; the API runs on Linux over PSRP"
}

output "session_manager_command" {
  description = "Open a PowerShell session on the instance. No key pair or open port needed."
  value       = "aws ssm start-session --target ${aws_instance.dhcp["primary"].id} --region ${var.region}"
}

output "bootstrap_log_command" {
  description = "Tail the bootstrap transcript to confirm setup finished."
  value       = "aws ssm start-session --target ${aws_instance.dhcp["primary"].id} --region ${var.region} --document-name AWS-StartInteractiveCommand --parameters command='Get-Content C:\\bootstrap.log -Tail 40'"
}

output "stop_command" {
  description = <<-EOT
    Stop every instance when idle — billing drops to the EBS volumes only.
    Covers the failover partner too when one exists; parking only half a pair
    leaves the relationship in COMMUNICATION-INTERRUPTED, which looks like a
    configuration fault rather than a stopped box.
  EOT
  value       = "aws ec2 stop-instances --instance-ids ${join(" ", [for i in aws_instance.dhcp : i.id])} --region ${var.region}"
}

output "start_command" {
  description = <<-EOT
    Restart the stopped instances. Each Elastic IP reattaches automatically, so
    the addresses, the WinRM certificate subjects, and DHCP_SERVER_HOST all stay
    valid. The partner's pinned private address returns unchanged as well, so
    existing failover relationships still name the right server.
  EOT
  value       = "aws ec2 start-instances --instance-ids ${join(" ", [for i in aws_instance.dhcp : i.id])} --region ${var.region}"
}

################################################################################
# Failover partner
#
# All of these read "n/a" when enable_failover_partner = false rather than being
# omitted, so a missing partner is visible in `terraform output` instead of
# looking like a typo in the output name.
################################################################################

output "partner_instance_id" {
  description = "EC2 instance ID of the failover partner. Target for the tunnel scripts via TARGET=partner."
  value       = var.enable_failover_partner ? aws_instance.dhcp["partner"].id : "n/a - enable_failover_partner = false"
}

output "partner_public_ip" {
  description = "Elastic IP of the partner, for RDP and the DHCP console. Not used by the API."
  value       = var.enable_failover_partner ? aws_eip.this["partner"].public_ip : "n/a - enable_failover_partner = false"
}

output "partner_server" {
  description = <<-EOT
    The value to put in the payload's failover.partnerServer.

    The private address, not the public one: the failover protocol and the RPC
    that configures it both run inside the VPC, and the public address routes out
    through the internet gateway where the security group's self-reference does
    not apply.

    Whether GET returns this string verbatim is exactly what this environment
    exists to find out — Windows may normalize it to a resolved host name, which
    would break the GET/PUT parity in CLAUDE.md §9 for any values file that
    pinned an address instead. Check with:
      curl -s "$API/api/v1/scopes/<scope>" | jq .failover.partnerServer
  EOT
  value       = var.enable_failover_partner ? aws_instance.dhcp["partner"].private_ip : "n/a - enable_failover_partner = false"
}

output "partner_session_manager_command" {
  description = "Open a PowerShell session on the partner, to inspect the relationship from its side."
  value = var.enable_failover_partner ? (
    "aws ssm start-session --target ${aws_instance.dhcp["partner"].id} --region ${var.region}"
  ) : "n/a - enable_failover_partner = false"
}

output "failover_payload" {
  description = <<-EOT
    A failover block for POST/PUT, ready to paste. HotStandby with this server
    Active, so the partner holds the reserve — the mode the production MCE pairs
    use. See test-env/README.md for the full request and for why the API cannot
    create the relationship over PSRP.
  EOT
  value = var.enable_failover_partner ? jsonencode({
    partnerServer            = aws_instance.dhcp["partner"].private_ip
    relationshipName         = "${local.name}-failover"
    mode                     = "HotStandby"
    serverRole               = "Active"
    reservePercent           = 5
    loadBalancePercent       = 0
    maxClientLeadTimeMinutes = 60
  }) : "n/a - enable_failover_partner = false"
}
