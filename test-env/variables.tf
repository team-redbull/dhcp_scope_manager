variable "region" {
  description = "AWS region to deploy the test environment into."
  type        = string
}

variable "allowed_cidr" {
  description = <<-EOT
    CIDR allowed to reach the DHCP API port. No default on purpose — set this
    to your own address, e.g. "203.0.113.4/32".
    Find it with: curl -s https://checkip.amazonaws.com
  EOT
  type        = string

  validation {
    condition     = can(cidrhost(var.allowed_cidr, 0)) && var.allowed_cidr != "0.0.0.0/0"
    error_message = "allowed_cidr must be a valid CIDR and must not be 0.0.0.0/0."
  }
}

variable "instance_type" {
  description = <<-EOT
    t3.small (2 vCPU / 2 GB) is the practical floor for Windows Server 2022
    Core running the DHCP role plus uvicorn. t3.micro's 1 GB thrashes during
    pip install and is not worth the few cents saved.
  EOT
  type        = string
  default     = "t3.small"
}

variable "use_spot" {
  description = <<-EOT
    Run as a Spot instance (~60-70% cheaper). Safe for a throwaway test box;
    the tradeoff is AWS can reclaim it with two minutes' notice, which loses
    any scopes created since launch.
  EOT
  type        = bool
  default     = false
}

variable "root_volume_size" {
  description = "GB. Windows Server 2022 Core needs ~32; 30 is the documented floor for the AMI."
  type        = number
  default     = 30
}

variable "repo_url" {
  description = "Public Git URL the instance clones the API from."
  type        = string
  default     = "https://github.com/team-redbull/dhcp_scope_manager.git"
}

variable "repo_ref" {
  description = "Branch, tag, or commit to check out."
  type        = string
  default     = "main"
}

variable "api_port" {
  description = "Port uvicorn binds. Matches the app's PORT setting default."
  type        = number
  default     = 8080
}

variable "api_token" {
  description = <<-EOT
    Value for DHCP_API_TOKEN on the instance. Empty disables auth, which is
    the app's own default. Anything set here lands in EC2 user-data, readable
    by any principal holding ec2:DescribeInstanceAttribute — use a throwaway.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "name_prefix" {
  description = "Prefix for tagged resource names."
  type        = string
  default     = "dhcp-scope-test"
}
