"""Isolated content-reader checks; no host/service/inbox calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from docket.delivery_evidence import EvidenceUnavailable, snapshot


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.write("source.py", "answer = 1\n")
        self.commit()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              check=True, capture_output=True, timeout=3).stdout

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")

    def snap(self):
        return snapshot(self.root)["fingerprint"]

    def test_content_dirty_baseline_and_touch(self):
        first = self.snap()
        os.utime(self.root / "source.py", None)
        self.assertEqual(first, self.snap())
        self.write("source.py", "answer = 2\n")
        dirty = self.snap()
        self.assertNotEqual(first, dirty)
        self.assertEqual(dirty, self.snap())
        self.write("new.py", "new = True\n")
        self.assertNotEqual(dirty, self.snap())

    def test_whitespace_writes_and_commits(self):
        first = self.snap()
        self.write("source.py", "answer  =  1\n\n")
        self.assertEqual(first, self.snap())
        self.commit()
        self.assertEqual(first, self.snap())

    def test_excluded_writes_and_log_only_commit(self):
        first = self.snap()
        for name in ("LEAD.md", "lead/notes.md", "private/result", "evidence/a.json",
                     "activity.jsonl", "x.log", ".opencode/x", "build/a.py"):
            self.write(name, "secret activity")
        self.assertEqual(first, self.snap())
        self.commit()
        self.assertEqual(first, self.snap())

    def test_meaningful_commit_and_revert(self):
        first = self.snap()
        self.write("source.py", "answer = 2\n")
        self.commit()
        self.write("source.py", "answer = 1\n")
        self.commit()
        self.assertNotEqual(first, self.snap())

    def test_deletion_and_rename(self):
        first = self.snap()
        (self.root / "source.py").rename(self.root / "renamed.py")
        self.assertNotEqual(first, self.snap())
        self.commit()
        committed = self.snap()
        (self.root / "renamed.py").unlink()
        self.assertNotEqual(committed, self.snap())

    def test_symlink_and_fifo_refused(self):
        (self.root / "source.py").unlink()
        (self.root / "source.py").symlink_to("/etc/passwd")
        with self.assertRaises(EvidenceUnavailable):
            self.snap()
        (self.root / "source.py").unlink()
        os.mkfifo(self.root / "source.py")
        with self.assertRaises(EvidenceUnavailable):
            self.snap()

    def test_artifacts_explicit_exact_and_metadata_opaque(self):
        self.write("private/delivered", "secret payload")
        artifact = self.root / "private/delivered"
        first = snapshot(self.root, [artifact])
        self.write("private/delivered", "secret  payload")
        self.assertNotEqual(first["fingerprint"], snapshot(self.root, [artifact])["fingerprint"])
        for value in first.values():
            self.assertNotIn(str(self.root), value)
            self.assertNotIn("secret", value)
        for path in ("relative", self.root / "missing"):
            with self.assertRaises(EvidenceUnavailable):
                snapshot(self.root, [path])

    def test_non_git_artifact_only(self):
        p = self.root / "plain"
        p.mkdir()
        with self.assertRaises(EvidenceUnavailable):
            snapshot(p)
        self.write("plain/delivered", "done")
        result = snapshot(p, [p / "delivered"])
        self.assertIn("Artifact-only", result["limitations"])

    def test_bounds_and_git_failure(self):
        for key, value in (("MAX_BYTES", 1), ("MAX_FILES", 0),
                           ("MAX_SECONDS", 0), ("MAX_OUTPUT", 1)):
            with patch("docket.delivery_evidence." + key, value):
                with self.assertRaises(EvidenceUnavailable):
                    self.snap()
        with patch("docket.delivery_evidence.subprocess.Popen", side_effect=OSError("secret")):
            with self.assertRaisesRegex(EvidenceUnavailable, "^observation unavailable or unsafe$"):
                self.snap()

    def test_mutation_between_passes(self):
        from docket.delivery_evidence import _Reader
        original = _Reader.source
        calls = []

        def changing(reader, root):
            calls.append(1)
            if len(calls) == 2:
                self.write("source.py", "changed")
            return original(reader, root)

        with patch.object(_Reader, "source", changing):
            with self.assertRaises(EvidenceUnavailable):
                self.snap()

    def test_history_bound_is_unavailable_not_stall(self):
        for i in range(3):
            self.write("source.py", "answer" + " " * (i + 2) + "= 1\n")
            self.commit()
        with patch("docket.delivery_evidence.MAX_HISTORY", 2):
            with self.assertRaisesRegex(EvidenceUnavailable, "history bound"):
                self.snap()

    def test_artifact_symlink_ancestor_and_binary(self):
        self.write("private/artifact", "a")
        (self.root / "alias").symlink_to(self.root / "private", target_is_directory=True)
        with self.assertRaises(EvidenceUnavailable):
            snapshot(self.root, [self.root / "alias/artifact"])
        # Remove the untracked source symlink before observing normal source.
        (self.root / "alias").unlink()
        p = self.root / "binary.dat"
        p.write_bytes(b"\0a b")
        first = self.snap()
        p.write_bytes(b"\0a  b")
        self.assertNotEqual(first, self.snap())

    def test_hooks_and_external_diff_not_invoked(self):
        hook = self.root / ".git/hooks/post-index-change"
        hook.write_text("#!/bin/sh\nexit 99\n")
        hook.chmod(0o755)
        self.git("config", "diff.external", "/does/not/exist")
        self.git("config", "core.fsmonitor", "/does/not/exist")
        self.assertEqual(self.snap(), self.snap())

    def test_nested_registered_worker_source_and_log_exclusion(self):
        initial = snapshot(self.root)
        first = initial["fingerprint"]
        # Newline and space exercise NUL-delimited porcelain path parsing.
        worker = self.root / "wt/worker space\nname"
        self.git("worktree", "add", "-qb", "worker", str(worker))
        self.assertEqual(first, self.snap(), "identical topology addition is not delivery")
        included = snapshot(self.root)
        self.assertNotEqual(initial["scope"], included["scope"])
        self.assertEqual(64, len(included["scope"]))
        self.assertEqual(included["scope"], snapshot(self.root)["scope"])
        self.assertNotIn(str(worker), included["scope"])
        artifact = worker / "source.py"
        configured = snapshot(self.root, [artifact])
        self.assertNotEqual(included["scope"], configured["scope"])
        log = worker / "activity.log"
        log.write_text("activity only")
        os.utime(worker / "source.py", None)
        self.assertEqual(first, self.snap())
        subprocess.run(["git", "-C", str(worker), "add", "activity.log"],
                       check=True, capture_output=True, timeout=3)
        subprocess.run(["git", "-C", str(worker), "-c", "core.hooksPath=/dev/null",
                        "commit", "-qm", "log only"],
                       check=True, capture_output=True, timeout=3)
        self.assertEqual(first, self.snap())
        (worker / "source.py").write_text("answer = 7\n")
        changed = self.snap()
        self.assertNotEqual(first, changed)
        self.assertEqual(changed, self.snap(), "existing dirty worker is stable baseline")
        self.assertEqual(included["scope"], snapshot(self.root)["scope"])
        self.assertEqual(configured["scope"], snapshot(self.root, [artifact])["scope"])
        subprocess.run(["git", "-C", str(worker), "add", "source.py"],
                       check=True, capture_output=True, timeout=3)
        subprocess.run(["git", "-C", str(worker), "-c", "core.hooksPath=/dev/null",
                        "commit", "-qm", "source delivery"],
                       check=True, capture_output=True, timeout=3)
        self.assertNotEqual(changed, self.snap())
        self.assertEqual(included["scope"], snapshot(self.root)["scope"])
        with patch("docket.delivery_evidence.MAX_WORKTREES", 0):
            with self.assertRaisesRegex(EvidenceUnavailable, "worktree bound"):
                self.snap()
        self.git("worktree", "remove", str(worker))
        removed = snapshot(self.root)
        self.assertEqual(initial["scope"], removed["scope"])
        self.assertEqual(first, removed["fingerprint"])


if __name__ == "__main__":
    unittest.main()
