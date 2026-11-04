"""
A throwaway git repository the `.sdlc` test suites run the router and the read set against.

The repository is built with git directly, never through `git_query.run_git`, so a defect in the code under test
cannot also shape the fixture its results are compared against.
"""

import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import route

SDLC = Path(__file__).parent
ROUTER = SDLC / "route.py"
SCRIPT_MODULES = (ROUTER, SDLC / "git_query.py")
GIT = shutil.which("git")

SCOPE = textwrap.dedent("""\
    version: 1
    required_reviewers:
      tenant-isolation:
        - [api]
        - [security]
      sensitive:
        - [security, ui-security]
    criticality_vocabulary:
      tenant-isolation: org boundary
      sensitive: secrets
      support: tooling
    reviewers:
      api: aist-api-reviewer
      security: aist-security-checker
      code: aist-code-reviewer
      reuse: aist-reuse-reviewer
    rules:
      - part: endpoints
        criticality: [tenant-isolation]
        reviewers: [api, security, code, reuse]
        paths:
          - app/api/**
      - part: app
        criticality: [sensitive]
        reviewers: [security, code, reuse]
        reuse_excludes:
          - app/generated/**
        paths:
          - app/**
      - part: vendored
        criticality: [support]
        reviewers: [reuse]
        reuse_excludes:
          - vendor/**
        paths:
          - vendor/**
      - part: meta
        criticality: [support]
        reviewers: []
        paths:
          - .sdlc/**
          - .claude/**
          - .gitattributes
          - .gitignore
          - README.md
""")


class RouterRepositoryTestCase(unittest.TestCase):

    """A committed repository holding `SCOPE`, the agent files it names, and a few source files."""

    def setUp(self):
        if GIT is None:
            self.fail("git is not on PATH")
        self.root = self.new_repository()
        self.scope = self.scope_with()
        for agent in ("aist-api-reviewer", "aist-security-checker", "aist-code-reviewer", "aist-reuse-reviewer"):
            self.write(f".claude/agents/{agent}.md", f"---\nname: {agent}\n---\n")
        self.write("app/core.py", "def run():\n    return 1\n")
        self.write("app/api/views.py", "def view():\n    return 2\n")
        self.write("README.md", "readme\n")
        self.commit()

    def new_repository(self, root=None):
        """Create a repository with the fixture's committer at `root`, or in a directory removed after the test."""
        if root is None:
            root = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, root)
        subprocess.run([GIT, "init", "-q", str(root)], check=True, capture_output=True)
        self.git("config", "user.email", "reviewer@example.test", repository=root)
        self.git("config", "user.name", "Reviewer", repository=root)
        return root

    def git(self, *args, repository=None):
        subprocess.run([GIT, "-C", str(repository or self.root), *args], check=True, capture_output=True)

    def commit(self, repository=None):
        self.git("add", "-A", repository=repository)
        self.git("commit", "-q", "-m", "fixture", repository=repository)

    def write(self, relative, text, repository=None):
        path = (repository or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def scope_with(self, *replacements):
        """Write `SCOPE` with each `(old, new)` replacement applied, and load it; every `old` must be present."""
        text = SCOPE
        for old, new in replacements:
            self.assertIn(old, text, "the replacement would not change the fixture scope")
            text = text.replace(old, new)
        return route.Scope.load(self.write(route.SCOPE_PATH.relative_to(route.ROOT), text))

    def run_script(self, script, *args, stdin="", preamble=""):
        """Run a copy of `.sdlc/<script>`, beside copies of the modules it imports, so its derived root is the fixture."""
        scripts = self.root / ".sdlc"
        for module in SCRIPT_MODULES:
            shutil.copy(module, scripts / module.name)
        copy = scripts / script.name
        shutil.copy(script, copy)
        runner = (
            f"{preamble}import runpy, sys; sys.path.insert(0, {str(scripts)!r}); "
            f"sys.argv = {[str(copy), *args]!r}; runpy.run_path({str(copy)!r}, run_name='__main__')"
        )
        return subprocess.run(
            [sys.executable, "-c", runner],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            cwd=self.root,
        )
