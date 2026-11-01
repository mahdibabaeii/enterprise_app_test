# Review 04: red team

**Scope:** A separate read-only agent attacked the running service after
step 11, at commit `bf9f7a7`. It started its own uvicorn server with the same
command as `make start`, on a spare port with its own database, and used
scratch scripts against `create_app`. It never touched `data/demo.db` or the
user's server, and changed no repository file. It was told to skip the
accepted limitations in `docs/limitations.md` (self-asserted role header,
head-of-chain edits, trusted client timestamps, public card reference key,
duplicate transaction ids).

**Verdict:** Verify caught every way of altering a stored record that was
tried, role enforcement held on every route and method, and decisions stayed
deterministic. Eight attacks succeeded: two Medium, four Low, two Info. R1 can
produce an approve where the PRD expects step-up.

## Summary of findings

| Id | Title | Severity | Violates |
| --- | --- | --- | --- |
| R1 | A leading zero on the card number gives the same card a new history, so velocity never fires | Medium | PRD velocity rule, plan R7 |
| R2 | Duplicate JSON keys in the request body are accepted and the last one wins, which can override `reported_stolen` | Medium | PRD "bad input is rejected, not guessed at" |
| R3 | uvicorn's access log records a card number sent in the URL path or query string | Low | A8, AGENTS.md "Never log full card numbers" |
| R4 | A card number placed in `transaction_id` or `merchant_id` is stored and returned in clear | Low | DAT-01, README "never in logs, responses or the database" |
| R5 | One unreadable stored row makes the list, get and replay routes return 500 | Low | AGENTS.md "Errors map to specific HTTP status codes" |
| R6 | A trigger added to the table can silently drop every decline, and verify still reports intact | Low | AUD-01, plan 2.5 |
| R7 | `merchant_country` accepts codes that are not ISO 3166-1, such as `ZZ` | Info | Plan 2.1 |
| R8 | `make tamper` with a very large record number prints a raw traceback | Info | Review 03 F5, AGENTS.md "clear message" |

---

## R1. A leading zero on the card number resets velocity (Medium)

**Steps.** On a fresh database, send six analyst `POST /decisions` requests
for card `4111111111111111`, merchant `M-SHOP`, country `AU`, 500 cents, all at
`2026-11-01T14:00:00+11:00`. The sixth has `attempts_in_window: 6`, scores 40
for `HIGH_VELOCITY` and is a step-up. Send the sixth as `04111111111111111`
instead: it has `attempts_in_window: 1`, scores 0 and is approved, while being
masked as `****1111` like the others. Cycling `0`, `00`, `000` prefixes gives
12 attempts in one second without any seeing more than 3.

**Why.** A leading zero does not change the Luhn sum, the pattern
`^[0-9]{12,19}$` accepts it, and `card_ref` is an HMAC of the raw string, so
each form of the same card has its own history.

**Fix.** Reject card numbers that start with 0. Issued card numbers never do.

**Test.** A zero-prefixed card number gives 422, and five plain attempts
followed by a zero-prefixed sixth never yield an approve.

## R2. Duplicate JSON keys override earlier values (Medium)

**Steps.** POST a raw body with `"card_status":"reported_stolen"` early and
`"card_status":"active"` later. Result: 201, stored `active`, approve. The same
works for a Luhn-invalid `card_number` followed by a valid one, and for a
repeated `amount_minor`.

**Why.** The request body is parsed with `json.loads`, which keeps the last
duplicate. A system upstream that reads the first key would see a stolen card
that this service approved. The store already rejects duplicate keys in stored
payloads for the same reason (plan 2.5).

**Fix.** Reject a request body with a duplicate key, with the same sanitised
422 shape as other validation errors.

**Test.** Duplicate `card_status`, `card_number` and `amount_minor` each give
422, echo no value, and add no record.

## R3. The access log records card numbers sent in the URL (Low)

**Steps.** Run the server with the same uvicorn command as `make start` and
request `POST /decisions?card_number=5555555555554444` and
`GET /decisions/4000056655665556`. The server log contains both numbers in the
access lines.

**Why.** uvicorn's access log is on by default and writes the raw path and
query string. TestClient never emits it, so the existing log test cannot see it.

**Fix.** Mask runs of 12 or more digits in `uvicorn.access` log lines.

**Test.** A filtered access-log record for those two URLs contains no run of
12 or more digits.

## R4. A card number in an identifier field is stored in clear (Low)

**Steps.** POST with `transaction_id: "4111111111111111"` and the same card
number, or with that number as `merchant_id`. Both give 201 and the number
appears in responses, `GET /decisions` and the database.

**Fix.** Reject a request whose `transaction_id` or `merchant_id` contains its
own `card_number`. A general ban on long digit runs would reject real merchant
ids, which are often long numbers.

**Test.** Both cases give 422, echo no value, and add no record.

## R5. An unreadable stored row gives 500 (Low)

**Steps.** In a seeded database, set record 5's payload to `x`, change a
feature to a string, or remove `rule_version`. `GET /decisions`,
`GET /decisions/5` and its replay all return 500, while `/audit/verify`
correctly names record 5. Because the list covers the latest 20 records, one
bad row empties the UI's recent table.

**Fix.** Raise a specific error when a stored record cannot be read, and map it
to 409 with the record id and a pointer to verify.

**Test.** With record 2 unreadable, list, get and replay give 409 naming record
2, and verify still names record 2.

## R6. An added trigger changes behaviour without breaking verify (Low)

**Steps.** In a seeded database, add a `BEFORE INSERT` trigger that silently
ignores any insert whose outcome is `decline`. Every decline now gets 503 with
no record, so under the caller contract it becomes a step-up, and declines
never enter velocity history. `/audit/verify` still reports intact.

**Why it is not an accepted limit.** No record is changed, so this is not one
of the documented head-of-chain limits. Verify checks records but never the
table definition.

**Fix.** Verify also checks that the `decisions` table has no triggers and has
the expected definition, and reports the chain as not intact otherwise.

**Test.** With a trigger on `decisions`, verify reports `intact: false` and
names the trigger.

## R7. Non-ISO country codes are accepted (Info)

`"merchant_country":"ZZ"` gives 201 and scores `FOREIGN_COUNTRY` (+30). The
plan says ISO 3166-1 alpha-2. The score can only go up, so this cannot produce
an approve. A full country list needs either a hand-maintained table or a new
dependency. **Not fixed**; recorded as a known gap.

## R8. `make tamper` with a huge record number gives a traceback (Info)

`tamper.main(["99999999999999999999"])` raises an uncaught `OverflowError`
("Python int too large to convert to SQLite INTEGER").

**Fix.** Treat a record number outside SQLite's integer range like any other
missing record.

**Test.** That argument exits 1 with a clear message.

## Attacks that held

- **Malformed input.** Timestamps at `+14:00`, `-12:00`, `+05:45`, `Z`, with a
  space separator or no seconds are accepted and decide the same as equivalent
  forms. Offsets with seconds, `+24:00`, leap seconds, seven fractional digits,
  trailing newlines, full-width digits, pre-1970 dates and Feb 30 give 422.
  Negative, zero, huge, fractional, boolean, text and NaN amounts give 422.
  Newlines, NUL, non-ASCII, 65-character and 1 MB identifiers give 422. Non-JSON,
  form, empty, null and array bodies give 422; deeply nested or invalid UTF-8
  bodies give 400.
- **Card data.** No 422 echoes a value, including a card number used as an
  unexpected key. The OpenAPI schema holds no card numbers. The app log writes
  only the masked card. The database and WAL bytes hold no submitted card
  number.
- **Roles and routing.** Every method on every route, with no role or as
  auditor, gave no unexpected success and wrote nothing. HEAD, OPTIONS and
  TRACE give 405. Path tricks (double slashes, case, encoded slashes, dot
  segments) give 404 or reach role-checked routes. Oddly cased, tabbed or
  comma-separated roles give 401. Method-override headers are ignored. There
  are no CORS headers. The UI writes values with `textContent`.
- **Determinism and fail safe.** Equivalent timestamp forms decide the same.
  40 parallel POSTs to a real server formed one linked chain. A trigger that
  drops inserts gives 503, never approve. An unreadable history row gives
  `HISTORY_UNAVAILABLE` and step-up.
- **Tamper evidence.** Non-JSON payloads, wrong types, removed fields, a
  consistent edit of input and outcome in the middle of the chain, an uppercase
  hash and an inserted row with id -1 are all caught at the right record.
- **Demo commands.** `make tamper` refuses other databases, refuses a second
  flip of the same record, and rejects -1, 0 and `abc` clearly. Seeding while
  a live server takes requests lost no acknowledged decision made after the
  reseed.

## Fixes

Each fix is its own commit, with a failing test first:

| Finding | Status |
| --- | --- |
| R1 | Fixed |
| R2 | Fixed |
| R3 | Fixed |
| R4 | Fixed |
| R5 | Fixed |
| R6 | Fixed |
| R7 | Not fixed: score can only rise; a country list is left for later |
| R8 | Fixed |
