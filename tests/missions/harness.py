"""Mission harness: runs every mission's solutions and counterexamples in real labs.

See docs/missions.md#mission-tests. Trusted test code, never part of the production
image (tests/ is in .dockerignore). It drives the production code paths and adds no
evaluator of its own:

1. the content directory is read with load_content and synced with sync_content
   into a throwaway database. Statuses are set to `published` on the loaded copy
   only, so drafts can be tested; the status is not part of a version, so the
   stored version is the one the real sync would store;
2. each case reads its script from the version stored in mission_versions, never
   from the content directory again;
3. each case gets a new user and a new lab from Labs.create (parameters, setup);
4. the lab is validated with Validations.validate (labctl probe, the evaluator,
   validation_runs, progress) before and after the script runs.

Reports name the mission, its version, the short hash, the case, the step, the
class and the code of a failure, and the statuses and reasons of the conditions.
They never carry parameters, script output, answers, or expected or observed
values. Cases are named by position (`solution-2`): script names and labels can
say how a mission is solved.
"""

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linuxlab.auth.models import User
from linuxlab.content.loader import Content, ContentInvalid, content_hash, load_content
from linuxlab.content.models import Mission, MissionVersion
from linuxlab.content.params import value_count
from linuxlab.content.schema import Answer, Param, checks
from linuxlab.content.stored import provisioning_spec, validation_spec
from linuxlab.content.sync import sync_content
from linuxlab.labs.lifecycle import (
    LabCapacityError,
    LabGoneError,
    Labs,
    LabStartError,
    MissionNotFoundError,
    MissionSpecError,
    PreparationError,
    PreparationStage,
)
from linuxlab.labs.models import EndReason, LabSession, LabStatus
from linuxlab.labs.runtime import ExecResult, LabRuntime, LabRuntimeError
from linuxlab.validation.evaluate import Node
from linuxlab.validation.models import Outcome, ValidationRun
from linuxlab.validation.router import MAX_ANSWER_BYTES
from linuxlab.validation.service import (
    LabNotReadyError,
    Run,
    ValidationInProgressError,
    Validations,
)

# Each solution and counterexample runs as a bash script read from standard input, as
# setup does, so nothing is written to the lab and a failing command stops it.
SCRIPT_COMMAND = ("bash", "-euo", "pipefail")
SCRIPT_SECONDS = 30

# Lists the processes the student can read the environment of, and prints how many
# carry a LAB_PARAM_ variable. Fixed code, run as the student: it checks what the
# student could see, and prints only a count, never a value.
LEAK_CHECK_CODE = """
import os
found = 0
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) == os.getpid():
        continue
    try:
        with open("/proc/" + name + "/environ", "rb") as environ:
            variables = environ.read().split(b"\\0")
    except OSError:
        continue
    if any(variable.startswith(b"LAB_PARAM_") for variable in variables):
        found += 1
print(found)
"""
LEAK_CHECK_COMMAND = ("python3", "-I", "-S", "-c", LEAK_CHECK_CODE)
LEAK_CHECK_SECONDS = 10

# The missions the roadmap plans for increment 11 (README).
INITIAL_MISSIONS = (
    "notes-backup",
    "tidy-project",
    "secure-deploy-script",
    "hidden-file",
    "runaway-process",
)


class CaseKind(enum.StrEnum):
    SOLUTION = "solution"
    COUNTEREXAMPLE = "counterexample"


class Step(enum.StrEnum):
    CREATE = "create"  # Labs.create: container, labctl init, setup
    FRESH = "fresh"  # validation before the script runs
    LEAK = "leak"  # LAB_PARAM_ check
    SCRIPT = "script"
    ANSWER = "answer"
    VALIDATE = "validate"  # validation after the script ran


class FailureClass(enum.StrEnum):
    CONTENT = "content"  # the mission must change
    PLATFORM = "platform"  # the platform or the test infrastructure failed


class FailureCode(enum.StrEnum):
    # content
    INVALID_CONTENT = "invalid_content"
    SETUP_FAILED = "setup_failed"
    SETUP_TIMED_OUT = "setup_timed_out"
    SCRIPT_FAILED = "script_failed"
    SCRIPT_TIMED_OUT = "script_timed_out"
    ANSWER_INVALID = "answer_invalid"
    FRESH_NOT_FAILED = "fresh_not_failed"
    SOLUTION_NOT_PASSED = "solution_not_passed"
    COUNTEREXAMPLE_NOT_FAILED = "counterexample_not_failed"
    PARAM_LEAK = "param_leak"
    PARAMS_DEGENERATE = "params_degenerate"
    # platform
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    LAB_START_FAILED = "lab_start_failed"
    LABCTL_INIT_FAILED = "labctl_init_failed"
    VALIDATION_ERROR = "validation_error"
    LEAK_CHECK_FAILED = "leak_check_failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


CONTENT_CODES = frozenset(
    {
        FailureCode.INVALID_CONTENT,
        FailureCode.SETUP_FAILED,
        FailureCode.SETUP_TIMED_OUT,
        FailureCode.SCRIPT_FAILED,
        FailureCode.SCRIPT_TIMED_OUT,
        FailureCode.ANSWER_INVALID,
        FailureCode.FRESH_NOT_FAILED,
        FailureCode.SOLUTION_NOT_PASSED,
        FailureCode.COUNTEREXAMPLE_NOT_FAILED,
        FailureCode.PARAM_LEAK,
        FailureCode.PARAMS_DEGENERATE,
    }
)


def failure_class(code: FailureCode) -> FailureClass:
    return FailureClass.CONTENT if code in CONTENT_CODES else FailureClass.PLATFORM


@dataclass(frozen=True)
class MissionUnderTest:
    slug: str
    version: int
    content_hash: str
    spec: Mapping[str, Any]

    @property
    def short_hash(self) -> str:
        return self.content_hash[:12]

    @property
    def requires_answer(self) -> bool:
        return any(isinstance(leaf, Answer) for leaf in checks(validation_spec(self.spec).tree))

    def cases(self) -> list["Case"]:
        return [
            Case(self, kind, index)
            for kind, key in (
                (CaseKind.SOLUTION, "solutions"),
                (CaseKind.COUNTEREXAMPLE, "counterexamples"),
            )
            for index in range(len(self.spec[key]))
        ]


@dataclass(frozen=True)
class Case:
    mission: MissionUnderTest
    kind: CaseKind
    index: int  # 0-based position in solutions or counterexamples

    @property
    def name(self) -> str:
        return f"{self.kind}-{self.index + 1}"

    @property
    def script(self) -> str:
        key = "solutions" if self.kind == CaseKind.SOLUTION else "counterexamples"
        content: str = self.mission.spec[key][self.index]["script"]["content"]
        return content

    @property
    def expected(self) -> Outcome:
        return Outcome.PASSED if self.kind == CaseKind.SOLUTION else Outcome.FAILED


@dataclass(frozen=True)
class Failure:
    code: FailureCode
    step: Step
    # Closed-world details only: outcomes, error codes, exit codes, condition ids
    # with statuses and reasons, exception class names.
    detail: str = ""

    @property
    def failure_class(self) -> FailureClass:
        return failure_class(self.code)


@dataclass(frozen=True)
class CaseResult:
    case: Case
    step: Step  # the last step reached
    failure: Failure | None = None
    lab_id: uuid.UUID | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def report(result: CaseResult) -> str:
    mission = result.case.mission
    head = f"{mission.slug} v{mission.version} {mission.short_hash} {result.case.name}"
    if result.failure is None:
        return f"{head}: ok"
    failure = result.failure
    line = f"{head} step={failure.step} [{failure.failure_class}] {failure.code}"
    return f"{line}: {failure.detail}" if failure.detail else line


def conditions(node: Node | None) -> str:
    """The leaves of a result as `id=status(reason)`, in tree order."""
    if node is None:
        return "no result"
    leaves: list[str] = []

    def walk(current: Node) -> None:
        if current.kind == "check":
            reason = f"({current.reason})" if current.reason else ""
            leaves.append(f"{current.id}={current.status}{reason}")
        for child in current.children:
            walk(child)

    walk(node)
    return " ".join(leaves)


def degenerate_params(params: Mapping[str, Param]) -> Failure | None:
    """A parameter whose generator can draw only one value is the same in every lab."""
    names = sorted(name for name, param in params.items() if value_count(param) < 2)
    if not names:
        return None
    return Failure(
        FailureCode.PARAMS_DEGENERATE,
        Step.CREATE,
        f"parameters that cannot vary between labs: {', '.join(names)}",
    )


def static_check(root: Path) -> list[str]:
    """What can be found without a database or a lab: the content rules of the sync,
    and parameters that cannot vary. One line per problem; empty when there is none."""
    try:
        content = load_content(root)
    except ContentInvalid as error:
        return [f"[content] {FailureCode.INVALID_CONTENT}: {item}" for item in error.errors]
    problems = []
    for mission in content.missions:
        failure = degenerate_params(provisioning_spec(mission.spec).params)
        if failure is not None:
            problems.append(
                f"{mission.slug} {mission.content_hash[:12]} [{failure.failure_class}] "
                f"{failure.code}: {failure.detail}"
            )
    return problems


class AnswerInvalidError(Exception):
    """The script's output is not a usable answer. The message never quotes it."""


def answer_from(result: ExecResult) -> str:
    """The answer a script printed: its standard output as UTF-8, without the final
    line terminator. Empty, truncated, non-UTF-8 or oversized output is refused."""
    if result.truncated:
        raise AnswerInvalidError("output was truncated")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise AnswerInvalidError("output is not UTF-8") from None
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise AnswerInvalidError("output is empty")
    if len(text.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise AnswerInvalidError(f"output is longer than {MAX_ANSWER_BYTES} bytes")
    return text


def published(content: Content) -> Content:
    """The same content with every module and mission published. Only for a
    throwaway database: statuses are not part of a version."""
    return replace(
        content,
        modules=tuple(replace(module, status="published") for module in content.modules),
        missions=tuple(replace(mission, status="published") for mission in content.missions),
    )


class HashMismatchError(Exception):
    pass


async def prepare(
    sessionmaker: async_sessionmaker[AsyncSession], root: Path
) -> list[MissionUnderTest]:
    """Load and sync the content in `root`, then read each mission back from its
    stored current version. Raises ContentInvalid when the content is invalid."""
    content = load_content(root)
    await sync_content(sessionmaker, published(content))
    loaded = {mission.slug: mission.content_hash for mission in content.missions}
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(
                    Mission.slug,
                    MissionVersion.version,
                    MissionVersion.content_hash,
                    MissionVersion.spec,
                )
                .join(
                    MissionVersion,
                    (MissionVersion.mission_id == Mission.id)
                    & (MissionVersion.version == Mission.current_version),
                )
                .where(Mission.slug.in_(loaded))
                .order_by(Mission.slug)
            )
        ).all()
    missions = [
        MissionUnderTest(slug, version, stored, spec) for slug, version, stored, spec in rows
    ]
    for mission in missions:
        if not (mission.content_hash == loaded[mission.slug] == content_hash(dict(mission.spec))):
            raise HashMismatchError(f"{mission.slug}: stored version does not match the content")
    if len(missions) != len(loaded):
        raise HashMismatchError("a mission of the content was not stored")
    return missions


class Harness:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        runtime: LabRuntime,
        labs: Labs,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._runtime = runtime
        self._labs = labs
        self._validations = Validations(sessionmaker, runtime, labs)

    async def run(self, case: Case) -> CaseResult:
        """Run one case in a new lab, owned by a new user, and end the lab."""
        lab: LabSession | None = None
        step = Step.CREATE
        try:
            user_id = await self._user()
            lab, failure = await self._create(user_id, case.mission.slug)
            if failure is not None:
                return CaseResult(case, step, failure, lab.id if lab else None)
            assert lab is not None
            step = Step.FRESH
            failure = await self._fresh(lab)
            if failure is None:
                step = Step.LEAK
                failure = await self._leak_check(lab)
            if failure is None:
                step = Step.SCRIPT
                failure, answer = await self._script(case, lab)
                if failure is None:
                    step = Step.VALIDATE
                    failure = await self._final(case, lab, answer)
            return CaseResult(case, failure.step if failure else step, failure, lab.id)
        except (
            LabRuntimeError,
            LabNotReadyError,
            ValidationInProgressError,
            SQLAlchemyError,
            OSError,
            TimeoutError,
        ) as error:
            failure = Failure(FailureCode.INFRASTRUCTURE_ERROR, step, type(error).__name__)
            return CaseResult(case, step, failure, lab.id if lab else None)
        finally:
            if lab is not None:
                await self._labs.end(lab.id, EndReason.USER)

    async def _user(self) -> uuid.UUID:
        user = User(
            id=uuid.uuid4(),
            email=f"harness-{uuid.uuid4().hex}@example.invalid",
            password_hash="$argon2id$harness",
            display_name="Harness",
        )
        async with self._sessionmaker() as db:
            db.add(user)
            await db.commit()
        return user.id

    async def _create(
        self, user_id: uuid.UUID, slug: str
    ) -> tuple[LabSession | None, Failure | None]:
        try:
            creation = await self._labs.create(user_id, slug, session_id=None)
        except LabStartError as error:
            failure = _start_failure(error)
            if failure.code == FailureCode.LAB_START_FAILED and not await self._runtime_available():
                failure = Failure(FailureCode.RUNTIME_UNAVAILABLE, Step.CREATE, failure.detail)
            return None, failure
        except (MissionNotFoundError, MissionSpecError, LabCapacityError) as error:
            return None, Failure(FailureCode.LAB_START_FAILED, Step.CREATE, type(error).__name__)
        lab = creation.lab
        if not creation.created or lab.status != LabStatus.READY:
            return lab, Failure(FailureCode.LAB_START_FAILED, Step.CREATE, f"lab {lab.status}")
        return lab, None

    async def _runtime_available(self) -> bool:
        try:
            await self._runtime.ping()
        except LabRuntimeError:
            return False
        return True

    async def _validate(
        self, lab: LabSession, answer: str | None, step: Step
    ) -> tuple[Run | None, Failure | None]:
        run = await self._validations.validate(lab.id, answer)
        if run.outcome == Outcome.ERROR:
            return run, Failure(FailureCode.VALIDATION_ERROR, step, await self._error_code(run))
        return run, None

    async def _fresh(self, lab: LabSession) -> Failure | None:
        run, failure = await self._validate(lab, None, Step.FRESH)
        if failure is not None:
            return failure
        assert run is not None
        if run.outcome != Outcome.FAILED:
            return Failure(
                FailureCode.FRESH_NOT_FAILED,
                Step.FRESH,
                f"expected failed, got {run.outcome}; {conditions(run.result)}",
            )
        return None

    async def _leak_check(self, lab: LabSession) -> Failure | None:
        container = await self._container(lab)
        result = await self._runtime.exec(
            container, LEAK_CHECK_COMMAND, user="student", time_limit=LEAK_CHECK_SECONDS
        )
        output = result.stdout.decode("ascii", errors="replace").strip()
        if result.exit_code != 0 or result.timed_out or not output.isdecimal():
            return Failure(
                FailureCode.LEAK_CHECK_FAILED,
                Step.LEAK,
                f"exit {result.exit_code}, timed out {result.timed_out}",
            )
        if int(output) > 0:
            return Failure(
                FailureCode.PARAM_LEAK,
                Step.LEAK,
                f"{int(output)} processes readable by the student carry LAB_PARAM_ variables",
            )
        return None

    async def _script(self, case: Case, lab: LabSession) -> tuple[Failure | None, str | None]:
        container = await self._container(lab)
        # No env: the script gets the fixed environment only, never the parameters.
        result = await self._runtime.exec(
            container,
            SCRIPT_COMMAND,
            user="student",
            time_limit=SCRIPT_SECONDS,
            stdin=case.script.encode("utf-8"),
        )
        if result.timed_out:
            return Failure(
                FailureCode.SCRIPT_TIMED_OUT, Step.SCRIPT, f"limit {SCRIPT_SECONDS} s"
            ), None
        if result.exit_code != 0:
            return Failure(FailureCode.SCRIPT_FAILED, Step.SCRIPT, f"exit {result.exit_code}"), None
        if not case.mission.requires_answer:
            return None, None
        try:
            return None, answer_from(result)
        except AnswerInvalidError as error:
            return Failure(FailureCode.ANSWER_INVALID, Step.ANSWER, str(error)), None

    async def _final(self, case: Case, lab: LabSession, answer: str | None) -> Failure | None:
        run, failure = await self._validate(lab, answer, Step.VALIDATE)
        if failure is not None:
            return failure
        assert run is not None
        if run.outcome == case.expected:
            return None
        code = (
            FailureCode.SOLUTION_NOT_PASSED
            if case.kind == CaseKind.SOLUTION
            else FailureCode.COUNTEREXAMPLE_NOT_FAILED
        )
        detail = f"expected {case.expected}, got {run.outcome}; {conditions(run.result)}"
        return Failure(code, Step.VALIDATE, detail)

    async def _container(self, lab: LabSession) -> str:
        try:
            return (await self._labs.running_container(lab)).id
        except LabGoneError:
            raise LabRuntimeError("the lab's container is gone") from None

    async def _error_code(self, run: Run) -> str:
        async with self._sessionmaker() as db:
            code = await db.scalar(
                select(ValidationRun.error_code).where(ValidationRun.id == run.id)
            )
        return f"outcome error ({code})"


def _start_failure(error: LabStartError) -> Failure:
    cause = error.__cause__
    if isinstance(cause, PreparationError):
        if cause.stage == PreparationStage.INIT:
            return Failure(
                FailureCode.LABCTL_INIT_FAILED,
                Step.CREATE,
                f"exit {cause.exit_code}, timed out {cause.timed_out}",
            )
        if cause.timed_out:
            return Failure(FailureCode.SETUP_TIMED_OUT, Step.CREATE, "setup timed out")
        return Failure(FailureCode.SETUP_FAILED, Step.CREATE, f"exit {cause.exit_code}")
    name = type(cause).__name__ if cause is not None else "unknown"
    return Failure(FailureCode.LAB_START_FAILED, Step.CREATE, name)


def case_ids(cases: Sequence[Case]) -> list[str]:
    return [f"{case.mission.slug}:{case.name}" for case in cases]
