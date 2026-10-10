"""A database failure while creating a lab must not reveal the lab's parameters.

Failed statements end up in logged tracebacks; their text names the SQL, never the
bound values.
"""

import logging
import traceback
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from linuxlab.labs import lifecycle
from tests.auth.support import sql

from .lab_support import create_lab, fake_runtime, publish_missions, user

pytestmark = pytest.mark.integration

# Synthetic, in the parameter format, and found nowhere else.
SYNTHETIC_VALUE = "zz-synthetic-param-7f3a"
FAILURE = "synthetic lab insert failure"


@pytest.fixture
def failing_insert(client: TestClient) -> Iterator[None]:
    """Make every INSERT into lab_sessions fail inside PostgreSQL."""
    sql(
        client,
        "CREATE FUNCTION test_fail_lab_insert() RETURNS trigger LANGUAGE plpgsql AS $$"
        f" BEGIN RAISE EXCEPTION '{FAILURE}'; END $$",
    )
    sql(
        client,
        "CREATE TRIGGER test_fail_lab_insert BEFORE INSERT ON lab_sessions"
        " FOR EACH ROW EXECUTE FUNCTION test_fail_lab_insert()",
    )
    try:
        yield
    finally:
        sql(client, "DROP TRIGGER test_fail_lab_insert ON lab_sessions")
        sql(client, "DROP FUNCTION test_fail_lab_insert()")


def test_engine_hides_statement_parameters(client: TestClient) -> None:
    engine: Any = client.app.state.engine  # type: ignore[attr-defined]
    assert engine.sync_engine.hide_parameters is True


def test_failed_lab_insert_does_not_reveal_parameters(
    client: TestClient,
    failing_insert: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(lifecycle, "generate_params", lambda *a, **k: {"token": SYNTHETIC_VALUE})
    ana = user(client, "ana@example.com")
    publish_missions(client)

    # The test client re-raises what the server would log as an unhandled error.
    with pytest.raises(DBAPIError) as raised:
        create_lab(client, ana)

    # What a server logs for it: the exception's traceback, with its causes.
    logged = "".join(traceback.format_exception(raised.value))
    assert SYNTHETIC_VALUE not in logged
    # Still identifiable: the database's message and the statement.
    assert FAILURE in logged
    assert "INSERT INTO lab_sessions" in logged
    assert "hidden due to hide_parameters" in logged

    formatter = logging.Formatter("%(message)s")
    for record in caplog.records:
        assert SYNTHETIC_VALUE not in formatter.format(record)

    # Nothing was created.
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(0,)]
    assert fake_runtime(client).calls == []
