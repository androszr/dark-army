"""Bite tests for scripts/check-keychain-group.py.

Every case runs the guard as a subprocess against text files in a temp
directory, through its path arguments — the real config files are never
written. The one no-argument case reads the real tree and must stay green.
"""

import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, 'scripts', 'check-keychain-group.py')
TOKEN = '$(KEYCHAIN_ACCESS_GROUP)'


def run(*paths: str, cwd: str = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SCRIPT, *paths], capture_output=True, text=True, cwd=cwd
    )


class CheckKeychainGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def files(self, *references: int) -> list[str]:
        """One file per argument, each holding that many references."""
        paths = []
        for index, n in enumerate(references):
            path = os.path.join(self.tmp, f'config-{index}.plist')
            body = '<plist><dict>' + ''.join(
                f'<key>k{i}</key><string>{TOKEN}</string>' for i in range(n)
            ) + '</dict></plist>\n'
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(body)
            paths.append(path)
        return paths

    def errors(self, result: subprocess.CompletedProcess) -> list[str]:
        return [line for line in result.stdout.splitlines() if line.startswith('::error::keychain-group:')]

    def test_no_file_references_the_group_passes_silently(self) -> None:
        result = run(*self.files(0, 0, 0, 0))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_every_file_references_it_once_passes_silently(self) -> None:
        result = run(*self.files(1, 1, 1, 1))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_one_referencing_three_not_fails_naming_the_three(self) -> None:
        paths = self.files(1, 0, 0, 0)
        result = run(*paths)
        self.assertEqual(result.returncode, 1)
        errors = self.errors(result)
        self.assertEqual(len(errors), 3, result.stdout)
        for path in paths[1:]:
            self.assertIn(path, result.stdout)
        self.assertNotIn(paths[0], result.stdout)

    def test_a_duplicate_reference_fails_naming_only_that_file(self) -> None:
        paths = self.files(1, 2, 1, 1)
        result = run(*paths)
        self.assertEqual(result.returncode, 1)
        errors = self.errors(result)
        self.assertEqual(len(errors), 1, result.stdout)
        self.assertIn(paths[1], errors[0])
        self.assertIn('2 times', errors[0])

    def test_two_referencing_two_not_fails_naming_the_two_without(self) -> None:
        paths = self.files(1, 0, 1, 0)
        result = run(*paths)
        self.assertEqual(result.returncode, 1)
        errors = self.errors(result)
        self.assertEqual(len(errors), 2, result.stdout)
        self.assertIn(paths[1], result.stdout)
        self.assertIn(paths[3], result.stdout)
        self.assertNotIn(paths[0], result.stdout)
        self.assertNotIn(paths[2], result.stdout)

    def test_an_inlined_literal_does_not_count_as_a_reference(self) -> None:
        paths = self.files(1, 1, 1, 0)
        with open(paths[3], 'w', encoding='utf-8') as handle:
            handle.write('<string>ABCDE12345.com.example.shared</string>\n')
        result = run(*paths)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.errors(result)), 1, result.stdout)
        self.assertIn(paths[3], result.stdout)

    def test_no_arguments_checks_the_real_tree_and_passes_silently(self) -> None:
        result = run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
