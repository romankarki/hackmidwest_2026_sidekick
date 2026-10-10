# Connect AWS (and Bedrock) to Sidekick

**Time:** ~30–40 minutes · **Cost:** a few dollars for a hackathon if you follow the guardrails

Sidekick uses AWS for two things:
1. **The model** — Claude on **Amazon Bedrock** (Configure → Model).
2. **Your infrastructure** — reading CloudWatch metrics, EKS, EC2, RDS, Kinesis… so it can root-cause incidents.

You'll do it in this order — **money guardrails first**, then a locked-down user for Sidekick, then Bedrock, then (optionally) a Kubernetes cluster.

> Prices below are approximate on-demand US prices — always check the official pricing pages. Region used throughout: **us-east-1 (N. Virginia)**.

---

## Step 0 — An AWS account you control

1. Sign in at https://console.aws.amazon.com (or create an account).
2. **Turn on MFA for the root user:** top-right account name → **Security credentials** → **Assign MFA device**.
3. Top-right region picker → choose **US East (N. Virginia) us-east-1**.

> 💡 New accounts may be on AWS's **Free plan** (credits, no charges until you upgrade). Check **Billing and Cost Management → Free tier / Plan** to see what you're on. Some services or models may not be available until you upgrade — if Step 4's test fails with an access error, that's the first thing to check.

---

## Step 1 — Spending guardrails (do this before anything else)

### 1a. A budget with email alerts
1. Console search bar → **Budgets** → **Create budget**.
2. Choose **Use a template** → **Monthly cost budget**.
3. Budget name: `sidekick-hackathon` · Budgeted amount: **$20** (pick your own limit).
4. Email recipients: your email → **Create budget**.

The template sets up email alerts as actual and forecasted spend approach your limit (you can edit the thresholds after creating it).

### 1b. A zero-spend early warning (optional but nice)
**Budgets** → **Create budget** → template **Zero spend budget** → your email → create.
You get an email the moment *anything* costs more than $0.01, so surprises show up on day one, not at month end.

### 1c. Cost anomaly alerts
Console search → **Cost Anomaly Detection** → **Create monitor** → *AWS services* → alert subscription with your email, threshold **$5**.

### 1d. (Recommended) Automatic kill switch at your limit
AWS budgets don't hard-stop spending by themselves — but a **budget action** can automatically attach a "deny everything" policy to Sidekick's user when you hit the limit, so the agent can't spend another cent.

1. **IAM → Policies → Create policy → JSON**, paste, name it `SidekickKillSwitch`:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{ "Effect": "Deny", "Action": "*", "Resource": "*" }]
   }
   ```
2. **IAM → Roles → Create role** → *AWS service* → use case **Budgets** → next → attach an inline policy that lets Budgets attach that policy:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["iam:AttachUserPolicy", "iam:DetachUserPolicy"],
       "Resource": "*"
     }]
   }
   ```
   Name the role `SidekickBudgetActions`.
3. Finish **Step 2** first (create the `sidekick-agent` user), then come back:
   **Budgets → `sidekick-hackathon` → Actions → Create action** → *Apply IAM policy* → policy `SidekickKillSwitch` → target user `sidekick-agent` → threshold **100%** of budgeted amount → role `SidekickBudgetActions` → **Automatically run this action**.

When spend reaches your limit, Sidekick's AWS access switches off. To restore it, detach `SidekickKillSwitch` from the user.

---

## Step 2 — A locked-down IAM user for Sidekick

Never give Sidekick your root or admin credentials.

1. **IAM → Users → Create user** → name `sidekick-agent` → **don't** give console access → Next.
2. **Attach policies directly → Create policy → JSON**, paste the policy below, name it `SidekickAgentAccess`, then attach it to the user.
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ClaudeOnBedrock",
         "Effect": "Allow",
         "Action": [
           "bedrock-mantle:CreateInference",
           "bedrock:InvokeModel",
           "bedrock:InvokeModelWithResponseStream",
           "bedrock:ListFoundationModels",
           "bedrock:ListInferenceProfiles"
         ],
         "Resource": "*"
       },
       {
         "Sid": "ReadOnlyInfra",
         "Effect": "Allow",
         "Action": [
           "eks:ListClusters", "eks:DescribeCluster",
           "cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms",
           "logs:DescribeLogGroups", "logs:FilterLogEvents",
           "ec2:DescribeInstances", "rds:DescribeDBInstances",
           "kinesis:ListStreams", "kinesis:DescribeStreamSummary"
         ],
         "Resource": "*"
       }
     ]
   }
   ```
   This is **read-only for infrastructure** — Sidekick can look but not change anything yet. Add write actions later, one at a time, when a workflow needs them.
3. Open the user → **Security credentials** → **Create access key** → *Application running outside AWS* → create.
4. Copy the **Access key ID** and **Secret access key** (shown once).

> 🔒 Rotate or delete this key after the hackathon. Never commit it.

### Add it to Sidekick
Dashboard → **Integrations → Amazon Web Services → Connect**:
- **Region:** `us-east-1`
- **Access key ID / Secret access key:** from step 4
- **EKS cluster name:** leave empty for now

**Save & test** → ✅ `Account 123456789012 · user/sidekick-agent`.

| If you see | Fix |
|---|---|
| `InvalidClientTokenId` / `SignatureDoesNotMatch` | Key copied wrong — paste again, no spaces |
| `AccessDenied … eks:DescribeCluster` | You entered a cluster name but the policy/cluster doesn't match — clear the field |

---

## Step 3 — Enable Claude models in Bedrock

1. Console search → **Amazon Bedrock** (region **us-east-1**).
2. Left menu → **Model access** (it may be under *Bedrock configurations*).
3. Request / enable the Anthropic models you want. Per Anthropic's docs, **Claude Sonnet 5, Claude Haiku 4.5, Claude Opus 4.8 and Claude Fable 5/5.1** are open to all Bedrock customers; others (e.g. **Claude Opus 5**) have their own access criteria shown in the console.
4. The first time, AWS may ask for a short **use-case form** for Anthropic models — fill it in; approval is usually quick.

---

## Step 4 — Point Sidekick's model at Bedrock and test it

Dashboard → **Configure → Model**:
1. **Provider:** Amazon Bedrock.
2. **Model:** if Opus 5 access isn't granted yet, pick **Claude Sonnet 5** or **Claude Haiku 4.5**.
3. **AWS region:** `us-east-1`.
4. **Max output tokens:** `4000` while you develop (keeps each call small).
5. Click **Test model** → ✅ `… answered "Sidekick model check OK" · 1,2xx ms · NN in / NN out tokens`.

A test call costs a fraction of a cent.

| If you see | Fix |
|---|---|
| `Permission denied (403) — is model access enabled…` | Step 3 isn't done for that model/region, or the IAM policy is missing `bedrock-mantle:CreateInference` |
| `Model not found (404)` | Model not available in that region — use `us-east-1`, or another model |
| `No AWS credentials found` | Step 2's key isn't saved — Integrations → AWS |

### Keeping model spend low
- Develop on **Haiku 4.5** or **Sonnet 5**; switch the default to Opus 5 for the demo.
- Use **effort `low`** for chatty skills (Infra Q&A) via *Per-skill overrides*.
- Keep **max output tokens** modest. Pricing: https://aws.amazon.com/bedrock/pricing/

---

## Step 5 (optional) — A Kubernetes cluster on EKS

Only do this if you want ShopLite running on real EKS for the demo. **EKS costs money every hour it exists, even idle**:

| Item | Approx. cost |
|---|---|
| EKS control plane | ~$0.10/hour |
| 2 × t3.medium nodes | ~$0.04/hour each |
| NAT gateway (eksctl default) | ~$0.045/hour + data |
| **Total** | **≈ $0.25/hour ≈ $6/day** |

Create it the day of the hackathon and **delete it the same day**.

```bash
# needs eksctl (https://eksctl.io) and your *own* admin credentials (not sidekick-agent)
eksctl create cluster --name shop-dev --region us-east-1 --managed --nodes 2 --node-type t3.medium
kubectl get nodes                 # 2 nodes Ready

# deploy ShopLite to it (from the shoplite folder) — see shoplite/README.md → Kubernetes
```

Then in Sidekick: **Integrations → AWS → EKS cluster name:** `shop-dev` → **Save & test** → ✅ `… · EKS shop-dev ACTIVE`.

**Delete when done:**
```bash
eksctl delete cluster --name shop-dev --region us-east-1
```

---

## End-of-hackathon checklist
- [ ] `eksctl delete cluster …` (if you made one) and check **EC2 → Load balancers / NAT gateways** are gone
- [ ] Bedrock: nothing to delete — you only pay per request
- [ ] IAM → Users → `sidekick-agent` → **deactivate/delete the access key**
- [ ] Dashboard → Integrations → AWS → **Disconnect**
- [ ] Keep the budget alerts — they're free
