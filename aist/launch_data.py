from __future__ import annotations


class PipelineLaunchData:

    """
    Typed facade over the ``launch_data`` JSON field of :class:`AISTPipeline`.

    ``launch_data`` is a mix of:
    - fields returned by the external *sast-combinator* package (we do not own
      these keys and cannot guarantee their presence);
    - fields we write ourselves (log_level, enrich config, etc.).

    This class provides typed, named access to all known fields and preserves
    any unknown keys so that round-tripping through ``from_dict`` / ``as_dict``
    never silently drops data from the external package.

    Usage::

        ld = PipelineLaunchData(pipeline.launch_data)
        ld.languages = ["python"]
        pipeline.launch_data = ld.as_dict()
    """

    def __init__(self, data: dict | None = None) -> None:
        self._data: dict = dict(data or {})

    # ------------------------------------------------------------------ #
    # External — sast-combinator output. No setters: we do not own these. #
    # ------------------------------------------------------------------ #

    @property
    def trim_path(self) -> str:
        return self._data.get("trim_path") or ""

    @property
    def project_path(self) -> str:
        return self._data.get("project_path") or ""

    @property
    def output_dir(self) -> str:
        return self._data.get("output_dir") or ""

    @property
    def tmp_analyzer_config_path(self) -> str | None:
        return self._data.get("tmp_analyzer_config_path")

    @property
    def resolved_commit(self) -> str:
        """Convenience accessor for the nested ``git.resolved_commit`` field."""
        return ((self._data.get("git") or {}).get("resolved_commit") or "").strip()

    # ------------------------------------------------------------------ #
    # Internal — our fields, with setters.                                 #
    # ------------------------------------------------------------------ #

    @property
    def languages(self) -> list[str]:
        return self._data.get("languages") or []

    @languages.setter
    def languages(self, value: list[str]) -> None:
        self._data["languages"] = value

    @property
    def ai(self) -> dict:
        return self._data.get("ai") or {}

    @ai.setter
    def ai(self, value: dict) -> None:
        self._data["ai"] = value

    @property
    def launch_config_id(self) -> str | None:
        return self._data.get("launch_config_id")

    @launch_config_id.setter
    def launch_config_id(self, value: str) -> None:
        self._data["launch_config_id"] = value

    @property
    def log_level(self) -> str:
        return self._data.get("log_level") or "INFO"

    @log_level.setter
    def log_level(self, value: str) -> None:
        self._data["log_level"] = value

    @property
    def project_version_descriptor(self) -> dict:
        return self._data.get("project_version_descriptor") or {}

    @project_version_descriptor.setter
    def project_version_descriptor(self, value: dict) -> None:
        self._data["project_version_descriptor"] = value

    @property
    def imported_test_ids(self) -> list[int]:
        return self._data.get("imported_test_ids") or []

    @imported_test_ids.setter
    def imported_test_ids(self, value: list[int]) -> None:
        self._data["imported_test_ids"] = value

    @property
    def action_runs(self) -> list[dict]:
        return self._data.get("action_runs") or []

    @action_runs.setter
    def action_runs(self, value: list[dict]) -> None:
        self._data["action_runs"] = value

    @property
    def one_off_actions(self) -> list[dict]:
        return self._data.get("one_off_actions") or []

    @one_off_actions.setter
    def one_off_actions(self, value: list[dict]) -> None:
        self._data["one_off_actions"] = value

    @property
    def one_off_actions_done(self) -> list[str]:
        return self._data.get("one_off_actions_done") or []

    @one_off_actions_done.setter
    def one_off_actions_done(self, value: list[str]) -> None:
        self._data["one_off_actions_done"] = value

    # ------------------------------------------------------------------ #
    # Serialisation                                                        #
    # ------------------------------------------------------------------ #

    def merge(self, fields: dict) -> None:
        """Merge a dict of fields (e.g. from ``PipelineArguments.enrich_config()``)."""
        self._data.update(fields)

    def as_dict(self) -> dict:
        """Return the underlying dict for storage in ``pipeline.launch_data``."""
        return self._data
