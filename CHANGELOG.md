# Changelog

## 2026-04-30

- Add internationalization (i18n) infrastructure with English (default) and Korean (한국어) bundled
- Add language picker (🌐) in the header — choice persists via URL `?lang=` and `localStorage`; first-visit auto-detection from `navigator.languages`
- Add hover tooltips with plain-language explanations for every stat card, chart title, and column header (what each metric means, including the cache-read discount and cache-creation premium)
- Add tests that fail the build if any locale drifts from the English key set or the LOCALES picker registry

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
