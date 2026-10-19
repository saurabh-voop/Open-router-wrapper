#!/bin/sh
# One-time AWS setup for Relay in ap-south-1. Review before running; it creates:
#   - a private S3 bucket with SSE-KMS by default, public access blocked, TLS-only policy,
#     versioning, and a lifecycle rule expiring backups/ after BACKUP_RETENTION_DAYS
#   - SSM SecureString parameters under /relay/prod/ (values prompted, never echoed)
# The EC2 instance role is created separately with aws/instance-policy.json.
set -eu
REGION=ap-south-1
BUCKET="${1:?usage: bootstrap.sh <bucket-name> <domain>}"
DOMAIN="${2:?usage: bootstrap.sh <bucket-name> <domain>}"
RETENTION="${BACKUP_RETENTION_DAYS:-35}"

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"},"BucketKeyEnabled":true}]}'
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-ownership-controls --bucket "$BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Sid":"TLSOnly","Effect":"Deny","Principal":"*",
 "Action":"s3:*","Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/*"],
 "Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
)"
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration "$(cat <<JSON
{"Rules":[
 {"ID":"expire-backups","Status":"Enabled","Filter":{"Prefix":"backups/"},
  "Expiration":{"Days":$RETENTION},"NoncurrentVersionExpiration":{"NoncurrentDays":7}},
 {"ID":"purge-deleted-files","Status":"Enabled","Filter":{"Prefix":"files/"},
  "NoncurrentVersionExpiration":{"NoncurrentDays":1},
  "Expiration":{"ExpiredObjectDeleteMarker":true}}]}
JSON
)"

put() {  # put NAME VALUE
  aws ssm put-parameter --region "$REGION" --name "/relay/prod/$1" --type SecureString \
    --value "$2" --overwrite >/dev/null
  echo "set /relay/prod/$1"
}
ask() {  # ask NAME -> prompts without echo
  printf '%s: ' "$1" >&2; stty -echo; read -r v; stty echo; printf '\n' >&2; put "$1" "$v"
}
rand() { python3 -c "import secrets;print(secrets.token_urlsafe($1))"; }

put S3_BUCKET "$BUCKET"
put RELAY_DOMAIN "$DOMAIN"
ask OPENROUTER_API_KEY
put JWT_SECRET "$(rand 48)"
put SANDBOX_TOKEN "$(rand 32)"
put POSTGRES_PASSWORD "$(rand 24)"
put FERNET_KEY "$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
echo "Optional later: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, CLAMAV_HOST, SANDBOX_RUNTIME"
