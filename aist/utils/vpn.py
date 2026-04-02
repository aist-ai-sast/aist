"""
VPN sidecar lifecycle management.

vpn_sidecar_context() starts a per-execution Docker container that runs OpenVPN
and a SOCKS5 proxy (microsocks on :1080).  It yields two values needed by callers:

  container_name  — pass to ``docker run --network container:<name>``
                    so the builder container shares the VPN tunnel transparently.

  socks5_proxy_url — pass to HTTP client libraries as the proxy URL
                     (e.g. ``proxies={"https": proxy_url}``) so Jira, GitLab,
                     and other Celery-side calls are routed through the VPN.

Both values are None when no VPN integration is configured for the project/provider.

Security notes:
- Credentials are decrypted in Celery worker memory and passed directly to
  ``docker run -e``; they are never written to disk on the host.
- The SOCKS5 port is bound to 127.0.0.1 only (not accessible from other machines).
- The sidecar container is stopped and removed on context exit, even on exception.
- The container name embeds the execution_id so cleanup_pipeline_containers()
  automatically covers it for pipeline-level cleanup.
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from aist.integrations.resolver import ResolvedIntegration

logger = logging.getLogger(__name__)

_TUN_WAIT_SECS = 35  # tun0 timeout (30 s in sidecar) + 5 s buffer for microsocks startup


def _get_vpn_sidecar_image() -> str:
    from django.conf import settings  # noqa: PLC0415
    return getattr(settings, "AIST_VPN_SIDECAR_IMAGE", "aist-vpn-sidecar:latest")


def _find_free_port() -> int:
    """Ask the OS for a free TCP port on localhost, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _assemble_env(vpn_secret) -> dict[str, str]:
    """
    Build the env dict passed to ``docker run -e``.
    Only non-empty fields are included so the sidecar's assembly logic
    (append-only-if-absent) behaves correctly when ovpn_content already
    contains inline cert blocks.
    """
    return {k: v for k, v in {
        "AIST_VPN_OVPN_CONTENT": vpn_secret.ovpn_content,
        "AIST_VPN_CA_CERT":       vpn_secret.ca_cert,
        "AIST_VPN_CLIENT_CERT":   vpn_secret.client_cert,
        "AIST_VPN_CLIENT_KEY":    vpn_secret.client_key,
        "AIST_VPN_TLS_AUTH_KEY":  vpn_secret.tls_auth_key,
        "AIST_VPN_USERNAME":      vpn_secret.vpn_username,
        "AIST_VPN_PASSWORD":      vpn_secret.vpn_password,
    }.items() if v}


def _wait_for_sidecar_ready(container_name: str, timeout: float = _TUN_WAIT_SECS) -> None:
    """
    Poll ``docker exec <name> ip link show tun0`` until the tunnel interface
    appears (meaning OpenVPN is up and microsocks is starting).
    Dumps the last 50 log lines on timeout to aid diagnosis.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", container_name, "ip", "link", "show", "tun0"],
            capture_output=True,
        )
        if result.returncode == 0:
            # Give microsocks a moment to bind after openvpn comes up
            time.sleep(0.5)
            return
        time.sleep(1.0)

    logs = subprocess.run(
        ["docker", "logs", "--tail", "50", container_name],
        capture_output=True, text=True,
    )
    logger.error(
        "sidecar=%s did not become ready within %ss. logs:\n%s",
        container_name, timeout, logs.stdout + logs.stderr,
    )
    raise RuntimeError(
        f"VPN sidecar {container_name!r} did not become ready within {timeout:.0f}s"
    )


def _stop_sidecar(container_name: str) -> None:
    subprocess.run(["docker", "stop", "--time", "5", container_name], capture_output=True)
    subprocess.run(["docker", "rm", "--force", container_name], capture_output=True)


@contextmanager
def vpn_sidecar_context(
    resolved: ResolvedIntegration | None,
    *,
    execution_id: str,
) -> Generator[tuple[str | None, str | None], None, None]:
    """
    Start a VPN sidecar container and yield ``(container_name, socks5_proxy_url)``.

    ``container_name``    — use as ``--network container:<name>`` for the builder
                            container so it transparently shares the VPN tunnel.
    ``socks5_proxy_url``  — use as ``proxies={"https": url}`` in HTTP clients
                            (Jira, GitLab, YouTrack, etc.) for SOCKS5 routing.

    Both values are ``None`` when ``resolved`` is ``None`` (no VPN needed).

    The sidecar is stopped and removed on context exit (normal or exception).
    Its name contains ``execution_id`` so ``cleanup_pipeline_containers()`` also
    catches it if the worker crashes before the finally block runs.
    """
    if resolved is None:
        yield None, None
        return

    vpn_secret = getattr(resolved.integration, "vpn_secret", None)
    if not vpn_secret or not vpn_secret.ovpn_content:
        logger.warning("execution=%s vpn=skipped reason=no_ovpn_content", execution_id)
        yield None, None
        return

    container_name = f"aist-vpn-{execution_id}"
    host_port = _find_free_port()
    proxy_url = f"socks5://127.0.0.1:{host_port}"
    env_dict = _assemble_env(vpn_secret)

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--cap-add", "NET_ADMIN",
        "--device", "/dev/net/tun",
        # Bind SOCKS5 port to localhost only — not reachable from other hosts
        "-p", f"127.0.0.1:{host_port}:1080",
    ]
    for k, v in env_dict.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(_get_vpn_sidecar_image())

    logger.info(
        "execution=%s vpn=starting sidecar=%s socks5_port=%d",
        execution_id, container_name, host_port,
    )
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        _wait_for_sidecar_ready(container_name)
        logger.info("execution=%s vpn=up sidecar=%s", execution_id, container_name)
        yield container_name, proxy_url
    finally:
        logger.info("execution=%s vpn=stopping sidecar=%s", execution_id, container_name)
        _stop_sidecar(container_name)


def cleanup_orphaned_vpn_containers(max_age_minutes: int = 240) -> int:
    """
    Stop and remove any ``aist-vpn-*`` containers older than *max_age_minutes*.

    These are VPN sidecar containers that were not cleaned up because the Celery
    worker was killed (SIGKILL, OOM) before the ``finally`` block in
    :func:`vpn_sidecar_context` could run.

    Returns the number of containers removed.
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "name=aist-vpn-",
                "--format", "{{json .}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.warning("cleanup_orphaned_vpn_containers: docker ps failed: %s", exc)
        return 0

    now = datetime.now(tz=timezone.utc)
    removed = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue

        name = info.get("Names", "")
        created_at_raw = info.get("CreatedAt", "")
        try:
            # Docker format: "2026-04-02 10:05:33 +0000 UTC"
            created_at = datetime.strptime(created_at_raw[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.debug(
                "cleanup_orphaned_vpn_containers: cannot parse CreatedAt=%r for %s",
                created_at_raw, name,
            )
            continue

        age_minutes = (now - created_at).total_seconds() / 60
        if age_minutes < max_age_minutes:
            continue

        logger.warning(
            "cleanup_orphaned_vpn_containers: removing orphaned sidecar %s (age=%.0f min)",
            name, age_minutes,
        )
        subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True)
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
        removed += 1

    if removed:
        logger.info("cleanup_orphaned_vpn_containers: removed %d orphaned VPN containers", removed)
    return removed
