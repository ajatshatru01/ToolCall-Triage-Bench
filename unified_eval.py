"""Run and score the 18-case Grounded Action Authorization Benchmark.

This release view combines the registered six-pair core final split and the
later three-pair curl stress set. The original protocols are never rewritten.
The aggregate is descriptive because the curl family was chosen post hoc.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import eval as grader
import external_eval
from challenge import run as challenge_runner

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "benchmark_cases.json"
MANIFEST = ROOT / "benchmark_manifest.json"
SUITES = ("main-final", "curl-challenge")


def source_cases() -> list[dict]:
    result = []
    for suite in SUITES:
        cases, _, _ = external_eval.suite_data(suite)
        result.extend({"source_suite": suite, **case} for case in cases)
    return result


def validate() -> list[dict]:
    challenge_runner.validate_protocol()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual = json.loads(CASES.read_text(encoding="utf-8"))
    if actual != source_cases():
        raise ValueError("Combined cases differ from their frozen source suites")
    hashes = {
        "benchmark_cases_sha256": external_eval.file_hash(CASES),
        "main_protocol_sha256": external_eval.file_hash(ROOT / "protocol.json"),
        "curl_protocol_sha256": external_eval.file_hash(ROOT / "challenge" / "protocol.json"),
        "prompt_sha256": grader.sha256(grader.MINIMAL_PROMPT),
    }
    for name, value in hashes.items():
        if manifest.get(name) != value:
            raise ValueError(f"Combined manifest mismatch: {name}")
    if len(actual) != 18 or len({case["id"] for case in actual}) != 18:
        raise ValueError("Expected 18 distinct scored cases")
    pairs: dict[str, list[dict]] = {}
    for case in actual:
        pairs.setdefault(case["pair"], []).append(case)
        if case["source_suite"] not in SUITES:
            raise ValueError("Unknown source suite")
        packet = external_eval.messages_for(case["source_suite"], case)[1]["content"]
        if any(key in json.loads(packet) for key in ("expected", "rationale", "changed_fact", "pair")):
            raise ValueError(f"Gold information leaked into model input: {case['id']}")
    if len(pairs) != 9 or any(len(members) != 2 for members in pairs.values()):
        raise ValueError("Expected nine two-variant task pairs")
    if {suite: sum(case["source_suite"] == suite for case in actual) for suite in SUITES} != {
        "main-final": 12, "curl-challenge": 6
    }:
        raise ValueError("Unexpected stratum sizes")
    return actual


def rows_at(directory: Path) -> list[dict]:
    return external_eval.read_rows(directory / "raw_outputs.jsonl")


def checked_rows(directory: Path, cases: list[dict], *, unified: bool) -> tuple[dict, list[dict]]:
    if ROOT not in directory.parents:
        raise ValueError("Result directory must be inside this benchmark folder")
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    rows = rows_at(directory)
    runs = metadata["runs_per_case"]
    if not isinstance(runs, int) or not 1 <= runs <= 10:
        raise ValueError("Invalid repeat count")
    expected = {(run, case["id"]) for run in range(1, runs + 1) for case in cases}
    found = [(row["run"], row["case_id"]) for row in rows]
    if len(found) != len(expected) or set(found) != expected:
        raise ValueError(f"Incomplete or duplicated results: {len(found)}/{len(expected)}")
    if any(row.get("request_error") for row in rows):
        raise ValueError("Transport errors are not model verdicts")
    by_id = {case["id"]: case for case in cases}
    for row in rows:
        case = by_id[row["case_id"]]
        if row["expected"] != case["expected"]:
            raise ValueError(f"Gold label mismatch: {case['id']}")
        if unified and row.get("source_suite") != case["source_suite"]:
            raise ValueError(f"Source stratum mismatch: {case['id']}")
        if row.get("input_sha256"):
            messages = external_eval.messages_for(case["source_suite"], case)
            if row["input_sha256"] != grader.sha256(grader.canonical(messages)):
                raise ValueError(f"Input hash mismatch: {case['id']}")
        elif unified or metadata.get("evaluation_type") == "post-registration external-model comparison":
            raise ValueError(f"Missing input hash: {case['id']}")
    if unified:
        if metadata.get("benchmark_manifest_sha256") != external_eval.file_hash(MANIFEST):
            raise ValueError("Combined manifest changed")
        if metadata.get("benchmark_cases_sha256") != external_eval.file_hash(CASES):
            raise ValueError("Combined cases changed")
        if metadata.get("prompt_sha256") != grader.sha256(grader.MINIMAL_PROMPT):
            raise ValueError("Prompt changed")
    return metadata, rows


def summarize(rows: list[dict], cases: list[dict]) -> dict:
    return {
        "overall_descriptive": grader.summarize(rows, cases),
        "by_source": {
            suite: grader.summarize(
                [row for row in rows if row["case_id"] in {case["id"] for case in cases if case["source_suite"] == suite}],
                [case for case in cases if case["source_suite"] == suite],
            )
            for suite in SUITES
        },
    }


def score_unified(directory: Path) -> dict:
    cases = validate()
    metadata, rows = checked_rows(directory, cases, unified=True)
    if metadata.get("case_count") != 18 or metadata.get("model") is None:
        raise ValueError("Not a complete unified run")
    if metadata.get("selected_case_ids") != [case["id"] for case in cases]:
        raise ValueError("Unified case order or selection differs")
    result = {"metadata": metadata, "metrics": summarize(rows, cases)}
    grader.write_json(directory / "summary.json", result)
    return result


def run(args: argparse.Namespace) -> None:
    if not args.confirm_spend:
        raise ValueError("Live calls require --confirm-spend")
    key = os.environ.get(args.api_key_env, "")
    if not key:
        raise ValueError(f"Set {args.api_key_env} in this shell; do not put it in a file or command")
    if not 1 <= args.runs <= 10 or not 64 <= args.max_tokens <= 8192:
        raise ValueError("Use 1–10 repeats and 64–8192 max tokens")
    cases = validate()
    output = Path(args.output).resolve()
    if ROOT not in output.parents:
        raise ValueError("Output must stay inside this benchmark folder")
    metadata = {
        "evaluation_type": "unified post-registration release benchmark",
        "benchmark_manifest_sha256": external_eval.file_hash(MANIFEST),
        "benchmark_cases_sha256": external_eval.file_hash(CASES),
        "prompt_sha256": grader.sha256(grader.MINIMAL_PROMPT),
        "model": args.model,
        "api_base": args.api_base,
        "case_count": 18,
        "runs_per_case": args.runs,
        "selected_case_ids": [case["id"] for case in cases],
        "temperature": None if args.omit_temperature else args.temperature,
        "max_tokens": args.max_tokens,
        "token_param": args.token_param,
        "reasoning_effort": args.reasoning_effort,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    output.mkdir(parents=True, exist_ok=True)
    metadata_path = output / "metadata.json"
    if metadata_path.exists():
        saved = json.loads(metadata_path.read_text(encoding="utf-8"))
        if any(saved.get(name) != value for name, value in metadata.items() if name != "started_at_utc"):
            raise ValueError("Existing output settings differ; use a new output directory")
    else:
        if any(output.iterdir()):
            raise ValueError("Output directory is not empty")
        grader.write_json(metadata_path, metadata)
    raw_path = output / "raw_outputs.jsonl"
    completed = {(row["run"], row["case_id"]) for row in rows_at(output)}
    if len(completed) != len(rows_at(output)):
        raise ValueError("Duplicate case-run slots")
    for repeat in range(1, args.runs + 1):
        for case in cases:
            if (repeat, case["id"]) in completed:
                continue
            messages = external_eval.messages_for(case["source_suite"], case)
            started = time.monotonic()
            for attempt in range(1, 5):
                try:
                    content, usage, response_id, finish_reason = grader.request_model(
                        api_base=args.api_base,
                        api_key=key,
                        model=args.model,
                        messages=messages,
                        temperature=None if args.omit_temperature else args.temperature,
                        max_tokens=args.max_tokens,
                        token_param=args.token_param,
                        timeout=args.timeout,
                        reasoning_effort=args.reasoning_effort,
                    )
                    break
                except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
                    retryable = not isinstance(exc, RuntimeError) or any(
                        f"Provider HTTP {code}" in str(exc) for code in (429, 500, 502, 503, 504)
                    )
                    if not retryable or attempt == 4:
                        raise RuntimeError(f"Request failed for {case['id']} after {attempt} attempt(s): {exc}") from exc
                    time.sleep(min(2**attempt, 20))
            predicted, issue = grader.parse_decision(content)
            row = {
                "run": repeat,
                "case_id": case["id"],
                "source_suite": case["source_suite"],
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
                "input_sha256": grader.sha256(grader.canonical(messages)),
            }
            with raw_path.open("a", encoding="utf-8") as file:
                file.write(grader.canonical(row) + "\n")
            print(f"{repeat}/{args.runs} {case['id']}: {predicted or issue}", flush=True)
    metrics = score_unified(output)["metrics"]
    overall = metrics["overall_descriptive"]
    print(f"Combined: {overall['strict_correct']}/{overall['records']} correct; "
          f"{overall['pairwise_both_correct']}/{overall['pairwise_total']} paired decisions correct", flush=True)


PRIOR = {
    "meta/muse-glimmer-30b": ("results/external/glimmer-main-final", "results/external/glimmer-curl-challenge"),
    "qwen/qwen3-235b-a22b-2507": ("results/external/qwen-main-final", "results/external/qwen-curl-challenge"),
    "deepseek/deepseek-v3.2": ("results/external/deepseek-v3-2-main-final", "results/external/deepseek-v3-2-curl-challenge"),
}


def prior_score(model: str, main_path: Path, curl_path: Path) -> dict:
    cases = validate()
    source = []
    all_rows = []
    for suite, path in zip(SUITES, (main_path, curl_path), strict=True):
        subset = [case for case in cases if case["source_suite"] == suite]
        metadata, rows = checked_rows(path.resolve(), subset, unified=False)
        if metadata.get("model") != model or metadata.get("runs_per_case") != 3:
            raise ValueError("Model or repeat count differs between sources")
        if metadata.get("case_count") != len(subset):
            raise ValueError("Source does not contain its full stratum")
        if metadata.get("evaluation_type") == "post-registration external-model comparison":
            if metadata.get("suite") != suite:
                raise ValueError("External result suite mismatch")
            external_eval.score(path.resolve())
        elif suite == "main-final":
            _, digest = grader.load_cases()
            if metadata.get("suite_sha256") != digest or metadata.get("selected_split") != "final":
                raise ValueError("Original main result protocol mismatch")
            if metadata.get("protocol_sha256") != grader.sha256(grader.PROTOCOL_PATH.read_text(encoding="utf-8")):
                raise ValueError("Original main protocol hash mismatch")
            if metadata.get("prompt_mode") != "minimal":
                raise ValueError("Original main prompt differs")
        else:
            if metadata.get("protocol_sha256") != external_eval.file_hash(ROOT / "challenge" / "protocol.json"):
                raise ValueError("Original curl protocol hash mismatch")
            if metadata.get("prompt_sha256") != grader.sha256(grader.MINIMAL_PROMPT):
                raise ValueError("Original curl prompt differs")
        source.append({"suite": suite, "path": path.relative_to(ROOT).as_posix(), "metadata": metadata})
        all_rows.extend(rows)
    return {"model": model, "sources": source, "metrics": summarize(all_rows, cases)}


def report(output: Path) -> dict:
    cases = validate()
    models = []
    for model, (main, curl) in PRIOR.items():
        slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
        unified_candidates = [
            ROOT / "results" / "unified" / f"openrouter-{slug}",
            ROOT / "results" / "unified" / slug,
        ]
        found_unified = None
        for candidate in unified_candidates:
            if (
                (candidate / "raw_outputs.jsonl").exists()
                and (candidate / "metadata.json").exists()
                and (candidate / "summary.json").exists()
            ):
                found_unified = candidate
                break
        if found_unified is not None:
            metadata, rows = checked_rows(found_unified, cases, unified=True)
            models.append({
                "model": model,
                "sources": [{"suite": "unified", "path": found_unified.relative_to(ROOT).as_posix(), "metadata": metadata}],
                "metrics": summarize(rows, cases),
            })
        elif (ROOT / main / "metadata.json").exists() and (ROOT / curl / "metadata.json").exists():
            models.append(prior_score(model, ROOT / main, ROOT / curl))
    result = {
        "benchmark_id": "grounded-action-authorization-v1-unified",
        "provenance": "Assembled after original and follow-up model runs; curl stress family was selected after a development failure.",
        "benchmark_manifest_sha256": external_eval.file_hash(MANIFEST),
        "scored_cases": 18,
        "task_pairs": 9,
        "always_block_baseline": summarize(
            [
                {"run": repeat, "case_id": case["id"], "expected": case["expected"], "predicted": "block"}
                for repeat in range(1, 4)
                for case in cases
            ],
            cases,
        ),
        "models": models,
    }
    if ROOT not in output.parents:
        raise ValueError("Report must stay inside this benchmark folder")
    grader.write_json(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    execute = commands.add_parser("run")
    execute.add_argument("--model", required=True)
    execute.add_argument("--api-base", default="https://openrouter.ai/api/v1")
    execute.add_argument("--api-key-env", default="EVAL_API_KEY")
    execute.add_argument("--runs", type=int, default=3)
    execute.add_argument("--temperature", type=float, default=0.0)
    execute.add_argument("--omit-temperature", action="store_true")
    execute.add_argument("--max-tokens", type=int, default=2048)
    execute.add_argument("--token-param", choices=("max_tokens", "max_completion_tokens"), default="max_tokens")
    execute.add_argument("--reasoning-effort")
    execute.add_argument("--timeout", type=int, default=90)
    execute.add_argument("--output", required=True)
    execute.add_argument("--confirm-spend", action="store_true")
    scoring = commands.add_parser("score")
    scoring.add_argument("--results", required=True)
    combined = commands.add_parser("report")
    combined.add_argument("--output", default="results/unified/scorecard.json")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            cases = validate()
            print(grader.canonical({"status": "valid", "cases": len(cases), "pairs": 9, "strata": {"main-final": 12, "curl-challenge": 6}}))
        elif args.command == "run":
            run(args)
        elif args.command == "score":
            print(grader.canonical(score_unified(Path(args.results).resolve())["metrics"]))
        else:
            result = report((ROOT / args.output).resolve())
            print(grader.canonical({model["model"]: model["metrics"]["overall_descriptive"]["strict_correct"] for model in result["models"]}))
    except (ValueError, FileNotFoundError, json.JSONDecodeError, RuntimeError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
