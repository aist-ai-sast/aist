"""Read deployment settings using Compose's interpolation, without exporting application secrets."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

DOCKER_ENV_KEYS = ("PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")


def compose_environment() -> dict[str, str]:
    """Deployment values come from the selected env file, not the operator's shell."""
    return {key: os.environ[key] for key in DOCKER_ENV_KEYS if key in os.environ}


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    origin: str
    domain: str
    tls_dir: str
    data_dir: str
    acme_webroot: str
    http_port: str
    tls_port: str
    tls_mode: str
    tls_valid_days: int
    backup_dir: str
    backup_retention_days: int
    backup_on_calendar: str
    backup_remote: str
    timezone: str
    firewall_manage: str


def _deployment_origin(origin: str, domain: str, use_tls: str, generate_certificate: str) -> SplitResult:
    parsed = urlsplit(origin.rstrip("/"))
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.path or parsed.query or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", parsed.hostname)
    ):
        msg = "DD_SITE_URL must be an HTTPS origin with a DNS hostname"
        raise ValueError(msg)
    if domain != parsed.hostname:
        msg = "AIST_DOMAIN must match the hostname in DD_SITE_URL"
        raise ValueError(msg)
    if use_tls != "true" or generate_certificate != "false":
        msg = "Production requires USE_TLS=true and GENERATE_TLS_CERTIFICATE=false for persistent TLS"
        raise ValueError(msg)
    return parsed


def _absolute_directory(value: str) -> str:
    if not value or not Path(value).is_absolute() or value == "/" or any(c in value for c in "\r\n\0"):
        msg = "TLS, PostgreSQL and backup directories must be explicit absolute paths"
        raise ValueError(msg)
    return value


def _positive_integer(value: str, name: str) -> int:
    result = int(value)
    if result < 1:
        msg = f"{name} must be positive"
        raise ValueError(msg)
    return result


def deployment_settings(config: object) -> DeploymentSettings:
    """Validate the Compose JSON boundary and retain only deployment fields."""
    try:
        services = config["services"]
        web = services["uwsgi"]["environment"]
        nginx = services["nginx"]["environment"]
        parsed = _deployment_origin(web["DD_SITE_URL"], nginx["AIST_DOMAIN"], nginx["USE_TLS"], nginx["GENERATE_TLS_CERTIFICATE"])
        for service in ("uwsgi", "celeryworker", "celerybeat", "initializer"):
            environment = services[service]["environment"]
            if environment["DD_SITE_URL"].rstrip("/") != parsed.geturl() or environment["PUBLIC_BASE_URL"].rstrip("/") != parsed.geturl():
                msg = "DD_SITE_URL and PUBLIC_BASE_URL must name the same origin in every application service"
                raise ValueError(msg)
        mounts = {volume["target"]: volume["source"] for volume in services["nginx"]["volumes"]}
        ports = {port["target"]: str(port["published"]) for port in services["nginx"]["ports"]}
        options = config["x-aist-deployment"]
        if any(not isinstance(value, str) for value in options.values()):
            msg = "Deployment options must be strings"
            raise ValueError(msg)
        if options["tls_mode"] not in {"selfsigned", "existing"}:
            msg = "AIST_TLS_MODE must be selfsigned or existing"
            raise ValueError(msg)
        if int(ports[443]) != (parsed.port or 443):
            msg = "DD_TLS_PORT must match the port in DD_SITE_URL"
            raise ValueError(msg)
        return DeploymentSettings(
            origin=parsed.geturl(), domain=parsed.hostname,
            tls_dir=_absolute_directory(mounts["/etc/nginx/ssl"]),
            data_dir=_absolute_directory(config["volumes"]["defectdojo_postgres"]["driver_opts"]["device"]),
            acme_webroot=_absolute_directory(mounts["/var/www/certbot"]),
            http_port=ports[8080], tls_port=ports[443], tls_mode=options["tls_mode"],
            tls_valid_days=_positive_integer(options["tls_valid_days"], "tls_valid_days"),
            backup_dir=_absolute_directory(options["backup_dir"]),
            backup_retention_days=_positive_integer(options["backup_retention_days"], "backup_retention_days"),
            backup_on_calendar=options["backup_on_calendar"], backup_remote=options["backup_remote"],
            timezone=options["timezone"], firewall_manage=options["firewall_manage"],
        )
    except (KeyError, TypeError, AttributeError) as exc:
        msg = "Compose configuration is missing a required deployment field or has an invalid type"
        raise ValueError(msg) from exc


def sync_destination(config: object) -> str:
    """Resolve the rsync destination from Compose-interpolated operator settings."""
    try:
        options = config["x-aist-sync"]
        user, host, directory = (options[key] for key in ("user", "host", "directory"))
    except (KeyError, TypeError) as exc:
        msg = "Set REMOTE_USER, REMOTE_HOST and REMOTE_DIR in the selected env file"
        raise ValueError(msg) from exc
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
        msg = "REMOTE_USER must be an SSH username"
        raise ValueError(msg)
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        msg = "REMOTE_HOST must be an SSH hostname or configured alias"
        raise ValueError(msg)
    if not isinstance(directory, str):
        msg = "REMOTE_DIR must be an absolute directory"
        raise TypeError(msg)
    directory = _absolute_directory(directory)
    return f"{user}@{host}:{shlex.quote(directory.rstrip('/') + '/')}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--compose-file", default="docker-compose.yml")
    parser.add_argument("--sync-destination", action="store_true", help="Print the configured rsync destination")
    parser.add_argument("--compose", nargs=argparse.REMAINDER, help="Execute Compose with the selected deployment environment")
    args = parser.parse_args()
    command = ["docker", "compose", "--env-file", args.env_file, "-f", args.compose_file]
    if args.compose is not None:
        os.chdir(args.project_dir)
        os.execvpe("docker", [*command, *args.compose], compose_environment())  # noqa: S606, S607 -- Execute operator's Docker CLI, preserving stdin/signals
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=args.project_dir, env=compose_environment(), capture_output=True, text=True, check=False,
    )
    if result.returncode:
        parser.exit(1, "Compose configuration failed; validate the selected env file locally.\n")
    try:
        config = json.loads(result.stdout)
        output = sync_destination(config) if args.sync_destination else json.dumps(asdict(deployment_settings(config)))
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        parser.exit(1, f"Deployment configuration invalid: {exc}\n")
    sys.stdout.write(output + "\n")


if __name__ == "__main__":
    main()
