"""
Claude as a first-class ``OrgIntegration`` — single concentrator.

This module is the **only** place in the codebase that knows the mapping
between an ``OrgIntegration(type=CLAUDE_CODE)`` and concrete environment
variable names that ``claude -p`` expects. Other modules (``BridgeClient``,
``run_sast_pipeline``, ``build_bridge_client_from_settings``, the
agent_bridge_runner) deal with a generic ``subprocess_env``/``auth_env``
dict and stay agent-agnostic.

Architectural invariants (enforced by Task 14 meta-test):

- I1 — the literal ``"CLAUDE_CODE_OAUTH_TOKEN"`` appears **only** in this
  file (plus its tests and the bridge entrypoint cleanup task).
- The module mirrors the layout of ``aist/utils/vpn.py``: all
  integration-type-specific knowledge concentrated here, generic resolver
  (``aist/integrations/resolver.py``) stays agent-agnostic.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import requests
from pydantic import SecretStr

from aist.integrations.resolver import resolve_integration
from aist.models import OrgIntegrationType

if TYPE_CHECKING:
    from aist.models import AISTProject, OrgIntegration

logger = logging.getLogger(__name__)


_AUTH_MODE_OAUTH = "oauth"
_AUTH_MODE_API_KEY = "api_key"

# The single source of truth for "which env var carries the Claude secret".
# Adding a new auth mode is a one-line change in this dict — no other file
# needs to know.
_ENV_BY_MODE: dict[str, str] = {
    _AUTH_MODE_OAUTH: "CLAUDE_CODE_OAUTH_TOKEN",
    _AUTH_MODE_API_KEY: "ANTHROPIC_API_KEY",
}

_REDACTED = "***REDACTED***"

# OAuth and API-key tokens produced by Anthropic share the ``sk-ant-`` prefix
# but differ in the version segment: OAuth user tokens (from
# ``claude setup-token``) are ``sk-ant-oat<version>-<body>``, API keys are
# ``sk-ant-api<version>-<body>``. The version digits are kept loose
# (``\d+``) so a future format bump (e.g. ``oat02``) does not reject valid
# tokens. Format validation is a quick-fail for typos at save time — the
# authoritative check is the on-demand probe (``probe_claude_token``)
# against the real API.
_OAUTH_TOKEN_PATTERN = re.compile(r"^sk-ant-oat\d+-[A-Za-z0-9_-]{20,}$")
_API_KEY_PATTERN = re.compile(r"^sk-ant-api\d+-[A-Za-z0-9_-]{20,}$")

_PROBE_URL = "https://api.anthropic.com/v1/models"
_PROBE_TIMEOUT_SECONDS = 10


def claude_auth_env(project: AISTProject) -> dict[str, SecretStr]:
    """
    Resolve the project's Claude integration into a subprocess env dict.

    Returns an empty dict if no integration is configured, the secret is
    empty, the integration is inactive, or ``auth_mode`` is unrecognised.
    Callers treat an empty dict as "Claude not available for this project"
    and fail fast at the call site rather than silently passing through.

    The returned secret is wrapped in ``pydantic.SecretStr`` so that
    ``repr()``/``str()``/structured logging frameworks mask it by default.
    Callers must explicitly call ``.get_secret_value()`` to extract the
    raw token at the boundary where it must be passed to the
    ``claude -p`` subprocess (the bridge's ``subprocess.create_subprocess_exec``).
    """
    resolved = resolve_integration(project, OrgIntegrationType.CLAUDE_CODE)
    if resolved is None:
        return {}
    secret = (resolved.integration.secret or "").strip()
    if not secret:
        return {}
    mode = resolved.config.get("auth_mode", _AUTH_MODE_OAUTH)
    env_var = _ENV_BY_MODE.get(mode)
    if env_var is None:
        return {}
    return {env_var: SecretStr(secret)}


def validate_claude_secret_format(secret: str, auth_mode: str = _AUTH_MODE_OAUTH) -> tuple[bool, str]:
    """
    Cheap format-only sanity check for use in API ``validate()`` hooks.

    Does NOT hit the network. Use ``probe_claude_token`` (asynchronously,
    via the existing ``OrgIntegrationValidateAPI`` flow) for real
    credential verification.
    """
    if not secret:
        return False, "token required"
    if auth_mode == _AUTH_MODE_OAUTH:
        if _OAUTH_TOKEN_PATTERN.match(secret):
            return True, ""
        return False, (
            "OAuth token must start with 'sk-ant-oat<version>-' followed by "
            "at least 20 token characters (run `claude setup-token` to obtain)."
        )
    if auth_mode == _AUTH_MODE_API_KEY:
        if _API_KEY_PATTERN.match(secret):
            return True, ""
        return False, (
            "API key must start with 'sk-ant-api<version>-' followed by "
            "at least 20 token characters."
        )
    return False, f"unsupported auth_mode: {auth_mode!r}"


def probe_claude_token(integration: OrgIntegration) -> tuple[bool, str]:
    """
    Perform a single authenticated GET against Anthropic's models endpoint.

    Used by the existing ``OrgIntegrationValidateAPI`` celery dispatch to
    answer the UI's "Validate" button. Honours ``integration.vpn_integration``
    via ``integration.scoped_session(...)`` — if the org routes its outbound
    traffic through a VPN sidecar, this probe will too.

    The detail string MUST NOT contain the token value: ``requests`` does
    not echo Authorization headers in ``str(response)`` / exceptions, but
    we still construct ``detail`` defensively from status codes and
    exception types only.
    """
    secret = (integration.secret or "").strip()
    if not secret:
        return False, "no token stored"
    auth_mode = (integration.config or {}).get("auth_mode", _AUTH_MODE_OAUTH)
    if auth_mode not in _ENV_BY_MODE:
        return False, f"unsupported auth_mode: {auth_mode!r}"

    headers = {
        "Authorization": f"Bearer {secret}",
        "anthropic-version": "2023-06-01",
    }
    try:
        with integration.scoped_session(execution_id=f"claude-probe-{integration.pk}") as session:
            response = session.get(
                _PROBE_URL,
                headers=headers,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
    except requests.ConnectionError:
        logger.warning("Claude probe[%s]: connection error to Anthropic API", integration.pk)
        return False, "unreachable: connection error"
    except requests.Timeout:
        logger.warning("Claude probe[%s]: timeout", integration.pk)
        return False, "unreachable: timeout"
    except Exception as exc:
        # Defensive — exception type name only, never repr(exc) which
        # could include URL+headers in some libraries.
        logger.exception("Claude probe[%s]: unexpected error", integration.pk)
        return False, f"unreachable: {type(exc).__name__}"

    if response.status_code == 200:
        return True, "token accepted by Anthropic API"
    if response.status_code in {401, 403}:
        return False, f"token rejected (HTTP {response.status_code})"
    return False, f"unexpected response (HTTP {response.status_code})"


def redact_claude_secret(text: str, env: dict[str, SecretStr]) -> str:
    """
    Mask known secret values found anywhere in ``text``.

    Used to scrub stderr/stdout/log lines that ``claude -p`` may emit on
    auth failure (the CLI sometimes echoes the rejected token). Only
    masks the literal values present in ``env`` for the current run —
    no hardcoded prefixes, no globally-known token list. This keeps
    the redactor agent-agnostic: the same function works for any future
    agent that uses the generic ``subprocess_env`` channel.
    """
    for secret in env.values():
        value = secret.get_secret_value()
        if value:
            text = text.replace(value, _REDACTED)
    return text
