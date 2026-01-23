from __future__ import annotations

import json
import logging
import uuid

from dojo.aist.logging_transport import install_pipeline_logging
from dojo.aist.models import AISTLaunchConfigAction, AISTPipeline
from dojo.aist.notifications import AISTSlackNotificationManager
from dojo.aist.utils.action_config import decrypt_action_secret_config
from dojo.aist.utils.export import build_ai_export_csv_text
from dojo.notifications.helper import EmailNotificationManger

logger = logging.getLogger("dojo.aist")


class BaseAction:
    action_type: str

    def __init__(self, action: AISTLaunchConfigAction) -> None:
        self.action = action
        self.config = action.config or {}
        self.secret_config = action.get_secret_config()

    def _build_message(self, *, pipeline: AISTPipeline, new_status: str, csv_text: str, for_slack: bool) -> str:
        header = f"AIST pipeline {pipeline.id} status changed to {new_status}."
        if not csv_text:
            return f"{header}\n\nNo AI report is available."
        if for_slack:
            return f"{header}\n\nAI report (CSV):\n```{csv_text}```"
        return f"{header}\n\nAI report (CSV):\n{csv_text}"

    def _build_simple_message(self, *, pipeline: AISTPipeline, new_status: str) -> str:
        return f"AIST pipeline {pipeline.id} status changed to {new_status}."

    def _get_csv_text(self, pipeline: AISTPipeline) -> str:
        return build_ai_export_csv_text(pipeline)

    def _include_ai_csv(self) -> bool:
        return bool(self.config.get("include_ai_csv"))

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

    def _build_slack_message(self, *, pipeline: AISTPipeline, new_status: str, title: str) -> str:
        description = self.config.get("description") or f"AIST pipeline {pipeline.id} status {new_status}."
        return AISTSlackNotificationManager()._create_notification_message(
            "other",
            None,
            "slack",
            {"title": title, "description": description},
        )

    def _get_csv_or_raise(self, pipeline: AISTPipeline) -> str:
        ai_response = (
            pipeline.ai_responses
            .order_by("-created")
            .first()
        )
        if not ai_response or not ai_response.payload:
            msg = "AI response not available; Slack file not sent"
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
            msg = "AI report has no rows to export; Slack file not sent"
            raise RuntimeError(msg)
        return csv_text

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

        title = self.config.get("title") or f"AIST pipeline {pipeline.id} status {new_status}"
        include_ai_csv = self._include_ai_csv()
        csv_text = self._get_csv_or_raise(pipeline) if include_ai_csv else None
        message = self._build_slack_message(pipeline=pipeline, new_status=new_status, title=title)

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

        title = self.config.get("title") or f"AIST pipeline {pipeline.id} status {new_status}"
        include_ai_csv = self._include_ai_csv()
        csv_text = ""
        if include_ai_csv:
            csv_text = self._get_csv_text(pipeline)
            if not csv_text:
                msg = "AI report has no rows to export; email not sent"
                raise RuntimeError(msg)

        description = self.config.get("description") or (
            self._build_message(
                pipeline=pipeline,
                new_status=new_status,
                csv_text=csv_text,
                for_slack=False,
            )
            if include_ai_csv
            else self._build_simple_message(
                pipeline=pipeline,
                new_status=new_status,
            )
        )

        mgr = EmailNotificationManger()
        for email in emails:
            mgr.send_mail_notification(
                event="other",
                user=None,
                recipient=email,
                title=title,
                description=description,
                url="",
            )


class WriteLogAction(BaseAction):
    action_type = AISTLaunchConfigAction.ActionType.WRITE_LOG

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:
        level = str(self.config.get("level") or "INFO").upper()
        include_ai_csv = self._include_ai_csv()
        csv_text = ""
        if include_ai_csv:
            csv_text = self._get_csv_text(pipeline)
            if not csv_text:
                msg = "AI report has no rows to export; log not written"
                raise RuntimeError(msg)

        description = self.config.get("description") or (
            self._build_message(
                pipeline=pipeline,
                new_status=new_status,
                csv_text=csv_text,
                for_slack=False,
            )
            if include_ai_csv
            else self._build_simple_message(
                pipeline=pipeline,
                new_status=new_status,
            )
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
