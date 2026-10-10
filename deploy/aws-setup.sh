#!/usr/bin/env bash
# AWS side of Sidekick: Claude on Bedrock + CloudWatch for ShopLite running on Akamai Cloud. Run from the repo root
# after `aws login` (an admin session), with the VM from deploy/provision.sh:
#
#   BUDGET=50 ALERT_EMAIL=you@example.com bash deploy/aws-setup.sh <vm-ip>
#
# Creates (idempotent):
#   • a monthly cost budget with email alerts at 80% / 100%, and a budget ACTION that attaches a deny-all policy to both
#     Sidekick IAM users when spend hits 100% (AWS budgets alone don't stop spending)
#   • IAM user sidekick-agent           → Claude on Bedrock + read-only CloudWatch (Sidekick's brain and debugging tools)
#   • IAM user akamai-cloudwatch-shipper → cloudwatch:PutMetricData into ShopLite/Akamai ONLY (runs on the Akamai VM)
#   • CloudWatch dashboard ShopLite-on-Akamai and alarms (p95 latency, 5xx, VM CPU)
# Then wires the prod VM (shipper keys in sidekick/.env) and prints the Sidekick keys for Integrations → AWS.
# Access keys are saved under deploy/.keys/ (gitignored). Region: us-west-2 (override with AWS_REGION).
set -euo pipefail
cd "$(dirname "$0")/.."
VM="$1"; : "${BUDGET:?set BUDGET (USD per month)}"; : "${ALERT_EMAIL:?set ALERT_EMAIL for budget alerts}"
REGION="${AWS_REGION:-us-west-2}"; NS="ShopLite/Akamai"; KEYS=deploy/.keys
aws() { "${AWS_CLI:-aws}" --region "$REGION" --output json "$@"; }
PY="$(command -v python3 || command -v python || command -v py)"
key_field() { "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['AccessKey'][sys.argv[2]])" "$@"; }  # $1 file, $2 field
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
echo "✓ AWS account $ACCOUNT · $REGION"
TMP="$KEYS/tmp.$$"; mkdir -p "$TMP"; trap 'rm -rf "$TMP"' EXIT   # relative path: works for both Linux and Windows aws builds

# ---- IAM: least-privilege users ----
cat > "$TMP/agent.json" <<EOF
{"Version": "2012-10-17", "Statement": [
  {"Sid": "ClaudeOnBedrock", "Effect": "Allow", "Resource": "*", "Action": [
    "bedrock-mantle:CreateInference", "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
    "bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles"]},
  {"Sid": "ReadCloudWatch", "Effect": "Allow", "Resource": "*", "Action": [
    "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms"]}]}
EOF
cat > "$TMP/shipper.json" <<EOF
{"Version": "2012-10-17", "Statement": [{"Sid": "PutShopLiteMetricsOnly", "Effect": "Allow", "Action": "cloudwatch:PutMetricData",
  "Resource": "*", "Condition": {"StringEquals": {"cloudwatch:namespace": "$NS"}}}]}
EOF
make_user() {  # $1 user, $2 policy file → access key saved to deploy/.keys/aws-$1.json (only created once)
  aws iam get-user --user-name "$1" >/dev/null 2>&1 || aws iam create-user --user-name "$1" --tags Key=project,Value=sidekick >/dev/null
  aws iam put-user-policy --user-name "$1" --policy-name "$1-access" --policy-document "file://$2"
  if [ ! -f "$KEYS/aws-$1.json" ]; then
    aws iam create-access-key --user-name "$1" > "$KEYS/aws-$1.json" && chmod 600 "$KEYS/aws-$1.json"
  fi
  echo "✓ IAM user $1"
}
make_user sidekick-agent "$TMP/agent.json"
make_user akamai-cloudwatch-shipper "$TMP/shipper.json"

# ---- money guardrails: budget + alerts + automatic deny when the limit is hit ----
echo '{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"*","Resource":"*"}]}' > "$TMP/deny.json"
KILL_ARN="arn:aws:iam::$ACCOUNT:policy/SidekickKillSwitch"
aws iam get-policy --policy-arn "$KILL_ARN" >/dev/null 2>&1 || aws iam create-policy --policy-name SidekickKillSwitch --policy-document "file://$TMP/deny.json" >/dev/null
echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"budgets.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > "$TMP/trust.json"
echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["iam:AttachUserPolicy","iam:DetachUserPolicy"],"Resource":"*"}]}' > "$TMP/role.json"
aws iam get-role --role-name SidekickBudgetActions >/dev/null 2>&1 || aws iam create-role --role-name SidekickBudgetActions --assume-role-policy-document "file://$TMP/trust.json" >/dev/null
aws iam put-role-policy --role-name SidekickBudgetActions --policy-name attach-kill-switch --policy-document "file://$TMP/role.json"
if ! aws budgets describe-budget --account-id "$ACCOUNT" --budget-name sidekick-hackathon >/dev/null 2>&1; then
  aws budgets create-budget --account-id "$ACCOUNT" \
    --budget "{\"BudgetName\":\"sidekick-hackathon\",\"BudgetLimit\":{\"Amount\":\"$BUDGET\",\"Unit\":\"USD\"},\"TimeUnit\":\"MONTHLY\",\"BudgetType\":\"COST\"}" \
    --notifications-with-subscribers "[
      {\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":80,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]},
      {\"Notification\":{\"NotificationType\":\"FORECASTED\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]}]"
fi
if [ "$(aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT" --budget-name sidekick-hackathon --query 'length(Actions)' --output text)" = "0" ]; then
  aws budgets create-budget-action --account-id "$ACCOUNT" --budget-name sidekick-hackathon --notification-type ACTUAL \
    --action-type APPLY_IAM_POLICY --action-threshold ActionThresholdValue=100,ActionThresholdType=PERCENTAGE \
    --definition "{\"IamActionDefinition\":{\"PolicyArn\":\"$KILL_ARN\",\"Users\":[\"sidekick-agent\",\"akamai-cloudwatch-shipper\"]}}" \
    --execution-role-arn "arn:aws:iam::$ACCOUNT:role/SidekickBudgetActions" --approval-model AUTOMATIC \
    --subscribers "[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]" >/dev/null
fi
echo "✓ budget sidekick-hackathon: \$$BUDGET/month, alerts to $ALERT_EMAIL, auto-deny for both Sidekick users at 100%"

# ---- CloudWatch: dashboard + alarms for ShopLite on Akamai ----
m() { echo "[\"$NS\",\"$1\",\"Environment\",\"prod\",\"Route\",\"ALL\",{\"stat\":\"$2\",\"label\":\"$3\"}]"; }
cat > "$TMP/dash.json" <<EOF
{"widgets": [
 {"type":"text","x":0,"y":0,"width":24,"height":2,"properties":{"markdown":"## ShopLite production — running on **Akamai Cloud**, observed in **Amazon CloudWatch**\nShipped every minute by \`akamai-cloudwatch\` on the Akamai VM · namespace \`$NS\` · debugged by Sidekick (Claude on Bedrock)"}},
 {"type":"metric","x":0,"y":2,"width":12,"height":6,"properties":{"title":"Latency (ms)","region":"$REGION","view":"timeSeries","period":60,"metrics":[$(m LatencyP95 Maximum p95),$(m LatencyAvg Average avg)]}},
 {"type":"metric","x":12,"y":2,"width":12,"height":6,"properties":{"title":"Requests and 5xx errors / min","region":"$REGION","view":"timeSeries","period":60,"metrics":[$(m Requests Sum requests),$(m Errors5xx Sum 5xx)]}},
 {"type":"metric","x":0,"y":8,"width":12,"height":6,"properties":{"title":"Akamai VM (sidekick-prod)","region":"$REGION","view":"timeSeries","period":60,"metrics":[["$NS","HostCPUUtilization","Host","sidekick-prod",{"label":"CPU %"}],["$NS","HostMemoryUtilization","Host","sidekick-prod",{"label":"Memory %"}]]}},
 {"type":"metric","x":12,"y":8,"width":12,"height":6,"properties":{"title":"Orders / min","region":"$REGION","view":"timeSeries","period":60,"metrics":[["$NS","Orders","Environment","prod",{"stat":"Sum","label":"orders"}]]}},
 {"type":"alarm","x":0,"y":14,"width":24,"height":3,"properties":{"title":"Alarms","alarms":["arn:aws:cloudwatch:$REGION:$ACCOUNT:alarm:ShopLite-prod-p95-latency-high","arn:aws:cloudwatch:$REGION:$ACCOUNT:alarm:ShopLite-prod-5xx-errors","arn:aws:cloudwatch:$REGION:$ACCOUNT:alarm:Akamai-VM-cpu-high"]}}]}
EOF
aws cloudwatch put-dashboard --dashboard-name ShopLite-on-Akamai --dashboard-body "file://$TMP/dash.json" >/dev/null
ALL="Name=Environment,Value=prod Name=Route,Value=ALL"
aws cloudwatch put-metric-alarm --alarm-name ShopLite-prod-p95-latency-high --namespace "$NS" --metric-name LatencyP95 --dimensions $ALL \
  --statistic Maximum --period 60 --evaluation-periods 5 --datapoints-to-alarm 3 --threshold 800 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-description "p95 latency of ShopLite on Akamai above 800 ms in 3 of the last 5 minutes"
aws cloudwatch put-metric-alarm --alarm-name ShopLite-prod-5xx-errors --namespace "$NS" --metric-name Errors5xx --dimensions $ALL \
  --statistic Sum --period 300 --evaluation-periods 1 --threshold 5 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-description "More than 5 server errors in 5 minutes on ShopLite (Akamai)"
aws cloudwatch put-metric-alarm --alarm-name Akamai-VM-cpu-high --namespace "$NS" --metric-name HostCPUUtilization --dimensions Name=Host,Value=sidekick-prod \
  --statistic Average --period 60 --evaluation-periods 10 --datapoints-to-alarm 8 --threshold 85 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-description "Akamai VM sidekick-prod CPU above 85% for 8 of 10 minutes"
echo "✓ CloudWatch dashboard ShopLite-on-Akamai + 3 alarms"

# ---- wire the Akamai VM: shipper credentials ----
SK="$(key_field "$KEYS/aws-akamai-cloudwatch-shipper.json" AccessKeyId)"; SS="$(key_field "$KEYS/aws-akamai-cloudwatch-shipper.json" SecretAccessKey)"
ssh -i "$HOME/.ssh/id_ed25519" "root@$VM" "ENVF=/opt/apps/sidekick/sidekick/.env; sed -i '/^CW_AWS_/d;/^CW_SYNTHETIC_TRAFFIC=/d' \$ENVF
printf 'CW_AWS_ACCESS_KEY_ID=%s\nCW_AWS_SECRET_ACCESS_KEY=%s\nCW_AWS_REGION=%s\nCW_SYNTHETIC_TRAFFIC=1\n' '$SK' '$SS' '$REGION' >> \$ENVF
cd /opt/apps/sidekick/sidekick && sudo -u deploy docker compose up -d --build akamai-cloudwatch >/dev/null && echo '✓ akamai-cloudwatch shipping from the VM'"
echo
echo "Sidekick → Integrations → AWS: region $REGION, access key $(key_field "$KEYS/aws-sidekick-agent.json" AccessKeyId) (secret in $KEYS/aws-sidekick-agent.json)"
echo "Dashboard: https://$REGION.console.aws.amazon.com/cloudwatch/home?region=$REGION#dashboards/dashboard/ShopLite-on-Akamai"
