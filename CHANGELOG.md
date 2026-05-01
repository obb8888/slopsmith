# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Library filtering by parts present or missing (#129, #69). New right-side Filters drawer (single button next to the format/sort row, with active-filter count badge and dismissible chips below) lets you require or exclude arrangements (Lead/Rhythm/Bass/Combo), specific stems on sloppaks (drums/bass/vocals/piano/other), lyrics, and tuning. Multi-select within an axis is OR (Lead OR Rhythm); cross-axis is AND. State persists across reloads. New endpoint `GET /api/library/tuning-names` returns distinct tunings present in the library, ordered by musical distance.
- Sort library by year (#128). Two new options in the sort dropdown: "Year (newest)" and "Year (oldest)". Songs without a year are pushed to the bottom for both directions.

### Changed
- Tuning sort is now ordered by musical distance from E Standard (#22) instead of alphabetical: E Standard first, then Drop D / F Standard at distance 2, then Eb Standard / F# Standard at distance 6, etc. Matches Rocksmith's grouping. Within a magnitude tier, down-tuned variants come before up-tuned, then alphabetical.
- Settings page restructured into separate "Slopsmith" (core) and "Plugins" sections, with each plugin's settings rendered as a collapsible panel (collapsed by default). "Plugin Updates" moved into the Plugins section.

### Migration notes
- The library filters depend on three new columns (`stem_ids`, `tuning_name`, `tuning_sort_key`) that are populated as songs are scanned. If filters look empty after upgrading, run **Settings → Full Rescan** to repopulate; alternatively the periodic background rescan picks them up over time.

## [0.2.4] - 2026-04-22

### Added
- Version badge in navbar (`/api/version` endpoint + `VERSION` file)
- `CHANGELOG.md` and semantic versioning
- Step Mode plugin
- `gp2midi` improvements and expanded test coverage
- Note Detection plugin factory-pattern refactor with multi-instance/splitscreen support
- Per-panel note detection in Split Screen plugin with M/L/R channel routing for multi-input interfaces

### Fixed
- `SLOPPAK_CACHE_DIR` moved to `CONFIG_DIR` for AppImage compatibility
- Improved error message when plugin requirements fail to install
