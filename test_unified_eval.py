"""Checks for the combined release view and its one-command runner."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import unified_eval


class UnifiedEvalTests(unittest.TestCase):
    def test_combined_cases_preserve_frozen_sources(self) -> None:
        cases = unified_eval.validate()
        self.assertEqual(len(cases), 18)
        self.assertEqual(len({case["pair"] for case in cases}), 9)
        self.assertEqual(
            {label: sum(case["expected"] == label for case in cases) for label in ("allow", "review", "block")},
            {"allow": 7, "review": 2, "block": 9},
        )

    def test_prior_results_and_baseline(self) -> None:
        scratch = unified_eval.ROOT / ".scratch"
        scratch.mkdir(exist_ok=True)
        report = unified_eval.report(scratch / "unified-test-report.json")
        scores = {item["model"]: item["metrics"]["overall_descriptive"]["strict_correct"] for item in report["models"]}
        self.assertEqual(
            scores,
            {
                "meta/muse-glimmer-30b": 46,
                "qwen/qwen3-235b-a22b-2507": 33,
                "deepseek/deepseek-v3.2": 46,
            },
        )
        baseline = report["always_block_baseline"]["overall_descriptive"]
        self.assertEqual((baseline["strict_correct"], baseline["records"]), (27, 54))
        self.assertEqual(baseline["pairwise_both_correct"], 0)

    def test_mock_unified_run_score_and_resume(self) -> None:
        scratch = unified_eval.ROOT / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            output = Path(temporary) / "run"
            args = argparse.Namespace(
                confirm_spend=True,
                api_key_env="EVAL_API_KEY",
                runs=1,
                max_tokens=512,
                output=str(output),
                model="synthetic/mock",
                api_base="https://example.test/v1",
                omit_temperature=False,
                temperature=0.0,
                token_param="max_tokens",
                timeout=5,
                reasoning_effort=None,
            )
            decisions = [case["expected"] for case in unified_eval.validate()]
            responses = [
                (json.dumps({"decision": label, "reason": "fixture", "evidence": "fixture"}), {}, None, "stop")
                for label in decisions
            ]
            with patch.dict(os.environ, {"EVAL_API_KEY": "synthetic-test-key"}), patch.object(
                unified_eval.grader, "request_model", side_effect=responses
            ) as request:
                unified_eval.run(args)
                unified_eval.run(args)
            self.assertEqual(request.call_count, 18)
            result = unified_eval.score_unified(output)
            self.assertEqual(result["metrics"]["overall_descriptive"]["strict_correct"], 18)
            self.assertEqual(result["metrics"]["overall_descriptive"]["pairwise_both_correct"], 9)


if __name__ == "__main__":
    unittest.main()
