"""Start the bridge image's real entrypoint from a test running inside that image as root."""
import os
import subprocess
import time

import httpx

STARTUP_SECONDS = 30


def start_bridge(socket_path: str, **env: str) -> subprocess.Popen:
    """Run ``/entrypoint.sh`` serving ``socket_path``, with ``env`` over the container's environment."""
    return subprocess.Popen(
        ["/entrypoint.sh"],
        env={**os.environ, **env, "AIST_LOCAL_TRIAGE_BRIDGE_SOCKET": socket_path},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def wait_until_serving(bridge: subprocess.Popen, socket_path: str) -> bool:
    """Whether this bridge process answers its health check on ``socket_path`` within the startup window."""
    deadline = time.monotonic() + STARTUP_SECONDS
    with httpx.Client(transport=httpx.HTTPTransport(uds=socket_path)) as client:
        while bridge.poll() is None and time.monotonic() < deadline:
            try:
                if client.get("http://bridge/health").status_code == 200:
                    return bridge.poll() is None
            except httpx.TransportError:
                pass
            time.sleep(0.1)
    return False
