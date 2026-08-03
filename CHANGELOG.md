# Changelog

## v1.5.6 — TBD

### Dashboard

- Fixed the dashboard serving a **frozen snapshot** when left open: the browser polls `/api/data` every 30s and the footer advertises "Auto-refresh in 30s", but nothing ever rescanned the transcripts after the startup scan, so the numbers stopped moving the moment that scan finished. `cli.py dashboard` now runs an incremental rescan on the same 30s cadence, so today's usage keeps filling in without a restart.
- Added `--auto-refresh SECONDS` (and the `AUTO_REFRESH` environment variable) to tune the rescan interval, plus `--no-auto-refresh` / `--auto-refresh 0` to restore the scan-once-at-startup behaviour. Rescans are incremental — `processed_files` skips any transcript whose mtime is unchanged — so a steady-state pass over a ~700-file backlog touches only the live sessions.
- Fixed a page left open **across midnight** not noticing the calendar moved. Date ranges resolve against the current date, so at midnight "This Month" silently becomes a different month — but nothing re-filtered, so the view kept the old window. Rolling into a month with no data yet (August 1st fell on a Saturday) made a perfectly healthy dashboard look dead. The refresh tick now detects the rollover and re-filters.
- Fixed the auto-refresh timer being armed **once, against the wrong range**. `rangeIncludesToday()` was evaluated a single time and frozen into whether an interval existed at all, so a range that excluded today killed the timer permanently — it never came back when the calendar moved that range back into view. The tick now runs unconditionally and re-decides on every pass. Restoring the range from the URL also moved ahead of arming the timer: `loadData()` does it too, but it's async, so the timer was previously armed against the `30d` default rather than the range on screen.
- The dashboard now refreshes on **`visibilitychange`**, so a tab returning from the background catches up immediately instead of waiting out the next tick. Browsers clamp timers hard in hidden tabs (Firefox throttles them to roughly once a second and can stall them further), which is what made a dashboard left open in another tab read as stale on return.

## v1.5.5 — 2026-07-10

### Dashboard

- Added an **Est. Cost line overlay** to the Daily Token Usage chart — a legend-toggleable line on a dedicated right-hand axis, priced **per model before the daily aggregation** so multi-model days are costed correctly (#151, thanks @paulabenzar).
- Fixed **"This Month"** (and the other calendar ranges) including the previous month's last day in UTC+ timezones: date-range bounds are now formatted from local calendar components instead of `toISOString()` (UTC). The same local-date helper is now used by `rangeIncludesToday`, so it can no longer disagree with the range bounds near UTC midnight (#151, thanks @paulabenzar).

### Scanner / CLI

- The `today`, `week`, and `stats` commands now run the idempotent schema migration when they open the database, so reading before the next `scan` no longer crashes with `sqlite3.OperationalError: no such column: is_subagent` on a database created before subagent tracking existed. The dashboard already did this; the CLI read path now matches (#153, thanks @iliaal).

### Project / docs

- Documented the `dashboard` command's `--host` / `--port` flags in the README and AGENTS.md — the flags already worked (and are shown in the built-in `USAGE` text) but only the `HOST` / `PORT` environment variables were documented (#150, thanks @subhamchbty).

## v1.5.4 — 2026-07-01

### Dashboard

- Added a **Title** column to the Recent Sessions table (after Project) and its CSV export, showing Claude Code's own session title: a user-set `custom-title` takes priority over an AI-generated `ai-title`. Long titles wrap within the column (min width 160px). Sessions without a title record show a muted **Untitled** placeholder — there is no fallback to the first user message, so prompt text never leaks into the table (#147, thanks @arojunior).
- The Recent Sessions **CSV export now includes full session IDs** (the table still shows the 8-char prefix for readability, but an 8-char prefix isn't useful in an export) (#147).

### VS Code extension

- The embedded dashboard now waits up to **20s** (was 10s) for the server to become ready on cold start — the one-time topic backfill can slow the first launch — and a failed start now offers a **Retry** button (both in the error notification and on the panel) instead of only pointing at the command palette (#147).

### Scanner

- The scanner now parses `custom-title` / `ai-title` transcript records into a new `sessions.topic` column (additive in-place migration; existing DBs upgrade without a rebuild). Titles are captured even when Claude Code appends them after the turns (picked up on the next incremental scan), and a title-only record can never create a token-less phantom session row (#147, thanks @arojunior).
- On upgrading an existing database, the next scan runs a **one-time topic backfill**: it re-reads the title records already present in previously-scanned transcripts (which an incremental scan would otherwise skip) so old sessions get a Topic too, then records via a `schema_meta` flag that it's done so it never repeats. Only title records are read, so token totals are untouched (#147).

## v1.5.3 — 2026-07-01

### Packaging

- Added a `pyproject.toml` so the tool installs with `uv tool install git+https://github.com/phuryn/claude-usage` (or `pipx install …`) — no clone needed (#144, thanks @jamesbraza). Packaging only: `[project.dependencies]` is empty so the tool stays stdlib-only at runtime, the version is read dynamically from `scanner.VERSION` (still the single source of truth), and the clone + `python cli.py` path is unchanged. The `cli.py` `__main__` block was lifted into a `main()` function to serve as the `claude-usage` console entry point.

## v1.5.2 — 2026-07-01

### Packaging

- Bumped the Homebrew formula pin from v1.1.0's commit to the **v1.5.1** tag tarball, so `brew install` / `brew upgrade` now installs current sources instead of year-old ones. The in-tree formula must reference the *previous* release to keep its `sha256` computable (a self-pointing hash is uncomputable), so Homebrew tracks one release behind by design (#46).

## v1.5.1 — 2026-07-01

### Packaging

- Fixed the Homebrew shim crashing at runtime with `python@3.13/bin/python3: No such file or directory`. Modern `python@3.x` kegs only ship the versioned `python3.13` in their `bin` (the unversioned `python3` symlink moved to `libexec/bin`), so the formula now execs `python3.13` directly (#46, thanks @adrianlungu and @Jeroendevr for reporting).
- Fixed the Homebrew install instructions: Homebrew disabled installing a formula from an arbitrary raw URL, so the README now taps the repo first (`brew tap phuryn/claude-usage …` then `brew install phuryn/claude-usage/claude-usage`) (#46, thanks @adrianlungu).

## v1.5.0 — 2026-06-21

### Dashboard

- Added a sticky **section nav** under the filters that teleports to any section and highlights where you currently are — much faster to navigate the long single-page report. It's three compact entries: **Overview**, plus **Graphs** and **Tables** menus that drop down their sections on hover (or keyboard focus).
- Made every chart and table card **collapsible**: click its title to fold the whole section away (independent of the in-table Show more/less paging). Collapsed sections are remembered across reloads, so you can permanently hide the views you don't use.
- Reworked table paging so small tables aren't paged pointlessly: a table with **12 rows or fewer now shows in full** (no "Show more" to reveal just a row or two). Larger tables start at 10 rows and page **10 → 25 → 50**, with **Show less** collapsing back to 10.
- Replaced the date-range button row with a compact **dropdown** — the eight buttons wrapped badly in the narrow VS Code panel.
- Fixed the embedded VS Code panel **losing its saved state on reload** (collapsed sections, the update-check cache): the extension now reuses the dashboard's port across launches when it's free, so the iframe's localStorage origin stays stable instead of changing every time.
- Added subagent attribution views: a **Subagent Tokens by Type** stacked bar chart and a **Top Subagent Dispatches** table, plus a Subagent Tokens stat card. Dispatched Task/Agent subagents (and Claude Code's auto-compaction) are surfaced separately while remaining included in the overall totals; both respect the existing model + range filters. All dynamic values are escaped via `esc()` (#140, thanks @john988).
- The **Top Subagent Dispatches** table now behaves like Recent Sessions: it pages with Show more / Show less and exports all filtered rows to CSV, and it was moved below the Cost by Model table. The full ranked set is now sent to the client (previously capped at 50 server-side). Its header explanation ("ranked by total tokens; unknown = …") was moved into a small **(i) tooltip** to declutter the title.
- Fixed **Cost by Project & Branch** default ordering: it now sorts by cost (descending) like the other cost tables, instead of grouping alphabetically by project name. Project name is kept only as a tiebreaker, and column sorting now matches the sibling tables.
- Fixed a crash on `/api/data` (`no such table: agents`) that could appear on the first dashboard load right after upgrading — the server serves before its background scan migrates the DB, so `get_dashboard_data` now migrates the schema on read (idempotent) before running the subagent queries.

### Scanner / CLI

- The scanner now attributes subagent usage: new `turns.is_subagent` / `turns.agent_id` columns and an `agents` dispatch table (additive, in-place migrations — existing DBs upgrade without a rebuild). Subagents are detected via `isSidechain` / `agentId` / a `subagents/` transcript path, and dispatch metadata (agent type, status, duration, tool-use count) is captured from the parent tool result (#140, thanks @john988).
- `today` and `stats` now print subagent token + turn summaries (counted as a subset of the totals, not added on top) (#140, thanks @john988).

### Packaging / docs

- Added Docker support: a `Dockerfile` and `scripts/run-docker.sh` run the dashboard in a container with `~/.claude` mounted **read-only** and the SQLite DB in a named volume, isolated from your home directory. A new `CLAUDE_USAGE_DB` env var makes the DB path configurable at runtime (default unchanged: `~/.claude/usage.db`) (#143, thanks @RafikFarhad).

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
