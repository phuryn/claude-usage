# Changelog

## v1.5.0 — 2026-06-17

### Subagent attribution

- The scanner now records which turns came from dispatched subagents (Task/Agent tool) via new `turns.is_subagent` / `turns.agent_id` columns and an `agents` dispatch table (agent type + aggregate stats captured from the parent `toolUseResult`). Subagents are detected by `isSidechain`, an `agentId`, or a `subagents/` transcript path. Schema changes are additive, so existing `usage.db` files migrate in place (no rebuild).
- Dashboard: a **Subagent Tokens by Type** chart, a **Top Subagent Dispatches** table, and a Subagent Tokens stat card — all driven by the existing model + range filters. Dynamic values are HTML-escaped.
- CLI: `today` and `stats` now print subagent token/turn summaries (included in totals).

### ccusage integration (optional)

- New optional bridge (`ccusage_bridge.py`): when Node/`npx` + [ccusage](https://github.com/ryoppippi/ccusage) are present, `scan` ingests ccusage's 5-hour billing **blocks** and per-source **daily** totals into new `billing_windows` / `ccusage_daily_cache` tables. It degrades gracefully when absent — the native, stdlib-only tool is unchanged and ccusage is never required. Windows `npx.cmd` invocation and UTF-8 output are handled; the subprocess never raises.
- Dashboard: a **Current 5h Billing Window** card (progress bar vs your P90 window baseline, burn rate, time remaining, projected end-of-window tokens/cost, cost so far) with an install prompt when ccusage isn't present, and an **Other Agent CLIs (via ccusage)** chart for non-Claude usage (Codex/Gemini/Copilot/…). Claude Code is always counted natively and never double-counted against ccusage.

### Scanner / CLI

- Pricing now lives in a single `pricing.py` module shared by the CLI and (via `/api/data`) the dashboard, so the Python and JS pricing tables can no longer drift; the embedded JS table is now only a cold-start fallback.
- Detect "Claude AI usage limit reached" events into a new `limit_events` table, gated on `isApiErrorMessage` so ordinary text mentioning a limit isn't misdetected.

### Project / docs

- Footer now notes that figures are transcript-derived estimates (Claude Code doesn't write every request to disk) and may not match Anthropic billing exactly, and that native vs ccusage numbers are shown separately, never summed.

### Fixes

- **Scanner:** a transcript rewritten *shorter* (e.g. compaction) no longer gets skipped forever — the shrink path now syncs the stored line count, not just the mtime, so later appends are still ingested.
- **CLI:** `today` and `week` now compute their date window in **UTC** to match the UTC transcript timestamps (previously used the local date, off by one near midnight away from UTC).
- **Dashboard:** the `/api/data` payload is cached keyed on the DB's path + mtime, so the 30-second poll doesn't re-run every GROUP BY/JOIN when nothing changed; any scan/ingest invalidates it. Dashboard date ranges (week/month/relative) are now computed in UTC for consistency with the data.

## v1.4.0 — 2026-06-15

### Dashboard

- Redesigned the model filter as a single-line multi-select dropdown. The filter bar now shows a compact trigger that opens a panel grouping models by **Anthropic** vs **Other providers**, with the All / None actions moved inside the panel. This replaces the wrapping row of pills, which grew unwieldy as new model versions accumulated. The trigger summarises the selection: **All models**, **No models**, **All Anthropic** (every opus/sonnet/haiku/mythos/fable selected, the default view) — optionally **All Anthropic +N** when other providers are also on — or otherwise the first two model names plus a **+N** overflow. The default selection (billable models only) and `?models=` URL persistence are unchanged.

## v1.3.0 — 2026-06-15

### Dashboard

- The footer now shows the running version, linked to its GitHub release tag, on both the standalone web dashboard and the embedded VS Code panel (#135).
- The standalone web dashboard now promotes the VS Code extension in the footer and, when a newer release is available, shows an **Update to vX.Y.Z** link to the latest GitHub release. The embedded panel shows neither — VS Code updates the extension itself, so it only displays the version. The dashboard learns its surface from a new `--surface` flag the extension passes (`--surface vscode`).
- **Version check / privacy:** to power the update link, the standalone web dashboard now makes one unauthenticated request to GitHub's public releases API (`api.github.com/repos/phuryn/claude-usage/releases/latest`), cached in the browser for 24 hours and fully fail-silent (offline, blocked, or rate-limited simply hides the link). It sends **none** of your usage data — only a plain GET for the latest release number. The embedded VS Code panel makes no such request. Your transcripts and usage data never leave your machine.

### Scanner / CLI

- Added a single source-of-truth `VERSION` constant (`scanner.VERSION`) surfaced via `python cli.py --version`. It stays in lockstep with the top CHANGELOG heading and the extension's `package.json` (a parity test enforces all three match).

### Project / docs

- Rewrote the extension's local-install instructions: a prebuilt `.vsix` is now downloadable from every GitHub Release (no build step), and the build-from-source section gives a Windows-correct invocation (`powershell -ExecutionPolicy Bypass -File scripts\install.ps1`) — running `.\scripts\install.ps1` from Git Bash or by double-clicking just opens it in an editor.

## v1.2.6 — 2026-06-15

### Dashboard

- Added an explicit `claude-opus-4-8` entry to both pricing tables and pointed the generic `opus` fallback at it (was 4.7). 4.8 already costed correctly via the substring fallback — this guards against silent mis-costing if 4.8 is ever repriced, and keeps the catch-all on the newest Opus (#133, #134, thanks @Ninhache).
- Added the `claude-fable-5`, `claude-mythos-5`, and `claude-opus-4-8` rows to the README cost table (they were already in the live CLI/dashboard tables) and listed `fable` / `mythos` in the README's "included models" note, so the docs match the code.
- Unified the pricing "as of" date to **June 2026** everywhere — the dashboard footer, the in-chart cost sublabel, the pricing code comment, and the README were inconsistently labelled April/May.
- Made the Rescan button non-destructive: `/api/rescan` now runs an incremental scan instead of deleting `usage.db` and rebuilding from scratch. `usage.db` is the only durable record of usage once Claude Code prunes old transcripts (`cleanupPeriodDays`), so the old wipe-and-rebuild could permanently lose history that was no longer on disk. The button stays (it's the only in-session way to ingest new turns); its tooltip now reflects the additive behaviour (#138, thanks @OtoGodfrey).

### Project / docs

- Bumped GitHub Actions to their Node 24-era major versions across all workflows (`actions/checkout@v5`, `actions/setup-node@v5`, `actions/setup-python@v6`, `actions/upload-artifact@v5`), ahead of GitHub forcing Node 24 on the runners (Node 20 actions are deprecated from 2026-06-16).

## v1.2.5 — 2026-06-15

### Scanner / CLI

- `cli.py dashboard` now binds and serves the port *first*, then runs the scan in a background thread, instead of scanning before starting the server. A cold scan over a large `~/.claude/projects` backlog can take over a minute, and the VS Code extension kills the dashboard process if it doesn't answer `/api/data` within ~10s — so the server was being killed long before it ever bound. The dashboard now comes up immediately and fills in data as the background scan commits.

### Dashboard

- Added Fable and Mythos to the pricing tables (both CLI and dashboard), priced at 2× Opus (input $10 / output $50 / MTok; cache-read $1.00, cache-write $12.50). `claude-mythos-5` shares `claude-fable-5`'s pricing. They're now billable, sort above Opus in the model filter, and resolve via the keyword fallback (`fable` / `mythos`).
- The "no data yet" path no longer wipes the page — on a fresh start the server serves before the initial scan creates the DB, so `/api/data` can briefly return an error. The dashboard now shows a non-destructive "retrying…" notice and re-polls until the background scan produces data.
- Added `PRAGMA busy_timeout = 5000` to the dashboard's read connection so reads wait briefly for the background scan's write locks instead of raising "database is locked".

### Packaging

- The auto-tag workflow ([.github/workflows/tag-on-merge.yml](.github/workflows/tag-on-merge.yml)) now also publishes a **GitHub Release** for each new version: it builds the VS Code extension `.vsix` and attaches it to the release, using the matching CHANGELOG section as the release notes. Tags and releases are deterministic projections of the CHANGELOG. The release step asserts `vscode-extension/package.json`'s version matches the CHANGELOG heading and fails loudly otherwise, so the `.vsix` asset is always correctly labelled. Bumped the extension to 1.2.5.

## v1.2.4 — 2026-05-30

### Dashboard

- Recoloured the dashboard to a warm, neutral palette aligned with the Claude Code interface (less blue): page `#161617`, cards `#1E1F20`, lighter text `#BFBFBF`, plus a `--raised` hover layer and a dedicated `--red`. Switched to an elevated layering model — cards sit *above* the page (lighter), hover lighter still — replacing the previous inverted scheme.
- Reworked chart colours: a warm, Anthropic-leaning donut palette (clay/tan/sage/dusty-blue/…); token series are blue / coral / green / amber; chart axis & legend text use a slightly lighter shade so small labels stay legible on the dark cards.
- Chart hovers now lighten consistently (bars/series go to full opacity; the doughnut pops the hovered slice). Tooltip colour swatches are solid and borderless — removed Chart.js's default white `multiKeyBackground` edge. The hourly chart's legend/tooltip use a coral circle for the output line and a blue square for the turns bars.
- Legend series toggles now persist across repaints (filter changes, auto-refresh, sorting) for every chart, including per-slice visibility on the doughnut.
- Header title and gauge icon now use the lightest text colour (neutral) instead of coral. Selected model chips use a neutral background with a coral border; range / timezone tabs use a neutral selected background (no orange).
- Cost values use thousand separators (e.g. `$1,050.49`).
- Header meta puts "Auto-refresh in 30s" on its own line, with more space before the Rescan button.
- Removed the ⚡ peak-hour markers from the hourly axis labels (they collided with the axis; peak hours are still shown by the red bars and the legend).
- Fixed a stale "Apr 2026" stat sublabel → "May 2026".

### Extension

- The loading / status screen now matches the dashboard header — same gauge icon (served via a webview URI), title, and elevated-palette colours — instead of the old coral heading on a mismatched background.

## v1.2.3 — 2026-05-30

### Extension

- Fixed CSV export (Download CSV buttons / "Download CSV to see all" links) not working inside the VS Code webview. The dashboard iframe's sandbox was missing `allow-downloads`, so Chromium silently blocked the Blob download; added the token. Keeps the export client-side, so it still respects the current model/range/sort filters.
- The extension no longer pops a system browser tab when opening the dashboard — it passes the new `--no-browser` flag to the bundled `cli.py dashboard`. Running `python cli.py dashboard` as a script still opens the browser as before.
- Matched the webview's pre-load / behind-iframe background to the dashboard (`#191A1B`), removing the brief `#0f1117` flash before the dashboard renders.

### Scanner / CLI

- Added a `--no-browser` flag to `cli.py dashboard` to start the server without opening a browser (used by the VS Code extension; standalone CLI usage is unchanged).

## v1.2.2 — 2026-05-30

### Dashboard

- Show the gauge icon to the left of the header title, tinted to match the title color (`var(--accent)`) via a CSS `mask` so it tracks the accent dynamically. Served from a new `GET /icon.svg` route that locates `resources/icon.svg` in both run contexts (bundled `.vsix` — `python/dashboard.py` → `../resources/icon.svg`; and standalone repo — `vscode-extension/resources/icon.svg`).
- Renamed the header from "Claude Code Usage Dashboard" to "Claude Code Usage".
- Fixed charts not resizing when the window is narrowed (they only adjusted on the next data refresh). Grid cards now set `min-width: 0` so the container can shrink below the canvas's intrinsic width, letting Chart.js's `ResizeObserver` fire live. Widening already worked.
- Debounced chart resizing with Chart.js `resizeDelay` (150 ms) so dragging the window narrower no longer re-renders the canvases on every tick.
- Cost by Model, Recent Sessions, Cost by Project, and Cost by Project & Branch tables now reveal rows in steps — 10 → 25 → 50 — with `Show more ▾` / `Show less ▴` controls at the bottom right (`Show less` appears only once a table is expanded past the first step). Rendering is capped at 50 rows for performance; past that the footer shows a "Download CSV to see all (N)" link (N = total rows) that triggers the same export as the table's CSV button, alongside Show less (which resets to 10). Sorting re-applies to the full data set, so the visible rows always reflect the active sort; the control is hidden when a table has 10 or fewer rows. Clicking Show less also scrolls back to the top of that table.
- Styled scrollbars to match the VS Code dark UI (no arrows) via `::-webkit-scrollbar`: a 21px-wide gutter with a `#28292B` thumb (`#8B8B8D` on hover) over a `#121314` track. The dashboard's webview iframe doesn't inherit VS Code's `--vscode-*` theme variables, so the colors are set directly.
- Added a CSV export for the Cost by Model table (`exportModelCSV`), used by its "Download CSV to see more" link.
- Adjusted the page background to `#191A1B`.
- Updated the pricing footnote date to "as of May 2026".

## v1.2.1 — 2026-05-29

### Extension

- Bundle the Python sources (`cli.py`, `scanner.py`, `dashboard.py`) inside the `.vsix` so the extension works standalone — the only end-user dependency is Python 3.8+ on `PATH`. The `vscode:prepublish` script copies the files from the repo root at package time, so each extension version embeds the matching Python snapshot.
- Auto-start the dashboard when the user clicks the sidebar icon (no Command Palette step needed). `DashboardSidebar` accepts an `onShow` callback wired to `openDashboard()`; in-flight startup coalescing on the host side keeps clicks idempotent.
- Discover `cli.py` in any open VS Code workspace folder — covers the "cloned the repo into c:\github\claude-usage and opened it in VS Code" case that the original monorepo-sibling fallback couldn't reach for installed extensions.
- Platform-aware error messages: missing-Python guidance now leads with the right install command (python.org installer on Windows with the "Add to PATH" reminder; `brew install python` on macOS; distro package manager on Linux). The Homebrew suggestion is hidden on Windows.
- Add a dedicated [vscode-extension/README.md](vscode-extension/README.md) for the marketplace listing.
- Real gauge icon shipped (was a placeholder).
- 4 new install-mode tests for bundled discovery + ordering; 4 sidebar tests for the auto-start callback. Total extension test count: 74.

## v1.2.0 — 2026-05-29

### Distribution

- Add a VS Code extension under [`vscode-extension/`](vscode-extension/). Spawns the existing Python dashboard server in the background and embeds it via a webview iframe — no UI rewrite, all existing charts and filters reused. Activity-bar sidebar entry, four commands (`Open Dashboard`, `Rescan`, `Restart Server`, `Show Logs`), and two settings (`pythonPath`, `cliPath`, `port`). 63 tests covering Python discovery, install-mode resolution, port allocation, server lifecycle, and webview HTML/CSP. Built as `.vsix`, installable via [scripts/install.sh / install.ps1](vscode-extension/scripts/).
- Auto-discovery: extension finds `claude-usage` on PATH (Homebrew users) or a sibling `cli.py` in the monorepo (dev). Setting overrides for both Python interpreter and CLI path.

### CI

- Add `.github/workflows/extension-ci.yml`: clean Ubuntu, `npm ci && npx tsc && npm test && npm run package`, uploads the `.vsix` as an artifact on every push to DEV/main that touches `vscode-extension/`.

## v1.1.2 — 2026-05-29

### Project / docs

- Add auto-tag-from-CHANGELOG GitHub Actions workflow ([.github/workflows/tag-on-merge.yml](.github/workflows/tag-on-merge.yml)). Every push to `main` that adds a new `## vX.Y.Z` heading to `CHANGELOG.md` now creates a matching lightweight git tag automatically. CHANGELOG is the source of truth; tags are a deterministic projection of it.
- Revise versioning convention in AGENTS.md: lightweight tags driven by the workflow are the new baseline (supersedes the "annotated, by hand" rule introduced in v1.1.1). No formal GitHub Releases at any cadence.

## v1.1.1 — 2026-05-28

### Packaging

- Add Homebrew formula at `Formula/claude-usage.rb` and install instructions; `claude-usage` is now installable on macOS/Linux without `git clone` (#46, #71, thanks @HaydenHaines)

### Project / docs

- Adopt versioning convention: SemVer with tags on every release, formal GitHub Releases only for major versions. Documented in AGENTS.md.
- Tighten `/triage` routine to leave the CHANGELOG date as `TBD` and let the maintainer fill it in at tag time.

## v1.1.0 — 2026-05-28

### Dashboard

- Fix `ReferenceError: cutoff is not defined` in the hourly filter that blanked the entire dashboard (#73, thanks @thomasleveil)
- Fix hourly chart ignoring the range upper bound for `week` / `month` / `prev-month` ranges
- Fix 404 on dashboard URLs containing query strings (`?range=...&models=...`) so reloads and bookmarks work, with regression tests (#81, thanks @jakduch)
- Fix blank dashboard for users whose data only contains non-billable / unknown models, or rows with empty model names: `COALESCE(NULLIF(model, ''), 'unknown')` normalises empty-string models (in both SELECT and GROUP BY so mixed NULL + '' rows collapse to a single "unknown" group), and the default selection falls back to all models when no billable models exist (#109, thanks @HaydenHaines)
- Use `ThreadingHTTPServer` so a slow `/api/data` no longer blocks other dashboard requests (#79, thanks @jakduch)
- Add a `Today` range button (#112, thanks @Fruhji)

### Scanner

- Fix incremental scan not updating `first_timestamp` when a newly discovered session's records arrive out of order (#111, thanks @Fruhji)

### Project / docs

- Adopt `AGENTS.md` as the canonical agent guide (shared with Codex); `CLAUDE.md` is now a thin `@AGENTS.md` import
- Codify the rule that future PR merges must preserve original-author commits (`merge --no-ff` for full merges, `cherry-pick` for partial, `Co-Authored-By` trailers when applying hunks manually)
- Drop unused `.claude/launch.json`

## v1.0.0 — 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
