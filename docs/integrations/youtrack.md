# YouTrack integration

`YOUTRACK` is a declared `WorkItemProviderType` (`aist/models.py`), so a
`WorkItemProvider` of this type can be created and findings can be linked to
YouTrack ticket URLs the same way as for any other provider.

## No sync backend yet

Unlike Jira, GitHub, and GitLab, there is no registered entry for `YOUTRACK`
in `aist/work_items/backends/registry.py` (only `jira.py`, `github.py`, and
`gitlab.py` register a backend). `aist.work_items.sync.sync_provider` checks
`has_backend(provider.provider_type)` before fetching and silently skips
providers without one — a YouTrack provider never raises an error, it simply
never receives an automatic status refresh, the same way a `GENERIC`
provider does not.

In practice a YouTrack link today behaves like a manual link: title, URL, and
status are whatever was entered when the link was created, and they do not
update on their own. See [work-item links](../product/work-item-links.md) for
the general link lifecycle, and [work-item-links.md](../product/work-item-links.md)'s
"Status refresh lifecycle" section for what an implemented backend would add.

## Adding real sync support

Implementing YouTrack sync means adding a `YoutrackBackend(WorkItemBackend)`
in `aist/work_items/backends/`, registering it with
`@register_backend("YOUTRACK")`, and mapping YouTrack's issue state model to
`WorkItemStatusCategory` — follow `aist/work_items/backends/jira.py` as the
template for a REST-token-based backend.
