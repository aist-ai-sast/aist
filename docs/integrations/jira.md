# Jira integration

Jira is a work-item provider. It links findings to Jira issues and can refresh
their title and status; it is not an SCM source.

## Configure Jira

Create a Jira work-item provider with the instance base URL and a token:

- **Jira Cloud:** supply the account email and an Atlassian API token. AIST uses
  email-and-token authentication.
- **Jira Data Center or Server:** omit the account email and supply a personal
  access token. AIST uses bearer-token authentication.

Attach an organization VPN when the Jira instance is available only on a
private network. Enable synchronization to allow recurring status refresh.

## Link and refresh issues

Create a link from a finding using the Jira key or URL. AIST queues an initial
refresh and later refreshes links for active providers with synchronization
enabled.

| Jira status category | AIST category |
|---|---|
| New | Open |
| Indeterminate | In progress |
| Done | Done |
| Unrecognized | Unknown |

Jira does not provide a separate cancelled status category through this mapping.
Ticket state remains remediation context and cannot close the AIST finding or
accept its risk. See [work-item links](../product/work-item-links.md).
