# Tamper-Evident Card Decision Service

A thin vertical slice of a card transaction decision service for a regulated
bank. Every automated decision is explainable (score and reason codes),
replayable (same input and rule version, same output) and provably unaltered
(an append-only, hash-chained log that an auditor can verify).

![Card Decision Service one-page UI: auditor view with the chain verified intact, a step-up decision scored 65 with its reasons, and the recent decisions table](docs/screenshots/ui.png)

All data is synthetic. Card numbers are test numbers, and only the last four
digits are ever stored or shown.

## Start

Requirements: [uv](https://docs.astral.sh/uv/), `make`, and Python 3.12
(uv installs it if missing). Port 8000 must be free.

```bash
git clone https://github.com/mahdibabaeii/enterprise_app_test.git
cd enterprise_app_test
make start
```

`make start` installs the pinned dependencies, reseeds the demo database with
14 sample decisions and serves:

- the one-page UI at http://localhost:8000/
- the API docs (Swagger) at http://localhost:8000/docs

| Command | What it does |
| --- | --- |
| `make start` | Install, reseed `data/demo.db`, serve the UI and API on port 8000 |
| `make verify` | Lint, format check, tests, tamper simulation, determinism replay. Exits 0 only if all pass |
| `make demo` | Replace `data/demo.db` with fresh sample decisions |
| `make tamper` | Corrupt one record in `data/demo.db` only (record 3 by default, `RECORD=n` to choose) |

## Five-minute walkthrough

**Analyst session**

1. Open http://localhost:8000/. The role switcher starts on **Analyst**.
2. Click **Decide**. The form holds the PRD example: 2,000 AUD at a new
   merchant in Great Britain at 03:00 local time. The decision is **Step-up,
   65**: foreign country +30, unusual amount +25, new merchant at night +10.
3. Change the inputs and decide again. Each decision becomes history for the
   card, so the merchant is no longer new. Change the country to `AU`: 25,
   approve (unusual amount only). Change the amount to `5000` as well: 0,
   approve. Set the card status to `reported_stolen`: decline, whatever the
   score. Click **Decide** six times without changing the timestamp: the
   sixth attempt in a minute adds 40 for velocity and declines.
4. Send bad input, such as a negative amount or a timestamp without an offset.
   The API rejects it with 422 and names the fields, without echoing the values.

**Auditor session**

1. Switch to **Auditor** and click **Decide**. The API refuses with 403: an
   auditor can never create a decision.
2. Click **Verify chain**. The bar turns green: every record matches its hash.
3. In a terminal, run `make tamper`. It flips record 3 from decline to approve
   directly in SQLite, as someone with database access could.
4. Click **Verify chain** again. The bar turns red and names record 3.
5. Run `make demo` to restore a clean chain.

Replay is in the API: `GET /decisions/{id}/replay` re-runs a stored decision on
its stored rule version and reports whether it matches. Try it from `/docs`
with the `X-Role` header set to `analyst` or `auditor`.

## How it works

```text
┌─────────────────┐  fetch, X-Role  ┌──────────────┐     ┌──────────────┐
│ UI (index.html) │────────────────▶│ API          │────▶│ Store        │
│ no logic        │                 │ app/main.py  │     │ app/store.py │
└─────────────────┘                 │ roles, errors│     │ SQLite chain │
                                    └──────┬───────┘     └──────┬───────┘
                                           │ loads              │ history
                                           ▼                    ▼
                                    ┌──────────────┐     ┌──────────────┐
                                    │ Rules        │────▶│ Engine       │
                                    │ rules/*.yaml │     │ pure decide()│
                                    └──────────────┘     └──────────────┘
```

- **Rules** live in `rules/v1.yaml`: two hard rules (stolen card, blocklisted
  merchant), four scored rules and the thresholds (under 30 approve, 30 to 69
  step-up, 70 or more decline). No threshold or point value is in code.
- **Engine** (`app/engine.py`) is a pure function of the transaction, its
  history and the rule set. It reads the transaction's own timestamp, never the
  server clock. If a rule errors or history is unavailable, the outcome is at
  least step-up, never approve.
- **Store** (`app/store.py`) appends each decision as canonical JSON (sorted
  keys) whose SHA-256 covers the previous record's hash. Appends are serialised
  with SQLite `BEGIN IMMEDIATE`, and a unique constraint stops two records
  extending the same hash. There is no update or delete path.
- **Verify** recomputes every hash from the first record and names the first
  record that no longer matches.

### API

| Endpoint | Roles | Purpose |
| --- | --- | --- |
| `POST /decisions` | analyst | Decide a transaction and record it |
| `GET /decisions` | analyst, auditor | The latest 20 decisions, newest first |
| `GET /decisions/{id}` | analyst, auditor | One stored decision |
| `GET /decisions/{id}/replay` | analyst, auditor | Re-run a decision on its stored rule version |
| `GET /audit/verify` | auditor | Walk the hash chain |

Every request carries an `X-Role` header. A missing or unknown role gets 401, a
role not allowed on the route gets 403, and a route without an explicit role
list refuses everyone. Other errors map to 404 (unknown id), 409 (rule version
not loaded for replay), 422 (invalid input) and 503 (the decision could not be
recorded).

### Caller contract

A caller that receives no decision, whether a 503, any other 5xx or a timeout,
must treat the transaction as **step-up**. The service never returns a decision
it could not record.

## Controls

Each control is backed by tests tagged with its id, for example
`@control("AUD-01")`.

| Id | Control | What the system does |
| --- | --- | --- |
| EXP-01 | Explainability | Every decision returns a score and reason codes with points |
| REP-01 | Reproducibility | Replay re-runs stored input on the stored rule version; `make verify` replays 200 seeded transactions |
| AUD-01 | Tamper evidence | Hash-chained log; verify names the first altered record; `make verify` runs a tamper simulation |
| AUD-02 | Append-only, serialised | No update or delete path; concurrent appends cannot extend the same hash |
| ACC-01 | Least privilege | Analyst and auditor roles enforced on every route, deny by default |
| FS-01 | Fail safe | Rule errors and missing history give step-up, never approve |
| DAT-01 | Data minimisation | Card numbers masked to the last 4 digits; never in logs, responses or the database |
| VER-01 | Versioned change | Rules are versioned files; each decision records its rule version and a hash of the rule content |

## Deliberate non-goals

These are out of scope for this slice by design:

- Cloud deployment, infrastructure as code or a managed database.
- Real authentication. The `X-Role` header stands in for an identity provider.
- Streaming ingestion, card-network integration or machine learning.
- A rule-change approval workflow. It is described below, not built.

## Known limitations

- **The role header is not authentication.** Anyone can send
  `X-Role: auditor`. Header forgery is an accepted limitation, not a finding.
- **The chain proves tampering only against someone who cannot rewrite all of
  it.** It does not detect deleting the latest records, editing the latest
  record (or a trailing run of records) with recomputed hashes, a forged record
  appended at the head, or a consistent rewrite of the whole chain.
- **An edit plus a recomputed hash is reported one record later.** Verify names
  the next record and says that either it or the one before was altered.
- **The card reference key is public.** Card history is linked by an HMAC of
  the card number. The demo key is in the code because all data is synthetic.
  Rotating the key breaks velocity history.
- **Client timestamps are trusted.** Spacing timestamps apart can evade the
  velocity rule. The server's receipt time is stored as evidence but never used
  in a decision, so decisions stay deterministic.
- **Velocity uses the local database**, so it does not scale across instances.
- **Duplicate transaction ids are not rejected.** A retry counts as another
  attempt.
- **Point values and thresholds are illustrative**, taken from the PRD, not
  calibrated on real data.
- **Rule content is kept in git, not the database.** Replay reports when the
  loaded rule content differs from the content a decision was made with.

## Production path (described, not built)

- An identity provider with real roles instead of a header.
- A managed database, with the latest chain hash anchored in write-once storage
  the writer cannot change.
- A message stream in front, and an in-memory store for velocity counters.
- Rule changes through maker-checker approval, with their own audit entries.
- A card reference key held in a vault.
- Monitoring on decision outcomes, with a circuit breaker to manual review if
  approval rates drift.

## Repository guide

| Path | Contents |
| --- | --- |
| [`docs/PRD.md`](docs/PRD.md) | Product requirements and acceptance criteria A1 to A9 |
| [`docs/plan.md`](docs/plan.md) | Approved plan: record format, what is hashed, tests mapped to A1 to A9 (section 4) |
| [`docs/challenges/`](docs/challenges/) | Independent challenge of the plan and the decisions taken |
| [`docs/reviews/`](docs/reviews/) | Reviews of the store and the API, and the fixes they led to |
| [`AGENTS.md`](AGENTS.md) | Conventions the coding agents follow |
| `app/` | API, engine, rules loader, store and the one-page UI |
| `rules/` | Versioned rule sets |
| `scripts/` | Demo seed, tamper command, tamper simulation and determinism replay |
| `tests/` | Tests tagged with control ids |

There is no separate `SPEC.md`: the PRD and the test table in the plan are the
spec.

## How this was built

Built with Amp coding agents in a 3-hour exercise, with three human gates:
the plan was proposed by an agent, challenged by a separate read-only agent and
edited by the author before any code was written; each build step ended only
when `make verify` exited 0; and independent reviews of the store and the API
led to the fix commits in the history. Agent-made commits carry a
`Co-authored-by: Amp` trailer.

The UI's look is modelled on
[cenksari/react-banking-app-template](https://github.com/cenksari/react-banking-app-template)
(MIT), reimplemented in plain CSS. No code was copied.
