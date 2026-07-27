# AIST

![Unit Tests](https://github.com/aist-ai-sast/aist/actions/workflows/unit-tests.yml/badge.svg?branch=master)
![Ruff Linter](https://github.com/aist-ai-sast/aist/actions/workflows/ruff.yml/badge.svg?branch=master)
![Shellcheck](https://github.com/aist-ai-sast/aist/actions/workflows/shellcheck.yml/badge.svg?branch=master)

AIST is a self-hosted application for security-analysis orchestration, finding
triage, and remediation tracking. It runs containerized SAST pipelines, can
launch standalone execution providers such as DAST, imports their reports into
one finding lifecycle, and supports human and AI-assisted review.

## Capabilities

- onboard source repositories or uploaded archives;
- run manual, scheduled, and SCM-triggered security pipelines;
- orchestrate SAST analyzers and standalone execution providers;
- import, deduplicate, enrich, and review findings;
- apply organization, role, and project-level access controls;
- connect source control, VPN routes, work-item systems, Slack, email, and AI
  execution backends;
- retain pipeline, finding, decision, and remediation history.

## Quick start

The development environment requires Docker Desktop.

```bash
docker compose up -d
```

Open `http://localhost:8080`, or `https://localhost:443` when TLS is enabled for
the environment.

## Documentation

Start with the [AIST documentation index](docs/README.md). Useful entry points:

- [AIST and DAST product architecture](docs/product-architecture/README.md)
- [Platform building blocks](docs/architecture/platform-building-blocks.md)
- [Pipeline execution](docs/product/pipeline-execution.md)
- [Access control and roles](docs/security/access-control-and-roles.md)
- [Deployment and recovery](docs/runbooks/deployment-and-recovery.md)

## Development checks

Run project checks through the repository scripts so they use the supported
container environment:

```bash
./run-unittest.sh
./run-rest-framework-tests.zsh
./run-integration-tests.sh
```

Frontend checks use `run-client-ui-tests.zsh`. See `.github/workflows/` for the
CI definitions and `AGENTS.md` for repository-specific contribution rules.
