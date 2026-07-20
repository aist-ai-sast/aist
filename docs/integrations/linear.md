# Linear integration

`LINEAR` is a declared `WorkItemProviderType` (`aist/models.py`), so a
`WorkItemProvider` of this type can be created and findings can be linked to
Linear issue URLs the same way as for any other provider.

## No sync backend yet

There is no registered entry for `LINEAR` in
`aist/work_items/backends/registry.py` (only `jira.py`, `github.py`, and
`gitlab.py` register a backend). `aist.work_items.sync.sync_provider` checks
`has_backend(provider.provider_type)` before fetching and silently skips
providers without one — a Linear provider never raises an error, it simply
never receives an automatic status refresh, the same way a `GENERIC`
provider does not.

In practice a Linear link today behaves like a manual link: title, URL, and
status are whatever was entered when the link was created, and they do not
update on their own. See [work-item links](../product/work-item-links.md) for
the general link lifecycle.

## Adding real sync support

Implementing Linear sync means adding a `LinearBackend(WorkItemBackend)` in
`aist/work_items/backends/`, registering it with
`@register_backend("LINEAR")`, and mapping Linear's issue state (Linear uses
GraphQL, not REST) to `WorkItemStatusCategory` — follow
`aist/work_items/backends/jira.py` as the template for the backend shape, but
expect the client library and auth model to differ (Linear's API is
GraphQL-only with an API-key or OAuth bearer token).
