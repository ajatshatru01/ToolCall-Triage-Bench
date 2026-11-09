"""Run the 18-case benchmark across open-weight models with one API key.

Paste your OpenRouter API key once; the launcher runs all models in
sequence: Muse Glimmer 30B, Qwen3 235B, and DeepSeek V3.2.
The key is passed only to child processes via an environment variable; it is
never written to a command, result file, or ZIP.

Usage:
    python run_eval.py                     # interactive: prompts for key, runs all 3
    python run_eval.py --runs 1            # 1 repeat instead of 3 (faster / cheaper)
    python run_eval.py --model meta/muse-glimmer-30b   # run only one specific model
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── ANSI colour palette ──────────────────────────────────────────────────────

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    CYAN    = "\033[96m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    WHITE   = "\033[97m"
    # composed
    OK      = "\033[92m"   # correct / success
    WARN    = "\033[93m"   # review / caution
    ERR     = "\033[91m"   # error / parse fail
    BLOCK   = "\033[94m"   # block decision (blue so it doesn't feel alarming when right)
    HEAD    = "\033[1;96m" # section headers
    FAINT   = "\033[2m"


def _enable_ansi() -> None:
    """Enable ANSI escape codes on Windows 10+ consoles."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        pass


def _strip(text: str) -> str:
    """Remove ANSI codes (for environments that don't support them)."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def cprint(text: str, end: str = "\n", file=None) -> None:
    out = file or sys.stdout
    out.write(text + end)
    out.flush()


def cursor_up(n: int = 1) -> None:
    sys.stdout.write(f"\033[{n}A\033[2K")
    sys.stdout.flush()


# ── Model registry ───────────────────────────────────────────────────────────

MODELS: list[dict] = [
    {
        "id":               "meta/muse-glimmer-30b",
        "label":            "Muse Glimmer 30B",
        "omit_temperature": False,
    },
    {
        "id":               "qwen/qwen3-235b-a22b-2507",
        "label":            "Qwen3 235B",
        "omit_temperature": False,
    },
    {
        "id":               "deepseek/deepseek-v3.2",
        "label":            "DeepSeek V3.2",
        "omit_temperature": True,   # DeepSeek rejects the temperature parameter
    },
]

MODEL_IDS = [m["id"] for m in MODELS]


# ── Path helpers ─────────────────────────────────────────────────────────────

def default_output(model_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")
    return f"results/unified/openrouter-{slug}"


def build_command(model: dict, runs: int, output: str) -> list[str]:
    cmd = [
        sys.executable, "unified_eval.py", "run",
        "--model",   model["id"],
        "--runs",    str(runs),
        "--output",  output,
        "--confirm-spend",
    ]
    if model["omit_temperature"]:
        cmd.append("--omit-temperature")
    return cmd


# ── API key ───────────────────────────────────────────────────────────────────

def api_key(*, force_prompt: bool) -> str:
    for name in ("OPENROUTER_API_KEY", "EVAL_API_KEY"):
        val = os.environ.get(name, "")
        if val and not force_prompt:
            return val
    key = getpass.getpass(
        f"  {C.CYAN}Paste your OpenRouter API key{C.RESET} (input is hidden): "
    ).strip()
    if not key:
        raise ValueError("API key cannot be empty.")
    return key


def key_source() -> str:
    """Return a display string showing whether a key is already set."""
    for name in ("OPENROUTER_API_KEY", "EVAL_API_KEY"):
        if os.environ.get(name):
            return f"{C.GREEN}✓ {name}{C.RESET}"
    return f"{C.DIM}not set — will prompt{C.RESET}"


# ── Banner & headers ──────────────────────────────────────────────────────────

BANNER_WIDTH = 64

def _hline(ch: str = "─") -> str:
    return C.FAINT + ch * BANNER_WIDTH + C.RESET

def print_banner(models_to_run: list[dict], runs: int) -> None:
    total_calls = 18 * runs * len(models_to_run)
    cost_lo = total_calls * 0.00015
    cost_hi = total_calls * 0.00060

    top    = "╔" + "═" * (BANNER_WIDTH - 2) + "╗"
    bot    = "╚" + "═" * (BANNER_WIDTH - 2) + "╝"
    spacer = "║" + " " * (BANNER_WIDTH - 2) + "║"

    def row(text: str) -> str:
        plain = _strip(text)
        pad   = BANNER_WIDTH - 2 - len(plain)
        return "║ " + text + " " * max(pad - 1, 0) + "║"

    cprint("")
    cprint(C.CYAN + top + C.RESET)
    cprint(C.CYAN + spacer + C.RESET)
    cprint(C.CYAN + row(C.BOLD + "  Grounded Action Authorization Benchmark" + C.RESET) + C.RESET)
    cprint(C.CYAN + row(f"  OpenRouter  ·  {len(models_to_run)} models  ·  18 cases  ·  {runs} repeats each") + C.RESET)
    cprint(C.CYAN + spacer + C.RESET)
    cprint(C.CYAN + bot + C.RESET)
    cprint("")

    cprint(f"  {C.BOLD}Models queued:{C.RESET}")
    for i, m in enumerate(models_to_run, 1):
        label = m["label"].ljust(20)
        cprint(f"    {C.FAINT}{i}.{C.RESET}  {C.WHITE}{label}{C.RESET}  {C.DIM}{m['id']}{C.RESET}")

    cprint("")
    cprint(f"  {C.FAINT}Total API calls:{C.RESET}  {C.BOLD}{total_calls}{C.RESET}"
           f"  {C.FAINT}(18 cases × {runs} repeats × {len(models_to_run)} models){C.RESET}")
    cprint(f"  {C.FAINT}Estimated cost:{C.RESET}   "
           f"{C.YELLOW}~US${cost_lo:.3f} – US${cost_hi:.3f}{C.RESET}"
           f"  {C.FAINT}(rough; depends on routing){C.RESET}")
    cprint(f"  {C.FAINT}API key:{C.RESET}          {key_source()}")
    cprint("")


def print_model_header(model: dict, index: int, total: int) -> None:
    cprint("")
    cprint(_hline())
    cprint(
        f"  {C.HEAD}[{index}/{total}]  {model['label']}{C.RESET}"
        f"  {C.FAINT}·  {model['id']}{C.RESET}"
    )
    cprint(_hline())
    cprint("")


def print_model_footer(label: str, result: dict | None, exitcode: int) -> None:
    cprint("")
    if exitcode != 0:
        cprint(f"  {C.ERR}✗  Failed (exit {exitcode}) — re-run to resume{C.RESET}")
    elif result is None:
        cprint(f"  {C.WARN}⚠  Run completed but summary not available{C.RESET}")
    else:
        overall = result["overall_descriptive"]
        correct = overall["strict_correct"]
        total   = overall["records"]
        pairs_c = overall["pairwise_both_correct"]
        pairs_t = overall["pairwise_total"]
        by_src  = result.get("by_source", {})
        main_s  = by_src.get("main-final", {})
        curl_s  = by_src.get("curl-challenge", {})

        pct = correct / total * 100 if total else 0
        bar_filled = round(correct / total * 24) if total else 0
        bar = C.GREEN + "█" * bar_filled + C.FAINT + "░" * (24 - bar_filled) + C.RESET

        cprint(f"  {bar}  {C.BOLD}{correct}/{total}{C.RESET}"
               f"  {C.FAINT}({pct:.1f}%){C.RESET}")
        cprint(
            f"  {C.FAINT}Pairs:{C.RESET} {C.BOLD}{pairs_c}/{pairs_t}{C.RESET}"
            f"  {C.FAINT}·  main-final:{C.RESET} {main_s.get('strict_correct','?')}/{main_s.get('records','?')}"
            f"  {C.FAINT}·  curl-stress:{C.RESET} {curl_s.get('strict_correct','?')}/{curl_s.get('records','?')}"
        )
    cprint("")


# ── Decision formatting ───────────────────────────────────────────────────────

_DECISION_FMT = {
    "allow":  (C.GREEN,   "✓", "allow "),
    "review": (C.YELLOW,  "~", "review"),
    "block":  (C.BLUE,    "●", "block "),
    None:     (C.ERR,     "✗", "ERROR "),
}


def _fmt_decision(raw: str) -> str:
    """Colour-format a decision string from the unified_eval output line."""
    # Line format: "{run}/{runs} {case_id}: {decision_or_error}"
    m = re.fullmatch(r"(\d+/\d+)\s+([^\s:]+):\s+(.*)", raw.strip())
    if not m:
        return f"  {C.FAINT}{raw.strip()}{C.RESET}"

    run_tag  = m.group(1)
    case_id  = m.group(2)
    decision = m.group(3).strip().lower()

    # Normalise decision token
    token = None
    for key in ("allow", "review", "block"):
        if key in decision:
            token = key
            break

    colour, symbol, label = _DECISION_FMT.get(token, _DECISION_FMT[None])

    run_str  = f"{C.FAINT}r{run_tag.split('/')[0]}{C.RESET}"
    case_str = f"{C.WHITE}{case_id:<26}{C.RESET}"
    dec_str  = f"{colour}{C.BOLD}{symbol} {label}{C.RESET}"

    return f"    {run_str}  {case_str}→  {dec_str}"


# ── Core: stream one model ────────────────────────────────────────────────────

def run_model(model: dict, runs: int, output: str, child_env: dict) -> tuple[int, dict | None]:
    """Launch unified_eval for one model; stream output with live formatting.

    Returns (exit_code, summary_metrics_or_None).
    """
    cmd        = build_command(model, runs, output)
    total_line = 18 * runs   # expected case-result lines
    done       = 0
    has_bar    = False        # whether a progress bar line is currently on screen

    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for raw_line in proc.stdout:  # type: ignore[union-attr]
        line = raw_line.rstrip()
        if not line:
            continue

        # Erase the progress bar before printing anything new
        if has_bar:
            cursor_up(1)
            has_bar = False

        # Detect: is this a scored case line?
        is_case = bool(re.match(r"\d+/\d+\s+\S+:", line))

        if is_case:
            done += 1
            cprint(_fmt_decision(line))

            # Draw fresh progress bar
            filled = round(done / total_line * 30)
            bar    = C.GREEN + "█" * filled + C.FAINT + "░" * (30 - filled) + C.RESET
            pct    = done / total_line * 100
            cprint(f"  {bar}  {C.FAINT}{done}/{total_line}  ({pct:.0f}%){C.RESET}")
            has_bar = True

        elif line.startswith("Combined:"):
            # Final summary line from unified_eval — render nicely
            cprint(f"  {C.HEAD}{line}{C.RESET}")
        else:
            # Other informational lines (e.g. errors, retries)
            cprint(f"  {C.FAINT}{line}{C.RESET}")

    proc.wait()
    exitcode = proc.returncode

    # Erase the progress bar now that the run is finished
    if has_bar:
        cursor_up(1)

    # Load the summary if the run succeeded
    summary: dict | None = None
    summary_path = ROOT / output / "summary.json"
    if exitcode == 0 and summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))["metrics"]
        except (KeyError, json.JSONDecodeError):
            pass

    return exitcode, summary


# ── Final scorecard table ─────────────────────────────────────────────────────

def print_scorecard(
    models_run: list[dict],
    scores: dict[str, dict | None],
    runs: int,
) -> None:
    cprint("")
    cprint(_hline("═"))
    cprint(f"  {C.HEAD}RESULTS  ·  {datetime.date.today().strftime('%d %b %Y')}{C.RESET}")
    cprint(_hline("═"))
    cprint("")

    # Column widths
    W_MODEL  = 24
    W_SCORE  = 10
    W_PAIRS  = 8
    W_MAIN   = 12
    W_CURL   = 12

    def th(text: str, width: int) -> str:
        return f" {text.center(width)} "

    sep = (
        "  ┌" + "─" * (W_MODEL + 2) + "┬"
              + "─" * (W_SCORE + 2) + "┬"
              + "─" * (W_PAIRS + 2) + "┬"
              + "─" * (W_MAIN  + 2) + "┬"
              + "─" * (W_CURL  + 2) + "┐"
    )
    mid = (
        "  ├" + "─" * (W_MODEL + 2) + "┼"
              + "─" * (W_SCORE + 2) + "┼"
              + "─" * (W_PAIRS + 2) + "┼"
              + "─" * (W_MAIN  + 2) + "┼"
              + "─" * (W_CURL  + 2) + "┤"
    )
    bot = (
        "  └" + "─" * (W_MODEL + 2) + "┴"
              + "─" * (W_SCORE + 2) + "┴"
              + "─" * (W_PAIRS + 2) + "┴"
              + "─" * (W_MAIN  + 2) + "┴"
              + "─" * (W_CURL  + 2) + "┘"
    )

    header = (
        "  │"
        + th("Model", W_MODEL) + "│"
        + th("Correct", W_SCORE) + "│"
        + th("Pairs", W_PAIRS) + "│"
        + th("Main-Final", W_MAIN) + "│"
        + th("Curl-Stress", W_CURL) + "│"
    )

    cprint(C.FAINT + sep + C.RESET)
    cprint(C.BOLD + header + C.RESET)
    cprint(C.FAINT + mid + C.RESET)

    max_correct = 18 * runs
    for m in models_run:
        s = scores.get(m["id"])
        if s is None:
            row_data = [m["label"][:W_MODEL], "FAILED", "—", "—", "—"]
            colour   = C.ERR
        else:
            ov   = s["overall_descriptive"]
            bs   = s.get("by_source", {})
            mn   = bs.get("main-final", {})
            cu   = bs.get("curl-challenge", {})
            correct = ov["strict_correct"]
            total   = ov["records"]
            pairs_c = ov["pairwise_both_correct"]
            pairs_t = ov["pairwise_total"]
            m_c  = mn.get("strict_correct", "?")
            m_t  = mn.get("records", "?")
            cu_c = cu.get("strict_correct", "?")
            cu_t = cu.get("records", "?")

            pct = correct / total * 100 if total else 0
            row_data = [
                m["label"][:W_MODEL],
                f"{correct}/{total} ({pct:.0f}%)",
                f"{pairs_c}/{pairs_t}",
                f"{m_c}/{m_t}",
                f"{cu_c}/{cu_t}",
            ]
            colour = C.GREEN if correct / total >= 0.85 else (C.YELLOW if correct / total >= 0.70 else C.RED)

        def cell(text: str, width: int, col: str = C.RESET) -> str:
            return f" {col}{text.center(width)}{C.RESET} "

        row = (
            "  │"
            + cell(row_data[0], W_MODEL, colour)
            + "│" + cell(row_data[1], W_SCORE)
            + "│" + cell(row_data[2], W_PAIRS)
            + "│" + cell(row_data[3], W_MAIN)
            + "│" + cell(row_data[4], W_CURL)
            + "│"
        )
        cprint(row)

    cprint(C.FAINT + bot + C.RESET)
    cprint("")
    cprint(
        f"  {C.FAINT}Raw outputs{C.RESET}  →  results/external/"
        f"  {C.FAINT}·  Scorecard{C.RESET}  →  results/unified/scorecard.json"
    )
    cprint("")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    _enable_ansi()

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model",
        choices=MODEL_IDS,
        default=None,
        metavar="MODEL_ID",
        help=f"Run only this model ID. Choices: {', '.join(MODEL_IDS)}",
    )
    parser.add_argument("--runs", type=int, default=3, help="Repeats per case (1–10; default 3)")
    parser.add_argument("--prompt-key", action="store_true",
                        help="Force key prompt even if an env-var is set")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt")
    args = parser.parse_args()

    if not 1 <= args.runs <= 10:
        print(f"{C.ERR}Error: --runs must be between 1 and 10{C.RESET}", file=sys.stderr)
        return 2

    models_to_run = [m for m in MODELS if args.model is None or m["id"] == args.model]

    # ── Banner ────────────────────────────────────────────────────────────────
    print_banner(models_to_run, args.runs)

    # ── Confirm ───────────────────────────────────────────────────────────────
    if not args.yes:
        try:
            answer = input(
                f"  {C.BOLD}Run paid evaluation for all models?{C.RESET} [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            cprint(f"\n  {C.FAINT}Cancelled.{C.RESET}")
            return 0
        if answer not in ("y", "yes"):
            cprint(f"  {C.FAINT}Cancelled before any API call.{C.RESET}\n")
            return 0

    # ── API key ───────────────────────────────────────────────────────────────
    try:
        key = api_key(force_prompt=args.prompt_key)
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        cprint(f"\n  {C.ERR}✗ {exc}{C.RESET}", end="\n")
        return 2

    child_env              = os.environ.copy()
    child_env["EVAL_API_KEY"] = key

    cprint(f"\n  {C.GREEN}✓ Key accepted{C.RESET}  {C.FAINT}(never written to disk){C.RESET}")

    # ── Run each model ────────────────────────────────────────────────────────
    scores:    dict[str, dict | None] = {}
    exitcodes: dict[str, int]         = {}

    for idx, model in enumerate(models_to_run, 1):
        output = default_output(model["id"])
        print_model_header(model, idx, len(models_to_run))

        exitcode, summary = run_model(model, args.runs, output, child_env)
        exitcodes[model["id"]] = exitcode
        scores[model["id"]]    = summary

        print_model_footer(model["label"], summary, exitcode)

    # ── Final scorecard ───────────────────────────────────────────────────────
    print_scorecard(models_to_run, scores, args.runs)

    # ── Status summary ────────────────────────────────────────────────────────
    all_ok = all(c == 0 for c in exitcodes.values())
    if not all_ok:
        failed = [m["label"] for m in models_to_run if exitcodes.get(m["id"], 1) != 0]
        cprint(
            f"  {C.WARN}⚠  {len(failed)} model(s) failed: {', '.join(failed)}\n"
            f"     Re-run with the same --model flag to resume.{C.RESET}\n",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
