"""
The git questions the review layer asks: tracked and changed paths, submodules, and ignore status.

Every command runs with configuration a repository cannot override, so a repository planted in the working tree
runs nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath

CHECK_IGNORE_NOT_IGNORED = 1  # `git check-ignore -q` exit status for a path no ignore rule matches
GITLINK_MODE = "160000"  # the index mode of a submodule entry
# Configuration a repository cannot override: no fsmonitor command runs, and no bare repository is discovered.
GIT_HARDENING = ("-c", "core.fsmonitor=false", "-c", "safe.bareRepository=explicit")


class GitError(RuntimeError):

    """A git command could not answer; the message says why."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


def run_git(root: Path, *args: str) -> str:
    """Return the stdout of git run in `root`; a failure raises `GitError` with git's exit status and reason."""
    git = shutil.which("git")
    if git is None:
        msg = "git is not on PATH"
        raise GitError(msg)
    try:
        return subprocess.run(
            [git, *GIT_HARDENING, "-C", str(root), *args], capture_output=True, check=True, text=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or "").strip() or f"exit status {exc.returncode}"
        msg = f"git {' '.join(args)}: {reason}"
        raise GitError(msg, exc.returncode) from exc


def git_entries(root: Path, *args: str) -> list[str]:
    """Return the NUL-terminated entries a git command prints with `-z`."""
    return [entry for entry in run_git(root, *args).split("\0") if entry]


def tracked_paths(root: Path) -> list[str]:
    return git_entries(root, "ls-files", "-z")


def submodule_paths(root: Path) -> list[PurePosixPath]:
    """Return the paths at which the index of `root` records a submodule."""
    return [
        PurePosixPath(entry.split("\t", 1)[1])
        for entry in git_entries(root, "ls-files", "--stage", "-z")
        if entry.startswith(f"{GITLINK_MODE} ")
    ]


def is_ignored(root: Path, path: PurePosixPath) -> bool:
    """
    Return whether git ignores `path`, relative to `root`, asking the submodule of `root` that holds it if any.

    Only `root` and the submodules its index records are asked, never a repository found beside the file.
    An answer other than "ignored" or "not ignored" raises `GitError`.
    """
    repository, inside = root, path
    for submodule in submodule_paths(root):
        if path.is_relative_to(submodule):
            repository, inside = root / submodule, path.relative_to(submodule)
            break
    try:
        run_git(repository, "check-ignore", "-q", "--", inside.as_posix())
    except GitError as exc:
        if exc.returncode == CHECK_IGNORE_NOT_IGNORED:
            return False
        raise
    return True


def changed_paths(root: Path) -> list[str]:
    """
    Return every path that differs from HEAD in the index or the working tree, untracked files included.

    A rename or copy reports its destination. An untracked directory is never collapsed into one entry, so a
    new file inside it routes like any other.
    """
    fields = iter(git_entries(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    paths = []
    for entry in fields:
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            next(fields)  # the source path of the rename or copy
        paths.append(path)
    return paths
