#!/usr/bin/env bash
# Create the personal AWS deployment; existing resources are retained.
# Usage: bash scripts/bootstrap_aws.sh
set -euo pipefail
set +xv

cd "$(dirname "$0")/.."
if [ "$#" -ne 0 ]; then
  echo "Usage: bash scripts/bootstrap_aws.sh" >&2
  exit 1
fi

# Explicit deployment settings take precedence over the local dotenv defaults.
overrides=()
for name in AWS_REGION AWS_DEFAULT_REGION AWS_PROFILE KAKEIBO_TABLE_NAME KAKEIBO_BUCKET_NAME KAKEIBO_FUNCTION_NAME KAKEIBO_ROLE_NAME; do
  if [ -n "${!name:-}" ]; then overrides+=("${name}=${!name}"); fi
done
if [ -f ./.env ]; then
  # shellcheck disable=SC1091
  { set -a; . ./.env; set +a; } >/dev/null 2>&1
fi
if [ "${#overrides[@]}" -gt 0 ]; then export "${overrides[@]}"; fi
# Resource names come from the shell or the defaults below, never from the dotenv
# file: the local dotenv holds kakeibo-dev names meant for scripts/dev_local.sh.
for name in KAKEIBO_TABLE_NAME KAKEIBO_BUCKET_NAME KAKEIBO_FUNCTION_NAME KAKEIBO_ROLE_NAME; do
  case " ${overrides[*]:-} " in *" ${name}="*) ;; *) unset "${name}" ;; esac
done
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-northeast-1}}"
export AWS_DEFAULT_REGION="${AWS_REGION}" AWS_PAGER="" AWS_CLI_AUTO_PROMPT=off
export KAKEIBO_TABLE_NAME="${KAKEIBO_TABLE_NAME:-kakeibo}"
export KAKEIBO_FUNCTION_NAME="${KAKEIBO_FUNCTION_NAME:-kakeibo}"
export KAKEIBO_ROLE_NAME="${KAKEIBO_ROLE_NAME:-${KAKEIBO_FUNCTION_NAME}-lambda}"
POLICY_NAME=kakeibo-runtime
IAM_WAIT_ATTEMPTS=12
IAM_WAIT_SECONDS=5

if ! command -v aws >/dev/null || [[ "$(aws --version 2>&1)" != aws-cli/2.* ]]; then
  echo "error: AWS CLI v2 is required." >&2
  exit 1
fi
command -v uv >/dev/null || { echo "error: uv is required." >&2; exit 1; }
umask 077
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/kakeibo-bootstrap.XXXXXX")"
trap 'rm -rf "${work_dir}"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Never print raw AWS errors: Lambda errors can repeat submitted secret values.
aws_call() {
  local status=0
  aws --region "${AWS_REGION}" "$@" 2>"${work_dir}/aws-error" || status=$?
  if [ "${status}" -ne 0 ]; then
    echo "error: AWS ${1} ${2} failed. Check credentials, permissions and resource settings." >&2
  fi
  return "${status}"
}

# Only a documented not-found error means a resource should be created.
exists() {
  local missing="$1"
  shift
  if aws --region "${AWS_REGION}" "$@" >"${work_dir}/lookup" 2>"${work_dir}/aws-error"; then
    return 0
  fi
  if grep -Eq "\(${missing}\)" "${work_dir}/aws-error"; then return 1; fi
  echo "error: AWS ${1} ${2} lookup failed. Check credentials and permissions." >&2
  exit 1
}

account_id="$(aws_call sts get-caller-identity --query Account --output text)"
caller_arn="$(aws_call sts get-caller-identity --query Arn --output text)"
partition="${caller_arn#arn:}"
partition="${partition%%:*}"
export KAKEIBO_BUCKET_NAME="${KAKEIBO_BUCKET_NAME:-kakeibo-${account_id}-${AWS_REGION}}"
export TABLE_ARN="arn:${partition}:dynamodb:${AWS_REGION}:${account_id}:table/${KAKEIBO_TABLE_NAME}"
export BUCKET_ARN="arn:${partition}:s3:::${KAKEIBO_BUCKET_NAME}"
export LOG_ARN="arn:${partition}:logs:${AWS_REGION}:${account_id}:log-group:/aws/lambda/${KAKEIBO_FUNCTION_NAME}"

function_exists=false
if exists ResourceNotFoundException lambda get-function \
  --function-name "${KAKEIBO_FUNCTION_NAME}" --query Configuration.FunctionArn; then
  function_exists=true
else
  for name in SESSION_SECRET APP_PASSCODE GEMINI_API_KEY; do
    if [ -z "${!name:-}" ]; then
      echo "error: ${name} is not set. Fill in .env or export it." >&2
      exit 1
    fi
    export "${name}"
  done
  # Build before creating resources, so packaging failures leave no partial setup.
  bash scripts/deploy.sh --build-only
  uv run --no-project --python 3.12 python - "${work_dir}/environment.json" <<'PY'
import json
import os
import sys

names = (
    "SESSION_SECRET", "APP_PASSCODE", "GEMINI_API_KEY",
    "KAKEIBO_TABLE_NAME", "KAKEIBO_BUCKET_NAME",
)
variables = {name: os.environ[name] for name in names}
variables["COOKIE_SECURE"] = "true"
with open(sys.argv[1], "w") as file:
    json.dump({"Variables": variables}, file)
PY
fi

if exists ResourceNotFoundException dynamodb describe-table --table-name "${KAKEIBO_TABLE_NAME}"; then
  echo "DynamoDB table already exists; skipping creation."
else
  aws_call dynamodb create-table --table-name "${KAKEIBO_TABLE_NAME}" \
    --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
    --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST >/dev/null
fi
aws_call dynamodb wait table-exists --table-name "${KAKEIBO_TABLE_NAME}"

if exists '404|NoSuchBucket|NotFound' s3api head-bucket \
  --bucket "${KAKEIBO_BUCKET_NAME}" --expected-bucket-owner "${account_id}"; then
  echo "S3 bucket already exists; skipping creation."
else
  if [ "${AWS_REGION}" != us-east-1 ]; then
    aws_call s3api create-bucket --bucket "${KAKEIBO_BUCKET_NAME}" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  else
    aws_call s3api create-bucket --bucket "${KAKEIBO_BUCKET_NAME}" >/dev/null
  fi
fi

# Repair missing or incomplete privacy settings after an interrupted setup.
public_block=None
if exists NoSuchPublicAccessBlockConfiguration s3api get-public-access-block \
  --bucket "${KAKEIBO_BUCKET_NAME}" --expected-bucket-owner "${account_id}" \
  --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' --output text; then
  public_block="$(cat "${work_dir}/lookup")"
fi
if [ "${public_block}" != $'True\tTrue\tTrue\tTrue' ]; then
  aws_call s3api put-public-access-block --bucket "${KAKEIBO_BUCKET_NAME}" \
    --expected-bucket-owner "${account_id}" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi
if ! exists ServerSideEncryptionConfigurationNotFoundError s3api get-bucket-encryption \
  --bucket "${KAKEIBO_BUCKET_NAME}" --expected-bucket-owner "${account_id}"; then
  aws_call s3api put-bucket-encryption --bucket "${KAKEIBO_BUCKET_NAME}" \
    --expected-bucket-owner "${account_id}" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
fi

uv run --no-project --python 3.12 python - "${work_dir}" <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
trust = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole",
}]}
policy = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": [
        "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem",
        "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:GetItem",
    ], "Resource": os.environ["TABLE_ARN"]},
    {"Effect": "Allow", "Action": [
        "s3:PutObject", "s3:DeleteObject", "s3:GetObject",
    ], "Resource": os.environ["BUCKET_ARN"] + "/receipts/*"},
    {"Effect": "Allow", "Action": "logs:CreateLogGroup",
     "Resource": os.environ["LOG_ARN"] + ":*"},
    {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
     "Resource": os.environ["LOG_ARN"] + ":log-stream:*"},
]}
(root / "trust.json").write_text(json.dumps(trust))
(root / "policy.json").write_text(json.dumps(policy))
PY

if exists NoSuchEntity iam get-role --role-name "${KAKEIBO_ROLE_NAME}"; then
  echo "IAM role already exists; skipping creation."
else
  aws_call iam create-role --role-name "${KAKEIBO_ROLE_NAME}" \
    --assume-role-policy-document "file://${work_dir}/trust.json" >/dev/null
fi
if ! exists NoSuchEntity iam get-role-policy --role-name "${KAKEIBO_ROLE_NAME}" --policy-name "${POLICY_NAME}"; then
  aws_call iam put-role-policy --role-name "${KAKEIBO_ROLE_NAME}" --policy-name "${POLICY_NAME}" \
    --policy-document "file://${work_dir}/policy.json"
fi
role_arn="$(aws_call iam get-role --role-name "${KAKEIBO_ROLE_NAME}" --query Role.Arn --output text)"

if [ "${function_exists}" = true ]; then
  echo "Lambda function already exists; use deploy.sh to update code or environment."
else
  # IAM is eventually consistent; retry only the role-assumption propagation error.
  for ((attempt = 1; attempt <= IAM_WAIT_ATTEMPTS; attempt++)); do
    sleep "${IAM_WAIT_SECONDS}"
    if aws --region "${AWS_REGION}" lambda create-function \
      --function-name "${KAKEIBO_FUNCTION_NAME}" --runtime python3.12 \
      --architectures arm64 --handler kakeibo.main.handler \
      --timeout 60 --memory-size 512 --role "${role_arn}" \
      --zip-file fileb://lambda.zip --environment "file://${work_dir}/environment.json" \
      >/dev/null 2>"${work_dir}/aws-error"; then
      function_exists=true
      break
    fi
    if ! grep -q 'The role defined for the function cannot be assumed by Lambda' "${work_dir}/aws-error"; then
      echo "error: Lambda creation failed. Check permissions, runtime settings and archive limits." >&2
      exit 1
    fi
    echo "Waiting for IAM role propagation (${attempt}/${IAM_WAIT_ATTEMPTS})."
  done
  if [ "${function_exists}" != true ]; then
    echo "error: IAM role did not propagate in time. Check its trust policy and rerun bootstrap." >&2
    exit 1
  fi
fi
aws_call lambda wait function-active-v2 --function-name "${KAKEIBO_FUNCTION_NAME}"

if exists ResourceNotFoundException lambda get-function-url-config \
  --function-name "${KAKEIBO_FUNCTION_NAME}" --query AuthType --output text; then
  if [ "$(cat "${work_dir}/lookup")" != NONE ]; then
    echo "error: existing Function URL uses a different auth type; expected NONE." >&2
    exit 1
  fi
else
  aws_call lambda create-function-url-config --function-name "${KAKEIBO_FUNCTION_NAME}" --auth-type NONE >/dev/null
fi

# New Function URLs require both actions; direct invocation stays restricted.
for statement in kakeibo-function-url kakeibo-invoke-via-url; do
  if exists ResourceNotFoundException lambda get-policy \
    --function-name "${KAKEIBO_FUNCTION_NAME}" --query Policy --output text; then
    if uv run --no-project --python 3.12 python - "${work_dir}/lookup" "${statement}" <<'PY'
import json
import sys

with open(sys.argv[1]) as file:
    policy = json.load(file)
sys.exit(0 if any(item.get("Sid") == sys.argv[2] for item in policy["Statement"]) else 1)
PY
    then
      continue
    fi
  fi
  if [ "${statement}" = kakeibo-function-url ]; then
    aws_call lambda add-permission --function-name "${KAKEIBO_FUNCTION_NAME}" \
      --statement-id "${statement}" --action lambda:InvokeFunctionUrl \
      --principal '*' --function-url-auth-type NONE >/dev/null
  else
    aws_call lambda add-permission --function-name "${KAKEIBO_FUNCTION_NAME}" \
      --statement-id "${statement}" --action lambda:InvokeFunction \
      --principal '*' --invoked-via-function-url >/dev/null
  fi
done

echo "Function URL:"
aws_call lambda get-function-url-config --function-name "${KAKEIBO_FUNCTION_NAME}" --query FunctionUrl --output text
