# Gitea integration

The Gitea SCM integration imports repositories and supplies source for
pipelines and file viewing.

## Configure Gitea

Create a Gitea organization integration with:

- the Gitea server base URL;
- a personal access token generated under **Settings → Applications**;
- an optional organization VPN for a private Gitea server.

The token is stored encrypted and is used for repository discovery, metadata,
source access, and HTTPS clone.

## Import repositories

1. Validate the Gitea integration.
2. On the Projects page, select **Import from Gitea**.
3. Choose the organization and list accessible repositories.
4. Select the repositories to import.

AIST records the `owner/repository` identity, the actual default branch, and
the languages reported by Gitea for analyzer selection. Private validation,
discovery, and source acquisition use the VPN selected on the integration.
