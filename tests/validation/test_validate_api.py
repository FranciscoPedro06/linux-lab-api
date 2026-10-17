"""POST /api/labs/{id}/validate against PostgreSQL, with labs on a FakeRuntime.

The probe is answered by the stand-in in fake_probe.py; Docker tests
(test_validation_docker.py) run the real labctl probe.
"""

import asyncio
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.exc import DBAPIError

from linuxlab.content.loader import load_content
from linuxlab.content.sync import sync_content
from linuxlab.labs.runtime import ContainerNotRunningError, ExecResult, ExecUser
from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.validation.probe import PROBE_COMMAND
from linuxlab.validation.router import MAX_ANSWER_BYTES, VALIDATE_ATTEMPTS
from linuxlab.validation.service import Validations
from tests.auth.support import request, run, sql
from tests.content.support import copy_fixture, edit_yaml, mission_file
from tests.labs.lab_support import (
    container,
    create_lab,
    end_lab,
    fake_runtime,
    hold,
    in_background,
    lab_row,
    ready_lab,
    run_async,
    user,
    wait_for_call,
)

from .fake_probe import Entry, LabState, install

pytestmark = pytest.mark.integration

SCRIPT = "/home/student/sample.sh"
LAB_STATE = "SELECT status, mission_id, mission_version, params, last_activity_at FROM lab_sessions"


def validate(client: TestClient, token: str | None, lab_id: str, **body: Any) -> Response:
    return request(client, "POST", f"/api/labs/{lab_id}/validate", token=token, json=body)


def lab_params(client: TestClient, lab_id: str) -> dict[str, str]:
    rows = sql(client, "SELECT params FROM lab_sessions WHERE id = :id", id=lab_id)
    params: dict[str, str] = rows[0][0]
    return params


def runs(client: TestClient) -> list[Any]:
    return sql(
        client,
        "SELECT id, lab_session_id, mission_id, mission_version, outcome, error_code, result,"
        " answer_provided, started_at, finished_at FROM validation_runs ORDER BY id",
    )


def solved_state(token: str) -> LabState:
    return LabState({SCRIPT: Entry(mode=0o600, content=f"#!/bin/sh\nTOKEN={token}\n".encode())})


def probe_calls(client: TestClient) -> list[Any]:
    return [call for call in fake_runtime(client).exec_calls if call.argv == PROBE_COMMAND]


@pytest.fixture
def setup(client: TestClient) -> tuple[TestClient, str, dict[str, Any]]:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    return client, token, lab


def test_a_solved_lab_passes_and_the_run_is_stored(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    params = lab_params(client, lab["id"])
    install(fake_runtime(client), solved_state(params["token"]))

    response = validate(client, token, lab["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "passed"
    assert body["mission"] == {"slug": "sample-file", "version": 1}
    assert body["result"] == {
        "kind": "all",
        "status": "passed",
        "message": None,
        "children": [
            {"kind": "check", "id": "exists", "status": "passed", "message": "Condição atendida."},
            {
                "kind": "check",
                "id": "content-kept",
                "status": "passed",
                "message": "Condição atendida.",
            },
            {"kind": "check", "id": "mode", "status": "passed", "message": "Condição atendida."},
        ],
    }
    (row,) = runs(client)
    run_id, lab_id, _, version, outcome, error_code, result, answered, started, finished = row
    assert body["run_id"] == run_id
    assert (str(lab_id), version, outcome, error_code, answered) == (
        lab["id"],
        1,
        "passed",
        None,
        False,
    )
    assert result["status"] == "passed"
    assert [child["reason"] for child in result["children"]] == [None, None, None]
    assert finished >= started


def test_an_unsolved_lab_fails_with_messages_and_reasons(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    params = lab_params(client, lab["id"])
    state = LabState({SCRIPT: Entry(mode=0o644, content=f"TOKEN={params['token']}".encode())})
    install(fake_runtime(client), state)

    body = validate(client, token, lab["id"]).json()

    assert body["outcome"] == "failed"
    statuses = [
        (child["id"], child["status"], child["message"]) for child in body["result"]["children"]
    ]
    assert statuses == [
        ("exists", "passed", "Condição atendida."),
        ("content-kept", "passed", "Condição atendida."),
        ("mode", "failed", "Condição ainda não atendida."),
    ]
    (row,) = runs(client)
    assert row[4] == "failed" and row[5] is None
    assert [child["reason"] for child in row[6]["children"]] == [None, None, "mismatch"]

    install(fake_runtime(client), LabState())
    body = validate(client, token, lab["id"]).json()
    assert [child["status"] for child in body["result"]["children"]] == ["failed"] * 3
    assert body["result"]["children"][0]["message"] == "O arquivo não existe."
    assert [child["reason"] for child in runs(client)[1][6]["children"]] == ["not_found"] * 3


def test_nothing_secret_is_returned_or_stored(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    params = lab_params(client, lab["id"])
    state = solved_state(params["token"])
    state.files[SCRIPT].content += b"student-content-marker\n"
    install(fake_runtime(client), state)

    response = validate(client, token, lab["id"], answer="literal-answer-marker")

    stored = json.dumps([list(map(str, row)) for row in runs(client)])
    for text in (response.text, stored):
        assert params["token"] not in text
        assert "student-content-marker" not in text
        assert "literal-answer-marker" not in text
        assert "TOKEN=" not in text
    assert runs(client)[0][7] is True  # only whether an answer was sent


def test_the_probe_request_comes_only_from_the_pinned_version(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    requests = install(fake_runtime(client), LabState())

    validate(client, token, lab["id"])

    assert requests == [
        {
            "v": 1,
            "facts": [
                {"op": "stat", "path": SCRIPT, "follow": False},
                {"op": "read", "path": SCRIPT},
            ],
        }
    ]
    (call,) = probe_calls(client)
    assert call.user == "root" and call.env == {}


@pytest.mark.parametrize(
    "body",
    [
        {"path": "/etc/shadow"},
        {"version": 2},
        {"regex": ".*"},
        {"command": "id"},
        {"answer": "x", "mission_version": 1},
        {"answer": 42},
        {"answer": ["a"]},
        {"answer": "a" * (MAX_ANSWER_BYTES + 1)},
        {"answer": "é" * (MAX_ANSWER_BYTES // 2 + 1)},
    ],
    ids=["path", "version", "regex", "command", "extra", "int", "list", "long", "long-utf8"],
)
def test_the_body_holds_at_most_an_answer(
    setup: tuple[TestClient, str, dict[str, Any]], body: dict[str, Any]
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    response = validate(client, token, lab["id"], **body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert runs(client) == []
    assert probe_calls(client) == []


def test_an_answer_of_exactly_the_limit_is_accepted(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    assert (
        validate(client, token, lab["id"], answer="é" * (MAX_ANSWER_BYTES // 2)).status_code == 200
    )


def test_authentication_and_ownership(setup: tuple[TestClient, str, dict[str, Any]]) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    other = user(client, "bia@example.com")

    assert validate(client, None, lab["id"]).status_code == 401
    for token_used, lab_id in (
        (other, lab["id"]),
        (token, str(uuid.uuid4())),
        (token, "not-a-uuid"),
        (token, lab["id"].upper()),
    ):
        response = validate(client, token_used, lab_id)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "lab_not_found"
    assert runs(client) == []
    assert probe_calls(client) == []


def test_origin_and_content_type_are_required(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    path = f"/api/labs/{lab['id']}/validate"
    assert (
        request(
            client, "POST", path, token=token, json={}, headers={"origin": "https://evil"}
        ).status_code
        == 403
    )
    assert request(client, "POST", path, token=token, content=b"{}").status_code == 415


def test_a_lab_that_is_not_ready_is_refused(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    assert end_lab(client, token, lab["id"]).status_code == 200

    response = validate(client, token, lab["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_ready"
    assert runs(client) == []


def test_a_lab_whose_container_is_gone_is_ended_and_refused(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    info = container(client, lab["id"])
    assert info is not None
    run_async(client, lambda: fake_runtime(client).remove(info.id))

    response = validate(client, token, lab["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_ready"
    assert lab_row(client, lab["id"]) == ("terminated", "container_lost")
    assert runs(client) == []


def failing(runtime: FakeRuntime, error: Exception | None = None, **result: Any) -> None:
    def handler(argv: Sequence[str], user: ExecUser) -> ExecResult:
        if error is not None:
            raise error
        values: dict[str, Any] = {"exit_code": 0, "stdout": b"", "stderr": b""}
        values.update(result)
        return ExecResult(**values)

    runtime.exec_handler = handler


@pytest.mark.parametrize(
    ("error", "result", "code"),
    [
        (None, {"exit_code": 1, "stderr": b"labctl: probe: invalid path"}, "probe_failed"),
        (None, {"exit_code": 124, "timed_out": True}, "probe_timeout"),
        (None, {"stdout": b"not json"}, "invalid_probe_output"),
        (None, {"stdout": b"{}", "truncated": True}, "invalid_probe_output"),
        (ContainerNotRunningError("gone"), {}, "container_gone"),
    ],
    ids=["exit", "timeout", "json", "truncated", "gone"],
)
def test_operational_errors_are_stored_runs_distinct_from_failures(
    setup: tuple[TestClient, str, dict[str, Any]],
    error: Exception | None,
    result: dict[str, Any],
    code: str,
) -> None:
    client, token, lab = setup
    failing(fake_runtime(client), error, **result)

    response = validate(client, token, lab["id"])

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "error"
    assert [child["status"] for child in body["result"]["children"]] == ["error"] * 3
    assert {child["message"] for child in body["result"]["children"]} == {
        "Não foi possível verificar esta condição."
    }
    (row,) = runs(client)
    assert (row[4], row[5]) == ("error", code)
    assert lab_row(client, lab["id"]) == ("ready", None)


def test_answer_missions_need_no_probe(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    response = create_lab(client, token, "sample-answer")
    assert response.status_code == 201, response.text
    lab = response.json()
    word = lab_params(client, lab["id"])["word"]
    runtime = fake_runtime(client)
    install(runtime, LabState())

    missing = validate(client, token, lab["id"]).json()
    wrong = validate(client, token, lab["id"], answer="definitely-not-it").json()
    right = validate(client, token, lab["id"], answer=f"  {word.upper()} ").json()

    assert missing["outcome"] == "failed"
    assert missing["result"]["message"] == "Envie uma resposta para verificar esta condição."
    assert wrong["outcome"] == "failed"
    assert wrong["result"]["message"] == "Essa não é a palavra."
    assert right["outcome"] == "passed"
    assert probe_calls(client) == []
    assert [(row[4], row[7]) for row in runs(client)] == [
        ("failed", False),
        ("failed", True),
        ("passed", True),
    ]
    assert [row[6]["reason"] for row in runs(client)] == ["no_answer", "mismatch", None]


def test_validation_uses_the_pinned_version_after_a_new_one_is_published(
    client: TestClient, tmp_path: Path
) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    params = lab_params(client, lab["id"])
    root = copy_fixture(tmp_path)

    def stricter(data: dict[str, Any]) -> None:
        data["validation"]["all"].append(
            {"id": "extra", "type": "path_absent", "path": SCRIPT, "fail_message": "Remova."}
        )

    edit_yaml(mission_file(root, "sample-file"), stricter)
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]
    run(client, lambda: sync_content(sessionmaker, load_content(root)))
    assert sql(client, "SELECT current_version FROM missions WHERE slug = 'sample-file'") == [(2,)]
    install(fake_runtime(client), solved_state(params["token"]))

    body = validate(client, token, lab["id"]).json()

    assert body["outcome"] == "passed"
    assert body["mission"]["version"] == 1
    assert [child["id"] for child in body["result"]["children"]] == [
        "exists",
        "content-kept",
        "mode",
    ]
    assert runs(client)[0][3] == 1


def test_a_stored_version_that_cannot_be_read_is_an_invalid_spec_error(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    # Versions are immutable; the trigger is lifted only to simulate a corrupt row.
    sql(client, "ALTER TABLE mission_versions DISABLE TRIGGER mission_versions_immutable")
    try:
        sql(
            client,
            "UPDATE mission_versions SET spec = jsonb_set(spec, '{validation}',"
            ' \'{"id": "x", "type": "run_command"}\')',
        )
    finally:
        sql(client, "ALTER TABLE mission_versions ENABLE TRIGGER mission_versions_immutable")

    response = validate(client, token, lab["id"])

    assert response.status_code == 200
    assert response.json()["outcome"] == "error"
    assert response.json()["result"] is None
    (row,) = runs(client)
    assert (row[4], row[5], row[6]) == ("error", "invalid_spec", None)
    assert probe_calls(client) == []


def test_validation_changes_nothing_in_the_lab(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    before = sql(client, LAB_STATE)
    runtime = fake_runtime(client)
    calls_before = len(runtime.exec_calls)
    install(runtime, LabState())

    validate(client, token, lab["id"])
    validate(client, token, lab["id"])

    assert sql(client, LAB_STATE) == before
    # Two probes and nothing else: setup and labctl init do not run again.
    assert [call.argv for call in runtime.exec_calls[calls_before:]] == [PROBE_COMMAND] * 2


def test_repeated_validations_are_separate_runs(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    ids = [validate(client, token, lab["id"]).json()["run_id"] for _ in range(3)]
    assert len(set(ids)) == 3
    assert [row[0] for row in runs(client)] == ids


def test_a_concurrent_validation_of_the_same_lab_is_refused(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    release = hold(client, "exec")
    first = in_background(lambda: validate(client, token, lab["id"]))
    wait_for_call(client, "exec")

    second = validate(client, token, lab["id"])

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "validation_in_progress"
    release()
    assert first.result(timeout=10).status_code == 200
    assert len(runs(client)) == 1
    # Released: the next one runs.
    assert validate(client, token, lab["id"]).status_code == 200


def test_validations_of_different_labs_do_not_block_each_other(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    ana_lab = ready_lab(client, ana)
    bia_lab = ready_lab(client, bia)
    install(fake_runtime(client), LabState())
    validations: Validations = client.app.state.validations  # type: ignore[attr-defined]

    async def both() -> list[str]:
        results = await asyncio.gather(
            validations.validate(uuid.UUID(ana_lab["id"]), None),
            validations.validate(uuid.UUID(bia_lab["id"]), None),
        )
        return [result.outcome for result in results]

    assert run_async(client, both) == ["failed", "failed"]
    assert {str(row[1]) for row in runs(client)} == {ana_lab["id"], bia_lab["id"]}


def test_a_cancelled_validation_stores_nothing_and_releases_the_lab(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    validations: Validations = client.app.state.validations  # type: ignore[attr-defined]
    release = hold(client, "exec")

    async def cancel_midway() -> bool:
        task = asyncio.create_task(validations.validate(uuid.UUID(lab["id"]), None))
        while not any(name == "exec" for name, _ in fake_runtime(client).calls):
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert run_async(client, cancel_midway) is True
    release()
    assert runs(client) == []
    assert validate(client, token, lab["id"]).status_code == 200


def test_validations_are_rate_limited_per_user(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    for _ in range(VALIDATE_ATTEMPTS):
        assert validate(client, token, lab["id"]).status_code == 200

    response = validate(client, token, lab["id"])

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert int(response.headers["retry-after"]) >= 1
    assert len(runs(client)) == VALIDATE_ATTEMPTS
    # Another user has their own budget.
    other = user(client, "bia@example.com")
    assert validate(client, other, lab["id"]).status_code == 404


def test_runs_are_append_only_and_tied_to_the_labs_version(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())
    validate(client, token, lab["id"])
    run_id, lab_id, mission_id = runs(client)[0][:3]

    with pytest.raises(DBAPIError, match="validation runs cannot be changed"):
        sql(client, "UPDATE validation_runs SET outcome = 'passed' WHERE id = :id", id=run_id)
    # Another version of the same mission is not the lab's pinned version.
    with pytest.raises(DBAPIError, match="fk_validation_runs_lab_session_id_lab_sessions"):
        sql(
            client,
            "INSERT INTO validation_runs (lab_session_id, mission_id, mission_version, outcome,"
            " result, answer_provided, started_at, finished_at) VALUES (:lab, :mission, 2,"
            " 'passed', '{}', false, now(), now())",
            lab=lab_id,
            mission=mission_id,
        )
    for values in (
        "'error', NULL, '{}'",  # error without a code
        "'failed', 'probe_failed', '{}'",  # a code without an error
        "'error', 'probe_failed', NULL",  # no result for an operational error
        "'error', 'nonsense', '{}'",
        "'maybe', NULL, '{}'",
    ):
        with pytest.raises(DBAPIError):
            sql(
                client,
                "INSERT INTO validation_runs (lab_session_id, mission_id, mission_version,"
                f" outcome, error_code, result, answer_provided, started_at, finished_at)"
                f" VALUES (:lab, :mission, 1, {values}, false, now(), now())",
                lab=lab_id,
                mission=mission_id,
            )
    # Deleting the user cascades to their labs and runs.
    sql(client, "DELETE FROM users")
    assert runs(client) == []
