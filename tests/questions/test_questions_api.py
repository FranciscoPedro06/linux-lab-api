"""Question endpoints against PostgreSQL, with the synthetic content synced first."""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from linuxlab.content.loader import load_content
from linuxlab.content.sync import sync_content
from linuxlab.questions.router import ANSWER_ATTEMPTS
from tests.auth.support import ORIGIN, request, run, session_token, signup, sql
from tests.content.support import copy_fixture, edit_yaml, module_file

pytestmark = pytest.mark.integration

QUESTION = "sample-choice"
RIGHT, WRONG, OTHER_WRONG = "pwd", "cd", "ls"
# What only an answer may reveal, as written in tests/fixtures/content.
FEEDBACK_CD = "Feedback sintético da alternativa cd."
FEEDBACK_LS = "Feedback sintético da alternativa ls."
EXPLANATION = "Explicação sintética da questão de teste.\n"
SECRETS = ("Feedback sintético", "Explicação sintética", "answer", "feedback", "explanation")
NOT_ANSWERED = {
    "status": "not_answered",
    "attempts": 0,
    "completed_version": None,
    "completed_at": None,
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


@pytest.fixture
def token(client: TestClient, root: Path) -> str:
    sync(client, root)
    return session_token(signup(client))


def sync(client: TestClient, root: Path) -> None:
    content = load_content(root)
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]
    run(client, lambda: sync_content(sessionmaker, content))


def get(client: TestClient, token: str | None, path: str) -> Any:
    response = request(client, "GET", path, token=token)
    assert response.status_code == 200, response.text
    return response.json()


def answer(
    client: TestClient, token: str | None, option: str, *, version: int = 1, slug: str = QUESTION
) -> Any:
    return request(
        client,
        "POST",
        f"/api/questions/{slug}/answers",
        token=token,
        json={"version": version, "option": option},
    )


def answered(client: TestClient, token: str, option: str, **kwargs: Any) -> dict[str, Any]:
    response = answer(client, token, option, **kwargs)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def attempts(client: TestClient) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in sql(
            client,
            "SELECT u.email, q.slug, a.question_version, a.option, a.correct"
            " FROM question_attempts a JOIN users u ON u.id = a.user_id"
            " JOIN questions q ON q.id = a.question_id ORDER BY a.id",
        )
    ]


def card(client: TestClient, token: str, slug: str = QUESTION) -> dict[str, Any]:
    modules = get(client, token, "/api/question-modules")
    cards = {card["slug"]: card for module in modules for card in module["questions"]}
    found: dict[str, Any] = cards[slug]
    return found


# Reading


def test_question_modules_list_published_questions_in_order(client: TestClient, token: str) -> None:
    modules = get(client, token, "/api/question-modules")

    assert [module["slug"] for module in modules] == ["quiz"]
    (quiz,) = modules
    assert (quiz["title"], quiz["completed"], quiz["total"]) == ("Questões de teste", 0, 2)
    assert [card["slug"] for card in quiz["questions"]] == ["sample-choice", "sample-path"]
    assert quiz["questions"][0] == {
        "slug": "sample-choice",
        "title": "Questão de teste",
        "summary": "Uma questão sintética de múltipla escolha.",
        "difficulty": 1,
        "estimated_minutes": 1,
        "tags": ["sample"],
        "version": 1,
        "progress": NOT_ANSWERED,
    }
    assert quiz["questions"][1]["tags"] == []


def test_mission_modules_are_unchanged(client: TestClient, token: str) -> None:
    modules = get(client, token, "/api/modules")
    assert [module["slug"] for module in modules] == ["alpha"]
    assert all("questions" not in module for module in modules)


def test_a_question_is_read_without_its_answer(client: TestClient, token: str) -> None:
    question = get(client, token, f"/api/questions/{QUESTION}")

    assert question == {
        "slug": "sample-choice",
        "title": "Questão de teste",
        "summary": "Uma questão sintética de múltipla escolha.",
        "difficulty": 1,
        "estimated_minutes": 1,
        "tags": ["sample"],
        "version": 1,
        "progress": NOT_ANSWERED,
        "module": {"slug": "quiz", "title": "Questões de teste"},
        "position": 1,
        "previous": None,
        "next": "sample-path",
        "question": "Qual comando mostra o diretório atual?\n",
        "options": [
            {"id": "cd", "text": "`cd`"},
            {"id": "pwd", "text": "`pwd`"},
            {"id": "ls", "text": "`ls`"},
        ],
        "hints": ["Dica sintética."],
    }
    assert get(client, token, "/api/questions/sample-path")["previous"] == "sample-choice"
    assert get(client, token, "/api/questions/sample-path")["next"] is None


def leaks(body: Any) -> list[str]:
    raw = json.dumps(body, ensure_ascii=False)
    # "not_answered" holds "answer": the status is replaced before looking.
    raw = raw.replace('"not_answered"', '""')
    return [secret for secret in SECRETS if secret in raw]


def test_reading_never_reveals_the_answer_even_after_completion(
    client: TestClient, token: str
) -> None:
    assert leaks(get(client, token, "/api/question-modules")) == []
    assert leaks(get(client, token, f"/api/questions/{QUESTION}")) == []

    answered(client, token, WRONG)
    answered(client, token, RIGHT)

    for path in ("/api/question-modules", f"/api/questions/{QUESTION}"):
        body = get(client, token, path)
        assert leaks(body) == []
        assert RIGHT not in json.dumps(body).replace('"id": "pwd"', "")


@pytest.mark.parametrize(
    "path",
    ["/api/questions/sample-choice-draft", "/api/questions/missing", "/api/questions/Bad_Slug"],
)
def test_drafts_and_unknown_questions_are_not_found(
    client: TestClient, token: str, path: str
) -> None:
    response = request(client, "GET", path, token=token)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "question_not_found"


def test_a_question_of_a_draft_module_is_not_found(
    client: TestClient, token: str, root: Path
) -> None:
    edit_yaml(module_file(root, "quiz"), lambda d: d.update(status="draft"))
    sync(client, root)

    assert get(client, token, "/api/question-modules") == []
    assert request(client, "GET", f"/api/questions/{QUESTION}", token=token).status_code == 404
    assert answer(client, token, RIGHT).status_code == 404


def test_reading_and_answering_need_a_session(client: TestClient, token: str) -> None:
    for method, path in (
        ("GET", "/api/question-modules"),
        ("GET", f"/api/questions/{QUESTION}"),
    ):
        assert request(client, method, path, token=None).status_code == 401
    assert answer(client, None, RIGHT).status_code == 401
    assert attempts(client) == []


# Answering


def test_a_wrong_answer_gets_its_own_feedback_and_the_explanation(
    client: TestClient, token: str
) -> None:
    body = answered(client, token, WRONG)

    assert body == {
        "question": {"slug": QUESTION, "version": 1},
        "option": WRONG,
        "correct": False,
        "feedback": FEEDBACK_CD,
        "explanation": EXPLANATION,
        "newly_completed": False,
        "progress": {
            "status": "attempted",
            "attempts": 1,
            "completed_version": None,
            "completed_at": None,
        },
    }
    assert answered(client, token, OTHER_WRONG)["feedback"] == FEEDBACK_LS
    assert card(client, token)["progress"]["status"] == "attempted"


def test_a_right_answer_completes_the_question(client: TestClient, token: str) -> None:
    body = answered(client, token, RIGHT)

    assert body["correct"] is True
    assert body["feedback"] is None
    assert body["explanation"] == EXPLANATION
    assert body["newly_completed"] is True
    assert body["progress"]["status"] == "completed"
    assert body["progress"]["completed_version"] == 1
    assert body["progress"]["attempts"] == 1
    assert card(client, token)["progress"] == body["progress"]
    assert get(client, token, "/api/question-modules")[0]["completed"] == 1


def test_answering_again_keeps_the_first_completion_and_every_attempt(
    client: TestClient, token: str
) -> None:
    answered(client, token, WRONG)
    first = answered(client, token, RIGHT)
    again = answered(client, token, RIGHT)
    after = answered(client, token, WRONG)

    assert again["newly_completed"] is False
    assert after["correct"] is False
    # A later wrong answer does not undo the completion.
    assert after["progress"]["status"] == "completed"
    assert after["progress"]["completed_at"] == first["progress"]["completed_at"]
    assert after["progress"]["attempts"] == 4
    assert attempts(client) == [
        ("ana@example.com", QUESTION, 1, WRONG, False),
        ("ana@example.com", QUESTION, 1, RIGHT, True),
        ("ana@example.com", QUESTION, 1, RIGHT, True),
        ("ana@example.com", QUESTION, 1, WRONG, False),
    ]
    assert sql(client, "SELECT count(*) FROM user_questions") == [(1,)]


def test_users_do_not_share_progress_or_attempts(client: TestClient, token: str) -> None:
    other = session_token(signup(client, "bia@example.com"))
    answered(client, token, RIGHT)

    assert card(client, other)["progress"] == NOT_ANSWERED
    assert get(client, other, f"/api/questions/{QUESTION}")["progress"] == NOT_ANSWERED
    body = answered(client, other, WRONG)
    assert body["progress"]["attempts"] == 1
    assert body["progress"]["status"] == "attempted"
    assert card(client, token)["progress"]["attempts"] == 1
    assert card(client, token)["progress"]["status"] == "completed"
    assert [row[0] for row in attempts(client)] == ["ana@example.com", "bia@example.com"]


def test_the_client_cannot_say_who_answers_or_whether_it_is_right(
    client: TestClient, token: str
) -> None:
    for extra in ({"correct": True}, {"is_correct": True}, {"user_id": str(uuid.uuid4())}):
        response = request(
            client,
            "POST",
            f"/api/questions/{QUESTION}/answers",
            token=token,
            json={"version": 1, "option": WRONG, **extra},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
    assert attempts(client) == []


@pytest.mark.parametrize(
    "body",
    [
        {"version": 1},
        {"option": RIGHT},
        {"version": "1", "option": RIGHT},
        {"version": True, "option": RIGHT},
        {"version": 0, "option": RIGHT},
        {"version": 1, "option": "PWD"},
        {"version": 1, "option": ""},
        {"version": 1, "option": "x" * 33},
        {"version": 1, "option": ["pwd"]},
    ],
)
def test_malformed_answers_are_refused(
    client: TestClient, token: str, body: dict[str, Any]
) -> None:
    response = request(client, "POST", f"/api/questions/{QUESTION}/answers", token=token, json=body)
    assert response.status_code == 422
    assert attempts(client) == []


def test_an_option_of_another_question_is_refused(client: TestClient, token: str) -> None:
    response = answer(client, token, "absolute")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_option"
    assert attempts(client) == []


@pytest.mark.parametrize("slug", ["sample-choice-draft", "missing", "Bad_Slug"])
def test_drafts_and_unknown_questions_cannot_be_answered(
    client: TestClient, token: str, slug: str
) -> None:
    response = answer(client, token, "a", slug=slug)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "question_not_found"
    assert attempts(client) == []


def test_answers_need_an_allowed_origin(client: TestClient, token: str) -> None:
    response = request(
        client,
        "POST",
        f"/api/questions/{QUESTION}/answers",
        token=token,
        json={"version": 1, "option": RIGHT},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert ORIGIN != "https://evil.example"
    assert attempts(client) == []


def test_answers_are_rate_limited_per_user(client: TestClient, token: str) -> None:
    for _ in range(ANSWER_ATTEMPTS):
        assert answer(client, token, WRONG).status_code == 200
    refused = answer(client, token, RIGHT)

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"
    assert int(refused.headers["retry-after"]) >= 1
    assert len(attempts(client)) == ANSWER_ATTEMPTS
    # Another user has their own limit.
    other = session_token(signup(client, "bia@example.com"))
    assert answer(client, other, RIGHT).status_code == 200


# Versions


def new_version(client: TestClient, root: Path) -> None:
    """sample-choice changes: cd becomes the right option."""
    edit_yaml(
        root / "questions" / f"{QUESTION}.yaml",
        lambda d: d.update(
            answer="cd",
            options=[
                {"id": "cd", "text": "`cd`"},
                {"id": "pwd", "text": "`pwd`", "feedback": "Na versão 2, não."},
                {"id": "ls", "text": "`ls`", "feedback": FEEDBACK_LS},
            ],
        ),
    )
    sync(client, root)


def test_an_answer_to_an_outdated_version_is_refused(
    client: TestClient, token: str, root: Path
) -> None:
    new_version(client, root)

    response = answer(client, token, RIGHT, version=1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "question_outdated"
    for version in (3, 99):
        assert answer(client, token, RIGHT, version=version).status_code == 409
    assert attempts(client) == []
    assert get(client, token, f"/api/questions/{QUESTION}")["version"] == 2


def test_a_new_version_is_checked_against_its_own_answer(
    client: TestClient, token: str, root: Path
) -> None:
    answered(client, token, WRONG)
    new_version(client, root)

    body = answered(client, token, "cd", version=2)
    assert body["correct"] is True
    assert body["question"] == {"slug": QUESTION, "version": 2}
    assert answered(client, token, "pwd", version=2)["feedback"] == "Na versão 2, não."
    assert attempts(client) == [
        ("ana@example.com", QUESTION, 1, WRONG, False),
        ("ana@example.com", QUESTION, 2, "cd", True),
        ("ana@example.com", QUESTION, 2, "pwd", False),
    ]


def test_attempts_count_per_version_and_the_completion_stays(
    client: TestClient, token: str, root: Path
) -> None:
    answered(client, token, WRONG)
    answered(client, token, RIGHT)
    new_version(client, root)

    progress = card(client, token)["progress"]
    # Completed on version 1; no answer to version 2 yet.
    assert progress["status"] == "completed"
    assert progress["completed_version"] == 1
    assert progress["attempts"] == 0
    body = answered(client, token, "ls", version=2)
    assert body["newly_completed"] is False
    assert body["progress"]["attempts"] == 1
    assert body["progress"]["completed_version"] == 1


def test_a_question_without_answers_on_its_new_version_is_not_answered(
    client: TestClient, token: str, root: Path
) -> None:
    answered(client, token, WRONG)
    new_version(client, root)
    assert card(client, token)["progress"] == NOT_ANSWERED


# The database


def test_attempts_and_completions_cannot_be_changed(client: TestClient, token: str) -> None:
    answered(client, token, RIGHT)
    with pytest.raises(DBAPIError, match="question attempts cannot be changed"):
        sql(client, "UPDATE question_attempts SET correct = false")
    with pytest.raises(DBAPIError, match="user questions cannot be changed"):
        sql(client, "UPDATE user_questions SET completed_version = 1")


def test_attempts_name_an_existing_version_of_their_question(
    client: TestClient, token: str
) -> None:
    answered(client, token, RIGHT)
    with pytest.raises(DBAPIError, match="fk_question_attempts_question_id_question_versions"):
        sql(
            client,
            "INSERT INTO question_attempts"
            " (user_id, question_id, question_version, option, correct)"
            " SELECT user_id, question_id, 7, option, correct FROM question_attempts",
        )
    with pytest.raises(DBAPIError, match="ck_question_attempts_option_format"):
        sql(
            client,
            "INSERT INTO question_attempts"
            " (user_id, question_id, question_version, option, correct)"
            " SELECT user_id, question_id, 1, 'Bad Option', correct FROM question_attempts",
        )


def test_deleting_a_user_deletes_their_answers(client: TestClient, token: str) -> None:
    answered(client, token, WRONG)
    answered(client, token, RIGHT)
    sql(client, "DELETE FROM users")
    assert sql(client, "SELECT count(*) FROM question_attempts") == [(0,)]
    assert sql(client, "SELECT count(*) FROM user_questions") == [(0,)]
