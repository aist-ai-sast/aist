# Deployment and recovery

This runbook covers the production AIST Docker Compose deployment on Ubuntu
amd64. Use the repository quick start for local development.

## Provision the host

Configure the production inventory and variables:

```text
infra/ansible/inventory/prod.ini
infra/ansible/group_vars/prod.yml
```

Provision the host with the repository playbook:

```bash
ANSIBLE_CONFIG=infra/ansible/ansible.cfg \
  ansible-playbook \
  -i infra/ansible/inventory/prod.ini \
  infra/ansible/playbooks/provision_aist.yml
```

Routine application releases use:

```bash
./scripts/deploy-prod.sh
```

Before the first production start, replace every development credential default
in the Compose environment. At minimum, set unique values for:

```dotenv
DD_SECRET_KEY=<long-random-django-secret>
DD_CREDENTIAL_AES_256_KEY=<32-byte-random-key>
FIELD_ENCRYPTION_KEY=<deployment-specific-field-encryption-key>
AIST_SERVICE_TOKEN=<random-internal-service-token>
MCP_AUTH_TOKEN=<random-mcp-client-token>
```

Keep these values in the deployment secret store, not in the repository. Rotating
an encryption key requires a data migration or restore plan; replacing it without
that plan can make stored credentials unreadable.

## Preserve database state

Set an explicit host path for PostgreSQL data in the deployment environment:

```dotenv
COMPOSE_PROJECT_NAME=aist
AIST_POSTGRES_DATA_DIR=/absolute/path/to/postgres-data
```

`docker compose down` keeps this data. Do not use `docker compose down -v`
during a routine update because it removes named volumes.

## Configure TLS

Production commonly uses a host-managed certificate mounted into Nginx:

```dotenv
USE_TLS=true
GENERATE_TLS_CERTIFICATE=false
DD_HTTP_PORT=80
DD_TLS_PORT=443
ACME_WEBROOT_HOST=/absolute/path/to/acme-webroot
NGINX_SSL_HOST_DIR=/absolute/path/to/nginx-ssl
```

After issuing or renewing the certificate, place `fullchain.pem` at
`nginx.crt`, place `privkey.pem` at `nginx.key`, restrict the key permissions,
validate the Nginx configuration, and reload Nginx.

```bash
docker compose --env-file .env exec -T nginx nginx -t
docker compose --env-file .env exec -T nginx nginx -s reload
```

The production provisioning installs the certificate renewal timer and deploy
hook. Set `certbot_email` in the production variables to use email-backed ACME
registration.

## Restore a PostgreSQL backup

Stop application writes before restoring. Select the intended dump explicitly,
then stream it into the running PostgreSQL service:

```bash
gzip -dc /var/backups/aist-postgres/defectdojo_YYYYmmdd_HHMMSS.sql.gz \
  | docker compose --env-file .env exec -T postgres \
    sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

After restoration, start or restart the application services and verify that
migrations, login, project listing, and a read-only pipeline request succeed.

## Validate the deployment

```bash
docker compose --env-file .env ps
docker compose --env-file .env logs --tail=200
docker compose --env-file .env exec -T nginx nginx -t
```

The required long-lived services are Nginx, Django, PostgreSQL, Valkey, Celery
Beat, Celery workers, the context-extractor MCP service, and the local AI bridge.
The deployment wrapper fails if any of them is absent after startup.

For certificate validation:

```bash
docker compose --env-file .env exec -T nginx \
  openssl x509 -in /etc/nginx/ssl/nginx.crt \
  -noout -issuer -subject -dates -ext subjectAltName
```

## Roll back an application release

1. Record the failing application and database revisions.
2. Restore the previous known-good application revision.
3. Restore the previous certificate pair if TLS changed.
4. Re-run the supported deployment wrapper.
5. Validate Nginx, migrations, service health, login, and project access.

Do not manually delete launch requests, execution leases, or pipeline rows as a
recovery shortcut. Use the application reconciliation and supported database
restore procedures so durable execution history remains consistent.
