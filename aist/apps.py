from django.apps import AppConfig


class AistConfig(AppConfig):
    name = "aist"
    verbose_name = "AIST Integration"

    def ready(self):
        # import modules that register Celery signals
        from . import celery_signals  # noqa: PLC0415, F401,
        from .monkeypatch import install_deduplication_monkeypatch  # noqa: PLC0415
        from .parser_overrides import (  # noqa: PLC0415
            install_bearer_parser_override,
            install_horusec_parser_override,
            install_semgrep_parser_override,
            install_snyk_code_parser_override,
        )

        install_deduplication_monkeypatch()
        install_snyk_code_parser_override()
        install_semgrep_parser_override()
        install_horusec_parser_override()
        install_bearer_parser_override()
