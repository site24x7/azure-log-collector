# Contributing

Thanks for your interest in improving the Azure Log Collector.

## Getting Started

1. Clone the repository and create a feature branch off `main`.
2. Install dev dependencies:
   ```bash
   cd function-app
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt -r requirements-dev.txt
   ```
3. Run tests before submitting changes:
   ```bash
   python -m pytest tests/ -v
   ```

## Pull Request Guidelines

- Keep changes scoped — one logical change per PR.
- All existing tests must continue to pass.
- Add tests for new functionality and bug fixes.
- Update `docs/` if the change affects architecture, configuration,
  or the user-facing API.
- Bump `function-app/VERSION` for any change that ships to customers
  (the release workflow auto-publishes when this file changes on `main`).

## Code Style

- Python: PEP 8, with type hints on all new public functions.
- JSON / YAML: 2-space indent.
- Markdown: one sentence per line where practical.

## Security

Please do **not** open public issues for security vulnerabilities.
See [SECURITY.md](SECURITY.md) for the private reporting process.
