output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.dhcp.id
}

output "public_ip" {
  description = "Public IP of the Windows DHCP server."
  value       = aws_instance.dhcp.public_ip
}

output "api_base_url" {
  description = "Base URL of the DHCP scope manager API."
  value       = "http://${aws_instance.dhcp.public_ip}:${var.api_port}"
}

output "healthz_command" {
  description = "Smoke test. Returns 200 once the bootstrap finishes."
  value       = "curl -s http://${aws_instance.dhcp.public_ip}:${var.api_port}/healthz"
}

output "session_manager_command" {
  description = "Open a PowerShell session on the instance. No key pair or open port needed."
  value       = "aws ssm start-session --target ${aws_instance.dhcp.id} --region ${var.region}"
}

output "stop_command" {
  description = "Stop the instance when idle — billing drops to the EBS volume only."
  value       = "aws ec2 stop-instances --instance-ids ${aws_instance.dhcp.id} --region ${var.region}"
}

output "start_command" {
  description = "Restart a stopped instance. The API comes back via its AtStartup scheduled task."
  value       = "aws ec2 start-instances --instance-ids ${aws_instance.dhcp.id} --region ${var.region}"
}
