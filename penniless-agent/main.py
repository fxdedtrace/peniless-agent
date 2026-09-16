#!/usr/bin/env python3
"""
Penniless Agent — autonomous, self-sovereign gig-solving agent.

Runs free on GitHub Actions (or any cron). Each run:
  1. Reads DEEPSEEK_API_KEY and AGENT_WALLET from the environment.
  2. Discovers a micro-task from a public bounty source:
       a) ugig.net public gigs API
       b) task-bounty.com public tasks API
       c) deterministic local fallback task (always available)
  3. Asks DeepSeek for a production-ready solution.
  4. Appends an x402-style payment invoice (USDC, Base or Solana) so an
     x402-compatible client can settle payment to AGENT_WALLET.
  5. Writes the full solution to solutions/ and a summary entry to status.md.

stdlib only — no pip dependencies, nothing to break on the Actions runner.

CLI:
    python main.py            # normal run (one task per invocation)
    python main.py --dry-run  # discovery + logging + invoice, no LLM call,
                              # no dedup-state writes (safe local test)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
SOLUTIONS_DIR = BASE_DIR / "solutions"
STATUS_FILE = BASE_DIR / "status.md"
ARCHIVE_FILE = BASE_DIR / "status_archive.md"
STATE_FILE = BASE_DIR / "run_state.json"
MAX_STATUS_ENTRIES = 200
PREVIEW_CHARS = 400
DRY_RUN_TAG = "[DRY RUN] "

# ---------------------------------------------------------------------------
# USDC constants (canonical mainnet addresses for the x402 invoice)
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

HTTP_TIMEOUT = 20
LLM_TIMEOUT = 120
MAX_RETRIES = 3

SOLUTION_PRICE_USDC = 1.00  # invoice amount the agent charges per solution

# ---------------------------------------------------------------------------
# Fallback tasks: used when every remote source is unreachable. Deterministic
# rotation keyed on 6-hour UTC slots so consecutive runs differ.
FALLBACK_TASKS = [
    {"id": "fb-csv-dedupe",
     "title": "Write a Python script that deduplicates a CSV by email column",
     "description": ("Produce a single-file Python 3 script that reads a CSV, "
                     "removes duplicate rows keyed on a case-insensitive email "
                     "column, writes the result, and prints a summary of rows "
                     "removed. Include argument parsing and docstrings.")},
    {"id": "fb-readme",
     "title": "Draft a professional README for a CLI backup tool",
     "description": ("Write a complete README.md for a fictional CLI tool "
                     "`snapvault` that snapshots folders to tar.gz archives "
                     "with retention pruning. Include install, quickstart, "
                     "flags table, and exit-code reference.")},
    {"id": "fb-cron-health",
     "title": "Write a cron-safe healthcheck shell script for a web endpoint",
     "description": ("Produce a POSIX shell script that curls an endpoint, "
                     "retries with exponential backoff, logs the result, and "
                     "exits non-zero after N failures. No bashisms.")},
    {"id": "fb-json-schema",
     "title": "Generate a JSON Schema for a user-profile API payload",
     "description": ("Write a draft-07 JSON Schema for a profile object "
                     "(username, email, birthday, avatar_url, preferences) "
                     "with sensible formats, required fields, and examples.")},
    {"id": "fb-git-hook",
     "title": "Create a pre-commit hook that blocks large files and secrets",
     "description": ("Write a portable pre-commit hook script that rejects "
                     "staged files over 1 MB and files matching common secret "
                     "patterns (AWS keys, private key headers).")},
    {"id": "fb-regex-cookbook",
     "title": "Build a regex cookbook for log-parsing in Python",
     "description": ("Create a markdown cookbook with 10 tested Python regex "
                     "recipes for common log lines (timestamps, IPs, levels) "
                     "each with an example input and expected output.")},
]


def log(msg: str) -> None:
    print(f"[penniless {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


class ConfigError(RuntimeError):
    """Fatal, user-fixable configuration problem."""


# ---------------------------------------------------------------------------
# Configuration

@dataclass
class Config:
    api_key: str
    wallet: str
    network: str
    model: str
    max_tokens: int
    dry_run: bool

    @property
    def usdc_asset(self) -> str:
        return USDC_BASE if self.network == "base" else USDC_SOLANA_MINT


def load_config(dry_run: bool) -> Config:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    wallet = (os.environ.get("AGENT_WALLET") or "").strip()
    network = (os.environ.get("AGENT_NETWORK") or "base").strip().lower()
    model = (os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash").strip()
    try:
        max_tokens = int(os.environ.get("PENNILESS_MAX_TOKENS") or "2048")
    except ValueError:
        max_tokens = 2048

    if network not in ("base", "solana"):
        raise ConfigError(
            f"AGENT_NETWORK must be 'base' or 'solana' (got {network!r})."
        )
    if not wallet:
        raise ConfigError(
            "AGENT_WALLET is not set. Add your public USDC payout address "
            "(0x... for Base, or your Solana address) as a secret/env var."
        )
    if not dry_run and not api_key:
        raise ConfigError(
            "DEEPSEEK_API_KEY is not set. Add it as a GitHub secret or export "
            "it locally. (Use --dry-run to test without it.)"
        )
    return Config(api_key=api_key, wallet=wallet, network=network,
                  model=model, max_tokens=max_tokens, dry_run=dry_run)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib, resilient)

def _header(headers: dict, name: str) -> Optional[str]:
    """Case-insensitive header lookup on a plain dict of headers."""
    lname = name.lower()
    for k, v in headers.items():
        if str(k).lower() == lname:
            return str(v).split(",")[0].strip()
    return None


def _http_request(url: str, *, method: str = "GET", body: Optional[dict] = None,
                  headers: Optional[dict] = None,
                  timeout: int = HTTP_TIMEOUT) -> tuple[int, dict, str]:
    """One HTTP request. Returns (status, response_headers, text) for any
    status, including 4xx/5xx (callers branch on status)."""
    data = None
    hdrs = {"User-Agent": "penniless-agent/1.0 (+github-actions)",
            "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        payload = ""
        try:
            payload = (e.read() or b"").decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, dict(e.headers or {}), payload


def _backoff_wait(headers: dict, attempt: int, cap: int) -> float:
    retry_after = _header(headers, "Retry-After")
    try:
        wait = float(retry_after) if retry_after else min(2 ** attempt, cap)
    except ValueError:
        wait = min(2 ** attempt, cap)
    return wait + random.uniform(0, 1)


def http_get_json(url: str, *, retries: int = MAX_RETRIES,
                  timeout: int = HTTP_TIMEOUT) -> Any:
    """GET a JSON URL with retries/backoff on 429/5xx and network glitches.
    Returns parsed JSON, or None when the resource is unavailable."""
    last_err: Optional[str] = None
    for attempt in range(1, retries + 1):
        try:
            status, resp_headers, text = _http_request(url, timeout=timeout)
            if status == 200:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    log(f"  {url} -> 200 but invalid JSON")
                    return None
            if status in (429, 500, 502, 503, 504):
                last_err = f"HTTP {status}"
                wait = _backoff_wait(resp_headers, attempt, 15)
                log(f"  {url} -> {last_err}, retrying in {wait:.1f}s ({attempt}/{retries})")
                time.sleep(wait)
                continue
            last_err = f"HTTP {status}"
            break  # permanent (404, 403, ...): don't hammer
        except (urllib.error.URLError, socket.timeout, ConnectionError,
                TimeoutError, OSError) as e:
            last_err = type(e).__name__
            wait = min(2 ** attempt, 15) + random.uniform(0, 1)
            log(f"  {url} -> network error ({last_err}), retrying in {wait:.1f}s ({attempt}/{retries})")
            time.sleep(wait)
            continue
    log(f"  giving up on {url} ({last_err})")
    return None


# ---------------------------------------------------------------------------
# Task discovery

@dataclass
class Task:
    id: str
    title: str
    description: str
    source: str
    url: str
    budget: str


def _first(d: dict, *keys, default=None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _unwrap(payload: Any, *list_keys: str) -> list:
    """Accept a bare list, or a dict wrapping the list under common keys."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in list_keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def discover_ugig() -> list[Task]:
    """ugig.net public gigs. GET /api/gigs requires no auth."""
    payload = http_get_json("https://ugig.net/api/gigs?limit=20")
    items = _unwrap(payload, "gigs", "data", "results")
    tasks = []
    for it in items:
        gid = _first(it, "id", "gig_id", "uuid")
        title = _first(it, "title", "name")
        if not gid or not title:
            continue
        desc = str(_first(it, "description", "summary", default="") or "")[:1500]
        bmin, bmax = _first(it, "budget_min", "budgetMin"), _first(it, "budget_max", "budgetMax")
        budget = _first(it, "budget", "budget_display") or (
            f"{bmin}-{bmax}" if bmin and bmax else (bmin or bmax or "n/a"))
        coin = _first(it, "payment_coin", "paymentCoin", default="")
        tasks.append(Task(
            id=f"ugig:{gid}",
            title=str(title)[:200],
            description=f"{desc}\n\nBudget: {budget} {coin}".strip(),
            source="ugig.net",
            url=str(_first(it, "url", "slug", default=f"https://ugig.net/gig/{gid}")),
            budget=f"{budget} {coin}".strip(),
        ))
    return tasks


def discover_taskbounty() -> list[Task]:
    """task-bounty.com public bounty board. GET /api/v1/tasks, no auth."""
    payload = http_get_json("https://www.task-bounty.com/api/v1/tasks")
    items = _unwrap(payload, "tasks", "data", "results")
    tasks = []
    for it in items:
        tid = _first(it, "id", "task_id")
        title = _first(it, "title", "summary")
        if not tid or not title:
            continue
        desc = str(_first(it, "description", "body", default="") or "")[:1500]
        amount = _first(it, "amount", "price", "bounty", "reward", default="n/a")
        lang = _first(it, "lang", "language", default="")
        tasks.append(Task(
            id=f"taskbounty:{tid}",
            title=str(title)[:200],
            description=f"{desc}\n\nLanguage: {lang}".strip(),
            source="task-bounty.com",
            url=str(_first(it, "url", "issue_url",
                           default="https://www.task-bounty.com/browse")),
            budget=f"~${amount}",
        ))
    return tasks


def discover_fallback() -> Task:
    day_slot = int(datetime.now(timezone.utc).strftime("%Y%m%d%H")) // 6
    fb = FALLBACK_TASKS[day_slot % len(FALLBACK_TASKS)]
    return Task(id=fb["id"], title=fb["title"], description=fb["description"],
                source="local-fallback",
                url="https://github.com/features/actions", budget="self-assigned")


def discover_task() -> Task:
    """Try each source in order, skipping tasks already completed in past runs."""
    done = load_state()["completed"]
    for name, fn in (("ugig.net", discover_ugig),
                     ("task-bounty.com", discover_taskbounty)):
        try:
            log(f"Discovering tasks from {name} ...")
            tasks = [t for t in fn() if t.id not in done]
        except Exception as e:  # a source failing must never kill the run
            log(f"  {name} discovery failed: {type(e).__name__}: {e}")
            tasks = []
        if tasks:
            log(f"  {len(tasks)} new candidate task(s) from {name}")
            return tasks[0]
    log("All remote sources unreachable/empty — using local fallback task.")
    return discover_fallback()


# ---------------------------------------------------------------------------
# Run state (dedup across 6-hour cron runs)

def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(state.get("completed"), dict):
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"completed": {}, "runs": 0}


def save_state(state: dict) -> None:
    completed = state.get("completed", {})
    if len(completed) > 500:  # keep the file small: drop oldest half
        keep = dict(sorted(completed.items(), key=lambda kv: kv[1])[-250:])
        state["completed"] = keep
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# DeepSeek execution

MODEL_FALLBACKS = ["deepseek-chat", "deepseek-reasoner"]

SYSTEM_PROMPT = (
    "You are Penniless Agent, an autonomous senior engineer. You receive a "
    "small gig or micro-bounty and must return a production-ready solution: "
    "complete, runnable, well-documented, and honest about limitations.\n"
    "Output plain GitHub-flavored markdown with these sections:\n"
    "## Approach\n## Solution\n## How to verify\n## Limitations & next steps\n"
    "Do NOT include any payment, invoice, or wallet information — the harness "
    "appends the settlement block itself."
)


def call_deepseek(cfg: Config, task: Task) -> tuple[str, dict]:
    """Call the DeepSeek chat API with retries. Returns (content, usage).

    Retry policy:
      - 429/5xx/network errors: exponential backoff (honors Retry-After).
      - 400 with model-name rejection: immediately try the next alias.
      - 401/402/403: fatal ConfigError (account/key problem, user must fix).
    """
    user_msg = (
        f"Task source: {task.source}\n"
        f"Task title: {task.title}\n"
        f"Task URL: {task.url}\n"
        f"Task details:\n{task.description}\n\n"
        "Deliver the full solution now."
    )
    models_to_try = [cfg.model] + [m for m in MODEL_FALLBACKS if m != cfg.model]
    last_error = "unknown error"

    for model in models_to_try:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": cfg.max_tokens,
            "temperature": 0.3,
            "stream": False,
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                status, resp_headers, text = _http_request(
                    "https://api.deepseek.com/chat/completions",
                    method="POST", body=body,
                    headers={"Authorization": f"Bearer {cfg.api_key}"},
                    timeout=LLM_TIMEOUT,
                )
                if status == 200:
                    data = json.loads(text)
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {})
                    if model != cfg.model:
                        log(f"  note: succeeded with fallback model {model!r}; "
                            f"set DEEPSEEK_MODEL={model} to silence this")
                    return content, usage
                last_error = f"HTTP {status}: {text[:300]}"
                if status == 400 and "model" in text.lower():
                    log(f"  model {model!r} rejected — trying next alias")
                    break
                if status in (401, 402, 403):
                    raise ConfigError(f"DeepSeek API error {status}: {text[:300]}")
                if status == 429 or status >= 500:
                    wait = _backoff_wait(resp_headers, attempt, 30)
                    log(f"  DeepSeek {status}, retrying in {wait:.1f}s ({attempt}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                break  # other 4xx: no point retrying this model
            except ConfigError:
                raise
            except (urllib.error.URLError, socket.timeout, ConnectionError,
                    TimeoutError, OSError, json.JSONDecodeError,
                    KeyError, IndexError) as e:
                last_error = f"{type(e).__name__}: {e}"
                wait = min(2 ** attempt, 30) + random.uniform(0, 2)
                log(f"  DeepSeek request error ({last_error[:120]}), "
                    f"retrying in {wait:.1f}s ({attempt}/{MAX_RETRIES})")
                time.sleep(wait)
    raise RuntimeError(f"DeepSeek API failed after all retries: {last_error}")


# ---------------------------------------------------------------------------
# x402 payment invoice injection

def build_x402_invoice(cfg: Config, task: Task, run_id: str) -> str:
    """Build an x402-style payment-requirement block (this agent is the payee).

    The JSON mirrors the x402 'exact' scheme requirements object that an
    x402-compatible client would receive in a 402 PAYMENT-REQUIRED response,
    encoded per the spec (Base64 of the JSON payload).
    """
    micro_units = str(int(round(SOLUTION_PRICE_USDC * 1_000_000)))  # 6 decimals
    requirements = {
        "x402Version": 1,
        "scheme": "exact",
        "network": cfg.network,
        "payTo": cfg.wallet,
        "asset": {
            "symbol": "USDC",
            "decimals": 6,
            "contractAddress": cfg.usdc_asset,
        },
        "maxAmountRequired": micro_units,
        "resource": task.url,
        "description": f"Penniless Agent solution {run_id} for: {task.title}",
        "mimeType": "text/markdown",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(requirements, separators=(",", ":")).encode()
    ).decode().rstrip("=")

    return (
        "\n\n---\n\n"
        "## ⚡ x402 Payment Settlement (pay-to: agent)\n\n"
        f"| Field | Value |\n|---|---|\n"
        f"| Protocol | x402 v1 (exact scheme) |\n"
        f"| Network | `{cfg.network}` |\n"
        f"| Asset | USDC (`{cfg.usdc_asset}`) |\n"
        f"| Pay to | `{cfg.wallet}` |\n"
        f"| Amount | ${SOLUTION_PRICE_USDC:.2f} ({micro_units} base units) |\n"
        f"| Resource | {task.url} |\n\n"
        f"**PAYMENT-REQUIRED (base64):**\n\n```\n{encoded}\n```\n\n"
        "An x402-compatible client settles by signing this requirement and "
        "submitting it to a facilitator; funds land directly in the agent's "
        "non-custodial wallet. No intermediaries, no custody.\n"
    )


# ---------------------------------------------------------------------------
# status.md logging (newest entry first, rotating to status_archive.md)

STATUS_HEADER = (
    "# Penniless Agent — Status Log\n\n"
    "> Newest entries first. Maintained automatically by `main.py`.\n"
)


def _split_status(text: str) -> tuple[str, list[str]]:
    """Return (preamble, entries) where entries are newest-first markdown
    chunks starting with '## Run'."""
    if not text.startswith("# Penniless Agent"):
        return "", [text] if text.strip() else []
    lines = text.split("\n")
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or lines[i].startswith(">") or not lines[i].strip()):
        i += 1
    preamble, body = "\n".join(lines[:i]), "\n".join(lines[i:])
    parts = [p for p in re.split(r"\n(?=## Run )", body) if p.strip()]
    return preamble, parts


def append_status(entry_md: str) -> None:
    old_text = STATUS_FILE.read_text(encoding="utf-8") if STATUS_FILE.exists() else STATUS_HEADER
    preamble, entries = _split_status(old_text)
    entries.insert(0, entry_md.strip())
    if len(entries) > MAX_STATUS_ENTRIES:
        archived = entries[MAX_STATUS_ENTRIES:]
        entries = entries[:MAX_STATUS_ENTRIES]
        with ARCHIVE_FILE.open("a", encoding="utf-8") as f:
            if ARCHIVE_FILE.stat().st_size == 0:
                f.write("# Penniless Agent — Archived Status\n\n")
            f.write("\n\n".join(archived) + "\n")
        log(f"  rotated {len(archived)} old entries to {ARCHIVE_FILE.name}")
    STATUS_FILE.write_text(preamble.rstrip() + "\n\n" + "\n\n".join(entries) + "\n",
                           encoding="utf-8")


def fmt_usage(usage: dict) -> str:
    p, c = usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?")
    return f"{p} prompt / {c} completion tokens"


def solution_quality_ok(text: str) -> bool:
    """Cheap sanity gate: the model must have produced real content."""
    return len(text) >= 300 and "## Approach" in text


# ---------------------------------------------------------------------------
# Main run

def run_once(cfg: Config) -> int:
    run_ts = datetime.now(timezone.utc)
    task = discover_task()
    run_id = hashlib.sha1(f"{task.id}|{run_ts.isoformat()}".encode()).hexdigest()[:10]
    log(f"Task: {task.title!r} (id={task.id}, source={task.source}, run={run_id})")

    # 1. Generate solution (or stub in dry-run)
    usage: dict = {}
    if cfg.dry_run:
        solution = (
            "## Approach\n\n[DRY RUN] Solution generation skipped — no LLM call, "
            "no API spend. This entry verifies discovery, dedup inputs, invoice "
            "construction, and status logging.\n\n"
            "## Solution\n\n(none — dry run)\n\n"
            "## How to verify\n\nRun without --dry-run to produce a real solution.\n\n"
            "## Limitations & next steps\n\nThis is a harness test entry only.\n"
        )
        log("DRY RUN: skipping DeepSeek call")
    else:
        log(f"Calling DeepSeek ({cfg.model}) ...")
        solution, usage = call_deepseek(cfg, task)
        if not solution_quality_ok(solution):
            solution = solution.strip()  # keep it anyway; visible in the log
            log("  warning: solution failed quality heuristic (short/missing sections)")

    # 2. Inject x402 invoice
    full_solution = solution.rstrip() + build_x402_invoice(cfg, task, run_id)

    # 3. Persist full solution file
    SOLUTIONS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower())[:40].strip("-") or "task"
    sol_path = SOLUTIONS_DIR / f"{run_ts:%Y-%m-%d}_{run_id}_{slug}.md"
    sol_path.write_text(
        f"# {task.title}\n\n- Run: `{run_id}` · Source: {task.source} · {task.url}\n\n"
        + full_solution,
        encoding="utf-8",
    )
    rel_path = sol_path.relative_to(BASE_DIR).as_posix()
    log(f"Solution saved: {rel_path}")

    # 4. status.md entry (newest first)
    preview = re.sub(r"\s+", " ", solution.strip())[:PREVIEW_CHARS] + \
        ("…" if len(solution) > PREVIEW_CHARS else "")
    tag = DRY_RUN_TAG if cfg.dry_run else ""
    entry = (
        f"## Run `{run_id}` — {run_ts:%Y-%m-%d %H:%M UTC} {tag}\n"
        f"- **Source**: {task.source}\n"
        f"- **Task**: [{task.title}]({task.url}) (`{task.id}`)\n"
        f"- **Budget**: {task.budget}\n"
        f"- **Model**: {cfg.model}"
        + (f" · usage: {fmt_usage(usage)}" if usage else "") + "\n"
        f"- **Solution**: `{rel_path}`\n"
        f"- **Payment**: x402 USDC/{cfg.network} → `{cfg.wallet[:10]}…{cfg.wallet[-6:]}`\n"
        f"- **Preview**: {preview}\n"
    )
    append_status(entry)

    # 5. Dedup state (dry run stays read-only so the task remains available)
    if not cfg.dry_run:
        state = load_state()
        state["completed"][task.id] = run_ts.date().isoformat()
        state["runs"] = state.get("runs", 0) + 1
        save_state(state)

    log(f"Run {run_id} complete ({task.source}).")
    return 0


def record_failure(where: str, err: Exception) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        append_status(
            f"## Run `failure` — {ts}\n"
            f"- **Stage**: {where}\n"
            f"- **Error**: `{type(err).__name__}: {str(err)[:300]}`\n"
            f"- Next run will retry automatically (6-hour cron).\n"
        )
    except Exception:
        pass  # never let logging a failure crash the failure handler


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Penniless Agent run loop")
    parser.add_argument("--dry-run", action="store_true",
                        help="skip the LLM call and dedup writes; test discovery + logging")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(dry_run=args.dry_run)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    try:
        return run_once(cfg)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        record_failure("config", e)
        return 2
    except Exception as e:
        # Transient (network/LLM down) failures must NOT red-flag the cron:
        # log them, exit 0, and let the next scheduled run retry.
        log(f"Run failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        record_failure("execution", e)
        return 0


if __name__ == "__main__":
    sys.exit(main())
