# Changelog

## 2026-04-11

- Fix Opus pricing constants that were set to 1/3 of correct Anthropic API rates (`$5/$25` instead of `$15/$75` per million input/output tokens; cache write `$6.25` instead of `$18.75`, cache read `$0.50` instead of `$1.50`). Sonnet and Haiku rates were unaffected.

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
