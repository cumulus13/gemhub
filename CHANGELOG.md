# Changelog

## 1.0.2

### Fixed (found by the first Windows CI run)
- **Windows: `PermissionError` on refresh.** Cached index bodies were replaced in place, which Windows refuses while a client is still reading the file; the request failed with HTTP 500. Bodies are now written to a new file and the metadata is switched over, and old files are removed on a best-effort basis. Caches from 0.2.0 are read as before.
- **Windows: corrupted `/names`.** The merged names file was written in text mode, so every line ended in `\r\n`. It is now written as bytes.
- A gem file finished by two workers at once no longer tries to replace a file that may be in use.
- Tests: no raw gem bytes in parametrize ids (overflowed an environment variable on Windows). CI: Linux matrix pinned to `ubuntu-24.04` (Python 3.9 is not available on 26.04) and the fixture script is run through `bash`; `.gitattributes` keeps `*.sh` on LF.

## 1.0.1

### Added
- **Private gems.** `gem push`, `gem yank` and `gem yank --undo` against the hub, authenticated with API keys (`gemhub key create`). Private gems are merged into the compact index (`/names`, `/versions`, `/info/<gem>`), downloadable, installable with `gem install` and Bundler, and shown in the web hub.
- **Database migrations with Alembic**, shipped in the package. `gemhub migrate`, `gemhub db-status`; migrations also run on start (`GEMHUB_AUTO_MIGRATE`). A 0.1 database is adopted without data loss.
- `GEMHUB_MAX_GEM_BYTES`, `GEMHUB_PRIVATE_NAME_POLICY`.
- CI job on PostgreSQL and an end-to-end job that runs the real `gem` and Bundler against private gems.

### Changed
- `GEMHUB_AUTO_CREATE_DB` is replaced by `GEMHUB_AUTO_MIGRATE`. Tables are no longer created with `create_all`.

### Fixed
- A digest made only of digits in `checksums.yaml.gz` was read by YAML as a number and skipped the integrity check on pushed gems. Caught by the new tests before release; digests are now always compared as text.

## 0.1.0
- Caching RubyGems mirror: compact index, legacy index, verified gem files, web hub, Redis and database metadata cache.
