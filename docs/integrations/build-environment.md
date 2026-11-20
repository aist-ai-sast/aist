# Build environment integration

A build environment integration holds named environment variables that a
project's SAST build needs but that do not belong in its init script — for
example, the credentials of a private package remote such as a Conan
repository. AIST hands the variables to the builder container of each run; it
does not call any external system with them.

## What reaches a run

| Destination | What it receives |
|---|---|
| Builder container: the project's init script, its build command, and every repository build file those commands execute | Every variable of the build environment the project resolves to, as ordinary environment variables |
| Analyzer code that runs inside the builder — the build-capture adapter, currently for C/C++ CodeQL | The builder's full environment, including every build variable |
| Separate analyzer containers | Only the variables an analyzer declares by name in its configuration; a build variable reaches one only when the analyzer declares the same name |
| Pipeline log | The builder's output, with copies of each stored value replaced by `***` on a best-effort basis |
| Saved launch configuration, launch request, broker message | Nothing — the variables are resolved when the run starts and are never written with the launch |

Because values are read when a run starts, a rotated or removed variable takes
effect for the next run that starts, including runs already queued.

## Configure a build environment

1. In **Organization → Integrations**, create an integration of type
   **Build environment**.
2. Under **Variables**, add one row per variable with its name and value.
3. Save. The integration list shows how many variables it holds.

A build environment carries its credentials only in its variables. It has no
separate secret and no VPN route of its own; a builder that must reach a private
network uses the route described in [VPN integration](vpn.md). **Validate**
reports success without a remote check, because there is no external system to
test. An integration's type is fixed when it is created.

Managing organization integrations requires the organization Maintainer or
Owner role. See [access control and roles](../security/access-control-and-roles.md).

## Rotate or remove a variable

- **Rotate:** edit the integration and type the new value into the variable's
  field. A field left blank keeps the stored value.
- **Remove:** delete the variable's row and save. A save replaces the whole set
  of variables, so a row that is not submitted is deleted.
- **Rename:** remove the variable and add it under the new name with its value.
  A stored variable's name cannot be edited in place.

Deleting the integration deletes its variables.

## Choose the build environment for a project

A project uses at most one build environment:

- by default, the organization's earliest-created active build environment;
- a project override can select a different build environment of the same
  organization;
- a project override can disable the build environment, so the project's runs
  receive no build variables.

Organization managers set overrides under **Organization → Integrations →
Per-Project Overrides**; that page is shown only to organization Maintainers and
Owners. A project Maintainer can also change the project's override through the
project integration-override API, which requires permission to operate the
project. When an override selects an integration that is later deactivated, the
project falls back to the organization default; disable the type for the project
when its runs must receive no variables.

## Names and values

A name must be a valid environment variable name of at most 128 characters:
letters, digits, and underscores, not starting with a digit. A value can hold
up to 4096 characters and cannot contain a line break. AIST refuses:

- `REPO_URL` and `PROJECT_NAME`, which the platform sets for every run;
- names the builder runtime assigns or reads as its own configuration, such as
  `PIPELINE_ID`, `PROJECT_PATH`, `LOG_LEVEL`, `JOBS`, `RESERVE_CORES`, and the
  builder and analyzer resource limits;
- the same name twice in one integration;
- a key of the launch's own environment parameters (`env`) that the project's
  build environment also defines.

Any other valid name is accepted and is set in the builder as given, including
names that change how build tools behave, such as `PATH`. A name an analyzer
declares for itself, such as `SNYK_TOKEN`, reaches that analyzer and takes
precedence over a value the deployment configures for the pipeline.

Repeated names, new names without a value, and launch keys that collide are
reported one at a time — the first one found — so correct it and save again
until the save succeeds. Creating or editing a launch configuration, and launching from
one, is refused with the colliding key and the build environment that defines
it. Saving a build environment does not check existing launch configurations,
so a launch from a configuration that has since come to collide is refused
until one side is renamed. A collision that appears after a run is queued is
refused by the execution runtime before the builder starts, and the run
finishes with warnings.

## Credentials and log masking

Variable values are encrypted at rest and are write-only: the API and the
integration form report only each variable's name and whether a value is
stored. The administrative site lists names only and cannot add, change, or
delete variables. A run's values reach the container runtime through an
owner-only file that is removed once the container exits, never on a command
line.

Masking in the pipeline log is best effort, not a confidentiality boundary. It
replaces exact copies of each stored value within a line of output. A build step
that prints a value encoded, split, or in part is not masked, and output written
without line breaks is relayed in pieces of 65,536 characters, so a value that
spans two pieces is not masked either.

Everything a build executes can read the values. That includes the init script
and build command, which project Maintainers control, and the repository's own
build files — for example a Conan recipe that the build installs. Anyone who can
change the source being built, such as a committer to a scanned branch or the
member who adds an archive version, can therefore read the values of the build
environment the project uses, and a project Maintainer can point the project at
any active build environment of the organization.

The builder runs as root with the worker host's Docker socket, which it uses to
start analyzer containers. Code a build executes therefore holds the worker's
Docker-daemon trust, which reaches beyond its own run: organization and project
separation do not isolate build variables from build code on the same worker
host. Treat every source a deployment builds as trusted with that host and with
the build credentials it handles. Worker-host and container-runtime
administrators remain trusted, as for every credential a run uses.

See [pipeline execution runtime](../architecture/sast-pipeline-runtime.md) for
the builder and analyzer containers, and
[security boundaries](../security/threat-register.md) for the residual trust
assumptions.
