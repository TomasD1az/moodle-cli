"""Shared test fixtures.

Fixtures are synthetic but shape-accurate: they reproduce the quirks the real campus
returns (null ``filepath``, ``isexternalfile`` on bibliography files, doubled extensions)
without committing real classmates' names or email addresses to the repository.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from moodle_cli import plugins

# Set before anything imports moodle_cli.cli or moodle_cli.mcp_server, because those mount
# plugins at import time -- which happens during collection, before any fixture can run.
# The autouse fixture below cannot undo that: clearing the discovery cache does not
# un-mount a Typer group or an MCP tool that is already registered. conftest is imported
# ahead of every test module, so this is the last moment that is still early enough.
os.environ.setdefault(plugins.DISABLE_ENV, "1")

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://campus.example.edu"
REST_URL = f"{BASE_URL}/webservice/rest/server.php"
TOKEN_URL = f"{BASE_URL}/login/token.php"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests that hit the real campus using credentials from the environment",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def posted_params(request: httpx.Request) -> dict[str, str]:
    """The form body of a REST call, decoded back into flat parameters."""
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Keep the developer's real .env and keyring out of the unit tests.

    Without this, a .env in the repo root would silently supply real credentials.
    """
    if "live" in request.keywords:
        return
    monkeypatch.setattr("moodle_cli.config._find_dotenv", lambda: None)
    monkeypatch.setattr("moodle_cli.config._ENV_LOADED", False)
    for var in ("MOODLE_URL", "MOODLE_USER", "MOODLE_PASS", "MOODLE_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_plugins(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep whatever plugins the developer has installed out of the suite.

    Without this, `moodle --help` and the MCP tool list differ between a maintainer's
    machine and CI. Autouse and opt-out rather than opt-in, because a test that forgets to
    ask for isolation is exactly the one that passes here and fails there. The cache is
    cleared on both sides so a discovery surviving a test cannot make the suite
    order-dependent.
    """
    monkeypatch.setenv(plugins.DISABLE_ENV, "1")
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture(autouse=True)
def fixed_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the zone so a date assertion says the same thing on every machine.

    Dates are rendered in local time, so without this the expected day depends on where
    the suite runs. The campus's own zone is the one the output has to read correctly in,
    and being off UTC is what keeps a test able to tell the two apart. `time.tzset` is
    POSIX-only, so this is a no-op rather than a crash where it doesn't exist (Windows).
    """
    monkeypatch.setenv("TZ", "America/Argentina/Buenos_Aires")
    if not hasattr(time, "tzset"):
        yield
        return
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture
def courses_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("courses.json")
    return payload


@pytest.fixture
def hidden_course() -> dict[str, Any]:
    """A course the dashboard filters out; only the all-including-hidden view returns it."""
    return {
        "id": 104,
        "fullname": "I204 - Sistemas Operativos (grupo 1) G:1 Teó 1 - 1er. Semestre 2025",
        "shortname": "I204 - 101313",
        "startdate": 1736910000,
        "enddate": 0,
        "visible": True,
        "viewurl": f"{BASE_URL}/course/view.php?id=104",
        "progress": None,
        "isfavourite": False,
        "hidden": True,
        "coursecategory": "Ingeniería en Inteligencia Artificial",
    }


@pytest.fixture
def contents_payload() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = load_fixture("contents.json")
    return payload


@pytest.fixture
def participants_payload() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = load_fixture("participants.json")
    return payload


@pytest.fixture
def forums_payload() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = load_fixture("forums.json")
    return payload


@pytest.fixture
def discussions_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("discussions.json")
    return payload


@pytest.fixture
def assignments_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("assignments.json")
    return payload


@pytest.fixture
def submission_status_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("submission_status.json")
    return payload


@pytest.fixture
def grades_overview_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("grades_overview.json")
    return payload


@pytest.fixture
def grade_items_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("grade_items.json")
    return payload


@pytest.fixture
def calendar_payload() -> dict[str, Any]:
    """Three action events: a quiz closing, an overdue assignment, and a site event.

    The site event is the awkward one and the reason it is here: it arrives with a null
    ``course``, ``modulename`` and ``action``, so anything that reads those without
    allowing for absence breaks on it rather than on the two ordinary rows.
    """
    payload: dict[str, Any] = load_fixture("calendar_events.json")
    return payload


@pytest.fixture
def course_updates_payload() -> dict[str, Any]:
    """Two activities the contents fixture names, one it does not, and one that is quiet.

    The unnamed course-module id is the realistic case the join cannot cover: an activity
    can change and still not be in the contents a student is allowed to see. The instance
    with an empty ``updates`` array is what Moodle sends for a module it checked and
    found unchanged.
    """
    payload: dict[str, Any] = load_fixture("course_updates.json")
    return payload


@pytest.fixture
def site_info_payload() -> dict[str, Any]:
    return {"userid": 63643, "functions": []}


@pytest.fixture
def quizzes_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("quizzes.json")
    return payload


@pytest.fixture
def quiz_attempts_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("quiz_attempts.json")
    return payload


@pytest.fixture
def quiz_attempt_review_payload() -> dict[str, Any]:
    """A finished attempt: one right, one wrong and flagged, one awaiting manual grading.

    The third question is the shape worth having — Moodle omits ``mark``, ``maxmark`` and
    ``state`` entirely for a question whose marks the reader may not see, so anything that
    reads them as always-present breaks on an essay before it breaks on anything else.
    """
    payload: dict[str, Any] = load_fixture("quiz_attempt_review.json")
    return payload


@pytest.fixture
def quiz_best_grade_payload() -> dict[str, Any]:
    payload: dict[str, Any] = load_fixture("quiz_best_grade.json")
    return payload


@pytest.fixture
def tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token in the environment keeps a surface off the keyring and off login/token.php.

    Requested per module with ``pytest.mark.usefixtures`` so the auth tests keep an empty
    environment and still exercise token resolution.
    """
    monkeypatch.setenv("MOODLE_URL", BASE_URL)
    monkeypatch.setenv("MOODLE_TOKEN", "test-token")


def route_by_function(**payloads: Any) -> respx.Route:
    """Dispatch the single REST endpoint on the wsfunction being requested.

    A payload may be a callable taking the encoded request body, for a function whose
    answer depends on the parameters it was called with.
    """

    def responder(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        for function, payload in payloads.items():
            if f"wsfunction={function}" in body:
                return httpx.Response(200, json=payload(body) if callable(payload) else payload)
        return httpx.Response(200, json={"errorcode": "unmocked", "message": body})

    return respx.post(REST_URL).mock(side_effect=responder)
