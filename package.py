"""Create a minimal micro1 ZIP with a README at the archive root."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

import unified_eval

ROOT = Path(__file__).resolve().parent
SOURCES = (
    ".env.example",
    "README.md",
    "SCORING.md",
    "TASKS.md",
    "assets/deepseek_v3_2_result.png",
    "assets/final_scorecard.png",
    "assets/muse_glimmer_30b_result.png",
    "assets/qwen3_235b_result.png",
    "benchmark_cases.json",
    "benchmark_manifest.json",
    "ORACLE_AUDIT.md",
    "challenge/README.md",
    "challenge/cases.json",
    "challenge/oracle_results.json",
    "challenge/protocol.json",
    "challenge/run.py",
    "challenge/verify_curl.py",
    "development.json",
    "final.json",
    "eval.py",
    "external_eval.py",
    "package.py",
    "policy.md",
    "protocol.json",
    "requirements.txt",
    "run_eval.py",
    "results/unified/scorecard.json",
    "test_eval.py",
    "test_challenge.py",
    "test_external_eval.py",
    "test_unified_eval.py",
    "test_run_eval.py",
    "unified_eval.py",
)
RESULTS = (
    "metadata.json",
    "raw_outputs.jsonl",
    "summary.json",
)
MAX_BYTES = 50 * 1024 * 1024
RESULT_DIRS = (
    "results/external/glimmer-main-final",
    "results/external/glimmer-curl-challenge",
    "results/external/qwen-main-final",
    "results/external/qwen-curl-challenge",
    "results/external/deepseek-v3-2-main-final",
    "results/external/deepseek-v3-2-curl-challenge",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_result_dirs() -> list[Path]:
    dirs: list[Path] = []
    unified_base = ROOT / "results" / "unified"
    if unified_base.exists():
        for item in sorted(unified_base.iterdir()):
            if item.is_dir() and all((item / name).exists() for name in RESULTS):
                dirs.append(item)
    for rel in RESULT_DIRS:
        p = ROOT / rel
        if p.exists() and all((p / name).exists() for name in RESULTS) and p not in dirs:
            dirs.append(p)
    return dirs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-url", help="Publicly accessible walkthrough URL")
    parser.add_argument("--output", default="dist/micro1-evaluation-draft.zip")
    args = parser.parse_args()
    if args.video_url and not re.fullmatch(r"https://\S+", args.video_url):
        parser.error("--video-url must be an HTTPS URL")
    unified_eval.report(ROOT / "results" / "unified" / "scorecard.json")
    results_dirs = find_result_dirs()
    output = (ROOT / args.output).resolve()
    if ROOT not in output.parents:
        parser.error("--output must be inside this benchmark folder")
    files: dict[str, bytes] = {}
    for name in SOURCES:
        path = ROOT / name
        files[name] = path.read_bytes()
    for results_dir in results_dirs:
        for name in RESULTS:
            path = results_dir / name
            files[f"{results_dir.relative_to(ROOT).as_posix()}/{name}"] = path.read_bytes()
    readme = files["README.md"].decode("utf-8")
    if (
        any(
            marker in readme
            for marker in (
                "RESULTS_HEADLINE_PENDING",
                "RESULTS_SUMMARY_PENDING",
                "FAILURE_ANALYSIS_PENDING",
            )
        )
    ):
        parser.error("Complete the measured results and failure analysis before packaging")
    if args.video_url:
        readme = readme.replace(
            "> **[Watch on YouTube / Google Drive / Loom — link to be added after recording]**",
            f"> **[Watch the public video]({args.video_url})**",
        )
    files["README.md"] = readme.encode("utf-8")
    for results_dir in results_dirs:
        metadata = json.loads((results_dir / "metadata.json").read_text(encoding="utf-8"))
        relative = results_dir.relative_to(ROOT).as_posix()
        eval_type = metadata.get("evaluation_type")
        if eval_type == "unified post-registration release benchmark":
            if metadata.get("case_count") != 18:
                parser.error(f"Unified result must contain 18 cases: {relative}")
        elif eval_type == "post-registration external-model comparison":
            if metadata.get("suite") not in ("main-final", "curl-challenge") or metadata.get("case_count") not in (6, 12):
                parser.error("External result must be a complete scored-suite comparison")
        elif relative.startswith("results/") and not relative.startswith("results/external/"):
            if metadata.get("case_count") != 12 or metadata.get("selected_split") != "final":
                parser.error("Original result must be a complete final-split evaluation")
        elif relative.startswith("challenge/results/"):
            if metadata.get("case_count") != 6:
                parser.error("Challenge result must contain all six registered cases")
        rows = (results_dir / "raw_outputs.jsonl").read_text(encoding="utf-8").splitlines()
        if len(rows) != metadata["case_count"] * metadata.get("runs_per_case", 0):
            parser.error(f"Raw output count does not match the declared run count in {relative}")
        if any(json.loads(row).get("request_error") for row in rows):
            parser.error(f"Result contains a transport error in {relative}")
    manifest = {name: sha256(contents) for name, contents in sorted(files.items())}
    files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in sorted(files.items()):
            archive.writestr(name, contents)
    if output.stat().st_size > MAX_BYTES:
        output.unlink()
        parser.error("ZIP exceeds 50 MB")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            parser.error("ZIP verification failed")
    print(f"{output} ({output.stat().st_size:,} bytes, {len(files)} files)")
    if not args.video_url:
        print("Draft: add --video-url before final submission")


if __name__ == "__main__":
    main()
