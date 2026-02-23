from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import urlencode, urljoin

from django.core.mail import EmailMessage
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone
from dojo.models import Finding
from dojo.notifications.helper import EmailNotificationManger

from aist.logging_transport import install_pipeline_logging
from aist.models import AISTLaunchConfigAction, AISTPipeline
from aist.notifications import AISTSlackNotificationManager
from aist.utils.action_config import decrypt_action_secret_config
from aist.utils.export import build_ai_export_csv_text
from aist.utils.project_version_refs import resolve_project_version_git_refs
from aist.utils.urls import get_public_base_url

logger = logging.getLogger("aist")


class BaseAction:
    action_type: str

    def __init__(self, action: AISTLaunchConfigAction) -> None:
        self.action = action
        self.config = action.config or {}
        self.secret_config = action.get_secret_config()

    def _build_simple_message(self, *, pipeline: AISTPipeline, new_status: str) -> str:
        project_name = self._get_project_name(pipeline)
        branch = self._get_branch(pipeline)
        commit = self._get_commit(pipeline)
        findings_url = self._build_pipeline_findings_url(pipeline)
        return (
            f"AIST pipeline {pipeline.id} status changed to {new_status}.\n"
            f"Project: {project_name}\n"
            f"Branch: {branch}\n"
            f"Commit: {commit}\n"
            f"Findings: {findings_url}"
        )

    def _build_default_title(self, *, pipeline: AISTPipeline, new_status: str) -> str:
        project_name = self._get_project_name(pipeline)
        return f"AIST [{project_name}] pipeline {pipeline.id} status {new_status}"

    def _build_common_summary(self, *, pipeline: AISTPipeline, new_status: str, for_slack: bool) -> str:
        findings_qs = Finding.objects.filter(test__aist_pipelines=pipeline)
        total_findings = findings_qs.count()
        false_positive_findings = findings_qs.filter(false_p=True).count()
        severity_counts_raw = findings_qs.order_by().values("severity").annotate(total=Count("id"))
        severity_counts = {str(item["severity"] or "Info"): int(item["total"]) for item in severity_counts_raw}

        severity_order = ["Critical", "High", "Medium", "Low", "Info"]
        severity_text = " | ".join(f"{sev}: {severity_counts.get(sev, 0)}" for sev in severity_order)

        if pipeline.project_version:
            version_text = f"{pipeline.project_version.version_type}:{pipeline.project_version.version}"
        else:
            version_text = "unknown"

        duration = self._pipeline_duration(pipeline)
        findings_url = self._build_pipeline_findings_url(pipeline)

        if for_slack:
            return (
                f"*AIST Pipeline Summary* (`{pipeline.id}`)\n"
                f"*Status:* {new_status}\n"
                f"*Project:* {self._get_project_name(pipeline)}\n"
                f"*Project version:* {version_text}\n"
                f"*Branch:* {self._get_branch(pipeline)}\n"
                f"*Commit:* {self._get_commit(pipeline)}\n"
                f"*Duration:* {duration}\n"
                f"*Findings total:* {total_findings}\n"
                f"*False positives:* {false_positive_findings}\n"
                f"*Severity:* {severity_text}\n"
                f"*Findings:* {findings_url}"
            )
        return (
            f"AIST Pipeline Summary ({pipeline.id})\n"
            f"Status: {new_status}\n"
            f"Project: {self._get_project_name(pipeline)}\n"
            f"Project version: {version_text}\n"
            f"Branch: {self._get_branch(pipeline)}\n"
            f"Commit: {self._get_commit(pipeline)}\n"
            f"Duration: {duration}\n"
            f"Findings total: {total_findings}\n"
            f"False positives: {false_positive_findings}\n"
            f"Severity: {severity_text}\n"
            f"Findings: {findings_url}"
        )

    @staticmethod
    def _get_project_name(pipeline: AISTPipeline) -> str:
        return pipeline.project.product.name

    @staticmethod
    def _get_branch(pipeline: AISTPipeline) -> str:
        refs = resolve_project_version_git_refs(pipeline.project_version)
        return refs.branch or "unknown"

    @staticmethod
    def _get_commit(pipeline: AISTPipeline) -> str:
        refs = resolve_project_version_git_refs(pipeline.project_version)
        return refs.commit or "unknown"

    @staticmethod
    def _build_pipeline_findings_url(pipeline: AISTPipeline) -> str:
        base_path = reverse("findings")
        query = urlencode({"product": pipeline.project.product_id, "pipeline": pipeline.id})
        path = f"{base_path}?{query}"
        base_url = get_public_base_url()
        if not base_url:
            return path
        return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

    @staticmethod
    def _pipeline_duration(pipeline: AISTPipeline) -> str:
        end = pipeline.updated or timezone.now()
        start = pipeline.started or pipeline.created
        if not start:
            return "unknown"
        if pipeline.created and start >= end and pipeline.created < end:
            start = pipeline.created
        if end < start and pipeline.created:
            start = pipeline.created
        end = max(end, start)
        seconds = int((end - start).total_seconds())
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    def _include_ai_csv(self) -> bool:
        return bool(self.config.get("include_ai_csv"))

    def _include_common_summary(self) -> bool:
        return bool(self.config.get("include_common_summary"))

    @staticmethod
    def _build_csv_from_ai_response_or_raise(pipeline: AISTPipeline) -> str:
        ai_response = pipeline.ai_responses.order_by("-created").first()
        if not ai_response or not ai_response.payload:
            msg = "AI response not available; CSV file not sent"
            raise RuntimeError(msg)

        payload = ai_response.payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                msg = "AI response payload is not valid JSON"
                raise RuntimeError(msg) from exc

        csv_text = build_ai_export_csv_text(pipeline, payload=payload, ignore_false_positives=True)
        if not csv_text:
            msg = "AI report has no rows to export; CSV file not sent"
            raise RuntimeError(msg)
        return csv_text

    def _validate_summary_mode(self) -> tuple[bool, bool]:
        include_ai_csv = self._include_ai_csv()
        include_common_summary = self._include_common_summary()
        if include_ai_csv and include_common_summary:
            msg = "include_common_summary and include_ai_csv are mutually exclusive"
            raise RuntimeError(msg)
        return include_ai_csv, include_common_summary

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SlackAction(BaseAction):
    action_type = AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK

    def _get_channels(self) -> list[str]:
        channels = self.config.get("channels") or []
        if isinstance(channels, str):
            channels = [channels]
        return [c for c in channels if c]

    def _get_token(self, mgr: AISTSlackNotificationManager) -> str | None:
        return self.secret_config.get("slack_token") or mgr.system_settings.slack_token

    def _build_slack_message(
        self,
        *,
        pipeline: AISTPipeline,
        new_status: str,
        title: str,
        include_common_summary: bool,
    ) -> str:
        description = self.config.get("description")
        if not description:
            if include_common_summary:
                description = self._build_common_summary(
                    pipeline=pipeline,
                    new_status=new_status,
                    for_slack=True,
                )
            else:
                description = self._build_simple_message(
                    pipeline=pipeline,
                    new_status=new_status,
                )
        return AISTSlackNotificationManager()._create_notification_message(
            "other",
            None,
            "slack",
            {"title": title, "description": description},
        )

    def _get_csv_or_raise(self, pipeline: AISTPipeline) -> str:
        return self._build_csv_from_ai_response_or_raise(pipeline)

    def _send_channel_message(
        self,
        *,
        mgr: AISTSlackNotificationManager,
        channel: str,
        message: str,
        token: str,
        title: str,
        csv_text: str | None,
        pipeline_id: str,
    ) -> None:
        if csv_text:
            logger.info("Sending Slack message+file to %s for pipeline %s", channel, pipeline_id)
            mgr.send_message_with_file(
                channel=channel,
                message=message,
                file_content=csv_text,
                filename=f"aist_ai_results_{pipeline_id}.csv",
                title=title,
                token=token,
            )
            logger.info("Slack file upload succeeded for %s (pipeline %s)", channel, pipeline_id)
        else:
            mgr.post_message_with_token(
                channel=channel,
                message=message,
                token=token,
            )
            logger.info("Slack message sent (no AI CSV) for %s (pipeline %s)", channel, pipeline_id)

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:
        channels = self._get_channels()
        if not channels:
            return

        mgr = AISTSlackNotificationManager()
        token = self._get_token(mgr)
        if not token:
            logger.warning("Slack token missing for action %s", self.action.id)
            return

        title = self.config.get("title") or self._build_default_title(
            pipeline=pipeline,
            new_status=new_status,
        )
        include_ai_csv, include_common_summary = self._validate_summary_mode()
        csv_text = self._get_csv_or_raise(pipeline) if include_ai_csv else None
        message = self._build_slack_message(
            pipeline=pipeline,
            new_status=new_status,
            title=title,
            include_common_summary=include_common_summary,
        )

        had_error = False
        error_message = ""

        for channel in channels:
            try:
                self._send_channel_message(
                    mgr=mgr,
                    channel=channel,
                    message=message,
                    token=token,
                    title=title,
                    csv_text=csv_text if include_ai_csv else None,
                    pipeline_id=pipeline.id,
                )
            except Exception as exc:
                logger.exception("Slack notification failed for action %s", self.action.id)
                had_error = True
                error_message = str(exc)

        if had_error:
            raise RuntimeError(error_message)


class EmailAction(BaseAction):
    action_type = AISTLaunchConfigAction.ActionType.SEND_EMAIL

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:
        emails = self.config.get("emails") or []
        if isinstance(emails, str):
            emails = [emails]
        if not emails:
            return

        title = self.config.get("title") or self._build_default_title(
            pipeline=pipeline,
            new_status=new_status,
        )
        include_ai_csv, include_common_summary = self._validate_summary_mode()

        description = self.config.get("description") or (
            self._build_common_summary(
                pipeline=pipeline,
                new_status=new_status,
                for_slack=False,
            )
            if include_common_summary
            else self._build_simple_message(
                pipeline=pipeline,
                new_status=new_status,
            )
        )

        mgr = EmailNotificationManger()
        csv_text = self._build_csv_from_ai_response_or_raise(pipeline) if include_ai_csv else None
        for email in emails:
            if not csv_text:
                mgr.send_mail_notification(
                    event="other",
                    user=None,
                    recipient=email,
                    title=title,
                    description=description,
                    url="",
                )
                continue

            notification_payload = {"title": title, "description": description, "url": ""}
            body = mgr._create_notification_message("other", None, "mail", notification_payload)
            subject = f"{mgr.system_settings.team_name} notification: {title}"
            email_message = EmailMessage(
                subject,
                body,
                mgr.system_settings.email_from,
                [email],
                headers={"From": f"{mgr.system_settings.email_from}"},
            )
            email_message.content_subtype = "html"
            email_message.attach(
                filename=f"aist_ai_results_{pipeline.id}.csv",
                content=csv_text,
                mimetype="text/csv",
            )
            email_message.send(fail_silently=False)


class WriteLogAction(BaseAction):
    action_type = AISTLaunchConfigAction.ActionType.WRITE_LOG

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:
        level = str(self.config.get("level") or "INFO").upper()

        description = self.config.get("description") or self._build_simple_message(
            pipeline=pipeline,
            new_status=new_status,
        )

        logger_inst = install_pipeline_logging(pipeline.id, level)
        log_fn = getattr(logger_inst, level.lower(), logger_inst.info)
        log_fn(description)


class OneOffAction:
    def __init__(self, *, action_id: str, action_type: str, config: dict, secret_config: dict) -> None:
        self.id = action_id
        self.action_type = action_type
        self.config = config or {}
        self._secret_config = secret_config or {}

    def get_secret_config(self) -> dict:
        return self._secret_config


def build_one_off_action(action_payload: dict) -> OneOffAction | None:
    if not action_payload:
        return None
    action_id = str(action_payload.get("id") or uuid.uuid4().hex)
    action_type = action_payload.get("action_type")
    if not action_type:
        return None
    config = action_payload.get("config") or {}
    secret_config = decrypt_action_secret_config(action_payload.get("secret_config") or {})
    return OneOffAction(
        action_id=action_id,
        action_type=action_type,
        config=config,
        secret_config=secret_config,
    )


_ACTION_HANDLERS = {
    AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK: SlackAction,
    AISTLaunchConfigAction.ActionType.SEND_EMAIL: EmailAction,
    AISTLaunchConfigAction.ActionType.WRITE_LOG: WriteLogAction,
}


def get_action_handler(action: AISTLaunchConfigAction | OneOffAction) -> BaseAction | None:
    handler_cls = _ACTION_HANDLERS.get(action.action_type)
    if not handler_cls:
        return None
    return handler_cls(action)
