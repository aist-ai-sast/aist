from __future__ import annotations

from enum import StrEnum


class AISTApiTag(StrEnum):
    AI = "ai"
    AUTH = "auth"
    CALENDAR = "calendar"
    FINDINGS = "findings"
    GITHUB = "gitHub"
    GITLAB = "gitLab"
    GERRIT = "gerrit"
    GITEA = "gitea"
    INTEGRATIONS = "integrations"
    LAUNCH_CONFIGS = "launch configs"
    LAUNCH_REQUESTS = "launch requests"
    LAUNCH_SCHEDULES = "launch schedules"
    MEMBERS = "members"
    ORGANIZATIONS = "organizations"
    PIPELINES = "pipelines"
    PRODUCTS = "products"
    PROFILE = "profile"
    PROJECTS = "projects"
    TOKENS = "api tokens"
    WORK_ITEMS = "work items"
