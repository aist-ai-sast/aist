from __future__ import annotations

from enum import StrEnum


class AISTApiTag(StrEnum):
    AI = "ai"
    AUTH = "auth"
    CALENDAR = "calendar"
    FINDINGS = "findings"
    GITHUB = "gitHub"
    GITLAB = "gitLab"
    LAUNCH_CONFIGS = "launch configs"
    LAUNCH_QUEUE = "launch queue"
    LAUNCH_SCHEDULES = "launch schedules"
    ORGANIZATIONS = "organizations"
    PIPELINES = "pipelines"
    PRODUCTS = "products"
    PROFILE = "profile"
    PROJECTS = "projects"
    WORK_ITEMS = "work items"
