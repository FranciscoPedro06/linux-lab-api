"""Lab endpoints. See docs/api.md#labs.

Every route acts on the authenticated user's labs only. A lab id that does not exist
and one that belongs to another user both answer 404.
"""

import logging
import math
import uuid
from collections.abc import Iterable
from datetime import datetime

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from linuxlab.api import ApiError, api_router
from linuxlab.auth.ratelimit import RateLimiter
from linuxlab.auth.router import CurrentUser, Database
from linuxlab.content.models import Mission, MissionVersion
from linuxlab.content.schema import SLUG
from linuxlab.labs.access import owned_lab
from linuxlab.labs.lifecycle import (
    DifferentMissionError,
    LabCapacityError,
    Labs,
    LabStartError,
    MissionNotFoundError,
    MissionSpecError,
)
from linuxlab.labs.models import EndReason, LabSession, LabStatus

logger = logging.getLogger(__name__)

router = api_router(prefix="/api/labs")

LIST_LIMIT = 20
CREATE_ATTEMPTS = 10
CREATE_WINDOW_SECONDS = 10 * 60


class LabMission(BaseModel):
    """The mission version a lab was created for, as the client sees it."""

    slug: str
    title: str
    version: int


class PublicLab(BaseModel):
    """A lab as the client sees it. Container ids, parameters and other internals stay
    on the server."""

    id: uuid.UUID
    status: LabStatus
    end_reason: EndReason | None
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    mission: LabMission | None

    @classmethod
    def of(cls, lab: LabSession, mission: LabMission | None) -> "PublicLab":
        return cls(
            id=lab.id,
            status=LabStatus(lab.status),
            end_reason=EndReason(lab.end_reason) if lab.end_reason else None,
            created_at=lab.created_at,
            expires_at=lab.expires_at,
            ended_at=lab.ended_at,
            mission=mission,
        )


class CreateLabRequest(BaseModel):
    # The client names the mission only. Everything else (version, parameters, setup,
    # image, runtime, user, limits) comes from the catalog and the server's settings.
    model_config = ConfigDict(extra="forbid")

    mission_slug: str


async def public_labs(db: AsyncSession, labs: Iterable[LabSession]) -> list[PublicLab]:
    """Labs with the slug and title of their pinned mission version."""
    labs = list(labs)
    pinned = {
        (lab.mission_id, lab.mission_version)
        for lab in labs
        if lab.mission_id is not None and lab.mission_version is not None
    }
    missions: dict[tuple[int, int], LabMission] = {}
    if pinned:
        rows = await db.execute(
            select(
                MissionVersion.mission_id,
                MissionVersion.version,
                Mission.slug,
                MissionVersion.spec["title"].astext,
            )
            .join(Mission, Mission.id == MissionVersion.mission_id)
            .where(tuple_(MissionVersion.mission_id, MissionVersion.version).in_(pinned))
        )
        for mission_id, version, slug, title in rows:
            missions[(mission_id, version)] = LabMission(slug=slug, title=title, version=version)
    return [
        PublicLab.of(
            lab,
            missions.get((lab.mission_id, lab.mission_version))
            if lab.mission_id is not None and lab.mission_version is not None
            else None,
        )
        for lab in labs
    ]


async def public_lab(db: AsyncSession, lab: LabSession) -> PublicLab:
    return (await public_labs(db, [lab]))[0]


def _labs(request: Request) -> Labs:
    labs: Labs = request.app.state.labs
    return labs


def _not_found() -> ApiError:
    return ApiError(404, "lab_not_found", "Laboratório não encontrado.")


def _mission_not_found() -> ApiError:
    return ApiError(404, "mission_not_found", "Missão não encontrada.")


@router.get("/current")
async def current_lab(request: Request, user: CurrentUser, db: Database) -> PublicLab | None:
    lab = await _labs(request).active(user.id)
    return await public_lab(db, lab) if lab else None


@router.get("")
async def list_labs(user: CurrentUser, db: Database) -> list[PublicLab]:
    labs = await db.scalars(
        select(LabSession)
        .where(LabSession.user_id == user.id)
        .order_by(LabSession.created_at.desc())
        .limit(LIST_LIMIT)
    )
    return await public_labs(db, labs)


@router.get("/{lab_id}")
async def get_lab(lab_id: str, user: CurrentUser, db: Database) -> PublicLab:
    lab = await owned_lab(db, user.id, lab_id)
    if lab is None:
        raise _not_found()
    return await public_lab(db, lab)


@router.post("", status_code=201)
async def create_lab(
    body: CreateLabRequest, request: Request, response: Response, user: CurrentUser, db: Database
) -> PublicLab:
    limiter: RateLimiter = request.app.state.lab_rate_limit
    retry_after = limiter.hit(str(user.id))
    if retry_after is not None:
        raise ApiError(
            429,
            "rate_limited",
            "Muitos laboratórios iniciados em pouco tempo. Aguarde alguns minutos.",
            headers={"retry-after": str(max(1, math.ceil(retry_after)))},
        )
    if not SLUG.fullmatch(body.mission_slug):
        raise _mission_not_found()

    try:
        creation = await _labs(request).create(user.id, body.mission_slug)
    except MissionNotFoundError:
        raise _mission_not_found() from None
    except DifferentMissionError:
        raise ApiError(
            409,
            "active_lab_for_different_mission",
            "Você já tem um laboratório ativo de outra missão. Encerre-o antes de iniciar este.",
        ) from None
    except MissionSpecError:
        raise ApiError(500, "internal_error", "Erro interno. Tente novamente.") from None
    except LabCapacityError:
        raise ApiError(
            503,
            "lab_capacity_reached",
            "Todos os laboratórios estão em uso. Tente novamente em alguns minutos.",
        ) from None
    except LabStartError:
        raise ApiError(
            503, "lab_start_failed", "Não foi possível iniciar o laboratório. Tente novamente."
        ) from None

    lab = creation.lab
    if creation.created:
        return await public_lab(db, lab)
    if lab.status == LabStatus.READY:
        response.status_code = 200
        return await public_lab(db, lab)
    if lab.status == LabStatus.PROVISIONING:
        raise ApiError(409, "lab_provisioning", "O laboratório já está sendo iniciado.")
    raise ApiError(
        409, "lab_terminating", "O laboratório anterior ainda está sendo encerrado. Aguarde."
    )


@router.delete("/{lab_id}")
async def end_lab(lab_id: str, request: Request, user: CurrentUser, db: Database) -> PublicLab:
    lab = await owned_lab(db, user.id, lab_id)
    if lab is None:
        raise _not_found()
    owned_id = lab.id
    # The transaction that read the lab must not stay open while the lab is removed.
    await db.rollback()
    ended = await _labs(request).end(owned_id, EndReason.USER)
    assert ended is not None
    logger.info("lab ended by its user: lab=%s status=%s", ended.id, ended.status)
    return await public_lab(db, ended)
