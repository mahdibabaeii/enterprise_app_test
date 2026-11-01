# AI usage

This slice was built with Amp coding agents. The author owned the spec, the
constraints and the definition of done; agents executed; `make verify` decided
whether a step was finished. This page records what each agent did, how its
work was checked, what it caught, where the author stepped in, and where the
process slipped.

Times are local (UTC+11) on 2026-11-01, from the commit history.

## How the work was split

Work ran in separate Amp threads, each started by the author with its own
prompt. Reviews and the plan challenge ran in fresh threads that saw only the
documents and code, not the builder's conversation. Their "oracle" was a
read-only subagent started inside that thread.

| Phase | Agent role | Thread | Output |
| --- | --- | --- | --- |
| Plan | Planner | Main build thread | `docs/plan.md` |
| Gate 2 | Challenger, read-only | Fresh thread | `docs/challenges/01-plan-challenge.md` |
| Steps 3–6, 8–11 | Implementer | Main build thread | Skeleton, CI, rules, engine, API, demo commands, UI, README |
| Step 7 | Implementer | Separate thread | Chain hashing, store, tamper simulation |
| Reviews | Reviewer, read-only | One fresh thread per review | `docs/reviews/01-store-review.md`, `02-api-review.md`, `03-demo-review.md` |
| Review fixes | Implementer | One thread per review | One commit per finding |
| Evidence | Implementer | Main build thread | `docs/controls.md` generator, `docs/limitations.md`, this file |

## Task log

| Task | How it was delegated | How it was verified | Defects caught | Human interventions |
| --- | --- | --- | --- | --- |
| Plan (09:42–09:51) | Planner prompt: read `AGENTS.md` and the PRD, no code, produce file layout, hashed fields, tests mapped to A1–A9, commit sequence, scope split, risks | Author read the plan before committing it | 29 PRD gaps and ambiguities, including: A5's scores of 29 and 69 are unreachable with the PRD points; replay would mismatch on velocity; FastAPI 422s echo card numbers | Committed the plan unchanged; decided D1–D5 later |
| Plan challenge (09:50–09:56) | Fresh thread. "Use the oracle. Read only docs/PRD.md and docs/plan.md. Do not edit any file." Five challenges, hardest to reverse first | Main agent checked every cited plan line. The challenger ran one throwaway SQLite check outside the repo and said so | Hash contradiction between plan §2.2 and §2.5; duplicate JSON keys read differently by Python and SQLite; no decision time stored; hard rules not fail-safe; failed-safe records would never replay; rule versions compared as strings | Accepted three CHANGE verdicts, accepted two risks, **overruled** two proposals (a rule-content table and a `source` field). Decisions recorded in the challenge file |
| Steps 3–6 (10:07–10:13) | Build prompt: tests first from the PRD and plan, then code, loop on `make verify`, stop after 5 failed loops, one commit per step, do not push | Tests failed first on the missing module, then passed. Six deliberate engine bugs (server timezone, `>=` for velocity, `>` for decline, missing fail-safe floors, a floor that downgrades) each failed a test | Public PyPI is blocked on the author's network; the agent stopped and asked rather than commit an internal package-mirror URL into a public repo | Chose local-only mirror installs with pinned versions and no committed lock file (D1 amended); changed the commit trailer convention to drop thread links |
| Step 7 store (10:30–10:34) | Build prompt, separate thread, same rules | Tests first; 13 deliberate store bugs, 2 of which first survived and got new tests; `make verify` 90 passed, tamper simulation 5/5 | While building: a payload id of `true` passing as 1, invalid UTF-8 crashing verify, a BLOB payload passing verify | Scoped to step 7 only, "Do not push" |
| Duplicate step 7 attempt (10:30–10:39) | A second thread was given the same step 7 prompt | Cross-ran its own 33 tests against the committed store: 32 passed, 1 failed on its own setup | A rollback gap later reported as review 02 F2 | None; it made no commits |
| Review 01, store (10:45–10:55) | Fresh thread. "Use the oracle… Do not edit any code or test file… Do not invent findings." Probes only under a scratch folder | Each finding reproduced against temporary databases; mutation checks showed T26, T39 and T40 fail when their check is removed | F1 Medium: editing the head of the chain with recomputed hashes passes verify. F4 Medium: a second velocity rule fails open. F2, F3, F6 Low; F5, F7 Info | Ran the review in a fresh thread to keep it independent |
| Review 01 fixes (10:56–10:59) | Store thread | Failing test first per finding (a strict expected failure for F1), then `make verify`: 104 passed | — | Authorised the push |
| Step 8 API (11:00–11:18) | Main build thread, same rules | Tests first; eight deliberate bugs (unlisted route allowed, default 422 handler, replay reading later history, `rules_match` always true, key-less card reference, exception message logged, no 503 mapping, no fixed clock in the replay check) each failed a test; a real uvicorn run showed no card number in the server log | — | Confirmed that history counts are always computed and stored |
| Review 02, API (11:19–11:31) | Fresh thread. "use oracle to review what has been done in step 8" | TestClient probes, one real uvicorn run, mutants on a scratch copy | F1 Medium: epoch-seconds text accepted as a timestamp and read as UTC, which can turn a step-up into an approve. F2–F5 Low; F6 Info | "02-api-review.md is good" |
| Review 02 fixes (11:35–11:41) | Separate thread: "dont change yet, i want to make sure that wont impact stage 09" | Failing test first per finding, `make verify` after each commit, ending at 173 passed; F5's four replay-check mutants now all fail | A test double raising the wrong error type, left unchanged | Confirmed step 9 was finished, then let the fixes go ahead |
| Step 9 demo (11:20–11:32) | Main build thread | Tests first; four deliberate bugs (tamper path guard removed, triggers not restored, seed appending, list route public) each failed a test; live `make start` checks | `make start` reseeded the database before checking the port, so a second start wiped a running server's database; fixed before commit | — |
| Review 03, demo (11:36–11:49) | Fresh thread. "use oracle to test Stage 9" | Real `make start` servers on spare ports, curl, direct SQLite edits, a local copy of the CI start check | F1 Medium: a second `make tamper` restored the original bytes, so verify reported intact. F2–F4 Low; F5, F6 Info | — |
| Review 03 fixes (11:50–11:53) | Separate thread: "See what changes should be applied" | Failing test first for F1, F4, F5; mutant check for F6; live `make start` for F2; full `make verify` once at the end: 178 passed | — | "pusht your changes only" |
| Step 10 UI (11:36–11:46) | Main build thread; style reference set by the author | Rendered in a real browser for every state (analyst decision, 422, auditor 403, intact, broken after `make tamper`, analyst refused verify, phone width) and inspected each screenshot | "Reasons" label crowding the score scale; form defaults reusing a seeded card and merchant, which scored 55 instead of the PRD's 65 | Chose the visual reference; required a README screenshot |
| Step 11 README (11:51) | Main build thread | Every walkthrough score run against the real API on a fresh database; README rendered through GitHub's markdown API and inspected | The first draft's walkthrough numbers were wrong (it treated a merchant as new after it had been used) | In a fix thread: "we need more descriptive readme. how to install and execute the repo?" |
| Controls document (12:01) | Main build thread | Generator refuses an unknown control id (checked by adding a fake tag) and a control with no test; `make verify` fails when the document is stale | — | — |
| Independent tests | Subagent told to read only the PRD, plan, challenge decisions, `rules/v1.yaml`, README and test fixtures, never `app/`, `scripts/` or the existing tests, and to test black-box through the API | 91 cases, all passing; asked to mark any spec mismatch as a strict expected failure rather than weaken a test | No mismatch with the spec | — |
| Red team, review 04 | Read-only subagent attacking its own server and database, told to skip the accepted limitations | Each attack reproduced; attacks that held listed by surface | R1 Medium: a leading zero on the card number reset velocity, turning a step-up into an approve. R2 Medium: duplicate JSON keys could override `reported_stolen`. R3–R6 Low: card numbers in the access log and in identifiers, 500s on an unreadable row, an insert trigger that verify could not see. R7, R8 Info | — |
| Red team fixes | Main build thread, one commit per finding | Failing test first for each, `make verify` after each commit, ending at 290 passed; R3 checked against a real uvicorn server, R6 rendered in the UI | Verify's first trigger check missed a trigger written across lines; replaced before commit | R7 (non-ISO country codes) left as a documented limitation |

## Process slips

These happened and are recorded rather than hidden:

- **A commit on a failing build.** In the review 01 fixes, a commit command
  used `;` instead of `&&`, so a commit landed while `make verify` failed. It
  was fixed and folded into the right commit before the push.
- **Fix commits without a full verify each.** The review 03 fixes ran targeted
  tests per commit and the full `make verify` once at the end. The build was
  never shown failing, but the definition of done expects a full run per step.
- **Acting beyond the request.** The review 03 thread applied and committed
  fixes when asked to "see what changes should be applied". The review 02
  thread committed the review file after "is good", and edited the plan in three
  commits after asking about one.
- **Concurrent sessions in one checkout.** Several threads worked in the same
  folder at once. One duplicate step 7 attempt overwrote committed files in the
  working tree and restored them; one fix thread stashed another session's
  uncommitted UI file and restored it. Commits pushed from a temporary worktree
  came back under new hashes, which the main thread reconciled by rebasing.
- **Searching outside the repository.** A thread that could not find a prompt
  file searched the author's home folders, which `AGENTS.md` says not to do.
- **A mutation check that did not clean up.** In step 9, a missing scratch
  folder made backups fail, so four deliberate bugs stayed in place until the
  agent noticed the climbing failure count and restored the files.
- **Thread links in early commits.** Amp added thread-link trailers to the
  first commits. The author changed the convention; unpushed commits were
  rewritten, and the four already published keep their links.

## What the agents did not decide

- Scope, priorities and the definition of done (`AGENTS.md`, PRD, plan).
- Every gate-2 decision, including the two overruled challenges.
- Every push.
- The package-install approach on a network that blocks public PyPI.
- Which review findings to fix, and when.
