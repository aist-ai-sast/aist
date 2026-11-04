"""
Derive the `reuse` read set and cut it into slices, as `.sdlc/review-process.md` "The reuse read set" states.

    python3 .sdlc/read_set.py   the read set as JSON: one entry per slice, with its id, size and files
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from git_query import changed_paths, git_entries
from route import READ_SET_REVIEWER, ROOT, SCOPE_PATH, Scope

SLICE_BYTES = 409_600


@dataclass(frozen=True, slots=True)
class Slice:

    """One pass of the read set; its id is the lowest-sorting path it holds."""

    slice_id: str
    files: tuple[str, ...]
    size: int


def text_paths(root: Path) -> frozenset[str]:
    """Return the tracked files git classifies as text, as `.gitattributes` directs; an empty file is not listed."""
    return frozenset(git_entries(root, "grep", "-I", "--name-only", "-z", "-e", ""))


def members(scope: Scope, text: frozenset[str]) -> list[str]:
    """Return every file of `text` claimed by a part routed to `reuse`, less that part's `reuse_excludes`."""
    found = []
    for path in text:
        rule = scope.route(path)
        if rule is not None and READ_SET_REVIEWER in rule.reviewers and not rule.excludes_from_reuse(path):
            found.append(path)
    return sorted(found)


def slices(root: Path, read_set: list[str], changed: list[str], limit: int = SLICE_BYTES) -> list[Slice]:
    """
    Cut `read_set` into slices of at most `limit` bytes, in sorted path order.

    The read-set files that share a directory with a changed path fill the leading slices; the rest follow in
    slices of their own. A file is never split, and one larger than `limit` is a slice of its own.
    """
    size = {path: (root / path).stat().st_size for path in read_set}
    changed_dirs = {PurePosixPath(path).parent for path in changed}
    ordered = sorted(read_set)
    near = [path for path in ordered if PurePosixPath(path).parent in changed_dirs]
    rest = [path for path in ordered if PurePosixPath(path).parent not in changed_dirs]
    groups = [*_pack(near, size, limit), *_pack(rest, size, limit)]
    return [Slice(slice_id=group[0], files=tuple(group), size=sum(size[p] for p in group)) for group in groups]


def _pack(paths: list[str], size: dict[str, int], limit: int) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for path in paths:
        if current and current_size + size[path] > limit:
            groups.append(current)
            current, current_size = [], 0
        current.append(path)
        current_size += size[path]
    if current:
        groups.append(current)
    return groups


def main() -> int:
    read_set = members(Scope.load(SCOPE_PATH), text_paths(ROOT))
    cut = slices(ROOT, read_set, changed_paths(ROOT))
    payload = [{"id": s.slice_id, "size": s.size, "files": list(s.files)} for s in cut]
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
