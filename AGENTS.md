# Repository Guidelines

## Project Structure & Module Organization

This Windows 11/Python 3.14+ package uses `uv`. Source lives in `src/meta_scanner/`: `cli.py` exposes commands, `scanner.py` coordinates work, `extractors.py` and `ocr.py` read documents, `llm_metadata.py` and `local_llm.py` generate metadata, and `repository.py` stores OCR and AI checkpoints. `docs/requirements.md` holds target behavior; `docs/architecture.md` describes the implemented flow and gaps. `config.example.toml` and `schemas/` define interfaces. Ignored sample documents live in `tests/test_files/` and `tests/ocr_test_data/`; there are no automated tests yet.

## Build, Test, and Development Commands

- `uv sync --python 3.14` creates the local environment from `uv.lock`.
- `uv run meta-scanner --help` lists the CLI commands.
- `uv run meta-scanner doctor --config config.local.toml` checks OCR files and the model hash; it does not run inference.
- `uv run meta-scanner scan --config config.local.toml --dry-run` counts candidate documents without processing them.
- `uv run meta-scanner run --config config.local.toml --ignore-window` processes documents outside the night window; add `--extract-only` for a preview without AI.
- `uv build` builds distributable package artifacts into `dist/`.
- `uv run pytest` runs the suite once `tests/test_*.py` files exist.

Run from the repository root. Copy `config.example.toml` to ignored `config.local.toml` before using the CLI. The example scans `tests/`.

## Coding Style & Naming Conventions

Use four spaces and standard Python naming: `snake_case` for modules/functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Keep package code inside `src/meta_scanner/`; add type hints to public functions. No formatter or linter is configured. Update the relevant document when changing CLI behavior, storage, or output fields.

## Testing Guidelines

Pytest is a dev dependency, but no test files or coverage threshold exist. Add `tests/test_*.py` for requested tests, using the cases in `docs/development-plan.md`. Separate pure unit checks from cases requiring Tesseract or llama.cpp. Keep private inputs under ignored `tests/` paths; never commit extracted text or generated catalog data.

## Commit & Pull Request Guidelines

The small history (`init uv`, `codex architecture`) shows no established message convention. Use a short, specific subject describing one change. Pull requests should explain behavior, list checks performed, link an issue when relevant, and include redacted sample output for CLI or catalog changes.

## Security & Configuration

Keep documents, extracted text, models, credentials, and local settings out of Git (`tests/test_files/`, `tests/ocr_test_data/`, `var/`, `models/`, `config.local.toml`). Preserve originals, skip protected files, and keep inference on `127.0.0.1`. Record the exact model hash and dependency versions when reporting results.
