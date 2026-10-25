"""command_history in PostgreSQL: constraints, seq assignment, limits and pages."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, IntegrityError

from linuxlab.history import service
from linuxlab.history.service import CommandHistory, LabHistoryFullError, read_page
from tests.auth.support import run, sql
from tests.labs.test_lab_schema import add_lab_in, add_user

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def store(client: TestClient) -> CommandHistory:
    return CommandHistory(client.app.state.sessionmaker)  # type: ignore[attr-defined]


def record(client: TestClient, lab_id: uuid.UUID, command: str) -> int:
    return run(client, lambda: store(client).record(lab_id, command, AT))


def rows(client: TestClient, lab_id: uuid.UUID) -> list[tuple[int, str]]:
    return [
        (seq, command)
        for seq, command in sql(
            client,
            "SELECT seq, command FROM command_history WHERE lab_session_id = :lab ORDER BY seq",
            lab=lab_id,
        )
    ]


def insert(client: TestClient, lab_id: uuid.UUID, seq: int, command: str) -> None:
    sql(
        client,
        "INSERT INTO command_history (lab_session_id, seq, command, recorded_at)"
        " VALUES (:lab, :seq, :command, now())",
        lab=lab_id,
        seq=seq,
        command=command,
    )


@pytest.fixture
def lab(client: TestClient) -> uuid.UUID:
    return add_lab_in(client, add_user(client), "ready")  # type: ignore[arg-type]


# Constraints


def test_entries_need_an_existing_lab(client: TestClient) -> None:
    with pytest.raises(IntegrityError, match="fk_command_history_lab_session_id_lab_sessions"):
        insert(client, uuid.uuid4(), 1, "ls")


def test_seq_is_unique_per_lab_and_positive(client: TestClient, lab: uuid.UUID) -> None:
    insert(client, lab, 1, "ls")
    with pytest.raises(IntegrityError, match="pk_command_history"):
        insert(client, lab, 1, "pwd")
    with pytest.raises(IntegrityError, match="ck_command_history_seq_positive"):
        insert(client, lab, 0, "pwd")


@pytest.mark.parametrize("command", ["", "a" * 4097, "é" * 2049])
def test_command_length_is_bounded_in_bytes(
    client: TestClient, lab: uuid.UUID, command: str
) -> None:
    with pytest.raises(IntegrityError, match="ck_command_history_command_length"):
        insert(client, lab, 1, command)


def test_command_of_exactly_the_limit_is_accepted(client: TestClient, lab: uuid.UUID) -> None:
    insert(client, lab, 1, "é" * 2048)
    assert rows(client, lab) == [(1, "é" * 2048)]


@pytest.mark.parametrize("character", ["\x01", "\x07", "\x1b", "\r", "\x7f", "\x9b"])
def test_control_characters_are_refused(client: TestClient, lab: uuid.UUID, character: str) -> None:
    with pytest.raises(IntegrityError, match="ck_command_history_command_characters"):
        insert(client, lab, 1, f"echo {character}")


def test_tabs_and_newlines_are_accepted(client: TestClient, lab: uuid.UUID) -> None:
    insert(client, lab, 1, "cat <<EOF\n\tbody\nEOF")
    assert rows(client, lab) == [(1, "cat <<EOF\n\tbody\nEOF")]


def test_entries_cannot_change(client: TestClient, lab: uuid.UUID) -> None:
    insert(client, lab, 1, "ls")
    with pytest.raises(DBAPIError, match="command history cannot be changed"):
        sql(client, "UPDATE command_history SET command = 'pwd'")
    assert rows(client, lab) == [(1, "ls")]


def test_removing_a_lab_or_its_user_removes_its_history(client: TestClient) -> None:
    user_id = add_user(client)
    first = add_lab_in(client, user_id, "terminated")  # type: ignore[arg-type]
    second = add_lab_in(client, user_id, "ready")  # type: ignore[arg-type]
    insert(client, first, 1, "ls")
    insert(client, second, 1, "pwd")

    sql(client, "DELETE FROM lab_sessions WHERE id = :id", id=first)
    assert sql(client, "SELECT lab_session_id FROM command_history") == [(second,)]
    sql(client, "DELETE FROM users WHERE id = :id", id=user_id)
    assert sql(client, "SELECT count(*) FROM command_history") == [(0,)]


# Recording


def test_seq_starts_at_one_and_grows_per_lab(client: TestClient) -> None:
    user_id = add_user(client)
    first = add_lab_in(client, user_id, "terminated")  # type: ignore[arg-type]
    second = add_lab_in(client, user_id, "ready")  # type: ignore[arg-type]

    assert [record(client, first, "ls"), record(client, first, "pwd")] == [1, 2]
    assert record(client, second, "ls") == 1
    assert rows(client, first) == [(1, "ls"), (2, "pwd")]
    assert rows(client, second) == [(1, "ls")]


def test_a_repeated_command_is_a_new_entry(client: TestClient, lab: uuid.UUID) -> None:
    record(client, lab, "ls")
    record(client, lab, "ls")
    assert rows(client, lab) == [(1, "ls"), (2, "ls")]


def test_recorded_at_is_the_time_given(client: TestClient, lab: uuid.UUID) -> None:
    record(client, lab, "ls")
    assert sql(client, "SELECT recorded_at FROM command_history") == [(AT,)]


def test_concurrent_writers_get_distinct_consecutive_seqs(
    client: TestClient, lab: uuid.UUID
) -> None:
    history = store(client)

    async def write_all() -> list[int]:
        return await asyncio.gather(
            *(history.record(lab, f"echo {index}", AT) for index in range(30))
        )

    seqs = run(client, write_all)

    assert sorted(seqs) == list(range(1, 31))
    assert sorted(command for _, command in rows(client, lab)) == sorted(
        f"echo {index}" for index in range(30)
    )


def test_a_full_lab_refuses_more_entries(
    client: TestClient, lab: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_ENTRIES_PER_LAB", 3)
    for command in ("a", "b", "c"):
        record(client, lab, command)

    with pytest.raises(LabHistoryFullError):
        record(client, lab, "d")
    assert rows(client, lab) == [(1, "a"), (2, "b"), (3, "c")]


def test_the_limit_is_two_thousand_entries() -> None:
    assert service.MAX_ENTRIES_PER_LAB == 2000


def test_a_missing_lab_is_a_database_error(client: TestClient) -> None:
    with pytest.raises(IntegrityError):
        record(client, uuid.uuid4(), "ls")


# Reading


def page(client: TestClient, lab_id: uuid.UUID, after: int, limit: int) -> tuple[list[int], bool]:
    async def read() -> service.Page:
        async with client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            return await read_page(db, lab_id, after, limit)

    result = run(client, read)
    return [entry.seq for entry in result.entries], result.more


def test_pages_follow_seq_and_say_whether_more_exist(client: TestClient, lab: uuid.UUID) -> None:
    for index in range(5):
        record(client, lab, f"echo {index}")

    assert page(client, lab, 0, 2) == ([1, 2], True)
    assert page(client, lab, 2, 2) == ([3, 4], True)
    assert page(client, lab, 4, 2) == ([5], False)
    assert page(client, lab, 3, 2) == ([4, 5], False)
    assert page(client, lab, 5, 2) == ([], False)


def test_a_page_holds_only_the_requested_lab(client: TestClient) -> None:
    user_id = add_user(client)
    other = add_lab_in(client, add_user(client, "bia@example.com"), "ready")  # type: ignore[arg-type]
    mine = add_lab_in(client, user_id, "ready")  # type: ignore[arg-type]
    record(client, other, "secret")
    record(client, mine, "ls")

    assert page(client, mine, 0, 10) == ([1], False)


def test_a_failed_insert_takes_no_seq(client: TestClient, lab: uuid.UUID) -> None:
    """The seq comes from the committed rows, not from a sequence: an insert that is
    rolled back leaves no gap."""
    with pytest.raises(IntegrityError, match="ck_command_history_command_characters"):
        record(client, lab, "echo \x1b")
    with pytest.raises(IntegrityError, match="ck_command_history_command_length"):
        record(client, lab, "")

    assert record(client, lab, "ls") == 1
    assert rows(client, lab) == [(1, "ls")]


def test_concurrent_writers_never_pass_the_limit(
    client: TestClient, lab: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_ENTRIES_PER_LAB", 5)
    history = store(client)

    async def write_all() -> list[int | BaseException]:
        return await asyncio.gather(
            *(history.record(lab, f"echo {index}", AT) for index in range(20)),
            return_exceptions=True,
        )

    results = run(client, write_all)

    assert sorted(result for result in results if isinstance(result, int)) == [1, 2, 3, 4, 5]
    assert all(
        isinstance(result, LabHistoryFullError) for result in results if not isinstance(result, int)
    )
    assert [seq for seq, _ in rows(client, lab)] == [1, 2, 3, 4, 5]
