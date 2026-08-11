import json
import tempfile
import unittest
from pathlib import Path

from boxporter.core import BoxPorter, BoxPorterError, parse_document, submission_sha


class BoxPorterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.porter = BoxPorter(self.base / ".boxporter", self.base)
        self.porter.init()

    def tearDown(self):
        self.temp.cleanup()

    def add_and_promote(self, task_id="task-1"):
        self.porter.add(task_id, "Demo", "# Goal\n\nMake the test pass.")
        self.porter.promote()
        return self.porter.active_task()[0]

    def write_evidence(self, result="done", verify="tests pass"):
        self.porter.layout.result.write_text(result, encoding="utf-8")
        self.porter.layout.verify.write_text(verify, encoding="utf-8")

    def test_init_creates_layout(self):
        self.assertTrue(self.porter.layout.pending.is_dir())
        self.assertTrue(self.porter.layout.passed.is_dir())
        self.assertEqual(self.porter.status()["pending_count"], 0)

    def test_add_promote_and_single_active_invariant(self):
        metadata = self.add_and_promote()
        self.assertEqual(metadata["state"], "READY")
        self.porter.add("task-2", "Next", "Later")
        with self.assertRaisesRegex(BoxPorterError, "active task"):
            self.porter.promote()

    def test_duplicate_task_id_rejected_across_boxes(self):
        self.add_and_promote()
        with self.assertRaisesRegex(BoxPorterError, "already exists"):
            self.porter.add("task-1", "Duplicate", "No")

    def test_submission_is_content_addressed(self):
        self.add_and_promote()
        self.porter.transition("WORKING")
        self.write_evidence()
        expected = submission_sha(self.porter.layout.result, self.porter.layout.verify)
        actual = self.porter.submit("executor-a")
        self.assertEqual(actual, expected)
        metadata, _ = self.porter.active_task()
        self.assertEqual(metadata["state"], "REVIEW_PENDING")

    def test_review_rejects_changed_evidence(self):
        self.add_and_promote()
        self.porter.transition("WORKING")
        self.write_evidence()
        self.porter.submit("executor-a")
        self.porter.layout.verify.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(BoxPorterError, "changed"):
            self.porter.review("PASS", "reviewer-b", "Looks good")

    def test_revise_returns_to_executor(self):
        self.add_and_promote()
        self.porter.transition("WORKING")
        self.write_evidence()
        self.porter.submit("executor-a")
        self.porter.review("REVISE", "reviewer-b", "Needs work", "Add a regression test")
        metadata, _ = self.porter.active_task()
        self.assertEqual(metadata["state"], "REVISE")
        self.assertEqual(metadata["handoff_to"], "executor")

    def test_pass_archives_and_preserves_human_readable_record(self):
        self.add_and_promote()
        self.porter.transition("WORKING")
        self.write_evidence()
        self.porter.submit("executor-a")
        archive = self.porter.review("PASS", "reviewer-b", "All gates passed")
        self.assertIsNotNone(archive)
        self.assertFalse(self.porter.layout.active.exists())
        metadata, body = parse_document(archive / "task.md")
        self.assertEqual(metadata["state"], "PASS")
        self.assertIn("Make the test pass", body)
        self.assertEqual(
            {path.name for path in archive.iterdir()},
            {"task.md", "result.md", "verify.md", "executor.md", "reviewer.md", "manifest.json"},
        )
        manifest = json.loads((archive / "manifest.json").read_text())
        for name, digest in manifest["files"].items():
            import hashlib

            self.assertEqual(hashlib.sha256((archive / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(self.porter.status()["passed_count"], 1)

    def test_archive_recovers_after_crash_between_bundle_and_active_cleanup(self):
        self.add_and_promote()
        self.porter.transition("WORKING")
        self.write_evidence()
        self.porter.submit("executor-a")
        archive = self.porter.review("PASS", "reviewer-b", "All gates passed")
        self.porter.layout.active.write_bytes((archive / "task.md").read_bytes())
        recovered = self.porter.archive_passed()
        self.assertEqual(recovered, archive)
        self.assertFalse(self.porter.layout.active.exists())
        self.assertEqual(self.porter.status()["passed_count"], 1)

    def test_tick_is_zero_model_when_hooks_are_empty(self):
        self.porter.add("task-1", "Demo", "Body")
        result = self.porter.tick()
        self.assertEqual(result["action"], "manual_handoff_required")
        self.assertFalse(result["model_call"])

    def test_block_moves_task_out_of_active(self):
        self.add_and_promote()
        path = self.porter.block("credential required")
        self.assertTrue(path.is_file())
        self.assertFalse(self.porter.layout.active.exists())
        self.assertEqual(parse_document(path)[0]["state"], "BLOCKED")

    def test_config_fails_closed(self):
        config = json.loads(self.porter.layout.config.read_text())
        config["stale_seconds"] = 0
        self.porter.layout.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(BoxPorterError, "positive integer"):
            self.porter.doctor()


if __name__ == "__main__":
    unittest.main()
