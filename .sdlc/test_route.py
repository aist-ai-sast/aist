"""
Behaviour of the review router against throwaway git repositories.

Run with `python3 -m unittest discover -s .sdlc -p 'test_*.py'`.
"""

import json
import unittest

import git_query
import route
from fixture_repository import ROUTER, RouterRepositoryTestCase


class ChangedPathRoutingTest(RouterRepositoryTestCase):

    def test_a_new_file_in_a_new_directory_reaches_the_reviewers_of_its_part(self):
        self.write("app/api/tokens/rotate.py", "def rotate():\n    return 3\n")

        changed = git_query.changed_paths(self.root)
        report = route.routing_report(self.scope, route.Routing.of(self.scope, changed))

        self.assertEqual(changed, ["app/api/tokens/rotate.py"])
        self.assertIn("endpoints [tenant-isolation] -> api, security, code, reuse", report)
        self.assertIn("api (aist-api-reviewer)", report)

    def test_each_reviewer_is_briefed_with_only_the_paths_routed_to_it(self):
        self.write("app/api/views.py", "def view():\n    return 20\n")
        self.write("app/core.py", "def run():\n    return 10\n")
        self.write("README.md", "changed readme\n")

        brief = route.routing_json(self.scope, route.Routing.of(self.scope, git_query.changed_paths(self.root)))

        self.assertEqual(brief["criticality"], ["sensitive", "support", "tenant-isolation"])
        self.assertEqual(
            {name: entry["paths"] for name, entry in brief["reviewers"].items()},
            {
                "api": ["app/api/views.py"],
                "code": ["app/api/views.py", "app/core.py"],
                "reuse": ["app/api/views.py", "app/core.py"],
                "security": ["app/api/views.py", "app/core.py"],
            },
        )
        self.assertEqual(brief["unrouted"], [])

    def test_a_renamed_file_routes_by_its_new_path(self):
        self.git("mv", "app/core.py", "app/api/core.py")

        self.assertEqual(git_query.changed_paths(self.root), ["app/api/core.py"])


class ScopeShapeTest(RouterRepositoryTestCase):

    def test_a_list_written_as_a_scalar_is_rejected_instead_of_matching_every_path(self):
        with self.assertRaises(route.ScopeError) as raised:
            self.scope_with(("    paths:\n      - app/api/**", "    paths: app/api/**"))

        self.assertIn("$.rules[0].paths", str(raised.exception))

    def test_every_file_inside_a_submodule_routes_to_the_security_reviewer(self):
        scope = route.Scope.load(route.SCOPE_PATH)

        for path in (
            "sast-combinator/context_extractor_service/ansible/files/mcp_server.py",
            "sast-combinator/sast-pipeline/pipeline/project_builder.py",
            "vendor/defectdojo/dojo/models.py",
        ):
            with self.subTest(path=path):
                rule = scope.route(path)
                self.assertIsNotNone(rule, f"{path} matches no rule in scope.yaml")
                self.assertIn("security", rule.reviewers)


class CoverageCheckTest(RouterRepositoryTestCase):

    def violations(self, scope=None):
        return route.violations(scope or self.scope, git_query.tracked_paths(self.root), self.root)

    def test_a_clean_scope_reports_nothing(self):
        self.assertEqual(self.violations(), [])

    def test_a_tracked_file_no_rule_claims_is_reported(self):
        self.write("deploy/compose.yml", "services: {}\n")
        self.git("add", "-A")

        self.assertEqual(self.violations(), ["deploy/compose.yml: matches no rule in scope.yaml"])

    def test_a_part_whose_tags_require_a_reviewer_it_lacks_is_reported(self):
        scope = self.scope_with(("reviewers: [api, security, code, reuse]", "reviewers: [code]"))

        self.assertEqual(
            self.violations(scope),
            [
                "endpoints: tagged `tenant-isolation` but routes to none of api",
                "endpoints: tagged `tenant-isolation` but routes to none of security",
            ],
        )

    def test_a_tag_outside_the_vocabulary_is_reported(self):
        scope = self.scope_with(("criticality: [sensitive]", "criticality: [sensitive, urgent]"))

        self.assertEqual(self.violations(scope), ["app: tag `urgent` is not in criticality_vocabulary"])

    def test_a_reviewer_no_agent_is_declared_for_is_reported(self):
        scope = self.scope_with(("reviewers: [security, code, reuse]", "reviewers: [security, code, reuse, style]"))

        self.assertEqual(self.violations(scope), ["app: reviewer `style` is not declared under `reviewers`"])

    def test_renaming_the_read_set_reviewer_everywhere_is_still_reported(self):
        scope = self.scope_with(
            ("  reuse: aist-reuse-reviewer\n", "  duplication: aist-reuse-reviewer\n"),
            ("reuse]", "duplication]"),
        )

        self.assertEqual(
            self.violations(scope),
            ["reviewer `reuse`, whose read set .sdlc/read_set.py derives, is not declared"],
        )

    def test_a_reviewer_whose_agent_file_is_missing_is_reported(self):
        (self.root / ".claude" / "agents" / "aist-reuse-reviewer.md").unlink()

        self.assertEqual(
            self.violations(),
            ["reviewer `reuse`: agent file .claude/agents/aist-reuse-reviewer.md does not exist"],
        )

    def test_the_check_command_exits_1_on_a_violation_and_0_once_it_is_fixed(self):
        self.write("deploy/compose.yml", "services: {}\n")
        self.git("add", "-A")

        failing = self.run_script(ROUTER, "--check")
        self.git("rm", "-q", "--cached", "deploy/compose.yml")
        passing = self.run_script(ROUTER, "--check")

        self.assertEqual(failing.returncode, 1)
        self.assertIn("deploy/compose.yml: matches no rule in scope.yaml", failing.stdout)
        self.assertEqual(passing.returncode, 0, passing.stdout)


class HookTest(RouterRepositoryTestCase):

    def hook(self, payload, preamble=""):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return self.run_script(ROUTER, "--hook", stdin=stdin, preamble=preamble)

    def edit(self, relative):
        return {"tool_input": {"file_path": str(self.root / relative)}}

    def test_an_edited_routed_file_names_its_reviewers(self):
        result = self.hook(self.edit("app/api/views.py"))

        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(result.returncode, 0)
        self.assertIn("app/api/views.py routes to part `endpoints`", context)
        self.assertIn("aist-api-reviewer, aist-security-checker, aist-code-reviewer, aist-reuse-reviewer", context)

    def test_an_edited_file_whose_part_has_no_reviewer_produces_no_output(self):
        result = self.hook(self.edit("README.md"))

        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_an_edited_file_no_rule_claims_is_reported_as_a_scope_defect(self):
        self.write("deploy/compose.yml", "services: {}\n")

        result = self.hook(self.edit("deploy/compose.yml"))

        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(result.returncode, 0)
        self.assertIn("deploy/compose.yml matches no rule in .sdlc/scope.yaml", context)

    def test_an_edited_file_git_ignores_produces_no_output(self):
        self.write(".gitignore", "local.env\n")
        self.commit()
        self.write("local.env", "TOKEN=placeholder\n")

        result = self.hook(self.edit("local.env"))

        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_an_edited_file_inside_the_git_directory_produces_no_output(self):
        result = self.hook(self.edit(".git/info/exclude"))

        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_an_edited_file_its_submodule_ignores_produces_no_output(self):
        library = self.new_repository()
        self.write(".gitignore", "secret.env\n", repository=library)
        self.commit(repository=library)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "library")
        self.write("library/secret.env", "TOKEN=placeholder\n")

        result = self.hook(self.edit("library/secret.env"))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_a_repository_planted_beside_an_edited_file_is_not_asked_whether_it_ignores_the_file(self):
        planted = self.new_repository(self.root / "app" / "planted")
        self.write(".git/info/exclude", "notes.py\n", repository=planted)
        self.write("app/planted/notes.py", "x = 1\n")

        result = self.hook(self.edit("app/planted/notes.py"))

        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("app/planted/notes.py routes to part `app`", context)

    def test_a_submodule_the_hook_asks_runs_none_of_its_configured_commands(self):
        library = self.new_repository()
        self.write("lib.py", "x = 1\n", repository=library)
        self.commit(repository=library)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "library")
        marker = self.root / "fsmonitor-ran"
        monitor = self.write("monitor.sh", f"#!/bin/sh\ntouch {marker}\n")
        monitor.chmod(0o755)
        self.git("config", "core.fsmonitor", str(monitor), repository=self.root / "library")

        result = self.hook(self.edit("library/lib.py"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists(), "the hook ran the submodule's fsmonitor command")

    def test_a_reviewer_the_scope_does_not_declare_fails_the_hook_naming_it(self):
        self.scope_with(("reviewers: [security, code, reuse]", "reviewers: [security, code, reuse, style]"))

        result = self.hook(self.edit("app/core.py"))

        self.assertEqual((result.returncode, result.stdout), (route.HOOK_FAILURE_EXIT, ""))
        self.assertIn("reviewer `style` is not declared", result.stderr)

    def test_a_git_failure_fails_the_hook_with_gits_reason(self):
        self.write(".git/index", "not an index\n")

        result = self.hook(self.edit("app/core.py"))

        self.assertEqual((result.returncode, result.stdout), (route.HOOK_FAILURE_EXIT, ""))
        self.assertIn("git ls-files --stage -z: ", result.stderr)
        self.assertIn("index file smaller than expected", result.stderr)

    def test_an_unreadable_scope_fails_the_hook_with_its_reason(self):
        self.write(route.SCOPE_PATH.relative_to(route.ROOT), "rules: [unclosed\n")

        result = self.hook(self.edit("app/core.py"))

        self.assertEqual((result.returncode, result.stdout), (route.HOOK_FAILURE_EXIT, ""))
        self.assertIn("review routing could not route this edit", result.stderr)
        self.assertIn("scope.yaml", result.stderr)

    def test_a_missing_parser_fails_the_hook_naming_what_to_install(self):
        result = self.hook(self.edit("app/core.py"), preamble="import sys; sys.modules['yaml'] = None; ")

        self.assertEqual((result.returncode, result.stdout), (route.HOOK_FAILURE_EXIT, ""))
        self.assertIn("needs PyYAML and jsonschema", result.stderr)

    def test_a_payload_without_an_edited_file_path_fails_the_hook(self):
        for payload in ([], {"tool_input": "app/core.py"}, {"tool_input": {}}, {"tool_input": {"file_path": ""}}):
            with self.subTest(payload=payload):
                result = self.hook(payload)

                self.assertEqual(result.returncode, route.HOOK_FAILURE_EXIT)
                self.assertIn("no tool_input object with a non-empty string file_path", result.stderr)


if __name__ == "__main__":
    unittest.main()
