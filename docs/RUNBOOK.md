# Relay runbook

Production is a single EC2 instance in ap-south-1 (Mumbai) running `deploy/docker-compose.prod.yml`. Caddy terminates TLS for `RELAY_DOMAIN`. Data lives in two places:

- Postgres, in the `pgdata` volume on the instance.
- S3, in the bucket named by the `S3_BUCKET` parameter.

Secrets live in SSM Parameter Store under `/relay/prod/` and are loaded at deploy time. AWS access comes from the instance role, never from static keys.

## 0. One-time setup

1. **AWS resources.** Run this from an admin workstation, not the instance:
   ```sh
   deploy/aws/bootstrap.sh <bucket-name> <relay.yourdomain.in>
   ```
   It creates:
   - a private S3 bucket with SSE-KMS, public access blocked, TLS only, versioning, and backup expiry;
   - the SSM parameters, with random JWT, sandbox, Postgres and Fernet secrets.

   It prompts for the OpenRouter key.
2. **OpenRouter account.**
   - Turn on ZDR enforcement in the privacy settings (SPEC section 6).
   - Keep prompt logging off.
3. **IAM role.**
   - Create an EC2 role with `deploy/aws/instance-policy.json`, replacing `RELAY_BUCKET` and `ACCOUNT_ID`.
   - Attach it to the instance.
4. **Instance.**
   - Ubuntu 24.04, at least 4 vCPU / 8 GB. Use 16 GB with ClamAV.
   - Security group: 22 open to the office IP only; 80 and 443 open to the world.
   - Install Docker Engine with the compose plugin.
   - Optional: install gVisor (`runsc`) and register it as a Docker runtime, then set `SANDBOX_RUNTIME=runsc` in SSM.
5. **DNS.** Point an A record for the domain at the instance's Elastic IP.
6. **Code.**
   ```sh
   sudo git clone <repo> /opt/relay
   ```
7. **`config/routing.yaml`.** It must contain real slugs, each in the catalog with a ZDR endpoint (TASKS Phase 3). The API refuses to start in production otherwise.

## 1. Deploy (first time and every update)

```sh
cd /opt/relay
sudo git fetch && sudo git checkout <tag-or-commit>
sudo deploy/load-secrets.sh                      # SSM -> /opt/relay/.env.production (0600)
sudo docker compose -f deploy/docker-compose.prod.yml --env-file /opt/relay/.env.production up -d --build
sudo docker compose -f deploy/docker-compose.prod.yml ps
curl -fsS https://$DOMAIN/api/health             # {"status":"ok"}
```

- Database migrations run automatically when the `api` container starts (`alembic upgrade head`).
- If the API exits right away, check its logs. A `routing.yaml is not usable` message names the bad slug.

Then check the sandbox. This is the one path that could not be tested in the build environment, so do it on every new host:

```sh
sudo docker compose -f deploy/docker-compose.prod.yml exec sandbox \
  python -c "import docker; print(docker.from_env().containers.list())"
```

Then, in the app, ask: "Plot y = x^2 for x from 0 to 10". You should get a chart back.

## 2. Roll back

1. Find the previous good tag with `git tag --sort=-creatordate | head`.
2. Check whether the bad release added a migration. Compare `backend/alembic/versions/` between the two tags.
3. If it did, downgrade first, while the new code is still running:
   ```sh
   sudo docker compose -f deploy/docker-compose.prod.yml exec api alembic downgrade <previous-revision>
   ```
4. Run the deploy steps (section 1) with the previous tag.

If a downgrade is not possible, for example because data was dropped, restore the backup taken before the release (section 3).

## 3. Backups and restore

The `backup` service writes a `pg_dump` custom-format file to `s3://$S3_BUCKET/backups/relay-<UTC time>.dump`:

- daily at 21:30 UTC, which is 03:00 IST;
- SSE-KMS encrypted;
- expired after 35 days by a lifecycle rule.

Take one now (do this before every release that has a migration):

```sh
sudo docker compose -f deploy/docker-compose.prod.yml run --rm backup once
```

To restore, test on a scratch instance first (TASKS Phase 9 requires one test restore):

```sh
aws s3 ls s3://$S3_BUCKET/backups/ | tail
aws s3 cp s3://$S3_BUCKET/backups/relay-<stamp>.dump /tmp/relay.dump
sudo docker compose -f deploy/docker-compose.prod.yml stop api web
sudo docker compose -f deploy/docker-compose.prod.yml exec -T db \
  sh -c 'dropdb -U relay --if-exists relay_restore && createdb -U relay relay_restore'
sudo docker compose -f deploy/docker-compose.prod.yml exec -T db \
  pg_restore -U relay -d relay_restore --no-owner --no-privileges < /tmp/relay.dump
# Check it:
sudo docker compose -f deploy/docker-compose.prod.yml exec db \
  psql -U relay -d relay_restore -c 'select count(*) from users; select * from alembic_version;'
# Swap it in:
sudo docker compose -f deploy/docker-compose.prod.yml exec db psql -U relay -d postgres -c \
  'ALTER DATABASE relay RENAME TO relay_old; ALTER DATABASE relay_restore RENAME TO relay;'
sudo docker compose -f deploy/docker-compose.prod.yml start api web
```

- Delete `relay_old` once the restored system is confirmed.
- The S3 files are not part of the dump. Versioning protects them, so recover a deleted object by removing its delete marker within the retention window.

## 4. Rotate the OpenRouter key

1. Create a new key in OpenRouter, with the same account and ZDR settings.
2. Store it and redeploy:
   ```sh
   aws ssm put-parameter --region ap-south-1 --name /relay/prod/OPENROUTER_API_KEY \
     --type SecureString --overwrite --value '<new key>'
   cd /opt/relay && sudo deploy/load-secrets.sh
   sudo docker compose -f deploy/docker-compose.prod.yml --env-file /opt/relay/.env.production up -d api
   ```
3. Send a test message, then revoke the old key in OpenRouter.

Rotate `JWT_SECRET` the same way. Doing so signs everyone out.

**Do not rotate `FERNET_KEY` without re-encrypting the stored OAuth tokens.** If you do, users have to reconnect Google.

## 5. Add a user

Admins add users at **Admin → Users**: email, name, initial password (12+ characters), role and monthly cap. There is no self sign-up. Tell the person their password through a separate channel.

For the very first admin, use the CLI:

```sh
sudo docker compose -f deploy/docker-compose.prod.yml exec api \
  python -m app.cli create-admin --email you@paikane.com --name "Your Name"
```

- **Raise a cap:** Admin → Users. The change takes effect immediately.
- **Disable a user:** untick *active*. Their session stops working on the next request.

## 6. Day-to-day checks

- **Admin → Catalog.** Shows the last model-catalog sync and its warnings: missing or non-ZDR slugs, virus scanning off, sandbox off.
- **Logs.** Docker json-file logs rotate at 20 MB × 5 per container; Caddy access logs at 50 MB × 10.
  ```sh
  sudo docker compose -f deploy/docker-compose.prod.yml logs --since 1h api
  ```
  Message and file content is never logged at info level.
- **Disk.**
  ```sh
  docker system df
  ```
  Remove old images after upgrades with `docker image prune`.
