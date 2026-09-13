#!/usr/bin/env bash
# Run the app locally without AWS: moto_server stands in for DynamoDB and S3.
# Usage: bash scripts/dev_local.sh
set -euo pipefail

cd "$(dirname "$0")/.."

MOTO_PORT="${MOTO_PORT:-5001}"
MOTO_ENDPOINT="http://127.0.0.1:${MOTO_PORT}"
APP_PORT="${APP_PORT:-8000}"
MOTO_WAIT_ATTEMPTS=60
MOTO_WAIT_SECONDS=0.25
REQUIRED_SECRETS=(SESSION_SECRET APP_PASSCODE GEMINI_API_KEY)

# Local secrets live in the untracked dotenv file (docs/03_tech_spec.md §6).
if [ -f ./.env ]; then
  set -a; . ./.env; set +a
fi

export AWS_ENDPOINT_URL="${MOTO_ENDPOINT}"
export AWS_ACCESS_KEY_ID="testing"
export AWS_SECRET_ACCESS_KEY="testing"
export AWS_REGION="${AWS_REGION:-ap-northeast-1}"
export AWS_DEFAULT_REGION="${AWS_REGION}"
export KAKEIBO_TABLE_NAME="${KAKEIBO_TABLE_NAME:-kakeibo-dev}"
export KAKEIBO_BUCKET_NAME="${KAKEIBO_BUCKET_NAME:-kakeibo-dev}"
export COOKIE_SECURE="false"

for name in "${REQUIRED_SECRETS[@]}"; do
  if [ -z "${!name:-}" ]; then
    echo "error: ${name} is not set. Copy .env.example to .env and fill it in." >&2
    exit 1
  fi
done

# macOS AirPlay Receiver listens on 5000 and answers every request with 403.
if lsof -nP -iTCP:"${MOTO_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "error: port ${MOTO_PORT} is already in use." >&2
  echo "  On macOS this is usually AirPlay Receiver (System Settings > General > AirDrop & Handoff)." >&2
  echo "  Either turn it off or rerun with another port: MOTO_PORT=5002 bash scripts/dev_local.sh" >&2
  exit 1
fi

moto_log="$(mktemp -t kakeibo-moto)"
uv run moto_server -p "${MOTO_PORT}" >"${moto_log}" 2>&1 &
moto_pid=$!

cleanup() {
  kill "${moto_pid}" 2>/dev/null || true
  pkill -f "moto_server -p ${MOTO_PORT}" 2>/dev/null || true
}
trap cleanup EXIT

echo "moto_server starting on ${MOTO_ENDPOINT} (log: ${moto_log})"
ready="false"
for _ in $(seq 1 "${MOTO_WAIT_ATTEMPTS}"); do
  # Require a real moto response, not just an open socket.
  if curl -sf -o /dev/null "${MOTO_ENDPOINT}/moto-api/data.json"; then
    ready="true"
    break
  fi
  sleep "${MOTO_WAIT_SECONDS}"
done
if [ "${ready}" != "true" ]; then
  echo "error: moto_server did not start. See ${moto_log}" >&2
  exit 1
fi

uv run python - <<'PY'
"""Create the local DynamoDB table and S3 bucket; ignore existing resources."""

import os

import boto3
from botocore.exceptions import ClientError

ENDPOINT_URL = os.environ["AWS_ENDPOINT_URL"]
REGION = os.environ["AWS_REGION"]
TABLE_NAME = os.environ["KAKEIBO_TABLE_NAME"]
BUCKET_NAME = os.environ["KAKEIBO_BUCKET_NAME"]
US_EAST_1 = "us-east-1"
ALREADY_EXISTS = frozenset(
    {"ResourceInUseException", "BucketAlreadyOwnedByYou", "BucketAlreadyExists"}
)


def error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def create_table() -> None:
    dynamodb = boto3.client("dynamodb", endpoint_url=ENDPOINT_URL, region_name=REGION)
    try:
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as error:
        if error_code(error) not in ALREADY_EXISTS:
            raise
        print(f"table {TABLE_NAME}: already exists")
        return
    dynamodb.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    print(f"table {TABLE_NAME}: created")


def create_bucket() -> None:
    s3 = boto3.client("s3", endpoint_url=ENDPOINT_URL, region_name=REGION)
    arguments = {"Bucket": BUCKET_NAME}
    if REGION != US_EAST_1:
        arguments["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
    try:
        s3.create_bucket(**arguments)
    except ClientError as error:
        if error_code(error) not in ALREADY_EXISTS:
            raise
        print(f"bucket {BUCKET_NAME}: already exists")
        return
    print(f"bucket {BUCKET_NAME}: created")


create_table()
create_bucket()
PY

lan_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
echo
echo "iPhone (same Wi-Fi): http://${lan_ip}:${APP_PORT}/"
echo "Mac:                 http://127.0.0.1:${APP_PORT}/"
echo

uv run uvicorn kakeibo.main:app --host 0.0.0.0 --port "${APP_PORT}"
