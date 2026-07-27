# GitLab integration

GitLab can provide source repositories and GitLab Issues. The SCM integration
and work-item provider use separate credentials and can be configured
independently.

## Source repositories

Create a GitLab organization integration with:

- a personal access token;
- an optional GitLab base URL, required for a self-managed instance;
- an optional organization VPN for a private instance.

When the base URL is empty, AIST uses GitLab.com. The token is stored encrypted
and is used for repository discovery, metadata, source access, and HTTPS clone.

To import source:

1. Validate the GitLab integration.
2. On the Projects page, select **Import from GitLab**.
3. Choose the organization and list its accessible GitLab projects.
4. Select the projects to import.

AIST preserves nested GitLab namespaces, records the actual default branch,
and imports reported languages for analyzer selection.

## GitLab Issues

Create a GitLab work-item provider with a personal or project access token and
the GitLab project identifier. A self-managed instance also needs its base URL.
Attach a VPN to the provider when that instance is private.

| GitLab issue | AIST category |
|---|---|
| Opened | Open |
| Closed | Done |
| Closed as unresolved, when reported by GitLab | Cancelled |

See [work-item links](../product/work-item-links.md) for refresh behavior.
