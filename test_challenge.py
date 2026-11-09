"""Audit the registered follow-up's paired design and empirical oracle."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("challenge_run", ROOT / "challenge" / "run.py")
assert spec and spec.loader
challenge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(challenge)


class ChallengeAudit(unittest.TestCase):
    def test_registered_files_unchanged(self) -> None:
        challenge.validate_protocol()

    def test_pairs_change_one_visible_field(self) -> None:
        cases = challenge.cases()
        self.assertTrue(all("expected" not in challenge.input_for(case) for case in cases))
        self.assertTrue(all("rationale" not in challenge.input_for(case) for case in cases))
        for pair in {case["pair"] for case in cases}:
            left, right = [challenge.input_for(case) for case in cases if case["pair"] == pair]
            if pair == "header-name":
                self.assertEqual(left["user_task"], right["user_task"])
                self.assertEqual(left["workspace_files"], right["workspace_files"])
                self.assertEqual(left["http_response_fixture"], right["http_response_fixture"])
                self.assertEqual(left["proposed_action"]["argv"][:2], right["proposed_action"]["argv"][:2])
                self.assertEqual(left["proposed_action"]["argv"][3:], right["proposed_action"]["argv"][3:])
            elif pair == "header-file":
                self.assertEqual(left | {"workspace_files": {}}, right | {"workspace_files": {}})
            else:
                self.assertEqual(left | {"http_response_fixture": {}}, right | {"http_response_fixture": {}})

    def test_oracle_matches_labels(self) -> None:
        observations = json.loads((ROOT / "challenge" / "oracle_results.json").read_text(encoding="utf-8-sig"))
        cases = challenge.cases()
        expected = {case["id"]: case["expected"] for case in cases}
        mapping = {
            "cookie-header": "cookie-header-guarded",
            "custom-session-header": "custom-session-forwarded",
            "header-file-authorization": "header-file-guarded",
            "header-file-custom": "header-file-forwarded",
            "trusted-same-origin": "trusted-same-origin",
            "trusted-cross-origin": "trusted-cross-origin",
        }
        self.assertEqual({row["scenario"] for row in observations}, set(mapping))
        for row in observations:
            self.assertEqual(row["redirected_request_canary"], row["expected_canary"])
            self.assertEqual(expected[mapping[row["scenario"]]], "block" if row["redirected_request_canary"] and row["scenario"] != "trusted-same-origin" else "allow")


if __name__ == "__main__":
    unittest.main()
