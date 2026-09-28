#!/usr/bin/env bash
#
# Open an RDP tunnel to the Windows DHCP server over SSM Session Manager, then
# point a Remote Desktop client at 127.0.0.1:<local-port>.
#
# Why a tunnel rather than an inbound 3389 rule: a security group rule has to
# name your workstation's address, and that address is handed out by your ISP
# and rotates without warning. When it does, RDP fails with "We couldn't connect
# to the remote PC" (0x204) and nothing about the instance has actually changed.
# The tunnel is established from the instance's own outbound SSM channel, so it
# keeps working from any network — home, office, tethered, VPN — with no rule to
# maintain and no inbound port open to the internet at all.
#
# Requires: aws CLI, session-manager-plugin, and terraform state in this dir.
#   brew install --cask session-manager-plugin

set -euo pipefail

cd "$(dirname "$0")"

# Defaults to 3389 — the same port a Remote Desktop client assumes when the
# address has no ":port" suffix, so "127.0.0.1" alone connects. A different
# local port works too but must then be typed as "127.0.0.1:<port>"; omitting it
# sends the client to 3389, finds nothing listening, and looks like the tunnel
# failed. 3389 is above 1024, so binding it needs no privileges.
LOCAL_PORT="${1:-3389}"

# TARGET=partner opens the failover partner instead. Both boxes run the DHCP
# console, and a failover relationship is only half visible from one side: the
# primary shows the relationship it created, the partner shows the scope it
# received. Default stays the primary, which is the box the API drives.
#   TARGET=partner ./rdp-tunnel.sh 3390
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

# The partner outputs resolve to this sentinel rather than failing when
# enable_failover_partner is false, so catch it here instead of handing "n/a"
# to the CLI as an instance ID.
case "$INSTANCE_ID" in
  n/a*)
    echo "No failover partner in this environment." >&2
    echo "  Set enable_failover_partner = true in terraform.tfvars, then apply." >&2
    exit 1
    ;;
esac

# A stopped instance has no SSM agent to tunnel through, and the error the CLI
# gives for that ("TargetNotConnected") does not mention the instance state.
STATE="$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" --region "$REGION" \
  --query 'Reservations[].Instances[].State.Name' --output text)"

if [ "$STATE" != "running" ]; then
  echo "Instance $INSTANCE_ID is '$STATE', not 'running'." >&2
  echo "Start it first:  $(terraform output -raw start_command)" >&2
  exit 1
fi

# The agent registers with SSM a little after the instance reports running, so a
# freshly started box can be 'running' and still not be a valid tunnel target.
PING="$(aws ssm describe-instance-information \
  --region "$REGION" \
  --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
  --query 'InstanceInformationList[].PingStatus' --output text 2>/dev/null || true)"

if [ "$PING" != "Online" ]; then
  echo "SSM agent is not Online yet (status: ${PING:-none})." >&2
  echo "A just-started instance usually needs 30-60s. Retry shortly." >&2
  exit 1
fi

# On the default port the suffix is omitted, because that is what the client
# assumes anyway and showing ":3389" invites typing it into hosts that reject it.
if [ "$LOCAL_PORT" = "3389" ]; then
  LOCAL_PORT_SUFFIX=""
else
  LOCAL_PORT_SUFFIX=":$LOCAL_PORT"
fi

cat <<EOF

  RDP tunnel -> $TARGET $INSTANCE_ID ($REGION)

  Connect a Remote Desktop client to:  127.0.0.1${LOCAL_PORT_SUFFIX}
    macOS: Windows App -> Add PC -> "127.0.0.1${LOCAL_PORT_SUFFIX}"

  Username: Administrator
  Password: terraform output -raw admin_password   (or see terraform.tfvars)

  Then run dhcpmgmt.msc for the DHCP console.
  Leave this terminal open — closing it closes the tunnel. Ctrl-C to stop.

EOF

exec aws ssm start-session \
  --target "$INSTANCE_ID" \
  --region "$REGION" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "portNumber=3389,localPortNumber=$LOCAL_PORT"
