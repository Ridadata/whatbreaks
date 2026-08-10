# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [Semantic Versioning](https://semver.org/).

While `0.x`, the JSON output shape and rule severities may change in minor
releases. `schema_version` in the JSON output is versioned independently so
consumers can pin to it.

## [0.1.0] — 2026-08-10

First usable release. [PyPI](https://pypi.org/project/whatbreaks/0.1.0/)

### Added
- `whatbreaks check --base <manifest> --head <manifest>` — reports what a change
  breaks downstream, with exit codes (`0` clean, `1` findings, `2` bad input).
- Rules `WB001` (column removed), `WB002` (model removed), `WB003` (column
  added) and `WB900` (model could not be analysed).
- Column-level lineage and blast radius, including columns needed only for
  filters, joins and grouping — these break the query without changing any
  downstream schema and are invisible to projection lineage alone.
- Offline SQL recovery: dbt-compiled SQL when available, otherwise Jinja
  rendered against macros compiled out of `manifest.json` itself.
- Schema inference with an explicit uncertainty model
  (`exact` / `partial` / `unknown`, each with a reason).
- Coverage reporting on every run, and an explicit warning that incomplete
  analysis means absence of findings is not proof of safety.
- Three output formats: `text`, `json` (versioned schema), `markdown` (shaped
  for a PR comment, injection-hardened).
- `whatbreaks debug schema|graph|sql|coverage` for inspecting the analysis.
- False-positive corpus of 15 no-op changes, enforced as a required CI job.

### Notes
- No warehouse connection, no secrets, no network access at any point.
- Renames, type changes and expression changes are deliberately absent: they are
  inference rather than fact, and shipping a guess as a finding is the failure
  mode this project exists to avoid.
