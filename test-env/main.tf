terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# Windows Server 2022 with Desktop Experience — the GUI is the point here: the
# DHCP management console (dhcpmgmt.msc) does not exist on Core, and inspecting
# scopes visually is a stated requirement for this environment.
#
# Windows Server 2022 is always version 21H2 (there is no other), and
# Get-DhcpServerVersion reports 10.0 on it, so both track the OS release rather
# than the patch level. What the monthly AMI changes is the cumulative-update
# build number, which does not alter the DhcpServer cmdlet surface.
data "aws_ssm_parameter" "windows_ami" {
  count = var.ami_id == "" ? 1 : 0
  name  = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
}

locals {
  name = var.name_prefix

  ami_id = var.ami_id != "" ? var.ami_id : data.aws_ssm_parameter.windows_ami[0].value

  tags = {
    Project   = "dhcp_scope_manager"
    Purpose   = "test-environment"
    ManagedBy = "terraform"
  }

  # One entry per Windows box. The partner exists only to give
  # Add-DhcpServerv4Failover a real server to point at; it is otherwise
  # identical to the primary, minus the API.
  instances = merge(
    {
      primary = {
        # No pinned address: the primary already holds an AWS-assigned one, and
        # setting private_ip here would replace a running instance for nothing.
        private_ip      = null
        instance_suffix = "server"
        eip_suffix      = "eip"
      }
    },
    var.enable_failover_partner ? {
      partner = {
        private_ip      = var.partner_private_ip
        instance_suffix = "partner"
        eip_suffix      = "partner-eip"
      }
    } : {}
  )
}

################################################################################
# Network
#
# A dedicated VPC rather than the default one. The instance runs a DHCP server,
# and keeping it off any shared subnet removes all doubt about interference.
################################################################################

resource "aws_vpc" "this" {
  cidr_block           = "10.100.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.tags, { Name = "${local.name}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.tags, { Name = "${local.name}-igw" })
}

# Not every AZ offers every instance type. Left unspecified, AWS picks an AZ for
# the subnet and the instance then fails to launch there (us-east-1e does not
# offer t3.small, for one). Pin the subnet to an AZ that actually offers the
# type, chosen deterministically so a re-apply does not move the subnet.
data "aws_ec2_instance_type_offerings" "supported" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.100.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = sort(data.aws_ec2_instance_type_offerings.supported.locations)[0]

  tags = merge(local.tags, { Name = "${local.name}-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(local.tags, { Name = "${local.name}-public" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "instance" {
  name        = "${local.name}-sg"
  description = "DHCP scope manager test instance"
  vpc_id      = aws_vpc.this.id

  tags = merge(local.tags, { Name = "${local.name}-sg" })
}

# Ingress is split by *source*, not just by port, because the two consumers are
# different machines:
#
#   RDP   — a human at a laptop, opening the DHCP console. Off by default:
#           laptop addresses are dynamic, so a pinned rule goes stale on its
#           own and RDP fails with 0x204 for no visible reason. Use the tunnel.
#   WinRM — the API, calling from wherever it runs. When the API runs in a
#           cluster, the source is that cluster's egress/NAT address, NOT the
#           laptop. Allowing only the laptop here silently black-holes every
#           call the API makes. That address is a stable NAT gateway, so
#           pinning it here is sound in a way pinning a laptop is not.
#
# SSM Session Manager remains available for shell access and needs no ingress.
# It also carries the RDP and WinRM tunnels — see rdp-tunnel.sh / winrm-tunnel.sh.

resource "aws_vpc_security_group_ingress_rule" "api" {
  count = var.install_api ? 1 : 0

  security_group_id = aws_security_group.instance.id
  description       = "DHCP scope manager API (co-located layout only)"
  cidr_ipv4         = var.allowed_cidr
  from_port         = var.api_port
  to_port           = var.api_port
  ip_protocol       = "tcp"
}

# Created only when rdp_allowed_cidr is set. The default is "" — RDP normally
# arrives through the Session Manager tunnel (./rdp-tunnel.sh), which needs no
# ingress and so cannot be invalidated by the workstation's address changing.
resource "aws_vpc_security_group_ingress_rule" "rdp" {
  count = var.rdp_allowed_cidr == "" ? 0 : 1

  security_group_id = aws_security_group.instance.id
  description       = "RDP for the DHCP management console"
  cidr_ipv4         = var.rdp_allowed_cidr
  from_port         = 3389
  to_port           = 3389
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "winrm" {
  for_each = toset(var.winrm_allowed_cidrs)

  security_group_id = aws_security_group.instance.id
  # ASCII only: EC2 rejects rule descriptions outside a-zA-Z0-9._-:/()#,@[]+=&;{}!$*
  description = "WinRM over HTTPS (PSRP transport from the API host)"
  cidr_ipv4   = each.value
  from_port   = 5986
  to_port     = 5986
  ip_protocol = "tcp"
}

# Server-to-server, for failover only. Source is the security group itself
# rather than a CIDR, so it covers both boxes without naming either address and
# survives the partner being rebuilt.
#
# Two distinct channels, and both are needed — allowing only 647 produces a
# relationship that cannot be *created*, and allowing only RPC produces one that
# is created and then never replicates:
#
#   647   — the DHCP failover protocol itself. The servers exchange BNDUPD /
#           BNDACK lease bindings over it for the life of the relationship.
#   135   — RPC endpoint mapper. Add-DhcpServerv4Failover configures *both*
#           servers, and reaches the partner over RPC.
#   49152+ — the port the endpoint mapper hands back. Windows negotiates a
#           dynamic high port per call, so the range cannot be narrowed without
#           reconfiguring RPC on both hosts.
resource "aws_vpc_security_group_ingress_rule" "failover_protocol" {
  count = var.enable_failover_partner ? 1 : 0

  security_group_id            = aws_security_group.instance.id
  description                  = "DHCP failover protocol between the two servers"
  referenced_security_group_id = aws_security_group.instance.id
  from_port                    = 647
  to_port                      = 647
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "failover_rpc_epmap" {
  count = var.enable_failover_partner ? 1 : 0

  security_group_id            = aws_security_group.instance.id
  description                  = "RPC endpoint mapper (Add-DhcpServerv4Failover configures the partner)"
  referenced_security_group_id = aws_security_group.instance.id
  from_port                    = 135
  to_port                      = 135
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "failover_rpc_dynamic" {
  count = var.enable_failover_partner ? 1 : 0

  security_group_id            = aws_security_group.instance.id
  description                  = "RPC dynamic port range negotiated through 135"
  referenced_security_group_id = aws_security_group.instance.id
  from_port                    = 49152
  to_port                      = 65535
  ip_protocol                  = "tcp"
}

# Not required by failover. Note this rule alone does NOT make the two boxes
# pingable: Windows Firewall drops inbound echo by default, so a healthy pair
# still fails Test-Connection and it is a misleading first diagnostic. Verified
# on this environment — 135 answered while ICMP did not. To actually use it:
#   Enable-NetFirewallRule -Name 'FPS-ICMP4-ERQ-In'
resource "aws_vpc_security_group_ingress_rule" "failover_icmp" {
  count = var.enable_failover_partner ? 1 : 0

  security_group_id            = aws_security_group.instance.id
  description                  = "ICMP echo between the two servers, for reachability checks"
  referenced_security_group_id = aws_security_group.instance.id
  from_port                    = 8
  to_port                      = -1
  ip_protocol                  = "icmp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.instance.id
  description       = "Outbound for Windows Update, Python, Git, and SSM"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

################################################################################
# Instance role — SSM Session Manager only
################################################################################

resource "aws_iam_role" "instance" {
  name = "${local.name}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.name
}

################################################################################
# Instance
################################################################################

resource "aws_instance" "dhcp" {
  for_each = local.instances

  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  private_ip             = each.value.private_ip
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  dynamic "instance_market_options" {
    for_each = var.use_spot ? [1] : []

    content {
      market_type = "spot"

      spot_options {
        # Terminate rather than hibernate/stop: the box is disposable and
        # rebuilding it is a single terraform apply.
        spot_instance_type             = "one-time"
        instance_interruption_behavior = "terminate"
      }
    }
  }

  # The Elastic IP is allocated independently of the instance, so its address is
  # known before first boot and can be baked into the WinRM certificate. This
  # creates no cycle: the EIP does not reference the instance; only the
  # association does, and it runs after both.
  #
  # The partner never runs the API regardless of var.install_api: it is a DHCP
  # server and nothing else. Its Administrator password is the primary's, which
  # is what makes the cross-server RPC work at all — with no Active Directory,
  # matching local accounts on both hosts is the only credential NTLM can use.
  user_data = templatefile("${path.module}/bootstrap.ps1.tftpl", {
    repo_url       = var.repo_url
    repo_ref       = var.repo_ref
    api_port       = var.api_port
    api_token      = var.api_token
    admin_password = var.admin_password
    install_api    = each.key == "primary" ? var.install_api : false
    public_ip      = aws_eip.this[each.key].public_ip
  })

  # Changing the bootstrap should rebuild the box, otherwise the running
  # instance silently diverges from the config that produced the plan.
  user_data_replace_on_change = true

  tags = merge(local.tags, { Name = "${local.name}-${each.value.instance_suffix}" })
}

# The primary predates the partner and was a single un-keyed resource. Without
# these, adding for_each reads as "destroy the server, create server[primary]"
# and takes the running box with it.
moved {
  from = aws_instance.dhcp
  to   = aws_instance.dhcp["primary"]
}

moved {
  from = aws_eip.this
  to   = aws_eip.this["primary"]
}

moved {
  from = aws_eip_association.this
  to   = aws_eip_association.this["primary"]
}

################################################################################
# Elastic IP
#
# An auto-assigned public IP is released on stop and a different one comes back
# on start. That breaks more than the address in a bookmark: bootstrap.ps1.tftpl
# bakes the public IP into the WinRM certificate subject and the listener
# hostname, and its user_data is <persist>false</persist>, so it never re-runs
# to correct them. DHCP_SERVER_HOST in the api_env output goes stale too.
#
# An Elastic IP survives stop/start, so the box can be parked between sessions
# and resume on the same address with the same certificate.
#
# Cost: AWS bills every public IPv4 at $0.005/hour. While the instance runs this
# replaces the auto-assigned address at identical cost; while it is stopped the
# EIP keeps billing (~$3.60/month) where the auto-assigned address would have
# been free. That is the price of an address that does not move.
################################################################################

resource "aws_eip" "this" {
  for_each = local.instances

  domain = "vpc"

  # An address in this VPC cannot route before the gateway exists.
  depends_on = [aws_internet_gateway.this]

  tags = merge(local.tags, { Name = "${local.name}-${each.value.eip_suffix}" })
}

resource "aws_eip_association" "this" {
  for_each = local.instances

  instance_id   = aws_instance.dhcp[each.key].id
  allocation_id = aws_eip.this[each.key].id
}
