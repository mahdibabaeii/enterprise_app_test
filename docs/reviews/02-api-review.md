# Review 02: decision API and roles (plan step 8)

**Scope:** This review covers commit `d3fdf36` ("Add decision API and roles"), checked at HEAD `d3fdf36` against `docs/plan.md` §1–§7 (the step table, the T-id table, the API, role and data-handling design, and decisions D1–D5 and risks R1–R30), the acceptance criteria in `docs/PRD.md`, `docs/challenges/01-plan-challenge.md` and its author decisions, `docs/reviews/01-store-review.md`, and `AGENTS.md`. I read every file the commit touches, plus `app/rules.py`, `app/chain.py`, `scripts/tamper_sim.py` and `rules/v1.yaml`. Every finding marked "confirmed" was reproduced by a throwaway probe against the real code: FastAPI `TestClient` against temporary SQLite files, one real `uvicorn` run of `create_app_from_env`, and mutation runs on a scratch copy of the code. The probes ran on Python 3.12.3, SQLite 3.45.1, FastAPI 0.162.0 and Pydantic 2.14.2. At HEAD, `pytest` reports 146 passed and 1 xfailed, and `scripts.replay_check` passes.

**Verdict:** The step mostly does what it claims. These held under probing:

- **Roles.** Role enforcement denies by default, and no auditor, unknown or missing role reached `POST /decisions` or any other write path.
- **Card numbers.** No card number reached a log (including real uvicorn access and error logs), a response or the database files.
- **Fail safe and replay.** The 503 path, the history-failure step-up and replay of failed-safe records behave as R13 and T45 describe.
- **Concurrency.** T27 fails if serialisation is weakened.

Every claimed T-id has a test that matches the plan's wording and carries the plan's control tag. Nothing is High. One Medium finding should be fixed before step 9 seeds demo data:

- **F1:** a timestamp sent as epoch-seconds text gets past the "explicit offset" rule and is read as UTC. For the same instant, that can turn a step-up into an approve.

The rest are Low or Info:

- **F2:** a narrow path where the API returns 503 "no decision" for a decision that was in fact recorded.
- **F3:** a 422 path that echoes input keys.
- **F4:** unmapped exceptions that give 500.
- **F5:** a determinism replay that cannot see clock, timezone or hash-seed dependence.
- **F6:** an undocumented and untested 409.

## Summary of findings

| Id | Title | Severity | Property / T-id affected |
| --- | --- | --- | --- |
| F1 | Epoch-seconds text is accepted as a timestamp and read as UTC, which skips the offset requirement and can flip the outcome | Medium | §2.1 / R5 input contract, PRD strict validation, T34, A1/A4 by extension |
| F2 | If SQLite rolls back the transaction itself during the history read, the API returns 503 "no decision was made" but the decision is committed | Low | R13, T43, AGENTS.md serialised appends |
| F3 | 422 bodies echo input keys (an unexpected field named with a card number is returned), and the 422 body has no Pydantic model | Low | R19, T30, DAT-01, AGENTS.md "Pydantic models for all … response bodies" |
| F4 | Unmapped exceptions give 500: an id too large for SQLite on get and replay, a failing clock on create, and replay while history is unreadable | Low | §3 status table, AGENTS.md error mapping |
| F5 | `scripts/replay_check.py` runs both builds in one process at one moment, so it cannot detect clock, timezone or hash-seed dependence, and T04 has no control tag | Low | T04, REP-01, A1, controls document |
| F6 | Replay of a record whose rule version is not loaded returns 409, which is not in the plan, and neither that path nor replay under a non-active version is tested | Info | §2.5, §3 status table, VER-01 |

---

## F1. Epoch-seconds text is accepted as a timestamp and read as UTC

**What.** Plan §2.1 says the timestamp is "ISO 8601 with an explicit UTC offset… Missing offset gives 422", and R5 makes that offset the merchant's local offset. `TransactionRequest._iso_text` rejects non-string values so that "an epoch number" cannot get through, and T34 tests only the JSON-number case. Pydantic's lax datetime parser also accepts *strings* of digits as Unix timestamps and returns them as UTC-aware datetimes, so `AwareDatetime` accepts them. A request with no offset at all is then decided as if the merchant were at UTC.

The night rule reads the hour in the stored offset. The same instant can therefore score differently depending only on how the caller formats it, and the difference can go towards approve. The stored record also shows a timestamp with an offset (`+00:00`) that the caller never sent.

As a secondary point in the same validator, `0001-01-01T00:00:30+00:00` is accepted. `read_features` then overflows when it computes the window start, so the record says `HISTORY_UNAVAILABLE` even though history was available. The outcome still fails safe, but the explanation is false.

**Where.**
- `app/models.py` L44–52 (the `timestamp` field and `_iso_text`)
- `tests/test_api.py` L349–350 (T34 covers only the epoch *number*)
- `app/store.py` L122 (the overflow in the secondary case)

**How to trigger (confirmed).**
```text
timestamp "1793502000"                -> 201, stored "2026-11-01T03:00:00+00:00"
timestamp "1793502000.5"              -> 201
# Same instant, AU merchant, 1,500 AUD, new merchant:
ISO   "2026-11-01T03:00:00+11:00"     -> score 35 [UNUSUAL_AMOUNT, NEW_MERCHANT_AT_NIGHT] step_up
epoch "1793462400"                    -> stored 2026-10-31T16:00:00+00:00, score 25 [UNUSUAL_AMOUNT] approve
timestamp "0001-01-01T00:00:30+00:00" -> 201, features null, [HISTORY_UNAVAILABLE], step_up
                                         (log: "history unavailable: OverflowError")
```
In the same probe, the space separator, lowercase `z`, `+1100` and minute-only forms were also accepted. Those all carry an offset, so they do not break the contract.

**Smallest fix (S).** In `_iso_text`, require ISO 8601 text with an explicit offset before Pydantic parses it, for example by matching `^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?([Zz]|[+-]\d{2}:?\d{2})$`. Add T34 cases for `"1793502000"` and `"1793502000.5"`. Optionally, bound the year (for example ≥ 1970) so the window cannot overflow.

---

## F2. A transaction rolled back by SQLite during the history read gives 503 with a recorded decision

**What.** `Store.append` opens `BEGIN IMMEDIATE` and reads features inside a broad `except` that turns any failure into `features = None`. For some errors (`SQLITE_IOERR`, `SQLITE_FULL`, `SQLITE_NOMEM`, `SQLITE_BUSY`), SQLite rolls the transaction back by itself. The connection uses autocommit mode (`isolation_level=None`), so the following `INSERT` then runs outside any transaction and commits at once. The explicit `COMMIT` fails with "no transaction is active", which becomes `StoreWriteError` and a 503 that tells the caller "no decision was made".

The chain then holds a step-up decision the caller was told does not exist, which breaks R13 and T43 ("no record added"). The insert also runs after the write lock has been released. Serialisation then depends only on `UNIQUE(prev_hash)`, not on `BEGIN IMMEDIATE` as AGENTS.md and §2.4 intend.

The code dates from step 7. Step 8 is where the 503 contract is made and claimed (T43).

**Where.**
- `app/store.py` L170–212, in particular the `except` at L177–181 and the `COMMIT` at L197–203
- `app/main.py` L105–110

**How to trigger (confirmed by simulation; the real SQLite I/O error was not reproduced).** I patched `store.read_features` to run `ROLLBACK` (which is what SQLite does itself on those errors) and then raise `sqlite3.OperationalError("disk I/O error")`.
```text
POST /decisions -> 503 {"detail":"decision could not be recorded, so no decision was made; ..."}
GET /decisions/1 -> features null, reasons [HISTORY_UNAVAILABLE], outcome step_up
GET /audit/verify -> intact true, records_checked 1
```
T18's monkeypatch raises before touching the connection, so it does not cover this path.

**Smallest fix (S).** After the feature read (success or failure), `if not connection.in_transaction: raise StoreWriteError("transaction was rolled back")`. Add a test using the same simulation that asserts 503 and zero records.

---

## F3. 422 bodies echo input keys, and the 422 body has no model

**What.** The custom handler removes `input` and `ctx`, but keeps `loc`. For an unexpected field, `loc` contains the caller's key, so any value the caller puts in a key is returned. R19 says "a card number is never returned", and the commit message says "422s never echo input".

Separately, the handler builds a plain dict. OpenAPI still advertises FastAPI's default `ValidationError` schema with `input` and `ctx`. That is the opposite of what the API returns, and it goes against AGENTS.md's "Pydantic models for all request and response bodies".

**Where.** `app/main.py` L69–76, and the route `responses` at L99–104, which have no 422 entry.

**How to trigger (confirmed).**
```text
POST {...valid..., "4539578763621486": "x"} ->
422 {"detail":[{"loc":["body","4539578763621486"],"msg":"Extra inputs are not permitted","type":"extra_forbidden"}]}
GET /openapi.json -> ValidationError properties: ['loc','msg','type','input','ctx']
```
Every other 422 I tried returned no input value: a Luhn failure, a pattern failure, a card number sent as a JSON number, a card number as the amount or the timestamp, a newline-suffixed card number, a list body, malformed JSON and a non-integer path id.

**Smallest fix (S).** In the handler, keep only `loc` entries that are declared field names (or cut `loc` to `["body"]` when `type == "extra_forbidden"`). Declare a `ValidationErrorResponse` Pydantic model and register it as `422` in `responses`. Add a T30 case with a card number as the key.

---

## F4. Unmapped exceptions give 500 instead of a specific status

**What.** Three paths give a generic 500:

1. **Ids too large for SQLite.** `decision_id` is an unbounded `int`. A value above 2^63−1 raises `OverflowError` when SQLite binds it, so get and replay return 500 instead of 404 or 422.
2. **A failing clock.** Exceptions that are not `sqlite3.Error` inside `append`, such as a failing injected clock, are not wrapped in `StoreWriteError`, so `POST /decisions` returns 500. Review 01 flagged this for step 8: "The API needs to map them, or they will surface as 500 rather than 503."
3. **Replay while history is unreadable.** Replay calls `read_features` without handling errors, so a history read failure at replay time gives 500.

No decision is returned on any of these paths, and no record is written. The caller contract (treat any 5xx as step-up) still holds, so this is not a fail-open. It does go against AGENTS.md's "Errors map to specific HTTP status codes with a clear message".

**Where.**
- `app/main.py` L121 and L132 (the `decision_id: int` parameters)
- `app/store.py` L163–212 (only `sqlite3.Error` is wrapped, at L204–207, while the clock is read at L187)
- `app/store.py` L260–266

**How to trigger (confirmed).**
```text
GET /decisions/99999999999999999999         -> 500 (uvicorn log: OverflowError: Python int too large to convert to SQLite INTEGER)
GET /decisions/99999999999999999999/replay  -> 500
POST /decisions with clock raising RuntimeError -> 500, records 0
GET /decisions/2/replay with read_features raising OperationalError -> 500
```

**Smallest fix (S).**
- Declare `decision_id: Annotated[int, Path(ge=1, le=2**63 - 1)]`, which gives a sanitised 422.
- In `append`, catch `Exception` (not only `sqlite3.Error`) as `StoreWriteError`, so the API returns its existing 503 "no decision".
- In replay, map a history failure to 503 with a clear message.

Add one test for each.

---

## F5. The determinism replay cannot detect clock, timezone or hash-seed dependence

**What.** `replay_check.py` builds two databases in the same Python process, one straight after the other, then replays in that same process. Anything that varies between processes or over time is identical in both runs, so the head hashes match and every replay matches:

- the real server clock
- the process `TZ`
- `PYTHONHASHSEED` (set or hash ordering)

The script fails only on in-process randomness and on replay or feature bugs. It also catches a server-clock rule during daytime hours, but only because its coverage check sees that `NEW_MERCHANT_AT_NIGHT` never fired. Run between 00:00 and 05:59 local time, the same mutation passes.

The pytest suite does catch all three mutants (T02, T22, T05 and others), so this is not a hole in REP-01 overall. But the step `make verify` labels "determinism replay" adds little beyond the unit tests. Separately, T04 is a script, not a tagged test, so the controls document generated from tags will not list it under REP-01.

**Where.**
- `scripts/replay_check.py` L80–86 (`run`) and L96–129 (`main`)
- `Makefile` L17

**How to trigger (confirmed by mutation, on a scratch copy of `app/` and `scripts/`).**
```text
A  night hour = datetime.now().hour, run at 11:24 local   -> replay check FAILED: batch never fired NEW_MERCHANT_AT_NIGHT
A' same, simulated at 03:00 (now().replace(hour=3))       -> "replay check passed: identical head hashes, every replay matches"
B  night hour = txn.timestamp.astimezone().hour (process TZ) -> passed
C  reasons.sort(key=hash(code)) (hash-seed dependent)      -> passed
D  random.shuffle(reasons) (positive control)              -> FAILED: record 189 does not replay ...
pytest on A' / B / C                                       -> 4 / 5 / 6 failures (T02, T22, T05, ...)
```

**Smallest fix (S).**
- Build the second database in a subprocess with a different `TZ` (for example `Pacific/Kiritimati`) and a different `PYTHONHASHSEED`, and compare head hashes across the two processes.
- Use `rule_sets[1]` explicitly, and pin the expected head hash for the fixed seed, clock and key in the script. Leakage from the clock or hour then fails at any time of day.
- Add a tagged pytest wrapper (`@control("REP-01")`) that calls `replay_check.main()`, so T04 appears in the controls document.

---

## F6. The 409 for a missing rule version is undocumented, and replay across versions is untested

**What.** If a record names a rule version that is not loaded, replay raises `RuleVersionUnavailable` and the route returns 409 with a clear message. That is a reasonable choice, but the plan's §3 status table has no 409 and no test exercises it. Replay of a record whose version is loaded but is not the active one works correctly (it uses the stored version), but no test covers that either. Plan §2.5 step 2 and VER-01 depend on this behaviour.

**Where.**
- `app/main.py` L127–139
- `app/store.py` L249–254

**How to trigger (confirmed).**
```text
record 1 under v1, add v2 (UNUSUAL_AMOUNT 45), record 2 under v2:
  replay 1 -> match True, rules_match True, features_match True, score 35   (v1 used)
  replay 2 -> match True, rules_match True, features_match True, score 55   (v2 used)
delete v1.yaml, restart: replay 1 -> 409 {"detail":"rule version 1 is not loaded; record 1 cannot be replayed"}
```

**Smallest fix (S).** Add a 409 row to the plan §3 status table, and add two VER-01 tests: replay under a non-active loaded version, and 409 for a missing version.

---

## Areas found sound

- **Role enforcement.** The allowlist is keyed by `(method, route.path)`, and an unlisted route gives 403 to both roles (tested). I also probed these cases:
  - **Header name and value variants.** The header name is case-insensitive. The values `Analyst`, `ANALYST`, `" analyst "`, `auditor, analyst` and an `X_Role` header name all give 401.
  - **Order of checks.** An auditor posting an invalid body gets 403 before validation runs. `PUT`, `PATCH` and `DELETE` on `/decisions/1` give 405, `POST /audit/verify` gives 405, and `POST /decisions/` only redirects.
  - **Duplicate headers.** With two `X-Role` headers, the first one wins, which matters little because the header is self-asserted (an accepted limitation).
  - **Malformed JSON.** It gets 422 before the role check, but no write happens.
  - **Record counts.** After all of these, the record count did not change.
  - **Verify.** `/audit/verify` is auditor-only, and its reasons hold no input values.
- **Card number handling.** The pattern and Luhn check happen at the boundary, and `card_number` has `repr=False`. `to_record` derives `card_masked` and the HMAC `card_ref` (first 32 hex characters) and drops the number. `RecordInput` patterns refuse a full number (review 01 F3). In a real uvicorn run, a 201, two 422s, a 401 and a 500 traceback left no 12-digit run of the card number in the logs. The database and WAL files held none either. The key comes from `CARD_REF_KEY`, defaulting to the public demo key as R20 describes. Replay never recomputes `card_ref`, so changing the key does not affect replay. One minor note: `CARD_REF_KEY=""` is accepted silently.
- **The 503 path (apart from F2).** A busy timeout and an `IntegrityError` both give a body with only `detail`, no record, and a fixed message that does not leak SQLite text.
- **History failure.** T18 gives 201 with `step_up`, `features: null` and a single `HISTORY_UNAVAILABLE` reason, and verify reports intact. T45 then replays with `match: true` and `features_match: null`.
- **Replay semantics.** `match` compares score, reasons and outcome. `features_match` recomputes features from records with a smaller id, using the stored version's window. `rules_match` compares the hash of the canonical rule set. All of this matches §2.5 and R17/R18.
- **Concurrency.** Changing `BEGIN IMMEDIATE` to `BEGIN`, or removing the transaction altogether, makes T27 fail (503s).
- **Server clock.** It is read only for `received_at` (`app/store.py` L105–106, L187). No rule or replay path reads it.
- **Layering.** The route handlers only parse requests, call the store and map errors. Masking lives in `models.py`, as §1 places it.
- **No new dependencies.** `pyproject.toml` is unchanged.
- **T-id coverage.** T03, T05, T12, T13, T14 (including "a route missing from the matrix fails"), T18, T27, T29, T30, T31, T38, T42 (replay half), T43, T44 and T45 each match the plan's wording and carry the plan's control tags. T34 and T35 are correctly untagged. T04 is covered by the script, with the gaps in F5.
- **Review 01 fixes.** F1–F7 are not regressed: the rowcount guard, the card-field patterns, exactly one velocity rule, rejection of floats and NaN, the strengthened tamper simulation and the out-of-sequence wording are all intact. Step 8 changes `append` only by extracting `_engine_transaction` and adding the INFO line after commit.
- **Commit hygiene.** The commit has the `Co-authored-by: Amp` trailer and no thread id.

**Assumptions.**
- In F2, the explicit `ROLLBACK` stands in for SQLite's documented automatic rollback on I/O, full-disk, out-of-memory and busy errors. A real disk error was not reproduced.
- "Never echo input" covers keys as well as values.
- A 5xx that returns no decision is fail-safe under the caller contract in §3, which is why F4 is Low.
