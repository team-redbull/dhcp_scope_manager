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

# Windows Server 2022 Core — no desktop GUI. The DHCP role and the DhcpServer
# PowerShell module are fully supported on Core, and access is via SSM Session
# Manager, so the GUI would only cost RAM and disk.
data "aws_ssm_parameter" "windows_ami" {
  name = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Core-Base"
}

locals {
  name = var.name_prefix

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

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.100.1.0/24"
  map_public_ip_on_launch = true

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

# The only ingress. Shell access is via SSM Session Manager, which is outbound
# only, so no RDP or WinRM port is exposed.
resource "aws_vpc_security_group_ingress_rule" "api" {
  security_group_id = aws_security_group.instance.id
  description       = "DHCP scope manager API"
  cidr_ipv4         = var.allowed_cidr
  from_port         = var.api_port
  to_port           = var.api_port
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
  ami                    = data.aws_ssm_parameter.windows_ami.value
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
    repo_url  = var.repo_url
    repo_ref  = var.repo_ref
    api_port  = var.api_port
    api_token = var.api_token
  })

  # Changing the bootstrap should rebuild the box, otherwise the running
  # instance silently diverges from the config that produced the plan.
  user_data_replace_on_change = true

  tags = merge(local.tags, { Name = "${local.name}-server" })
}
