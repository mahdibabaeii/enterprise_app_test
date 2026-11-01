# Plan: Tamper-Evident Card Decision Service (vertical slice)

Status: revised after challenge 01. The author accepted the decisions recorded
in `docs/challenges/01-plan-challenge.md`, and this plan includes them. The
author approved D1–D5 in section 7 as proposed. Approved for build. No code has
been written.
Sources: `docs/PRD.md`, `AGENTS.md`, `docs/challenges/01-plan-challenge.md`.

The slice is done when a reviewer can run `make start`, make a decision as an
analyst, replay it, see an auditor get 403 on create, run `make tamper`, and
watch `GET /audit/verify` name the first broken record. Everything else is
secondary.

The author's decisions D1–D5 are recorded in section 7.

---

## 1. File layout

```
Makefile                  start, verify, demo, tamper
pyproject.toml            dependencies, ruff config, pytest marker registration
.gitignore                .venv/, data/, __pycache__/
README.md                 one-command start, non-goals, limitations summary
rules/
  v1.yaml                 rule set version 1: thresholds, hard rules, scored rules
app/
  __init__.py
  main.py                 FastAPI app, routes, role dependency, error handlers, static UI
  models.py               Pydantic request/response models, card masking
  rules.py                load and validate rules/*.yaml into typed models
  engine.py               pure decide(txn, features, ruleset) -> Decision; no I/O, no clock
  store.py                SQLite: serialised append, get, list, history features, verify
  chain.py                canonical JSON and SHA-256 record hashing
  static/index.html       the one-page UI (vanilla JS, no business logic)
scripts/
  seed.py                 seed demo decisions through the real engine and store
  tamper.py               corrupt one record in data/demo.db only (refuses other paths)
  tamper_sim.py           verify step: tamper a throwaway DB, expect detection
  replay_check.py         verify step: determinism replay over a synthetic batch
tests/
  controls.py             `control = pytest.mark.control` so tests use @control("AUD-01")
  conftest.py             fixtures: tmp DB, test client, test rule sets
  test_rules.py
  test_engine.py
  test_chain.py
  test_store.py
  test_api.py
  test_data_handling.py
.github/workflows/verify.yml   read-only permissions, runs make verify on push
data/                     demo.db lives here, gitignored
docs/
  PRD.md, plan.md
  challenges/             challenger output and decisions
```

Layering: `main.py` (API) handles roles, request parsing and status codes only.
`engine.py` makes the decision. `store.py` owns SQLite and the chain. The engine
never touches the database. The store passes history-derived features to it.

Configuration (environment variables, with defaults in the Makefile):
- `DECISIONS_DB`, default `data/demo.db`. Tests always use a temporary file.
- `CARD_REF_KEY`, the HMAC key for card references. The demo default is a fixed,
  public string because all data is synthetic. The limitations doc says so, and
  says a production key belongs in a vault (see risk R20).

---

## 2. Decision record and what is hashed

### 2.1 Transaction input (request body of `POST /decisions`)

The PRD does not define the input schema (R9). Proposed, strict
(`extra="forbid"`), all fields required:

| Field | Type and validation |
| --- | --- |
| `transaction_id` | string, 1–64 chars, `[A-Za-z0-9_-]` |
| `card_number` | string of 12–19 digits, Luhn-valid. Never stored or logged in full |
| `card_status` | `active` or `reported_stolen`, supplied by the upstream card system (D3) |
| `amount_minor` | integer, > 0, ≤ 100,000,000 (cents; no floats anywhere) |
| `currency` | `AUD` only in v1 (R10) |
| `merchant_id` | string, 1–64 chars, `[A-Za-z0-9_-]` |
| `merchant_country` | ISO 3166-1 alpha-2, uppercase |
| `timestamp` | ISO 8601 with an explicit UTC offset, which must be the merchant's local offset (input contract, R5). Missing offset gives 422 |

At the API boundary `card_number` becomes:
- `card_masked`: `****` plus the last 4 digits, e.g. `****1111`.
- `card_ref`: HMAC-SHA256(`CARD_REF_KEY`, card_number), first 32 hex characters.
  Used only to link a card's history for velocity and new-merchant checks.

The full number is dropped after these two values are computed.

### 2.2 Stored record

SQLite table `decisions`:

| Column | Notes |
| --- | --- |
| `id` | INTEGER PRIMARY KEY, chain position 1, 2, 3, … assigned inside the write lock |
| `prev_hash` | TEXT NOT NULL UNIQUE. Hash of record `id - 1`, or 64 zeros for record 1 |
| `payload` | TEXT. The hashed payload below, written exactly as its canonical JSON |
| `hash` | TEXT NOT NULL UNIQUE. SHA-256 hex of the stored `payload` bytes (UTF-8) |

There are no other columns, so nothing outside the hash can be edited unnoticed.
Queries (history, list) use SQLite `json_extract` on `payload`. The data is small,
so this is fast enough.

### 2.3 Hashed payload (exactly these fields, nothing else)

```json
{
  "record_version": 1,
  "id": 7,
  "prev_hash": "<64 hex>",
  "received_at": "2026-11-01T03:00:01.123456+00:00",
  "rule_version": 1,
  "rules_sha256": "<sha256 of the canonical JSON of the validated rule set>",
  "input": {
    "transaction_id": "T-0007",
    "card_masked": "****1111",
    "card_ref": "<32 hex>",
    "card_status": "active",
    "amount_minor": 200000,
    "currency": "AUD",
    "merchant_id": "M-1234",
    "merchant_country": "GB",
    "timestamp": "2026-11-01T03:00:00+00:00"
  },
  "features": {
    "attempts_in_window": 1,
    "merchant_seen_before": false
  },
  "score": 65,
  "reasons": [
    {"code": "FOREIGN_COUNTRY", "points": 30},
    {"code": "UNUSUAL_AMOUNT", "points": 25},
    {"code": "NEW_MERCHANT_AT_NIGHT", "points": 10}
  ],
  "outcome": "step_up"
}
```

Field rules:
- `prev_hash` is inside the payload, so `hash = sha256(stored payload bytes)`
  covers "canonical JSON plus the previous record's hash" in a single step.
- `id` is hashed, so records cannot be reordered or renumbered.
- `received_at` is the server's UTC time when the append ran, read from an
  injected clock inside the write lock. It is audit evidence only: rules never
  read it, and replay does not compare it. The rule "never the server clock"
  still holds for decisions (challenge 01, item 2).
- `rule_version` is an integer, so "highest version" ordering is unambiguous.
- `rules_sha256` is the SHA-256 of the canonical JSON of the validated rule set,
  not of the file bytes. Comment or line-ending changes do not affect it. An edit
  to rule content under the same version does, and replay reports it (R18).
- `features` are hashed, so the history the decision relied on is part of the
  evidence. When history was unavailable, `features` is `null`.
- `reasons` are ordered as the rules appear in the YAML, giving a stable order.
- When a hard rule fires, `score` is `null` and `reasons` holds only the hard
  rule's code with `"points": null` (R11).
- On a rule or history error, `reasons` gets `{"code": "RULE_ERROR" |
  "HISTORY_UNAVAILABLE", "points": null}`. A `RULE_ERROR` entry also carries the
  failing rule's code as `"rule": "<code>"`.
- The payload contains only strings, integers, booleans, null, lists and objects.
  It never contains floats.

Canonical JSON: `json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=True)` encoded as UTF-8. The store writes exactly these bytes, and
the hash is SHA-256 of them, in lowercase hex.

### 2.4 Append (serialised)

Connections use autocommit mode (`isolation_level=None`) so the explicit
transaction below is the only one, and the database runs in WAL mode so a long
verify read does not block writers. Each append runs in one SQLite transaction
opened with `BEGIN IMMEDIATE`:

1. Read the last record's `id` and `hash`.
2. Compute features from earlier records.
3. Run the engine.
4. Read `received_at` from the injected clock, then build and hash the payload.
5. INSERT, then COMMIT.

`BEGIN IMMEDIATE` takes SQLite's write lock, so a second writer waits until the
first commits and then reads the new head. As a second guard, the `UNIQUE`
constraint on `prev_hash` means the database itself refuses two records that
extend the same hash (A7). A busy timeout of 5 seconds avoids spurious failures
under the concurrency test. An `IntegrityError` or an expired busy timeout maps
to 503 with no decision (R13).

There is no update or delete path in the code. (Should) SQLite triggers abort any
UPDATE or DELETE on `decisions`. `make tamper` drops the triggers if they exist,
edits a row, and restores them if they existed, to show that someone with
database access can bypass them and verify still catches it. It works whether or
not the triggers have been built.

### 2.5 Verify and replay

`GET /audit/verify` walks records in `id` order and stops at the first failure.
A record fails when any of these holds:

- its `id` is not previous + 1 (a gap means a deleted record)
- `sha256(stored payload bytes)` does not equal `hash`
- the stored payload is not canonical: parsing it with an `object_pairs_hook`
  that rejects duplicate keys fails, or re-serialising it canonically does not
  give the stored bytes exactly
- `payload.id` does not equal the column `id`
- `payload.prev_hash` or the `prev_hash` column does not equal the previous record's `hash`

The canonical check closes the duplicate-key gap found in challenge 01: Python's
`json.loads` keeps the last duplicate key while SQLite's `json_extract` keeps the
first, so a payload with a duplicate key could read differently in verify and in
history queries.

It returns `{"intact": true, "records_checked": n}` or `{"intact": false,
"records_checked": n, "first_broken_id": k, "reason": "..."}`.

If a record is edited and its own hash recomputed, the break shows at the next
record as a link mismatch. Verify cannot tell which of the two records was
altered, so `first_broken_id` is the later record and the reason names both,
for example "prev_hash of record 4 does not match hash of record 3; record 3 or
4 was altered". Nothing is detected when the latest records are deleted, when
the latest record or any trailing run of records is edited and relinked with
recomputed hashes, when a forged record is appended at the head, or when the
whole chain is rewritten. Without an external head anchor every one of these
looks like an intact chain. They go in the limitations doc (R16, review 01 F1).

`GET /decisions/{id}/replay`:

1. Load the stored payload.
2. Load the rule set for `rule_version` and compare its canonical hash with
   `rules_sha256` (`rules_match`). Rule content is kept in git, not in the
   database (challenge 01, item 1b overruled).
3. Re-run the engine on the stored input and the stored `features`. Stored
   `null` features replay as history unavailable. Compare `score`, `reasons`
   and `outcome` (`match`).
4. If stored `features` is not `null`, recompute them from records with a
   smaller `id` and compare (`features_match`). This set is fixed because the log
   is append-only. If stored `features` is `null`, `features_match` is `null`.

It returns `{"id", "match", "rules_match", "features_match", "stored",
"replayed"}`. A correctly failed-safe decision therefore replays with
`match: true`.

---

## 3. Rules, engine and API (to make the tests concrete)

`rules/v1.yaml` holds every threshold and point value. Code holds only the logic
for each rule type.

```yaml
version: 1
thresholds: {step_up: 30, decline: 70}   # <30 approve, 30-69 step_up, >=70 decline
home_country: AU
hard_rules:
  - {code: CARD_REPORTED_STOLEN, type: card_status_is, status: reported_stolen}
  - {code: MERCHANT_BLOCKLISTED, type: merchant_in_list, merchants: [M-BLOCK-001, M-BLOCK-002]}
scored_rules:
  - {code: FOREIGN_COUNTRY,       type: foreign_country,        points: 30}
  - {code: UNUSUAL_AMOUNT,        type: amount_at_least,        amount_minor: 100000, points: 25}
  - {code: HIGH_VELOCITY,         type: attempts_over,          window_seconds: 60, max_attempts: 5, points: 40}
  - {code: NEW_MERCHANT_AT_NIGHT, type: new_merchant_at_night,  night_start_hour: 0, night_end_hour: 6, points: 10}
```

Engine order:
1. Hard rules, each inside its own try/except. If one matches, the outcome is
   `decline` and the engine stops; no history is read. A hard rule that raises
   adds `RULE_ERROR` and forces at least `step_up`; the remaining hard rules and
   the scored rules still run.
2. Features are passed in already computed. If the store could not provide them,
   they are `null`, and the outcome is at least `step_up` with
   `HISTORY_UNAVAILABLE`. Rules that need features are skipped.
3. Each scored rule runs inside its own try/except. A rule that raises adds
   `RULE_ERROR` and forces at least `step_up`.
4. The total is mapped through the thresholds.
5. The final outcome is the most severe of the score outcome and any fail-safe
   floor. Fail-safe never downgrades a decline.

Rule set loading: all `rules/*.yaml` files are loaded at startup and validated
by Pydantic. An unknown rule type, a missing field or a duplicate version stops
the service from starting (fail closed). The active version is the highest
version number (R27). Replay uses the version stored in the record.

Endpoints:

| Endpoint | Roles | Success |
| --- | --- | --- |
| `POST /decisions` | analyst | 201, decision with `thresholds` for display |
| `GET /decisions` | analyst, auditor | 200, the last 20 decisions, newest first (R15) |
| `GET /decisions/{id}` | analyst, auditor | 200 |
| `GET /decisions/{id}/replay` | analyst, auditor | 200 |
| `GET /audit/verify` | auditor | 200 |
| `GET /`, `/docs` | no role needed | UI and Swagger |

Role enforcement is a per-route allowlist that denies by default: a route with
no allowlist entry refuses every role. The only public routes are `/`, `/docs`
and the OpenAPI schema, and they are listed explicitly. T14 enumerates
`app.routes`, so a new route without an entry fails the test. The `X-Role`
header is self-asserted. Forging it is a documented, accepted limitation
(challenge 01, item 4), not a red-team finding.

Errors and status codes:

| Status | When |
| --- | --- |
| 401 | `X-Role` missing or not `analyst`/`auditor` (R14) |
| 403 | The role is valid but not allowed on this endpoint |
| 404 | Unknown decision id |
| 409 | Replay of a record whose rule version is not loaded, so it cannot be replayed |
| 422 | Validation failure. A custom handler removes the echoed input values so a card number is never returned (R19) |
| 503 | The database cannot be written, including `IntegrityError`, an expired busy timeout and any other failure inside the append. No decision is returned (R13). Also a replay whose history cannot be read |

Logging: one INFO line per decision with `id`, `outcome`, `score` and
`card_masked`. A rule error logs a WARNING with the rule code and the exception
class, never input values or the exception message. Request bodies are never
logged.

Caller contract (stated in the README): a caller that receives no decision,
whether a 503, a 5xx or a timeout, must treat the transaction as step-up.

---

## 4. Tests mapped to acceptance criteria

Expected values come from the PRD by hand, not from running the code.
Control tags:
- `EXP-01` explainability
- `REP-01` reproducibility
- `AUD-01` tamper evidence (verify)
- `AUD-02` append-only and serialised appends
- `ACC-01` least privilege
- `FS-01` fail safe
- `DAT-01` data minimisation
- `VER-01` versioned change

A9 has no control in the PRD table, so its tests carry no control tag.

| Id | Test | Layer | Control | AC |
| --- | --- | --- | --- | --- |
| T01 | The same txn, features and rule set decided twice give equal decisions, including with input keys shuffled | engine | REP-01 | A1 |
| T02 | A night-rule decision is identical with process `TZ=UTC` and `TZ=Australia/Melbourne` (uses the txn's own offset, not the server clock) | engine | REP-01 | A1 |
| T03 | POST then replay gives `match: true`, `rules_match: true`, `features_match: true` | api | REP-01 | A1 |
| T04 | `replay_check.py`: 200 seeded synthetic txns (bursts, night, foreign, hard-rule hits) under rule version 1 into two fresh DBs with the same fixed injected clock, the second built in a subprocess with a different `TZ` and `PYTHONHASHSEED`, both give the pinned head hash, and every replay matches | script in verify, plus a tagged test that runs it | REP-01 | A1 |
| T05 | A rule's points edited under the same version make replay report `rules_match: false` | api | VER-01 | A1 |
| T06 | A fresh chain of 5 verifies intact, and an empty chain verifies intact | store | AUD-01 | A2 |
| T07 | Changing `outcome` inside record 3's payload makes verify fail with `first_broken_id: 3` | store | AUD-01 | A2 |
| T08 | Changing record 3 and recomputing its hash makes verify fail at 4 with a link reason naming records 3 and 4 | store | AUD-01 | A2 |
| T09 | Deleting record 3 (bypassing triggers) makes verify fail at 4 with a gap reason | store | AUD-01 | A2 |
| T10 | `tamper_sim.py`: throwaway DB, tamper record k, verify names k | script in verify | AUD-01 | A2 |
| T11 | (should) Raw SQL UPDATE and DELETE on `decisions` are refused by triggers | store | AUD-02 | A2 |
| T12 | Auditor `POST /decisions` gives 403, and the record count is unchanged | api | ACC-01 | A3 |
| T13 | Missing or unknown `X-Role` on POST gives 401, and the record count is unchanged | api | ACC-01 | A3 |
| T14 | Role matrix: every route in `app.routes` × {analyst, auditor, none} gives the expected status, including analyst on verify giving 403. A route missing from the matrix fails the test | api | ACC-01 | A3 |
| T15 | A scored rule that raises, on a txn that would otherwise score 0, gives `step_up` with `RULE_ERROR` | engine | FS-01 | A4 |
| T16 | History unavailable gives `step_up` with `HISTORY_UNAVAILABLE` | engine | FS-01 | A4 |
| T17 | A rule error plus a hard-rule hit stays `decline`, and a rule error plus score ≥ 70 stays `decline` | engine | FS-01 | A4 |
| T18 | Via the API, an injected history failure gives 201 `step_up`, and the decision is recorded in the chain with `features: null` | api | FS-01 | A4 |
| T19 | A rule file with an unknown type, missing points or a non-integer version fails to load | rules | FS-01, VER-01 | A4 |
| T20 | Threshold mapping with v1 thresholds: 0 and 29 approve, 30 and 69 step_up, 70 and 105 decline | engine | EXP-01 | A5 |
| T21 | End to end with a test rule set whose points make 29, 30, 69 and 70 reachable, each outcome is correct | engine | EXP-01 | A5 |
| T22 | PRD example: 2,000 AUD, GB merchant, 03:00 local, new merchant gives 65 `step_up` with the three reason codes and points | engine | EXP-01 | A5 |
| T23 | Every non-hard decision's reason points sum to its score | engine | EXP-01 | — |
| T24 | A stolen card on an otherwise zero-score txn gives `decline` with `CARD_REPORTED_STOLEN` and score null | engine | EXP-01 | A6 |
| T25 | A blocklisted merchant gives `decline` with `MERCHANT_BLOCKLISTED` | engine | EXP-01 | A6 |
| T26 | 20 threads append to one file DB, giving ids 1–20, all `prev_hash` values distinct and linked, verify intact | store | AUD-02 | A7 |
| T27 | 20 concurrent `POST /decisions` through the API give the same result as T26 | api | AUD-02 | A7 |
| T28 | Inserting a second record with an existing `prev_hash` raises IntegrityError | store | AUD-02 | A7 |
| T29 | POST with a full card number: captured logs (all levels), response body and DB file bytes contain no 12+ digit run of it, only `****1111` | api | DAT-01 | A8 |
| T30 | POST with a Luhn-invalid card number gives 422, and the body does not echo it | api | DAT-01 | A8 |
| T31 | Masking: `4111111111111111` becomes `****1111`, and `card_ref` is stable for the same key | models | DAT-01 | A8 |
| T32 | The app starts against a fresh seeded DB, and `GET /` and `GET /docs` return 200 | api | — | A9 |
| T33 | The CI step runs `make start` in the background and polls `http://localhost:8000/` until 200 (60 s timeout) | CI | — | A9 |
| T34 | Bad input gives 422: negative amount, non-AUD currency, timestamp without offset, extra field, non-digit card | api | — | spec |
| T35 | Unknown id gives 404 for get and for replay | api | — | spec |
| T36 | A stored record's `rule_version` and `rules_sha256` equal the loaded rule set | store | VER-01 | — |
| T38 | A comment-only edit to the rule file leaves replay `rules_match: true` | api | VER-01 | A1 |
| T39 | Rewriting record 3's payload with the same values but non-canonical formatting (spaces, key order) makes verify fail at 3 | store | AUD-01 | A2 |
| T40 | Inserting a duplicate `card_ref` key before the real one in record 3's payload makes verify fail at 3 | store | AUD-01 | A2 |
| T41 | A hard rule that raises gives `step_up` with `RULE_ERROR`; a later matching hard rule still gives `decline` | engine | FS-01 | A4 |
| T42 | The same txn appended under two different injected clocks gives the same `features`, `score`, `reasons` and `outcome`, and replay ignores `received_at` | store | REP-01 | A1 |
| T43 | A simulated write failure (`IntegrityError` or busy timeout) gives 503 with no decision in the body and no record added | api | FS-01 | A4 |
| T44 | A rule error logs the rule code and exception class, and no input values | api | DAT-01 | A8 |
| T45 | Replaying the decision recorded in T18 gives `match: true` and `features_match: null` | api | REP-01, FS-01 | A4 |

Plus T37, a manual clean-clone check of `make start` before submission (A9).

Velocity and new-merchant edge cases go into T04's batch and get small engine
tests during the engine step:
- the 6th attempt in 60 s fires, and the 5th does not
- a timestamp exactly 60 s earlier counts

---

## 5. Commit sequence

Each commit leaves `make verify` green. Times are agent build estimates.
Steps 3 to 11 are the must set, about 105 minutes in total after the
challenge 01 changes. That is over the 1.5-hour target, so the cut order in
section 7 applies early.

| # | Commit | Contents | Verify additions | Est. |
| --- | --- | --- | --- | --- |
| 1 | Add plan | `docs/plan.md` (author-edited) | — | — |
| 2 | Add plan challenge and decisions | `docs/challenges/01-plan-challenge.md` | — | — |
| 3 | Add project skeleton and verify target | pyproject, Makefile (`verify` = ruff check, ruff format --check, pytest), `.gitignore`, `tests/controls.py` with marker registration, one trivial app test | lint + tests | 8 |
| 4 | Add CI workflow | `.github/workflows/verify.yml`, read-only, `make verify` on push | — | 2 |
| 5 | Add versioned rule set and loader | `rules/v1.yaml`, `app/rules.py`, T19 | — | 8 |
| 6 | Add decision engine | `app/engine.py`, T01, T02, T15–T17, T20–T25, T41 and velocity edges | — | 18 |
| 7 | Add hash-chained store | `app/chain.py`, `app/store.py`, T06–T09, T26, T28, T36, T39, T40, T42, `scripts/tamper_sim.py` | tamper simulation | 22 |
| 8 | Add decision API and roles | `app/models.py`, `app/main.py`, T03, T05, T12–T14, T18, T27, T29–T31, T34, T35, T38, T43–T45, `scripts/replay_check.py` | determinism replay | 24 |
| 9 | Add demo seed, tamper command and start target | `scripts/seed.py`, `scripts/tamper.py`, `make start/demo/tamper`, T32, CI step T33 | — | 8 |
| 10 | Add one-page UI | `app/static/index.html`: role switcher, form, decision panel, recent table, verify bar. Visual style modelled on cenksari/react-banking-app-template (palette, card panels, typography, badges), reimplemented in plain CSS; no React, no copied code, no build step. When the page is finished, a screenshot of it (`docs/screenshots/ui.png`) is added to the repo-level `README.md` | — | 12 |
| 11 | Add README | start command, demo walkthrough, caller contract, non-goals, limitations summary | — | 3 |
| 12+ | Should items, one commit each | see section 6 | — | — |

The PRD's early signal applies: if steps 5 and 6 are not done and reviewed within
about 30 minutes of build start, cut to the must list without any should items.

UI checks: the UI has no logic to unit test. Step 10 is checked by rendering the
page and walking through both sessions by hand, with screenshots, before the
commit. The screenshots are also compared against the reference template's look.

---

## 6. Must / should / could

### Must (about 1.75 hours after challenge 01, steps 3–11)
- `make start`, `make verify`, `make demo`, `make tamper`
- Versioned YAML rules: 2 hard rules, 4 scored rules, thresholds, fail-closed loading
- Deterministic engine with fail-safe step-up on hard and scored rule errors, and
  reason codes with points
- Hash-chained append-only SQLite store: serialised appends, `prev_hash UNIQUE`,
  WAL, canonical-bytes verify with duplicate-key rejection, `received_at`
- API: create, list, get, replay (`match`, `rules_match`, `features_match`),
  verify; deny-by-default role allowlist; status code mapping; masked card
  numbers; sanitised 422s
- Tests T01–T10, T12–T36 and T38–T45, tagged with control ids
- Tamper simulation and determinism replay inside `make verify`
- One-page UI: role switcher, form, decision panel, recent table, verify bar
- CI workflow running `make verify`
- README with the start command, caller contract, non-goals and limitations summary

### Required after the build (PRD time blocks 1:50 to 3:00)
These are PRD process and evidence items, not build features. They happen in
the PRD's later time blocks rather than inside the 1.5-hour build, and are not
optional (challenge 01).
- Independent test author, reviewer and red-team passes, with findings turned
  into failing tests and then fixes
- Generated controls document from test tags (`docs/controls.md`)
- `docs/limitations.md` and the production path, including: header forgery,
  the public demo `CARD_REF_KEY`, truncation and whole-chain rewrite, unbounded
  client timestamp skew, rule content kept in git rather than the database
- `AI_USAGE.md`

### Should (if time remains, in this order)
1. SQLite triggers blocking UPDATE/DELETE, and T11
2. Replay buttons in the UI (first in the PRD cut order)
3. Load sample button in the UI (second in the PRD cut order)

### Could (not planned)
- Property-based determinism tests with Hypothesis (new dependency, needs approval).
  The seeded batch in T04 covers A1 without it.
- Dependency scan in CI (pip-audit, new dependency)
- Containerfile
- `SPEC.md`: not built (D5)

---

## 7. Risks and ambiguities

### Author decisions (approved as proposed on 2026-11-01)

| # | Question | Decision |
| --- | --- | --- |
| D1 | Dependencies beyond the AGENTS.md stack | Add `uvicorn` (FastAPI needs a server), `PyYAML` (rules are YAML), `httpx` (FastAPI's TestClient needs it). Use `uv` with Python 3.12. Approved. Amended at step 3: public PyPI is blocked on the author's network, so local installs use the corporate package mirror from the shell environment only. Exact versions are pinned in `pyproject.toml`, `uv.lock` is gitignored so no internal URL enters the repo, and CI installs the same pins from public PyPI |
| D2 | Point values (R1) | Use the PRD's starting values unchanged as rule set v1. Approved |
| D3 | Where "card reported stolen" comes from (R8) | `card_status` input field supplied by the upstream card system. Approved |
| D4 | "Unusual amount" (R3) | Fixed threshold in YAML: ≥ 1,000.00 AUD. Approved |
| D5 | `SPEC.md` (R23) | Treat the PRD plus this plan's test table as the spec. No separate SPEC.md. Approved |

### Ambiguities in the PRD, with the proposed reading

| # | Gap | Proposed reading |
| --- | --- | --- |
| R1 | Point values are "starting values to confirm" | v1 uses +30, +25, +40, +10 and thresholds 30 and 70, all in YAML. Changing them later means a new rule version |
| R2 | With v1 points, totals of 29 and 69 cannot be reached, yet A5 tests them | Test the threshold mapping directly (T20), plus a test-only rule set where they are reachable (T21) |
| R3 | "Unusual amount" is undefined: unusual for this card or absolute? | Absolute threshold in YAML. A per-card baseline is a documented limitation |
| R4 | "Foreign country" relative to what? | `merchant_country` ≠ `home_country` in YAML (AU). The alternative is a card issuing-country input field |
| R5 | "Night" undefined, timezone unspecified | 00:00–05:59 in the transaction's own UTC offset, hours in YAML. The input contract says the offset is the merchant's local offset. A timestamp without an offset is rejected |
| R6 | "New merchant" undefined | No earlier record in the chain for the same `card_ref` and `merchant_id` |
| R7 | "More than 5 attempts in 1 minute": what counts as an attempt, is the current one included, is the window inclusive? | Earlier decisions of any outcome for the same `card_ref` with a txn timestamp in [t − 60 s, t], plus the current one. Fires when the total exceeds 5. Uses txn timestamps, not arrival order. Rejected (422) requests are not attempts |
| R8 | No source for stolen-card status | Input field (D3). The alternative is a list of card refs in the rule YAML, which couples rule versions to the HMAC key |
| R9 | No input schema | Section 2.1 |
| R10 | Currency handling unspecified | AUD only in v1, amounts in integer cents. Foreign-currency conversion is out of scope |
| R11 | What score a hard-rule decline shows | `score: null`, because scored rules do not run ("hard rules stop everything") |
| R12 | Fail-safe versus decline precedence | Fail-safe sets a step-up floor. It never downgrades a decline |
| R13 | "History unavailable" and "store unavailable" are the same SQLite file in this slice | History read failure gives step-up and is recorded with `features: null`. If the record cannot be written, the API returns 503 and no decision, because an unrecorded decision would break auditability. The README states that callers must treat no decision as step-up |
| R14 | Status for a missing or invalid role | 401. The PRD specifies only 403 for auditor create |
| R15 | The UI needs a recent decisions list, but the PRD lists only 4 endpoints | Add `GET /decisions` (last 20). It is required by the UI requirement, not a new feature |
| R16 | What the chain cannot detect | Truncation of the latest records; an edit or relinked rewrite of the latest record or any trailing run of records, including a forged record appended at the head; and a consistent rewrite of the whole chain (review 01 F1). A strict xfail test keeps the latest-record case visible. All are documented with the write-once anchoring production path. An edit plus rehash is reported at the next record, with a reason naming both records. `received_at` is stored as evidence, but it comes from the server clock and proves nothing against someone who controls that clock |
| R17 | Replay and velocity: a naive replay re-reads current history and mismatches | Store features in the hashed payload. Replay decides from the stored features (`match`) and separately recomputes them from records with a smaller id (`features_match`) |
| R18 | A rule file edited without a version bump would silently change replays | Store and compare `rules_sha256`, the hash of the canonical validated rule set. Rule content stays in git, not the database |
| R19 | FastAPI/Pydantic 422 responses echo the input, which would leak card numbers | Custom validation error handler without input values (T30) |
| R20 | Linking a card's history without storing the number | HMAC-SHA256 with `CARD_REF_KEY`. An unkeyed hash of a card number can be brute-forced. The demo key is public, which is acceptable only because all data is synthetic; the limitations doc says a production key belongs in a vault. Rotating the key breaks velocity history |
| R21 | Python 3.12 is required but the local Python is 3.11.15, and the second laptop's OS and `make` are unknown (PRD open question) | uv provisions 3.12 (D1). Test the clean-clone path on the second laptop early |
| R22 | Pushing `.github/workflows/` needs a token with `workflow` scope | VS Code's GitHub sign-in normally includes it. If the push is rejected, the author re-authorises in VS Code |
| R23 | The PRD names `SPEC.md` as evidence, but AGENTS.md points to the PRD and plan | No SPEC.md (D5). The README points reviewers to the PRD and this plan's test table |
| R24 | Regulatory references are "to be confirmed" | The controls doc names only the type of expectation until the author confirms references |
| R25 | Port 8000 may be in use | `make start` fails clearly. No automatic port switching |
| R26 | Duplicate `transaction_id` handling unspecified | Not enforced. A duplicate counts as another attempt. Idempotency is a documented limitation |
| R27 | How the active rule version is chosen | Highest integer version among `rules/*.yaml`. Only v1 exists in the slice |
| R28 | `make demo` "reloads sample data", which means discarding the demo DB | Replace the demo DB file and reseed through the real append path. No record is ever updated or deleted in place |
| R29 | Timebox: the PRD's first 30 minutes (spec, AGENTS, verify skeleton) are partly done already, and the skeleton moves into step 3 | No action. Watch the early signal in section 5 |
| R30 | Client timestamps are unbounded, so spacing timestamps can evade velocity | Accepted risk (challenge 01, item 2b). Rejecting skew would need the server clock and break determinism. `received_at` lets an auditor compare. Documented in the limitations |

### Delivery risks

- **Scope.** The must list is about 105 minutes of agent build time, which is
  over the 1.5-hour target. The cut order is: should items, then Replay buttons, then the sample
  loader. Never cut the chain, the verify bar, the tests or `make start`.
- **Mirrored tests.** Implementer tests can copy implementation mistakes. The
  independent test author (required after the build, section 6) works from the PRD and this plan only.
- **Concurrency test flakiness.** SQLite locking under threads can produce
  `database is locked` errors. Use a busy timeout and one connection per
  request, and run T26 and T27 against a file DB, not `:memory:`.
