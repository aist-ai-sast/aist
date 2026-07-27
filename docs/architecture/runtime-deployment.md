# Runtime deployment

AIST runs as a Docker Compose application. Long-lived services handle product
traffic, durable state, queued work, and local AI execution. Celery workers
create additional containers only for the operation that needs them.

![AIST runtime deployment](../assets/runtime-deployment.svg)

## Application ingress

Nginx is the application entry point. It serves or proxies browser and API
traffic to the uWSGI/Django service. Django authenticates the request, applies
the product workflow, reads or writes PostgreSQL, and publishes background work
through Valkey.

The interactive web service does not start scan or VPN containers. Work that
needs Docker, a private route, or a long-running external call is handed to a
worker.

## State and queued work

PostgreSQL stores the durable product and execution state used by both Django
and workers. Valkey transports Celery tasks. Celery Beat publishes recurring
tasks to the same broker, and Celery workers consume them.

The database, not the broker or worker process, determines the lasting state of
a launch, pipeline, finding, or integration. This lets the application expose a
consistent history when a task is retried or a worker restarts.

## Worker-owned runtimes

Celery workers have two local capabilities that are deliberately absent from
the web process:

- access to the Docker socket for per-operation pipeline, connector, and VPN
  containers;
- access to the Unix socket shared with the long-running local AI bridge.

SAST runs create a workspace plus builder and analyzer containers. A standalone
provider run creates its connector and communicates with the external provider.
When private connectivity is required, the operation also receives an
execution-specific VPN sidecar or proxy.

## External boundaries

SCM systems, work-item providers, notification services, AI webhooks, and DAST
gateways are outside the Compose deployment. The owning integration determines
credentials and, where supported, the organization VPN route. Returned reports
and callbacks re-enter through a validated platform boundary before they become
durable state.

## Scaling and trust

Web and worker replicas may share PostgreSQL and Valkey, but every replica must
use the same application configuration and database schema. Capacity for
long-running execution must leave workers available for dispatch, cancellation,
cleanup, and unrelated pipelines.

The Docker socket is a privileged host boundary: a worker that controls the
daemon can inspect or create containers on that host. Production deployments
should isolate worker hosts, restrict deployable images, and limit access to the
socket. See [security boundaries](../security/threat-register.md) and
[SAST pipeline runtime](sast-pipeline-runtime.md).
