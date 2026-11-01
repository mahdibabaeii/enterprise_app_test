# Dependencies are pinned in pyproject.toml. uv.lock is not committed (see
# docs/plan.md D1), so .venv is synced only when it is missing or older than
# pyproject.toml, and every other command runs with --no-sync. The stamp file
# is written only after a successful sync, so a failed install is retried.
RUN := uv run --no-sync
PORT := 8000

.PHONY: start verify demo tamper

.venv/.synced: pyproject.toml
	uv sync
	touch $@

# Install, reseed the demo database, and serve the UI and API on port 8000.
# The port is checked before reseeding, so a second start cannot wipe the
# database under a server that is already running. DECISIONS_DB and
# CARD_REF_KEY are cleared for the server so it serves exactly what the seed
# wrote (data/demo.db, demo key); run uvicorn directly for other settings.
start: .venv/.synced
	@$(RUN) python -c "import socket, sys; s = socket.socket(); \
	sys.exit(0) if s.connect_ex(('127.0.0.1', $(PORT))) else \
	sys.exit('port $(PORT) is already in use; stop the other server first')"
	$(RUN) python -m scripts.seed
	@echo "Serving UI at http://localhost:$(PORT)/ and API docs at http://localhost:$(PORT)/docs"
	env -u DECISIONS_DB -u CARD_REF_KEY \
	$(RUN) uvicorn app.main:create_app_from_env --factory --host 127.0.0.1 --port $(PORT)

verify: .venv/.synced
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) pytest
	$(RUN) python -m scripts.tamper_sim
	$(RUN) python -m scripts.replay_check
	$(RUN) python -m scripts.controls_doc --check

# Replace data/demo.db with fresh sample decisions.
demo: .venv/.synced
	$(RUN) python -m scripts.seed

# Corrupt one record in data/demo.db (default record 3; RECORD=n to choose).
tamper: .venv/.synced
	$(RUN) python -m scripts.tamper $(RECORD)
