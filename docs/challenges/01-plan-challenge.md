# Plan challenge 01

Scope: this review covers `docs/PRD.md` and `docs/plan.md` only. The AGENTS.md domain rules are treated as given constraints. Plan citations use section and line numbers in `docs/plan.md` (for example §2.3, L106–160). PRD citations use line numbers in `docs/PRD.md`. One external check was made, an in-memory Python/SQLite run outside the repository, to confirm how duplicate JSON keys are handled. The five decisions below are ordered by how hard they are to reverse, hardest first.

| # | Decision | Verdict |
| --- | --- | --- |
| 1 | What is hashed: record format, canonicalisation, card reference | **CHANGE** |
| 2 | Time handling: the caller's offset decides "night", and no decision time is stored | **CHANGE** |
| 3 | Failure defaults: step-up floor, 503 on write failure, rules fail to load = service won't start | **CHANGE** |
| 4 | Role model: `X-Role` header, 401/403 | **ACCEPT-RISK** |
| 5 | Concurrency: `BEGIN IMMEDIATE` + `UNIQUE(prev_hash)` | **KEEP** |

---

## 1. What is hashed (record format and canonicalisation)

**Decision.** There is one `payload` column. `hash` is "SHA-256 hex of `payload`" (§2.2, L100), and "there are no other columns" (L102). `prev_hash`, `id`, `rules_sha256`, `features`, `card_masked` and `card_ref` all sit inside the payload (§2.3, L106–157). Canonical form is `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=True)` (L159–160). Verify recomputes `sha256(canonical(json.loads(payload)))` (§2.5, L191). History and list queries read the payload with SQLite `json_extract` (L103). `card_ref` is HMAC-SHA256 under `CARD_REF_KEY`, whose demo default is "a fixed, non-secret string" (L59–62, L86–87). `rules_sha256` is "sha256 of the rules file bytes" (L114).

**Strongest case against.**
- **The plan contradicts itself.** §2.2 hashes the stored bytes, but §2.5 parses them and hashes them again. Under §2.5:
  - Any non-canonical rewrite of a payload still passes verify.
  - Duplicate keys let two readers see different values. Python's `json.loads` keeps the last duplicate. SQLite `json_extract` returns the first; this was checked on SQLite 3.53.3: `{"a":"first","a":"last"}` gives `'first'` from SQL and `'last'` from Python.
  - Verify and the history queries therefore read different values from the same stored text.
- **`card_ref` can never be removed.** It goes into append-only records alongside the last 4 digits. With a public key, recovering a PAN means searching only the middle digits, a small space once the BIN and the Luhn check digit are taken into account. Synthetic data makes this acceptable in the slice. Copied to production, it is a PAN-linkage problem that the chain cannot erase.
- **`rules_sha256` covers raw file bytes.** Changing line endings or editing a comment flips `rules_file_match` without any change to the rules. Git `autocrlf` on the second laptop could do this (its OS is unknown, PRD L244). Replay also runs whatever file is on disk now, because the rule content that was in force is not stored in the database (§2.5, L204–208).

**Failure scenario.** An insider with database access adds a duplicate `"card_ref":"000…"` before the real key in the `input` of a card's last 5 records.
- Verify uses Python's last-key-wins parse, so it reports **intact**.
- The velocity query uses `json_extract`, which takes the first key, so it no longer counts those 5 attempts. The next attempt in the burst is **approved**.
- If replay recomputes features through the same store query, as L103 and L206 imply, replay reports `match: true`.

The auditor sees an intact chain and a matching replay for an approval that was made on tampered history.

**Alternative and cost.**
- Verify should require two things: `sha256(stored_bytes) == hash`, and `stored_bytes == canonical(json.loads(stored_bytes))` with duplicate keys rejected through `object_pairs_hook`. Cost: about 5 lines and 2 tests.
- Hash the canonical JSON of the *validated* rule model instead of the file bytes. On first use, store the canonical rule set in a `rule_sets` table keyed by that hash, and have replay load from there. Cost: one table and about 30 lines. It no longer detects comment-only edits, which is the behaviour you want.
- Keep `card_ref`, but state in the limitations that the demo key is public and that a production key belongs in a vault.

**Verdict: CHANGE.** These are small fixes. They must land before the first record is written, because afterwards every change needs a `record_version` migration and verifiers that support both formats forever.

---

## 2. Time handling

**Decision.**
- The timestamp must be ISO 8601 with an explicit offset, otherwise 422 (L82).
- "Night" is 00:00–05:59 "in the transaction's own UTC offset" (R5, L422).
- The velocity window uses transaction timestamps (R7, L424).
- "No server timestamp is stored", which keeps chains byte-identical for the determinism check (L153–155, R16). T04 compares head hashes (L295).

**Strongest case against.**
- **The caller chooses the offset, so the same instant can be night or day.** `2026-11-01T03:00:00+00:00` fires the night rule; `2026-11-01T14:00:00+11:00`, the same instant, does not. The plan does not say whose local time is meant (merchant or cardholder). If the upstream system always sends `Z`, an Australian transaction at 02:00 local time is never "night".
- **Timestamps have no bounds.** The plan does not specify any skew limit. Six attempts sent within 5 seconds, stamped 61 seconds apart, never trip velocity. This is deterministic, but it can be gamed.
- **The decision time is never recorded.** The plan gives up the auditor's first question, "when was this decision made?", only to keep head hashes byte-identical. Evidence that is never captured cannot be filled in later for any record already written.

**Failure scenario.** In a card-testing incident review, one card has 40 approvals within 2 minutes of wall-clock time, but upstream spread or normalised the timestamps. The auditor asks for decision times to match against upstream logs, and the chain has none. Every replay matches, so the evidence looks clean even though the velocity and night controls never fired.

**Alternative and cost.**
- Add a `received_at` field (server UTC) to the payload, recorded as evidence only. It must be excluded from rule inputs and from the replay comparison, so the AGENTS.md rule "never the server clock" still holds.
- Change T04 to compare decision fields, or to inject a fixed clock, instead of comparing head hashes. Cost: one field, an injected clock in the store, and a T04 change. It needs author approval because the PRD's record fields (L78) do not list it.
- Pin down the night semantics. The cheapest option is an input contract that says "offset = merchant local time". The alternative is a `night_timezone` in YAML; that likely needs the `tzdata` package on Windows, which would be a new dependency.
- Do **not** reject skewed timestamps based on the server clock. That would break determinism.

**Verdict: CHANGE.** Every record written without a decision time loses it permanently, and the meaning of "night" is currently left to whoever sends the request.

---

## 3. Failure defaults

**Decision.**
- Each scored rule runs in its own try/except. An error adds `RULE_ERROR` and sets a `step_up` floor, and the floor never downgrades a decline (§3, L233–242; R12, L429).
- If history is unavailable, the outcome is `step_up` with `HISTORY_UNAVAILABLE`, and the decision is recorded (L237, T18).
- If the write fails, the API returns 503 and no decision (R13, L430).
- Any invalid rule file stops startup (L244–246).

**Strongest case against.** The policy itself is right. The gaps are:
- **Hard rules are not covered.** Only scored rules are wrapped (L238). The plan does not say what happens when a hard rule raises; most likely the result is a 500 with no record.
- **Replay of fail-safe records will report mismatches.** The plan does not specify what `features` holds when history was unavailable. Replay recomputes features from earlier records (L206), which will succeed later, so `match: false` is guaranteed for every decision that correctly failed safe. No test replays a T18 record.
- **No decision means the caller decides.** A 503, or a refusal to start, produces no decision. "Never approve" then depends on what the caller does when it gets no answer, and neither document says what that is.
- **Errors are hard to diagnose.** The broad except turns bugs into quiet step-ups, and the log line (L270–271) carries no error class or rule code.
- **A bad rule file blocks auditors.** One malformed new file stops startup, which also takes replay and verify away from auditors.

**Failure scenario.** After a database-contention spike, an auditor samples the `HISTORY_UNAVAILABLE` decisions. Every replay reports a mismatch, so the reproducibility control (REP-01) is reported as failing. The team cannot tell tampering apart from a design artefact.

**Alternative and cost.**
- Store `features: null` for these records. Replay should run the engine on the *stored* failure state and report a separate `features_match` for the recompute.
- Wrap hard rules too: an error gives `RULE_ERROR` and a step-up floor.
- Log the rule code and error class, never input values.
- State in the README/spec that a missing decision (503 or timeout) must be treated as step-up by the caller.

Cost: about 20 lines and 3 tests.

**Verdict: CHANGE.** The floor is correct, but the shape of failure records is hashed and outcomes already issued cannot be recalled, so specify both before building.

---

## 4. Role model

**Decision.**
- `X-Role` is `analyst` or `auditor`. A missing or unknown role gives 401; a valid role on a disallowed endpoint gives 403. Only auditors can verify, and only analysts can create (§3, L249–268; R14; T12–T14).
- `/` and `/docs` need no role.
- The seed and demo scripts append "through the real engine and store" (L35), not through the API.

**Strongest case against.**
- **The header is self-asserted.** A3 holds only for honest clients; anyone can send `X-Role: analyst`.
- **The red-team rule cannot be satisfied.** The PRD red team explicitly tries "forge a header" and turns every successful attack into a failing test (PRD L146). This attack cannot be fixed within scope.
- **Records carry no actor or source.** Seeded decisions and API decisions cannot be told apart in the chain.
- **Default-deny is not specified.** The plan does not say whether role enforcement denies routes by default.

**Failure scenario.** The red team switches the UI to auditor, edits the header, and creates a decision. Under the PRD process this must become a failing test, but that test can only pass with out-of-scope authentication. Either `make verify` stays red, or someone weakens the test.

**Alternative and cost.**
- Keep the header, as the PRD requires (L31, L37).
- Record header forgery up front as a documented accepted limitation, so it is not raised as a red-team finding.
- Make role enforcement a per-route allowlist that denies by default, and have T14 enumerate `app.routes` so a route with no role fails the test. Cost: about 10 lines.
- Optionally add `source: api|seed` to the payload. This goes beyond the PRD, so ask the author first.

**Verdict: ACCEPT-RISK.** Real authentication is a stated non-goal, and the header check is code-only and easy to replace. The only permanent effect is that records carry no actor.

---

## 5. Concurrency

**Decision.**
- Each append is one transaction opened with `BEGIN IMMEDIATE`: read the head, compute features, run the engine, hash, insert, commit.
- `UNIQUE(prev_hash)` is a second guard. Busy timeout is 5 seconds, with one connection per request (§2.4, L162–176; L455–457; T26–T28).

**Strongest case against.** Nothing serious. `BEGIN IMMEDIATE` takes the write lock *before* the head is read, so two writers cannot both extend the same hash. Unlike an in-process mutex, this works across threads and across worker processes. Features are computed under the same lock, so history matches the head. Three things are left unspecified:
- **Transaction mode.** Python `sqlite3`'s default implicit transaction handling mixes poorly with an explicit `BEGIN`.
- **Error mapping.** Nothing says what an `IntegrityError` or an expired busy timeout returns.
- **Journal mode.** In rollback-journal mode, a long verify read blocks commits.

**Failure scenario.** An auditor runs verify on a large chain at peak load. In rollback-journal mode, writers queue behind the read until they pass 5 seconds and get 503s with no decision; the effect is then determined by item 3. This is unlikely at slice scale.

**Alternative and cost.** A `threading.Lock` is simpler, but it fails silently with more than one worker, so it is worse. Instead, add to the plan: `isolation_level=None` (or `autocommit`), WAL mode, and `IntegrityError` or busy timeout mapped to 503. Cost: about 3 lines.

**Verdict: KEEP.** The approach is correct and works across processes; only the transaction mode, journal mode and error mapping need to be written into the plan.

---

## What the PRD is missing

- **Decision time.** The record fields (L78) have no server receipt or decision time, so "when was it decided" cannot be answered.
- **Actor identity.** There is no submitter id or source on a record; with a role header alone, an auditor cannot attribute a decision.
- **Caller contract when there is no decision.** Nothing says what the caller must do on a 503 or timeout. Fail-safe holds only if the caller defaults to step-up.
- **Timezone for "night".** Neither "night" nor the timezone is defined (merchant, cardholder or UTC), and that changes outcomes.
- **Timestamp trust.** Nothing bounds or records how far a client timestamp may be from receipt time; velocity can be evaded by spacing timestamps.
- **Input schema and the source of card status.** There is no schema and no source for "stolen"; implementers have to invent the contract (plan R8, R9).
- **Rule definitions.** "Unusual amount", "foreign", "new merchant" and "attempt" are undefined, and their meaning decides outcomes (plan R3–R7).
- **Rule activation and retention.** Nothing says how a version becomes active, when, or that old rule content must be kept for replay; replaying years later depends on it.
- **Meaning of "unaltered".** It is not stated whether integrity covers bytes or parsed meaning; this decides whether verify must reject non-canonical payloads.
- **Response to a broken chain.** Nothing says what happens when verify finds a break: keep deciding, freeze, or alert. An incident responder needs a defined action.
- **Corrections and overrides.** There is no way to correct a wrong decision, or record a step-up result or an analyst override, in an append-only log. Without one, people end up editing records.
- **Retention and erasure.** Retention periods are not stated, and immutable records (including a linkable `card_ref`) conflict with privacy correction or erasure requests.
- **Backup and restore.** A restored backup truncates the chain and looks the same as a truncation attack; this needs a procedure.
- **Duplicates and idempotency.** Nothing says what happens to a repeated `transaction_id`; retries inflate velocity and create duplicate decisions (plan R26).
- **Read-access logging.** Nothing says whether reads, replays and verifies by either role are logged; auditors often need access evidence.
- **Operational logging.** The PRD forbids sensitive logs but does not say what must be logged for an incident (rule error detail, verify results).
- **Latency.** There is no latency target; a 5 s busy timeout may exceed what an authorisation path tolerates.
- **Key management for linking card history.** The PRD requires last-4 masking only and is silent on how card history is linked without the PAN, and on key custody.

## Where plan.md departs from the PRD

- **Hash definition (internal).** §2.2 hashes the stored `payload` bytes, but §2.5 re-canonicalises them (L100 vs L191). Pick bytes (item 1).
- **A2 wording.** For an edit followed by a rehash, verify returns `first_broken_id` = next record, while the reason names the edited one (L196–197, T08). The PRD says "name the first broken record" (L182). An auditor may investigate the wrong id.
- **PRD process items moved to Should.** These are core PRD process or evidence, but the plan downgrades them to Should (L382–391):
  - the generated controls document (PRD L115, L191)
  - `docs/limitations.md` (PRD L87)
  - the independent test author, reviewer and red team (PRD L131–146, L204)
  - `AI_USAGE.md` (PRD L164)
- **Property-based determinism tests.** The PRD lists them as a test layer (L191); the plan moves them to Could (L394).
- **`SPEC.md`.** The PRD names it as evidence (L161); the plan drops it (D5, R23). The author must approve this explicitly.
- **Additions beyond the PRD.** The plan adds `GET /decisions` (R15), the 401 status (R14), and the `card_ref`/`CARD_REF_KEY`, `features`, `rules_sha256` and `record_version` fields. Each is justified, but none is in the PRD's record fields, so each needs author sign-off at gate 2.
- **Rule version ordering.** The "highest version number" activation rule (L246) uses a string version (`version: "1"`, L220), and the ordering is not specified: as strings, `"10"` sorts below `"9"`. Make the version an integer.
- **`make tamper` and the triggers.** §2.4 assumes `make tamper` drops and restores the triggers (L179–181), but the triggers are only a Should. The tamper script must still work when they don't exist.

---

## Author decisions

Decided by the author (gate 2) on 2026-10-31 after reviewing the challenger's
recommendations. `docs/plan.md` has been updated to match. Two challenges were
overruled.

| # | Challenge | Decision | Change made in `docs/plan.md` |
| --- | --- | --- | --- |
| 1 | What is hashed | **CHANGE, accepted** | The hash covers the stored payload bytes. Verify also requires those bytes to be canonical and rejects duplicate keys with an `object_pairs_hook` (§2.2, §2.5; tests T39, T40). `rules_sha256` hashes the canonical validated rule set, not the file bytes, so a comment-only edit still matches (§2.3; T38). The limitations doc states that the demo `CARD_REF_KEY` is public and that a production key belongs in a vault (R20) |
| 1b | Store canonical rule content in a `rule_sets` table | **Overruled** | Not built. It goes beyond the PRD and costs about 30 lines. Rule files are retained in git, and replay reports a content change under the same version through `rules_match` (§2.5, R18) |
| 2 | Time handling | **CHANGE, accepted** | `received_at` (server UTC, injected clock) is added to the hashed payload as evidence only. Rules never read it and replay ignores it (§2.3; T42). T04 uses a fixed injected clock so head hashes still match. The input contract says the timestamp offset is the merchant's local offset (§2.1, R5) |
| 2b | Bound client timestamp skew | **Accept-risk** | Not enforced, because a bound would need the server clock and break determinism. Documented in the limitations (R30) |
| 3 | Failure defaults | **CHANGE, accepted** | Hard rules are wrapped too: an error gives `RULE_ERROR` and a step-up floor (§3; T41). History failure records `features: null`. Replay decides from the stored features (`match`) and recomputes them separately (`features_match`), so failed-safe records replay as a match (§2.5; T45). Rule errors log the rule code and exception class only (T44). The README states that callers must treat no decision as step-up. A bad rule file still stops startup |
| 4 | Role model | **Accept-risk** | `X-Role` header kept. Header forgery is a documented accepted limitation, not a red-team finding. Role enforcement is a per-route allowlist that denies by default, and T14 enumerates `app.routes` (§3) |
| 4b | Add `source: api\|seed` to the payload | **Overruled** | Not built. It goes beyond the PRD, and seed data is reset by `make demo` |
| 5 | Concurrency | **KEEP** | Added autocommit mode (`isolation_level=None`), WAL mode, and mapping of `IntegrityError` and busy timeouts to 503 (§2.4; T43) |

Departures from the PRD raised by the challenger:

| Point | Decision |
| --- | --- |
| Hash definition contradiction (§2.2 vs §2.5) | Resolved under item 1: hash the stored bytes |
| A2 wording for edit plus rehash | Keep reporting the later record as `first_broken_id`. Verify cannot tell which of the two records was altered, so the reason names both (§2.5; T08) |
| PRD process items listed as should | Moved to a new "Required after the build" tier: independent tests, reviewer and red team, controls doc, limitations doc and `AI_USAGE.md` (§6) |
| Property-based tests moved to could | Unchanged. Hypothesis is a new dependency and has not been approved |
| `SPEC.md` dropped | Still open as D5 |
| Additions beyond the PRD (`GET /decisions`, 401, `card_ref`, `features`, `rules_sha256`, `record_version`) | Accepted with the plan, together with `received_at` |
| String rule version ordering | Changed: `version` is an integer (§3; T19) |
| `make tamper` depends on the optional triggers | Changed: the tamper script works whether or not the triggers exist (§2.4) |

Still open before the build: D1–D5 in `docs/plan.md` section 7.
