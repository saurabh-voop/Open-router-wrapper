#!/bin/sh
# Daily encrypted Postgres backups to S3 (SPEC section 10).
#   relay-backup loop      run forever, once a day at BACKUP_HOUR_UTC
#   relay-backup once      take one backup now
# Objects are written with SSE-KMS; retention is an S3 lifecycle rule on backups/.
set -eu

: "${S3_BUCKET:?S3_BUCKET is required}"
: "${AWS_REGION:=ap-south-1}"
: "${BACKUP_HOUR_UTC:=21}"

backup_once() {
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  key="backups/relay-${stamp}.dump"
  echo "backup: starting ${key}"
  # Custom format: compressed, and restorable table by table with pg_restore.
  pg_dump --format=custom --compress=9 --no-owner --no-privileges \
    | aws s3 cp - "s3://${S3_BUCKET}/${key}" --region "${AWS_REGION}" --sse aws:kms --only-show-errors
  echo "backup: done ${key}"
}

case "${1:-loop}" in
  once) backup_once ;;
  loop)
    while true; do
      now=$(date -u +%s)
      next=$(date -u -d "$(date -u +%Y-%m-%d) ${BACKUP_HOUR_UTC}:30:00" +%s)
      [ "$next" -le "$now" ] && next=$((next + 86400))
      sleep $((next - now))
      backup_once || echo "backup: FAILED" >&2
    done
    ;;
  *) echo "usage: relay-backup [loop|once]" >&2; exit 2 ;;
esac
