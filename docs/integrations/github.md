# GitHub integration

GitHub can provide source repositories and GitHub Issues. These are separate
capabilities: repository access uses a GitHub App installation, while issue
synchronization uses a work-item provider.

## Source repositories

### Authentication

AIST uses a GitHub App installation rather than storing a long-lived repository
token on the organization integration. The user installing the App selects the
repositories AIST may access. AIST obtains short-lived installation tokens when
it needs repository metadata or source access.

GitHub Enterprise Server can use a configured API base URL. GitHub Enterprise
Server instances reachable only through an organization VPN are not currently
supported for the GitHub App SCM path.

### Import a repository

1. On the Projects page, select **Connect GitHub**.
2. Install or update the AIST GitHub App and select the repositories to expose.
3. Return to AIST and list repositories from the installation.
4. Select one or more repositories and import them.

AIST records the repository identity, its actual default branch, and detected
languages for analyzer selection. The same connection flow can attach an
existing AIST project to one repository.

## GitHub Issues

Create a GitHub work-item provider for the organization. A dedicated personal
or fine-grained token may be supplied. When no provider token is present, AIST
can reuse an available GitHub App installation owned by the same organization.

The repository is normally derived from each issue URL. GitHub issue state is
normalized as follows:

| GitHub issue | AIST category |
|---|---|
| Open | Open |
| Closed | Done |
| Closed as not planned | Cancelled |

GitHub Issues synchronization can use the VPN attached to the work-item
provider, independently of the SCM limitation above.

See [work-item links](../product/work-item-links.md) for refresh behavior and
the effect of ticket status on findings.
