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

variable "rdp_allowed_cidr" {
  description = <<-EOT
    CIDR allowed to reach RDP (3389), for the DHCP management console. This is
    a human sitting at a laptop, so it is your own address.
    Find it with: curl -s https://checkip.amazonaws.com
  EOT
  type        = string

  validation {
    condition     = can(cidrhost(var.rdp_allowed_cidr, 0)) && var.rdp_allowed_cidr != "0.0.0.0/0"
    error_message = "rdp_allowed_cidr must be a valid CIDR and must not be 0.0.0.0/0."
  }
}

variable "winrm_allowed_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach WinRM (5986) — the port the API drives DHCP through.

    This is deliberately separate from rdp_allowed_cidr, because it is a
    *different source*. The API does not call from your laptop; it calls from
    wherever it runs. For a cluster-hosted API that is the cluster's egress
    (NAT gateway) address, not your workstation.

    Find a Kubernetes/OpenShift cluster's egress with:
      oc run egress --image=registry.access.redhat.com/ubi9/ubi-minimal \
        --restart=Never --rm -i --command -- curl -s https://checkip.amazonaws.com

    Check every worker node — a cluster with one NAT gateway per AZ has more
    than one egress address, and allowing only some produces intermittent
    failures that look like flakiness.
  EOT
  type        = list(string)

  validation {
    condition = alltrue([
      for c in var.winrm_allowed_cidrs : can(cidrhost(c, 0)) && c != "0.0.0.0/0"
    ])
    error_message = "Each winrm_allowed_cidrs entry must be a valid CIDR and must not be 0.0.0.0/0."
  }
}

variable "admin_password" {
  description = <<-EOT
    Password set on the local Administrator account, used for both RDP and for
    NTLM authentication over WinRM.

    NTLM rather than Kerberos because this box is a standalone workgroup server
    with no Active Directory to authenticate against. In production, prefer a
    domain-joined server and Kerberos so no password is stored at all.

    Lands in EC2 user-data, readable by any principal holding
    ec2:DescribeInstanceAttribute — use a throwaway, never a reused password.
  EOT
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.admin_password) >= 14
    error_message = "admin_password must be at least 14 characters (Windows policy rejects weak passwords)."
  }
}

variable "install_api" {
  description = <<-EOT
    Install and run the FastAPI service on this Windows box (the co-located
    DHCP_TRANSPORT=local layout).

    Leave false when the API runs elsewhere — on Linux, driving this host over
    PSRP. The box is then purely a DHCP server plus a WinRM endpoint, and the
    bootstrap does not depend on the application repo being reachable.
  EOT
  type        = bool
  default     = false
}

variable "ami_id" {
  description = <<-EOT
    Explicit Windows Server 2022 AMI. Empty means "latest", resolved from the
    SSM public parameter.

    Pin this when the environment is meant to mirror a fixed production
    baseline: the SSM alias floats to each month's new image, so an otherwise
    unchanged terraform apply can silently rebuild on a different OS build.

    Note that AWS only keeps a few months of Windows AMIs published; images
    older than that are deregistered and cannot be launched.
  EOT
  type        = string
  default     = ""
}

variable "instance_type" {
  description = <<-EOT
    t3.small (2 vCPU / 2 GB) — Microsoft's documented minimum for Windows
    Server 2022 with Desktop Experience, and the cheapest Windows-capable type
    that AWS still counts as free-tier eligible.

    2 GB is workable here specifically because install_api defaults to false:
    the memory-hungry step was always the pip install, and with the API running
    on Linux this box only carries the DHCP role, WinRM, and an RDP session.
    Set install_api = true and this should go up.

    An account on the FREE plan can only launch free-tier-eligible types; the
    eligible x86 options with more memory (c7i-flex.large, m7i-flex.large) cost
    4x or more once the Windows licence is included.
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
  description = <<-EOT
    GB. Windows Server 2022 with Desktop Experience needs ~40 before updates;
    50 leaves room for Windows Update and the DHCP database without a
    mid-test disk-full failure.
  EOT
  type        = number
  default     = 50
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
