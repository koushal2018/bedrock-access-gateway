# Mantle routing + in-VPC deployment (fork additions)

> This is a fork of [`aws-samples/bedrock-access-gateway`](https://github.com/aws-samples/bedrock-access-gateway)
> with two additions:
> 1. **Mantle routing** — models served by Amazon Bedrock's native
>    OpenAI-compatible endpoint (`bedrock-mantle`) are proxied through as-is;
>    everything else (e.g. Llama) keeps using `bedrock-runtime` / Converse.
> 2. **An in-VPC CloudFormation template** (`BedrockProxyVpc.template`) with a
>    **preflight check** that validates your VPC prerequisites before deploying
>    and fails fast with exact remediation if something is missing.

## How routing works

```
                ┌─ model on Mantle's list (qwen, gpt-oss, gemma, …)  →  bedrock-mantle (as-is, no Converse translation)
client → BAG ───┤
                └─ everything else (Llama, …)                        →  bedrock-runtime / Converse
```

At startup the gateway fetches Mantle's `/v1/models` and routes by model name —
no manual config. Controlled by env vars (set as CFN parameters):

| Env var | Default | Meaning |
|---|---|---|
| `ENABLE_MANTLE` | `true` | Route Mantle-served models to `bedrock-mantle`. |
| `MANTLE_REGION` | stack region | Region for the `bedrock-mantle` endpoint. |

If `ENABLE_MANTLE=false`, the gateway behaves exactly like upstream (all Converse).

## Deploy steps

### 1. Build & push the image to your ECR

The Lambda runs a **container image** (`src/Dockerfile`, arm64). Build it in your
account and push to an ECR repo. If you have no local Docker, the simplest path
is AWS CodeBuild; otherwise:

```bash
aws ecr create-repository --repository-name bedrock-proxy-api --region <region>
# build arm64 image from src/Dockerfile and push to:
#   <account>.dkr.ecr.<region>.amazonaws.com/bedrock-proxy-api:latest
```

### 2. Create the API key secret

```bash
aws secretsmanager create-secret --name bedrock-proxy-api-key \
  --secret-string '{"api_key":"<your-key>"}' --region <region>
```

### 3. Deploy `BedrockProxyVpc.template`

Deploy into your **existing** VPC. The template owns only the Lambda, API
Gateway, IAM, and the preflight resource — it does **not** create VPC endpoints,
subnets, or routing (those are prerequisites it validates; see below).

Key parameters (the console shows dropdowns of your real resources):

| Parameter | Notes |
|---|---|
| `ContainerImageUri` | From step 1. |
| `ApiKeySecretArn` | From step 2. |
| `VpcId` / `PrivateSubnetIds` / `LambdaSecurityGroupId` | Your VPC placement. Subnets must be in **distinct AZs**. |
| `DefaultModelId` | Must exist in this region (preflight verifies). E.g. `meta.llama3-70b-instruct-v1:0` in ap-south-1. |
| `EnableMantle` / `MantleRegion` | Mantle routing. |
| `EnableCrossRegionInference` | Keep `false` for a single-region private VPC (a regional `bedrock-runtime` endpoint only serves THIS region). |
| `RequireNatlessImagePullEndpoints` | `true` if your private subnets have no NAT (preflight then also requires ecr.api, ecr.dkr, S3 gateway endpoints). |
| `SkipPreflight` | `false` (recommended). |

## VPC prerequisites (validated by preflight)

Your VPC must already have these, or preflight blocks the deploy with the exact
fix. **All interface endpoints need `PrivateDnsEnabled: true` and a security
group that allows inbound 443 from the Lambda's security group.**

| Endpoint | Why |
|---|---|
| `com.amazonaws.<region>.bedrock` | **REQUIRED.** `ListFoundationModels` runs at startup; if this is missing the app never starts and every request times out. (This is the #1 gotcha — it is a *separate* endpoint from `bedrock-runtime`.) |
| `com.amazonaws.<region>.bedrock-runtime` | Converse / Invoke. |
| `com.amazonaws.<region>.bedrock-mantle` | Mantle models (if `ENABLE_MANTLE=true`). |
| `com.amazonaws.<region>.secretsmanager` | Read the API key secret. |
| `ecr.api`, `ecr.dkr` (interface) + `s3` (gateway) | Only if subnets have **no NAT** — needed to pull the container image. |

Also: the VPC needs `enableDnsSupport` **and** `enableDnsHostnames` = true, and
the Lambda security group needs **egress 443**.

### IAM

The gateway's execution role needs (the template grants these): `bedrock:*`
(ListFoundationModels, ListInferenceProfiles, InvokeModel*), the **Mantle**
actions `bedrock-mantle:CreateInference|ListModels|GetModel`, and
`secretsmanager:GetSecretValue` on your secret.

## Testing

The deployment **streams** responses. The API Gateway console **Test** tab does
not support streaming (it buffers, then returns status 0 / "no data" after 35s),
so use a real client:

```bash
curl --no-buffer -i -X POST \
  "https://<api-id>.execute-api.<region>.amazonaws.com/api/v1/chat/completions" \
  -H "Authorization: Bearer <api-key>" -H "Content-Type: application/json" \
  -d '{"model":"qwen.qwen3-235b-a22b-2507","max_tokens":50,
       "messages":[{"role":"user","content":"Capital of India?"}]}'
```
