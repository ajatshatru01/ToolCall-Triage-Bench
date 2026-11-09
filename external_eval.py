"""Run an additional OpenAI-compatible model without changing registered results.

This is a post-registration comparison, not part of either original model cohort.
Only synthetic descriptions are sent to the API; proposed actions are not run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import eval as benchmark

ROOT = Path(__file__).resolve().parent
CHALLENGE = ROOT / "challenge"
SUITES = ("main-development", "main-final", "curl-challenge")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def suite_data(name: str) -> tuple[list[dict], str, str]:
    """Return cases, reference-protocol hash, and case-file hash."""
    if name.startswith("main-"):
        all_cases, digest = benchmark.load_cases()
        protocol = json.loads(benchmark.PROTOCOL_PATH.read_text(encoding="utf-8"))
        if digest != protocol["suite_sha256"] or file_hash(ROOT / "eval.py") != protocol["runner_sha256"]:
            raise ValueError("Main registered benchmark changed")
        split = "development" if name == "main-development" else "final"
        cases = [case for case in all_cases if case["split"] == split]
        return cases, file_hash(benchmark.PROTOCOL_PATH), benchmark.sha256(benchmark.canonical(cases))
    protocol_path = CHALLENGE / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if file_hash(CHALLENGE / "cases.json") != protocol["cases_sha256"]:
        raise ValueError("Curl challenge cases changed")
    if benchmark.sha256(benchmark.MINIMAL_PROMPT) != protocol["prompt_sha256"]:
        raise ValueError("Registered prompt changed")
    cases = json.loads((CHALLENGE / "cases.json").read_text(encoding="utf-8"))
    return cases, file_hash(protocol_path), protocol["cases_sha256"]


def messages_for(name: str, case: dict) -> list[dict[str, str]]:
    if name.startswith("main-"):
        return benchmark.messages_for(case, "minimal")
    packet = {
        "user_task": case["user_task"],
        "proposed_action": case["action"],
        "workspace_files": case["files"],
        "http_response_fixture": case["http_response_fixture"],
    }
    return [
        {"role": "system", "content": benchmark.MINIMAL_PROMPT},
        {"role": "user", "content": benchmark.canonical(packet)},
    ]


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line] if path.exists() else []


def score(output: Path) -> dict:
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    cases, protocol_hash, case_hash = suite_data(metadata["suite"])
    by_id = {case["id"]: case for case in cases}
    if metadata["reference_protocol_sha256"] != protocol_hash or metadata["cases_sha256"] != case_hash:
        raise ValueError("Source or registered protocol changed")
    if metadata["prompt_sha256"] != benchmark.sha256(benchmark.MINIMAL_PROMPT):
        raise ValueError("Prompt changed")
    selected = metadata["selected_case_ids"]
    if len(set(selected)) != len(selected) or any(case_id not in by_id for case_id in selected):
        raise ValueError("Result selection does not match the suite")
    rows = read_rows(output / "raw_outputs.jsonl")
    expected = {(repeat, case_id) for repeat in range(1, metadata["runs_per_case"] + 1) for case_id in selected}
    found = [(row["run"], row["case_id"]) for row in rows]
    if len(found) != len(expected) or set(found) != expected:
        raise ValueError(f"Incomplete or duplicated results: {len(found)}/{len(expected)}")
    for row in rows:
        case = by_id[row["case_id"]]
        if row["expected"] != case["expected"]:
            raise ValueError(f"Gold label differs for {row['case_id']}")
        if row["input_sha256"] != benchmark.sha256(benchmark.canonical(messages_for(metadata["suite"], case))):
            raise ValueError(f"Input differs for {row['case_id']}")
    result = {"metadata": metadata, "metrics": benchmark.summarize(rows, [by_id[case_id] for case_id in selected])}
    benchmark.write_json(output / "summary.json", result)
    return result


def run(args: argparse.Namespace) -> None:
    if not args.confirm_spend:
        raise ValueError("Live API calls require --confirm-spend")
    key = os.environ.get(args.api_key_env, "")
    if not key:
        raise ValueError(f"Set {args.api_key_env} in your shell; do not put it in a file or command")
    if args.runs not in range(1, 11) or not 64 <= args.max_tokens <= 8192:
        raise ValueError("Use 1–10 repeats and 64–8192 max tokens")
    cases, protocol_hash, case_hash = suite_data(args.suite)
    if args.case_id:
        if args.suite != "main-development":
            raise ValueError("--case-id is for development probes only")
        cases = [case for case in cases if case["id"] in args.case_id]
        if len(cases) != len(set(args.case_id)):
            raise ValueError("Unknown or duplicate --case-id")
    output = Path(args.output).resolve()
    if ROOT not in output.parents:
        raise ValueError("Output must stay inside the micro1 evaluation folder")
    output.mkdir(parents=True, exist_ok=True)
    metadata_path = output / "metadata.json"
    metadata = {
        "evaluation_type": "post-registration external-model comparison",
        "suite": args.suite,
        "reference_protocol_sha256": protocol_hash,
        "cases_sha256": case_hash,
        "prompt_sha256": benchmark.sha256(benchmark.MINIMAL_PROMPT),
        "external_runner_sha256": file_hash(Path(__file__)),
        "model": args.model,
        "api_base": args.api_base,
        "case_count": len(cases),
        "runs_per_case": args.runs,
        "selected_case_ids": [case["id"] for case in cases],
        "temperature": None if args.omit_temperature else args.temperature,
        "max_tokens": args.max_tokens,
        "token_param": args.token_param,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    if metadata_path.exists():
        saved = json.loads(metadata_path.read_text(encoding="utf-8"))
        for field, value in metadata.items():
            if field != "started_at_utc" and saved.get(field) != value:
                raise ValueError(f"Existing output differs on {field}; choose another --output")
    else:
        if any(output.iterdir()):
            raise ValueError("Output directory is not empty")
        benchmark.write_json(metadata_path, metadata)
    raw_path = output / "raw_outputs.jsonl"
    completed = {(row["run"], row["case_id"]) for row in read_rows(raw_path)}
    if len(completed) != len(read_rows(raw_path)):
        raise ValueError("Raw output has duplicate case runs")
    for repeat in range(1, args.runs + 1):
        for case in cases:
            if (repeat, case["id"]) in completed:
                continue
            messages = messages_for(args.suite, case)
            started = time.monotonic()
            for attempt in range(1, 5):
                try:
                    content, usage, response_id, finish_reason = benchmark.request_model(
                        api_base=args.api_base,
                        api_key=key,
                        model=args.model,
                        messages=messages,
                        temperature=None if args.omit_temperature else args.temperature,
                        max_tokens=args.max_tokens,
                        token_param=args.token_param,
                        timeout=args.timeout,
                        reasoning_effort=None,
                    )
                    break
                except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
                    retryable = not isinstance(exc, RuntimeError) or any(f"Provider HTTP {code}" in str(exc) for code in (429, 500, 502, 503, 504))
                    if not retryable or attempt == 4:
                        raise RuntimeError(f"Request failed for {case['id']} after {attempt} attempt(s): {type(exc).__name__}: {exc}") from exc
                    time.sleep(min(2**attempt, 20))
            predicted, issue = benchmark.parse_decision(content)
            row = {
                "run": repeat,
                "case_id": case["id"],
                "pair": case["pair"],
                "expected": case["expected"],
                "predicted": predicted,
                "format_issue": issue if predicted is not None else None,
                "parse_error": issue if predicted is None else None,
                "raw_response": content,
                "request_attempts": attempt,
                "provider_response_id": response_id,
                "finish_reason": finish_reason,
                "usage": usage,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "input_sha256": benchmark.sha256(benchmark.canonical(messages)),
            }
            with raw_path.open("a", encoding="utf-8") as file:
                file.write(benchmark.canonical(row) + "\n")
            print(f"{repeat}/{args.runs} {case['id']}: {predicted or issue}", flush=True)
    print(benchmark.canonical(score(output)["metrics"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--suite", choices=SUITES, required=True)
    execute.add_argument("--api-base", default="https://openrouter.ai/api/v1")
    execute.add_argument("--api-key-env", default="EVAL_API_KEY")
    execute.add_argument("--model", required=True)
    execute.add_argument("--runs", type=int, default=3)
    execute.add_argument("--case-id", action="append")
    execute.add_argument("--temperature", type=float, default=0.0)
    execute.add_argument("--omit-temperature", action="store_true")
    execute.add_argument("--max-tokens", type=int, default=2048)
    execute.add_argument("--token-param", choices=("max_tokens", "max_completion_tokens"), default="max_tokens")
    execute.add_argument("--timeout", type=int, default=90)
    execute.add_argument("--output", required=True)
    execute.add_argument("--confirm-spend", action="store_true")
    examine = commands.add_parser("score")
    examine.add_argument("--results", required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            run(args)
        else:
            output = Path(args.results).resolve()
            if ROOT not in output.parents:
                raise ValueError("Results must stay inside this folder")
            print(benchmark.canonical(score(output)["metrics"]))
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
