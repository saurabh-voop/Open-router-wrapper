#!/bin/sh
# Writes /opt/relay/.env.production from SSM Parameter Store (SPEC section 10).
# Parameters live under /relay/prod/ as SecureString, e.g. /relay/prod/OPENROUTER_API_KEY.
# Needs the instance role (deploy/aws/instance-policy.json). Run as root before compose up.
set -eu
PREFIX="${RELAY_SSM_PREFIX:-/relay/prod/}"
OUT="${RELAY_ENV_FILE:-/opt/relay/.env.production}"
REGION="${AWS_REGION:-ap-south-1}"

umask 077
tmp="$(mktemp)"
aws ssm get-parameters-by-path --region "$REGION" --path "$PREFIX" --recursive \
  --with-decryption --query 'Parameters[*].[Name,Value]' --output text |
while IFS="$(printf '\t')" read -r name value; do
  printf '%s=%s\n' "${name#"$PREFIX"}" "$value"
done > "$tmp"

for required in OPENROUTER_API_KEY JWT_SECRET FERNET_KEY POSTGRES_PASSWORD S3_BUCKET RELAY_DOMAIN SANDBOX_TOKEN; do
  grep -q "^${required}=" "$tmp" || { echo "missing SSM parameter ${PREFIX}${required}" >&2; rm -f "$tmp"; exit 1; }
done
mv "$tmp" "$OUT"
chmod 600 "$OUT"
echo "wrote $OUT ($(wc -l < "$OUT") values)"
