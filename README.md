# Penniless Agent

Autonomous, self-sovereign, non-custodial gig-solving agent that runs **free** on GitHub Actions.

Every 6 hours it wakes up, finds a micro-task from public bounty APIs, has DeepSeek write a production-ready solution, stamps an **x402 payment invoice** (USDC on Base or Solana) payable to your wallet, and logs everything back to the repo.

No servers. No hosting bills. Only cost: one LLM call per run (fractions of a cent).

---

## Architecture

```
GitHub Actions cron (every 6h)
        │
        ▼
   main.py
        │
        ├─ 1. Read DEEPSEEK_API_KEY + AGENT_WALLET (fail-fast if missing)
        │
        ├─ 2. Discover task (first success wins):
        │       a) ugig.net          GET /api/gigs          (public)
        │       b) task-bounty.com   GET /api/v1/tasks      (public)
        │       c) local fallback task (never fails)
        │
        ├─ 3. DeepSeek (deepseek-flash, alias auto-fallback)
        │       retries w/ backoff on 429/5xx; 401/402/403 = fatal config error
        │
        ├─ 4. Append x402 invoice block (USDC, exact scheme, base64
        │       PAYMENT-REQUIRED JSON, payTo = AGENT_WALLET)
        │
        ├─ 5. Save full solution → solutions/<date>_<run>_<slug>.md
        │
        └─ 6. Prepend summary entry → status.md (newest first)
                + update run_state.json (task dedup across runs)
                workflow auto-commits all three back to the repo
```

### Sources (verified live)

| Source | Endpoint | Auth | Notes |
|---|---|---|---|
| ugig.net | `https://ugig.net/api/gigs?limit=20` | none | Public gig listing. (`/bounties?format=json` from the original brief does not exist.) |
| task-bounty.com | `https://www.task-bounty.com/api/v1/tasks` | none | Real GitHub-issue bounties $10–100, pays USDC on Base/Solana. |
| DeepSeek | `https://api.deepseek.com/chat/completions` | `DEEPSEEK_API_KEY` | Model `deepseek-flash` (current); legacy aliases auto-tried on rejection. |

## Files

| File | Purpose |
|---|---|
| `main.py` | The entire agent. stdlib-only, zero pip dependencies. |
| `.github/workflows/agent.yml` | Cron + manual dispatch + auto-commit of logs. |
| `status.md` | Run log, newest first. Committed every run. |
| `solutions/` | Full generated solutions, one markdown file per run. |
| `run_state.json` | Completed-task ids (dedup across 6h reruns). |
| `status_archive.md` | Entries rotated out of status.md past 200. |

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | yes (except `--dry-run`) | — | DeepSeek platform key |
| `AGENT_WALLET` | yes | — | Public USDC payout address (`0x…` for Base, Solana address for Solana) |
| `AGENT_NETWORK` | no | `base` | `base` or `solana` — invoice network |
| `DEEPSEEK_MODEL` | no | `deepseek-flash` | LLM model |
| `PENNILESS_MAX_TOKENS` | no | `2048` | Solution token cap |

Non-custodial by design: the code only ever reads the **public** address; no private keys are ever handled, and no transactions are signed or broadcast — the x402 block is a payment *requirement* an external client would settle.

---

## Verification guide

### Local (5 minutes)

```bash
cd penniless-agent

# 1. no keys needed — tests discovery + logging + invoice, zero API spend
python main.py --dry-run

# 2. full run with real secrets
export DEEPSEEK_API_KEY=sk-... 
export AGENT_WALLET=0xYourAddress
python main.py

# 3. run again — dedup should pick a DIFFERENT task this time
python main.py
```

What to look for:

- `--dry-run` prints `[penniless ...] Discovering tasks from ...` then
  `Task: '...'` and `DRY RUN: skipping DeepSeek call`, and creates a
  `status.md` entry tagged `[DRY RUN]`.
- A full run creates `solutions/<date>_<runid>_<slug>.md` whose tail contains
  the `⚡ x402 Payment Settlement` block with your wallet in `Pay to`.
- The second full run logs a different `task.id` — dedup is working.
- If `status.md` shows a `## Run \`failure\`` entry but the command exited 0,
  that's correct behavior: transient errors never red-flag the cron.

### GitHub Actions (end-to-end)

1. Push this folder as its own repo (workflow must live at
   `.github/workflows/agent.yml`).
2. Repo → Settings → Secrets and variables → Actions → add
   `DEEPSEEK_API_KEY` and `AGENT_WALLET`.
3. Actions tab → enable workflows → **Penniless Agent** → **Run workflow**.
4. Expected result: a green run whose log shows discovery + a DeepSeek call,
   followed by a bot commit `agent: run log <timestamp>` containing
   `status.md` (+ `solutions/`, `run_state.json`).
5. `CONFIG ERROR` in the log = a missing/misspelled secret (exit code 2).
6. Wait for the next scheduled slot (cron `0 */6 * * *`) to confirm the timer;
   `concurrency` prevents overlapping runs.

### Cost

One DeepSeek call per run (fractions of a cent) + ~10s of Actions minutes
(free on public repos). Fallback tasks cost nothing extra — same single call.

---

## Honest scope limits

- The agent **generates and archives** solutions. It does **not** yet submit
  applications to ugig.net / task-bounty.com — real submission requires each
  platform's API key (`X-API-Key` / `tb_live_…`) and is a natural next
  milestone.
- Solution quality is gated by a heuristic (length + required sections),
  not human review.
- ugig.net budget formats are normalized best-effort from untyped API fields.
