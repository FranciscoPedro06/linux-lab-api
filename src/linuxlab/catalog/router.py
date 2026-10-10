"""Catalog endpoints: published modules and missions. See docs/api.md#catalog.

Only published content is visible: a published mission, listed in a published
module. The one exception is the mission of the user's active lab, which its owner
sees at the lab's pinned version for as long as the lab is active, even if the
mission was archived or changed since. Responses carry presentation fields; setup,
parameters, conditions, solutions, counterexamples and the explanation never leave
the server. Progress is not part of the catalog yet.
"""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from linuxlab.api import ApiError, api_router
from linuxlab.auth.router import CurrentUser, Database
from linuxlab.content.models import Mission, MissionVersion, Module
from linuxlab.content.schema import SLUG
from linuxlab.labs.models import ACTIVE_STATUSES, LabSession

router = api_router(prefix="/api")

PUBLISHED = "published"
# Fields read from the current version's specification. `validation` is only used to
# compute requires_answer and is not returned.
CARD_FIELDS = ("title", "summary", "difficulty", "estimated_minutes", "tags", "validation")
DETAIL_FIELDS = ("objectives", "hints")


class MissionCard(BaseModel):
    slug: str
    title: str
    summary: str
    difficulty: int
    estimated_minutes: int
    tags: list[str]
    version: int
    requires_answer: bool


class CatalogModule(BaseModel):
    slug: str
    title: str
    description: str
    missions: list[MissionCard]


class ModuleRef(BaseModel):
    slug: str
    title: str


class MissionDetail(MissionCard):
    module: ModuleRef
    briefing: str  # Markdown
    objectives: list[str]
    hints: list[str]


def requires_answer(condition: dict[str, Any]) -> bool:
    """Whether a stored condition tree contains an `answer` condition."""
    if condition.get("type") == "answer":
        return True
    children = [*condition.get("all", []), *condition.get("any", [])]
    if "not" in condition:
        children.append(condition["not"])
    return any(requires_answer(child) for child in children)


def _visible() -> Any:
    return (
        (Module.status == PUBLISHED) & (Mission.status == PUBLISHED) & Mission.position.is_not(None)
    )


def _current_version() -> Any:
    return (MissionVersion.mission_id == Mission.id) & (
        MissionVersion.version == Mission.current_version
    )


def _card(slug: str, version: int, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": fields["title"],
        "summary": fields["summary"],
        "difficulty": fields["difficulty"],
        "estimated_minutes": fields["estimated_minutes"],
        "tags": fields.get("tags", []),
        "version": version,
        "requires_answer": requires_answer(fields["validation"]),
    }


@router.get("/modules")
async def list_modules(user: CurrentUser, db: Database) -> list[CatalogModule]:
    """Published modules that have published missions, by slug; missions in module order."""
    rows = await db.execute(
        select(
            Module.slug,
            Module.title,
            Module.description,
            Mission.slug,
            Mission.current_version,
            *(MissionVersion.spec[name] for name in CARD_FIELDS),
        )
        .join(Mission, Mission.module_id == Module.id)
        .join(MissionVersion, _current_version())
        .where(_visible())
        .order_by(Module.slug, Mission.position)
    )
    modules: dict[str, CatalogModule] = {}
    for module_slug, title, description, slug, version, *values in rows:
        module = modules.get(module_slug)
        if module is None:
            module = modules[module_slug] = CatalogModule(
                slug=module_slug, title=title, description=description, missions=[]
            )
        card = _card(slug, version, dict(zip(CARD_FIELDS, values, strict=True)))
        module.missions.append(MissionCard.model_validate(card))
    return list(modules.values())


@router.get("/missions/{slug}")
async def get_mission(slug: str, user: CurrentUser, db: Database) -> MissionDetail:
    """The mission at the version pinned by the user's active lab for it, if there is
    one, even if the mission was archived or changed since. Otherwise a published
    mission at its current version."""
    if not SLUG.fullmatch(slug):
        raise _not_found()
    pinned = await db.scalar(
        select(LabSession.mission_version)
        .join(Mission, Mission.id == LabSession.mission_id)
        .where(
            (LabSession.user_id == user.id)
            & LabSession.status.in_(ACTIVE_STATUSES)
            & (Mission.slug == slug)
        )
    )
    if pinned is not None:
        version = (MissionVersion.mission_id == Mission.id) & (MissionVersion.version == pinned)
        condition = Mission.slug == slug
    else:
        version = _current_version()
        condition = _visible() & (Mission.slug == slug)
    row = (
        await db.execute(
            select(
                Module.slug,
                Module.title,
                MissionVersion.version,
                MissionVersion.spec["briefing"]["content"],
                *(MissionVersion.spec[name] for name in (*CARD_FIELDS, *DETAIL_FIELDS)),
            )
            .join(Mission, Mission.module_id == Module.id)
            .join(MissionVersion, version)
            .where(condition)
        )
    ).one_or_none()
    if row is None:
        raise _not_found()
    module_slug, module_title, version, briefing, *values = row
    fields = dict(zip((*CARD_FIELDS, *DETAIL_FIELDS), values, strict=True))
    return MissionDetail.model_validate(
        {
            **_card(slug, version, fields),
            "module": {"slug": module_slug, "title": module_title},
            "briefing": briefing,
            "objectives": fields["objectives"],
            "hints": fields["hints"] or [],
        }
    )


def _not_found() -> ApiError:
    return ApiError(404, "mission_not_found", "Missão não encontrada.")
