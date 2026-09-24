"""Bite tests for scripts/check-entitlements.py.

Every case runs the guard as a subprocess against doctored copies in a temp
directory, through its path arguments — the real entitlements are never
written. The one no-argument case reads the real tree and must stay green.
"""

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, 'scripts', 'check-entitlements.py')


def run(*paths: str, cwd: str = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SCRIPT, *paths], capture_output=True, text=True, cwd=cwd
    )


def write(tmpdir: str, name: str, mapping: dict) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, 'wb') as handle:
        plistlib.dump(mapping, handle)
    return path


class CheckEntitlementsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def pair(self, debug: dict, release: dict) -> tuple[str, str]:
        return (
            write(self.tmp, 'Debug.entitlements', debug),
            write(self.tmp, 'Release.entitlements', release),
        )

    def assertPasses(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')

    def assertFails(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('::error::entitlements:', result.stdout)

    def test_both_empty_passes_silently(self) -> None:
        self.assertPasses(run(*self.pair({}, {})))

    def test_both_declare_push_with_their_own_environment_passes(self) -> None:
        self.assertPasses(
            run(*self.pair({'aps-environment': 'development'}, {'aps-environment': 'production'}))
        )

    def test_matching_capabilities_without_push_pass(self) -> None:
        caps = {'com.apple.security.application-groups': ['group.example']}
        self.assertPasses(run(*self.pair(caps, dict(caps))))

    def test_only_debug_declares_push_fails_naming_release(self) -> None:
        debug, release = self.pair({'aps-environment': 'development'}, {})
        result = run(debug, release)
        self.assertFails(result)
        errors = [line for line in result.stdout.splitlines() if line.startswith('::error::')]
        self.assertEqual(len(errors), 1, result.stdout)
        self.assertIn(release, errors[0])

    def test_only_release_declares_push_fails_naming_debug(self) -> None:
        debug, release = self.pair({}, {'aps-environment': 'production'})
        result = run(debug, release)
        self.assertFails(result)
        self.assertIn(debug, result.stdout)
        self.assertNotIn(release, result.stdout)

    def test_debug_claiming_production_fails(self) -> None:
        debug, release = self.pair(
            {'aps-environment': 'production'}, {'aps-environment': 'production'}
        )
        result = run(debug, release)
        self.assertFails(result)
        self.assertIn(debug, result.stdout)
        self.assertIn("expected 'development'", result.stdout)

    def test_both_development_fails_naming_release(self) -> None:
        debug, release = self.pair(
            {'aps-environment': 'development'}, {'aps-environment': 'development'}
        )
        result = run(debug, release)
        self.assertFails(result)
        self.assertIn(release, result.stdout)
        self.assertIn("expected 'production'", result.stdout)

    def test_capability_only_in_debug_fails_as_different_keys(self) -> None:
        result = run(*self.pair({'com.apple.developer.healthkit': True}, {}))
        self.assertFails(result)
        self.assertIn('different keys', result.stdout)
        self.assertIn('com.apple.developer.healthkit', result.stdout)

    def test_capability_only_in_release_fails_as_different_keys(self) -> None:
        result = run(*self.pair({}, {'com.apple.developer.healthkit': True}))
        self.assertFails(result)
        self.assertIn('different keys', result.stdout)

    def test_shared_key_with_different_values_fails(self) -> None:
        result = run(
            *self.pair(
                {'com.apple.security.application-groups': ['group.a']},
                {'com.apple.security.application-groups': ['group.b']},
            )
        )
        self.assertFails(result)
        self.assertIn('com.apple.security.application-groups differs', result.stdout)

    def test_one_path_is_refused(self) -> None:
        debug, _ = self.pair({}, {})
        result = run(debug)
        self.assertFails(result)
        self.assertIn('pass exactly two entitlements paths, or none', result.stdout)

    def test_three_paths_are_refused(self) -> None:
        debug, release = self.pair({}, {})
        result = run(debug, release, debug)
        self.assertFails(result)
        self.assertIn('pass exactly two entitlements paths, or none', result.stdout)

    def test_no_arguments_checks_the_real_tree_and_passes(self) -> None:
        self.assertPasses(run())


if __name__ == '__main__':
    unittest.main()
