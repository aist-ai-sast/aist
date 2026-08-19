"""
VPN sidecar lifecycle management.

vpn_sidecar_context() starts a per-execution Docker container that runs OpenVPN
and an HTTP CONNECT proxy (tinyproxy on :1080).  It yields two values needed by callers:

  container_name  — pass to ``docker run --network container:<name>``
                    so the builder container shares the VPN tunnel transparently.

  proxy_url        — pass to HTTP client libraries as the proxy URL
                     (e.g. ``proxies={"https": proxy_url}``) so Jira, GitLab,
                     and other Celery-side calls are routed through the VPN.

Both values are None when no VPN integration is configured for the project/provider.

Security notes:
- Credentials are decrypted in Celery worker memory and passed via ``docker run -e``.
  Anyone with Docker socket access can read them via ``docker inspect``.
  The Docker socket must be treated as a high-privilege boundary.
- tinyproxy listens on the sidecar's eth0 interface only (not on tun0), so the
  corporate-VPN side cannot reach the proxy.  Connections are restricted to
  AIST_ALLOWED_IP (the celeryworker's eth0 IP) and 127.0.0.1.
- The sidecar container is stopped and removed on context exit, even on exception.
- The container name embeds the execution_id so cleanup_pipeline_containers()
  automatically covers it for pipeline-level cleanup.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

    from aist.integrations.resolver import ResolvedIntegration

logger = logging.getLogger(__name__)

# The sidecar waits up to 30 s for tun0 and 10 s for tinyproxy before declaring readiness.
_SIDECAR_WAIT_SECS = 50
# Written by the entrypoint last: tun0 up, VPN-pushed resolvers in /etc/resolv.conf, proxy listening.
_SIDECAR_READY_MARKER = "/run/aist-vpn-ready"
_VPN_IMAGE_BUILD_TIMEOUT_SECS = 300

# Lines in docker logs output that may expose client DNS server IPs or search domains.
_REDACT_LOG_PREFIXES = ("[VPN] DNS configured", "nameserver ", "search ")

_VPN_CONTAINER_PREFIX = "aist-vpn-"
# The container name doubles as the proxy hostname, so it has to stay one valid DNS label. Docker
# itself accepts a longer name, but a client refuses to resolve a label over 63 characters and the
# request then fails before it is ever sent -- with a parse error, nowhere near the name that caused
# it. Anything composing this name has to respect the limit.
_MAX_CONTAINER_NAME_LENGTH = 63
_CONTAINER_NAME_DIGEST_LENGTH = 12


def vpn_sidecar_container_name(execution_id: str) -> str:
    """
    Return the sidecar container name for *execution_id*, kept resolvable as a single DNS label.

    An execution id that fits is used verbatim, so the name still *contains* the id -- which is what
    ``cleanup_pipeline_containers`` filters on when it removes a pipeline's containers by substring.
    A longer one is shortened and given a digest of the full id, which keeps the name unique and
    reproducible for the same execution without crossing the label limit.
    """
    name = f"{_VPN_CONTAINER_PREFIX}{execution_id}"
    if len(name) <= _MAX_CONTAINER_NAME_LENGTH:
        return name
    digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:_CONTAINER_NAME_DIGEST_LENGTH]
    room = _MAX_CONTAINER_NAME_LENGTH - len(_VPN_CONTAINER_PREFIX) - len(digest) - 1
    stem = execution_id[:room].rstrip("-")
    # The digest alone identifies the execution, so a stem that strips away to nothing is dropped
    # rather than left to contribute a stray separator.
    if not stem:
        return f"{_VPN_CONTAINER_PREFIX}{digest}"
    return f"{_VPN_CONTAINER_PREFIX}{stem}-{digest}"


def _find_executable(name: str) -> str | None:
    return shutil.which(name)


def _get_vpn_sidecar_image() -> str:
    from django.conf import settings  # noqa: PLC0415

    return settings.AIST_VPN_SIDECAR_IMAGE


def _build_vpn_sidecar_if_needed(image: str) -> None:
    """
    Build the VPN sidecar image if it is not present locally.

    Delegates to the shared image contract in sast-pipeline's ``docker_utils.ensure_image``:
    nothing in the runtime deployment builds these images, so every containerized step brings up
    its own, and it does so in one place.

    The Dockerfile context is ``sast-combinator/vpn-sidecar/`` which lives
    next to ``sast-combinator/sast-pipeline/`` (AIST_PIPELINE_CODE_PATH).
    Both directories are COPY-ed into the Django image in Dockerfile.django-debian,
    so the context is always available at runtime inside the Celery worker container.
    """
    docker_bin = _find_executable("docker")
    if docker_bin is None:
        msg = "Docker CLI is not available; cannot manage VPN sidecar containers."
        raise RuntimeError(msg)

    # Checked before anything else is required: an image that is already present must not need
    # the build context to exist, so a deployment that ships the image prebuilt keeps working.
    present = subprocess.run(
        [docker_bin, "images", "-q", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if present.stdout.strip():
        logger.debug("vpn sidecar image=%s already present, skipping build", image)
        return

    from django.conf import settings  # noqa: PLC0415

    pipeline_path = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)
    if not pipeline_path:
        msg = (
            f"VPN sidecar image {image!r} not found and AIST_PIPELINE_CODE_PATH is not set; "
            "cannot build the image automatically."
        )
        raise RuntimeError(msg)

    # Imported here, not at module scope: the sast-pipeline package only enters sys.path at
    # runtime, and this module must stay importable in contexts that never touch Docker.
    from aist.utils.pipeline_imports import _import_sast_pipeline_package  # noqa: PLC0415

    _import_sast_pipeline_package()
    from pipeline.docker_utils import ensure_image  # type: ignore[import-not-found]  # noqa: PLC0415

    ensure_image(
        image,
        str(Path(pipeline_path).parent / "vpn-sidecar"),
        # 5-minute cap; a hung build would block the Celery worker indefinitely.
        timeout=_VPN_IMAGE_BUILD_TIMEOUT_SECS,
    )


def _find_free_port() -> int:
    """
    Ask the OS for a free TCP port on localhost, then release it.

    Note: there is a TOCTOU race between releasing the socket here and Docker
    binding to it.  This race is only active in the rarely-triggered fallback
    path (when the primary named-network path cannot be used).  It is accepted
    as a known limitation of the fallback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _running_in_docker() -> bool:
    """Return True when the current process is running inside a Docker container."""
    return Path("/.dockerenv").exists()


def _get_own_docker_network() -> str | None:
    """
    Return the first named Docker network this container is attached to.

    Used to start the VPN sidecar on the same network so its HTTP CONNECT proxy
    port is reachable by container name — without publishing a host port.  This
    avoids the Linux issue where ``-p 127.0.0.1:{port}:1080`` is NOT reachable
    via ``host.docker.internal`` (unlike Docker Desktop on macOS which works
    around this in its VM layer).

    Reads the container ID from ``/etc/hostname`` and inspects it via Docker
    socket (available in celeryworker via ``/var/run/docker.sock`` bind-mount).
    Skips ``bridge`` and ``host`` pseudo-networks; prefers named user networks.
    Returns ``None`` on any error so callers can fall back gracefully.
    """
    try:
        return _resolve_own_docker_network()
    except Exception:
        return None


def _resolve_own_docker_network() -> str | None:
    docker_bin = _find_executable("docker")
    if docker_bin is None:
        return None
    container_id = Path("/etc/hostname").read_text(encoding="utf-8").strip()
    result = subprocess.run(
        [
            docker_bin,
            "inspect",
            "--format",
            "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
            container_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    networks = [n.strip() for n in result.stdout.splitlines() if n.strip()]
    named = [n for n in networks if n not in {"bridge", "host", "none"}]
    return named[0] if named else None


def _get_own_eth0_ip() -> str | None:
    """
    Return this container's primary IP address (the one used for inter-container traffic).

    Used to pass AIST_ALLOWED_IP to the sidecar so tinyproxy can restrict
    Allow to the celeryworker's IP only.

    Strategy (in order):
    1. Python socket — gethostbyname(gethostname()) — always available, no external command.
    2. ``ip -4 addr show eth0`` — more precise but requires iproute2 tools.
    3. ``hostname -i`` — fallback when ip is unavailable (minimal container images).
    Returns None if running outside a container or all methods fail.
    """
    # Method 1: Python socket — fastest, no subprocess, works everywhere
    try:
        ip = socket.gethostbyname(socket.gethostname())
        # gethostbyname may return 127.0.0.1 on some configurations — skip loopback
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        logger.debug("vpn: socket-based eth0 IP detection failed", exc_info=True)

    # Method 2: ip command (iproute2 — not always installed in minimal images)
    try:
        ip = _eth0_ip_via_ip_command()
        if ip:
            return ip
    except Exception:
        logger.debug("vpn: ip-command eth0 detection failed", exc_info=True)

    # Method 3: hostname -i (busybox / minimal Debian)
    try:
        ip = _eth0_ip_via_hostname_command()
        if ip:
            return ip
    except Exception:
        logger.debug("vpn: hostname-based eth0 detection failed", exc_info=True)

    return None


def _eth0_ip_via_ip_command() -> str | None:
    ip_bin = _find_executable("ip")
    if ip_bin is None:
        msg = "ip not found"
        raise FileNotFoundError(msg)
    result = subprocess.run(
        [ip_bin, "-4", "addr", "show", "eth0"],
        capture_output=True,
        text=True,
        check=False,
    )
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("inet "):
            # "inet 172.20.0.5/16 brd ..."
            return line.split()[1].split("/")[0]
    return None


def _eth0_ip_via_hostname_command() -> str | None:
    hostname_bin = _find_executable("hostname")
    if hostname_bin is None:
        msg = "hostname not found"
        raise FileNotFoundError(msg)
    result = subprocess.run([hostname_bin, "-i"], capture_output=True, text=True, check=False)
    ip = result.stdout.strip().split()[0]
    return ip if ip and not ip.startswith("127.") else None


def _b64(value: str) -> str:
    """
    Base64-encode a string so it can be safely passed as a ``docker run -e`` value.

    Docker CLI truncates env-var values at the first embedded newline when they
    are passed as ``-e KEY=VALUE`` arguments.  Multi-line fields (PEM certs,
    .ovpn config) must therefore be encoded before being placed on the command
    line.  The VPN sidecar entrypoint decodes them with ``base64 -d``.
    """
    return base64.b64encode(value.encode()).decode()


def _extract_key_direction(ovpn_content: str) -> str:
    """
    Extract key-direction value from .ovpn config body.

    Returns the value found in the config ("0" or "1"), or "1" as the default
    (standard OpenVPN client convention).  Only "0" and "1" are valid; any other
    value or absence defaults to "1".

    This is not secret — it is passed as AIST_VPN_TLS_KEY_DIRECTION to the
    entrypoint, which uses it when appending <tls-auth> blocks so the value
    matches what the server expects instead of always hardcoding "1".
    """
    for line in ovpn_content.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "key-direction" and parts[1] in {"0", "1"}:
            return parts[1]
    return "1"


def _assemble_env(vpn_secret) -> dict[str, str]:
    """
    Build the env dict passed to ``docker run -e``.

    Multi-line fields (certs, .ovpn config) are base64-encoded so that Docker
    CLI does not truncate them at embedded newlines.  Single-line credentials
    (username, password) are passed as-is.  Only non-empty fields are included.

    The VPN sidecar entrypoint (entrypoint.sh) decodes the base64 fields with
    ``base64 -d`` before writing them to disk.

    Non-secret metadata (tls_key_type, tls_key_direction) are added separately
    in vpn_sidecar_context alongside AIST_ALLOWED_IP.
    """
    return {
        k: v
        for k, v in {
            "AIST_VPN_OVPN_CONTENT": _b64(vpn_secret.ovpn_content) if vpn_secret.ovpn_content else "",
            "AIST_VPN_CA_CERT": _b64(vpn_secret.ca_cert) if vpn_secret.ca_cert else "",
            "AIST_VPN_CLIENT_CERT": _b64(vpn_secret.client_cert) if vpn_secret.client_cert else "",
            "AIST_VPN_CLIENT_KEY": _b64(vpn_secret.client_key) if vpn_secret.client_key else "",
            "AIST_VPN_TLS_AUTH_KEY": _b64(vpn_secret.tls_auth_key) if vpn_secret.tls_auth_key else "",
            "AIST_VPN_USERNAME": vpn_secret.vpn_username,
            "AIST_VPN_PASSWORD": vpn_secret.vpn_password,
        }.items()
        if v
    }


def _redact_vpn_log(raw: str) -> str:
    """Remove lines that may expose client DNS server IPs or internal search domains."""
    lines = []
    for line in raw.splitlines():
        if any(line.lstrip().startswith(p) for p in _REDACT_LOG_PREFIXES):
            lines.append("[VPN] <DNS config redacted>")
        else:
            lines.append(line)
    return "\n".join(lines)


def _parse_docker_created_at(raw: str) -> datetime | None:
    """
    Parse Docker's CreatedAt string to an aware UTC datetime.

    Docker ps ``--format "{{json .}}"`` format: "2026-04-02 10:05:33 +0000 UTC"
    The original code used ``[:19]`` and appended UTC, silently ignoring the
    timezone offset.  On a Docker host with a non-UTC timezone (e.g. +0200 CEST)
    this caused containers to appear N hours older or newer than actual, leading
    to incorrect orphan cleanup decisions.
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-])(\d{2})(\d{2})", raw)
    if not m:
        return None
    dt_naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    sign = 1 if m.group(2) == "+" else -1
    offset = timezone(sign * timedelta(hours=int(m.group(3)), minutes=int(m.group(4))))
    return dt_naive.replace(tzinfo=offset).astimezone(UTC)


def _wait_for_sidecar_ready(container_name: str, timeout: float = _SIDECAR_WAIT_SECS) -> None:
    """
    Wait for the readiness marker the sidecar writes when its namespace is actually usable.

    Deliberately not ``tun0``: a container joining the namespace inherits its ``/etc/resolv.conf``,
    which the sidecar rewrites with the VPN-pushed resolvers only after the tunnel is up. Probing
    DNS from here cannot substitute -- a VPN-internal name has no answer in this process.

    Dumps the last 50 log lines on timeout, with DNS lines redacted.
    """
    docker_bin = _find_executable("docker")
    if docker_bin is None:
        msg = "Docker CLI is not available; cannot verify VPN sidecar readiness."
        raise RuntimeError(msg)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [docker_bin, "exec", container_name, "test", "-f", _SIDECAR_READY_MARKER],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1.0)

    logs = subprocess.run(
        [docker_bin, "logs", "--tail", "50", container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    logger.error(
        "sidecar=%s did not become ready within %ss. logs:\n%s",
        container_name,
        timeout,
        _redact_vpn_log(logs.stdout + logs.stderr),
    )
    msg = f"VPN sidecar {container_name!r} did not become ready within {timeout:.0f}s"
    raise RuntimeError(msg)


def stop_sidecar(container_name: str) -> None:
    docker_bin = _find_executable("docker")
    if docker_bin is None:
        logger.warning("vpn: docker CLI unavailable while stopping sidecar=%s", container_name)
        return
    try:
        r = subprocess.run([docker_bin, "stop", "--time", "5", container_name], capture_output=True, check=False)
        if r.returncode != 0:
            logger.warning("vpn: docker stop failed rc=%d name=%s", r.returncode, container_name)
    except Exception:
        logger.warning("vpn: docker stop raised for name=%s", container_name, exc_info=True)
    try:
        r = subprocess.run([docker_bin, "rm", "--force", container_name], capture_output=True, check=False)
        if r.returncode != 0:
            logger.warning("vpn: docker rm failed rc=%d name=%s", r.returncode, container_name)
    except Exception:
        logger.warning("vpn: docker rm raised for name=%s", container_name, exc_info=True)


def sidecar_credential_argv(vpn_secret) -> list[str]:
    """
    Build the ``-e`` argv (non-secret TLS metadata + base64 credential env vars)
    shared by every sidecar ``docker run``, ephemeral or warm.

    Kept here so there is a single source of truth for how this image receives
    its credentials; callers only decide name/network/allow policy.
    """
    argv = ["-e", f"AIST_VPN_TLS_KEY_TYPE={getattr(vpn_secret, 'tls_key_type', 'tls-auth') or 'tls-auth'}"]
    argv += ["-e", f"AIST_VPN_TLS_KEY_DIRECTION={_extract_key_direction(vpn_secret.ovpn_content or '')}"]
    for k, v in _assemble_env(vpn_secret).items():
        argv += ["-e", f"{k}={v}"]
    return argv


def run_sidecar_detached(cmd: list[str], *, log_ctx: str) -> None:
    """
    Run a sidecar ``docker run`` command, redacting credentials on failure.

    ``cmd`` contains ``-e KEY=VALUE`` credential args, so on error we log only
    the return code and stderr (which carries no credential values) and drop the
    ``CalledProcessError`` chain (it repeats ``cmd``).
    """
    docker_bin = _find_executable("docker")
    if docker_bin is None:
        msg = "Docker CLI is not available; cannot start VPN sidecar."
        raise RuntimeError(msg)
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        logger.error("%s vpn=docker_run_failed rc=%d stderr=%s", log_ctx, exc.returncode, (exc.stderr or "")[:300])
        msg = f"VPN sidecar failed to start ({log_ctx}, docker rc={exc.returncode})"
        raise RuntimeError(msg) from None


def own_eth0_ip() -> str | None:
    """Public accessor for this container's primary IP (used to build allow lists)."""
    return _get_own_eth0_ip()


def start_named_sidecar(
    container_name: str,
    vpn_secret,
    *,
    allowed_ips: list[str],
    log_ctx: str,
) -> str:
    """
    Start a long-lived sidecar under a FIXED name on this container's Docker
    network and return its HTTP CONNECT proxy URL (``http://<name>:1080``).

    Unlike :func:`vpn_sidecar_context` this does NOT stop the container — the
    caller owns its lifecycle (see ``aist/integrations/egress.py``).  It is the
    single place that knows how to launch the sidecar image with credentials, so
    warm-egress code never rebuilds the ``docker run`` line itself.

    Requires running inside a container on a named Docker network (so the proxy
    is reachable by container name from the web process).  Raises ``RuntimeError``
    if that precondition is not met or the container fails to start.

    Idempotency is the caller's concern; note that a second ``docker run`` with an
    already-used ``--name`` fails — callers treat that as "already running".
    """
    docker_bin = _find_executable("docker")
    if docker_bin is None:
        msg = "Docker CLI is not available; cannot start VPN sidecar."
        raise RuntimeError(msg)

    network = _get_own_docker_network()
    if not network:
        # No named network → the proxy would not be reachable by container name
        # from the web process, which is the whole point of the warm egress.
        msg = f"warm egress requires a named Docker network ({log_ctx}); none detected"
        raise RuntimeError(msg)

    image = _get_vpn_sidecar_image()
    _build_vpn_sidecar_if_needed(image)

    cmd = [
        docker_bin, "run", "-d",
        "--name", container_name,
        "--cap-add", "NET_ADMIN",
        "--device", "/dev/net/tun",
        "--network", network,
    ]
    # Single env var with a comma-separated list; the entrypoint emits one
    # tinyproxy ``Allow`` line per IP.  (Repeated ``-e SAME_KEY`` would collapse
    # to the last value, so a list must go in one variable.)
    if allowed_ips:
        cmd += ["-e", f"AIST_ALLOWED_IP={','.join(allowed_ips)}"]
    cmd += sidecar_credential_argv(vpn_secret)
    cmd.append(image)

    run_sidecar_detached(cmd, log_ctx=log_ctx)
    _wait_for_sidecar_ready(container_name)
    return f"http://{container_name}:1080"


@contextmanager
def vpn_sidecar_context(
    resolved: ResolvedIntegration | None,
    *,
    execution_id: str,
) -> Generator[tuple[str | None, str | None]]:
    """
    Start a VPN sidecar container and yield ``(container_name, proxy_url)``.

    ``container_name``  — use as ``--network container:<name>`` for the builder
                          container so it transparently shares the VPN tunnel.
    ``proxy_url``       — use as ``proxies={"https": url}`` in HTTP clients
                          (Jira, GitLab, YouTrack, etc.) for HTTP CONNECT routing.

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
        logger.error("execution=%s vpn=failed reason=no_ovpn_content", execution_id)
        msg = "The selected VPN integration has no usable OpenVPN configuration."
        raise RuntimeError(msg)

    container_name = vpn_sidecar_container_name(execution_id)

    image = _get_vpn_sidecar_image()
    _build_vpn_sidecar_if_needed(image)
    docker_bin = _find_executable("docker")
    if docker_bin is None:
        msg = "Docker CLI is not available; cannot start VPN sidecar."
        raise RuntimeError(msg)

    cmd = [
        docker_bin,
        "run",
        "-d",
        "--name",
        container_name,
        "--cap-add",
        "NET_ADMIN",
        "--device",
        "/dev/net/tun",
    ]

    if _running_in_docker():
        # Running inside a Celery worker container.  Start the sidecar on the
        # same Docker network so the HTTP CONNECT proxy port is reachable by
        # container name without publishing a host port.
        #
        # Why NOT use -p 127.0.0.1:{port}:1080 here:
        #   On Linux Docker Engine, 127.0.0.1-bound ports are only accessible on
        #   the host's loopback — not via host.docker.internal (which resolves to
        #   the host's bridge gateway IP, not 127.0.0.1).  Docker Desktop on
        #   macOS/Windows hides this by routing host.docker.internal through a VM
        #   shim, but production Linux deployments fail silently with ECONNREFUSED.
        #
        # By sharing the same named network, Docker DNS resolves {container_name}
        # directly to the sidecar's eth0 IP, and tinyproxy listens on :1080 there.
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
                execution_id,
                host_port,
            )

        # Pass our eth0 IP to the sidecar so tinyproxy restricts Allow to us only.
        # This prevents other containers on the shared Docker network from pivoting
        # through the VPN tunnel.
        own_ip = _get_own_eth0_ip()
        if own_ip:
            cmd += ["-e", f"AIST_ALLOWED_IP={own_ip}"]
    else:
        # Running directly on the host (local dev / tests).
        host_port = _find_free_port()
        cmd += ["-p", f"127.0.0.1:{host_port}:1080"]
        proxy_url = f"http://127.0.0.1:{host_port}"

    # TLS metadata + base64 credential env vars (single source of truth).
    cmd += sidecar_credential_argv(vpn_secret)
    cmd.append(image)

    logger.info(
        "execution=%s vpn=starting sidecar=%s proxy=%s",
        execution_id,
        container_name,
        proxy_url,
    )
    try:
        run_sidecar_detached(cmd, log_ctx=f"execution={execution_id}")
        _wait_for_sidecar_ready(container_name)
        logger.info("execution=%s vpn=up sidecar=%s proxy=%s", execution_id, container_name, proxy_url)
        yield container_name, proxy_url
    finally:
        logger.info("execution=%s vpn=stopping sidecar=%s", execution_id, container_name)
        stop_sidecar(container_name)


def _vpn_orphan_max_age_minutes() -> int:
    from django.conf import settings  # noqa: PLC0415

    return settings.AIST_VPN_ORPHAN_MAX_AGE_MINUTES


def _sidecar_ownership() -> tuple[set[str], str]:
    """
    Return the sidecar names a live pipeline still owns, plus the warm-egress prefix.

    A pipeline owns its sidecar until it is terminal, which for a DAST run can be days -- so age is
    not evidence of abandonment. Names are derived forwards from the pipelines because
    :func:`vpn_sidecar_container_name` truncates a long execution id and cannot be reversed.
    Warm-egress sidecars are retired by their own idle reaper, so they are excluded outright.

    Imports are local: this module stays importable where the app registry is not ready.
    """
    from aist.integrations.egress import NAME_PREFIX as WARM_EGRESS_PREFIX  # noqa: PLC0415
    from aist.models import AISTPipeline  # noqa: PLC0415
    from aist.services.pipeline_lifecycle import is_terminal_pipeline_status  # noqa: PLC0415

    names = {
        vpn_sidecar_container_name(pipeline_id)
        for pipeline_id, status in AISTPipeline.objects.values_list("id", "status")
        if not is_terminal_pipeline_status(status)
    }
    return names, WARM_EGRESS_PREFIX


def cleanup_orphaned_vpn_containers(max_age_minutes: int | None = None) -> int:
    """
    Stop and remove abandoned ``aist-vpn-*`` containers older than *max_age_minutes*.

    These are sidecars left behind when a Celery worker was killed before the ``finally`` block in
    :func:`vpn_sidecar_context` could run. Only unowned sidecars are eligible: this is a leak sweep,
    never an execution time limit. Returns the number of containers removed.
    """
    if max_age_minutes is None:
        max_age_minutes = _vpn_orphan_max_age_minutes()
    try:
        owned_names, egress_prefix = _sidecar_ownership()
    except Exception:
        # Fail closed: without the ownership answer the sweep cannot tell a leak from live work.
        logger.warning("cleanup_orphaned_vpn_containers: could not resolve sidecar ownership", exc_info=True)
        return 0
    try:
        docker_bin = _find_executable("docker")
        if docker_bin is None:
            logger.warning("cleanup_orphaned_vpn_containers: docker CLI not available")
            return 0
        result = subprocess.run(
            [
                docker_bin,
                "ps",
                "-a",
                "--filter",
                "name=aist-vpn-",
                "--format",
                "{{json .}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.warning("cleanup_orphaned_vpn_containers: docker ps failed: %s", exc)
        return 0

    now = datetime.now(tz=UTC)
    removed = 0
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue

        name = info.get("Names", "")
        if name in owned_names or name.startswith(egress_prefix):
            logger.debug("cleanup_orphaned_vpn_containers: %s is still owned", name)
            continue
        created_at_raw = info.get("CreatedAt", "")
        created_at = _parse_docker_created_at(created_at_raw)
        if created_at is None:
            logger.debug(
                "cleanup_orphaned_vpn_containers: cannot parse CreatedAt=%r for %s",
                created_at_raw,
                name,
            )
            continue

        age_minutes = (now - created_at).total_seconds() / 60
        if age_minutes < max_age_minutes:
            continue

        logger.warning(
            "cleanup_orphaned_vpn_containers: removing orphaned sidecar %s (age=%.0f min)",
            name,
            age_minutes,
        )
        subprocess.run([docker_bin, "stop", "--time", "5", name], capture_output=True, check=False)
        subprocess.run([docker_bin, "rm", "--force", name], capture_output=True, check=False)
        removed += 1

    if removed:
        logger.info("cleanup_orphaned_vpn_containers: removed %d orphaned VPN containers", removed)
    return removed
