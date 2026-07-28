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
#   RDP   — a human at a laptop, opening the DHCP console.
#   WinRM — the API, calling from wherever it runs. When the API runs in a
#           cluster, the source is that cluster's egress/NAT address, NOT the
#           laptop. Allowing only the laptop here silently black-holes every
#           call the API makes.
#
# SSM Session Manager remains available for shell access and needs no ingress.

resource "aws_vpc_security_group_ingress_rule" "api" {
  count = var.install_api ? 1 : 0

  security_group_id = aws_security_group.instance.id
  description       = "DHCP scope manager API (co-located layout only)"
  cidr_ipv4         = var.allowed_cidr
  from_port         = var.api_port
  to_port           = var.api_port
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "rdp" {
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
  description       = "WinRM over HTTPS (PSRP transport from the API host)"
  cidr_ipv4         = each.value
  from_port         = 5986
  to_port           = 5986
  ip_protocol       = "tcp"
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
  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
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

  user_data = templatefile("${path.module}/bootstrap.ps1.tftpl", {
    repo_url       = var.repo_url
    repo_ref       = var.repo_ref
    api_port       = var.api_port
    api_token      = var.api_token
    admin_password = var.admin_password
    install_api    = var.install_api
  })

  # Changing the bootstrap should rebuild the box, otherwise the running
  # instance silently diverges from the config that produced the plan.
  user_data_replace_on_change = true

  tags = merge(local.tags, { Name = "${local.name}-server" })
}
