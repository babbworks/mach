"""mach publish: library-level tests that need to interleave steps (the shell harness cannot)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import mach_publish as mp  # noqa: E402


class ApplyVerifiesScannedContent(unittest.TestCase):
    def test_file_changed_after_scan_aborts_apply_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            for name, text in (("a.md", "clean\n"), ("b.md", "clean too\n")):
                with open(os.path.join(root, name), "w") as handle:
                    handle.write(text)
            man = mp.parse_manifest("include *.md\n")
            plan = mp.build_plan(root, man, {})
            self.assertEqual(plan["findings"], [])
            with open(os.path.join(root, "b.md"), "w") as handle:   # changes after the scan
                handle.write("ghp_" + "a" * 24 + "\n")
            with self.assertRaises(mp.ApplyError):
                mp.apply_plan(root, out, plan)
            self.assertEqual(os.listdir(out), [], "nothing may be written when any file changed")

    def test_unchanged_files_are_written(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            with open(os.path.join(root, "a.md"), "w") as handle:
                handle.write("clean\n")
            plan = mp.build_plan(root, mp.parse_manifest("include a.md\n"), {})
            mp.apply_plan(root, out, plan)
            self.assertEqual(os.listdir(out), ["a.md"])


class ManifestParsing(unittest.TestCase):
    def test_bad_lines_name_their_line_number(self):
        with self.assertRaisesRegex(mp.ManifestError, "line 2"):
            mp.parse_manifest("include a\nfrobnicate b\n")

    def test_unknown_scan_check_is_an_error_not_silently_ignored(self):
        with self.assertRaisesRegex(mp.ManifestError, "unknown scan check"):
            mp.parse_manifest("scan secrets typos\n")


FEATURES = (
    "feature,paths,tiers,status,note\n"
    "core,lib/;bin/,org;public,ready,\n"
    "guide,docs/GUIDE.md,org;public,needs-edit,held\n"
    "na,config/na/,org,org-only,\n"
    "all-tests,tests/,org;public,ready,\n"
    "na-test,tests/test_na.py,org,org-only,\n"
)


class FeaturesIndex(unittest.TestCase):
    def files(self):
        return set(["lib/a.py", "bin/mach", "docs/GUIDE.md", "config/na/x.json", "tests/test_a.py", "tests/test_na.py", "stray.txt"])

    def chosen(self, tier, features=FEATURES, rules=""):
        man = mp.manifest_from_features(features, tier, rules)
        return sorted(mp.select(man, self.files()))

    def test_only_ready_rows_naming_the_tier_are_exported(self):
        self.assertEqual(self.chosen("public"), ["bin/mach", "lib/a.py", "tests/test_a.py"])

    def test_org_tier_gets_its_own_ready_rows_and_not_held_ones(self):
        self.assertEqual(self.chosen("org"), ["bin/mach", "lib/a.py", "tests/test_a.py"])

    def test_a_held_row_beats_a_broader_ready_row(self):
        self.assertNotIn("tests/test_na.py", self.chosen("public"))
        self.assertNotIn("tests/test_na.py", self.chosen("org"), "org-only rows are not `ready` for tier org here")

    def test_unlisted_and_dead_paths_are_reported(self):
        self.assertEqual(mp.unlisted_files(FEATURES, self.files()), ["stray.txt"])
        self.assertEqual(mp.dead_feature_paths(FEATURES, set(["lib/a.py"])),
                         ["bin/", "config/na/", "docs/GUIDE.md", "tests/", "tests/test_na.py"])

    def test_bad_status_and_missing_column_are_errors(self):
        with self.assertRaisesRegex(mp.ManifestError, "status"):
            mp.manifest_from_features("feature,paths,tiers,status,note\nx,a,org,maybe,\n", "org")
        with self.assertRaisesRegex(mp.ManifestError, "lacks column"):
            mp.manifest_from_features("feature,paths\nx,a\n", "org")

    def test_rules_text_supplies_scan_and_deny_lines(self):
        man = mp.manifest_from_features(FEATURES, "public", "scan names\ndeny secretword\n")
        self.assertIn("names", man.scans)
        self.assertEqual(man.deny, ["secretword"])


class PathContainment(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.root = os.path.join(self.base, "root")
        self.out = os.path.join(self.base, "deep", "out")
        os.makedirs(self.root)
        os.makedirs(self.out)
        with open(os.path.join(self.root, "a.md"), "w") as handle:
            handle.write("clean\n")
        with open(os.path.join(self.base, "victim.txt"), "w") as handle:
            handle.write("precious\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def test_map_destinations_must_stay_inside(self):
        for bad in ("../x.md", "/etc/x", "a/../../x", "a//b", "./x", "..", "back\\slash"):
            with self.assertRaisesRegex(mp.ManifestError, "line 1", msg=bad):
                mp.parse_manifest("map a.md %s\n" % bad)
        mp.parse_manifest("map a.md sub/dir/ok.md\n")

    def test_state_file_cannot_delete_outside_the_export_folder(self):
        plan = mp.build_plan(self.root, mp.parse_manifest("include a.md\n"), {"../../victim.txt": "x", "/abs": "x"})
        self.assertEqual(plan["removed"], [])
        self.assertEqual(plan["ignored_state"], ["../../victim.txt", "/abs"])
        mp.apply_plan(self.root, self.out, plan)
        self.assertTrue(os.path.exists(os.path.join(self.base, "victim.txt")))

    def test_apply_refuses_to_write_through_a_symlink_that_leaves_the_export_folder(self):
        os.symlink(self.base, os.path.join(self.out, "sub"))
        plan = mp.build_plan(self.root, mp.parse_manifest("include a.md\nmap a.md sub/a.md\n"), {})
        with self.assertRaises(mp.ApplyError):
            mp.apply_plan(self.root, self.out, plan)
        self.assertFalse(os.path.exists(os.path.join(self.base, "a.md")))

    def test_a_tampered_plan_destination_is_refused(self):
        plan = mp.build_plan(self.root, mp.parse_manifest("include a.md\n"), {})
        plan["entries"][0]["dest"] = "../../escaped.md"
        with self.assertRaises(mp.ApplyError):
            mp.apply_plan(self.root, self.out, plan)
        self.assertFalse(os.path.exists(os.path.join(self.base, "escaped.md")))


class NoTimeOfCheckTimeOfUseGaps(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.root = os.path.join(self.base, "root")
        self.out = os.path.join(self.base, "out")
        os.makedirs(self.root)
        os.makedirs(self.out)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w") as handle:
            handle.write(text)

    def test_each_source_file_is_opened_once_so_the_scan_and_the_hash_see_the_same_bytes(self):
        self.write("a.md", "clean\n")
        self.write("b.md", "clean too\n")
        real_open = os.open
        opened = []

        def counting_open(path, *args, **kwargs):
            if str(path).startswith(self.root):
                opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("os.open", side_effect=counting_open):
            mp.build_plan(self.root, mp.parse_manifest("include *.md\n"), {})
        self.assertEqual(sorted(opened), sorted([os.path.join(self.root, "a.md"), os.path.join(self.root, "b.md")]))

    def test_the_entry_hash_is_the_hash_of_the_scanned_bytes(self):
        import hashlib
        self.write("a.md", "ghp_" + "a" * 24 + "\n")
        plan = mp.build_plan(self.root, mp.parse_manifest("include a.md\n"), {})
        self.assertEqual(plan["entries"][0]["sha"], hashlib.sha256(("ghp_" + "a" * 24 + "\n").encode()).hexdigest())
        self.assertEqual(plan["findings"][0]["kind"], "github-token")

    def test_a_source_that_became_a_symlink_is_not_read(self):
        outside = os.path.join(self.base, "outside.txt")
        with open(outside, "w") as handle:
            handle.write("not part of the tree\n")
        os.symlink(outside, os.path.join(self.root, "a.md"))
        data, digest, size, reason = mp.read_source(os.path.join(self.root, "a.md"))
        self.assertIsNone(data)
        self.assertEqual(digest, "")
        self.assertIn("unreadable", reason)

    def test_apply_refuses_when_a_source_became_a_symlink_after_the_scan(self):
        outside = os.path.join(self.base, "outside.txt")
        with open(outside, "w") as handle:
            handle.write("not part of the tree\n")
        self.write("a.md", "clean\n")
        plan = mp.build_plan(self.root, mp.parse_manifest("include a.md\n"), {})
        os.remove(os.path.join(self.root, "a.md"))
        os.symlink(outside, os.path.join(self.root, "a.md"))
        with self.assertRaises(mp.ApplyError):
            mp.apply_plan(self.root, self.out, plan)
        self.assertEqual(os.listdir(self.out), [])

    @unittest.skipUnless(mp._have_dir_fd(), "needs dir_fd support")
    def test_a_directory_swapped_for_a_symlink_after_the_check_cannot_redirect_the_write(self):
        self.write("a.md", "clean\n")
        os.makedirs(os.path.join(self.out, "sub"))
        elsewhere = os.path.join(self.base, "elsewhere")
        os.makedirs(elsewhere)
        plan = mp.build_plan(self.root, mp.parse_manifest("include a.md\nmap a.md sub/a.md\n"), {})
        real_check = mp._checked_target
        calls = []

        def check_then_swap(out, dest):
            result = real_check(out, dest)           # the check passes...
            calls.append(dest)
            sub = os.path.join(out, "sub")
            if len(calls) == 2 and os.path.isdir(sub) and not os.path.islink(sub):   # 2nd call = the one right before the write
                os.rmdir(sub)                        # ...then the directory becomes a link to somewhere else
                os.symlink(elsewhere, sub)
            return result

        with mock.patch.object(mp, "_checked_target", side_effect=check_then_swap):
            with self.assertRaises(mp.ApplyError):
                mp.apply_plan(self.root, self.out, plan)
        self.assertEqual(os.listdir(elsewhere), [], "nothing may be written through the swapped link")

    @unittest.skipUnless(mp._have_dir_fd(), "needs dir_fd support")
    def test_stale_removal_does_not_follow_a_swapped_directory(self):
        elsewhere = os.path.join(self.base, "elsewhere")
        os.makedirs(elsewhere)
        victim = os.path.join(elsewhere, "gone.md")
        with open(victim, "w") as handle:
            handle.write("keep me\n")
        os.symlink(elsewhere, os.path.join(self.out, "sub"))
        mp._remove_below(self.out, "sub/gone.md")
        self.assertTrue(os.path.exists(victim))


if __name__ == "__main__":
    unittest.main()
