# AIST

![Unit Tests](https://github.com/aist-ai-sast/aist/actions/workflows/unit-tests.yml/badge.svg?branch=master)
![Ruff Linter](https://github.com/aist-ai-sast/aist/actions/workflows/ruff.yml/badge.svg?branch=master)
![Shellcheck](https://github.com/aist-ai-sast/aist/actions/workflows/shellcheck.yml/badge.svg?branch=master)

**AIST** is a self-hosted SAST platform with AI-assisted triage. It runs analyzer pipelines, aggregates findings, performs deduplication, and supports an explicit AI review stage in the workflow.

**Key Capabilities**
- Orchestrates SAST pipelines in Docker
- Deduplication tracking and progress monitoring
- AI triage workflows with an explicit review stage
- GitHub/GitLab project onboarding
- Source archive (zip/tar) ingestion
- Results export for AI triage decisions
- Notifications to Slack and email
- Scheduled pipeline runs
- Project- and product-scoped access control
- Audit-friendly pipeline history and logs

## Documentation

Start with the [AIST documentation index](docs/README.md).

## Quickstart

1. Ensure Docker Desktop is running.
2. Start the stack:
```bash
docker compose up -d
```
3. Open the UI:
```bash
http://localhost:8080
```
If TLS is enabled in your environment, use:
```bash
https://localhost:443
```

## Tests

Unit tests:
```bash
./run-unittest.sh
```

REST framework tests:
```bash
./run-rest-framework-tests.zsh
```

Integration tests:
```bash
./run-integration-tests.sh
```

## CI

GitHub Actions runs unit tests and linting on `master`. See workflows in `.github/workflows`.

**Configuration**
- Environment variables are loaded via `docker-compose.yml` and `.env`.
- Local settings can be mounted via `docker/extra_settings`.
- To keep Postgres data stable across restarts, set in `.env`:
```bash
COMPOSE_PROJECT_NAME=aist
AIST_POSTGRES_DATA_DIR=/absolute/path/to/postgres-data
```
- Important: `docker compose down` keeps DB data, but `docker compose down -v` removes named volumes.
- Optional operator tooling (`pgadmin`) is available via profile:
```bash
docker compose --profile ops up -d pgadmin
```
- TLS variables:
```bash
# prod (.env)
USE_TLS=true
GENERATE_TLS_CERTIFICATE=false
DD_HTTP_PORT=80
DD_TLS_PORT=443
ACME_WEBROOT_HOST=/absolute/path/to/acme-webroot
NGINX_SSL_HOST_DIR=/absolute/path/to/nginx-ssl

# local dev (.env.dev)
USE_TLS=true
GENERATE_TLS_CERTIFICATE=true
```
- CI test stacks use:
  - `docker-compose.tests.yml` for REST/unit style test runs
  - `docker-compose.integration.yml` for integration test runs

**Let's Encrypt (HTTP-01)**
1. Ensure DNS for your domain points to the host and ports `80/443` are reachable.
2. Create challenge and cert directories on host:
```bash
mkdir -p "${ACME_WEBROOT_HOST}" "${NGINX_SSL_HOST_DIR}"
```
3. Issue certificate:
```bash
sudo certbot certonly --webroot -w /var/www/certbot -d your-domain.com
```
4. Deploy cert to nginx mount path:
```bash
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem "${NGINX_SSL_HOST_DIR}/nginx.crt"
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem "${NGINX_SSL_HOST_DIR}/nginx.key"
sudo chmod 640 "${NGINX_SSL_HOST_DIR}/nginx.key"
```
5. Reload nginx:
```bash
docker compose exec nginx nginx -s reload
```
6. For renewals, use deploy-hook with the same copy + reload steps.

**Production Provisioning (IaC, Ubuntu 24.04 amd64)**
1. Fill production inventory and variables:
   - `infra/ansible/inventory/prod.ini`
   - `infra/ansible/group_vars/prod.yml`
2. Run provisioning:
```bash
ANSIBLE_CONFIG=infra/ansible/ansible.cfg ansible-playbook -i infra/ansible/inventory/prod.ini infra/ansible/playbooks/provision_aist.yml
```
3. Deploy/update app stack with wrapper:
```bash
./scripts/deploy-prod.sh
```

**How certificate renew works**
- `certbot.timer` runs periodic renew checks.
- Renew/deploy hook is installed at `/usr/local/lib/aist/certbot-deploy-hook.sh`.
- Hook actions:
  - copies `fullchain.pem` -> `${NGINX_SSL_HOST_DIR}/nginx.crt`
  - copies `privkey.pem` -> `${NGINX_SSL_HOST_DIR}/nginx.key`
  - validates nginx config (`nginx -t`)
  - reloads nginx in compose (`nginx -s reload`)
- In `infra/ansible/group_vars/prod.yml` current policy is `certbot_register_unsafe_without_email: true` (set `certbot_email` to switch to recommended registration mode).

**Restore from backup**
1. Pick dump file from backup directory (default `/var/backups/aist-postgres`).
2. Restore into running postgres container:
```bash
gzip -dc /var/backups/aist-postgres/defectdojo_YYYYmmdd_HHMMSS.sql.gz \
  | docker compose --env-file .env exec -T postgres sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

**Health Validation Commands**
- TLS cert details from nginx container:
```bash
docker compose --env-file .env exec -T nginx openssl x509 -in /etc/nginx/ssl/nginx.crt -noout -issuer -subject -dates -ext subjectAltName
```
- Renewal dry-run:
```bash
certbot renew --dry-run --run-deploy-hooks --no-random-sleep-on-renew --deploy-hook /usr/local/lib/aist/certbot-deploy-hook.sh
```
- Backup timer/service:
```bash
systemctl status aist-db-backup.timer --no-pager
systemctl start aist-db-backup.service
journalctl -u aist-db-backup.service -n 100 --no-pager
```

**Rollback (short plan)**
1. Disable timer if needed: `systemctl disable --now aist-db-backup.timer`.
2. Revert nginx cert files in `${NGINX_SSL_HOST_DIR}` to previous known-good pair.
3. Reload nginx: `docker compose --env-file .env exec -T nginx nginx -t && docker compose --env-file .env exec -T nginx nginx -s reload`.
4. Re-run previous known-good git revision and `docker compose --env-file .env up -d`.

**Troubleshooting**
- Check service logs:
```bash
docker compose logs --tail=200
```
- Rebuild images:
```bash
docker compose build --no-cache
```
