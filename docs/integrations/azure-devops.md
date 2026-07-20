# Azure DevOps integration

`AZURE_DEVOPS` is a declared `WorkItemProviderType` (`aist/models.py`), so a
`WorkItemProvider` of this type can be created and findings can be linked to
Azure DevOps work-item URLs the same way as for any other provider.

## No sync backend yet

There is no registered entry for `AZURE_DEVOPS` in
`aist/work_items/backends/registry.py` (only `jira.py`, `github.py`, and
`gitlab.py` register a backend). `aist.work_items.sync.sync_provider` checks
`has_backend(provider.provider_type)` before fetching and silently skips
providers without one — an Azure DevOps provider never raises an error, it
simply never receives an automatic status refresh, the same way a `GENERIC`
provider does not.

In practice an Azure DevOps link today behaves like a manual link: title,
URL, and status are whatever was entered when the link was created, and they
do not update on their own. See [work-item links](../product/work-item-links.md)
for the general link lifecycle.

## Adding real sync support

Implementing Azure DevOps sync means adding an
`AzureDevopsBackend(WorkItemBackend)` in `aist/work_items/backends/`,
registering it with `@register_backend("AZURE_DEVOPS")`, and mapping Azure
Boards' work-item state model (state names are configurable per process
template, unlike Jira's fixed status-category model) to
`WorkItemStatusCategory` — follow `aist/work_items/backends/jira.py` as the
template for the backend shape, but expect to need a per-organization or
per-process-template state mapping rather than a fixed table.
