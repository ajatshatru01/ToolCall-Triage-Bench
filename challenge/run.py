"""Registered follow-up stress test of curl redirect credential handling.

This extension was designed after the original final split and is reported
separately. Proposed actions are never executed during model evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
import eval as benchmark  # noqa: E402

CASES = ROOT / "cases.json"
PROTOCOL = ROOT / "protocol.json"
MODELS = ("muse-wonder-1.0", "muse-spark-1.3")
RUNS = 3


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def current_hashes() -> dict[str, str]:
    return {
        "cases_sha256": digest(CASES.read_bytes()),
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "base_runner_sha256": digest((ROOT.parent / "eval.py").read_bytes()),
        "policy_sha256": digest((ROOT.parent / "policy.md").read_bytes()),
        "prompt_sha256": benchmark.sha256(benchmark.MINIMAL_PROMPT),
        "oracle_script_sha256": digest((ROOT / "verify_curl.py").read_bytes()),
        "oracle_results_sha256": digest((ROOT / "oracle_results.json").read_bytes()),
    }


def cases() -> list[dict]:
    items = json.loads(CASES.read_text(encoding="utf-8"))
    if len(items) != 6 or len({case["id"] for case in items}) != 6:
        raise ValueError("Expected six unique cases")
    pairs = Counter(case["pair"] for case in items)
    if len(pairs) != 3 or set(pairs.values()) != {2}:
        raise ValueError("Expected three two-variant pairs")
    for pair in pairs:
        members = [case for case in items if case["pair"] == pair]
        if {case["expected"] for case in members} != {"allow", "block"}:
            raise ValueError(f"Bad pair labels: {pair}")
    if any(case["action"].get("kind") != "shell" for case in items):
        raise ValueError("This challenge is limited to curl shell proposals")
    return items


def validate_protocol() -> dict:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    for name, value in current_hashes().items():
        if protocol[name] != value:
            raise ValueError(f"Frozen protocol mismatch: {name}")
    if protocol["models"] != list(MODELS) or protocol["runs"] != RUNS:
        raise ValueError("Frozen model settings changed")
    return protocol


def input_for(case: dict) -> dict:
    # Everything in this packet is model-visible. The label and rationale are not.
    return {
        "user_task": case["user_task"],
        "proposed_action": case["action"],
        "workspace_files": case["files"],
        "http_response_fixture": case["http_response_fixture"],
    }


def results_dir(model: str) -> Path:
    return ROOT / "results" / model


def rows_for(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def score(model: str) -> dict:
    items = cases()
    output = results_dir(model)
    rows = rows_for(output / "raw_outputs.jsonl")
    expected_keys = {(run, case["id"]) for run in range(1, RUNS + 1) for case in items}
    found_keys = [(row["run"], row["case_id"]) for row in rows]
    if len(set(found_keys)) != len(found_keys) or set(found_keys) != expected_keys:
        raise ValueError(f"Incomplete or duplicate results for {model}: {len(rows)}/{len(expected_keys)}")
    if any(row.get("request_error") for row in rows):
        raise ValueError("Transport errors are not scored as model errors")
    summary = {
        "metadata": json.loads((output / "metadata.json").read_text(encoding="utf-8")),
        "metrics": benchmark.summarize(rows, items),
    }
    benchmark.write_json(output / "summary.json", summary)
    return summary


def run(model: str) -> None:
    if model not in MODELS:
        raise ValueError("Model outside frozen protocol")
    protocol = validate_protocol()
    key = os.environ.get("MODEL_API_KEY", "")
    if not key:
        raise ValueError("Set MODEL_API_KEY without printing it")
    items = cases()
    output = results_dir(model)
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "raw_outputs.jsonl"
    metadata_path = output / "metadata.json"
    metadata = {
        "protocol_sha256": digest(PROTOCOL.read_bytes()),
        "model": model,
        "api_base": "https://api.meta.ai/v1",
        "case_count": len(items),
        "runs_per_case": RUNS,
        "prompt_sha256": protocol["prompt_sha256"],
        "temperature": None,
        "max_completion_tokens": 2048,
        "reasoning_effort": "low",
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    if metadata_path.exists():
        saved = json.loads(metadata_path.read_text(encoding="utf-8"))
        for field in ("protocol_sha256", "model", "case_count", "runs_per_case", "prompt_sha256"):
            if saved[field] != metadata[field]:
                raise ValueError(f"Existing result metadata differs: {field}")
    else:
        benchmark.write_json(metadata_path, metadata)
    completed = {(row["run"], row["case_id"]) for row in rows_for(raw_path)}
    for repeat in range(1, RUNS + 1):
        for case in items:
            if (repeat, case["id"]) in completed:
                continue
            packet = input_for(case)
            messages = [
                {"role": "system", "content": benchmark.MINIMAL_PROMPT},
                {"role": "user", "content": benchmark.canonical(packet)},
            ]
            for attempt in range(1, 9):
                started = time.monotonic()
                try:
                    content, usage, response_id, finish_reason = benchmark.request_model(
                        api_base="https://api.meta.ai/v1",
                        api_key=key,
                        model=model,
                        messages=messages,
                        temperature=None,
                        max_tokens=2048,
                        token_param="max_completion_tokens",
                        timeout=90,
                        reasoning_effort="low",
                    )
                    break
                except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
                    if attempt == 8:
                        raise
                    time.sleep(min(2**attempt, 20))
            predicted, issue = benchmark.parse_decision(content)
            row = {
                "run": repeat,
                "case_id": case["id"],
                "expected": case["expected"],
                "predicted": predicted,
                "format_issue": issue,
                "parse_error": issue if predicted is None else None,
                "raw_response": content,
                "request_attempts": attempt,
                "usage": usage,
                "provider_response_id": response_id,
                "finish_reason": finish_reason,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "recorded_at_utc": datetime.now(UTC).isoformat(),
            }
            with raw_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{model} run {repeat} {case['id']}: {predicted or issue}", flush=True)
    summary = score(model)
    print(json.dumps(summary["metrics"], indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "validate", "run", "score"))
    parser.add_argument("--model", choices=MODELS)
    parser.add_argument("--confirm-spend", action="store_true")
    args = parser.parse_args()
    cases()
    if args.command == "register":
        if PROTOCOL.exists():
            parser.error("Protocol already registered; never overwrite it")
        benchmark.write_json(PROTOCOL, {
            "id": "curl-redirect-followup-2026-11-05",
            "registered_at_utc": datetime.now(UTC).isoformat(),
            "status": "Follow-up stress test designed after original final results; development probe excluded",
            "models": list(MODELS),
            "runs": RUNS,
            "temperature": "provider default",
            "max_completion_tokens": 2048,
            "reasoning_effort": "low",
            "primary_measure": "Exact allow/block decision accuracy on six unseen cases in three paired tasks",
            "analysis_rule": "Report all errors and all raw responses. Do not revise cases, labels or prompt after registration.",
            **current_hashes(),
        })
        print(f"Registered {PROTOCOL}")
    elif args.command == "validate":
        validate_protocol()
        print("Frozen challenge protocol valid")
    elif args.command == "run":
        if not args.model or not args.confirm_spend:
            parser.error("run requires --model and --confirm-spend")
        run(args.model)
    else:
        if not args.model:
            parser.error("score requires --model")
        validate_protocol()
        print(json.dumps(score(args.model)["metrics"], indent=2))


if __name__ == "__main__":
    main()
