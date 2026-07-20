# Runtime Deployment

This page describes the AIST services defined by the production Compose
configuration and the local runtime relationships between them. It is an
operational map, not a system-context diagram.

![AIST runtime deployment](../assets/runtime-deployment.svg)

## Web entry and application processes

Nginx terminates the application-facing traffic and proxies it to the uWSGI
application service. The same application image is used by uWSGI, Celery Beat,
and Celery workers, so the Django models and application configuration remain
consistent across request and background paths.

## State and scheduled work

PostgreSQL is the durable database. Valkey is the Celery broker. Celery Beat
enqueues scheduled work; Celery workers perform the queued work and update
PostgreSQL. The web process also talks to PostgreSQL and publishes background
work through the broker.

## Execution-specific services

Pipeline and VPN containers are not permanent Compose services. A worker uses
the Docker socket to create them for the relevant run or operation. The local
AI triage bridge is a Compose service and shares a Unix socket volume with the
worker; it is available only for local triage or agent-bridge analyzer modes.

## Operational consequence

The Docker socket boundary is privileged: it enables workers to create
containers and inspect their state. Review its mount, the images it may run,
and the host deployment controls with the security owner whenever this runtime
changes. See the [threat register](../security/threat-register.md) for the
tracked review item.
