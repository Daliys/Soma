import asyncio
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Soma"))

import scout_pipeline


class ScoutPipelineTests(unittest.TestCase):
    def run_gather(self, prompt, project_root, *extra_args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            asyncio.run(
                scout_pipeline.run_gather(
                    prompt,
                    str(project_root),
                    "[]",
                    *extra_args,
                )
            )
        return json.loads(stdout.getvalue())

    def make_repo(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "Soma").mkdir()
        (root / "Soma" / "relay.py").write_text("MODEL = 'qwen3:4b'\n\ndef relay():\n    return 'ok'\n")
        (root / "Soma" / "ContentView.swift").write_text("import SwiftUI\n\nstruct ContentView: View {\n    var body: some View { Text(\"Soma\") }\n}\n")
        (root / "Package.swift").write_text("// swift-tools-version: 5.9\n")
        (root / "ollama_logs.txt").write_text("INFO server started\n")
        (root / "README.md").write_text("old readme\n")
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
        (root / "Soma" / "relay.py").write_text("MODEL = 'qwen3:4b'\n\ndef relay():\n    return 'fast'\n")
        (root / "README.md").write_text("new readme\n")
        (root / ".DS_Store").write_text("noise")
        (root / "Soma" / "__pycache__").mkdir()
        (root / "Soma" / "__pycache__" / "relay.cpython-313.pyc").write_bytes(b"noise")
        return tmp, root

    def test_gather_omits_raw_git_diff(self):
        tmp, root = self.make_repo()
        with tmp:
            bundle = self.run_gather("relay is slow, check diff", root, "fast", False)

        self.assertIsNone(bundle["git_diff"])
        self.assertGreater(bundle["git_diff_summary"]["raw_diff_chars_omitted"], 0)
        self.assertNotIn("diff --git", bundle["codex_packet"])
        self.assertLessEqual(bundle["estimated_tokens"], scout_pipeline.TOKEN_BUDGETS["fast"])

    def test_explicit_project_file_is_prioritized(self):
        tmp, root = self.make_repo()
        with tmp:
            explicit = root / "Soma" / "relay.py"
            bundle = self.run_gather(f"check {explicit} for relay latency", root, "fast", False)

        self.assertTrue(bundle["evidence_items"])
        self.assertEqual(bundle["evidence_items"][0]["path"], scout_pipeline.normalize_path(explicit))

    def test_general_prompt_direct_pass(self):
        tmp, root = self.make_repo()
        with tmp:
            bundle = self.run_gather("explain this app", root, "fast", False)

        self.assertEqual(bundle["routing_decision"], "direct_pass_through")
        self.assertEqual(bundle["codex_packet"], "explain this app")
        self.assertEqual(bundle["evidence_items"], [])

    def test_typo_changed_prompt_triggers_changes_mode(self):
        tmp, root = self.make_repo()
        with tmp:
            bundle = self.run_gather("What we changet", root, "balanced", False)

        self.assertEqual(bundle["packet_mode"], "changes")
        self.assertEqual(bundle["routing_decision"], "gathered_and_relayed")

    def test_changed_prompt_triggers_changes_mode(self):
        tmp, root = self.make_repo()
        with tmp:
            bundle = self.run_gather("what changed", root, "balanced", False)

        self.assertEqual(bundle["packet_mode"], "changes")

    def test_bugs_prompt_triggers_review_mode(self):
        tmp, root = self.make_repo()
        with tmp:
            bundle = self.run_gather("do we have bugs?", root, "balanced", False)

        self.assertEqual(bundle["packet_mode"], "review")

    def test_review_prioritizes_changed_files_above_manifest_and_logs(self):
        tmp, root = self.make_repo()
        with tmp:
            bundle = self.run_gather("do we have bugs?", root, "balanced", False)

        paths = [Path(item["path"]).name for item in bundle["evidence_items"][:3]]
        self.assertIn("relay.py", paths)
        self.assertNotEqual(paths[0], "Package.swift")
        self.assertNotEqual(paths[0], "ollama_logs.txt")

    def test_noise_files_are_omitted(self):
        tmp, root = self.make_repo()
        with tmp:
            subprocess.run(["git", "add", ".DS_Store", "Soma/__pycache__/relay.cpython-313.pyc"], cwd=root, check=True)
            bundle = self.run_gather("what changed", root, "balanced", False)

        packet = bundle["codex_packet"]
        changed_paths = [item["path"] for item in bundle["git_diff_summary"]["changed_files"]]
        evidence_paths = [item["path"] for item in bundle["evidence_items"]]
        self.assertFalse(any(".DS_Store" in path for path in changed_paths + evidence_paths))
        self.assertFalse(any("__pycache__" in path or path.endswith(".pyc") for path in changed_paths + evidence_paths))
        self.assertNotIn(".DS_Store", packet)
        self.assertNotIn("__pycache__", packet)

    def test_ranker_failure_does_not_block_packet(self):
        tmp, root = self.make_repo()
        with tmp, patch("scout_pipeline.query_ollama_model", new=AsyncMock(return_value={"error": "offline"})):
            bundle = self.run_gather("do we have bugs?", root, "balanced", False, "ranked")

        self.assertEqual(bundle["analysis_depth"], "ranked")
        self.assertTrue(bundle["codex_packet"])
        self.assertEqual(bundle["analysis_stages"][-1]["stage"], "ranker")
        self.assertEqual(bundle["analysis_stages"][-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
