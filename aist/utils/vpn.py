"""
VPN sidecar lifecycle management.

vpn_sidecar_context() starts a per-execution Docker container that runs OpenVPN
and a SOCKS5 proxy (microsocks on :1080).  It yields two values needed by callers:

  container_name  — pass to ``docker run --network container:<name>``
                    so the builder container shares the VPN tunnel transparently.

  proxy_url        — pass to HTTP client libraries as the proxy URL
                     (e.g. ``proxies={"https": proxy_url}``) so Jira, GitLab,
                     and other Celery-side calls are routed through the VPN.

Both values are None when no VPN integration is configured for the project/provider.

Security notes:
- Credentials are decrypted in Celery worker memory and passed directly to
  ``docker run -e``; they are never written to disk on the host.
- The SOCKS5 port is bound to 127.0.0.1 only (not accessible from other machines).
- When running inside Docker (Celery worker), callers reach the SOCKS5 port via
  `host.docker.internal` (Docker Desktop) or with `extra_hosts: host.docker.internal:host-gateway`
  on Linux.
- The sidecar container is stopped and removed on context exit, even on exception.
- The container name embeds the execution_id so cleanup_pipeline_containers()
  automatically covers it for pipeline-level cleanup.
"""
from __future__ import annotations

import base64
import json
import logging
import socket
import subprocess
import time
from pathlib import Path
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


def _build_vpn_sidecar_if_needed(image: str) -> None:
    """Build the VPN sidecar image if it is not present locally.

    Mirrors ``build_image_if_needed`` in sast-pipeline's ``analyzer_runner.py``:
    check with ``docker images -q``, skip if already present, build otherwise.

    The Dockerfile context is ``sast-combinator/vpn-sidecar/`` which lives
    next to ``sast-combinator/sast-pipeline/`` (AIST_PIPELINE_CODE_PATH).
    Both directories are COPY-ed into the Django image in Dockerfile.django-debian,
    so the context is always available at runtime inside the Celery worker container.
    """
    present = subprocess.run(
        ["docker", "images", "-q", image],
        capture_output=True, text=True,
    )
    if present.stdout.strip():
        logger.debug("vpn sidecar image=%s already present, skipping build", image)
        return

    from django.conf import settings  # noqa: PLC0415
    pipeline_path = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)
    if not pipeline_path:
        raise RuntimeError(
            f"VPN sidecar image {image!r} not found and AIST_PIPELINE_CODE_PATH is not set; "
            "cannot build the image automatically."
        )
    dockerfile_dir = str(Path(pipeline_path).parent / "vpn-sidecar")
    logger.info("vpn sidecar image=%s not found; building from %s", image, dockerfile_dir)
    subprocess.run(
        ["docker", "build", "-t", image, dockerfile_dir],
        check=True,
    )


def _find_free_port() -> int:
    """Ask the OS for a free TCP port on localhost, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _running_in_docker() -> bool:
    """Return True when the current process is running inside a Docker container."""
    return Path("/.dockerenv").exists()


def _get_own_docker_network() -> str | None:
    """Return the first named Docker network this container is attached to.

    Used to start the VPN sidecar on the same network so its SOCKS5 port is
    reachable by container name — without publishing a host port.  This avoids
    the Linux issue where ``-p 127.0.0.1:{port}:1080`` is NOT reachable via
    ``host.docker.internal`` (unlike Docker Desktop on macOS which works around
    this in its VM layer).

    Reads the container ID from ``/etc/hostname`` and inspects it via Docker
    socket (available in celeryworker via ``/var/run/docker.sock`` bind-mount).
    Skips ``bridge`` and ``host`` pseudo-networks; prefers named user networks.
    Returns ``None`` on any error so callers can fall back gracefully.
    """
    try:
        container_id = Path("/etc/hostname").read_text().strip()
        result = subprocess.run(
            [
                "docker", "inspect",
                "--format", "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
                container_id,
            ],
            capture_output=True, text=True,
        )
        networks = [n.strip() for n in result.stdout.splitlines() if n.strip()]
        named = [n for n in networks if n not in ("bridge", "host", "none")]
        return named[0] if named else None
    except Exception:
        return None


def _b64(value: str) -> str:
    """Base64-encode a string so it can be safely passed as a ``docker run -e`` value.

    Docker CLI truncates env-var values at the first embedded newline when they
    are passed as ``-e KEY=VALUE`` arguments.  Multi-line fields (PEM certs,
    .ovpn config) must therefore be encoded before being placed on the command
    line.  The VPN sidecar entrypoint decodes them with ``base64 -d``.
    """
    return base64.b64encode(value.encode()).decode()


def _assemble_env(vpn_secret) -> dict[str, str]:
    """
    Build the env dict passed to ``docker run -e``.

    Multi-line fields (certs, .ovpn config) are base64-encoded so that Docker
    CLI does not truncate them at embedded newlines.  Single-line credentials
    (username, password) are passed as-is.  Only non-empty fields are included.

    The VPN sidecar entrypoint (entrypoint.sh) decodes the base64 fields with
    ``base64 -d`` before writing them to disk.
    """
    return {k: v for k, v in {
        "AIST_VPN_OVPN_CONTENT": _b64(vpn_secret.ovpn_content) if vpn_secret.ovpn_content else "",
        "AIST_VPN_CA_CERT":      _b64(vpn_secret.ca_cert)       if vpn_secret.ca_cert       else "",
        "AIST_VPN_CLIENT_CERT":  _b64(vpn_secret.client_cert)   if vpn_secret.client_cert   else "",
        "AIST_VPN_CLIENT_KEY":   _b64(vpn_secret.client_key)    if vpn_secret.client_key    else "",
        "AIST_VPN_TLS_AUTH_KEY": _b64(vpn_secret.tls_auth_key)  if vpn_secret.tls_auth_key  else "",
        # Pass the tls key type so the entrypoint uses the correct block tag
        # ("tls-auth" vs "tls-crypt" — different OpenVPN protocols, not interchangeable).
        "AIST_VPN_TLS_KEY_TYPE": getattr(vpn_secret, "tls_key_type", "tls-auth") or "tls-auth",
        "AIST_VPN_USERNAME":     vpn_secret.vpn_username,
        "AIST_VPN_PASSWORD":     vpn_secret.vpn_password,
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
                            (Jira, GitLab, YouTrack, etc.) for HTTP CONNECT proxy routing.

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
    env_dict = _assemble_env(vpn_secret)

    image = _get_vpn_sidecar_image()
    _build_vpn_sidecar_if_needed(image)

    # The sidecar always runs on Docker's default bridge network so that it has a
    # clean single-interface routing table and can reach the VPN server on the internet.
    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--cap-add", "NET_ADMIN",
        "--device", "/dev/net/tun",
    ]

    if _running_in_docker():
        # Running inside a Celery worker container.  Start the sidecar on the
        # same Docker network so the SOCKS5 port is reachable by container name
        # without publishing a host port.
        #
        # Why NOT use -p 127.0.0.1:{port}:1080 here:
        #   On Linux Docker Engine, 127.0.0.1-bound ports are only accessible on
        #   the host's loopback — not via host.docker.internal (which resolves to
        #   the host's bridge gateway IP, not 127.0.0.1).  Docker Desktop on
        #   macOS/Windows hides this by routing host.docker.internal through a VM
        #   shim, but production Linux deployments fail silently with ECONNREFUSED.
        #
        # By sharing the same named network, Docker DNS resolves {container_name}
        # directly to the sidecar's eth0 IP, and microsocks listens on :1080 there.
        own_network = _get_own_docker_network()
        if own_network:
            cmd += ["--network", own_network]
            proxy_url = f"http://{container_name}:1080"
            logger.debug("execution=%s vpn=using network=%s", execution_id, own_network)
        else:
            # Fallback: publish to all interfaces so host.docker.internal can reach it.
            host_port = _find_free_port()
            cmd += ["-p", f"{host_port}:1080"]
            proxy_url = f"http://host.docker.internal:{host_port}"
            logger.warning(
                "execution=%s vpn=could not detect own network, falling back to port publish port=%d",
                execution_id, host_port,
            )
    else:
        # Running directly on the host (local dev / tests).
        host_port = _find_free_port()
        cmd += ["-p", f"127.0.0.1:{host_port}:1080"]
        proxy_url = f"http://127.0.0.1:{host_port}"
    for k, v in env_dict.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(image)

    logger.info(
        "execution=%s vpn=starting sidecar=%s socks5=%s",
        execution_id, container_name, proxy_url,
    )
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        _wait_for_sidecar_ready(container_name)
        logger.info("execution=%s vpn=up sidecar=%s proxy=%s", execution_id, container_name, proxy_url)
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
