# Deployment and recovery

This runbook covers the production AIST Docker Compose deployment on Ubuntu
amd64. Use the repository quick start for local development.

## Provision the host

Use the [Ansible deployment guide](../../infra/ansible/README.md) to select the
host and checkout, supply a private env file, and prepare persistent storage and
TLS. The default `prepare` phase starts no application containers. Restore state
before using the `start` phase on a relocation target.

Routine application releases use `./scripts/deploy-prod.sh` from that checkout.
The wrapper validates a single HTTPS origin, builds images, stops application
writers, applies migrations, starts services and checks runtime readiness and
TLS. It reads deployment values from the selected env file through Compose;
application values exported in the invoking shell cannot override that file.

Before the first production start, replace every development credential default
in the Compose environment. At minimum, set unique values for:

```dotenv
DD_SECRET_KEY=<long-random-django-secret>
DD_CREDENTIAL_AES_256_KEY=<32-byte-random-key>
FIELD_ENCRYPTION_KEY=<deployment-specific-field-encryption-key>
AIST_SERVICE_TOKEN=<random-internal-service-token>
```

Keep these values in the deployment secret store, not in the repository. Rotating
an encryption key requires a data migration or restore plan; replacing it without
that plan can make stored credentials unreadable.

## Keep the embedded platform defaults

Two settings of the embedded DefectDojo foundation are set deliberately by the
AIST Compose files, against the foundation's own defaults. A deployment that
overrides them loses platform behavior AIST depends on:

```dotenv
DD_V3_FEATURE_LOCATIONS=False
DD_DJANGO_ADMIN_ENABLED=True
```

`DD_V3_FEATURE_LOCATIONS` switches report import to the foundation's newer
representation of a web location. AIST's DAST deduplication correlates findings
through the endpoint records an import writes today, so enabling it leaves DAST
findings uncorrelated. Moving deduplication onto the newer representation is an
open design decision, not a configuration change an operator can make.

Enabling it also turns on per-save validation across the foundation's core
records — findings, products, engagements and product types are validated in
full on every write. Imports that the platform accepts today can start failing
on records whose stored values predate that validation, so the flag is not a
setting to flip on a running deployment to try it out.

`DD_DJANGO_ADMIN_ENABLED` keeps the Django administration site mounted under
`/aist-admin/`. Superusers use it as the fallback surface for organization
integrations, stored VPN credential indicators, and the membership audit log
when the REST API or the client UI cannot be reached. That area stays
superuser-only. The foundation ships this setting off, so an environment that
drops the variable silently loses the fallback.

Apply both to every service that runs application code — the web service, Celery
Beat, the Celery workers, and the startup initializer — so a worker never
imports under different rules than the request that queued it.

## Preserve database state

Set an explicit host path for PostgreSQL data in the deployment environment:

```dotenv
COMPOSE_PROJECT_NAME=aist
AIST_POSTGRES_DATA_DIR=/absolute/path/to/postgres-data
```

`docker compose down` keeps this data. Do not use `docker compose down -v`
during a routine update because it removes named volumes.

The PostgreSQL 18 image stores its cluster at `/var/lib/postgresql/18/docker`.
Compose mounts the configured host directory at `/var/lib/postgresql`, so the
cluster lives under `18/docker` inside `AIST_POSTGRES_DATA_DIR`. The test
overlays replace that same mount with their own named volumes.

### Recover a deployment using the previous mount

An existing deployment may still mount its configured directory at
`/var/lib/postgresql/data`, with an additional anonymous volume declared at
`/var/lib/postgresql`. Inspect the effective filesystem before deciding where
the cluster lives: a `data -> .` symlink can make both mounts resolve to the
parent directory, with the configured host directory holding the active
cluster. `docker inspect` mount destinations and `SHOW data_directory` alone
do not establish which backing directory supplies the files.

1. Inspect the running database before stopping or recreating it:

   ```bash
   docker compose --env-file .env exec -T postgres \
     sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SHOW data_directory"'
   docker inspect "$(docker compose --env-file .env ps -q postgres)" \
     --format '{{json .Mounts}}'
   docker compose --env-file .env exec -T postgres \
     sh -c 'ls -ld /var/lib/postgresql/data; readlink -f "$PGDATA"; awk '\''/\/var\/lib\/postgresql/ {print}'\'' /proc/self/mountinfo'
   ```

   Resolve named-volume driver options with `docker volume inspect`. Confirm
   the backing directory using the effective mount entries and matching file
   inodes, for example the cluster's `PG_VERSION`, on the host and in the
   container. Do not move or overwrite data based only on a textual path match.

2. Take a logical backup from the running database and verify restoration into
   an isolated PostgreSQL 18 instance. Preserve database roles, extensions, the
   application image, configuration, encryption keys, and media alongside it.
3. Schedule a write freeze: stop new requests and launches, drain active and
   reserved tasks, then stop Beat, workers, and other database writers. Take a
   final logical backup and stop PostgreSQL cleanly.
4. Preserve the host directory and anonymous volume. If the active cluster is
   already under `18/docker` in the configured host directory, no data transfer
   is needed: change only the mount destination. Otherwise, for a same-version
   repair, copy the complete stopped cluster volume to an empty staging
   directory, preserving ownership and permissions. Check the copy before
   moving it into the configured host directory, retaining its previous
   contents separately. Alternatively, restore the final logical backup into
   a fresh PostgreSQL 18 cluster there.
5. Recreate only PostgreSQL with the corrected Compose mount. Confirm
   `SHOW data_directory`, the effective mount source, cluster system identifier,
   database counts, and migration
   history before starting application writers. Recreate once more while
   writers are stopped and confirm that the data persists.
6. Resume the application using its recorded image revision. Verify login,
   finding access, source files, and background work. Retain the previous
   volume and backups until recovery has been accepted.

Changing a named volume's bind source in `.env` does not update an already
created volume's driver options. Inspect the actual source before restarting;
use an explicitly prepared new volume if the host path changes.

Backups must read the running database through `pg_dump`, or use a supported
physical backup procedure. An ordinary archive of a running PostgreSQL data
directory is not a consistent physical backup. A successful archive upload does
not establish recoverability; test restoration and monitor backup freshness.

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

For an internal hostname without certificate issuance, use the playbook's
persistent self-signed mode. It generates a hostname SAN and preserves the key
and certificate on repeated runs. Distribute the public certificate to clients
and runtime trust stores that call AIST, and arrange replacement before expiry.
Do not disable TLS verification. See the Ansible guide for renewal and validation.

## Relocate a stopped installation

The [Ansible relocation command](../../infra/ansible/README.md#relocate-an-existing-installation-with-one-command)
automates preparation, freeze, snapshot transfer, restoration and deployment.
The following sequence describes its data boundaries and the acceptance checks
that follow it.

1. Stop user writes, Beat and other launch producers; finish or cancel all
   pipelines through supported lifecycle operations. Check active execution
   leases, active/reserved/scheduled Celery tasks and broker queues. Keep a
   record of counts and the exact application, submodule and image revisions.
2. Stop workers, the AI bridge and web ingress. Preserve the existing `.env`,
   encryption keys and service tokens in restricted backup storage. Copying an
   installation must not generate replacement encryption keys.
3. Take a final consistent `pg_dump -Fc`, verify its checksum and restore it to
   an isolated PostgreSQL instance before relying on it. Archive media and
   retained analyzer output after writers have stopped. Keep these archives and
   the database snapshot together, with a manifest and an off-host copy.
4. Prepare the target host. Restore into an empty database using the same
   PostgreSQL major version. For a custom-format dump, run `pg_restore
   --exit-on-error --no-owner` with the target database's configured user. Start
   only PostgreSQL for restoration; keep application initialization, workers
   and Beat stopped until data and media are ready.
5. Restore the media volume and `/tmp/aist/output` at their existing container
   paths. Keep the `/tmp/aist` mount: its `projects` child contains disposable
   per-run checkouts and can start empty once every pipeline is terminal.
   New Git runs clone their sources; uploaded archives remain in media. Preserve
   any triage output needed for audit. Do not remove the whole workspace root
   or assume all children are rebuildable.
6. Set the target origin consistently in `DD_SITE_URL`, `PUBLIC_BASE_URL` and
   `AIST_DOMAIN`, including host/CSRF allowlists and persistent TLS paths. Leave
   SMTP identity and credentials unchanged. Keep the DAST gateway on its
   existing host, with its token and CA. Remove an obsolete DAST VPN selection
   through integration configuration, then revalidate and synchronize targets.
7. Start the prepared release. Migration `0061` converts stored source links to
   root-relative paths without changing finding hashes or source versions.
   Unrecognized source-link metadata stops the migration with its record ID;
   investigate it before retrying, rather than leaving mixed link formats.
8. Compare database counts and check login, organization isolation, project and
   finding pages, source snippets, media downloads, SCM access, SMTP and AI
   configuration. Run one controlled SAST pipeline and one DAST pipeline,
   then verify result import, source access and terminal status.
9. Run the new backup job and test restoration. Keep the old host stopped and
   retain its verified pre-migration snapshot until acceptance. Never allow
   both hosts' schedulers or workers to operate on divergent copies of the data.

The daily database timer creates custom-format dumps, validates their table of
contents and records checksums. An optional configured rclone destination gets
the completed dump and checksum before local retention runs. Media, retained
output, deployment secrets and TLS keys need their own protected backups. A dump
listing alone does not demonstrate recoverability; repeat isolated restore tests.

## Restore a PostgreSQL backup

Stop application writes and select the intended snapshot explicitly. Verify its
checksum, create an empty target database, then stream the custom-format dump
into PostgreSQL:

```bash
docker compose --env-file .env exec -T postgres \
  sh -c 'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /path/to/verified-backup.dump
```

This command expects an empty database; it deliberately does not erase existing
data. Older gzip SQL backups require `gzip -dc` piped to `psql -v ON_ERROR_STOP=1`.
Restore media and retained output before starting the application, then verify
counts, migrations, login, project listing and representative source files.

## Release a change to queued task arguments

Celery tasks already queued when the workers restart run with the arguments the
previous release published. When a release changes what a task takes, its
release notes name the task. Before deploying such a release, stop new launches
and wait until the broker queue is empty and no worker holds a reserved message
of a named task:

```bash
docker compose --env-file .env exec -T valkey valkey-cli llen celery
docker compose --env-file .env exec -T celeryworker \
  celery --app=dojo inspect reserved
```

## Validate the deployment

```bash
docker compose --env-file .env ps
docker compose --env-file .env logs --tail=200
docker compose --env-file .env exec -T nginx nginx -t
```

The required long-lived services are Nginx, Django, PostgreSQL, Valkey, Celery
Beat, Celery workers, and the local AI bridge. The deployment wrapper fails if
any of them is absent after startup.

For certificate validation:

```bash
docker compose --env-file .env exec -T nginx \
  openssl x509 -in /etc/nginx/ssl/nginx.crt \
  -noout -issuer -subject -dates -ext subjectAltName
```

## Roll back an application release

1. Record the failing application and database revisions.
2. Restore the previous known-good application revision and its pinned submodules.
   For a data migration, restore the matching pre-upgrade database snapshot too;
   reversing a migration marker does not reconstruct the old source-link origins.
3. Restore the previous certificate pair if TLS changed.
4. Re-run the supported deployment wrapper.
5. Validate Nginx, migrations, service health, login, and project access.

Do not manually delete launch requests, execution leases, or pipeline rows as a
recovery shortcut. Use the application reconciliation and supported database
restore procedures so durable execution history remains consistent.
