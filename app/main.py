"""API layer: routes, roles and HTTP error mapping. No business logic here.

Run with: uvicorn app.main:create_app_from_env --factory
"""

import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.models import CreatedDecision, DecisionRecord, ErrorResponse, TransactionRequest
from app.rules import RULES_DIR, active_rule_set, load_rule_sets
from app.store import (
    ReplayResult,
    RuleVersionUnavailable,
    Store,
    StoreWriteError,
    VerifyResult,
    utc_now,
)

# Public demo key: all data is synthetic. A production key belongs in a vault (plan R20).
DEMO_CARD_REF_KEY = "demo-card-ref-key-public-not-a-secret"
DEFAULT_DB = Path("data/demo.db")
RECENT_LIMIT = 20

ROLES = frozenset({"analyst", "auditor"})
BOTH = frozenset({"analyst", "auditor"})

# Deny by default: a route missing here refuses every role (plan section 3).
# /docs and /openapi.json are not API routes, so this check does not apply to them.
ALLOWED_ROLES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/decisions"): frozenset({"analyst"}),
    ("GET", "/decisions"): BOTH,
    ("GET", "/decisions/{decision_id}"): BOTH,
    ("GET", "/decisions/{decision_id}/replay"): BOTH,
    ("GET", "/audit/verify"): frozenset({"auditor"}),
}

NOT_FOUND = "decision not found"
NO_DECISION = (
    "decision could not be recorded, so no decision was made;"
    " the caller must treat the transaction as step-up"
)
ERRORS = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
}


def require_role(
    request: Request, x_role: Annotated[str | None, Header(description="analyst or auditor")] = None
) -> str:
    """Check the X-Role header against the route's allowlist entry."""
    if x_role not in ROLES:
        raise HTTPException(401, "X-Role header must be 'analyst' or 'auditor'")
    path = request.scope["route"].path
    if x_role not in ALLOWED_ROLES.get((request.method, path), frozenset()):
        raise HTTPException(403, f"role '{x_role}' may not {request.method} {path}")
    return x_role


async def validation_error(request: Request, error: Exception) -> JSONResponse:
    """422 without the submitted values, so a card number is never echoed (plan R19)."""
    assert isinstance(error, RequestValidationError)
    detail = [
        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


def create_app(
    db_path: Path,
    *,
    rules_dir: Path = RULES_DIR,
    clock: Callable[[], datetime] = utc_now,
    card_ref_key: str = DEMO_CARD_REF_KEY,
) -> FastAPI:
    """Build the app. Fails to start if any rule file is invalid (fail closed)."""
    rule_sets = load_rule_sets(rules_dir)
    active = active_rule_set(rule_sets)
    decisions = Store(db_path, clock=clock)

    app = FastAPI(
        title="Card Decision Service",
        dependencies=[Depends(require_role)],
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.add_exception_handler(RequestValidationError, validation_error)

    @app.post(
        "/decisions",
        status_code=201,
        response_model=CreatedDecision,
        responses={**ERRORS, 503: {"model": ErrorResponse}},
    )
    def create_decision(transaction: TransactionRequest) -> CreatedDecision:
        try:
            payload = decisions.append(transaction.to_record(card_ref_key), active)
        except StoreWriteError as error:
            raise HTTPException(503, NO_DECISION) from error
        return CreatedDecision.model_validate({**payload, "thresholds": active.thresholds})

    @app.get("/decisions", response_model=list[DecisionRecord], responses=ERRORS)
    def recent_decisions() -> list[DecisionRecord]:
        return [DecisionRecord.model_validate(p) for p in decisions.latest(RECENT_LIMIT)]

    @app.get(
        "/decisions/{decision_id}",
        response_model=DecisionRecord,
        responses={**ERRORS, 404: {"model": ErrorResponse}},
    )
    def get_decision(decision_id: int) -> DecisionRecord:
        payload = decisions.get(decision_id)
        if payload is None:
            raise HTTPException(404, NOT_FOUND)
        return DecisionRecord.model_validate(payload)

    @app.get(
        "/decisions/{decision_id}/replay",
        response_model=ReplayResult,
        responses={**ERRORS, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    def replay_decision(decision_id: int) -> ReplayResult:
        try:
            result = decisions.replay(decision_id, rule_sets)
        except RuleVersionUnavailable as error:
            raise HTTPException(409, str(error)) from error
        if result is None:
            raise HTTPException(404, NOT_FOUND)
        return result

    @app.get("/audit/verify", response_model=VerifyResult, responses=ERRORS)
    def verify_chain() -> VerifyResult:
        return decisions.verify()

    return app


def create_app_from_env() -> FastAPI:
    """App factory for uvicorn, configured by DECISIONS_DB and CARD_REF_KEY."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    db_path = Path(os.environ.get("DECISIONS_DB", DEFAULT_DB))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(db_path, card_ref_key=os.environ.get("CARD_REF_KEY", DEMO_CARD_REF_KEY))
