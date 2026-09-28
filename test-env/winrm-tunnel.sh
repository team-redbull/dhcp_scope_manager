#!/usr/bin/env bash
#
# Open a WinRM/5986 tunnel over SSM, for testing the PSRP path from this
# workstation without keeping a workstation address in winrm_allowed_cidrs.
#
# The cluster's own entry (the NAT egress address) still belongs in that list —
# a NAT gateway address is stable and the cluster cannot tunnel. This script
# only removes the need for the *workstation* entry, which is the one that goes
# stale, for the same reason described in rdp-tunnel.sh.
#
# With the tunnel up, point the API at localhost:
#
#   DHCP_TRANSPORT=psrp
#   DHCP_SERVER_HOST=127.0.0.1
#   WINRM_PORT=15986
#   WINRM_USE_SSL=true
#   WINRM_AUTH=ntlm
#   WINRM_USERNAME=Administrator
#   WINRM_PASSWORD=<admin_password>
#   WINRM_CERT_VALIDATION=false
#
# Certificate validation must stay off here — the listener's self-signed
# certificate has the Elastic IP as its subject, so it will not match
# 127.0.0.1. That is already the setting the api_env output recommends for this
# throwaway box, and is not acceptable in production either way.

set -euo pipefail

cd "$(dirname "$0")"

LOCAL_PORT="${1:-15986}"

# TARGET=partner tunnels to the failover partner instead — useful for running
# Get-DhcpServerv4Failover from its side. Default is the primary.
TARGET="${TARGET:-primary}"

if ! command -v session-manager-plugin >/dev/null 2>&1; then
  echo "session-manager-plugin is not installed." >&2
  echo "  brew install --cask session-manager-plugin" >&2
  exit 1
fi

case "$TARGET" in
  primary) OUTPUT_NAME="instance_id" ;;
  partner) OUTPUT_NAME="partner_instance_id" ;;
  *) echo "TARGET must be 'primary' or 'partner', got '$TARGET'." >&2; exit 1 ;;
esac

INSTANCE_ID="$(terraform output -raw "$OUTPUT_NAME")"
REGION="$(terraform output -raw region 2>/dev/null || echo us-east-1)"

case "$INSTANCE_ID" in
  n/a*)
    echo "No failover partner in this environment." >&2
    echo "  Set enable_failover_partner = true in terraform.tfvars, then apply." >&2
    exit 1
    ;;
esac

STATE="$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" --region "$REGION" \
  --query 'Reservations[].Instances[].State.Name' --output text)"

if [ "$STATE" != "running" ]; then
  echo "Instance $INSTANCE_ID is '$STATE', not 'running'." >&2
  echo "Start it first:  $(terraform output -raw start_command)" >&2
  exit 1
fi

echo
echo "  WinRM tunnel -> $TARGET $INSTANCE_ID   https://127.0.0.1:$LOCAL_PORT"
echo "  Ctrl-C to stop. Leave this terminal open."
echo

exec aws ssm start-session \
  --target "$INSTANCE_ID" \
  --region "$REGION" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "portNumber=5986,localPortNumber=$LOCAL_PORT"
