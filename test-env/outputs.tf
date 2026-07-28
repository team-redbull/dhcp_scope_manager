output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.dhcp.id
}

output "public_ip" {
  description = "Public IP of the Windows DHCP server."
  value       = aws_instance.dhcp.public_ip
}

output "rdp_target" {
  description = <<-EOT
    Open this in a Remote Desktop client, log in as Administrator with
    admin_password, then run dhcpmgmt.msc for the DHCP console.
    On macOS: Windows App (formerly Microsoft Remote Desktop) from the App Store.
  EOT
  value       = "${aws_instance.dhcp.public_ip}:3389"
}

output "winrm_endpoint" {
  description = "WinRM HTTPS endpoint the API connects to over PSRP."
  value       = "https://${aws_instance.dhcp.public_ip}:5986"
}

output "api_env" {
  description = <<-EOT
    Environment for the Linux-hosted API. Certificate validation is off because
    the listener uses a self-signed certificate — correct for a throwaway box,
    not for production.
  EOT
  value = join("\n", [
    "DHCP_TRANSPORT=psrp",
    "DHCP_SERVER_HOST=${aws_instance.dhcp.public_ip}",
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
  value       = var.install_api ? "http://${aws_instance.dhcp.public_ip}:${var.api_port}" : "n/a — install_api = false; the API runs on Linux over PSRP"
}

output "session_manager_command" {
  description = "Open a PowerShell session on the instance. No key pair or open port needed."
  value       = "aws ssm start-session --target ${aws_instance.dhcp.id} --region ${var.region}"
}

output "bootstrap_log_command" {
  description = "Tail the bootstrap transcript to confirm setup finished."
  value       = "aws ssm start-session --target ${aws_instance.dhcp.id} --region ${var.region} --document-name AWS-StartInteractiveCommand --parameters command='Get-Content C:\\bootstrap.log -Tail 40'"
}

output "stop_command" {
  description = "Stop the instance when idle — billing drops to the EBS volume only."
  value       = "aws ec2 stop-instances --instance-ids ${aws_instance.dhcp.id} --region ${var.region}"
}

output "start_command" {
  description = <<-EOT
    Restart a stopped instance. NOTE: the public IP changes on restart, which
    invalidates both the WinRM certificate subject and DHCP_SERVER_HOST.
    Re-run terraform apply, or attach an Elastic IP, if you plan to stop/start.
  EOT
  value       = "aws ec2 start-instances --instance-ids ${aws_instance.dhcp.id} --region ${var.region}"
}
