"""API layer: routes, roles and HTTP error mapping. No business logic here.

Run with: uvicorn app.main:create_app_from_env --factory
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app.models import (
    CreatedDecision,
    DecisionRecord,
    ErrorResponse,
    TransactionRequest,
    ValidationErrorResponse,
    ValidationIssue,
)
from app.rules import RULES_DIR, active_rule_set, load_rule_sets
from app.store import (
    HistoryUnavailable,
    ReplayResult,
    RuleVersionUnavailable,
    Store,
    StoreWriteError,
    VerifyResult,
    utc_now,
)

# Public demo key: all data is synthetic. A production key belongs in a vault (plan R20).
DEMO_CARD_REF_KEY = "demo-card-ref-key-public-not-a-secret"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "demo.db"
INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"
RECENT_LIMIT = 20
# Record ids are SQLite integers; a larger id cannot be bound, so it is a 422.
DecisionId = Annotated[int, PathParam(ge=1, le=2**63 - 1)]

ROLES = frozenset({"analyst", "auditor"})
BOTH = frozenset({"analyst", "auditor"})

# Deny by default: a route missing here refuses every role (plan section 3).
# /docs and /openapi.json are not API routes, so this check does not apply to them.
PUBLIC_ROUTES = frozenset({("GET", "/")})
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
    422: {"model": ValidationErrorResponse},
}


def require_role(
    request: Request, x_role: Annotated[str | None, Header(description="analyst or auditor")] = None
) -> str | None:
    """Check the X-Role header against the route's allowlist entry."""
    path = request.scope["route"].path
    if (request.method, path) in PUBLIC_ROUTES:
        return None
    if x_role not in ROLES:
        raise HTTPException(401, "X-Role header must be 'analyst' or 'auditor'")
    if x_role not in ALLOWED_ROLES.get((request.method, path), frozenset()):
        raise HTTPException(403, f"role '{x_role}' may not {request.method} {path}")
    return x_role


def _refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise RequestValidationError(
            [{"loc": ("body",), "msg": "duplicate key in JSON body", "type": "json_duplicate_key"}]
        )
    return dict(pairs)


async def reject_duplicate_keys(request: Request) -> None:
    """Refuse a body that repeats a key. json.loads keeps the last duplicate, so another
    parser reading the first could see a different transaction (red team R2)."""
    try:
        json.loads(await request.body(), object_pairs_hook=_refuse_duplicate_keys)
    except (ValueError, UnicodeDecodeError):
        return  # Not JSON at all: FastAPI's own body validation reports it.


async def validation_error(request: Request, error: Exception) -> JSONResponse:
    """422 without the submitted values, so a card number is never echoed (plan R19)."""
    assert isinstance(error, RequestValidationError)
    detail = [
        ValidationIssue(
            # An unexpected field's name is caller input too, so it is cut from loc.
            loc=list(item["loc"][:-1] if item["type"] == "extra_forbidden" else item["loc"]),
            msg=item["msg"],
            type=item["type"],
        )
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=422, content=ValidationErrorResponse(detail=detail).model_dump()
    )


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

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(INDEX_HTML, media_type="text/html")

    @app.post(
        "/decisions",
        status_code=201,
        response_model=CreatedDecision,
        responses={**ERRORS, 503: {"model": ErrorResponse}},
        dependencies=[Depends(reject_duplicate_keys)],
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
    def get_decision(decision_id: DecisionId) -> DecisionRecord:
        payload = decisions.get(decision_id)
        if payload is None:
            raise HTTPException(404, NOT_FOUND)
        return DecisionRecord.model_validate(payload)

    @app.get(
        "/decisions/{decision_id}/replay",
        response_model=ReplayResult,
        responses={
            **ERRORS,
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def replay_decision(decision_id: DecisionId) -> ReplayResult:
        try:
            result = decisions.replay(decision_id, rule_sets)
        except RuleVersionUnavailable as error:
            raise HTTPException(409, str(error)) from error
        except HistoryUnavailable as error:
            raise HTTPException(503, f"{error}, so the decision could not be replayed") from error
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
