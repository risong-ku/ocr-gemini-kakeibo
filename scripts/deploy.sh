#!/usr/bin/env bash
# Build the Lambda archive and optionally update code and environment variables.
# Usage: bash scripts/deploy.sh [--build-only] [--env]
set -euo pipefail
set +xv

cd "$(dirname "$0")/.."

build_only=false
push_env=false
for argument in "$@"; do
  case "${argument}" in
    --build-only) build_only=true ;;
    --env) push_env=true ;;
    -h|--help)
      echo "Usage: bash scripts/deploy.sh [--build-only] [--env]"
      exit 0
      ;;
    *) echo "error: unknown argument: ${argument}" >&2; exit 1 ;;
  esac
done

command -v uv >/dev/null || { echo "error: uv is required." >&2; exit 1; }
umask 077
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/kakeibo-deploy.XXXXXX")"
trap 'rm -rf "${work_dir}"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# A build needs neither dotenv secrets nor AWS credentials.
if [ "${build_only}" = false ]; then
  overrides=()
  for name in AWS_REGION AWS_DEFAULT_REGION AWS_PROFILE KAKEIBO_TABLE_NAME KAKEIBO_BUCKET_NAME KAKEIBO_FUNCTION_NAME; do
    if [ -n "${!name:-}" ]; then overrides+=("${name}=${!name}"); fi
  done
  if [ -f ./.env ]; then
    # shellcheck disable=SC1091
    { set -a; . ./.env; set +a; } >/dev/null 2>&1
  fi
  if [ "${#overrides[@]}" -gt 0 ]; then export "${overrides[@]}"; fi
  # Resource names come from the shell or the defaults below, never from the dotenv
  # file: the local dotenv holds kakeibo-dev names meant for scripts/dev_local.sh.
  for name in KAKEIBO_TABLE_NAME KAKEIBO_BUCKET_NAME KAKEIBO_FUNCTION_NAME; do
    case " ${overrides[*]:-} " in *" ${name}="*) ;; *) unset "${name}" ;; esac
  done
  export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-northeast-1}}"
  export AWS_DEFAULT_REGION="${AWS_REGION}" AWS_PAGER="" AWS_CLI_AUTO_PROMPT=off
  export KAKEIBO_FUNCTION_NAME="${KAKEIBO_FUNCTION_NAME:-kakeibo}"
  if ! command -v aws >/dev/null || [[ "$(aws --version 2>&1)" != aws-cli/2.* ]]; then
    echo "error: AWS CLI v2 is required." >&2
    exit 1
  fi
  if [ "${push_env}" = true ]; then
    for name in SESSION_SECRET APP_PASSCODE GEMINI_API_KEY; do
      if [ -z "${!name:-}" ]; then
        echo "error: ${name} is not set. Fill in .env or export it." >&2
        exit 1
      fi
      export "${name}"
    done
    account_id="$(aws sts get-caller-identity --region "${AWS_REGION}" --query Account --output text)"
    export KAKEIBO_TABLE_NAME="${KAKEIBO_TABLE_NAME:-kakeibo}"
    export KAKEIBO_BUCKET_NAME="${KAKEIBO_BUCKET_NAME:-kakeibo-${account_id}-${AWS_REGION}}"
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
fi

# Extract only direct runtime requirements; Lambda supplies the AWS SDK.
uv run --no-project --python 3.12 python - >"${work_dir}/runtime.txt" <<'PY'
import re
import tomllib

with open("pyproject.toml", "rb") as file:
    dependencies = tomllib.load(file)["project"]["dependencies"]
for dependency in dependencies:
    name = re.match(r"[A-Za-z0-9_.-]+", dependency.strip()).group().lower()
    if name not in {"boto3", "botocore"}:
        print(dependency)
PY

# Keep runtime versions aligned with the reviewed lockfile, without dev groups.
uv export --locked --no-dev --no-default-groups --no-emit-project --no-hashes \
  --format requirements.txt >"${work_dir}/constraints.txt"
rm -rf build/
rm -f lambda.zip
uv pip install --target build/ \
  --python-platform aarch64-manylinux2014 --python-version 3.12 \
  --only-binary :all: \
  -r "${work_dir}/runtime.txt" -c "${work_dir}/constraints.txt"
cp -R src/kakeibo build/kakeibo

# Filter every path component, including tests and SDKs in transitive dependencies.
uv run --no-project --python 3.12 python - <<'PY'
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

root = Path("build")
excluded = {"__pycache__", "tests", "test", "boto3", "botocore"}
with ZipFile("lambda.zip", "w", compression=ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in excluded for part in relative.parts):
            continue
        if any(part.startswith(("boto3-", "botocore-")) for part in relative.parts):
            continue
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            info = ZipInfo.from_file(path, relative.as_posix())
            # Lambda's runtime user must be able to read the extracted files.
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=ZIP_DEFLATED)
size = Path("lambda.zip").stat().st_size
print(f"lambda.zip: {size:,} bytes ({size / 1024 / 1024:.2f} MiB)")
PY

if [ "${build_only}" = true ]; then exit 0; fi

# Lambda responses and validation errors can contain environment values.
if ! aws lambda update-function-code --region "${AWS_REGION}" \
  --function-name "${KAKEIBO_FUNCTION_NAME}" --architectures arm64 \
  --zip-file fileb://lambda.zip > /dev/null 2>"${work_dir}/aws-error"; then
  echo "error: Lambda code update failed. Check credentials, function name and archive limits." >&2
  exit 1
fi
aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${KAKEIBO_FUNCTION_NAME}"
if [ "${push_env}" = true ]; then
  if ! aws lambda update-function-configuration --region "${AWS_REGION}" \
    --function-name "${KAKEIBO_FUNCTION_NAME}" \
    --environment "file://${work_dir}/environment.json" > /dev/null 2>"${work_dir}/aws-error"; then
    echo "error: Lambda environment update failed. Check configuration permissions and environment limits." >&2
    exit 1
  fi
  aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${KAKEIBO_FUNCTION_NAME}"
fi
echo "Deployment complete: ${KAKEIBO_FUNCTION_NAME} (${AWS_REGION})"
