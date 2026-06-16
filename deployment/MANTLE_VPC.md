# Mantle routing + in-VPC deployment

> This is a fork of [`aws-samples/bedrock-access-gateway`](https://github.com/aws-samples/bedrock-access-gateway)
> with two additions:
> 1. **Mantle routing** — models served by Amazon Bedrock's native
>    OpenAI-compatible endpoint (`bedrock-mantle`) are proxied through as-is;
>    everything else (e.g. Llama) keeps using `bedrock-runtime` / Converse.
> 2. **An in-VPC CloudFormation template** (`BedrockProxyVpc.template`) with a
>    **preflight check** that validates your VPC prerequisites before deploying
>    and fails fast with exact remediation if something is missing.

This guide is the complete, follow-along procedure for deploying into an
**existing private VPC**. Read [§0 Concepts](#0-concepts) first — it explains the
one thing that most commonly breaks this deployment.

---

## 0. Concepts

### Two model paths
```
                ┌─ model on Mantle's list (qwen, gpt-oss, gemma, grok, …)  →  bedrock-mantle  (native OpenAI, NO Converse translation)
client → BAG ───┤
                └─ everything else (Llama, …)                              →  bedrock-runtime / Converse
```
At startup the gateway calls Mantle's `GET /v1/models` and routes **by model
name** — no manual lists. For a Mantle model the request/response are forwarded
verbatim (it's already OpenAI-shaped). For everything else the gateway converts
OpenAI → Bedrock **Converse**.

### The #1 gotcha: the `bedrock` control-plane endpoint
At startup the gateway calls `bedrock:ListFoundationModels` to build its model
list. That is a **control-plane** API on the **`com.amazonaws.<region>.bedrock`**
endpoint — which is a **different VPC endpoint** from `bedrock-runtime`
(Converse) and `bedrock-mantle`. In a private VPC with no route to that
endpoint, the startup call hangs through retries, the app **never finishes
starting**, the Lambda Web Adapter health check never passes, and **every
request times out** — not just `/models`. The preflight step below catches this
before you deploy.

### Streaming + the API Gateway console "Test" tab
The deployment **streams** responses. The API Gateway console **Test** feature
does **not** support streaming — it buffers and returns `status: 0` / "no data"
after 35 s, with "Execution log is not available for streaming response." **This
is not a gateway failure.** Always test with a streaming client (`curl
--no-buffer`, the OpenAI SDK with `stream=True`, Postman) against the deployed
stage URL — never the console Test tab. See [§5 Testing](#5-testing).

---

## 1. Prerequisites (create these in your VPC first)

The template deliberately owns **only** the Lambda, API Gateway, IAM, and the
preflight resource. It does **not** create VPC endpoints, subnets, security
groups, or routing — because in an existing VPC those usually already exist, and
creating duplicates (especially a second private-DNS-enabled endpoint for a
service) fails. So you provide them; preflight verifies them.

### 1a. VPC DNS attributes
Both must be `true` (required for endpoint private DNS to resolve):
```bash
aws ec2 describe-vpc-attribute --vpc-id <vpc-id> --attribute enableDnsSupport   --region <region> --query 'EnableDnsSupport.Value'
aws ec2 describe-vpc-attribute --vpc-id <vpc-id> --attribute enableDnsHostnames --region <region> --query 'EnableDnsHostnames.Value'
# If either is false:
aws ec2 modify-vpc-attribute --vpc-id <vpc-id> --enable-dns-support   --region <region>
aws ec2 modify-vpc-attribute --vpc-id <vpc-id> --enable-dns-hostnames --region <region>
```

### 1b. Private subnets
Two or more, in **distinct Availability Zones** (interface endpoints allow at
most one ENI per AZ). Note their subnet IDs.

### 1c. Security groups (two, wired together)
- **Lambda SG** — attached to the gateway Lambda. Needs **egress 443**.
- **Endpoint SG** — attached to your interface endpoints. Needs **inbound 443
  FROM the Lambda SG** (by security-group reference, not CIDR).

```bash
# Lambda SG: allow egress 443 (the default all-egress rule also satisfies this)
aws ec2 authorize-security-group-egress --group-id <lambda-sg> \
  --protocol tcp --port 443 --cidr 0.0.0.0/0 --region <region>

# Endpoint SG: allow inbound 443 from the Lambda SG
aws ec2 authorize-security-group-ingress --group-id <endpoint-sg> \
  --protocol tcp --port 443 --source-group <lambda-sg> --region <region>
```

### 1d. VPC interface endpoints
Create each with **`PrivateDnsEnabled: true`**, in your private subnets, attached
to the **endpoint SG** from 1c.

| Endpoint service name | Required? | Why |
|---|---|---|
| `com.amazonaws.<region>.bedrock` | **YES — always** | `ListFoundationModels` at startup (see §0). Missing = app never starts. |
| `com.amazonaws.<region>.bedrock-runtime` | **YES** | Converse / Invoke (Llama and other non-Mantle models). |
| `com.amazonaws.<region>.bedrock-mantle` | YES if `EnableMantle=true` | Mantle models (qwen, gemma, grok, …). |
| `com.amazonaws.<region>.secretsmanager` | **YES** | Lambda reads the API key secret at startup. |
| `com.amazonaws.<region>.ecr.api` + `ecr.dkr` (interface) | YES if subnets have **no NAT** | Pull the Lambda container image. |
| `com.amazonaws.<region>.s3` (**gateway**, on the subnets' route table) | YES if subnets have **no NAT** | ECR image layers live in S3. |

Example (one interface endpoint — repeat per service):
```bash
aws ec2 create-vpc-endpoint --region <region> \
  --vpc-id <vpc-id> --vpc-endpoint-type Interface \
  --service-name com.amazonaws.<region>.bedrock \
  --subnet-ids <subnet-a> <subnet-b> \
  --security-group-ids <endpoint-sg> \
  --private-dns-enabled
```
S3 gateway endpoint (no SG; attaches to a route table):
```bash
aws ec2 create-vpc-endpoint --region <region> \
  --vpc-id <vpc-id> --vpc-endpoint-type Gateway \
  --service-name com.amazonaws.<region>.s3 \
  --route-table-ids <private-rtb-id>
```

> **Shared VPC?** If your VPC is owned by a different (networking) account, the
> endpoints may be owned there and won't show up in describe calls from this
> account. Preflight downgrades endpoint checks to **warnings** in that case —
> verify the endpoints exist in the owning account manually.

---

## 2. Build & push the container image to ECR

The Lambda is a **container image** (`src/Dockerfile`, **arm64**). Build it in
your account and push to ECR.

### Option A — AWS CodeBuild (no local Docker required)
This fork includes a CodeBuild bootstrap you can adapt, but the minimal manual
path is:
```bash
# 1. Create the ECR repo
aws ecr create-repository --repository-name bedrock-proxy-api --region <region>

# 2. Create a CodeBuild project using an ARM image (aws/codebuild/amazonlinux2-aarch64-standard:3.0),
#    PrivilegedMode=true, source = this repo, with a buildspec that runs:
#      docker build -f src/Dockerfile -t <ecr-uri>:latest src
#      docker push <ecr-uri>:latest
```
(If your org builds images through a security-scanned pipeline / Nexus and then
promotes to ECR, use that — the gateway only needs the final image in an ECR
repo your Lambda can pull from.)

### Option B — local Docker
```bash
ECR=<account>.dkr.ecr.<region>.amazonaws.com/bedrock-proxy-api
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker build --platform linux/arm64 -f src/Dockerfile -t $ECR:latest src
docker push $ECR:latest
```

Record the final image URI: `<account>.dkr.ecr.<region>.amazonaws.com/bedrock-proxy-api:latest`

---

## 3. Create the API key secret

Any string (no spaces). The secret value **must be JSON with key `api_key`**.
```bash
aws secretsmanager create-secret --region <region> \
  --name bedrock-proxy-api-key \
  --secret-string '{"api_key":"<your-key>"}'
```
Record the returned secret **ARN**.

---

## 4. Deploy `BedrockProxyVpc.template`

**Console:** CloudFormation → Create stack → upload `deployment/BedrockProxyVpc.template`.
**CLI:**
```bash
aws cloudformation deploy --region <region> \
  --stack-name bedrock-proxy-vpc \
  --template-file deployment/BedrockProxyVpc.template \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    ContainerImageUri="<ecr-uri>:latest" \
    ApiKeySecretArn="<secret-arn>" \
    DefaultModelId="meta.llama3-70b-instruct-v1:0" \
    VpcId="<vpc-id>" \
    PrivateSubnetIds="<subnet-a>,<subnet-b>" \
    LambdaSecurityGroupId="<lambda-sg>" \
    EnableMantle="true" \
    MantleRegion="<region>" \
    EnableCrossRegionInference="false" \
    RequireNatlessImagePullEndpoints="true"
```

### Parameters

| Parameter | Required | Notes |
|---|---|---|
| `ContainerImageUri` | ✅ | From §2. |
| `ApiKeySecretArn` | ✅ | From §3. |
| `VpcId` | ✅ | Console shows a dropdown of your VPCs. |
| `PrivateSubnetIds` | ✅ | Distinct AZs (§1b). |
| `LambdaSecurityGroupId` | ✅ | The Lambda SG (§1c). |
| `DefaultModelId` | — | Model used when a client omits `model` or sends `gpt-*`. **Must exist in this region** (preflight verifies). e.g. `meta.llama3-70b-instruct-v1:0` in ap-south-1. |
| `EnableMantle` | — | `true` (default) routes Mantle models to `bedrock-mantle`. |
| `MantleRegion` | — | Blank = stack region. |
| `EnableCrossRegionInference` | — | **Keep `false`** in a single-region private VPC: a regional `bedrock-runtime` endpoint only serves *this* region, so `us.*`/`eu.*`/`global.*` profile IDs would resolve to a region with no endpoint and time out. |
| `RequireNatlessImagePullEndpoints` | — | `true` (default) if subnets have no NAT → preflight also requires `ecr.api`, `ecr.dkr`, S3 gateway. Set `false` only if subnets have NAT egress. |
| `SkipPreflight` | — | `false` (recommended). `true` bypasses validation. |

### What preflight does
A small Lambda runs **before** the gateway is created and **fails the stack with
a clear message** (≈30–60 s, no silent hang) if any prerequisite is wrong. The
gateway Lambda only gets created after preflight passes.

| Preflight message | Fix |
|---|---|
| `Missing VPC interface endpoint com.amazonaws.<region>.bedrock ...` | Create that endpoint (§1d). Most common — the control-plane endpoint. |
| `Endpoint ... has PrivateDnsEnabled=false` | Recreate/modify it with private DNS on. |
| `Lambda SG ... has no egress rule allowing 443` | Add egress 443 to the Lambda SG (§1c). |
| `Endpoint ... SG(s) ... do not allow inbound 443 from Lambda SG ...` | Add the inbound 443 from-Lambda-SG rule (§1c). |
| `PrivateSubnetIds must be in distinct AZs` | Pick subnets in different AZs (§1b). |
| `VPC ... has enableDns... =false` | Enable both VPC DNS attributes (§1a). |
| `DefaultModelId '...' is not available in <region>` | Use a model ID from `GET /v1/models`. |

On success the stack creates the Lambda + API Gateway. Find the base URL in the
stack **Outputs** → `APIBaseUrl` (e.g. `https://xxxx.execute-api.<region>.amazonaws.com/api/v1`).

---

## 5. Testing

> ❌ Do **not** use the API Gateway console **Test** tab — it can't handle
> streaming and will mislead you (status 0 / no data / 35 s timeout). See §0.

List models:
```bash
curl --no-buffer "<APIBaseUrl>/models" -H "Authorization: Bearer <api-key>"
```
A Mantle model (routed to `bedrock-mantle`):
```bash
curl --no-buffer -i -X POST "<APIBaseUrl>/chat/completions" \
  -H "Authorization: Bearer <api-key>" -H "Content-Type: application/json" \
  -d '{"model":"qwen.qwen3-235b-a22b-2507","max_tokens":50,
       "messages":[{"role":"user","content":"Capital of India?"}]}'
```
A Converse model (routed to `bedrock-runtime`):
```bash
curl --no-buffer -i -X POST "<APIBaseUrl>/chat/completions" \
  -H "Authorization: Bearer <api-key>" -H "Content-Type: application/json" \
  -d '{"model":"meta.llama3-70b-instruct-v1:0","max_tokens":50,
       "messages":[{"role":"user","content":"Capital of France?"}]}'
```
Both should return `200`. (A Mantle response has a UUID `id` and `service_tier`;
a Converse response has `system_fingerprint`.)

---

## 6. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every request times out; Lambda logs show `app is not ready after ...ms` on `/health` | Missing `com.amazonaws.<region>.bedrock` control-plane endpoint, OR SG not wired both ways, OR (no-NAT) missing ECR/S3 endpoints so the image can't be pulled. |
| `/models` works, but one model 404s / "unsupported model" | That model isn't available in this region, or you used a bare ID where only a `us.`/`apac.` inference-profile ID exists. Check `GET /v1/models`. |
| API Gateway console Test shows status 0 / no data / 35 s | Expected — console Test can't stream. Use `curl --no-buffer`. |
| A Mantle model returns 403 | The Lambda role lacks `bedrock-mantle:*`, or the `bedrock-mantle` endpoint/SG isn't reachable. |
| Stack rolls back at `Preflight` | Read the failure reason — it names the exact missing prerequisite and fix (see §4 table). |
