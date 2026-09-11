# Repository Guidelines

## Project Structure & Module Organization

Core SDK code lives in `botpy/`. Top-level modules such as `client.py`, `api.py`, and `gateway.py` implement client and transport behavior; subsystems are grouped under `botpy/protocol/`, `botpy/middleware/`, `botpy/storage/`, and `botpy/ext/`. Add payload types to `botpy/types/`. Tests live in `tests/`, runnable samples in `examples/`, and longer explanations in `docs/`. Record breaking changes in `MIGRATION.md`.

## Build, Test, and Development Commands

- `uv sync` resolves dependencies, updates `uv.lock`, and installs runtime and development packages.
- `uv run python -m unittest discover -s tests -p "test_[!a]*.py"` runs the credential-free local suite. The pattern intentionally excludes `tests/test_api.py`.
- `uv run python tests/test_gateway.py` runs one focused test module.
- `uv run pre-commit run --all-files` applies Black and checks Flake8 rules.
- `uv run python -m compileall -q botpy examples` catches syntax errors.
- `uv lock --check` verifies the lockfile; `uv build` creates wheel and source distributions in `dist/`.

## Coding Style & Naming Conventions

Use four-space indentation and Black formatting with a 120-character line limit. Flake8 ignores `W503` and `E203`. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for constants. Do not introduce blocking I/O into client, gateway, or middleware paths. Public APIs should include useful type annotations and concise docstrings.

## Testing Guidelines

Tests use `unittest`, including `IsolatedAsyncioTestCase` and `unittest.mock`. Name files `test_<feature>.py`, classes `<Feature>Tests`, and methods `test_<behavior>`. Add regression coverage for bug fixes. There is no enforced coverage percentage, but exercise new branches and error paths. Run `tests/test_api.py` only with an isolated test bot and explicit credentials; it mutates live platform resources.
For Gateway or HTTP send changes, assert request issuance, exact retries, and `TransportError` context. Keep regression coverage for proactive POST failures remaining non-retriable.

## Commit & Pull Request Guidelines

Recent history uses short messages such as `upd` and `fix intents`; prefer a more informative imperative subject, for example `Fix gateway intent validation`. Keep each commit focused. Pull requests should explain the problem and approach, link relevant issues, list verification commands, and call out compatibility or configuration changes. Include logs or screenshots only when behavior is difficult to verify from tests, and update docs/examples for public API changes.

## Security & Configuration

Copy `examples/config.example.yaml` for local setup, and never commit AppSecret values, tokens, guild IDs, or generated `config.yaml` files. Keep tests deterministic and offline unless they are clearly documented as integration tests.
