# Review 03: demo seed, tamper command and start target (plan step 9)

**Scope:** This review covers commit `beec46b` ("Add demo seed, tamper command and start target"), checked at HEAD `27ffce0`. The two later commits, `e3a16d1` and `27ffce0`, are covered only where they touch step 9. I checked it against:

- `docs/plan.md`: §1 file layout and configuration, §2.4 tamper and triggers, §3 public routes, §4 T32, T33 and T14, §5 step 9, §6 must list, D1–D5 and R25/R28
- `docs/PRD.md`: A2, A3, A8, A9, and the start and walkthrough text at L95–113
- `docs/challenges/01-plan-challenge.md`
- `docs/reviews/01-store-review.md` and `docs/reviews/02-api-review.md`
- `AGENTS.md`

I read every file the commit touches, plus `app/store.py` (connection handling) and the T14 test.

All probes ran on a detached worktree of `27ffce0` under `.amp/in/`. `main` moved on to `bfa1dce` and then `86f60a1` during the review; those commits are not reviewed. Port 8000 was already taken by another `uvicorn` started from the main checkout. I left it alone and ran real `make start` servers on ports 8010, 8011 and 8012 using `PORT=…`.

Public PyPI refused `uv sync` (TLS error, then 403), as D1 expects on this network. I therefore pointed the worktree's `.venv` at the repository's existing `.venv` and ran make as `make -o .venv …`. Other probes:

- real `curl` against the servers
- `sqlite3` edits of the worktree's `data/demo.db`
- a local copy of the T33 CI script with the port changed
- one mutation run on `app/main.py`

Tool versions: Python 3.12.3, SQLite 3.45.1, FastAPI 0.162.0, Pydantic 2.14.2, uvicorn 0.54.0, pytest 9.2.0, uv 0.12.10, GNU Make 3.81.

**`make verify` at `27ffce0` (run as `make -o .venv verify`) exits 0:**
- `ruff check`: all checks passed
- `ruff format --check`: 24 files already formatted
- `pytest`: 162 passed, 1 xfailed
- `scripts.tamper_sim`: 13 of 13 alterations located
- `scripts.replay_check`: passed

**Verdict:** The step does what the plan and the PRD walkthrough need, and these held under probing:

- **Start.** `make start` checks the port before it seeds, seeds 14 records and serves `/`, `/docs` and `/openapi.json` with 200. A second start on a busy port is refused before the seed runs.
- **Demo data.** The samples are synthetic and go through the real request model, engine and store. They cover all six v1 reason codes and all three outcomes. Decisions are identical across processes with different `TZ` and `PYTHONHASHSEED`.
- **Card numbers.** No full card number reached the database, the server log or any command output.
- **Tamper.** `make tamper` names the altered record through the running server. It always restores the triggers it drops, including on failure, and it cannot be pointed at another database by argument or environment.
- **CI.** T33 fails the job when start fails.
- **Roles.** T14 covers the new public route.

Nothing is High. One Medium finding should be fixed before the step 10 and 11 walkthrough is written:

- **F1:** running `make tamper` twice on the same record silently undoes the first tamper, so verify reports intact right after the command says it tampered.

The rest are Low or Info:

- **F2:** `make start` seeds one configuration and serves another.
- **F3:** a failed first install sticks.
- **F4:** the seed script deletes any path it is given.
- **F5:** tamper shows tracebacks for SQLite errors.
- **F6:** test gaps around the start path and the per-sample expectations.

## Summary of findings

| Id | Title | Severity | Property / T-id affected |
| --- | --- | --- | --- |
| F1 | A second `make tamper` on the same record restores the original bytes, so verify reports intact although the command reports a tamper | Medium | A2 walkthrough (PRD L111), plan §2.4, the slice's done criterion |
| F2 | `make start` always seeds `data/demo.db` with the demo key, but the server it starts honours `DECISIONS_DB` and `CARD_REF_KEY`. The served database can then be empty, `make tamper` can hit a file the server does not serve, and seeded velocity history is not linked | Low | A9, A2 walkthrough, plan §1 configuration, R7 |
| F3 | A failed first `uv sync` leaves `.venv` newer than `pyproject.toml`. The retry skips the install and fails with `ModuleNotFoundError` (rule from step 3, now on the A9 path) | Low | A9, T33, T37 |
| F4 | `python -m scripts.seed <path>` deletes and overwrites any file, with no guard like tamper's | Low | AGENTS.md "Do not add features beyond them", data safety |
| F5 | `make tamper` exits with a raw traceback for an empty, non-SQLite or locked database | Info | AGENTS.md "clear message" convention |
| F6 | T32 does not exercise `create_app_from_env` or the Makefile, and the seed's per-sample expected outcomes are not asserted, contrary to its comment | Info | T32, A9 |

---

## F1. A second `make tamper` on the same record undoes the first

**What.** `FLIPPED` maps `approve → decline`, `decline → approve` and `step_up → approve`. A second run on a decline or approve record therefore writes back the original outcome. The payload is re-serialised canonically, so the bytes match the stored hash exactly and verify reports `intact: true`. The script still prints "tampered record 3 … Verify the chain as auditor to see it named."

The default record, 3, is a decline. So a reviewer who runs `make tamper` twice (repeating the PRD L111 walkthrough without `make demo` in between) sees a "tamper" that verify does not catch. That looks exactly like a failure of the headline control, A2. 7 of the 14 demo records (1, 3, 4, 5, 12, 13, 14) behave this way. Step-up records never return to their original value, so they stay detected.

**Where.**
- `scripts/tamper.py` L21 (`FLIPPED`)
- `scripts/tamper.py` L35–49: the payload is read and edited with no check that the record is still unaltered
- No test runs tamper twice (`tests/test_demo.py` L102–141)

**How to trigger (confirmed).**
```text
$ make demo && make tamper && make tamper
tampered record 3 in .../data/demo.db: outcome decline -> approve, hash left unchanged. Verify the chain as auditor to see it named.
tampered record 3 in .../data/demo.db: outcome approve -> decline, hash left unchanged. Verify the chain as auditor to see it named.
Store(data/demo.db).verify() -> intact=True records_checked=14 first_broken_id=None

$ make demo && make tamper RECORD=6 && make tamper RECORD=6     # step_up record
outcome step_up -> approve, then approve -> decline
verify -> intact=False first_broken_id=6
```

**Smallest fix (S).**
1. Select `hash` together with `payload`. If `sha256(payload)` no longer equals `hash`, refuse with "record k is already altered; run make demo to reset" and exit 1.
2. Add a test that calls `tamper.main([])` twice: the second call returns 1, and verify still names record 3.

---

## F2. `make start` seeds one configuration and serves another

**What.** Plan §1 makes `DECISIONS_DB` and `CARD_REF_KEY` the service configuration, "with defaults in the Makefile". The Makefile defines neither. Instead, `scripts.seed` always writes `DEFAULT_DB` with `DEMO_CARD_REF_KEY` and reads neither variable, while `create_app_from_env` honours both. With either variable set in the shell, `make start` serves something other than what it just seeded:

- **`DECISIONS_DB` set.** The served database is empty or stale. `make tamper`, which is correctly pinned to `data/demo.db`, then alters a file the server does not read, so the auditor walkthrough cannot show a break.
- **`CARD_REF_KEY` set.** Seeded records carry `card_ref` values made with the demo key. Live requests for the same demo cards do not link to that history: velocity and new-merchant features differ, and the outcome can drop from decline to step-up.

**Where.**
- `Makefile` L16–22: seed at L20 and uvicorn at L22, with no environment pinned
- `scripts/seed.py` L124–135 (L133 uses `DEMO_CARD_REF_KEY`)
- `app/main.py` L157–162

**How to trigger (confirmed).**
```text
$ DECISIONS_DB=$PWD/../alt.db CARD_REF_KEY=other-key make -o .venv start PORT=8011
seeded 14 demo decisions into .../review03/data/demo.db
GET /audit/verify (auditor) -> {"intact":true,"records_checked":0,"first_broken_id":null,"reason":null}
$ make tamper   -> "tampered record 3 in .../review03/data/demo.db" (not the served alt.db)

# The 7th burst attempt (card A, M-ONLINE-77, 2026-11-01T14:00:45-05:00) on a freshly seeded DB, via TestClient:
card_ref_key=demo key   -> features {attempts_in_window: 7, merchant_seen_before: true}, score 70, decline
card_ref_key=other-key  -> features {attempts_in_window: 1, merchant_seen_before: false}, score 30, step_up
```
I did not capture the server's verify after that tamper, because the request raced the server shutdown. The tamper output shows it altered the unserved file.

**Smallest fix (S).** Make `start` serve exactly what it seeded. Either run uvicorn as `env -u DECISIONS_DB -u CARD_REF_KEY $(RUN) uvicorn …`, or set both to the demo values on that line. Running `uvicorn` directly with the environment stays available for custom configurations. Alternatively, have `seed` read `CARD_REF_KEY` the same way the app does, and have `start` refuse when `DECISIONS_DB` resolves to anything other than `data/demo.db`.

---

## F3. A failed first install leaves `.venv` looking up to date

**What.** The `.venv: pyproject.toml` rule runs `uv sync` and then `touch .venv`. When `uv sync` fails after creating the environment, which is what happens on a fresh clone with no index access, the `.venv` directory already exists and is newer than `pyproject.toml`. The next `make start` skips the install and fails inside `scripts.seed` with `ModuleNotFoundError: No module named 'fastapi'`, which does not point at the install.

The rule dates from step 3 (`a782b55`). Step 9 puts it on the A9 path: a fresh clone runs `make start`, CI runs T33, and the author runs T37.

**Where.** `Makefile` L9–11.

**How to trigger (confirmed).** Run on the fresh worktree, where the index is unreachable:
```text
$ make start
uv sync
Creating virtual environment at: .venv
error: Failed to fetch: `https://pypi.org/simple/pyyaml/` ... invalid peer certificate: UnknownIssuer
make: *** [.venv] Error 2
$ stat -f "%m %N" .venv pyproject.toml
1793493555 .venv
1793493458 pyproject.toml
$ make start PORT=8010
uv run --no-sync python -m scripts.seed
ModuleNotFoundError: No module named 'fastapi'
make: *** [start] Error 1
```

**Smallest fix (S).** Use `uv sync || { rm -rf .venv; exit 1; }`. Alternatively, make the target a stamp file such as `.venv/.synced` that is touched only after a successful sync.

---

## F4. The seed script deletes any path it is given

**What.** `scripts/seed.py` takes an optional path argument. `seed()` unlinks that file and its `-wal` and `-shm` siblings, then writes a demo database there. The plan describes the script only as "seed demo decisions" into the demo database. `make` never passes the argument, but it is documented in the module docstring. Unlike `scripts/tamper.py`, which refuses anything but `data/demo.db`, the seed script has no guard, so a mistyped path destroys the file.

**Where.**
- `scripts/seed.py` L7 (usage)
- `scripts/seed.py` L124–128 (the unlinks)
- `scripts/seed.py` L138–141 (`argv[0]` as the path)

**How to trigger (confirmed).**
```text
$ echo important > ../victim.txt
$ uv run --no-sync python -m scripts.seed ../victim.txt
seeded 14 demo decisions into ../victim.txt
$ file ../victim.txt
../victim.txt: SQLite 3.x database, last written using SQLite version 3045001, ...
```

**Smallest fix (S).** Remove the `argv` path from `main` and always seed `DEFAULT_DB`. Keep the `path` parameter of `seed()` for tests. Alternatively, apply the same `resolve() == DEFAULT_DB.resolve()` guard as tamper.

---

## F5. `make tamper` shows raw tracebacks for SQLite errors

**What.** `main` catches `ValueError`, `LookupError` and `TamperRefused`, but not `sqlite3.Error`. A zero-byte or foreign `data/demo.db`, or a database whose write lock is held for more than the default 5 s timeout (for example during a long append), ends in a Python traceback. The transaction rolls back, so nothing is altered and the exit status is non-zero. Only the message is unclear.

**Where.** `scripts/tamper.py` L32–34 and L60–69.

**How to trigger (confirmed).**
```text
: > data/demo.db; make tamper            -> sqlite3.OperationalError: no such table: decisions
echo hello > data/demo.db; make tamper   -> sqlite3.DatabaseError: file is not a database
another connection holds BEGIN IMMEDIATE -> after 5.5 s: sqlite3.OperationalError: database is locked
```

**Smallest fix (S).** Add `sqlite3.Error` to the `except` in `main`, with "tamper failed: …; run make demo to recreate the demo database".

---

## F6. Test gaps around the start path and the demo expectations

**What.**

1. **T32 skips the start path.** T32 builds the app with `create_app(path)` in-process. That matches the plan's wording ("api" layer), but nothing in `make verify` exercises `create_app_from_env` or the `start` recipe. Only CI's T33 does, after a push.
2. **Per-sample outcomes are not asserted.** `scripts/seed.py` L34 says "Expected v1 outcomes are noted for the demo walkthrough; tests check them". The tests assert the set of reason codes, that one 65 step-up exists, that record 3 is a decline, and that all three outcomes appear. They do not assert each sample's score and outcome, such as DEMO-0005 25 approve, DEMO-0006 30 step-up or DEMO-0012 70 decline. The step 11 README walkthrough will rely on those values. I did not run a mutation for this part.

**Where.**
- `tests/test_demo.py` L47–64 (T32) and L67–85
- `scripts/seed.py` L34
- `app/main.py` L157–162

**How to trigger (confirmed for item 1).**
```text
mutant: `raise RuntimeError("mutant")` as the first line of create_app_from_env
uv run --no-sync pytest -q -> 162 passed, 1 xfailed
```

**Smallest fix (S).**
- In T32, monkeypatch `DECISIONS_DB` to the seeded temporary file and build the app with `create_app_from_env()`.
- Add a parametrised test of `(transaction_id, score, outcome)` for every sample, written from the comments in `SAMPLES`.

---

## Areas found sound

- **Start path.** On port 8010:
  - The port check runs before the seed.
  - The output was `seeded 14 demo decisions into …/data/demo.db`, then uvicorn started.
  - `GET /` returned 200 `text/html`, `GET /docs` 200 and `GET /openapi.json` 200.
  - Auditor verify reported intact with 14 records.

  The guard was probed twice. `make start` on the occupied port 8000 printed "port 8000 is already in use; stop the other server first" and exited 2, and `data/` was never created. A second `make start PORT=8010` was refused the same way, before the seed line ran. The guard is port-based by design (R25): a server for the same database on another port is not detected, and `make demo` has no guard (R28). I ran `make demo` under a running server: the server kept working, verify reported intact with 14 records, and a following POST gave record 15 and intact. This works because `Store` opens one connection per call. I did not probe an append that is in flight during a reseed.
- **Demo samples.**
  - **Real append path.** Every sample goes through `TransactionRequest.model_validate` → `to_record` → `Store.append` with the active rule set. There are no hand-built records.
  - **Outcomes match the comments.** Approve: records 1, 5, 13 and 14. Step-up: record 2 (score 65), record 6 (30), and burst records 7–11 (30 each). Decline: record 3 (stolen card), record 4 (blocklisted merchant) and record 12 (70: `FOREIGN_COUNTRY` + `HIGH_VELOCITY`).
  - **Synthetic card numbers.** The card numbers are public test values: `4111111111111111`, `5555555555554444`, and `4539578763621486`, which is published in the creditcard.js documentation, among others.
  - **Compatible with `e3a16d1`.** All timestamps pass the new ISO-with-offset pattern and the 1970 lower bound.
- **Determinism.** Seeding under `TZ=UTC`, `Pacific/Kiritimati` and `America/Los_Angeles`, with `PYTHONHASHSEED` set to 1, 2 and random, gave identical decision content (SHA prefix `9246a92781ff29db` over all payloads without `received_at` and `prev_hash`). Head hashes differed between runs only because `received_at` is the real append time, as §2.3 requires. Nothing in the plan or the commit claims a fixed demo head hash, and fixing one would mean faking `received_at`.
- **Data minimisation (A8).** No full card number appeared in `data/demo.db` or its WAL, the uvicorn log (which shows `card=****1486`), verify output or make output. No other 12-digit run appeared either, apart from fragments of hex hashes. The seed configures no logging.
- **Tamper target.**
  - **Pinned to the demo database.** `main` always passes `DEMO_DB`, which is derived from the location of `app/main.py`. `RECORD` is only the record id, and no environment variable is read. A tamper of another path is refused (tested).
  - **Symlink caveat (Info).** A symlink placed at `data/demo.db` is followed, because both sides are `resolve()`d: tamper altered `../other/prod.db` that way. Reaching it needs the user to create the link.
- **Tamper inputs.** `RECORD=1` and `RECORD=14` (the last record) were named by verify as records 1 and 14. Record 15, 0, -1, `three` and `3.0` gave a clear error, exit 1, and an intact chain. A missing database printed "run make demo first", and no file was created. `" 3"`, `1_0` and `"3 5"` were accepted as 3, 10 and 3, which is harmless.
- **Triggers.** The drop, the edit and the re-create run in one `BEGIN IMMEDIATE` transaction, and SQLite DDL is transactional. With `BEFORE UPDATE` and `BEFORE DELETE` triggers installed:
  - **Failure.** A failure mid-tamper (a record with payload `{}`, giving "tamper failed: 'outcome'") left both triggers in place.
  - **Success.** A successful tamper restored both triggers, and raw `UPDATE` and `DELETE` were still refused with "append-only" afterwards.
- **PRD walkthrough against a live server.**
  - An analyst POST returned 201 and an auditor POST returned 403.
  - After `make tamper`, auditor verify returned `first_broken_id: 3` with "hash of record 3 does not match its payload; record 3 was altered".
  - Replay of record 3 returned `match: false`.
- **T33 / CI.** I ran the step's script locally with the port changed to 8012:
  - **Success path.** "make start answered after 2s", exit 0.
  - **Failure path.** With `RUN=false`, start failed at once, and the step printed the log and exited 1 after 61.7 s.

  `make start` runs only after `make verify` succeeds. The workflow is still `permissions: contents: read` with `persist-credentials: false`, and there is no deploy job. The background server is left to the runner's orphan-process cleanup.
- **T32.** Its wording matches the plan, and it correctly carries no control tag (A9 has none). It does fail if seeding breaks: it checks the count, list length, intact verify and all three outcomes. The extra tamper tests are tagged `AUD-01`, which fits A2.
- **Role exposure.** `("GET", "/")` is the only addition to `PUBLIC_ROUTES`. It serves a static page with no script and no data. T14's matrix includes it and still asserts that the served routes equal the matrix. Step 9 adds no other route. The placeholder page holds no logic, and the route handler only returns the file.
- **No new dependencies.** `pyproject.toml` is unchanged.
- **Commit hygiene.** The commit has the `Co-authored-by: Amp` trailer and no thread id. As a minor standards note, adding `.DS_Store` to `.gitignore` is unrelated to the step's purpose ("one purpose per commit").

**Assumptions.**
- The review is pinned to `27ffce0`, as the brief asked. `bfa1dce` and `86f60a1` landed on `main` during the review and were not examined.
- The real fresh-clone install was not verified. The public index was unreachable from this machine, and the D1 mirror was not configured in my shell. "Installs" in A9 is therefore covered only by the failure mode in F3 and by reasoning about CI.
- Port 8000 belonged to another process, which I did not touch. Every server I started was on port 8010–8012 and was stopped. T33 was simulated locally, not run on GitHub Actions.
- In F1, I assume every run of a command described as "corrupt one record" should leave the chain detectably broken.
- In F2, the plan's "defaults in the Makefile" means `make start` should serve the configuration it seeds.
