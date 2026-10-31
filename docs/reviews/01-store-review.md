# Review 01: hash-chained store (plan step 7)

**Scope:** commits `7e9cb28` (canonical JSON and chain hashing), `60153c9` (hash-chained decision store) and `845c0c6` (tamper simulation in `make verify`). These were reviewed at HEAD `845c0c6` against `docs/plan.md` §2.2–2.5 and §4, `docs/PRD.md` acceptance criteria A2 and A7, `docs/challenges/01-plan-challenge.md` and its recorded author decisions, and `AGENTS.md`. I also read `app/engine.py`, `app/rules.py` and `rules/v1.yaml`. Every finding marked "confirmed" was reproduced by a throwaway probe that ran the real code against temporary SQLite files.

**Verdict:** The core chain is sound. Verify catches every single-record edit I tried in the middle of the chain. Appends stay serialised across threads and processes, and the payload format matches §2.3. Nothing here blocks step 7. The main gaps are:

- an undocumented blind spot at the head of the chain (F1)
- an insert that can be silently dropped while the append still reports success (F2)
- a feature-window coupling that fails open after a rule change (F4)

## Summary of findings

| Id | Title | Severity | Property |
| --- | --- | --- | --- |
| F1 | Editing the latest record (or any trailing suffix) and recomputing its hash passes verify; the documented limits do not cover this | Medium | 1 |
| F2 | An INSERT that writes no row still returns a recorded decision, and the same id is issued twice | Low | 2, 3 |
| F3 | `RecordInput` accepts a full card number in `card_masked` or `card_ref` and writes it into the immutable log | Low | 5 |
| F4 | The feature window comes from the first `attempts_over` rule only: a second velocity rule silently uses the wrong count, and a rule set with no velocity rule always records `HISTORY_UNAVAILABLE` | Medium | 4 |
| F5 | `parse_canonical` accepts `NaN`, `Infinity` and floats, which SQLite's JSON functions read differently | Info | 1, 3 |
| F6 | The tamper simulation (T10) only tries a mutation the first hash check catches, and T42's replay half is claimed but not tested | Low | 6 |
| F7 | Verify gives a nonsensical gap reason when the first row has id ≤ 0 | Info | 1 |

---

## F1. Editing the latest record (or any trailing suffix) and recomputing its hash passes verify

**What.** Verify checks each record against its own hash and against the next record's `prev_hash`. Nothing comes after the latest record, so if someone edits it and recomputes its `hash` column, verify still reports `intact: true`. The same holds for an edit to record k when records k..n are relinked (a suffix rewrite), and for a new record forged at the head. PRD A2 says "Editing any stored record makes `verify` fail".

The plan lists only two blind spots, in §2.5 L222–223 and R16 L497: the last record being *deleted*, and a rewrite of the *whole* chain. An in-place edit of the latest record is neither. It keeps the record count, and it is a single-row change.

This matters most where it hurts. The velocity and new-merchant features read the most recent records for a card. That is the failure scenario from challenge 01, item 1, and it still works through a suffix rewrite.

**Where.**
- `app/store.py` L186–210 (`verify`) and L213–247 (`_broken_reason`)
- `docs/plan.md` L218–223 and R16 L497

**How to trigger (confirmed).**
```python
# 5-record chain, then with raw SQL:
p = json.loads(payload_of(5)); p["outcome"] = "decline"
data = canonical_json(p)
UPDATE decisions SET payload=data, hash=sha256(data) WHERE id=5
store.verify()  # -> intact=True
```
Other probe results:
- **Suffix rewrite:** changing `input.card_ref` in records 3–5 and relinking them gave `intact=True`.
- **Forged head record:** a new record 6 appended with the correct link gave `intact=True`.
- **Velocity evasion:** I wrote five attempts for one card 5 s apart, rewrote their `card_ref` and relinked them. The sixth attempt then got `features={'attempts_in_window': 1, ...}` and `outcome=approve`, and verify reported intact. Without the rewrite, the sixth attempt gets `attempts_in_window=6` and `step_up`. (That probe rewrote all 5 records of a 5-record chain. The separate suffix-only probe above shows the same works when earlier records exist.)

**Severity: Medium.** An unkeyed hash chain cannot detect this without an external anchor, so the fix in this slice is documentation. But it falls outside the limits the plan and challenge record as accepted, and it meets A2 only partially.

Smallest fix (S):
- Restate R16 and §2.5 as "edit or rewrite of the latest record or any trailing run of records, including forged new records, is undetectable without an external head anchor".
- Carry the same wording into `docs/limitations.md`.
- Add the test below so the limit is visible.

**Suggested failing test.**
```python
@control("AUD-01")
@pytest.mark.xfail(strict=True, reason="R16: latest-record edit with rehash needs an external head anchor")
def test_edit_of_latest_record_with_recomputed_hash_is_detected(chain, raw):
    append_quiet(chain, 5)
    payload = stored_payload(raw, 5)
    payload["outcome"] = "decline"
    overwrite(raw, 5, canonical(payload), rehash=True)
    assert not chain.verify().intact
```

---

## F2. An INSERT that writes no row still returns a recorded decision

**What.** `append` runs the INSERT and COMMIT and returns the payload without checking that a row was written. A `BEFORE INSERT` trigger that does `RAISE(IGNORE)` makes SQLite skip the row without raising. The append then returns `id: n` and an outcome as if the decision were recorded, and verify still reports intact. The next append is issued the same id.

R13 says a decision that cannot be recorded returns 503 and no decision. Here a decision is handed out that the chain never holds, and two different decisions carry the same id.

Triggers sit outside the hash, and §2.4 plans triggers on this table, so a mis-written trigger is a realistic path, not only an attacker one.

**Where.** `app/store.py` L170–174.

**How to trigger (confirmed).**
```python
store.append(txn(1), V1)
raw.execute("CREATE TRIGGER swallow BEFORE INSERT ON decisions BEGIN SELECT RAISE(IGNORE); END")
store.append(txn(2), V1)      # returns id 2, outcome approve
# SELECT count(*) -> 1; verify -> intact=True, records_checked=1
raw.execute("DROP TRIGGER swallow")
store.append(txn(3), V1)      # returns id 2 again
```
The probe also confirmed that `cursor.rowcount` is `0` in this case, so checking it detects the skipped row.

**Severity: Low.** Creating the trigger needs write access to the database. Even so, the store returns a decision it never recorded, and the guard is one line: `if cursor.rowcount != 1: raise StoreWriteError(...)` before COMMIT. Size S.

**Suggested failing test.**
```python
@control("AUD-02")
def test_insert_that_stores_no_row_raises_store_write_error(chain, raw):
    append_quiet(chain, 1)
    raw.execute("CREATE TRIGGER swallow BEFORE INSERT ON decisions BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(StoreWriteError):
        chain.append(txn(transaction_id="T-0002"), V1)
    raw.execute("DROP TRIGGER swallow")
    assert len(rows(raw)) == 1
```

---

## F3. `RecordInput` accepts a full card number and writes it into the immutable log

**What.** `RecordInput` forbids a `card_number` field, but `card_masked` and `card_ref` are plain `str`. Any value passes, including a full PAN, and it is hashed into an append-only record. It can never be removed without breaking the chain from that record onward.

The store is a real boundary, not only an API internal. The plan has `scripts/seed.py` append "through the real engine and store" (plan L37), and `scripts/tamper_sim.py` and the tests already call `Store.append` directly. AGENTS.md says "Never log full card numbers. Mask to the last 4 digits", and plan §2.1 fixes both formats:
- `card_masked` is `****` plus 4 digits
- `card_ref` is 32 hex characters

**Where.**
- `app/store.py` L48–61 (`RecordInput`)
- `app/store.py` L163 (written into the payload)

**How to trigger (confirmed).**
```python
store.append(RecordInput(..., card_masked="4111111111111111", card_ref="4111111111111111", ...), V1)
# b"4111111111111111" in db_bytes + wal_bytes -> True
```

**Severity: Low.** It needs a caller bug, but once triggered it is permanent and cannot be cleaned up. Smallest fix (S): add `Field(pattern=r"^\*{4}\d{4}$")` on `card_masked` and `Field(pattern=r"^[0-9a-f]{32}$")` on `card_ref`.

**Suggested failing test.**
```python
@control("DAT-01")
@pytest.mark.parametrize("field", ["card_masked", "card_ref"])
def test_store_refuses_a_full_card_number(field):
    with pytest.raises(pydantic.ValidationError):
        txn(**{field: "4111111111111111"})
```

---

## F4. The feature window comes from the first `attempts_over` rule only

**What.** `read_features` takes `window_seconds` from the first `attempts_over` rule (`next(...)`) and stores a single `attempts_in_window`. The engine then compares that same count against every `attempts_over` rule's `max_attempts`. The loader accepts both of these rule sets:

1. **A second velocity rule with a different window** (for example 3 attempts in 3600 s). It is evaluated on the 60 s count, so its `window_seconds` is silently ignored and it fails open.
2. **No velocity rule at all.** `next()` raises `StopIteration`, which the broad except turns into `features: null`. Every decision then records `HISTORY_UNAVAILABLE` and `step_up`, even though history was available and `merchant_seen_before` could have been computed.

New rule versions in YAML are the supported change path (AGENTS.md: "Rules live in `rules/*.yaml` with a version"). So the configured window and the behaviour can disagree with nothing to warn anyone.

**Where.**
- `app/store.py` L81–108, especially L92–94
- `app/engine.py` L84–87
- `app/rules.py` L89–101 (no constraint on the number of `attempts_over` rules)

**How to trigger (confirmed).**
- **Case 1:** I added `HIGH_VELOCITY_HOUR` (`attempts_over`, `window_seconds: 3600`, `max_attempts: 2`, 40 points) to a copy of v1. Three earlier attempts at −30, −20 and −10 minutes plus the current one is 4 attempts in the hour, which is more than 2. The result was `features={'attempts_in_window': 1, ...}`, `reasons=[]`, `outcome=approve`.
- **Case 2:** v1 with `attempts_over` removed gave `features=None`, `reasons=[HISTORY_UNAVAILABLE]`, `step_up`, and the log line `WARNING:app.store:history unavailable: StopIteration`.

**Severity: Medium.** v1 is not affected today. But a valid-looking rule version can silently fail open with no error at load time, and the explanation recorded for case 2 is false.

Smallest fix (S): have the rule loader require exactly one `attempts_over` rule. That matches the plan's single `attempts_in_window` feature (§2.3) and its fail-closed loading (§3). Supporting several windows would change the hashed `features` shape and need a `record_version` bump, so leave that out.

**Suggested failing test** (in `tests/test_rules.py`):
```python
@control("VER-01")
@pytest.mark.parametrize("count", [0, 2])
def test_rule_set_must_have_exactly_one_attempts_over_rule(tmp_path, count):
    data = yaml.safe_load((RULES_DIR / "v1.yaml").read_text())
    velocity = [r for r in data["scored_rules"] if r["type"] == "attempts_over"][0]
    others = [r for r in data["scored_rules"] if r["type"] != "attempts_over"]
    extra = [{**velocity, "code": f"HIGH_VELOCITY_{n}", "window_seconds": 3600} for n in range(count - 1)]
    data["scored_rules"] = others + ([velocity] + extra if count else [])
    (tmp_path / "v1.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(RuleSetError):
        load_rule_sets(tmp_path)
```

---

## F5. `parse_canonical` accepts `NaN`, `Infinity` and floats

**What.** Python's `json.loads` accepts the non-standard tokens `NaN`, `Infinity` and `-Infinity`, and `json.dumps` writes them back unchanged. So `parse_canonical` passes them as canonical. Canonical floats such as `0.0` also pass, although plan §2.3 says the payload "never contains floats". SQLite 3.45.1 (the version bundled with this venv) disagrees on the tokens:
- `json_valid('{"a":NaN}')` is `0`
- `json_extract` returns `NULL` for `NaN`, where Python reads `nan`

That is the same kind of parser disagreement that challenge 01 asked the canonical check to close.

Any edit to a record in the middle of the chain still breaks the next link (confirmed: a `"score":0.0` rewrite of record 3 with its hash recomputed failed at record 4). So this only adds to what F1 already allows at the head of the chain.

**Where.** `app/chain.py` L41–57.

**How to trigger (confirmed).** Replacing `"score":0` with `"score":NaN` in record 5 of 5 and recomputing its hash gives `intact=True`. Separately, `parse_canonical(b'{"a":NaN}')` returns `{'a': nan}` without raising.

**Severity: Info.** No impact beyond F1 today. The fix is two arguments to `json.loads` (`parse_constant` and `parse_float` that raise `NonCanonicalPayload`). Size S.

**Suggested failing test.**
```python
@control("AUD-01")
@pytest.mark.parametrize("data", [b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', b'{"a":0.5}'])
def test_parse_canonical_rejects_non_json_constants_and_floats(data):
    with pytest.raises(NonCanonicalPayload):
        parse_canonical(data)
```

---

## F6. The tamper simulation only tries a trivially caught mutation, and T42 is only half tested

**What.**
- **T10 (`scripts/tamper_sim.py`).** For each k, the simulation flips `outcome` in record k without recomputing the hash. Verify's very first check (stored bytes against the `hash` column) catches that. The simulation would still pass if verify lost its link check, its gap check, its canonical check or its payload-id check. It never edits a column other than `payload`, never recomputes a hash and never deletes a row. It meets the letter of T10 ("tamper record k, verify names k"), but adds almost nothing beyond T07. The unit tests (T08, T09, T39, T40 and the prev_hash-column test) do cover those checks.
- **T42.** The commit message says it covers T42. The plan's T42 also requires "replay ignores `received_at`", and nothing tests that yet; replay does not exist until step 8. The clock-independence half is real: clock A would make a clock-reading rule see night.

**Where.**
- `scripts/tamper_sim.py` L44–55 and L58–77
- `tests/test_store.py` L522–546
- commit `60153c9` message

**How to trigger (confirmed by reasoning and probe).** Every simulation run reports the reason `hash of record k does not match its payload`, the first branch of `_broken_reason` (`app/store.py` L229–232). As a mutation check, I replaced `parse_canonical` with plain `json.loads`: T39 and T40 then fail (verify breaks at 4 instead of 3), while the simulation's mutation would still be caught at the hash check.

**Severity: Low.** The unit tests cover the other paths. But `make verify` is the evidence shown to reviewers, and its tamper step is weaker than it looks.

Smallest fix (S):
- Add a second mutation per k (edit and recompute the hash, expecting first broken = k+1 for k < n) plus one deletion, mirroring the `make tamper` behaviour planned in §2.4.
- Mark T42's replay clause as pending for step 8.

**Suggested failing test.**
```python
@control("AUD-01")
@pytest.mark.parametrize("mutation, expected", [("flip", 3), ("flip_rehash", 4), ("delete", 4)])
def test_tamper_sim_mutations_are_located(tmp_path, mutation, expected):
    path = tmp_path / "sim.db"
    chain = tamper_sim.build_chain(path)
    tamper_sim.MUTATIONS[mutation](path, 3)
    assert chain.verify().first_broken_id == expected
```

---

## F7. Verify gives a nonsensical gap reason when the first row has id ≤ 0

**What.** A row inserted with id `0`, or a negative id, is detected. But the reason reads "record 0 follows record 0; records 1 to -1 are missing", which is wrong.

**Where.** `app/store.py` L223–228.

**How to trigger (confirmed).** `INSERT INTO decisions VALUES (0, '1'*64, '{}', '2'*64)` on a 5-record chain gives `intact=False, first_broken_id=0` with the reason above.

**Severity: Info.** Detection is correct; only the wording an auditor reads is wrong. Fix: when `record_id <= prev_id`, report "record {id} is out of sequence".

**Suggested failing test.**
```python
@control("AUD-01")
def test_record_before_record_1_is_reported_out_of_sequence(chain, raw):
    append_quiet(chain, 2)
    raw.execute("INSERT INTO decisions VALUES (0, ?, '{}', ?)", ("1" * 64, "2" * 64))
    result = chain.verify()
    assert (result.intact, result.first_broken_id) == (False, 0)
    assert "-1" not in result.reason
```

---

## Areas found sound

Every one of the six properties has at least one finding above. These are the parts of each that I tried to break and that held.

**1. Undetected changes.** These were all detected at the expected record (probes plus existing tests):
- edits to `payload` (with or without recomputing the hash), the `prev_hash` column, the `hash` column and the payload `id`
- swapping ids 3 and 4
- deleting a middle record, and gaps
- non-canonical rewrites (key order, whitespace, `\u0030` escape variants, uppercase escapes)
- duplicate keys, including at nested levels

Type tricks are refused or are harmless:
- `STRICT` together with `NOT NULL` makes SQLite refuse a BLOB or NULL payload (`IntegrityError`).
- If the table is rebuilt without `STRICT`, the `typeof(payload)` check catches a BLOB payload (existing test).
- `INTEGER PRIMARY KEY` coerces a text id `'3'` back to integer 3, so nothing changes.
- Duplicate ids are impossible under the primary key.

An added column passes verify, but no code reads it: every query names its columns or reads `payload`.

**2. Serialised appends and partial writes.**
- Connections use `isolation_level=None`, and `BEGIN IMMEDIATE` takes the write lock before the head is read (L141–145). Features, the engine run and `received_at` all happen under that lock (existing test).
- `UNIQUE` on both `prev_hash` and `hash` is present (T28).
- Four separate processes appending 10 records each produced ids 1–40, all linked, verify intact, and no errors.
- **T26 can fail:** downgrading `BEGIN IMMEDIATE` to a deferred `BEGIN` through a connection proxy made 18 of 20 appends fail with `database is locked`, so the test does detect the regression.
- Every exit path leaves nothing behind. A clock that raises mid-transaction left 0 rows and released the write lock. The single INSERT is atomic.
- Note for step 8: exceptions that are not `sqlite3.Error` (for example a failing clock) escape as raw exceptions rather than `StoreWriteError`. The API needs to map them, or they will surface as 500 rather than 503. Either way no decision is returned.

**3. Meaning outside the hash.**
- `json_extract` only reads `input.card_ref`, `input.timestamp` and `input.merchant_id`.
- Canonical-only, duplicate-free payloads remove the first-key/last-key disagreement from challenge 01. T39 and T40 discriminate: replacing `parse_canonical` with `json.loads` makes both fail.
- Timestamps are compared in Python as timezone-aware datetimes, not as SQL strings.
- There are no indexes, views, collations or schema defaults that affect meaning. Triggers are covered by F2.

**4. Clocks and rules leaking into decisions.**
- `decide` receives only the five transaction fields and the features. `received_at` never reaches it.
- The window is `[t − window, t]` on the transaction's own timestamp, with history limited to records with a smaller id, which is the fixed set the plan's replay design needs.
- The window test uses an hourly clock, so a window based on `received_at` would fail it. T42 would fail if any rule read the clock.
- Rule files are loaded in `sorted()` order and keyed by integer version. Reasons follow YAML order, and JSON keys are sorted.

**5. Card data.**
- `extra="forbid"` stops a `card_number` field from reaching the store.
- The only log call writes the exception class name.
- `StoreWriteError` carries SQLite's message, which never includes bound values.
- The `repr` of `RecordInput` shows only the masked value and the reference.

**6. Test quality.**
- The expected payload, PRD example score, velocity edges and rules hash are written out by hand from the plan. The rules hash is checked against a hand-written dictionary, not against the function under test.
- T07, T08, T09, T39, T40, T26 and T28 each target the check the plan names, and can fail when that check is removed or weakened.
- The one gap is the tamper simulation and T42 (F6).

**Assumptions.**
- "Documented limits" means exactly what plan §2.5 and R16 state: deletion of the latest records, and a rewrite of the whole chain.
- An attacker with direct database write access is in scope, as the plan's `make tamper` implies.
- Probes ran on SQLite 3.45.1 under Python 3.12.3 from the repository's virtualenv.
