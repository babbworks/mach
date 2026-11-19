"""Run index.csv append on finish, reindex preview/apply, hermetic HOME/MACH env."""
import csv
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
import mach_common as mc
import mach_run_index as ri
import mach_runs as mr


class RunIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.home = root / "home"
        self.mf = root / "mf"
        self.runs = self.mf / "files" / "runs"
        self.home.mkdir()
        self.runs.mkdir(parents=True)
        self.env = {
            "HOME": str(self.home),
            "MACH_MF_ROOT": str(self.mf),
            "MACH_RUNS_DIR": str(self.runs),
            "MACH_HOST": "linux-box",
            "MACH_NOW": "2026-10-08 09:30:00",
            "TZ": "America/Toronto",
        }
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _brief(self, directory, extra="", body="Body about DEMO-99 work"):
        text = (
            "---\nmach: 1\ntype: task\ntitle: Fix DEMO-12 parser\ngoal: Fix\n"
            "tool: codex\noutput: stdout\nmodel: gpt-test\n"
            + extra
            + "---\n"
            + body
            + "\n"
        )
        pathlib.Path(directory, "input.md").write_text(text)

    def _run(self, rid="20261008-0930-linux-box-fix-demo-12", status="done", **meta):
        d = self.runs / "2026" / "10" / rid
        d.mkdir(parents=True)
        run = mr.Run(str(d))
        run.meta = {
            "id": rid,
            "created": "2026-10-08 09:30:00",
            "machine": "linux-box",
            "tool": "codex",
            "path": str(self.mf / "repo-proj"),
            "status": status,
            "kind": "run",
            "title": "Fix DEMO-12 parser",
            "elapsed_seconds": "12.5",
            "agent_model": "agent-model-x",
        }
        run.meta.update(meta)
        self._brief(d)
        (self.mf / "repo-proj" / ".git").mkdir(parents=True, exist_ok=True)
        run.save()
        return run

    def _rows(self):
        path = ri.index_path()
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_append_on_finish_creates_header_and_row(self):
        run = self._run(status="running")
        run.set_status("done")
        path = pathlib.Path(ri.index_path())
        self.assertTrue(path.is_file())
        text = path.read_text()
        self.assertTrue(text.startswith("run_id,date,machine,tool,model,repo,linear_issue,requester,status,duration,link\n"))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["run_id"], run.id)
        self.assertEqual(row["date"], "2026-10-08T09:30:00")
        self.assertEqual(row["machine"], "linux-box")
        self.assertEqual(row["tool"], "codex")
        self.assertEqual(row["model"], "agent-model-x")
        self.assertEqual(row["repo"], "repo-proj")
        self.assertEqual(row["linear_issue"], "DEMO-12")
        self.assertEqual(row["requester"], "owner")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["duration"], "12.5")
        self.assertEqual(row["link"], "files/runs/2026/10/%s" % run.id)

    def test_model_falls_back_to_brief_model(self):
        run = self._run(status="running")
        run.meta.pop("agent_model", None)
        run.set_status("done")
        self.assertEqual(self._rows()[0]["model"], "gpt-test")

    def test_requester_from_brief(self):
        run = self._run(status="running")
        self._brief(run.dir, extra="requester: alice\n")
        run.set_status("failed", reason="boom")
        self.assertEqual(self._rows()[0]["requester"], "alice")
        self.assertEqual(self._rows()[0]["status"], "failed")

    def test_skip_duplicate_same_run_id_and_status(self):
        run = self._run(status="running")
        run.set_status("done")
        run.set_status("done")
        self.assertEqual(len(self._rows()), 1)

    def test_rejected_cancelled_is_indexed(self):
        run = self._run(status="waiting-approval")
        run.set_status("cancelled", reason="rejected")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "cancelled")

    def test_non_finish_status_does_not_append(self):
        run = self._run(status="queued")
        run.set_status("running")
        self.assertFalse(pathlib.Path(ri.index_path()).exists())

    def test_bare_temp_run_does_not_touch_index(self):
        """Hermetic tests that use a flat temp dir must not write the real/sandbox index."""
        with tempfile.TemporaryDirectory() as bare:
            run = mr.Run(bare)
            run.meta = dict(id="leaky", tool="codex", status="running", machine="x")
            pathlib.Path(bare, "input.md").write_text("---\ntitle: x\n---\n")
            run.set_status("done")
            self.assertFalse(pathlib.Path(ri.index_path()).exists())
            self.assertFalse(pathlib.Path(bare, "index.csv").exists())

    def test_repo_empty_when_path_not_git(self):
        run = self._run(status="running", path=str(self.mf / "not-a-repo"))
        (self.mf / "not-a-repo").mkdir(parents=True, exist_ok=True)
        run.set_status("done")
        self.assertEqual(self._rows()[0]["repo"], "")

    def test_reindex_preview_and_apply_from_runs_and_codex(self):
        done = self._run(rid="20261008-0930-linux-box-done-one", status="done")
        done.save()
        failed = self._run(rid="20261008-0930-linux-box-fail-two", status="failed",
                           title="DEMO-7 fail", agent_model="")
        self._brief(failed.dir, body="see DEMO-7")
        failed.save()

        codex = self.home / "na" / "runs" / "codex-runs"
        codex.mkdir(parents=True)
        (codex / "demo79.md").write_text("# Task: DEMO-79 — normaliser\nLinear issue: DEMO-79\n")
        (codex / "demo79.log").write_text("model: gpt-6-sol\nworkdir: /workspace/project\n")

        preview = ri.reindex(apply=False)
        self.assertGreaterEqual(preview["would_append"], 3)
        self.assertEqual(preview["appended"], 0)
        self.assertFalse(pathlib.Path(ri.index_path()).exists())

        applied = ri.reindex(apply=True)
        self.assertEqual(applied["appended"], applied["would_append"])
        rows = self._rows()
        ids = {r["run_id"] for r in rows}
        self.assertIn(done.id, ids)
        self.assertIn(failed.id, ids)
        self.assertIn("demo79", ids)
        codex_row = next(r for r in rows if r["run_id"] == "demo79")
        self.assertEqual(codex_row["tool"], "codex")
        self.assertEqual(codex_row["status"], "unknown")
        self.assertEqual(codex_row["linear_issue"], "DEMO-79")

        again = ri.reindex(apply=True)
        self.assertEqual(again["appended"], 0)
        self.assertEqual(len(self._rows()), len(rows))


if __name__ == "__main__":
    unittest.main()
