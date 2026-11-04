"""
Route changed paths to the reviewers `.sdlc/scope.yaml` assigns them.

`scope.yaml` is the one authority for routing; this module is its one reader. It needs PyYAML and jsonschema
on the interpreter that runs it.

    python3 .sdlc/route.py           the working tree's changes by part, and the reviewers they need
    python3 .sdlc/route.py --json    the same, as each reviewer's paths for the review brief
    python3 .sdlc/route.py --check   every tracked file routes and every invariant holds; exit 1 otherwise
    python3 .sdlc/route.py --hook    PostToolUse hook: name the reviewers an edited file routes to. A failure
                                     exits 2 with its reason, which the harness hands to the session agent
                                     without undoing the edit.

The `reuse` read set is `.sdlc/read_set.py`'s.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from git_query import GitError, changed_paths, is_ignored, tracked_paths

try:
    import jsonschema
    import yaml
except ImportError as exc:
    _MISSING_PARSER: ImportError | None = exc
else:
    _MISSING_PARSER = None

ROOT = Path(__file__).resolve().parent.parent
SCOPE_PATH = ROOT / ".sdlc" / "scope.yaml"
AGENTS_DIR = Path(".claude") / "agents"
READ_SET_REVIEWER = "reuse"
GIT_DIR_NAME = ".git"
HOOK_FAILURE_EXIT = 2  # PostToolUse exit status whose stderr the harness hands to the session agent

_STRINGS = {"type": "array", "items": {"type": "string", "minLength": 1}}
SCOPE_SCHEMA = {
    "type": "object",
    "required": ["version", "rules", "reviewers", "criticality_vocabulary"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": 1},
        "required_reviewers": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {**_STRINGS, "minItems": 1}},
        },
        "criticality_vocabulary": {"type": "object", "additionalProperties": {"type": "string"}},
        "reviewers": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
        "rules": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["part", "paths", "reviewers", "criticality"],
                "additionalProperties": False,
                "properties": {
                    "part": {"type": "string", "minLength": 1},
                    "paths": {**_STRINGS, "minItems": 1},
                    "reviewers": _STRINGS,
                    "criticality": {**_STRINGS, "minItems": 1},
                    "reuse_excludes": _STRINGS,
                    "note": {"type": "string"},
                },
            },
        },
    },
}


class ScopeError(ValueError):

    """`scope.yaml` cannot be read into the shape this module uses."""


def _any_match(patterns: tuple[str, ...], path: str) -> bool:
    """Return whether `path` matches any pattern; `*` also matches `/`, so `dir/**` is everything under `dir/`."""
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


@dataclass(frozen=True, slots=True)
class Rule:

    """One routing rule: the paths of a part, its criticality tags and its reviewers."""

    part: str
    paths: tuple[str, ...]
    reviewers: tuple[str, ...]
    criticality: tuple[str, ...]
    reuse_excludes: tuple[str, ...]

    def claims(self, path: str) -> bool:
        return _any_match(self.paths, path)

    def excludes_from_reuse(self, path: str) -> bool:
        return _any_match(self.reuse_excludes, path)


@dataclass(frozen=True, slots=True)
class Scope:

    """The parsed `scope.yaml`."""

    rules: tuple[Rule, ...]
    vocabulary: frozenset[str]
    agents: dict[str, str]
    required: dict[str, tuple[tuple[str, ...], ...]]

    @classmethod
    def load(cls, path: Path) -> Scope:
        """
        Parse `path` and check it against SCOPE_SCHEMA; an unreadable or ill-shaped file raises `ScopeError`.

        References between entries — tags, reviewers, agent files — are `violations()`'s to check.
        """
        if _MISSING_PARSER is not None:
            msg = f"reading {path} needs PyYAML and jsonschema on {sys.executable}: {_MISSING_PARSER}"
            raise ScopeError(msg)
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            msg = f"{path}: {exc}"
            raise ScopeError(msg) from exc
        error = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(SCOPE_SCHEMA).iter_errors(spec))
        if error is not None:
            msg = f"{path}: {error.json_path}: {error.message}"
            raise ScopeError(msg)
        return cls(
            rules=tuple(
                Rule(
                    part=entry["part"],
                    paths=tuple(entry["paths"]),
                    reviewers=tuple(entry["reviewers"]),
                    criticality=tuple(entry["criticality"]),
                    reuse_excludes=tuple(entry.get("reuse_excludes", ())),
                )
                for entry in spec["rules"]
            ),
            vocabulary=frozenset(spec["criticality_vocabulary"]),
            agents=dict(spec["reviewers"]),
            required={
                tag: tuple(tuple(any_of) for any_of in groups)
                for tag, groups in spec.get("required_reviewers", {}).items()
            },
        )

    def route(self, path: str) -> Rule | None:
        """Return the first rule claiming `path`, in declaration order."""
        return next((rule for rule in self.rules if rule.claims(path)), None)

    def agent(self, reviewer: str) -> str:
        """Return the agent `reviewer` names; a reviewer `reviewers` does not declare raises `ScopeError`."""
        try:
            return self.agents[reviewer]
        except KeyError:
            msg = f"reviewer `{reviewer}` is not declared under `reviewers`; run python3 .sdlc/route.py --check"
            raise ScopeError(msg) from None


def violations(scope: Scope, tracked: list[str], root: Path) -> list[str]:
    found = [f"{path}: matches no rule in scope.yaml" for path in tracked if scope.route(path) is None]
    for rule in scope.rules:
        for tag in rule.criticality:
            if tag not in scope.vocabulary:
                found.append(f"{rule.part}: tag `{tag}` is not in criticality_vocabulary")
            found.extend(
                f"{rule.part}: tagged `{tag}` but routes to none of {', '.join(any_of)}"
                for any_of in scope.required.get(tag, ())
                if not set(any_of) & set(rule.reviewers)
            )
        found.extend(
            f"{rule.part}: reviewer `{name}` is not declared under `reviewers`"
            for name in rule.reviewers
            if name not in scope.agents
        )
    if READ_SET_REVIEWER not in scope.agents:
        found.append(f"reviewer `{READ_SET_REVIEWER}`, whose read set .sdlc/read_set.py derives, is not declared")
    found.extend(
        f"reviewer `{name}`: agent file {AGENTS_DIR / f'{agent}.md'} does not exist"
        for name, agent in scope.agents.items()
        if not (root / AGENTS_DIR / f"{agent}.md").is_file()
    )
    return found


@dataclass(frozen=True, slots=True)
class Routing:

    """One change's paths grouped by the rule that claims them."""

    by_part: dict[str, tuple[Rule, tuple[str, ...]]]
    unrouted: tuple[str, ...]

    @classmethod
    def of(cls, scope: Scope, paths: list[str]) -> Routing:
        grouped: dict[str, tuple[Rule, list[str]]] = {}
        unrouted = []
        for path in sorted(paths):
            rule = scope.route(path)
            if rule is None:
                unrouted.append(path)
            else:
                grouped.setdefault(rule.part, (rule, []))[1].append(path)
        return cls(
            by_part={part: (rule, tuple(found)) for part, (rule, found) in sorted(grouped.items())},
            unrouted=tuple(unrouted),
        )

    @property
    def criticality(self) -> list[str]:
        return sorted({tag for rule, _ in self.by_part.values() for tag in rule.criticality})

    def paths_by_reviewer(self) -> dict[str, list[str]]:
        """Return each routed reviewer's paths: what it judges; the rest of the diff is its context."""
        result: dict[str, list[str]] = {}
        for rule, paths in self.by_part.values():
            for name in rule.reviewers:
                result.setdefault(name, []).extend(paths)
        return {name: sorted(paths) for name, paths in sorted(result.items())}


def routing_report(scope: Scope, routing: Routing) -> str:
    if not routing.by_part and not routing.unrouted:
        return "No changes."
    lines = ["Changed, by part:"]
    for part, (rule, paths) in routing.by_part.items():
        lines.append(f"  {part} [{', '.join(rule.criticality)}] -> {', '.join(rule.reviewers) or 'no reviewer'}")
        lines.extend(f"    {path}" for path in paths)
    if routing.unrouted:
        lines.append("Matching no rule (a scope.yaml defect):")
        lines.extend(f"    {path}" for path in routing.unrouted)
    needed = routing.paths_by_reviewer()
    lines += [
        "",
        f"criticality: [{', '.join(routing.criticality)}]",
        "Reviewers: " + (", ".join(f"{name} ({scope.agent(name)})" for name in needed) or "none"),
        "Process: .sdlc/review-process.md",
    ]
    return "\n".join(lines)


def routing_json(scope: Scope, routing: Routing) -> dict:
    """Return the routing as the review brief consumes it."""
    return {
        "criticality": routing.criticality,
        "reviewers": {
            name: {"agent": scope.agent(name), "paths": paths} for name, paths in routing.paths_by_reviewer().items()
        },
        "unrouted": list(routing.unrouted),
    }


def hook_context(scope: Scope, root: Path, file_path: str) -> dict | None:
    """
    Return the PostToolUse output for the edited `file_path`, or None when there is nothing to say.

    A file outside the repository, inside a `.git` directory, ignored by git, or in a part with no reviewers
    has nothing to say. A file no rule claims is reported as the `scope.yaml` defect it is, never as a file
    without reviewers.
    """
    try:
        inside = PurePosixPath(Path(file_path).resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        return None
    if GIT_DIR_NAME in inside.parts or is_ignored(root, inside):
        return None
    relative = inside.as_posix()
    rule = scope.route(relative)
    if rule is None:
        text = (
            f"{relative} matches no rule in .sdlc/scope.yaml, so no reviewer is routed to it. "
            "Add it to the part that owns it, then run python3 .sdlc/route.py --check."
        )
    elif not rule.reviewers:
        return None
    else:
        text = (
            f"{relative} routes to part `{rule.part}` [{', '.join(rule.criticality)}]. "
            "Before this change is ready to commit, run the review in .sdlc/review-process.md. "
            f"Reviewers for this part: {', '.join(scope.agent(name) for name in rule.reviewers)}."
        )
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def _hook_failure(reason: object) -> int:
    sys.stderr.write(f"review routing could not route this edit: {reason}\n")
    return HOOK_FAILURE_EXIT


def _run_hook() -> int:
    try:
        payload = json.load(sys.stdin)
        scope = Scope.load(SCOPE_PATH)
    except ValueError as exc:
        return _hook_failure(exc)
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path:
        return _hook_failure("the hook payload has no tool_input object with a non-empty string file_path")
    try:
        context = hook_context(scope, ROOT, file_path)
    except (GitError, ScopeError) as exc:
        return _hook_failure(exc)
    if context is not None:
        sys.stdout.write(json.dumps(context) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--hook", action="store_true")
    mode.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.hook:
        return _run_hook()
    try:
        scope = Scope.load(SCOPE_PATH)
        if args.check:
            found = violations(scope, tracked_paths(ROOT), ROOT)
            sys.stdout.write("".join(f"{line}\n" for line in found) or "scope.yaml: every tracked file routes.\n")
            return 1 if found else 0
        routing = Routing.of(scope, changed_paths(ROOT))
        output = json.dumps(routing_json(scope, routing), indent=2) if args.json else routing_report(scope, routing)
    except (GitError, ScopeError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
