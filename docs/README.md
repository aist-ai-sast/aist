# AIST documentation

This index routes each reader question to one canonical page. Product and
architecture pages explain stable behavior. Integration pages help configure an
external system. Runbooks contain commands, diagnostics, and recovery.

![AIST documentation map](assets/documentation-map.svg)

## Start here

- [AIST and DAST product architecture](product-architecture/README.md) — what
  the two products own and how they interact
- [Platform building blocks](architecture/platform-building-blocks.md) — how
  the AIST application is divided internally
- [Access control and roles](security/access-control-and-roles.md) — who can see
  and change organization- and project-owned data

## Understand the product

- [Organization and membership](product/organization.md)
- [Project and source onboarding](product/project-and-source-onboarding.md)
- [Pipeline execution](product/pipeline-execution.md)
- [Scheduled pipeline launches](product/scheduled-pipeline-launches.md)
- [Finding review](product/finding-review.md)
- [AI triage](product/ai-triage.md)
- [Work-item links](product/work-item-links.md)
- [Pipeline actions](product/pipeline-actions.md)
- [Canonical deduplication](aist-canonical-dedupe.md)

## Configure an integration

- Source control: [GitHub](integrations/github.md),
  [GitLab](integrations/gitlab.md), [Gerrit](integrations/gerrit.md), and
  [Gitea](integrations/gitea.md)
- Work items: [Jira](integrations/jira.md),
  [GitHub Issues](integrations/github.md), [GitLab Issues](integrations/gitlab.md),
  [YouTrack](integrations/youtrack.md), [Linear](integrations/linear.md), and
  [Azure DevOps](integrations/azure-devops.md)
- Delivery and network: [Slack](integrations/slack.md) and
  [VPN](integrations/vpn.md)
- Execution provider: [DAST](integrations/dast.md)

The [work-item support matrix](product/work-item-links.md#supported-providers)
distinguishes synchronized providers from link-only providers.

## Understand architecture and data flow

- [Runtime deployment](architecture/runtime-deployment.md)
- [Pipeline execution runtime](architecture/sast-pipeline-runtime.md)
- [Source file access](data-flows/source-file-access.md)
- [AI triage execution](data-flows/ai-triage-execution.md)
- [VPN-routed operations](data-flows/vpn-routed-operations.md)

## Operate AIST

- [Deployment and recovery](runbooks/deployment-and-recovery.md)
- [Recompute canonical duplicates](runbooks/canonical-deduplication-recompute.md)
- [DAST operations](runbooks/dast-operations.md)
- [DAST staging compatibility canary](runbooks/dast-staging-canary.md)

## Review security

- [Security documentation](security/README.md)
- [Tenant isolation](security/tenant-isolation-and-access.md)
- [Security boundaries and trust assumptions](security/threat-register.md)

Report suspected vulnerabilities privately according to
[`SECURITY.md`](../SECURITY.md).
