"""Skill upkeep proposals for mach memory review (hermetic)."""
import os
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.dont_write_bytecode = True

import mach_skills as msk  # noqa: E402


class SkillUpkeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.mf = root / "mf"
        self.skills = self.mf / "ai-authored" / "skills"
        self.skills.mkdir(parents=True)
        self.env = {"HOME": str(root / "home"), "MACH_MF_ROOT": str(self.mf), "MACH_NOW": "2026-10-08 09:30:00"}
        (root / "home").mkdir()
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _skill(self, name, mtime_days_ago=0):
        d = self.skills / name
        d.mkdir()
        p = d / "SKILL.md"
        p.write_text("---\nname: %s\ndescription: x\n---\nbody\n" % name)
        if mtime_days_ago:
            ts = time.time() - mtime_days_ago * 86400
            os.utime(p, (ts, ts))
        return d

    def test_missing_from_index_and_stale(self):
        self._skill("fresh-skill", mtime_days_ago=1)
        self._skill("old-skill", mtime_days_ago=120)
        (self.skills / "INDEX.md").write_text("# Skills\n- fresh-skill\n")
        findings = msk.skill_upkeep_findings(str(self.skills))
        msgs = [m for _lv, m in findings]
        self.assertTrue(any("old-skill" in m and "INDEX.md" in m for m in msgs), msgs)
        self.assertTrue(any("old-skill" in m and "90" in m for m in msgs), msgs)
        self.assertFalse(any("fresh-skill" in m and "INDEX.md" in m for m in msgs), msgs)

    def test_skills_csv_also_checked(self):
        self._skill("listed")
        self._skill("orphan")
        (self.skills / "skills.csv").write_text("id,name\n1,listed\n")
        findings = msk.skill_upkeep_findings(str(self.skills))
        msgs = [m for _lv, m in findings]
        self.assertTrue(any("orphan" in m and "skills.csv" in m for m in msgs), msgs)
        self.assertFalse(any(m.startswith("skill upkeep: listed missing from skills.csv") for m in msgs), msgs)

    def test_absent_skills_dir_is_quiet(self):
        self.assertEqual(msk.skill_upkeep_findings(str(self.mf / "missing")), [])


if __name__ == "__main__":
    unittest.main()
