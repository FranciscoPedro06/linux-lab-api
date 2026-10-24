"""Mission cases in real labs, under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc in
CI, runsc in the Runtime workflow), with PostgreSQL.

- `content`: every case of every mission in the content directory under test.
- Synthetic missions: those of tests/fixtures/content pass; those of
  tests/fixtures/harness fail each in the way they were written to, and the draft
  among them passes.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import aiodocker
import pytest

from linuxlab.labs.runtime.docker import DockerRuntime
from tests.content.support import FIXTURE
from tests.labs.support import OCI_RUNTIME

from .conftest import (
    HARNESS_FIXTURE,
    ContentDirError,
    Databases,
    Prepared,
    active_labs,
    content_case_ids,
    content_dir,
    harness_for,
)
from .harness import FailureCode, Harness, Step, report

pytestmark = pytest.mark.mission_labs


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "content_case" in metafunc.fixturenames:
        try:
            ids = content_case_ids(content_dir())
        except ContentDirError:
            ids = []
        metafunc.parametrize("content_case", ids, ids=ids)


@pytest.fixture
async def runtime(docker_client: aiodocker.Docker) -> DockerRuntime:
    runtime = DockerRuntime(docker_client, oci_runtime=OCI_RUNTIME)
    await runtime.ping()
    return runtime


async def harness_on(
    databases: Databases, root: Path, runtime: DockerRuntime
) -> AsyncIterator[tuple[Prepared, Harness]]:
    prepared = await databases.prepared(root)
    async with harness_for(prepared, runtime) as harness:
        yield prepared, harness
    assert await active_labs(prepared) == 0


@pytest.fixture
async def content(
    databases: Databases, runtime: DockerRuntime
) -> AsyncIterator[tuple[Prepared, Harness]]:
    async for pair in harness_on(databases, content_dir(), runtime):
        yield pair


@pytest.fixture
async def fixtures(
    databases: Databases, runtime: DockerRuntime
) -> AsyncIterator[tuple[Prepared, Harness]]:
    async for pair in harness_on(databases, FIXTURE, runtime):
        yield pair


@pytest.fixture
async def broken(
    databases: Databases, runtime: DockerRuntime
) -> AsyncIterator[tuple[Prepared, Harness]]:
    async for pair in harness_on(databases, HARNESS_FIXTURE, runtime):
        yield pair


@pytest.mark.content
def test_the_content_directory_has_cases() -> None:
    # Without it, a content directory with no missions would only skip test_mission_case.
    try:
        root = content_dir()
    except ContentDirError as error:
        pytest.fail(str(error), pytrace=False)
    if not content_case_ids(root):
        pytest.fail(f"{root} has no mission cases to run", pytrace=False)


@pytest.mark.content
async def test_mission_case(content: tuple[Prepared, Harness], content_case: str) -> None:
    prepared, harness = content
    result = await harness.run(prepared.case(content_case))
    if not result.ok:
        # pytrace=False: no traceback or assertion details, only the sanitized report.
        pytest.fail(report(result), pytrace=False)


@pytest.mark.parametrize(
    "case",
    [
        "sample-file:solution-1",
        "sample-file:solution-2",
        "sample-file:counterexample-1",
        "sample-answer:solution-1",
        "sample-answer:solution-2",
        "sample-answer:counterexample-1",
        "sample-draft:solution-1",
        "sample-draft:solution-2",
        "sample-draft:counterexample-1",
    ],
)
async def test_the_synthetic_missions_pass(fixtures: tuple[Prepared, Harness], case: str) -> None:
    prepared, harness = fixtures
    result = await harness.run(prepared.case(case))
    assert result.ok, report(result)


@pytest.mark.parametrize(
    ("case", "code", "step"),
    [
        ("harness-draft:solution-1", None, Step.VALIDATE),
        ("harness-draft:solution-2", None, Step.VALIDATE),
        ("harness-draft:counterexample-1", None, Step.VALIDATE),
        ("leaky-setup:solution-1", FailureCode.PARAM_LEAK, Step.LEAK),
        ("weak-validation:solution-1", None, Step.VALIDATE),
        ("weak-validation:solution-2", FailureCode.SOLUTION_NOT_PASSED, Step.VALIDATE),
        ("weak-validation:counterexample-1", FailureCode.COUNTEREXAMPLE_NOT_FAILED, Step.VALIDATE),
        ("solved-at-start:solution-1", FailureCode.FRESH_NOT_FAILED, Step.FRESH),
        ("failing-setup:solution-1", FailureCode.SETUP_FAILED, Step.CREATE),
    ],
)
async def test_the_harness_finds_what_each_broken_mission_gets_wrong(
    broken: tuple[Prepared, Harness], case: str, code: FailureCode | None, step: Step
) -> None:
    prepared, harness = broken
    result = await harness.run(prepared.case(case))

    assert (result.failure.code if result.failure else None, result.step) == (code, step), report(
        result
    )
