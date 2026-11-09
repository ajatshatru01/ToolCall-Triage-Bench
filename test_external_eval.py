"""Offline checks for the post-registration provider comparison runner."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import external_eval


class ExternalEvalTest(unittest.TestCase):
    def test_registered_suites_and_packets(self) -> None:
        for name, count in (("main-development", 8), ("main-final", 12), ("curl-challenge", 6)):
            cases, protocol_hash, case_hash = external_eval.suite_data(name)
            self.assertEqual(len(cases), count)
            self.assertEqual(len(protocol_hash), 64)
            self.assertEqual(len(case_hash), 64)
            message = external_eval.messages_for(name, cases[0])
            self.assertNotIn("expected", message[1]["content"])
            self.assertNotIn("rationale", message[1]["content"])

    def test_mock_api_run_score_and_resume(self) -> None:
        scratch = external_eval.ROOT / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            output = Path(temporary) / "result"
            args = argparse.Namespace(
                confirm_spend=True,
                api_key_env="EVAL_API_KEY",
                runs=1,
                max_tokens=512,
                suite="main-development",
                case_id=["patch-unique"],
                output=str(output),
                model="synthetic/mock-model",
                api_base="https://example.test/v1",
                omit_temperature=False,
                temperature=0.0,
                token_param="max_tokens",
                timeout=5,
            )
            response = json.dumps({"decision": "allow", "reason": "unique target", "evidence": "fixture"})
            with patch.dict(os.environ, {"EVAL_API_KEY": "synthetic-test-key"}), patch.object(
                external_eval.benchmark,
                "request_model",
                return_value=(response, {"prompt_tokens": 1, "completion_tokens": 1}, "fake-id", "stop"),
            ) as request:
                external_eval.run(args)
                external_eval.run(args)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(external_eval.score(output)["metrics"]["strict_correct"], 1)


if __name__ == "__main__":
    unittest.main()
