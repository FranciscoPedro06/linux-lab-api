"""The mission harness itself: how it reports, and what it classifies how.

Unit tests, then tests marked `missions` that run the harness on a FakeRuntime with
PostgreSQL: the production lab and validation services, a fake lab state answering
labctl probe (tests/validation/fake_probe.py), and scripted results for setup, the
scripts and the leak check.
"""

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from linuxlab.auth.models import User
from linuxlab.config import get_settings
from linuxlab.content.loader import load_content
from linuxlab.content.models import Mission
from linuxlab.content.params import generate_params
from linuxlab.db import create_engine
from linuxlab.labs import lifecycle
from linuxlab.labs.lifecycle import LABCTL_INIT, SETUP_COMMAND
from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime import ExecResult, ExecUser
from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.validation.evaluate import Node, Reason, Status
from linuxlab.validation.models import ValidationRun
from linuxlab.validation.probe import PROBE_COMMAND
from linuxlab.validation.router import MAX_ANSWER_BYTES
from tests.content.support import FIXTURE
from tests.validation.fake_probe import Entry, LabState

from .conftest import HARNESS_FIXTURE, Databases, Prepared, active_labs, harness_for
from .harness import (
    CONTENT_CODES,
    LEAK_CHECK_COMMAND,
    SCRIPT_COMMAND,
    SCRIPT_SECONDS,
    AnswerInvalidError,
    Case,
    CaseKind,
    CaseResult,
    Failure,
    FailureClass,
    FailureCode,
    Harness,
    MissionUnderTest,
    Step,
    answer_from,
    case_ids,
    conditions,
    failure_class,
    report,
)

OK = ExecResult(exit_code=0, stdout=b"", stderr=b"")
SAMPLE = "/home/student/sample.sh"

MISSION = MissionUnderTest(
    slug="secure-file",
    version=3,
    content_hash="3fa9c01e2b7d" + "0" * 52,
    spec={"solutions": [{}, {}], "counterexamples": [{}]},
)


# Unit


def test_every_failure_code_has_a_class() -> None:
    assert {code for code in FailureCode if failure_class(code) == FailureClass.CONTENT} == {
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
    assert {code for code in FailureCode if code not in CONTENT_CODES} == {
        FailureCode.RUNTIME_UNAVAILABLE,
        FailureCode.LAB_START_FAILED,
        FailureCode.LABCTL_INIT_FAILED,
        FailureCode.VALIDATION_ERROR,
        FailureCode.LEAK_CHECK_FAILED,
        FailureCode.INFRASTRUCTURE_ERROR,
    }


def test_cases_are_named_by_kind_and_position() -> None:
    cases = MISSION.cases()

    assert case_ids(cases) == [
        "secure-file:solution-1",
        "secure-file:solution-2",
        "secure-file:counterexample-1",
    ]
    assert [case.expected for case in cases] == ["passed", "passed", "failed"]


def test_a_report_names_mission_version_hash_case_step_class_and_code() -> None:
    case = Case(MISSION, CaseKind.COUNTEREXAMPLE, 0)
    failure = Failure(
        FailureCode.COUNTEREXAMPLE_NOT_FAILED, Step.VALIDATE, "expected failed, got passed"
    )

    assert report(CaseResult(case, Step.VALIDATE, failure)) == (
        "secure-file v3 3fa9c01e2b7d counterexample-1 step=validate [content] "
        "counterexample_not_failed: expected failed, got passed"
    )
    assert report(CaseResult(case, Step.VALIDATE)) == (
        "secure-file v3 3fa9c01e2b7d counterexample-1: ok"
    )
    platform = Failure(FailureCode.VALIDATION_ERROR, Step.FRESH, "outcome error (probe_failed)")
    assert "step=fresh [platform] validation_error" in report(
        CaseResult(case, Step.FRESH, platform)
    )


def test_conditions_list_the_leaves_with_status_and_reason() -> None:
    tree = Node(
        kind="all",
        status=Status.FAILED,
        children=(
            Node(kind="check", status=Status.PASSED, id="exists"),
            Node(
                kind="any",
                status=Status.FAILED,
                children=(
                    Node(kind="check", status=Status.FAILED, id="mode", reason=Reason.MISMATCH),
                ),
            ),
        ),
    )

    assert conditions(tree) == "exists=passed mode=failed(mismatch)"
    assert conditions(None) == "no result"


def output(stdout: bytes, *, truncated: bool = False) -> ExecResult:
    return ExecResult(exit_code=0, stdout=stdout, stderr=b"", truncated=truncated)


@pytest.mark.parametrize(
    ("stdout", "answer"),
    [
        (b"otter\n", "otter"),
        (b"otter", "otter"),
        (b"otter\n\n", "otter\n"),
        (b"  otter  \n", "  otter  "),
        (b"otter\r\n", "otter\r"),
        ("ação\n".encode(), "ação"),
        (b"a" * MAX_ANSWER_BYTES + b"\n", "a" * MAX_ANSWER_BYTES),
    ],
)
def test_the_answer_is_the_output_without_its_final_newline(stdout: bytes, answer: str) -> None:
    assert answer_from(output(stdout)) == answer


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (output(b""), "output is empty"),
        (output(b"\n"), "output is empty"),
        (output(b"\xff\xfe\n"), "output is not UTF-8"),
        (output(b"a" * (MAX_ANSWER_BYTES + 1)), f"longer than {MAX_ANSWER_BYTES} bytes"),
        (output("é".encode() * 513), f"longer than {MAX_ANSWER_BYTES} bytes"),
        (output(b"otter\n", truncated=True), "output was truncated"),
    ],
)
def test_an_unusable_answer_is_refused_without_quoting_it(result: ExecResult, message: str) -> None:
    with pytest.raises(AnswerInvalidError) as error:
        answer_from(result)
    assert message in str(error.value)
    assert "otter" not in str(error.value)


# On a FakeRuntime, with PostgreSQL

# A script's effect on the fake lab: given its state and the lab's parameters, change
# the state and return what the script printed and its exit code.
Effect = Callable[[LabState, Mapping[str, str]], ExecResult]


def chmod(mode: int) -> Effect:
    def effect(state: LabState, params: Mapping[str, str]) -> ExecResult:
        state.files[SAMPLE] = Entry(mode=mode, content=state.files[SAMPLE].content)
        return OK

    return effect


def empty_file(state: LabState, params: Mapping[str, str]) -> ExecResult:
    state.files[SAMPLE] = Entry(mode=0o600, content=b"")
    return OK


def nothing(state: LabState, params: Mapping[str, str]) -> ExecResult:
    return OK


def sample_file_setup(mode: int = 0o644) -> Effect:
    def effect(state: LabState, params: Mapping[str, str]) -> ExecResult:
        state.files[SAMPLE] = Entry(mode=mode, content=f"TOKEN={params['token']}\n".encode())
        return OK

    return effect


# The fixture mission sample-file, solved for real.
SAMPLE_FILE: dict[str, Effect] = {
    "solution-1": chmod(0o600),
    "solution-2": chmod(0o600),
    "counterexample-1": empty_file,
}


@dataclass
class FakeLab:
    """Answers the harness's execs on a FakeRuntime, one LabState per container."""

    runtime: FakeRuntime
    mission: MissionUnderTest
    scripts: dict[str, Effect]
    setup: Effect = field(default_factory=sample_file_setup)
    init: ExecResult = OK
    leak: ExecResult = field(default_factory=lambda: output(b"0\n"))
    probe: ExecResult | None = None
    states: dict[str, LabState] = field(default_factory=dict)
    params: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._names = {case.script.encode(): case.name for case in self.mission.cases()}
        self.runtime.exec_handler = self.handle

    def handle(self, argv: Sequence[str], user: ExecUser) -> ExecResult:
        call = self.runtime.exec_calls[-1]
        state = self.states.setdefault(call.container_id, LabState())
        command = tuple(argv)
        if command == LABCTL_INIT:
            return self.init
        if command == LEAK_CHECK_COMMAND:
            return self.leak
        if command == PROBE_COMMAND:
            assert call.stdin is not None
            return self.probe or output(state.answer(json.loads(call.stdin)))
        assert command == SETUP_COMMAND
        if call.container_id not in self.params:
            self.params[call.container_id] = {
                name.removeprefix("LAB_PARAM_").lower(): value for name, value in call.env.items()
            }
            return self.setup(state, self.params[call.container_id])
        assert call.stdin is not None
        return self.scripts[self._names[call.stdin]](state, self.params[call.container_id])


@pytest.fixture
async def fixture_content(databases: Databases) -> Prepared:
    return await databases.prepared(FIXTURE)


@pytest.fixture
async def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
async def harness(fixture_content: Prepared, runtime: FakeRuntime) -> AsyncIterator[Harness]:
    async with harness_for(fixture_content, runtime) as harness:
        yield harness


def sample_file(prepared: Prepared, runtime: FakeRuntime, **lab: Any) -> FakeLab:
    mission = prepared.missions["sample-file"]
    return FakeLab(runtime, mission, lab.pop("scripts", dict(SAMPLE_FILE)), **lab)


async def run_all(harness: Harness, mission: MissionUnderTest) -> list[CaseResult]:
    return [await harness.run(case) for case in mission.cases()]


async def labs_of(prepared: Prepared, results: Sequence[CaseResult]) -> list[LabSession]:
    ids = [result.lab_id for result in results]
    async with prepared.sessionmaker() as db:
        found = {
            lab.id: lab
            for lab in await db.scalars(select(LabSession).where(LabSession.id.in_(ids)))
        }
    return [found[lab_id] for lab_id in ids if lab_id is not None]


def single(results: Sequence[CaseResult]) -> Failure:
    failures = [result.failure for result in results if result.failure is not None]
    assert len(failures) == 1, [report(result) for result in results]
    return failures[0]


@pytest.mark.missions
async def test_every_case_of_a_sound_mission_passes_each_in_its_own_lab(
    harness: Harness, fixture_content: Prepared, runtime: FakeRuntime
) -> None:
    mission = fixture_content.missions["sample-file"]
    sample_file(fixture_content, runtime)

    results = await run_all(harness, mission)

    assert [report(result) for result in results] == [
        f"sample-file v1 {mission.short_hash} {name}: ok"
        for name in ("solution-1", "solution-2", "counterexample-1")
    ]
    labs = await labs_of(fixture_content, results)
    assert len({lab.id for lab in labs}) == len({lab.user_id for lab in labs}) == 3
    assert [(lab.status, lab.end_reason) for lab in labs] == [("terminated", "user")] * 3
    assert all(lab.mission_version == mission.version for lab in labs)
    assert await active_labs(fixture_content) == 0
    async with fixture_content.sessionmaker() as db:
        runs = (
            await db.execute(
                select(ValidationRun.lab_session_id, ValidationRun.outcome).order_by(
                    ValidationRun.id
                )
            )
        ).all()
    # Each lab: validated before the script (failed), then after it.
    assert [(row[0], row[1]) for row in runs if row[0] in {lab.id for lab in labs}] == [
        (labs[0].id, "failed"),
        (labs[0].id, "passed"),
        (labs[1].id, "failed"),
        (labs[1].id, "passed"),
        (labs[2].id, "failed"),
        (labs[2].id, "failed"),
    ]


@pytest.mark.missions
async def test_scripts_run_as_the_student_from_standard_input_without_parameters(
    harness: Harness, fixture_content: Prepared, runtime: FakeRuntime
) -> None:
    mission = fixture_content.missions["sample-file"]
    sample_file(fixture_content, runtime)

    await harness.run(mission.cases()[0])

    calls = [call for call in runtime.exec_calls if call.argv not in (LABCTL_INIT, PROBE_COMMAND)]
    setup, leak, script = calls
    assert setup.env.keys() == {"LAB_PARAM_TOKEN"}
    assert (leak.argv, leak.user, leak.env, leak.stdin) == (LEAK_CHECK_COMMAND, "student", {}, None)
    assert (script.argv, script.user, script.env) == (SCRIPT_COMMAND, "student", {})
    assert script.time_limit == SCRIPT_SECONDS == 30
    assert script.stdin == mission.cases()[0].script.encode()
    # The script was read from the stored version, which holds what the content says.
    assert script.stdin == b"chmod 600 ~/sample.sh\n"


@pytest.mark.missions
async def test_an_answer_mission_submits_what_the_solution_printed(
    harness: Harness, fixture_content: Prepared, runtime: FakeRuntime
) -> None:
    mission = fixture_content.missions["sample-answer"]

    def prints_word(state: LabState, params: Mapping[str, str]) -> ExecResult:
        return output(f"{params['word']}\n".encode())

    FakeLab(
        runtime,
        mission,
        {
            "solution-1": prints_word,
            "solution-2": prints_word,
            "counterexample-1": lambda state, params: output(b"palpite-errado\n"),
        },
        setup=nothing,
    )

    results = await run_all(harness, mission)

    assert all(result.ok for result in results), [report(result) for result in results]
    async with fixture_content.sessionmaker() as db:
        provided = (
            await db.scalars(
                select(ValidationRun.answer_provided)
                .where(ValidationRun.lab_session_id.in_([result.lab_id for result in results]))
                .order_by(ValidationRun.id)
            )
        ).all()
    assert provided == [False, True] * 3


@pytest.mark.missions
@pytest.mark.parametrize(
    ("scripts", "setup", "code", "step", "detail"),
    [
        (
            {**SAMPLE_FILE, "solution-2": nothing},
            sample_file_setup(),
            FailureCode.SOLUTION_NOT_PASSED,
            Step.VALIDATE,
            "expected passed, got failed; exists=passed content-kept=passed mode=failed(mismatch)",
        ),
        (
            {**SAMPLE_FILE, "counterexample-1": chmod(0o600)},
            sample_file_setup(),
            FailureCode.COUNTEREXAMPLE_NOT_FAILED,
            Step.VALIDATE,
            "expected failed, got passed; exists=passed content-kept=passed mode=passed",
        ),
    ],
)
async def test_a_case_that_does_not_reach_its_outcome_is_a_content_failure(
    harness: Harness,
    fixture_content: Prepared,
    runtime: FakeRuntime,
    scripts: dict[str, Effect],
    setup: Effect,
    code: FailureCode,
    step: Step,
    detail: str,
) -> None:
    sample_file(fixture_content, runtime, scripts=scripts, setup=setup)

    failure = single(await run_all(harness, fixture_content.missions["sample-file"]))

    assert (failure.code, failure.step, failure.failure_class) == (code, step, "content")
    assert failure.detail == detail
    assert await active_labs(fixture_content) == 0


@pytest.mark.missions
async def test_a_lab_that_passes_before_any_action_fails_every_case(
    harness: Harness, fixture_content: Prepared, runtime: FakeRuntime
) -> None:
    sample_file(fixture_content, runtime, setup=sample_file_setup(0o600))

    results = await run_all(harness, fixture_content.missions["sample-file"])

    assert {(result.failure.code, result.step) for result in results if result.failure} == {
        (FailureCode.FRESH_NOT_FAILED, Step.FRESH)
    }
    assert all(not result.ok for result in results)
    # The script never ran.
    assert not [call for call in runtime.exec_calls if call.argv == SCRIPT_COMMAND and not call.env]


def exits(code: int, *, timed_out: bool = False, stdout: bytes = b"") -> Effect:
    def effect(state: LabState, params: Mapping[str, str]) -> ExecResult:
        return ExecResult(exit_code=code, stdout=stdout, stderr=b"", timed_out=timed_out)

    return effect


@pytest.mark.missions
@pytest.mark.parametrize(
    ("lab", "code", "step", "detail"),
    [
        ({"setup": exits(3)}, FailureCode.SETUP_FAILED, Step.CREATE, "exit 3"),
        (
            {"setup": exits(-1, timed_out=True)},
            FailureCode.SETUP_TIMED_OUT,
            Step.CREATE,
            "setup timed out",
        ),
        (
            {"scripts": {**SAMPLE_FILE, "solution-1": exits(2)}},
            FailureCode.SCRIPT_FAILED,
            Step.SCRIPT,
            "exit 2",
        ),
        (
            {"scripts": {**SAMPLE_FILE, "solution-1": exits(-1, timed_out=True)}},
            FailureCode.SCRIPT_TIMED_OUT,
            Step.SCRIPT,
            "limit 30 s",
        ),
        (
            {"leak": output(b"2\n")},
            FailureCode.PARAM_LEAK,
            Step.LEAK,
            "2 processes readable by the student carry LAB_PARAM_ variables",
        ),
        (
            {"init": exits(1)(LabState(), {})},
            FailureCode.LABCTL_INIT_FAILED,
            Step.CREATE,
            "exit 1, timed out False",
        ),
        (
            {"leak": output(b"oops\n")},
            FailureCode.LEAK_CHECK_FAILED,
            Step.LEAK,
            "exit 0, timed out False",
        ),
        (
            {"probe": ExecResult(exit_code=1, stdout=b"", stderr=b"")},
            FailureCode.VALIDATION_ERROR,
            Step.FRESH,
            "outcome error (probe_failed)",
        ),
        (
            {"probe": output(b"not json")},
            FailureCode.VALIDATION_ERROR,
            Step.FRESH,
            "outcome error (invalid_probe_output)",
        ),
    ],
)
async def test_failures_are_classified_by_step_and_code(
    harness: Harness,
    fixture_content: Prepared,
    runtime: FakeRuntime,
    lab: dict[str, Any],
    code: FailureCode,
    step: Step,
    detail: str,
) -> None:
    sample_file(fixture_content, runtime, **lab)

    result = await harness.run(fixture_content.missions["sample-file"].cases()[0])

    assert result.failure is not None
    assert (result.failure.code, result.step, result.failure.detail) == (code, step, detail)
    assert result.failure.failure_class == ("content" if code in CONTENT_CODES else "platform")
    assert await active_labs(fixture_content) == 0


@pytest.mark.missions
@pytest.mark.parametrize(
    ("available", "code"),
    [(False, FailureCode.RUNTIME_UNAVAILABLE), (True, FailureCode.LAB_START_FAILED)],
)
async def test_a_runtime_that_cannot_start_the_lab_is_a_platform_failure(
    harness: Harness,
    fixture_content: Prepared,
    runtime: FakeRuntime,
    available: bool,
    code: FailureCode,
) -> None:
    sample_file(fixture_content, runtime)
    runtime.fail.add("start")
    runtime.available = available

    result = await harness.run(fixture_content.missions["sample-file"].cases()[0])

    assert result.failure is not None
    assert (result.failure.code, result.failure.failure_class) == (code, "platform")
    assert result.failure.detail == "LabRuntimeError"
    runtime.available = True


@pytest.mark.missions
@pytest.mark.parametrize(
    "stdout",
    [b"", b"\xff\n", b"x" * (MAX_ANSWER_BYTES + 1)],
)
async def test_an_unusable_answer_is_a_content_failure_and_is_not_submitted(
    harness: Harness, fixture_content: Prepared, runtime: FakeRuntime, stdout: bytes
) -> None:
    mission = fixture_content.missions["sample-answer"]
    FakeLab(runtime, mission, {"solution-1": exits(0, stdout=stdout)}, setup=nothing)

    result = await harness.run(mission.cases()[0])

    assert result.failure is not None
    assert (result.failure.code, result.step) == (FailureCode.ANSWER_INVALID, Step.ANSWER)
    async with fixture_content.sessionmaker() as db:
        runs = (
            await db.scalars(
                select(ValidationRun.id).where(ValidationRun.lab_session_id == result.lab_id)
            )
        ).all()
    assert len(runs) == 1  # only the validation before the script


@pytest.mark.missions
async def test_reports_carry_no_parameter_and_no_output(
    harness: Harness, fixture_content: Prepared, runtime: FakeRuntime
) -> None:
    mission = fixture_content.missions["sample-answer"]
    printed = b"wrong-answer-with-a-marker\n"

    def wrong(state: LabState, params: Mapping[str, str]) -> ExecResult:
        return ExecResult(
            exit_code=0, stdout=printed + params["word"].encode(), stderr=b"err-marker"
        )

    def failing(state: LabState, params: Mapping[str, str]) -> ExecResult:
        return ExecResult(
            exit_code=5, stdout=params["word"].encode(), stderr=params["word"].encode()
        )

    lab = FakeLab(runtime, mission, {"solution-1": wrong, "solution-2": failing}, setup=nothing)

    results = [await harness.run(case) for case in mission.cases()[:2]]

    lines = [report(result) for result in results]
    assert [result.failure.code for result in results if result.failure] == [
        FailureCode.SOLUTION_NOT_PASSED,
        FailureCode.SCRIPT_FAILED,
    ]
    words = [params["word"] for params in lab.params.values()]
    for line in lines:
        assert "marker" not in line
        for word in words:
            assert word not in line


@pytest.mark.missions
async def test_parameters_are_drawn_again_for_each_lab(
    harness: Harness,
    fixture_content: Prepared,
    runtime: FakeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sequential:
        """Draws 0, 1, 2, ...: two labs cannot coincide by chance."""

        def __init__(self) -> None:
            self.next = 0

        def choice[T](self, items: Sequence[T]) -> T:
            self.next += 1
            return items[(self.next - 1) % len(items)]

        def randbelow(self, bound: int) -> int:
            self.next += 1
            return (self.next - 1) % bound

    randomness = Sequential()
    monkeypatch.setattr(
        lifecycle, "generate_params", lambda declared: generate_params(declared, randomness)
    )
    lab = sample_file(fixture_content, runtime)
    mission = fixture_content.missions["sample-file"]

    results = [await harness.run(case) for case in mission.cases()[:2]]

    assert all(result.ok for result in results)
    stored = [lab_row.params for lab_row in await labs_of(fixture_content, results)]
    assert stored == [{"token": "01234567"}, {"token": "89abcdef"}]
    # Each lab's setup received that lab's own values.
    assert list(lab.params.values()) == stored


@pytest.mark.missions
async def test_drafts_are_tested_at_the_version_the_sync_stores(databases: Databases) -> None:
    prepared = await databases.prepared(HARNESS_FIXTURE)
    content = load_content(HARNESS_FIXTURE)
    loaded = {mission.slug: mission for mission in content.missions}

    assert loaded["harness-draft"].status == "draft"
    assert content.modules[0].status == "draft"
    for slug, mission in prepared.missions.items():
        assert (mission.version, mission.content_hash) == (1, loaded[slug].content_hash)
    case = prepared.missions["harness-draft"].cases()[0]
    assert case.script == "mkdir ~/backup\ncp ~/notas.txt ~/backup/\n"

    # The harness writes only to its own database.
    engine = create_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            listed = await connection.execute(
                select(Mission.slug).where(Mission.slug == "harness-draft")
            )
            assert listed.all() == []
    finally:
        await engine.dispose()


@pytest.mark.missions
async def test_the_stored_version_cannot_be_changed(fixture_content: Prepared) -> None:
    with pytest.raises(DBAPIError):
        async with fixture_content.sessionmaker() as db:
            await db.execute(text("UPDATE mission_versions SET spec = '{}'::jsonb"))
            await db.commit()


@pytest.mark.missions
async def test_each_case_has_its_own_user(
    fixture_content: Prepared, harness: Harness, runtime: FakeRuntime
) -> None:
    sample_file(fixture_content, runtime)
    async with fixture_content.sessionmaker() as db:
        before = len((await db.scalars(select(User.id))).all())

    await run_all(harness, fixture_content.missions["sample-file"])

    async with fixture_content.sessionmaker() as db:
        assert len((await db.scalars(select(User.id))).all()) == before + 3
