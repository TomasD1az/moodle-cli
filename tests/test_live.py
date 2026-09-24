"""Integration tests against the real campus.

Skipped unless ``--live`` is passed. These exist to catch the campus changing its API in a
way the recorded fixtures cannot: the mocked suite proves the code is self-consistent, this
proves the assumptions still hold upstream.

Requires MOODLE_URL plus either a stored token, MOODLE_TOKEN, or MOODLE_USER/MOODLE_PASS.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from moodle_cli.capabilities import evaluate
from moodle_cli.client import MoodleClient
from moodle_cli.downloads import download_file, plan_downloads
from moodle_cli.errors import MoodleAPIError
from moodle_cli.session import open_client

pytestmark = pytest.mark.live

MAX_SMOKE_DOWNLOAD_BYTES = 2_000_000


@pytest.fixture(scope="module")
def live_client() -> MoodleClient:
    return open_client()


@pytest.fixture(scope="module")
def live_token(live_client: MoodleClient) -> str:
    return live_client.token


def test_site_info_reports_web_services_and_downloads(live_client: MoodleClient) -> None:
    info = live_client.get_site_info()
    assert info.userid > 0
    assert info.downloadfiles, "file downloads are disabled for this token"
    # The four functions the tool is built on must stay exposed by the mobile service.
    assert {
        "core_webservice_get_site_info",
        "core_course_get_enrolled_courses_by_timeline_classification",
        "core_course_get_contents",
        "core_enrol_get_enrolled_users",
    } <= info.function_names


def test_every_view_and_sort_combination_is_accepted(live_client: MoodleClient) -> None:
    from moodle_cli.client import SORTS, VIEWS

    for view in VIEWS:
        for sort in SORTS:
            live_client.list_courses(view=view, sort=sort)  # must not raise


def test_starred_is_a_strict_subset_of_all(live_client: MoodleClient) -> None:
    all_ids = {c.id for c in live_client.list_courses(view="all")}
    starred = live_client.list_courses(view="starred")
    assert starred, "expected at least one starred course"
    assert {c.id for c in starred} <= all_ids


def test_course_contents_and_participants_round_trip(live_client: MoodleClient) -> None:
    course = live_client.list_courses(view="starred", sort="last-accessed")[0]

    sections = live_client.get_course_contents(course.id)
    assert sections

    participants = live_client.get_participants(course.id)
    assert participants
    assert all(p.id > 0 and p.fullname for p in participants)


def test_downloading_a_real_file_matches_the_declared_size(
    live_client: MoodleClient, live_token: str, tmp_path: Path
) -> None:
    """The size check is the whole point: a JSON error body would not match filesize."""
    for course in live_client.list_courses(view="starred", sort="last-accessed"):
        planned = plan_downloads(live_client.get_course_contents(course.id), tmp_path)
        candidates = [p for p in planned if 0 < p.file.filesize <= MAX_SMOKE_DOWNLOAD_BYTES]
        if not candidates:
            continue

        target = candidates[0]
        with httpx.Client(timeout=120, follow_redirects=True) as http:
            result = download_file(http, target.file, live_token, target.destination)

        assert result.size == target.file.filesize
        assert target.destination.stat().st_size == target.file.filesize
        return

    pytest.skip("no starred course exposes a small enough file to smoke-test")


def test_course_contents_url_modules_expose_a_real_link(live_client: MoodleClient) -> None:
    """At least one course must have a url module whose target is not the Moodle page itself."""
    for course in live_client.list_courses(view="all"):
        for section in live_client.get_course_contents(course.id):
            for module in section.modules:
                for link in module.links:
                    assert link.fileurl
                    assert link.fileurl != module.url
                    return
    pytest.skip("no enrolled course currently has a url module")


def test_get_announcements_reads_a_real_news_forum(live_client: MoodleClient) -> None:
    """mod_forum_get_forum_discussions must stay exposed; get_discussions (no "forum_") is not."""
    for course in live_client.list_courses(view="all"):
        announcements = live_client.get_announcements([course.id])
        if announcements:
            assert announcements[0].subject
            assert announcements[0].courseid == course.id
            # Real posts are HTML: entities decoded and blocks kept apart, or the text
            # reads as "Estimados,Ya tienen el examen subido."
            assert "&nbsp;" not in announcements[0].message_text
            assert "<" not in announcements[0].message_text
            return
    pytest.skip("no enrolled course currently has announcements")


def test_every_assignment_status_parses(live_client: MoodleClient) -> None:
    """Which optional fields arrive null varies per assignment, so one is not a sample.

    Sweeping every assignment is what catches a field the campus leaves null on a
    minority of them; a single round trip passes right through that.
    """
    assignments = live_client.get_assignments()
    if not assignments:
        pytest.skip("no enrolled course currently has an assignment")

    for assignment in assignments:
        status = live_client.get_assignment_status(assignment.id)
        assert isinstance(status.submitted, bool)
        assert isinstance(status.graded, bool)


def test_get_quizzes_and_status_round_trip(live_client: MoodleClient) -> None:
    quizzes = live_client.get_quizzes()
    if not quizzes:
        pytest.skip("no enrolled course currently has a quiz")

    status = live_client.get_quiz_status(quizzes[0].id)
    assert status.attempt_count >= 0
    assert isinstance(status.has_grade, bool)


def test_get_grade_overview_always_succeeds(live_client: MoodleClient) -> None:
    """Unlike get_grade_items, this must never raise nopermissiontoviewgrades."""
    live_client.get_grade_overview()


def test_get_grade_items_either_succeeds_or_reports_the_known_permission_error(
    live_client: MoodleClient,
) -> None:
    for grade in live_client.get_grade_overview():
        try:
            items = live_client.get_grade_items(grade.courseid)
        except MoodleAPIError as exc:
            assert exc.errorcode == "nopermissiontoviewgrades"
        else:
            # Aggregate rows come unnamed; every row still has to render as something.
            assert all(item.label for item in items)
        return
    pytest.skip("no course grade overview to test against")


def test_calendar_returns_events_for_a_bounded_window(live_client: MoodleClient) -> None:
    """The calendar endpoints must stay exposed and keep their paging contract.

    A window a year wide either side, because a quiet fortnight is normal and an empty
    answer would prove nothing about whether the endpoint still works.
    """
    now = int(time.time())
    year = 365 * 86_400
    events = live_client.get_calendar_events(since=now - year, until=now + year, limit=60)
    if not events:
        pytest.skip("no calendar events in the past or coming year")

    assert all(event.id > 0 for event in events)
    assert all(event.timesort > 0 for event in events)
    # timesort is what the calendar orders by, and paging depends on that order holding.
    assert [e.timesort for e in events] == sorted(e.timesort for e in events)


def test_calendar_by_course_is_a_subset_of_the_campus_sweep(live_client: MoodleClient) -> None:
    """The two functions must agree, or narrowing by course would hide events."""
    now = int(time.time())
    year = 365 * 86_400
    everything = live_client.get_calendar_events(since=now - year, until=now + year, limit=200)
    with_a_course = [e for e in everything if e.course_id]
    if not with_a_course:
        pytest.skip("no course-bound calendar events to compare")

    course_id = with_a_course[0].course_id
    one_course = live_client.get_calendar_events(
        course_id=course_id, since=now - year, until=now + year, limit=200
    )

    assert {e.id for e in one_course} <= {e.id for e in everything}
    assert all(e.course_id == course_id for e in one_course)


def test_course_updates_answers_for_every_course(live_client: MoodleClient) -> None:
    """core_course_get_updates_since must stay exposed and answer without warnings."""
    now = int(time.time())
    checked = False
    for course in live_client.list_courses(view="all"):
        updates = live_client.get_course_updates(course.id, since=now - 90 * 86_400)
        assert all(u.id > 0 for u in updates)
        assert all(u.updates for u in updates), "an unchanged activity must be dropped"
        checked = True
    if not checked:
        pytest.skip("no enrolled courses")


def test_quiz_review_reads_back_a_finished_attempt(live_client: MoodleClient) -> None:
    """Review options vary, so the contract asserted here is only what always holds."""
    for quiz in live_client.get_quizzes():
        status = live_client.get_quiz_status(quiz.id)
        if not status.attempt_ids:
            continue
        try:
            review = live_client.get_quiz_attempt_review(status.attempt_ids[-1])
        except MoodleAPIError as exc:
            # A quiz still open, or one whose review the teacher closed, is a real answer.
            assert exc.errorcode in {"noreviewattempt", "noreview", "attemptclosed"}
            return
        assert all(q.slot > 0 for q in review.questions)
        assert all(q.number for q in review.questions)
        # Marks may be hidden, but a question that reports one must report a number.
        assert all(q.mark_value is not None for q in review.questions if q.mark)
        return
    pytest.skip("no attempted quiz to review")


def test_the_capability_table_matches_what_the_campus_reports(live_client: MoodleClient) -> None:
    """Every feature with a command must be one this campus can actually run.

    This is the assertion that fails when a campus turns a function off, which is the
    thing `auth capabilities` exists to tell a user about — so it should fail loudly here
    rather than only in their terminal.
    """
    available = live_client.get_site_info().function_names
    assert available, "the campus reported no function list"

    unavailable = [s.feature.name for s in evaluate(available) if s.feature.commands and s.missing]
    assert not unavailable, f"commands exist for features this campus lacks: {unavailable}"


def test_batching_agrees_with_calling_one_at_a_time(live_client: MoodleClient) -> None:
    """A batched answer must be indistinguishable from the same calls sent separately."""
    if not live_client.supports_batching:
        pytest.skip("this campus does not expose tool_mobile_call_external_functions")

    courses = live_client.list_courses(view="all")[:2]
    if len(courses) < 2:
        pytest.skip("need two courses to compare a batch against separate calls")

    calls = [("core_course_get_contents", {"courseid": c.id}) for c in courses]
    batched = live_client.call_many(calls)
    separately = [live_client.call(function, **params) for function, params in calls]

    assert batched == separately
