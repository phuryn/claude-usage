# AGENTS.md

Guidance for any coding agent (Codex, Claude Code, etc.) working on this repository.

> **Naming note.** This project *analyzes* Claude Code's local usage logs, so "Claude Code" below always refers to that product (the source of the JSONL data) — not to the agent reading this file. The agent working on the codebase is referred to as "the coding agent" or just "you".

## Project shape

Three Python files, stdlib only, no `pip install` step. Python 3.8+.

- [scanner.py](scanner.py) — parses Claude Code JSONL transcripts into a SQLite DB at `~/.claude/usage.db`.
- [cli.py](cli.py) — terminal commands (`scan` / `today` / `week` / `stats` / `dashboard`).
- [dashboard.py](dashboard.py) — single-file `http.server` serving an embedded HTML/JS SPA on `localhost:8080`.

Use `python` on Windows, `python3` on macOS/Linux. Both work the same.

## Common commands

```
python cli.py scan                  # incremental scan (fast on re-run)
python cli.py today                 # today's usage by model
python cli.py week                  # last 7 days, per-day + by-model
python cli.py stats                 # all-time stats
python cli.py dashboard                          # scan + open http://localhost:8080
python cli.py dashboard --host 0.0.0.0 --port 9000
python cli.py dashboard --auto-refresh 120       # rescan every 120s (default 30)
python cli.py dashboard --no-auto-refresh        # scan once at startup only
python cli.py scan --projects-dir PATH           # scan a custom transcripts dir
# or via env vars:
HOST=0.0.0.0 PORT=9000 python cli.py dashboard
AUTO_REFRESH=120 python cli.py dashboard

python -m unittest discover -s tests -v             # full test suite (CI runs this)
python -m unittest tests.test_scanner -v            # one file
python -m unittest tests.test_scanner.TestProjectNameFromCwd.test_windows_path  # one test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the suite on Python 3.9 / 3.11 / 3.12 against `main` and PRs.

## Architecture

### Data flow

```
~/.claude/projects/**/*.jsonl   →   scanner.parse_jsonl_file()
~/Library/.../Xcode/...                  ↓
                              aggregate_sessions() → upsert_sessions() + insert_turns()
                                         ↓
                              ~/.claude/usage.db (SQLite)
                                         ↓
                  cli.py queries   ←──────────→   dashboard.py /api/data
```

By default the scanner walks both `~/.claude/projects/` and the Xcode coding-assistant directory; missing dirs are silently skipped. Override with `--projects-dir`.

### SQLite schema (created/migrated in [scanner.py](scanner.py) `init_db`)

- **`turns`** — one row per assistant API response. The source of truth for tokens and per-model attribution.
- **`sessions`** — aggregated per session (denormalized totals + chosen primary model).
- **`processed_files`** — incremental-scan tracking: `(path, mtime, lines)`. A file is skipped if its mtime matches; if it grew, only lines past the stored `lines` count are processed.

A conditional unique index on `turns.message_id` (where non-empty) lets `INSERT OR IGNORE` cheaply dedupe replays across rescans.

### Non-obvious invariants

These three things will bite you if you don't know them:

1. **Streaming dedupe by `message.id`.** Claude Code writes multiple JSONL records per API response — only the *last* one for a given `message.id` has the final usage tallies. `parse_jsonl_file` keeps the last record per `message_id` in a dict; earlier records are discarded. Don't sum across records of the same `message_id`.

2. **Session totals are recomputed from `turns` at the end of `scan()`.** During an incremental scan `upsert_sessions` adds tokens additively, but `insert_turns` uses `INSERT OR IGNORE` against the `message_id` unique index — so if a turn is a duplicate, session totals would drift. The final `UPDATE sessions ... (SELECT SUM ... FROM turns)` block reconciles this. Preserve it if you refactor scan logic.

3. **Session primary model priority is opus > sonnet > haiku** (`_model_priority` in [scanner.py](scanner.py)). This prevents a subagent's haiku turn from overwriting the session's opus model when an existing session is updated. Per-turn model is always honored in the `turns` table; only the session-level summary uses the priority.

### Cost calculation

Costs are computed **per turn** (each turn knows its own model), then summed. This is true in both the CLI ([cli.py](cli.py) `calc_cost`) and the dashboard JS ([dashboard.py](dashboard.py) `calcCost` inside the embedded HTML). Aggregating tokens first and applying a single price is wrong for sessions that span multiple models.

Pricing is duplicated in two places that **must stay in sync**:
- [cli.py](cli.py) `PRICING` dict (Python)
- [dashboard.py](dashboard.py) `PRICING` const inside `HTML_TEMPLATE` (JavaScript)

`get_pricing` / `getPricing` resolve in three tiers: exact match → `startswith` (handles date-suffixed model IDs like `claude-opus-4-7-20260215`) → substring fallback on `opus` / `sonnet` / `haiku`. Models that don't match any tier return `None` and are billed at $0 (shown as `n/a`) — this is intentional so local/3rd-party models (gemma, glm, etc.) aren't charged at Sonnet rates.

### Dashboard server

`http.server.BaseHTTPRequestHandler`-based, two endpoints:
- `GET /api/data` → JSON snapshot from `get_dashboard_data()`. Returns *all* history; client-side filters by date range and model. **It only reads the DB — it never scans.** Freshness comes from the rescan loop in `cli.cmd_dashboard`'s background thread (`--auto-refresh`, default 30s), which is what makes the browser's 30s `/api/data` poll actually show new data. Before that loop existed, a dashboard left open served the startup scan's snapshot forever while the footer cheerfully advertised "Auto-refresh in 30s". If you refactor the scan scheduling, keep *something* writing to the DB while the server runs.
- `POST /api/rescan` → deletes the DB and runs a full rescan. Passes `db_path` and `projects_dirs` explicitly so tests that monkey-patch the module globals work — scan's default arg values are frozen at def time, so don't switch to bare defaults.

The entire UI lives in `HTML_TEMPLATE` as a raw string. Chart.js is loaded from CDN.

Client-side UI state (collapsed sections, the 24h update-check cache) is kept in **`localStorage`**, which is keyed by the page's origin. In the VS Code extension the dashboard is embedded as an **iframe at `http://127.0.0.1:<port>/`**, so that state only survives a window reload if the port is stable. The extension therefore remembers the last port in `workspaceState` and reuses it when it's still free (`resolveStablePort` in [vscode-extension/src/port-allocator.ts](vscode-extension/src/port-allocator.ts)) — don't revert that to a fresh `pickFreePort` every launch, or the panel silently loses its state each reload.

## Testing notes

- `tests/test_scanner.py` and `tests/test_dashboard.py` use `tempfile.NamedTemporaryFile` for an isolated DB; never touch the user's real `~/.claude/usage.db`.
- The `/api/rescan` test patches `dashboard.DB_PATH` and `scanner.DEFAULT_PROJECTS_DIRS` — keep that contract intact (see commit 8ae2664).
- On Windows, `~/.claude/` may not exist on a fresh checkout. `get_db` creates the parent dir (`mkdir(parents=True, exist_ok=True)`) — don't remove that or `sqlite3.connect` will fail in CI / fresh installs (commit b5d1e15).

## Respecting contributors

When merging community PRs, **preserve the original author's commit so they get GitHub contributor credit**. In practice:

- `git fetch origin pull/<N>/head:pr-<N>` → `git merge --no-ff pr-<N>` keeps the author commit verbatim inside the merge bubble (don't squash, don't rebase-flatten).
- For a partial merge — when only one hunk of a PR is wanted — use `git cherry-pick <commit-sha>` against the specific upstream commit so authorship is preserved. If the diff isn't a clean single commit, fall back to applying the hunk manually + adding a `Co-Authored-By: Name <email>` trailer.
- Improvements that the bot/maintainer makes _on top_ of a contributor's work go in **separate follow-up commits**, not amendments to the contributor's commit.
- When closing duplicate PRs (multiple authors fixed the same bug independently), thank each one and explain that landing the earliest version isn't a quality judgment.
- **After a PR merges (or you close it), post a short comment on it.** Thank the author, note the release it shipped in, and mention any follow-up changes you layered on top of their commit. A `git merge --no-ff` of the exact PR head commit makes GitHub auto-close the PR as *merged* (so the author keeps contributor credit) — but it doesn't notify or thank them, so the comment closes the loop.

This applies to all agents working on this repo, not just Claude Code.

### Signing GitHub comments

Every GitHub issue/PR comment posted on the maintainer's behalf ends with a signature on its own final line, in italics. Pick by whether Paweł actually reviewed the comment text before it went out:

- He reviewed it → `_Written with an agent, reviewed by Paweł_`
- The agent posted it without his review → `_Written by Pawel's agent_`

Only claim review when it actually happened. (Convention shared with the sibling `grok-build-vscode` repo.)

## Versioning and releases

[SemVer](https://semver.org/). **`CHANGELOG.md` is the canonical version reference**; tags are a projection of it, created automatically.

The release flow:
1. While work accumulates on `DEV`, the `## vX.Y.Z — TBD` heading at the top of `CHANGELOG.md` collects bullets. (For automated triage runs, see the routine note below.)
2. When the maintainer is ready to release, they finalize the heading (`TBD` → today's date), **bump both `scanner.VERSION` and `vscode-extension/package.json`'s `version` to match the CHANGELOG version** (all three ship in lockstep — the extension bundles the Python sources, and `scanner.VERSION` is the runtime version reported by `cli.py --version` and the dashboard footer since the CHANGELOG isn't bundled into the `.vsix`), **run [`scripts/bump-formula.sh`](scripts/bump-formula.sh) on `DEV` to repoint the Homebrew formula at the previous release's tag tarball** (see "Homebrew formula and self-referential SHA" below — this is a plain `DEV` commit that reaches brew users via this same merge, so it never touches `main` directly), merge `DEV → main` with `merge --no-ff` (so the release boundary is visible in `git log main`), and push `main`. A parity test (`tests/test_version.py`) fails the suite if the three drift, so in practice they're bumped together on `DEV` when the version heading is written.
3. [`.github/workflows/tag-on-merge.yml`](.github/workflows/tag-on-merge.yml) fires on the push, sees the new `## vX.Y.Z` heading in the CHANGELOG diff, and:
   - creates a lightweight tag at the merge commit (**no `git tag` step for the maintainer**), then
   - builds the VS Code extension `.vsix` and publishes a **GitHub Release** for that tag — the matching CHANGELOG section as the release notes, the built `.vsix` attached as a release asset.

So every release is both a tag *and* a GitHub Release with the installable `.vsix` downloadable from it. This mirrors the manual procedure in the sibling `grok-build-vscode` repo (`scripts/release.*`: tag + `gh release create` with the `.vsix` attached), adapted to this repo's CHANGELOG-driven, merge-to-`main` model — so it's automated rather than a local script. Marketplace publish (`vsce publish`) stays separate and explicit, exactly as there.

**The release step asserts `vscode-extension/package.json`'s version equals the CHANGELOG version and fails loudly if not** — the `.vsix` filename embeds the package version, so a mismatch would mislabel the asset. If you forget the bump, the tag is still created but the Release step fails; bump `package.json` and create the Release by hand (`gh release create vX.Y.Z --notes-file <section> vscode-extension/<name>-X.Y.Z.vsix`), since a same-commit re-push won't re-add the heading to re-trigger the workflow.

The workflow is idempotent: if the tag already exists (someone tagged manually before the workflow caught up) the tag step is a no-op, and if the Release already exists the release step is a no-op. It also no-ops entirely on pushes that don't add a new version heading (typo fixes, docs-only edits, etc.).

Existing tags `v1.0.0`, `v1.1.0`, `v1.1.1` are lightweight and were created by hand before the workflow existed. `v1.1.2` was the first tag created by the workflow. The workflow only *adds* missing tags; it never reconciles existing ones. Don't bother re-tagging the legacy ones.

### CHANGELOG conventions

The workflow trusts the CHANGELOG, so the format matters. Every new release entry on `DEV` follows this exact shape:

```
## vX.Y.Z — TBD

### <Area>

- One bullet per change, past tense, with a PR/issue link and `thanks @author` where the change came from a contributor (#73, thanks @thomasleveil)
```

Format rules the workflow relies on:

| Field | Required form | Why |
|---|---|---|
| Heading | `## vX.Y.Z` (exactly two `#`, the `v` prefix, three numeric components — strict semver) | The workflow regex `^## v[0-9]+\.[0-9]+\.[0-9]+([[:space:]]|$)` won't match anything else. `v1.1`, `v1.1.0-rc1`, `V1.1.0` are all silently ignored. |
| Separator | ` — ` (em-dash with surrounding spaces) | Cosmetic but consistent. The workflow ignores everything after the version. |
| Date | `TBD` while accumulating on `DEV`; replace with `YYYY-MM-DD` *at the moment of merging to `main`* | The workflow doesn't enforce dates — but a `TBD` heading that ships to main means the release looks unfinished forever. |
| Subsections | `### Dashboard`, `### Scanner`, `### Packaging`, `### Project / docs` — pick the smallest set that fits | Keeps the CHANGELOG scannable. |
| Bullets | Past tense, link the PR/issue with `#N`, credit external contributors with `thanks @login` | Lets readers (and future maintainers tracing history) find the source quickly. |

**The TBD → date rule is the only step a human must remember at release time.** If you forget, the workflow still tags correctly, but the CHANGELOG entry on main reads `## v1.1.3 — TBD` forever. Fix-up commit can correct it, but it'll feel sloppy.

Patch (`Z` increments) is the default for any release. Bump minor (`Y`) when a non-breaking user-visible feature lands (e.g. Today range button shipping alone would have been a minor in a different world). Bump major (`X`) only on breaking changes — there have been none and likely won't be soon. There's no automation around picking the right bump; the maintainer (or `/triage`) decides when writing the CHANGELOG heading on `DEV`.

### Homebrew formula and self-referential SHA

The Homebrew formula at `Formula/claude-usage.rb` lives inside this same repo. Be careful when bumping it: if the formula's `url` points at a tarball that **contains the formula itself with that sha256**, the sha256 is self-referential and uncomputable. Practical rule: a release's formula must point at the **previous** release's tarball, never its own. In v1.1.1 the formula points at v1.1.0's commit-SHA tarball, so brew users installing v1.1.1's formula receive v1.1.0 code — that's the trade-off of keeping the formula in-tree.

Now that the auto-tag workflow exists, formula bumps use the tag-tarball URL (`archive/refs/tags/vX.Y.Z.tar.gz`) instead of commit SHAs — stabler and shorter — as long as the tag-tarball pointed at is from the *previous* release.

**Automate the bump; never hand-edit the three pinned lines.** [`scripts/bump-formula.sh`](scripts/bump-formula.sh) fetches a released tag's tarball, computes its `sha256`, and rewrites the `url` / `version` / `sha256` lines (leaving `head`, `homepage`, and comments alone). With no argument it targets the latest `v*` tag on origin — run during release prep, before the new tag exists, that's the previous release, exactly what the self-referential rule requires.

**Why a `DEV`-only commit is enough — and why it's one release behind.** Brew reads the formula from the tap's default branch (`main`) HEAD, never from `DEV` or a tag. So the bump is a normal `DEV` commit that becomes visible to brew users only when `DEV → main` merges — which is precisely at the next release. That timing is the point: it lets us pin at the just-frozen *previous* tag and ship it with the release, so **brew always tracks one release behind, advancing automatically each release**, with no direct push to `main` (dodging `main`'s branch protection) and no hand-editing to forget. The manual, forget-prone bump is what let the pin silently rot at v1.1.0 from v1.1.1 through v1.5.0; v1.5.2 caught it up to v1.5.1 and wired in this routine. The only thing a `DEV`-only bump can't do is move brew *between* releases — you'd need a release (a `DEV → main` merge) for that, which is fine because the pin only ever changes at release boundaries anyway.

## Weekly triage routine

The repo has a self-contained slash command at [.claude/commands/triage.md](.claude/commands/triage.md) that automates the weekly PR/issue cleanup we used to ship v1.1.0: classify open items with Codex, merge no-brainers to DEV preserving authorship, run tests, close duplicates / scope-violations with friendly messages, bump CHANGELOG by patch, push DEV. **The routine never pushes to `main`** — release decisions stay with the maintainer.

Register the Windows Task Scheduler entry with [scripts/setup-weekly-triage.ps1](scripts/setup-weekly-triage.ps1). Logs go to `logs/triage-*.log`.

If you're working on this repo and want to invoke the routine ad-hoc, just type `/triage` in Claude Code. Hard safety rails (test-passing gates, no security-sensitive auto-merges, no scope-changing merges, Codex sign-off required on closures) live inside `triage.md`.
