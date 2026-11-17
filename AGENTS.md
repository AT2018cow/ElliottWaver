# Repository Guidelines

## Project Structure & Module Organization

`src/eliotbot/` contains the Python package: `cli/` handles commands, `data/` handles providers and snapshots, `schemas/` defines contracts, `engine/` implements wave analysis, `evaluation/` runs replay, and `visualization/` produces reports. `service.py` coordinates analysis; keep the engine independent of network access.

Tests live in `tests/`, reusable fixtures in `tests/conftest.py`, and snapshot validation tooling in `scripts/`. Read `docs/USER_GUIDE.md` for usage and `docs/IMPLEMENTATION_STATUS.md` for implemented scope. Generated charts and data belong in ignored `reports/` and `.cache/` directories.

## Build, Test, and Development Commands

Run from the repository root with Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,cn,us]'
.venv/bin/eliot doctor
.venv/bin/eliot demo
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

These commands install development/provider dependencies, inspect configuration, generate an offline synthetic report, and run tests and style checks. Apply formatting with `.venv/bin/ruff format src tests scripts`. Build a wheel with `.venv/bin/python -m pip wheel --no-deps . --wheel-dir dist`.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Ruff targets Python 3.11 and a 100-character line length. Preserve strict Pydantic contracts, timezone-aware timestamps, deterministic results, and separation between confirmed pivots and developing endpoints.

## Testing Guidelines

Use pytest and Hypothesis. Name files `test_*.py` and tests `test_<behavior>`. Add independent expected-value or regression cases for changed calculations, calendar boundaries, adjustment order, snapshot integrity, and replay causality. Mock provider responses; keep normal tests independent of credentials and network access. Mark optional live checks `network`. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Git history is unavailable in this checkout, so no existing commit convention could be verified. Use concise imperative subjects, such as `Fix weekly adjustment order`. Keep commits scoped. PRs should explain behavior changes, reference relevant issues, list validation commands/results, update affected documentation, and include screenshots for chart changes.

## Security & Configuration

Use `config.example.yaml` and `.env.example` as templates. Keep credentials in local `.env` or environment variables; never commit secrets, caches, or generated reports. Preserve explicit feed identity and label frozen-data replay separately from certified point-in-time evidence.
