#!/usr/bin/env bash
# =============================================================================
# One-command deploy of the Bedrock Access Gateway into an EXISTING VPC, with
# Mantle routing. Builds the Lambda image in-account (CodeBuild, no local
# Docker), creates the API key secret, then deploys BedrockProxyVpc.template.
#
# Prereqs you must already have in the VPC (see deployment/MANTLE_VPC.md §1):
#   - private subnets in >= 2 distinct AZs
#   - a Lambda security group (egress 443) and your interface endpoints' SG
#     allowing inbound 443 from it
#   - VPC interface endpoints: bedrock, bedrock-runtime, bedrock-mantle,
#     secretsmanager (+ ecr.api, ecr.dkr, s3-gateway if subnets have no NAT)
#   The deploy's PREFLIGHT will tell you exactly what's missing if anything is.
#
# Usage:
#   ./scripts/deploy-vpc.sh            # interactive (prompts for VPC inputs)
#   Or pre-set any of the env vars below to skip prompts:
#     REGION VPC_ID SUBNET_IDS LAMBDA_SG API_KEY DEFAULT_MODEL \
#     ENABLE_MANTLE MANTLE_REGION CROSS_REGION NATLESS STACK_NAME REPO_NAME
# =============================================================================
set -euo pipefail

# ---- config / discovery -----------------------------------------------------
REGION="${REGION:-$(aws configure get region || echo us-west-2)}"
STACK_NAME="${STACK_NAME:-bedrock-proxy-vpc}"
BUILD_STACK="${STACK_NAME}-imagebuild"
REPO_NAME="${REPO_NAME:-bedrock-proxy-api}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need() { command -v "$1" >/dev/null || { echo "ERROR: '$1' is required but not installed."; exit 1; }; }
need aws; need jq; need zip

ask() { # ask VAR "prompt" — only prompts if VAR is empty
  local var="$1" prompt="$2" cur="${!1:-}"
  if [[ -z "$cur" ]]; then read -r -p "$prompt: " cur; fi
  printf -v "$var" '%s' "$cur"
}

echo "==> Region: $REGION   Stack: $STACK_NAME"
ask VPC_ID       "VPC id (e.g. vpc-0abc123)"
ask SUBNET_IDS   "Private subnet ids, comma-separated, distinct AZs (e.g. subnet-a,subnet-b)"
ask LAMBDA_SG    "Lambda security group id (egress 443; endpoints allow 443 from it)"
DEFAULT_MODEL="${DEFAULT_MODEL:-}"
ask DEFAULT_MODEL "Default model id available in $REGION (e.g. meta.llama3-70b-instruct-v1:0)"
API_KEY="${API_KEY:-}"
ask API_KEY      "API key to create (any string, no spaces)"
ENABLE_MANTLE="${ENABLE_MANTLE:-true}"
MANTLE_REGION="${MANTLE_REGION:-$REGION}"
CROSS_REGION="${CROSS_REGION:-false}"
NATLESS="${NATLESS:-true}"

# ---- 1. image build stack (ECR + CodeBuild) ---------------------------------
echo "==> [1/4] Deploying image-build stack ($BUILD_STACK)..."
aws cloudformation deploy --region "$REGION" --stack-name "$BUILD_STACK" \
  --template-file "$HERE/deployment/BedrockProxyImageBuild.template" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides RepoName="$REPO_NAME" >/dev/null

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$BUILD_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
BUCKET="$(out SourceBucket)"; PROJECT="$(out CodeBuildProjectName)"; ECR_URI="$(out EcrUri)"

# ---- 2. zip src + run CodeBuild ---------------------------------------------
echo "==> [2/4] Building the Lambda image (CodeBuild)..."
TMPZIP="$(mktemp -d)/src.zip"
( cd "$HERE" && zip -qr "$TMPZIP" src -x '*.pyc' '*/__pycache__/*' '*.DS_Store' )
aws s3 cp --quiet "$TMPZIP" "s3://${BUCKET}/src.zip"
BUILD_ID="$(aws codebuild start-build --region "$REGION" --project-name "$PROJECT" --query 'build.id' --output text)"
printf "    waiting for build"
while :; do
  S="$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text)"
  [[ "$S" == "IN_PROGRESS" ]] || break
  printf "."; sleep 15
done
echo " $S"
[[ "$S" == "SUCCEEDED" ]] || { echo "ERROR: image build failed ($S). Check CodeBuild logs for $BUILD_ID."; exit 1; }

# ---- 3. API key secret ------------------------------------------------------
echo "==> [3/4] Creating API key secret..."
SECRET_NAME="${STACK_NAME}-apikey"
if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_NAME" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET_NAME" \
    --secret-string "{\"api_key\":\"${API_KEY}\"}" >/dev/null
else
  aws secretsmanager create-secret --region "$REGION" --name "$SECRET_NAME" \
    --secret-string "{\"api_key\":\"${API_KEY}\"}" >/dev/null
fi
SECRET_ARN="$(aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_NAME" --query ARN --output text)"

# ---- 4. deploy the gateway (preflight runs first) ---------------------------
echo "==> [4/4] Deploying the gateway (preflight validates your VPC first)..."
set +e
aws cloudformation deploy --region "$REGION" --stack-name "$STACK_NAME" \
  --template-file "$HERE/deployment/BedrockProxyVpc.template" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    ContainerImageUri="${ECR_URI}:latest" \
    ApiKeySecretArn="$SECRET_ARN" \
    DefaultModelId="$DEFAULT_MODEL" \
    VpcId="$VPC_ID" \
    PrivateSubnetIds="$SUBNET_IDS" \
    LambdaSecurityGroupId="$LAMBDA_SG" \
    EnableMantle="$ENABLE_MANTLE" \
    MantleRegion="$MANTLE_REGION" \
    EnableCrossRegionInference="$CROSS_REGION" \
    RequireNatlessImagePullEndpoints="$NATLESS"
RC=$?
set -e
if [[ $RC -ne 0 ]]; then
  echo
  echo "Deploy did not complete. If it rolled back at 'Preflight', the reason names"
  echo "the exact missing prerequisite — see it with:"
  echo "  aws cloudformation describe-stack-events --region $REGION --stack-name $STACK_NAME \\"
  echo "    --query \"StackEvents[?ResourceStatus=='CREATE_FAILED'].ResourceStatusReason\" --output text | head"
  exit $RC
fi

URL="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='APIBaseUrl'].OutputValue" --output text)"
echo
echo "✅ Done. API base URL: $URL"
echo
echo "Test (use a streaming client, NOT the API Gateway console Test tab):"
echo "  curl --no-buffer \"$URL/models\" -H \"Authorization: Bearer $API_KEY\""
echo "  curl --no-buffer -X POST \"$URL/chat/completions\" \\"
echo "    -H \"Authorization: Bearer $API_KEY\" -H \"Content-Type: application/json\" \\"
echo "    -d '{\"model\":\"qwen.qwen3-235b-a22b-2507\",\"max_tokens\":50,\"messages\":[{\"role\":\"user\",\"content\":\"Capital of India?\"}]}'"
