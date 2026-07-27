# Gerrit integration

The Gerrit SCM integration imports repositories and supplies source for
pipelines and file viewing.

## Configure Gerrit

Create a Gerrit organization integration with:

- the Gerrit server base URL;
- the account's HTTP username;
- the HTTP password generated under **Settings → HTTP Credentials**;
- an optional organization VPN for a private Gerrit server.

The password is stored encrypted. AIST uses Gerrit's authenticated REST and
clone paths for validation, repository discovery, and source acquisition.

## Import repositories

1. Validate the Gerrit integration.
2. On the Projects page, select **Import from Gerrit**.
3. Choose the organization and list accessible projects.
4. Select the projects to import.

Gerrit project names may contain nested path segments. Gerrit does not provide
language statistics through the API used by AIST, so assign supported languages
on the imported project before relying on automatic analyzer selection.

Private validation, discovery, and source acquisition use the VPN selected on
the Gerrit integration.
