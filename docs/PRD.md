# PRD: Tamper-Evident Card Decision Service

Nov 1, 2026 · @Mahdi

## Overview

This service makes every automated card decision explainable, replayable and provably unaltered, built as a thin vertical slice a regulated bank could credibly adopt.

**Problem.** A bank that decides card transactions automatically must explain each outcome to customers and auditors, reproduce it later, and show the record was not changed after the fact. Most prototypes show the decision and skip the evidence.

**What it does.** A transaction goes in. A versioned rule set returns approve, step-up or decline with reason codes. The decision is written to an append-only, hash-chained log. Anyone with the auditor role can replay any decision or verify the whole chain.

**Context.** This is a 3-hour exercise submission. It is built in a public repository with incremental commits, using AI coding agents (Amp) as the primary implementation tool. The aim is to show how agents are used to frame, design, build, verify and document the work, not to build an AI product.

## Goals and non-goals

The slice succeeds if a reviewer can run one command, make a decision, replay it, break a record, and watch verification catch it.

**Goals**

- Deterministic decisions: same input and same rule version always give the same outcome.
- Every decision carries a score and reason codes.
- Every decision is stored in a tamper-evident log, and any alteration is detected and located.
- Role separation: analysts decide, auditors only read and verify.
- One-line local start, with demo data and a one-page UI.
- Visible evidence of agentic delivery: spec, checks, independent review, commit history.

**Non-goals** (stated in the README as deliberate)

- Cloud deployment, infrastructure as code, or a managed database.
- Real authentication. A role header stands in for an identity provider.
- Streaming ingestion, card-network integration, or machine learning.
- A rule-change approval workflow. It is described, not built.

## Users and roles

Two roles cover the slice, selected with an `X-Role` header in this version.

| Role | Can do | Cannot do |
| --- | --- | --- |
| Analyst | Submit transactions, read decisions, replay a decision | Change or delete stored records |
| Auditor | Read decisions, replay, verify the chain | Create decisions (returns 403) |

A third role, rule administrator, is described but not built: it would propose rule changes that a second person must approve.

**Reviewer of the exercise** is a third audience. They clone the repo, run the start command, and expect the controls to be visible without reading code.

## Functional requirements

The service exposes four operations and decides with one hard rule plus three scored rules.

| Endpoint | Role | Behaviour |
| --- | --- | --- |
| `POST /decisions` | Analyst | Validate input, run rules, store the record, return outcome, score and reasons |
| `GET /decisions/{id}` | Both | Return a stored decision |
| `GET /decisions/{id}/replay` | Both | Re-run the stored input against the stored rule version and report match or mismatch |
| `GET /audit/verify` | Auditor | Walk the hash chain and report intact, or the first broken record |

**Decision logic.** Rules live in a versioned YAML file, not in code.

1. Hard rules stop everything: a card reported stolen or a blocklisted merchant gives decline immediately.
2. Scored rules add points and a reason code each, for example foreign country (+30), unusual amount (+25), more than 5 attempts in 1 minute (+40), new merchant at night (+10).
3. Thresholds map the total: under 30 approve, 30 to 69 step-up, 70 or more decline.

The point values above are starting values to confirm in the spec. Example: a 2,000 AUD purchase from overseas at 3am scores 30 + 25 + 10 = 65, giving step-up.

**Rules for behaviour**

- Fail safe: if a rule errors or history is unavailable, the outcome is step-up, never approve.
- Deterministic: rules use the transaction's own timestamp, never the server clock.
- Strict input validation: bad input is rejected, not guessed at.
- Velocity checks read recent history from the local database.

## Audit and integrity

Each decision is stored so that changing any past record breaks every record after it, and verification points to the first break.

**Decision record fields:** inputs (with the card number masked), score, reason codes, outcome, rule version, transaction timestamp, the hash of the previous record, and its own hash.

**How the chain works**

- A record's hash covers its canonical JSON (sorted keys) plus the previous record's hash.
- Records are only ever appended. No update or delete path exists in the code.
- `GET /audit/verify` recomputes every hash from the start and returns intact, or the id of the first record that no longer matches.
- `GET /decisions/{id}/replay` re-runs the stored input with the stored rule version and compares outcomes.

**Known limit, stated openly.** Someone with write access to the whole database could rewrite the entire chain consistently. Production would anchor the latest hash somewhere the writer cannot change, such as write-once storage. This goes in the limitations document.

**Concurrency.** Appending must be serialised so two requests cannot extend the same previous hash. A test covers this.

## User experience

One page, served by the same process as the API, lets a reviewer see the controls working in under five minutes.

**Start.** `git clone <repo> && cd <repo> && make start` installs dependencies, seeds demo data and serves the UI and API docs at `http://localhost:8000`.

**The page contains**

1. A role switcher (analyst or auditor) that sets the `X-Role` header.
2. A transaction form with a Load sample button.
3. A decision panel: outcome badge, score against the thresholds, reason codes with points.
4. A recent decisions table with a Replay button per row.
5. An integrity bar with a Verify button: chain intact in green, or broken at a named record in red.

The UI holds no business logic and only calls the API. Swagger stays available at `/docs`.

**Look.** The page follows the visual style of [cenksari/react-banking-app-template](https://github.com/cenksari/react-banking-app-template) (MIT): its palette, card-style panels, typography and badges. The style is reimplemented in plain CSS inside the single static HTML file. No React, no copied code and no build step. Once the page is built, a screenshot of it is added to the repo-level README.

**Analyst session:** open as analyst, submit a transaction, read the decision, replay it, try edge cases near scores of 30 and 70.

**Auditor session:** switch to auditor, attempt to create a decision and see 403, verify the chain, run `make tamper` to alter one stored record, verify again and see the first broken record named.

Other commands: `make verify` runs all automated checks, `make demo` reloads sample data, `make tamper` corrupts one record in the demo database only.

## Controls and data handling

Each control below is backed by a test tagged with its control id, and the controls document is generated from those tags.

| Control | What the system does | Expectation it supports |
| --- | --- | --- |
| Explainability | Every decision returns reason codes and a score | Model risk management, fair treatment of customers |
| Reproducibility | Replay re-runs stored input on the stored rule version | Model risk, audit evidence |
| Tamper evidence | Hash-chained, append-only log with verify | Record integrity, internal audit |
| Least privilege | Analyst and auditor roles enforced on every endpoint | Access control, separation of duties |
| Fail safe | Errors and missing data give step-up, never approve | Operational resilience |
| Data minimisation | Synthetic data only, card numbers masked to the last 4 digits, no sensitive values in logs | Privacy, payment data handling |
| Versioned change | Rules are versioned files, and each decision records its version | Change management |

The mapping names the type of expectation, such as model risk, privacy or operational resilience. The exact regulatory references (for example APRA CPS 230 or the Privacy Act) are to be confirmed before they appear in the submitted controls document.

## Agentic delivery

The author owns the spec, the constraints and the definition of done; agents execute; automated checks decide whether work is finished. It does not run from one prompt: three human gates sit between phases of autonomous agent work.

&#91;embedded content: agentic delivery flow · 7 stages, 3 human gates, 2 loops\]

The author edits the plan at gate 2 and signs off at gate 3; findings from the independent checks loop back into the build until verification passes.

**Agents and what each is for**

| Agent | Job | Constraint |
| --- | --- | --- |
| Planner | Propose file layout, data model, tests and commit sequence from the spec | Plan only, no code |
| Challenger | Question the plan's hardest-to-reverse decisions: what is hashed, role model, failure defaults | Read-only, fresh thread, sees artefacts only, at most 5 challenges |
| Implementer | Build against the approved plan, loop on `make verify` | Done only when `make verify` exits 0; stops and reports after 5 failed loops |
| Test author | Write independent tests from the spec alone | Never sees the implementation |
| Reviewer | Check the diff against the spec and controls | Read-only, findings only |
| Red team | Try to break the running system, for example edit a row, forge a header, send malformed input | Every successful attack becomes a failing test |

**Skills (final set of four)**

| Skill | Purpose |
| --- | --- |
| `verify-and-report` | Run `make verify`, summarise failures, update a status file |
| `add-rule` | Edit the YAML, bump the version, add the reason code, add tests including threshold edges |
| `red-team` | A checklist of attacks and how to turn each into a test |
| `challenge-decision` | Structured challenge output: decision, strongest counter-argument, failure scenario, alternative, verdict (keep, change, accept-risk) |

Persona skills such as backend developer or UI designer are deliberately not included. Conventions for layering, typing and error handling go in `AGENTS.md` as concrete rules.

**Evidence left in the repository**

- `SPEC.md` with executable acceptance criteria, and `AGENTS.md` with conventions.
- A `Co-authored-by: Amp` trailer on each agent-made commit, without a thread link.
- `docs/challenges/` holding each challenge and the decision taken, including at least one overruled.
- `AI_USAGE.md` with a table of task, how it was delegated, how it was verified, defects caught, and human interventions.

## Verification and acceptance

One command, `make verify`, defines done for the agents, for CI and for the final check, and it must exit 0.

**What `make verify` runs**

- Lint and format check.
- Unit and integration tests.
- A tamper simulation against a throwaway database.
- A determinism replay over a batch of synthetic transactions.

**Acceptance criteria**

| Id | Criterion |
| --- | --- |
| A1 | The same input and rule version always give the same decision |
| A2 | Editing any stored record makes `verify` fail and name the first broken record |
| A3 | The auditor role can never create a decision |
| A4 | A rule error or unavailable history never produces approve |
| A5 | Scores of 29, 30, 69 and 70 land in the correct outcome |
| A6 | A hard rule overrides a low score |
| A7 | Concurrent writes cannot extend the same previous hash |
| A8 | Logs contain no full card numbers |
| A9 | A fresh clone starts with `make start` and answers on port 8000 |

**Test layers.** Tests written with the code, independent tests written from the spec by a separate agent, property-based tests for determinism, a reviewer pass, and a red-team pass. Each control carries a tag, and the controls document is generated from the tags.

**CI.** One GitHub Actions workflow with read-only permissions runs `make verify` on every push. A dependency scan is optional if time allows. There is no deploy job. Commit the workflow early so most of the history shows green builds.

## Timeline and cut order

The 3-hour timebox is split into six blocks, each ending in at least one commit; the author expects the build itself to take roughly 1.5 to 2.5 hours with agents, leaving slack for verification.

| Block | Work | Commit evidence |
| --- | --- | --- |
| 0:00 to 0:30 | Write `SPEC.md`, `AGENTS.md`, the `make verify` skeleton and the control tag convention; README stub | Spec, conventions, README |
| 0:30 to 0:45 | Planner proposes the plan; challenger reviews it; author edits and approves | Plan and first ADR, `docs/challenges/` |
| 0:45 to 1:50 | Implementer builds rules engine, hash-chained store, API, roles, then the one-page UI, looping on `make verify` | One commit per component |
| 1:50 to 2:30 | Independent test author, reviewer and red team; findings become failing tests, then fixes | Test and fix commits |
| 2:30 to 2:50 | CI workflow, generated controls document, limitations | Workflow, docs |
| 2:50 to 3:00 | Final `make verify`, clean-clone check of `make start`, `AI_USAGE.md` | Final commit |

**If time runs short, cut in this order:** Replay buttons in the UI, then the sample loader, then the dependency scan, then the Dockerfile if one was added. Never cut the hash chain, the verify bar, the tests or `make start`.

**Early signal.** If the rules engine and its tests are done and reviewed within the first 30 minutes of building, the plan is on track.

## Risks, limitations and production path

The main delivery risk is scope, and the main design limit is that a local hash chain proves tampering only against someone who cannot rewrite all of it.

**Risks and responses**

| Risk | Response |
| --- | --- |
| Scope grows beyond 3 hours | Fixed cut order above; non-goals stated in the README |
| Agent tests mirror agent code | A separate test author writes from the spec only |
| An agent loop gets stuck | Stop and report after 5 failed loops, a rule in `AGENTS.md` |
| Tools run without approval prompts | Work stays inside the repository folder, and the author reads each commit |
| Setup fails on the other laptop | Test the clone, install, test and push path before starting |

**Limitations to document**

- The role header is not authentication.
- A whole-chain rewrite by someone with database write access is not detected.
- Velocity rules use a local database, so they do not scale across instances.
- Rule-change approval and a rule administrator role are described, not built.
- Point values and thresholds are illustrative, not calibrated on real data.

**Production path (described in the README, not built)**

- Identity provider with real roles instead of a header.
- Managed database, with the latest chain hash anchored in write-once storage.
- A message stream in front and an in-memory store for velocity counters.
- Rule changes through a maker-checker approval with its own audit entries.
- Monitoring on decision outcomes, with a circuit breaker to manual review if approval rates drift.

**Open questions**

- Which operating system is the second laptop, and is `make` available on it?
- Should the point values be confirmed before the spec is written?
- Which regulatory references go into the controls document?
