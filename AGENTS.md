# Project conventions

## What this is
A card transaction decision service for a regulated bank. Every automated
decision must be explainable, replayable and provably unaltered.
Priorities, in order: correctness, auditability, explainability, simplicity.

The product requirements are in `docs/PRD.md`. The approved plan is in
`docs/plan.md` once it exists. Do not add features beyond them without asking.

## Stack
- Python 3.12, FastAPI, Pydantic, SQLite, pytest, ruff
- One-page UI: a single static HTML file with vanilla JavaScript, served by
  FastAPI. No framework and no build step.

## Commands
These are created during the build. If one is missing when you need it,
creating it is part of the current step.
- `make start`   install, seed demo data, serve UI and API on port 8000
- `make verify`  lint, tests, tamper simulation, determinism replay
- `make demo`    reload sample data into the demo database
- `make tamper`  corrupt one record in the demo database only

## Definition of done
- A step is done only when `make verify` exits 0.
- If `make verify` fails, fix it and rerun. After 5 failed loops on the same
  problem, stop and report what is blocking you. Do not keep retrying.

## Working rules
- Work in small steps. One purpose per commit, with a clear message.
  Leave the Amp thread trailers on commits.
- Write or update tests with every change. Tests come from the spec, not from
  the implementation.
- Stay inside this repository folder. Do not touch files outside it.
- Do not add dependencies without asking, and say why when you do.
- When unsure about scope or design, stop and ask rather than guess.
- Containers: this project uses Podman, not Docker. The optional container file
  is named Containerfile. Never write docker commands. Containers are optional
  and are built only after the must-tier work is green.

## Domain rules (non-negotiable)
- Decisions are deterministic: same input and same rule version, same output.
  Rules use the transaction's own timestamp, never the server clock.
- Fail safe: if a rule errors or data is missing, the outcome is step-up,
  never approve.
- The audit log is append-only and hash-chained. Never update or delete
  records. Hash canonical JSON (sorted keys) plus the previous record's hash.
- Appending to the chain must be serialised so two requests cannot extend the
  same previous hash.
- Never log full card numbers. Mask to the last 4 digits. Synthetic data only.
- Rules live in `rules/*.yaml` with a version. Do not hardcode thresholds or
  point values in code.
- The auditor role can never create a decision.
- The UI contains no business logic. It only calls the API.

## Code conventions
- Layers: API, engine, store. No business logic in route handlers.
- Type hints on all functions. Pydantic models for all request and response
  bodies.
- Errors map to specific HTTP status codes with a clear message.
- Tag tests with the control id they cover, for example `@control("AUD-01")`,
  so the controls document can be generated from the tags.

## Out of scope (do not build)
Cloud deployment, real authentication, streaming ingestion, machine learning,
a rule-change approval workflow.
