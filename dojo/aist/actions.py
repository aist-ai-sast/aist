from __future__ import annotations

import logging
import uuid

import requests
from django.conf import settings

from dojo.aist.logging_transport import install_pipeline_logging
from dojo.aist.models import AISTLaunchConfigAction, AISTPipeline
from dojo.aist.utils.action_config import decrypt_action_secret_config
from dojo.aist.utils.export import build_ai_export_csv_text
from dojo.notifications.helper import EmailNotificationManger, SlackNotificationManger

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

    def _get_csv_text(self, pipeline: AISTPipeline) -> str:
        return build_ai_export_csv_text(pipeline)

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SlackAction(BaseAction):
    action_type = AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:
        channels = self.config.get("channels") or []
        if isinstance(channels, str):
            channels = [channels]
        if not channels:
            return

        mgr = SlackNotificationManger()
        token = self.secret_config.get("slack_token") or mgr.system_settings.slack_token
        if not token:
            logger.warning("Slack token missing for action %s", self.action.id)
            return

        title = self.config.get("title") or f"AIST pipeline {pipeline.id} status {new_status}"
        description = self.config.get("description") or self._build_message(
            pipeline=pipeline,
            new_status=new_status,
            csv_text=self._get_csv_text(pipeline),
            for_slack=True,
        )
        message = mgr._create_notification_message(
            "other",
            None,
            "slack",
            {"title": title, "description": description},
        )

        for channel in channels:
            res = requests.request(
                method="POST",
                url="https://slack.com/api/chat.postMessage",
                data={
                    "token": token,
                    "channel": channel,
                    "username": mgr.system_settings.slack_username,
                    "text": message,
                },
                timeout=settings.REQUESTS_TIMEOUT,
            )
            if "error" in res.text:
                logger.error("Slack error for action %s: %s", self.action.id, res.text)


class EmailAction(BaseAction):
    action_type = AISTLaunchConfigAction.ActionType.SEND_EMAIL

    def run(self, *, pipeline: AISTPipeline, new_status: str) -> None:
        emails = self.config.get("emails") or []
        if isinstance(emails, str):
            emails = [emails]
        if not emails:
            return

        title = self.config.get("title") or f"AIST pipeline {pipeline.id} status {new_status}"
        description = self.config.get("description") or self._build_message(
            pipeline=pipeline,
            new_status=new_status,
            csv_text=self._get_csv_text(pipeline),
            for_slack=False,
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
        description = self.config.get("description") or self._build_message(
            pipeline=pipeline,
            new_status=new_status,
            csv_text=self._get_csv_text(pipeline),
            for_slack=False,
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
