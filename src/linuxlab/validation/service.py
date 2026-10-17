"""Running a validation: from a ready lab to a stored, final run.

1. Read the lab again and require it `ready`, with a pinned mission version.
2. Read that version's condition tree (never the mission's current version) and
   resolve it with the lab's parameters, in memory.
3. Check that the container runs, then collect every fact with one `labctl probe`
   (none when every condition is an answer).
4. Evaluate the tree and, in one transaction, insert the run, final, in
   validation_runs and, if it passed, the user's first completion of the mission in
   user_missions, with the lab's owner, mission and pinned version.

Validation reads only: it never runs setup again and changes nothing in the lab, its
parameters or its version. PostgreSQL and Docker share no transaction; nothing is
written before the result is known, so a validation interrupted midway (a cancelled
request, a crash) leaves no row rather than a partial one. The run and the
completion are committed together or not at all.

At most one validation runs per lab at a time, tracked in memory in this API
process. Like the terminal registry, this assumes a single process and has to move
elsewhere if the API runs as several workers.
"""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linuxlab.content.models import Mission, MissionVersion
from linuxlab.content.stored import InvalidSpecError, validation_spec
from linuxlab.labs.lifecycle import LabGoneError, Labs, utcnow
from linuxlab.labs.models import LabStatus
from linuxlab.labs.runtime import LabRuntime, LabRuntimeError
from linuxlab.progress.service import Progress, progress_of, record_completion
from linuxlab.validation.evaluate import Node, evaluate, to_record
from linuxlab.validation.models import ErrorCode, Outcome, ValidationRun
from linuxlab.validation.plan import Plan, plan
from linuxlab.validation.probe import Facts, ProbeError, ProbeErrorCode, run_probe

logger = logging.getLogger(__name__)


class LabNotReadyError(Exception):
    """The lab is not ready, has no pinned mission, or its container is gone."""


class ValidationInProgressError(Exception):
    """Another validation of the same lab has not finished."""


@dataclass(frozen=True)
class Run:
    id: int
    outcome: Outcome
    mission_slug: str
    mission_version: int
    # None only when the pinned version could not be read (invalid_spec).
    result: Node | None
    # The user's progress on the mission once the run is stored, and whether this run
    # recorded the first completion.
    progress: Progress
    newly_completed: bool


class Validations:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        runtime: LabRuntime,
        labs: Labs,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._runtime = runtime
        self._labs = labs
        self._running: set[uuid.UUID] = set()

    @contextmanager
    def _claim(self, lab_id: uuid.UUID) -> Iterator[None]:
        """Refuse a second validation of the same lab instead of waiting for the first.
        Released however the validation ends, including cancellation."""
        if lab_id in self._running:
            raise ValidationInProgressError(lab_id)
        self._running.add(lab_id)
        try:
            yield
        finally:
            self._running.discard(lab_id)

    async def validate(self, lab_id: uuid.UUID, answer: str | None) -> Run:
        """Validate the lab's current state. The caller has checked ownership.

        Raises LabNotReadyError or ValidationInProgressError before anything is
        stored. Every other outcome, including operational errors, is a stored run.
        """
        with self._claim(lab_id):
            return await self._validate(lab_id, answer)

    async def _validate(self, lab_id: uuid.UUID, answer: str | None) -> Run:
        lab = await self._labs.get(lab_id)
        if (
            lab is None
            or lab.status != LabStatus.READY
            or lab.mission_id is None
            or lab.mission_version is None
            or lab.params is None
        ):
            raise LabNotReadyError(lab_id)
        mission_id, version = lab.mission_id, lab.mission_version
        started_at = utcnow()

        async with self._sessionmaker() as db:
            row = (
                await db.execute(
                    select(MissionVersion.spec, Mission.slug)
                    .join(Mission, Mission.id == MissionVersion.mission_id)
                    .where(
                        MissionVersion.mission_id == mission_id, MissionVersion.version == version
                    )
                )
            ).one()
        spec, slug = row

        try:
            prepared: Plan | None = plan(validation_spec(spec).tree, lab.params)
        except InvalidSpecError:
            prepared = None

        facts: Facts | None = None
        failure: ProbeErrorCode | None = None
        try:
            container = await self._labs.running_container(lab)
        except LabGoneError:
            raise LabNotReadyError(lab_id) from None
        except LabRuntimeError:
            failure = ProbeErrorCode.RUNTIME_UNAVAILABLE
        else:
            if prepared is not None and prepared.facts:
                try:
                    facts = await run_probe(self._runtime, container.id, prepared.facts)
                except ProbeError as error:
                    failure = error.code

        result: Node | None = None
        if prepared is None:
            outcome, error_code = Outcome.ERROR, ErrorCode.INVALID_SPEC
        else:
            result = evaluate(prepared, facts, answer)
            outcome = Outcome(result.status.value)
            error_code = None
            if outcome == Outcome.ERROR:
                error_code = ErrorCode(failure.value) if failure else ErrorCode.CHECK_FAILED

        async with self._sessionmaker() as db:
            run = ValidationRun(
                lab_session_id=lab_id,
                mission_id=mission_id,
                mission_version=version,
                outcome=outcome.value,
                error_code=error_code.value if error_code else None,
                result=to_record(result) if result else None,
                answer_provided=answer is not None,
                started_at=started_at,
                finished_at=utcnow(),
            )
            db.add(run)
            await db.flush()
            # Only a pass completes the mission, at the lab's pinned version; failed
            # and error leave progress as it is.
            newly_completed = outcome == Outcome.PASSED and await record_completion(
                db, lab.user_id, mission_id, version, run.finished_at
            )
            progress = await progress_of(db, lab.user_id, mission_id)
            await db.commit()
        logger.info(
            "lab validated: lab=%s run=%s outcome=%s error=%s newly_completed=%s",
            lab_id,
            run.id,
            outcome,
            error_code,
            newly_completed,
        )
        return Run(
            id=run.id,
            outcome=outcome,
            mission_slug=slug,
            mission_version=version,
            result=result,
            progress=progress,
            newly_completed=newly_completed,
        )
