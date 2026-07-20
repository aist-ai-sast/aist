# Tenant Isolation and Access

An AIST organization is the tenant boundary for projects and organization-owned
configuration. Access is derived from the authenticated user and is evaluated
again for each protected object; knowing an identifier does not grant access.

![Tenant access model](../assets/tenant-isolation-and-access.svg)

## Organization membership and project visibility

An organization owns a product type and AIST projects. A user must first have
membership in that organization's product type before any project grant can be
used. A full member can see projects permitted by the organization-level role.
A restricted member sees only the projects granted to that user. Per-project
overrides can reduce or deny access; they cannot elevate a user beyond the
organization-level role.

The same ownership chain scopes source versions, pipeline runs, findings,
launch configurations, organization integrations, and work-item providers.
API views obtain authorised querysets for the requested permission and resolve
objects from those querysets. A cross-organization object is therefore not a
valid result for the caller's request.

## Write decisions

Different actions require different permissions. For example, creating a source
version or starting a pipeline requires project edit permission; changing a
finding requires finding permissions; risk acceptance has its own permission;
and membership changes require organization member-management permission.
The API validates request data through serializers before applying a write.

## API tokens

The current API-token model is user-scoped rather than organization-scoped: one
token can authenticate a user across every organization in which that user has
access. This conflicts with the required one-token/one-organization tenant
boundary and is an open security issue.

## Review boundary

Tenant isolation is a cross-cutting security property. Every new API, task, or
background lookup that reaches organization-owned data must preserve this
ownership chain. Focused data-flow pages identify the additional process and
storage boundaries for each operation.
