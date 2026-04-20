# Changelog

## Unreleased

- Add `1h`, `5h`, `1d` range buttons and a `Custom` date range picker (max 90-day span); `7d / 30d / 90d / All` remain unchanged
- Add hourly and 5-minute aggregations to `/api/data` (`hourly_by_model`, `fivemin_by_model`) so sub-day charts bucket at the right granularity
- Fix timestamp comparison format mismatch (ISO `T` vs datetime space separator) that could cause off-by-timezone results on daily filters
- `daily_by_model` rows now use a `bucket` field (ISO timestamp) instead of `day` (date only) to share the schema used by the new series; sessions include a `last_iso` field for precise sub-day filtering

## 2026-04-09

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
