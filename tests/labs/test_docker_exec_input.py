"""Exec with standard input and parameters, in real containers.

Runs under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc locally, runsc in the
Runtime workflow).
"""

import hashlib

import pytest

from linuxlab.labs.runtime import ContainerInfo
from linuxlab.labs.runtime.docker import MAX_OUTPUT_BYTES, DockerRuntime

pytestmark = pytest.mark.docker


# Standard input


async def test_stdin_is_delivered_and_closed(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    # cat only exits once its input is closed; without the end of file it would run
    # into the time limit.
    result = await runtime.exec(
        lab.id, ["cat"], user="student", time_limit=10, stdin=b"first\nsecond\n"
    )

    assert result.exit_code == 0
    assert not result.timed_out
    assert result.stdout == b"first\nsecond\n"


async def test_empty_stdin_is_closed_at_once(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await runtime.exec(lab.id, ["wc", "-c"], user="student", time_limit=10, stdin=b"")

    assert (result.exit_code, result.timed_out, result.stdout) == (0, False, b"0\n")


async def test_without_stdin_the_command_reads_nothing(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    result = await runtime.exec(lab.id, ["wc", "-c"], user="student", time_limit=10)

    assert (result.exit_code, result.timed_out, result.stdout) == (0, False, b"0\n")


async def test_large_input_and_output_do_not_deadlock(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    """The command fills its output pipes before it reads any input."""
    size = 2 * 1024 * 1024
    data = bytes(range(256)) * (size // 256)
    script = "head -c 3000000 /dev/zero | tr '\\0' x >&2; sha256sum | cut -c1-64"

    result = await runtime.exec(
        lab.id, ["sh", "-c", script], user="student", time_limit=30, stdin=data
    )

    assert result.exit_code == 0, result.stdout
    assert not result.timed_out
    assert result.stdout.splitlines()[0] == hashlib.sha256(data).hexdigest().encode()
    assert len(result.stderr) == MAX_OUTPUT_BYTES
    assert result.truncated


async def test_bash_runs_a_script_read_from_stdin(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    script = b'set -- a b c\nprintf "%s;" "$@"\necho "$0"\n'

    result = await runtime.exec(
        lab.id, ["bash", "-euo", "pipefail"], user="student", time_limit=10, stdin=script
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == b"a;b;c;bash\n"


async def test_a_command_that_stops_reading_early_is_not_an_error(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    result = await runtime.exec(
        lab.id, ["head", "-c", "3"], user="student", time_limit=10, stdin=b"x" * 100_000
    )

    assert (result.exit_code, result.stdout) == (0, b"xxx")


async def test_stdin_command_still_has_a_time_limit(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    result = await runtime.exec(
        lab.id, ["sh", "-c", "cat >/dev/null; sleep 30"], user="student", time_limit=2, stdin=b"x"
    )

    assert result.timed_out


# Environment


async def test_parameters_are_added_to_a_fixed_environment(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    result = await runtime.exec(
        lab.id,
        ["env"],
        user="student",
        time_limit=5,
        env={"LAB_PARAM_TOKEN": "c0ffee", "LAB_PARAM_WORD": "otter"},
    )

    variables = dict(line.split("=", 1) for line in result.stdout.decode().splitlines())
    assert variables["LAB_PARAM_TOKEN"] == "c0ffee"
    assert variables["LAB_PARAM_WORD"] == "otter"
    assert variables["LANG"] == "C.UTF-8"
    assert variables["PATH"].startswith("/usr/local/sbin:")
    # Docker adds HOSTNAME and HOME; nothing comes from the API's own environment.
    assert set(variables) <= {
        "PATH",
        "LANG",
        "HOSTNAME",
        "HOME",
        "LAB_PARAM_TOKEN",
        "LAB_PARAM_WORD",
    }


@pytest.mark.parametrize(
    "env",
    [
        {"PATH": "/tmp"},
        {"LD_PRELOAD": "/tmp/x.so"},
        {"LAB_PARAM_token": "a"},
        {"LAB_PARAM_TOKEN": "a b"},
        {"LAB_PARAM_TOKEN": "a\nB=c"},
        {"LAB_PARAM_TOKEN": ""},
    ],
)
async def test_other_variables_are_refused(
    runtime: DockerRuntime, lab: ContainerInfo, env: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        await runtime.exec(lab.id, ["true"], user="student", time_limit=5, env=env)
