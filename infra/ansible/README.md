# Prepare and deploy an AIST host

This playbook prepares an Ubuntu 24.04 host with Docker Compose, persistent TLS,
and database backup automation. Preparation leaves the application stopped so
an operator can restore a database before enabling workers and schedules.

## Supply the checkout and configuration

Check out the intended AIST revision and initialize its pinned submodules on the
host. Add the host, SSH user and absolute `project_dir` to an inventory in the
`aist_prod` group. Verify the SSH host key before connecting. The sample inventory
is a placeholder, not a deployment destination.

Copy the deployment `.env` securely to that checkout and set its permissions to
`0600`. Preserve the database credentials, encryption keys, internal service token
and SMTP configuration when relocating an existing installation. The bootstrap
variables `project_dir`, `compose_env_file`, `compose_file` and `deployment_phase`
select the checkout, configuration and operation; application and operational
values come from the selected env file through Compose interpolation.

Set these deployment values in that env file:

| Variable | Purpose |
|---|---|
| `COMPOSE_PROJECT_NAME` | Stable name for the stack and its named volumes |
| `DD_SITE_URL` | Canonical HTTPS origin, including the published port if nonstandard |
| `AIST_DAST_ALLOWED_PRIVATE_NETWORKS` | Comma-separated gateway CIDRs; no private networks are allowed by default. See [DAST network configuration](../../docs/integrations/dast.md#network-and-credentials). |
| `PUBLIC_BASE_URL` | Same origin; omit it to inherit `DD_SITE_URL` |
| `AIST_DOMAIN` | Hostname from that origin |
| `DD_ALLOWED_HOSTS`, `DD_CSRF_TRUSTED_ORIGINS` | Allowed hostnames and trusted origins for this deployment |
| `DD_TLS_PORT`, `DD_HTTP_PORT` | Published ports |
| `USE_TLS`, `GENERATE_TLS_CERTIFICATE` | `true` and `false`, respectively |
| `NGINX_SSL_HOST_DIR` | Persistent absolute directory containing `nginx.crt` and `nginx.key` |
| `AIST_TLS_MODE` | `selfsigned` (default) or `existing` for a supplied certificate/key pair |
| `AIST_TLS_VALID_DAYS` | Lifetime of a newly generated self-signed certificate; default 365 days |
| `AIST_POSTGRES_DATA_DIR` | Absolute PostgreSQL data directory, mounted at `/var/lib/postgresql` |
| `AIST_BACKUP_DIR` | Absolute directory for restricted, consistent custom-format PostgreSQL dumps |
| `AIST_BACKUP_REMOTE` | Optional preconfigured rclone destination for off-host copies |
| `AIST_BACKUP_RETENTION_DAYS` | Local retention; default 30 days |
| `AIST_BACKUP_ON_CALENDAR` | systemd calendar expression; default daily at 03:00 |
| `AIST_MANAGE_FIREWALL` | Whether to configure UFW; default `false` |
| `TZ` | Host timezone; default `UTC` |

Compose resolves quoting and variable interpolation. The deployment commands
exclude application variables from the invoking shell, so a stale exported URL
cannot override the selected env file. Docker connection and credential-store
settings are retained. Configuration containing application secrets is never
printed by the configuration helper or Ansible.

## Synchronize a checkout with rsync

The existing `sast-combinator/utils/push_to_server.sh` copies the local checkout
with rsync and removes stale code files at the destination. Set `REMOTE_USER`,
`REMOTE_HOST` (an SSH hostname or alias), and the absolute `REMOTE_DIR` in the
selected `.env`. There is no built-in destination. The helper uses Compose to
read these values, so shell application variables cannot redirect the transfer.

Run it from any directory. To select another env file, use
`COMPOSE_ENV_FILE=/absolute/path/to/.env sast-combinator/utils/push_to_server.sh`.
The destination's `.env*` files and `media/` are protected; local review artifacts
are excluded. Provision the destination environment separately. Synchronization
does not start services or run migrations; use the deployment steps below afterward.

## Prepare, restore, then start

Run Ansible from the repository root, with an inventory identifying the intended
host and checkout. Install the pinned collection on the Ansible controller first:

```bash
ansible-galaxy collection install -r infra/ansible/requirements.yml
ANSIBLE_CONFIG=infra/ansible/ansible.cfg ansible-playbook \
  -i /path/to/inventory.ini infra/ansible/playbooks/provision_aist.yml \
  -e deployment_phase=prepare
```

Preparation installs packages and creates directories and TLS files. It starts
no application containers and installs no backup timer. Restore the stopped
installation's database, media and retained pipeline output as described in the
[deployment and recovery runbook](../../docs/runbooks/deployment-and-recovery.md).
Then run the same playbook with `-e deployment_phase=start`.

Start builds the connector and application images, stops application writers,
runs initialization/migrations, starts services, checks migrations and runtime
configuration, and verifies HTTPS using the mounted certificate. A failed
migration leaves the application stopped for investigation. Routine releases use
`./scripts/deploy-prod.sh` on the selected host; it uses `.env` by default.

## Relocate an existing installation with one command

Use `playbooks/relocate_aist.yml` for a full move. The inventory must contain
exactly one host in `aist_source` and one different host in `aist_prod`, each with
its SSH user and `project_dir`. The source SSH user needs direct Docker and backup
access; the target user needs sudo. Prepare the target env file on the controller
with the new origin and paths, preserving the source encryption keys and SMTP.

Supply these bootstrap inputs in a private operator YAML file:

| Input | Purpose |
|---|---|
| `migration_repository`, `migration_revision` | Git repository and full committed revision, including pinned submodules |
| `migration_target_env` | Absolute controller path to the prepared target env file |
| `migration_id` | Unique snapshot name using letters, digits, underscores or hyphens |
| `migration_source_dir` | Absolute source-host directory for retained snapshots |
| `migration_local_dir` | Private absolute controller directory with room for the full snapshot |
| `migration_images` | Optional list of locally built analyzer image tags to preserve |
| `migration_disable_dast_vpn` | Explicitly remove obsolete VPN routing from DAST integrations; default false |

```bash
ANSIBLE_CONFIG=infra/ansible/ansible.cfg ansible-playbook \
  -i /path/to/inventory.ini -e @/path/to/relocation.yml \
  infra/ansible/playbooks/relocate_aist.yml
```

The playbook prepares the target, stops source ingress and scheduling, requires
drained workers and broker queues, then stops application writers. It preserves
the database, media, workspace excluding disposable `projects`, deployment
configuration, local code fixes and selected images. Ansible transfers the
snapshot through the controller with checksum verification. The source remains
stopped and intact for rollback.

Only target PostgreSQL starts for restoration. Existing target database tables,
media or workspace files cause a refusal. After restoring, exact record counts
and finding hashes must match before the existing deployment playbook starts the
application and backup timer. Final checks cover verified HTTPS, canonical source
links and DAST validation/capability synchronization. A controlled SAST and DAST
run and authenticated UI acceptance still follow this command.

Keep the snapshot directory private: it contains application secrets. A failed
restore never drops the partially restored database. Preserve it and select fresh
storage before retrying. A completed snapshot can be reused only while source
writers remain stopped; a verified restore has a `RESTORED` marker. Once the target
accepts new writes, rollback requires a deliberate data-reconciliation decision.

The real PostgreSQL regression scenario is `tests/relocation.yml`; it also checks
corrupted-snapshot rejection and preservation of existing target data. Run it in
a disposable container with Docker CLI, the Docker socket and this checkout.

## Trust and renew TLS

The TLS role uses `community.crypto`; preparation installs its Python cryptography
dependency on the target. Self-signed mode generates a certificate with the
configured hostname in its DNS SAN only when both key and certificate are absent.
Re-running the playbook keeps
the pair. A partial pair, hostname mismatch, missing SAN, mismatched key or less
than one day of validity fails preparation. It never silently changes a trusted
certificate. Existing mode requires an already supplied pair.

Distribute the public certificate to user devices and runtimes that connect to
this origin; keep verification enabled. Before expiry, stage a replacement,
distribute its trust, replace the pair during a maintenance window and recreate
nginx and the bridge. Ansible copies only the public certificate to
`NGINX_SSL_HOST_DIR/trusted`; the bridge mounts this directory read-only and runs
the standard `update-ca-certificates` at startup. HTTPX, Requests and Node use the
resulting system bundle, retaining public roots and hostname verification.
The certificate's private key must remain on the host with mode `0640`,
readable by nginx's group 0. No public DNS, ACME challenge or certbot is needed.

The bridge certificate regression test runs inside its disposable image as root:

```bash
docker run --rm --entrypoint python \
  -v "$PWD/tools/aist-triage-bridge/test_tls_trust.py:/app/bridge/test_tls_trust.py:ro" \
  aist-claude-bridge:latest -m unittest test_tls_trust -v
```

## Verify and restore backups

The start phase enables `aist-db-backup.timer`. A run uses `pg_dump -Fc`, checks
its table of contents with `pg_restore`, writes a SHA-256 file, and uploads both
when an rclone destination is configured. Local retention runs only after those
steps succeed. Jobs are serialized with a lock. Failed partial dumps remain for
diagnosis and are not considered completed backups. Check failures through
`journalctl -u aist-db-backup.service` and verify the timer's next run.

Configure rclone for the root account before enabling off-host copies. Remote
retention is managed by that backup destination. Without `AIST_BACKUP_REMOTE`,
backups remain on the same host and do not protect against host loss. A successful
dump/list check is not a restore test: regularly restore to an isolated database
and check counts and representative records. Database automation does not archive
media, retained analyzer output, the env file or TLS keys; protect these separately
according to the recovery runbook.
