from __future__ import annotations

from drf_spectacular.settings import spectacular_settings


def build_schema_custom_settings(*, preprocessing_hook: str | None = None) -> dict:
    settings = {**spectacular_settings.user_settings}

    preprocessing_hooks = list(settings.get("PREPROCESSING_HOOKS", []))
    if preprocessing_hook:
        preprocessing_hooks = [preprocessing_hook]
    settings["PREPROCESSING_HOOKS"] = preprocessing_hooks

    return settings
