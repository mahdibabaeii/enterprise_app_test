# Limitations and production path

This slice proves the controls on one machine with synthetic data. This page
lists what it does not do, why each gap was accepted, and what production would
do instead. The README carries a short summary.

Sources: `docs/PRD.md` (limitations and production path), `docs/plan.md`
(risks R3, R16, R20, R26, R30), `docs/challenges/01-plan-challenge.md` and the
reviews in `docs/reviews/`.

## Integrity

### The chain detects tampering only by someone who cannot rewrite all of it

Verify recomputes every hash from the first record, so changing a record that
has later records after it is caught. Without an external anchor for the latest
hash, these all look like an intact chain:

- deleting the latest record or any run of latest records (truncation)
- editing the latest record, or a trailing run of records, and recomputing
  their hashes and links
- appending a forged record at the head
- rewriting the whole chain consistently

A strict expected-failure test (`test_edit_of_latest_record_with_recomputed_hash_is_detected`
in `tests/test_store.py`) keeps the latest-record case visible: it fails the
suite if this ever starts passing without the limitation being revisited.

Sources: plan R16, review 01 F1.

**Production:** publish the latest hash at intervals to storage the writer
cannot change, such as write-once object storage or a separate audit system.
Verify then also checks the chain still reaches the last published hash.

### An edit plus a recomputed hash is reported one record later

If a record is edited and its own hash recomputed, its successor's link no
longer matches. Verify cannot tell which of the two was changed, so it names
the later record and says that either it or the one before was altered.

Source: plan §2.5, challenge 01 (departures from the PRD).

### The receipt time comes from the server clock

Each record stores `received_at`, the server's UTC time when the append ran, as
evidence for an auditor. It proves nothing against someone who controls that
clock. Rules never read it, so decisions stay deterministic.

Source: challenge 01 item 2, plan R16.

### Rule content is kept in git, not in the database

Each decision stores its rule version and a hash of the rule set's validated
content. Replay loads the rule file for that version from the repository and
reports `rules_match: false` if the content differs. If a rule file is lost,
replay of its decisions returns 409. The decisions themselves remain verifiable.

Source: challenge 01 item 1b (overruled).

**Production:** store each rule set's canonical content alongside the chain
when it is first used, under maker-checker approval.

## Access

### The role header is not authentication

The caller chooses its role with the `X-Role` header, so anyone can claim to
be an auditor or an analyst. Roles are still enforced on every route, deny by
default, which keeps the authorisation logic ready for a real identity. Header
forgery is an accepted limitation, not a red-team finding. With duplicate
`X-Role` headers the first one wins.

Records do not carry an actor or source, so a decision cannot be attributed to
a person.

Sources: PRD non-goals, challenge 01 item 4 (accept-risk) and 4b (overruled),
review 02.

**Production:** an identity provider issues signed tokens with role claims, and
each record carries the authenticated actor.

### Reads are not logged

Who read, replayed or verified which decision is not recorded.

## Data

### The card reference key is public

Card history (velocity and new-merchant checks) is linked by an HMAC-SHA256 of
the card number, so the number itself is never stored. The demo key is a fixed
string in `app/main.py`, which is acceptable only because every card number is
a synthetic test number. With a known key, a card number could be recovered
from its reference by searching the middle digits. Rotating the key breaks the
link to earlier history.

Sources: plan R20, challenge 01 item 1.

**Production:** a key held in a vault or HSM, or a tokenisation service, with a
documented rotation procedure.

### Records cannot be corrected or erased

The log is append-only by design. There is no way to record a correction, an
analyst override or the result of a step-up, and no path for privacy erasure
requests. Retention periods are not defined.

Source: challenge 01 (what the PRD is missing).

**Production:** corrections and step-up results as new linked records; a
retention and erasure policy agreed with privacy and legal.

## Decisions

### Client timestamps are trusted

Rules use the transaction's own timestamp so that replay is deterministic. A
caller can space timestamps apart to stay under the velocity window, or stamp
an offset that moves a purchase out of the night window. Rejecting skewed
timestamps would need the server clock and break determinism, so the risk is
accepted. `received_at` lets an auditor compare the two times.

Sources: plan R30, challenge 01 item 2b (accept-risk).

**Production:** the upstream card system supplies a trusted timestamp, and
large gaps between it and `received_at` raise an alert.

### "Unusual amount" is a fixed threshold

An amount of 1,000.00 AUD or more scores, for every card. There is no per-card
baseline.

Sources: plan R3, D4.

### Point values and thresholds are illustrative

The values in `rules/v1.yaml` are the PRD's starting values. They are not
calibrated on real data.

Source: PRD limitations, plan D2.

### Duplicate transaction ids are not rejected

A retried request is decided and recorded again, and it counts as another
attempt for the velocity rule.

Source: plan R26.

**Production:** an idempotency key returns the original decision for a retry.

### A missing decision relies on the caller

If the decision cannot be recorded, the API returns 503 and no decision. Fail
safe then depends on the caller treating a 503, any other 5xx or a timeout as
step-up, as the README's caller contract says.

Source: challenge 01 item 3, plan R13.

## Operations

### Velocity uses the local database

History is read from the same SQLite file, so the service runs as a single
instance. Concurrent appends are serialised by SQLite's write lock.

Source: PRD limitations.

**Production:** a managed database, and an in-memory store for velocity
counters behind a message stream.

### No response to a broken chain

When verify finds a break, the service keeps deciding. There is no alert,
freeze or incident procedure, and a restored backup looks the same as a
truncation.

Source: challenge 01 (what the PRD is missing).

### No latency target

The SQLite busy timeout is 5 seconds, which may be more than an authorisation
path tolerates.

## Out of scope by design

These are PRD non-goals, not gaps:

- cloud deployment, infrastructure as code and a managed database
- real authentication
- streaming ingestion, card-network integration and machine learning
- a rule-change approval workflow, and a rule administrator role (described,
  not built)

## Production path

Described, not built:

- An identity provider with real roles instead of a header.
- A managed database, with the latest chain hash anchored in write-once storage
  the writer cannot change.
- A message stream in front, and an in-memory store for velocity counters.
- Rule changes through maker-checker approval, with their own audit entries.
- A card reference key held in a vault.
- Monitoring on decision outcomes, with a circuit breaker to manual review if
  approval rates drift.
