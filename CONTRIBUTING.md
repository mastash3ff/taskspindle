# Contributing

- Install: `uv sync --frozen`
- Test: `uv run pytest -q`
- Lint: `uv run ruff check .`
- Build: `uv build`

Tests must not call any model. Provider behaviour is exercised through the fake ACP
agent in `tests/fakes/`. Keep commits small with a one-line imperative message.
