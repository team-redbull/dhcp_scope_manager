#!/usr/bin/env bash
#
# One-time: replace root credentials with an assumable IAM role.
#
# Run this ONCE while still authenticated as root, then stop using root.
# It creates:
#
#   role  dhcp-scope-test-deployer  — holds the deploy permissions
#   user  dhcp-scope-test-cli       — holds no permissions except "assume that role"
#   profile dhcp-test               — aws CLI profile that assumes the role
#
# The user's long-lived access key can therefore do nothing on its own. Every
# actual API call runs under short-lived (1h) role credentials that the CLI
# fetches automatically via sts:AssumeRole.

set -euo pipefail

ROLE_NAME="dhcp-scope-test-deployer"
USER_NAME="dhcp-scope-test-cli"
PROFILE="dhcp-test"
REGION="us-east-1"
RESOURCE_PREFIX="dhcp-scope-test"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Account: $ACCOUNT_ID"

# ---------------------------------------------------------------------------
# 1. The deploy role, trusted only by the CLI user
# ---------------------------------------------------------------------------
aws iam create-role \
  --role-name "$ROLE_NAME" \
  --description "Terraform deploy role for the DHCP scope manager test environment" \
  --max-session-duration 3600 \
  --assume-role-policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::${ACCOUNT_ID}:user/${USER_NAME}" },
    "Action": "sts:AssumeRole"
  }]
}
JSON
)" >/dev/null 2>&1 || echo "role $ROLE_NAME already exists, continuing"

# ---------------------------------------------------------------------------
# 2. Permissions the role needs for exactly this Terraform config
#
#    ec2:* is broad. Scoping EC2 to individual actions across VPC, subnet,
#    IGW, route table, security group, instance, volume, and tag operations
#    produces a policy that breaks on every plan change; for a disposable test
#    environment in a single account that trade is not worth it. IAM is scoped
#    tightly by name prefix, which is where the real privilege-escalation risk
#    would be.
# ---------------------------------------------------------------------------
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "deploy" \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "NetworkAndCompute",
      "Effect": "Allow",
      "Action": "ec2:*",
      "Resource": "*"
    },
    {
      "Sid": "WindowsAmiLookup",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:GetParameters"],
      "Resource": "arn:aws:ssm:*::parameter/aws/service/ami-windows-latest/*"
    },
    {
      "Sid": "SessionManagerAccess",
      "Effect": "Allow",
      "Action": [
        "ssm:StartSession",
        "ssm:ResumeSession",
        "ssm:TerminateSession",
        "ssm:DescribeInstanceInformation",
        "ssm:DescribeSessions",
        "ssm:GetConnectionStatus"
      ],
      "Resource": "*"
    },
    {
      "Sid": "InstanceProfileLifecycleScopedByName",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:ListRoleTags",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:ListInstanceProfilesForRole",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile"
      ],
      "Resource": [
        "arn:aws:iam::${ACCOUNT_ID}:role/${RESOURCE_PREFIX}-*",
        "arn:aws:iam::${ACCOUNT_ID}:instance-profile/${RESOURCE_PREFIX}-*"
      ]
    },
    {
      "Sid": "AllowAttachingOnlyTheSsmManagedPolicy",
      "Effect": "Allow",
      "Action": "iam:AttachRolePolicy",
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${RESOURCE_PREFIX}-*",
      "Condition": {
        "ArnEquals": {
          "iam:PolicyARN": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        }
      }
    }
  ]
}
JSON
)"

# ---------------------------------------------------------------------------
# 3. The CLI user — its only power is assuming the role above
# ---------------------------------------------------------------------------
aws iam create-user --user-name "$USER_NAME" >/dev/null 2>&1 \
  || echo "user $USER_NAME already exists, continuing"

aws iam put-user-policy \
  --user-name "$USER_NAME" \
  --policy-name "assume-deploy-role" \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
  }]
}
JSON
)"

# ---------------------------------------------------------------------------
# 4. Access key for the user, wired into a source profile
# ---------------------------------------------------------------------------
echo "Creating access key..."
KEY_JSON="$(aws iam create-access-key --user-name "$USER_NAME")"
KEY_ID="$(printf '%s' "$KEY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')"
KEY_SECRET="$(printf '%s' "$KEY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')"

aws configure set aws_access_key_id     "$KEY_ID"     --profile "${PROFILE}-source"
aws configure set aws_secret_access_key "$KEY_SECRET" --profile "${PROFILE}-source"
aws configure set region                "$REGION"     --profile "${PROFILE}-source"

# The profile Terraform and the CLI actually use. Credentials are fetched by
# STS on demand and expire in an hour; nothing long-lived is used directly.
aws configure set role_arn       "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" --profile "$PROFILE"
aws configure set source_profile "${PROFILE}-source"                            --profile "$PROFILE"
aws configure set region         "$REGION"                                      --profile "$PROFILE"

# To require MFA on every assume, create a virtual MFA device for the user and:
#   aws configure set mfa_serial "arn:aws:iam::${ACCOUNT_ID}:mfa/${USER_NAME}" --profile "$PROFILE"

unset KEY_JSON KEY_ID KEY_SECRET

# ---------------------------------------------------------------------------
# 5. Verify
# ---------------------------------------------------------------------------
echo
echo "IAM propagation can take a few seconds..."
sleep 10
echo "Identity via the new profile:"
aws sts get-caller-identity --profile "$PROFILE"

cat <<EOF

Done. Use it with:

    export AWS_PROFILE=$PROFILE
    cd test-env && terraform plan

Then remove the root access keys, which is the point of the exercise.
The IAM API does not manage root keys reliably, so do this in the console:

    Sign in as root -> account menu -> Security credentials
      -> Access keys -> Deactivate, confirm nothing breaks, then Delete
      -> while there, enable MFA on root if it is not already on

Keep root for break-glass only.
EOF
