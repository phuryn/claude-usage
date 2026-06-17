# Security & Quality Audit — claude-usage

**Audited:** the pre-sync local working copy (`scanner.py` / `cli.py` / `dashboard.py`
at commit `d3b7985` plus uncommitted changes), 2026-06-17.
**Method:** 8-dimension review (scanner correctness, cost accuracy, data integrity,
security, cross-platform, performance, maintainability, docs) with adversarial
per-finding verification — 80 raised → **73 confirmed / 7 refuted**.

> ⚠️ **Read this first.** The audited tree was ~2 months behind upstream. Syncing
> to **v1.4.0** resolved most findings, and the subagent + ccusage work in **PR
> #140** resolves the rest. The status column reflects the **current** code, not
> the audited snapshot. As of PR #140 every confirmed finding is addressed; the
> section below records the three that were still open after the v1.4.0 sync and
> how PR #140 fixed them. One residual limitation is noted inline.

## Severity summary (confirmed, as of the audited snapshot)

`0 critical · 2 high · 9 medium · 42 low · 20 info`

## Key findings & current status

| Finding | Severity | Status |
|---|---|---|
| `claude-opus-4-8` mispriced ~3× via greedy `claude-opus-4` prefix match | High | ✅ Fixed in v1.4.0 (explicit 4.8 entry) |
| Cross-file token inflation (session totals accumulate duplicates) | Med | ✅ Fixed in v1.4.0 (`message_id` unique index + recompute-from-`turns`) |
| CLI had no billable gate → unknown/local models charged Sonnet rates | Med | ✅ Fixed in v1.4.0 (`get_pricing` → `None` → $0) |
| `claude-haiku-4-6` (and future minors) mispriced by CLI | Med | ✅ Fixed in v1.4.0 |
| README pricing table omitted models / `opus-4-8` | Med | ✅ Fixed in v1.4.0 |
| Pricing duplicated in `cli.py` (Python) vs `dashboard.py` (JS) → drift | Med | ✅ Fixed in **PR #140** (`pricing.py` single source; JS reads `/api/data`) |
| DOM XSS: model/project/agent names into `innerHTML` unescaped | Low | ✅ Fixed in v1.4.0 (`esc()`) |
| Single-threaded server: a slow `/api/data` blocks other requests | Low | ✅ Fixed in v1.4.0 (`ThreadingHTTPServer`) |
| Incremental scan read each updated file multiple times | Low | ✅ Fixed in v1.4.0 |
| No automated tests / CI | Med | ✅ Fixed in v1.4.0 (`tests/` + GitHub Actions) |
| `launch.json` ran `python dashboard.py` (no scan; `python` on macOS) | Med | ✅ Fixed in v1.4.0 (orphan `launch.json` removed) |
| Usage-limit events not tracked | Low | ✅ Added in **PR #140** (`limit_events`, gated on `isApiErrorMessage`) |
| "Transcript ≠ Anthropic billing" not disclosed | Info | ✅ Added in **PR #140** (footer caveat) |
| Scanner: a shrunk/compacted JSONL was skipped forever | Med | ✅ Fixed in **PR #140** (shrink path syncs `lines`, not just mtime) |
| `today` / `week` compared local date vs UTC timestamps | Low | ✅ Fixed in **PR #140** (UTC date in CLI; `getRangeBounds` UTC) |
| Dashboard recomputed every query per request | Med (perf) | ✅ Fixed in **PR #140** (mtime-keyed `/api/data` cache) |

## Open after the v1.4.0 sync — now fixed in PR #140

1. **Scanner shrink/compaction permanent-skip** (`scanner.py`, the
   `if line_count <= old_lines:` branch). It updated `processed_files.mtime` but
   not `lines`; on the next scan the mtime matched and the file was skipped, so a
   compacted (rewritten-smaller) transcript was never re-ingested. **Fixed:** the
   branch now also `SET lines = ?`, so later appends are detected.
   *Residual limitation:* stale turns from the pre-compaction content aren't
   purged (turns aren't linked to a source file); a full re-ingest-on-shrink
   would need a `source_file` column on `turns`. Low real-world frequency
   (transcripts are usually append-only).

2. **Timezone "today"/"week"** (`cli.py`): `date.today()` (local) was compared to
   `substr(timestamp,1,10)` (UTC), so near midnight users far from UTC saw the
   wrong day; the dashboard's `getRangeBounds` had the same seam. **Fixed:** the
   CLI uses a UTC date (`_utc_today()`) and `getRangeBounds` uses UTC date math.

3. **Dashboard query cost** (`dashboard.py:get_dashboard_data`): every
   `/api/data` hit re-ran all GROUP BY/JOIN queries. **Fixed:** the payload is now
   cached keyed on the DB's path + mtime, so the 30s poll reuses it until a
   scan/ingest changes the DB. *Note:* the client still receives full history and
   filters client-side; server-side date filtering remains a larger future change
   (acceptable at current scale).

## Verified safe (refuted findings)

- **SQL injection** via the `PRAGMA table_info(...)` and `AGENT_TYPE_EXPR`
  f-strings — not exploitable; every interpolated value is an internal constant.
- **Cross-platform paths** — `Path.home()` + `pathlib` + checking both
  `\subagents\` and `/subagents/` is correct on Windows and POSIX.
- **CLI vs dashboard cache pricing** — numerically identical (the CLI's derived
  `input×0.10/×1.25` equals the dashboard's explicit per-model cache rates).

## Full data

The complete per-finding output (all 73, each with its adversarial-verification
rationale) was produced by the audit workflow and is large; it lives outside the
repo at the run's task-output file (`tasks/w2o3go5v6.output`) and was not
committed.
