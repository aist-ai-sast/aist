# AIST Production IaC (Ubuntu 24.04)

## Files
- `playbooks/provision_aist.yml` provisions host packages, firewall, TLS automation, and DB backups.
- `inventory/prod.ini` contains production host(s).
- `group_vars/prod.yml` contains deployment variables.

## Configure
1. Update `inventory/prod.ini` with your host and SSH user.
2. Update `group_vars/prod.yml`:
   - `domain`
   - `project_dir`
   - `acme_webroot_host`
   - `nginx_ssl_host_dir`
   - `certbot_email` (recommended) or `certbot_register_unsafe_without_email: true`
   - backup schedule/retention values

## Run provisioning
```bash
ANSIBLE_CONFIG=infra/ansible/ansible.cfg ansible-playbook -i infra/ansible/inventory/prod.ini infra/ansible/playbooks/provision_aist.yml
```

## Validate TLS
```bash
ssh <host> 'certbot certificates'
ssh <host> 'certbot renew --dry-run --run-deploy-hooks --no-random-sleep-on-renew --deploy-hook /usr/local/lib/aist/certbot-deploy-hook.sh'
ssh <host> 'cd /root/work/aist-defect-dojo && docker compose --env-file .env exec -T nginx openssl x509 -in /etc/nginx/ssl/nginx.crt -noout -issuer -subject -dates -ext subjectAltName'
ssh <host> 'ls -l /root/work/aist/nginx-ssl/nginx.key'
```

Notes:
- Renew uses `http-01` with webroot from `acme_webroot_host`.
- `nginx.key` must be `-rw-r-----` (`0640`) so nginx in container (`uid=1001`, `gid=0`) can read it.

## Validate HTTP/HTTPS behavior
```bash
curl -I http://aist.itsec-europe.com/
curl -i http://aist.itsec-europe.com/.well-known/acme-challenge/test
```

## Validate backup automation
```bash
ssh <host> 'systemctl status aist-db-backup.timer --no-pager'
ssh <host> 'systemctl start aist-db-backup.service'
ssh <host> 'ls -lh /var/backups/aist-postgres'
ssh <host> 'journalctl -u aist-db-backup.service -n 100 --no-pager'
```
