from __future__ import annotations

import logging

import requests
from django.conf import settings

from dojo.notifications.helper import SlackNotificationManger

logger = logging.getLogger("dojo.aist")


class AISTSlackNotificationManager(SlackNotificationManger):
    def _resolve_channel_id(self, *, channel: str, token: str) -> str:
        # If it's already a channel ID, return as-is.
        if channel and channel[0] in {"C", "G", "D"}:
            return channel

        name = channel.lstrip("#")
        if not name:
            msg = "Slack channel is empty"
            raise RuntimeError(msg)

        cursor = None
        for _ in range(10):
            res = requests.get(
                url="https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "limit": 1000,
                    "types": "public_channel,private_channel",
                    "cursor": cursor or "",
                },
                timeout=settings.REQUESTS_TIMEOUT,
            )
            try:
                payload = res.json()
            except ValueError:
                payload = {}
            if not payload.get("ok"):
                err = payload.get("error") or res.text
                raise RuntimeError("Error listing Slack channels: " + str(err))

            for item in payload.get("channels") or []:
                if item.get("name") == name:
                    return item.get("id") or channel

            cursor = (payload.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

        msg = f"Slack channel id not found for '{channel}'"
        raise RuntimeError(msg)

    def post_message_with_token(
        self,
        *,
        channel: str,
        message: str,
        token: str,
    ) -> None:
        res = requests.request(
            method="POST",
            url="https://slack.com/api/chat.postMessage",
            data={
                "token": token,
                "channel": channel,
                "username": self.system_settings.slack_username,
                "text": message,
            },
            timeout=settings.REQUESTS_TIMEOUT,
        )
        if "error" in res.text:
            logger.error("Slack post error: %s", res.text)
            raise RuntimeError("Error posting message to Slack: " + res.text)

    def send_message_with_file(
        self,
        *,
        channel: str,
        message: str,
        file_content: str,
        filename: str,
        title: str,
        token: str | None = None,
    ) -> None:
        slack_token = token or self.system_settings.slack_token
        if not slack_token:
            msg = "Slack token missing"
            raise RuntimeError(msg)

        if message:
            self.post_message_with_token(
                channel=channel,
                message=message,
                token=slack_token,
            )

        file_bytes = file_content.encode("utf-8")
        upload_req = requests.post(
            url="https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {slack_token}"},
            data={
                "filename": filename,
                "length": len(file_bytes),
            },
            timeout=settings.REQUESTS_TIMEOUT,
        )
        try:
            upload_meta = upload_req.json()
        except ValueError:
            upload_meta = {}
        if not upload_meta.get("ok"):
            err = upload_meta.get("error") or upload_req.text
            logger.error("Slack getUploadURLExternal error: %s", err)
            raise RuntimeError("Error requesting Slack upload URL: " + str(err))

        upload_url = upload_meta.get("upload_url")
        file_id = upload_meta.get("file_id")
        if not upload_url or not file_id:
            msg = "Slack upload URL response missing upload_url or file_id"
            raise RuntimeError(msg)

        upload_resp = requests.post(
            upload_url,
            files={"filename": (filename, file_bytes)},
            timeout=settings.REQUESTS_TIMEOUT,
        )
        if not upload_resp.ok:
            logger.error("Slack file upload HTTP error: %s", upload_resp.text)
            msg = f"Error uploading file to Slack: HTTP {upload_resp.status_code}"
            raise RuntimeError(msg)

        complete_payload = {
            "files": [{"id": file_id, "title": title}],
            "channel_id": self._resolve_channel_id(channel=channel, token=slack_token),
        }
        complete_req = requests.post(
            url="https://slack.com/api/files.completeUploadExternal",
            headers={"Authorization": f"Bearer {slack_token}"},
            json=complete_payload,
            timeout=settings.REQUESTS_TIMEOUT,
        )
        try:
            complete_meta = complete_req.json()
        except ValueError:
            complete_meta = {}
        if not complete_meta.get("ok"):
            err = complete_meta.get("error") or complete_req.text
            logger.error("Slack completeUploadExternal error: %s", err)
            raise RuntimeError("Error completing Slack upload: " + str(err))
