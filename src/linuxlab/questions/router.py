"""Question endpoints. See docs/api.md#questions and docs/questions.md.

Only published questions listed in a published module exist for the client; a draft,
archived or unknown question is `404 question_not_found`, the same answer for all.

Reading a question never reveals which option is correct: the listing and the detail
carry the statement, the options' ids and texts and the hints, and never the answer,
the options' feedback or the explanation, whatever the user's progress. Those leave
the server only in the response to an answer, which the API checks against the
question's current version: the client sends an option id and the version it shows,
nothing else, and is never trusted to say whether the answer is right.

Each response carries the requesting user's progress, and never anyone else's: the
user comes from the session, never from the request.
"""

import math
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from linuxlab.api import ApiError, api_router
from linuxlab.auth.ratelimit import RateLimiter
from linuxlab.auth.router import CurrentUser, Database
from linuxlab.content.models import Module, Question, QuestionVersion
from linuxlab.content.schema import OPTION_ID, SLUG
from linuxlab.content.stored import question_spec
from linuxlab.questions.models import UserQuestion
from linuxlab.questions.service import (
    QuestionProgress,
    attempt_counts,
    progress_of,
    record_answer,
)

router = api_router(prefix="/api")

PUBLISHED = "published"
ANSWER_ATTEMPTS = 30
ANSWER_WINDOW_SECONDS = 60
# Read from the current version's specification for a card. The detail also reads the
# statement, the hints and the options, whole: each option's feedback is loaded with it
# and dropped by the route, which returns only the id and the text. `answer` and
# `explanation` are not read by the read routes; nothing of them, or of a feedback, is
# in their responses.
CARD_FIELDS = ("title", "summary", "difficulty", "estimated_minutes", "tags")


class QuestionCard(BaseModel):
    slug: str
    title: str
    summary: str
    difficulty: int
    estimated_minutes: int
    tags: list[str]
    # The question's current version, the one an answer is checked against.
    version: int
    progress: QuestionProgress


class QuestionModule(BaseModel):
    slug: str
    title: str
    description: str
    # Questions of the module the user completed, out of those listed.
    completed: int
    total: int
    questions: list[QuestionCard]


class ModuleRef(BaseModel):
    slug: str
    title: str


class PublicOption(BaseModel):
    id: str
    text: str  # Markdown, one line


class QuestionDetail(QuestionCard):
    module: ModuleRef
    # 1-based, among the module's published questions; the neighbours' slugs, if any.
    position: int
    previous: str | None
    next: str | None
    question: str  # Markdown
    options: list[PublicOption]
    hints: list[str]


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # The version the client shows: an answer to any other is refused.
    version: int = Field(ge=1)
    option: str = Field(pattern=rf"^{OPTION_ID.pattern}$")


class AnsweredQuestion(BaseModel):
    slug: str
    version: int


class AnswerResponse(BaseModel):
    question: AnsweredQuestion
    option: str
    correct: bool
    # The chosen option's feedback; none when it is the correct one.
    feedback: str | None
    explanation: str  # Markdown
    # Whether this answer recorded the user's first completion of the question.
    newly_completed: bool
    progress: QuestionProgress


def _visible() -> Any:
    return (
        (Module.status == PUBLISHED)
        & (Question.status == PUBLISHED)
        & Question.position.is_not(None)
    )


def _current_version() -> Any:
    return (QuestionVersion.question_id == Question.id) & (
        QuestionVersion.version == Question.current_version
    )


def _card(
    slug: str, version: int, fields: dict[str, Any], progress: QuestionProgress
) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": fields["title"],
        "summary": fields["summary"],
        "difficulty": fields["difficulty"],
        "estimated_minutes": fields["estimated_minutes"],
        "tags": fields.get("tags") or [],
        "version": version,
        "progress": progress,
    }


@router.get("/question-modules")
async def list_question_modules(user: CurrentUser, db: Database) -> list[QuestionModule]:
    """Published modules that have published questions, by slug; questions in module
    order."""
    attempts = attempt_counts(user.id).subquery()
    rows = await db.execute(
        select(
            Module.slug,
            Module.title,
            Module.description,
            Question.slug,
            Question.current_version,
            attempts.c.attempts,
            UserQuestion.completed_version,
            UserQuestion.completed_at,
            *(QuestionVersion.spec[name] for name in CARD_FIELDS),
        )
        .join(Question, Question.module_id == Module.id)
        .join(QuestionVersion, _current_version())
        .outerjoin(
            UserQuestion,
            (UserQuestion.user_id == user.id) & (UserQuestion.question_id == Question.id),
        )
        .outerjoin(
            attempts,
            (attempts.c.question_id == Question.id)
            & (attempts.c.question_version == Question.current_version),
        )
        .where(_visible())
        .order_by(Module.slug, Question.position)
    )
    modules: dict[str, QuestionModule] = {}
    for (
        module_slug,
        title,
        description,
        slug,
        version,
        count,
        done_version,
        done_at,
        *values,
    ) in rows:
        module = modules.get(module_slug)
        if module is None:
            module = modules[module_slug] = QuestionModule(
                slug=module_slug,
                title=title,
                description=description,
                completed=0,
                total=0,
                questions=[],
            )
        fields = dict(zip(CARD_FIELDS, values, strict=True))
        progress = QuestionProgress.of(count or 0, done_version, done_at)
        module.questions.append(QuestionCard.model_validate(_card(slug, version, fields, progress)))
        module.total += 1
        module.completed += progress.status == "completed"
    return list(modules.values())


@router.get("/questions/{slug}")
async def get_question(slug: str, user: CurrentUser, db: Database) -> QuestionDetail:
    """A published question at its current version, without its answer."""
    if not SLUG.fullmatch(slug):
        raise _not_found()
    attempts = attempt_counts(user.id).subquery()
    row = (
        await db.execute(
            select(
                Question.module_id,
                Module.slug,
                Module.title,
                Question.current_version,
                attempts.c.attempts,
                UserQuestion.completed_version,
                UserQuestion.completed_at,
                QuestionVersion.spec["question"],
                QuestionVersion.spec["options"],
                QuestionVersion.spec["hints"],
                *(QuestionVersion.spec[name] for name in CARD_FIELDS),
            )
            .join(Module, Module.id == Question.module_id)
            .join(QuestionVersion, _current_version())
            .outerjoin(
                UserQuestion,
                (UserQuestion.user_id == user.id) & (UserQuestion.question_id == Question.id),
            )
            .outerjoin(
                attempts,
                (attempts.c.question_id == Question.id)
                & (attempts.c.question_version == Question.current_version),
            )
            .where(_visible() & (Question.slug == slug))
        )
    ).one_or_none()
    if row is None:
        raise _not_found()
    (
        module_id,
        module_slug,
        module_title,
        version,
        count,
        done_version,
        done_at,
        statement,
        options,
        hints,
        *values,
    ) = row
    siblings = list(
        await db.scalars(
            select(Question.slug)
            .join(Module, Module.id == Question.module_id)
            .where(_visible() & (Question.module_id == module_id))
            .order_by(Question.position)
        )
    )
    index = siblings.index(slug)
    fields = dict(zip(CARD_FIELDS, values, strict=True))
    progress = QuestionProgress.of(count or 0, done_version, done_at)
    return QuestionDetail.model_validate(
        {
            **_card(slug, version, fields, progress),
            "module": {"slug": module_slug, "title": module_title},
            "position": index + 1,
            "previous": siblings[index - 1] if index > 0 else None,
            "next": siblings[index + 1] if index + 1 < len(siblings) else None,
            "question": statement,
            # Only the id and the text of each option: never its feedback.
            "options": [{"id": option["id"], "text": option["text"]} for option in options],
            "hints": hints or [],
        }
    )


@router.post("/questions/{slug}/answers")
async def answer_question(
    slug: str, body: AnswerRequest, request: Request, user: CurrentUser, db: Database
) -> AnswerResponse:
    limiter: RateLimiter = request.app.state.question_rate_limit
    retry_after = limiter.hit(str(user.id))
    if retry_after is not None:
        raise ApiError(
            429,
            "rate_limited",
            "Muitas respostas em pouco tempo. Aguarde um momento.",
            headers={"retry-after": str(max(1, math.ceil(retry_after)))},
        )
    if not SLUG.fullmatch(slug):
        raise _not_found()
    row = (
        await db.execute(
            select(Question.id, Question.current_version, QuestionVersion.spec)
            .join(Module, Module.id == Question.module_id)
            .join(QuestionVersion, _current_version())
            .where(_visible() & (Question.slug == slug))
        )
    ).one_or_none()
    if row is None:
        raise _not_found()
    question_id, version, stored = row
    if body.version != version:
        raise ApiError(
            409,
            "question_outdated",
            "Esta questão foi atualizada. Recarregue a página para ver a versão atual.",
        )
    # Read back with every rule of the sync; a version that breaks them is an error
    # (500), never half used.
    spec = question_spec(stored)
    chosen = next((option for option in spec.options if option.id == body.option), None)
    if chosen is None:
        raise ApiError(422, "invalid_option", "A alternativa escolhida não existe nesta questão.")
    correct = chosen.id == spec.answer
    newly_completed = await record_answer(db, user.id, question_id, version, chosen.id, correct)
    progress = await progress_of(db, user.id, question_id, version)
    await db.commit()
    return AnswerResponse(
        question=AnsweredQuestion(slug=slug, version=version),
        option=chosen.id,
        correct=correct,
        feedback=None if correct else chosen.feedback,
        explanation=spec.explanation,
        newly_completed=newly_completed,
        progress=progress,
    )


def _not_found() -> ApiError:
    return ApiError(404, "question_not_found", "Questão não encontrada.")
