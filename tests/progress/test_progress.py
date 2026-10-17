"""The progress object sent to the client, without a database."""

from datetime import UTC, datetime

from linuxlab.progress.service import NOT_COMPLETED, Progress

AT = datetime(2026, 10, 17, 14, 3, 12, tzinfo=UTC)


def test_no_completion_is_not_completed() -> None:
    assert Progress.of(None, None) == NOT_COMPLETED
    assert NOT_COMPLETED.model_dump(mode="json") == {
        "status": "not_completed",
        "completed_version": None,
        "completed_at": None,
    }


def test_a_completion_is_completed_with_its_version_and_time() -> None:
    assert Progress.of(1, AT).model_dump(mode="json") == {
        "status": "completed",
        "completed_version": 1,
        "completed_at": "2026-10-17T14:03:12Z",
    }
