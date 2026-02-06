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

**Integrations**
- GitHub: projects can be created via GitHub App events; source links resolve to GitHub repositories.
- GitLab: import projects by ID or list projects via API, including self-hosted GitLab.
- Archives: upload source archives to create immutable project versions.

**Export & Notifications**
- Export AI triage results for a pipeline.
- Optional actions on pipeline status: send to Slack or email.


**Architecture**
- Django application (`aist`, `aist_site`)
- Analyzer pipeline runner (`sast-combinator`)
- Docker-based execution environment

**Quickstart**
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
https://localhost:8443
```

**Tests**
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

**CI**
GitHub Actions runs unit tests and linting on `master`. See workflows in `.github/workflows`.

**Configuration**
- Environment variables are loaded via `docker-compose.yml` and `.env`.
- Local settings can be mounted via `docker/extra_settings`.

**Troubleshooting**
- Check service logs:
```bash
docker compose logs --tail=200
```
- Rebuild images:
```bash
docker compose build --no-cache
```
