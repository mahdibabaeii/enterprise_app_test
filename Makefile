# Dependencies are pinned in pyproject.toml. uv.lock is not committed (see
# docs/plan.md D1), so .venv is synced only when it is missing or older than
# pyproject.toml, and every other command runs with --no-sync.
RUN := uv run --no-sync

.PHONY: verify

.venv: pyproject.toml
	uv sync
	touch .venv

verify: .venv
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) pytest
