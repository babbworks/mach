"""Bindings keyed by stable system IDs: loading, helpers and lint. Run under both TOML parsers."""
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import mach_bindings as mb
import mach_subsystem as ss

MANIFEST = (ROOT / 'config/subsystem.example.toml').read_text()
GOOD = '''
schema = 1
assembly_remote = "example-org/assembly-system"

[assembly_root]
linux-box = "~/example/assembly-system"

[repos.R-0001]
legacy = "example"
[repos.R-0001.paths]
linux-box = "~/example/checkout"

[repos.R-0002]
legacy = "other"
[repos.R-0002.paths]

[units.U-0001]
legacy = "alpha"
[units.U-0001.roles]
lead = "grokbot:Example"
since = "2026-10-08"
changed = []
assignments = []
drafting = "claude"
review = "user"
notes = "The user reviews before anything ships."

[units.U-0002]
legacy = "beta"
[units.U-0002.roles]
lead = "user"
since = "2026-10-08"
changed = []
assignments = []
research = "codex"

[units.U-0003]
legacy = "gamma"
todo = "choose a lead"
[units.U-0003.roles]
lead = "user"
since = "2026-10-08"
changed = []
assignments = []
'''


def parse(text, minitoml):
    old = os.environ.get('MACH_FORCE_MINITOML')
    if minitoml:
        os.environ['MACH_FORCE_MINITOML'] = '1'
    else:
        os.environ.pop('MACH_FORCE_MINITOML', None)
    try:
        return ss.parse(text)
    finally:
        if old is None:
            os.environ.pop('MACH_FORCE_MINITOML', None)
        else:
            os.environ['MACH_FORCE_MINITOML'] = old


class BindingsTests(unittest.TestCase):
    def setUp(self):
        self.manifest = parse(MANIFEST, False)

    def lint(self, text, minitoml=False, manifest=None):
        return mb.lint(parse(text, minitoml), manifest or self.manifest)

    def levels(self, results):
        return sorted(set(level for level, _ in results))

    def test_good_file_lints_clean_under_both_parsers(self):
        for minitoml in (False, True):
            self.assertEqual(self.lint(GOOD, minitoml), [], 'minitoml=%s' % minitoml)

    def test_load_missing_file_is_none_and_present_file_parses(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ['MACH_SUBSYSTEMS_DIR'] = d
            try:
                self.assertIsNone(mb.load('demo'))
                os.makedirs(os.path.join(d, 'demo'))
                with open(os.path.join(d, 'demo', 'bindings.toml'), 'w') as fh:
                    fh.write(GOOD)
                self.assertEqual(mb.load('demo')['units']['U-0001']['legacy'], 'alpha')
                with open(os.path.join(d, 'demo', 'bindings.toml'), 'w') as fh:
                    fh.write('schema = = 1')
                with self.assertRaises(ValueError):
                    mb.load('demo')
            finally:
                os.environ.pop('MACH_SUBSYSTEMS_DIR', None)

    def test_helpers_map_between_ids_and_legacy_keys(self):
        b = parse(GOOD, False)
        self.assertEqual(mb.unit_legacy(b, 'U-0002'), 'beta')
        self.assertIsNone(mb.unit_legacy(b, 'U-0099'))
        self.assertEqual(mb.unit_id(b, 'alpha'), 'U-0001')
        self.assertEqual(mb.repo_id(b, 'example'), 'R-0001')
        self.assertIsNone(mb.repo_id(b, 'nothing'))
        self.assertEqual(mb.repo_path(b, 'R-0001', 'linux-box'), '~/example/checkout')
        self.assertIsNone(mb.repo_path(b, 'R-0001', 'mac-laptop'))
        self.assertIsNone(mb.repo_path(b, 'R-0002', 'linux-box'))
        self.assertTrue(mb.is_unit_id('U-0021'))
        for bad in ('alpha', 'u-0021', 'U-21', 'R-0001', 'U-0021/x'):
            self.assertFalse(mb.is_unit_id(bad), bad)

    def test_company_facts_in_a_unit_entry_fail(self):
        for key, val in (('folder', '"alpha"'), ('name', '"Alpha"'), ('kind', '"pillar"'), ('repo', '"example"'), ('parent', '"x"'), ('verified', 'true')):
            text = GOOD.replace('legacy = "alpha"\n', 'legacy = "alpha"\n%s = %s\n' % (key, val))
            res = self.lint(text)
            self.assertTrue(any(l == 'FAIL' and key in m and 'Assembly System' in m for l, m in res), (key, res))

    def test_unknown_keys_fail(self):
        res = self.lint(GOOD.replace('legacy = "alpha"\n', 'legacy = "alpha"\ncolour = "red"\n'))
        self.assertTrue(any(l == 'FAIL' and 'colour' in m for l, m in res), res)

    def test_legacy_must_exist_in_the_manifest_and_be_unique(self):
        res = self.lint(GOOD.replace('legacy = "beta"', 'legacy = "nope"'))
        self.assertTrue(any(l == 'FAIL' and 'nope' in m for l, m in res), res)
        res = self.lint(GOOD.replace('legacy = "beta"', 'legacy = "alpha"'))
        self.assertTrue(any(l == 'FAIL' and 'alpha' in m and 'twice' in m for l, m in res), res)

    def test_bad_ids_schema_and_tables_fail(self):
        self.assertTrue(any(l == 'FAIL' and 'schema' in m for l, m in self.lint(GOOD.replace('schema = 1', 'schema = 2'))))
        self.assertTrue(any(l == 'FAIL' and 'U-1' in m for l, m in self.lint(GOOD.replace('U-0003', 'U-1'))))
        self.assertTrue(any(l == 'FAIL' and 'R-9' in m for l, m in self.lint(GOOD.replace('R-0002', 'R-9'))))
        res = self.lint(GOOD.replace('linux-box = "~/example/checkout"', 'linux-box = 5'))
        self.assertTrue(any(l == 'FAIL' and 'R-0001' in m for l, m in res), res)

    def test_roles_are_checked_like_the_manifest(self):
        res = self.lint(GOOD.replace('review = "user"', 'review = "mystery"'))
        self.assertTrue(any(l == 'FAIL' and 'U-0001' in m and 'review' in m for l, m in res), res)
        res = self.lint(GOOD.replace('lead = "grokbot:Example"', 'lead = "grokbot:Nobody"'))
        self.assertTrue(any(l == 'FAIL' and 'service profile' in m for l, m in res), res)

    def test_drift_from_the_manifest_is_a_warning_not_a_failure(self):
        res = self.lint(GOOD.replace('research = "codex"', 'research = "claude"'))
        self.assertEqual(self.levels(res), ['WARN'])
        self.assertTrue(any('U-0002' in m and 'roles' in m for _, m in res), res)
        missing = GOOD.split('[units.U-0003]')[0]
        res = self.lint(missing)
        self.assertEqual(self.levels(res), ['WARN'])
        self.assertTrue(any('gamma' in m and 'no bindings entry' in m for _, m in res), res)


if __name__ == '__main__':
    unittest.main()
