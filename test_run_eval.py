"""Offline checks for the guided provider launcher."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import run_eval


class GuidedRunnerTests(unittest.TestCase):

    def test_all_models_defined(self) -> None:
        ids = [m["id"] for m in run_eval.MODELS]
        self.assertIn("meta/muse-glimmer-30b", ids)
        self.assertIn("qwen/qwen3-235b-a22b-2507", ids)
        self.assertIn("deepseek/deepseek-v3.2", ids)
        self.assertEqual(len(run_eval.MODELS), 3)

    def test_build_command_qwen(self) -> None:
        model = next(m for m in run_eval.MODELS if m["id"] == "qwen/qwen3-235b-a22b-2507")
        cmd = run_eval.build_command(model, 3, "results/unified/openrouter-qwen")
        self.assertIn("unified_eval.py", cmd)
        self.assertIn("--confirm-spend", cmd)
        self.assertNotIn("--omit-temperature", cmd)
        self.assertNotIn("--api-base", cmd)

    def test_build_command_deepseek_omits_temperature(self) -> None:
        model = next(m for m in run_eval.MODELS if m["id"] == "deepseek/deepseek-v3.2")
        cmd = run_eval.build_command(model, 3, "results/unified/openrouter-deepseek")
        self.assertIn("--omit-temperature", cmd)

    def test_build_command_glimmer_no_omit_temperature(self) -> None:
        model = next(m for m in run_eval.MODELS if m["id"] == "meta/muse-glimmer-30b")
        cmd = run_eval.build_command(model, 3, "results/unified/openrouter-glimmer")
        self.assertNotIn("--omit-temperature", cmd)
        self.assertIn("--confirm-spend", cmd)

    def test_default_output_stable(self) -> None:
        self.assertEqual(
            run_eval.default_output("meta/muse-glimmer-30b"),
            run_eval.default_output("meta/muse-glimmer-30b"),
        )
        self.assertIn("glimmer", run_eval.default_output("meta/muse-glimmer-30b"))
        self.assertIn("deepseek", run_eval.default_output("deepseek/deepseek-v3.2"))

    def test_fmt_decision_allow(self) -> None:
        formatted = run_eval._fmt_decision("1/3 pretest-local: allow")
        self.assertIn("allow", formatted.lower())
        self.assertIn("pretest-local", formatted)

    def test_fmt_decision_block(self) -> None:
        formatted = run_eval._fmt_decision("2/3 pretest-upload: block")
        self.assertIn("block", formatted.lower())

    def test_fmt_decision_parse_error(self) -> None:
        formatted = run_eval._fmt_decision("1/3 some-case: ERROR parse failed")
        # Should not crash; should still mention the case
        self.assertIn("some-case", formatted)

    def test_hidden_key_reaches_child_environment_only(self) -> None:
        """API key must arrive in child env, never in the command string."""
        scratch = run_eval.ROOT / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary)
            # Pre-create summary files for all 4 models
            for model in run_eval.MODELS:
                output = run_eval.default_output(model["id"])
                summary_dir = root / output
                summary_dir.mkdir(parents=True, exist_ok=True)
                (summary_dir / "summary.json").write_text(
                    json.dumps({
                        "metrics": {
                            "overall_descriptive": {
                                "strict_correct": 18, "records": 18,
                                "pairwise_both_correct": 9, "pairwise_total": 9,
                            },
                            "by_source": {
                                "main-final":     {"strict_correct": 12, "records": 12},
                                "curl-challenge": {"strict_correct": 6,  "records": 6},
                            },
                        }
                    }),
                    encoding="utf-8",
                )

            argv = ["run_eval.py", "--yes", "--prompt-key"]
            fake_proc = MagicMock()
            fake_proc.stdout = iter([
                "1/3 pretest-local: allow\n",
                "Combined: 18/18 correct; 9/9 paired decisions correct\n",
            ] * len(run_eval.MODELS))
            fake_proc.returncode = 0
            fake_proc.wait.return_value = None

            with (
                patch.object(sys, "argv", argv),
                patch.object(run_eval, "ROOT", root),
                patch.object(run_eval.getpass, "getpass", return_value="synthetic-test-key") as prompt,
                patch.object(run_eval.subprocess, "Popen", return_value=fake_proc) as popen_mock,
                patch.dict(os.environ, {"EVAL_API_KEY": "", "OPENROUTER_API_KEY": ""}),
            ):
                run_eval.main()

            prompt.assert_called_once()
            for call in popen_mock.call_args_list:
                cmd = call.args[0]
                # Key must NOT be in any command argument
                self.assertNotIn("synthetic-test-key", " ".join(cmd))
                # Key MUST be in the child env
                self.assertEqual(call.kwargs["env"]["EVAL_API_KEY"], "synthetic-test-key")


if __name__ == "__main__":
    unittest.main()
