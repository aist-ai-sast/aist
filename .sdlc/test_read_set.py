"""
Behaviour of the reuse read set against throwaway git repositories.

Run with `python3 -m unittest discover -s .sdlc -p 'test_*.py'`.
"""

import json
import unittest

import read_set
from fixture_repository import SDLC, RouterRepositoryTestCase

READ_SET_SCRIPT = SDLC / "read_set.py"
MODULE = "x = 1\n" * 40  # 240 bytes


class ReuseReadSetTest(RouterRepositoryTestCase):

    def setUp(self):
        super().setUp()
        self.write("app/api/helpers.py", "def helper():\n    return 4\n")
        self.write("app/generated/schema.py", "SCHEMA = {}\n")
        self.write("app/logo.png", "\x89PNG\x00binary")
        self.write("app/narrative.ts", 'const MARKER = "\x00SENT\x00";\n')
        self.write(".gitattributes", "app/narrative.ts diff\n")
        self.write("vendor/lib/util.py", "def util():\n    return 4\n")
        for name in ("alpha", "beta", "gamma"):
            self.write(f"app/modules/{name}.py", MODULE)
        self.write("app/zbig.py", "y = 2\n" * 100)  # 600 bytes, larger than the limit used below
        self.commit()

    def members(self):
        return read_set.members(self.scope, read_set.text_paths(self.root))

    def test_excluded_vendored_and_binary_files_are_left_out_and_a_file_marked_text_is_kept(self):
        self.assertEqual(
            self.members(),
            [
                "app/api/helpers.py",
                "app/api/views.py",
                "app/core.py",
                "app/modules/alpha.py",
                "app/modules/beta.py",
                "app/modules/gamma.py",
                "app/narrative.ts",
                "app/zbig.py",
            ],
        )

    def test_the_changed_files_directory_comes_first_then_files_pack_in_path_order_without_splitting(self):
        code = [path for path in self.members() if path.endswith(".py")]

        cut = read_set.slices(self.root, list(reversed(code)), ["app/api/views.py"], limit=480)

        self.assertEqual(
            [(s.slice_id, s.files, s.size) for s in cut],
            [
                ("app/api/helpers.py", ("app/api/helpers.py", "app/api/views.py"), 27 + 25),
                ("app/core.py", ("app/core.py", "app/modules/alpha.py"), 24 + 240),
                ("app/modules/beta.py", ("app/modules/beta.py", "app/modules/gamma.py"), 240 + 240),
                ("app/zbig.py", ("app/zbig.py",), 600),
            ],
        )

    def test_a_changed_directory_larger_than_the_limit_fills_several_leading_slices(self):
        code = [path for path in self.members() if path.endswith(".py")]

        cut = read_set.slices(self.root, code, ["app/modules/alpha.py"], limit=480)

        self.assertEqual(
            [(s.slice_id, s.files, s.size) for s in cut],
            [
                ("app/modules/alpha.py", ("app/modules/alpha.py", "app/modules/beta.py"), 240 + 240),
                ("app/modules/gamma.py", ("app/modules/gamma.py",), 240),
                ("app/api/helpers.py", ("app/api/helpers.py", "app/api/views.py", "app/core.py"), 27 + 25 + 24),
                ("app/zbig.py", ("app/zbig.py",), 600),
            ],
        )

    def test_the_command_puts_the_edited_files_directory_in_a_slice_of_its_own(self):
        self.write("app/api/views.py", "def view():\n    return 20\n")

        result = self.run_script(READ_SET_SCRIPT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [(entry["id"], entry["files"]) for entry in json.loads(result.stdout)],
            [
                ("app/api/helpers.py", ["app/api/helpers.py", "app/api/views.py"]),
                (
                    "app/core.py",
                    [
                        "app/core.py",
                        "app/modules/alpha.py",
                        "app/modules/beta.py",
                        "app/modules/gamma.py",
                        "app/narrative.ts",
                        "app/zbig.py",
                    ],
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
