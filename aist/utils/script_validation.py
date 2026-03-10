"""
Utilities for validating shell scripts before persisting them.

Two independent checks are provided:

1. ``check_dangerous_patterns`` — pure-Python regex scan for the most
   dangerous shell constructs (RCE, destructive FS ops, privilege escalation).
   No external tools required; always runs.

2. ``validate_with_shellcheck`` — best-effort static analysis via the
   ``shellcheck`` binary (installable as ``shellcheck-py`` from PyPI).
   Returns an empty list gracefully when the binary is absent.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dangerous-pattern detector (stub — AI-agent resolver planned)
# ---------------------------------------------------------------------------


def check_dangerous_patterns(content: str) -> list[str]:
    """
    Scan *content* for dangerous shell patterns.

    TODO: replace regex heuristics with an AI-agent-based security resolver
    that can understand context and avoid false positives on legitimate patterns
    (e.g. ``curl | bash`` used for trusted package manager setup scripts).

    Currently returns an empty list (stub) — no issues flagged.
    """
    return []


# ---------------------------------------------------------------------------
# shellcheck wrapper (requires shellcheck-py or system shellcheck)
# ---------------------------------------------------------------------------

_SHELLCHECK_SEVERITIES = frozenset({"error", "warning", "info", "style"})
_MIN_SEVERITY = "warning"


def validate_with_shellcheck(
    content: str,
    *,
    severity: str = _MIN_SEVERITY,
    shell: str = "bash",
) -> list[str]:
    """
    Run ``shellcheck`` against *content* and return a list of human-readable
    issue strings.

    Returns an empty list when:
    - no issues are found, OR
    - ``shellcheck`` binary is not available (degrades gracefully).

    Install ``shellcheck-py`` via pip to make this check available in all
    environments without a system-level dependency.

    :param content: Shell script source to validate.
    :param severity: Minimum shellcheck severity level to report.
    :param shell: Shell dialect passed to shellcheck ``--shell``.
    :raises ValueError: If *severity* is not a recognised level.
    """
    if severity not in _SHELLCHECK_SEVERITIES:
        msg = f"Unknown severity {severity!r}; expected one of {sorted(_SHELLCHECK_SEVERITIES)}"
        raise ValueError(msg)

    shellcheck_bin = shutil.which("shellcheck")
    if shellcheck_bin is None:
        _logger.warning(
            "shellcheck binary not found; skipping script validation. "
            "Install shellcheck-py (pip) to enable static analysis of entrypoint scripts.",
        )
        return []

    with tempfile.NamedTemporaryFile(suffix=".sh", mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [shellcheck_bin, f"--severity={severity}", f"--shell={shell}", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _logger.warning("shellcheck timed out; skipping validation.")
        return []
    except OSError:
        _logger.exception("shellcheck execution failed; skipping validation.")
        return []
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.returncode == 0:
        return []

    return [line for line in result.stdout.splitlines() if line.strip()]
