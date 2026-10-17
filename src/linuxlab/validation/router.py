"""POST /api/labs/{id}/validate. See docs/api.md#post-apilabsidvalidate.

The client sends at most an answer. The mission version, the conditions, paths,
patterns, the probe and every limit come from the lab's pinned version and the
server. The response carries a status and a message per condition: messages come
from the mission or are fixed defaults, never from the lab's state. It also carries
the lab owner's progress on the mission and whether this run completed it.
"""

import math
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, field_validator

from linuxlab.api import ApiError, api_router
from linuxlab.auth.ratelimit import RateLimiter
from linuxlab.auth.router import CurrentUser, Database
from linuxlab.labs.access import owned_lab
from linuxlab.progress.service import Progress
from linuxlab.validation.evaluate import Node
from linuxlab.validation.models import Outcome
from linuxlab.validation.service import LabNotReadyError, ValidationInProgressError, Validations

router = api_router(prefix="/api/labs")

VALIDATE_ATTEMPTS = 20
VALIDATE_WINDOW_SECONDS = 60
MAX_ANSWER_BYTES = 1024

NodeStatus = Literal["passed", "failed", "error"]


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Compared by the API with the mission's expected answer; never stored, logged or
    # sent to the lab.
    answer: str | None = None

    @field_validator("answer")
    @classmethod
    def _bounded(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > MAX_ANSWER_BYTES:
            raise ValueError(f"answer longer than {MAX_ANSWER_BYTES} bytes")
        return value


class CheckResult(BaseModel):
    kind: Literal["check"]
    id: str
    status: NodeStatus
    message: str


class OperatorResult(BaseModel):
    kind: Literal["all", "any", "not"]
    status: NodeStatus
    message: str | None
    children: list["CheckResult | OperatorResult"]


class ValidationMission(BaseModel):
    slug: str
    version: int


class ValidationResponse(BaseModel):
    run_id: int
    outcome: Outcome
    # Whether this validation recorded the user's first completion of the mission.
    newly_completed: bool
    # The lab's pinned mission version, which the run was checked against.
    mission: ValidationMission
    # The user's progress on the mission, across versions, after this run.
    progress: Progress
    result: CheckResult | OperatorResult | None


def public_result(node: Node) -> CheckResult | OperatorResult:
    if node.kind == "check":
        assert node.id is not None and node.message is not None
        return CheckResult(kind="check", id=node.id, status=node.status.value, message=node.message)
    assert node.kind in ("all", "any", "not")
    return OperatorResult(
        kind=node.kind,
        status=node.status.value,
        message=node.message,
        children=[public_result(child) for child in node.children],
    )


@router.post("/{lab_id}/validate")
async def validate_lab(
    lab_id: str, body: ValidateRequest, request: Request, user: CurrentUser, db: Database
) -> ValidationResponse:
    limiter: RateLimiter = request.app.state.validation_rate_limit
    retry_after = limiter.hit(str(user.id))
    if retry_after is not None:
        raise ApiError(
            429,
            "rate_limited",
            "Muitas verificações em pouco tempo. Aguarde um momento.",
            headers={"retry-after": str(max(1, math.ceil(retry_after)))},
        )
    lab = await owned_lab(db, user.id, lab_id)
    if lab is None:
        raise ApiError(404, "lab_not_found", "Laboratório não encontrado.")
    owned_id = lab.id
    # No transaction stays open while the lab is probed.
    await db.rollback()

    validations: Validations = request.app.state.validations
    try:
        run = await validations.validate(owned_id, body.answer)
    except LabNotReadyError:
        raise ApiError(
            409, "lab_not_ready", "O laboratório não está pronto para ser verificado."
        ) from None
    except ValidationInProgressError:
        raise ApiError(
            409,
            "validation_in_progress",
            "Uma verificação deste laboratório já está em andamento.",
        ) from None
    return ValidationResponse(
        run_id=run.id,
        outcome=run.outcome,
        newly_completed=run.newly_completed,
        mission=ValidationMission(slug=run.mission_slug, version=run.mission_version),
        progress=run.progress,
        result=public_result(run.result) if run.result else None,
    )
