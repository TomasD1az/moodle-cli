"""Client tests: error detection, parameter encoding, filter mapping, pagination."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from moodle_cli.client import SORTS, VIEWS, MoodleClient, _flatten_params, check_api_error
from moodle_cli.errors import MoodleAPIError, MoodleError
from tests.conftest import BASE_URL, REST_URL, posted_params


@pytest.fixture
def client() -> MoodleClient:
    return MoodleClient(BASE_URL, "test-token")


# -- error detection -------------------------------------------------------------


def test_check_api_error_passes_lists_and_plain_dicts() -> None:
    check_api_error([{"id": 1}])
    check_api_error({"courses": []})


@pytest.mark.parametrize(
    "body",
    [
        {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Invalid token"},
        {"errorcode": "accessexception", "message": "Access control exception"},
    ],
)
def test_check_api_error_raises_on_error_payloads(body: dict[str, Any]) -> None:
    with pytest.raises(MoodleAPIError):
        check_api_error(body, function="core_course_get_contents")


@respx.mock
def test_call_raises_on_http_200_error_body(client: MoodleClient) -> None:
    respx.post(REST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "Invalid token - token not found",
            },
        )
    )
    with pytest.raises(MoodleAPIError) as excinfo:
        client.get_site_info()
    assert excinfo.value.errorcode == "invalidtoken"


@respx.mock
def test_call_raises_on_non_json_response(client: MoodleClient) -> None:
    respx.post(REST_URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(MoodleError, match="non-JSON"):
        client.get_site_info()


# -- parameter encoding ----------------------------------------------------------


def test_flatten_params_encodes_php_style_arrays() -> None:
    flat = _flatten_params({"courseid": 5, "options": [{"name": "limitfrom", "value": 0}]})
    assert flat == {
        "courseid": "5",
        "options[0][name]": "limitfrom",
        "options[0][value]": "0",
    }


def test_flatten_params_drops_none_and_encodes_bools() -> None:
    assert _flatten_params({"a": None, "b": True, "c": False}) == {"b": "1", "c": "0"}


# -- filters ---------------------------------------------------------------------


@respx.mock
def test_list_courses_maps_public_names_to_moodle_values(
    client: MoodleClient, courses_payload: dict[str, Any]
) -> None:
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=courses_payload))

    client.list_courses(view="starred", sort="last-accessed")

    params = posted_params(route.calls[0].request)
    assert params["classification"] == "favourites"
    assert params["sort"] == "ul.timeaccess desc"
    assert params["wsfunction"] == "core_course_get_enrolled_courses_by_timeline_classification"


def test_view_and_sort_tables_cover_the_dashboard_options() -> None:
    assert set(VIEWS) == {
        "all",
        "all-including-hidden",
        "in-progress",
        "future",
        "past",
        "starred",
        "hidden",
    }
    assert set(SORTS) == {"name", "last-accessed"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"view": "bogus"}, "Unknown view"), ({"sort": "bogus"}, "Unknown sort")],
)
def test_list_courses_rejects_unknown_filters(
    client: MoodleClient, kwargs: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        client.list_courses(**kwargs)


# -- contents and participants ---------------------------------------------------


@respx.mock
def test_get_course_contents_keeps_only_real_files(
    client: MoodleClient, contents_payload: list[dict[str, Any]]
) -> None:
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=contents_payload))

    sections = client.get_course_contents(101)
    files = [f for s in sections for m in s.modules for f in m.files]

    # 1 resource + 2 folder entries; the url module's link is excluded.
    assert len(files) == 3
    assert all(f.type == "file" for f in files)
    # External-repository files still count: they are served through pluginfile.php.
    assert any(f.isexternalfile for f in files)


@respx.mock
def test_get_course_contents_surfaces_url_module_targets_as_links(
    client: MoodleClient, contents_payload: list[dict[str, Any]]
) -> None:
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=contents_payload))

    sections = client.get_course_contents(101)
    links = [link for s in sections for m in s.modules for link in m.links]

    assert len(links) == 1
    assert links[0].filename == "Slack de la Materia"
    assert links[0].fileurl == "https://slack.example.com/join"
    # A url module's target never shows up in `files`: there is nothing to download.
    assert all(
        link not in [f for s in sections for m in s.modules for f in m.files] for link in links
    )


@respx.mock
def test_get_participants_pages_until_short_page(client: MoodleClient) -> None:
    from moodle_cli.client import _PARTICIPANT_PAGE_SIZE

    full = [{"id": i, "fullname": f"User {i}", "roles": []} for i in range(_PARTICIPANT_PAGE_SIZE)]
    tail = [{"id": 9999, "fullname": "Last One", "roles": []}]
    route = respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json=full),
            httpx.Response(200, json=tail),
        ]
    )

    people = client.get_participants(101)

    assert len(people) == _PARTICIPANT_PAGE_SIZE + 1
    assert posted_params(route.calls[1].request)["options[0][value]"] == str(_PARTICIPANT_PAGE_SIZE)


# -- announcements -----------------------------------------------------------------


@respx.mock
def test_get_announcements_only_reads_news_forums(
    client: MoodleClient,
    forums_payload: list[dict[str, Any]],
    discussions_payload: dict[str, Any],
) -> None:
    route = respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json=forums_payload),
            httpx.Response(200, json=discussions_payload),
        ]
    )

    announcements = client.get_announcements([101])

    # Two calls total: the general-discussion forum (502) is never queried.
    assert len(route.calls) == 2
    assert posted_params(route.calls[1].request)["forumid"] == "501"

    assert [a.id for a in announcements] == [9001, 9000]  # newest first
    assert all(a.courseid == 101 for a in announcements)
    assert "<strong>" not in announcements[0].message_text
    assert "S004" in announcements[0].message_text


@respx.mock
def test_get_announcements_returns_empty_without_a_news_forum(
    client: MoodleClient, forums_payload: list[dict[str, Any]]
) -> None:
    general_only = [f for f in forums_payload if f["type"] != "news"]
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=general_only))

    assert client.get_announcements([101]) == []
    assert len(route.calls) == 1  # no discussion read for a forum that carries no news


@respx.mock
def test_get_announcements_sweeps_dashboard_hidden_courses_too(
    client: MoodleClient, courses_payload: dict[str, Any]
) -> None:
    route = respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json=courses_payload),
            httpx.Response(200, json=[]),
        ]
    )

    client.get_announcements()

    assert posted_params(route.calls[0].request)["classification"] == "allincludinghidden"
    assert posted_params(route.calls[1].request)["courseids[0]"] == "101"


@respx.mock
def test_get_announcements_rejects_a_forum_record_missing_its_course(client: MoodleClient) -> None:
    """A wire payload becomes a model before use, so a missing field fails by name."""
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=[{"id": 501, "type": "news"}]))

    with pytest.raises(ValidationError) as exc:
        client.get_announcements([101])

    assert "course" in str(exc.value)


@respx.mock
def test_get_announcements_raises_on_a_warning_beside_the_discussions(
    client: MoodleClient, forums_payload: list[dict[str, Any]]
) -> None:
    """A partial read must not be reported as an empty one."""
    respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json=forums_payload),
            httpx.Response(
                200,
                json={
                    "discussions": [],
                    "warnings": [
                        {"warningcode": "errorreadingforum", "message": "Could not read the forum"}
                    ],
                },
            ),
        ]
    )

    with pytest.raises(MoodleAPIError) as exc:
        client.get_announcements([101])

    assert "errorreadingforum" in str(exc.value)


# -- assignments -------------------------------------------------------------------


@respx.mock
def test_get_assignments_flattens_courses(
    client: MoodleClient, assignments_payload: dict[str, Any]
) -> None:
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=assignments_payload))

    assignments = client.get_assignments()

    assert [a.name for a in assignments] == ["Actividad semana 1", "Trabajo Práctico 1"]
    assert all(a.course == 101 for a in assignments)


@respx.mock
def test_a_negative_grade_is_a_scale_id_and_not_a_maximum(
    client: MoodleClient, assignments_payload: dict[str, Any]
) -> None:
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=assignments_payload))

    points, scale = client.get_assignments()

    assert points.scale_graded is False
    assert points.max_grade == 100
    assert scale.grade == -52  # the scale's id, as the campus sends it
    assert scale.scale_graded is True
    assert scale.max_grade is None


@respx.mock
def test_get_assignment_status_extracts_submission_and_grade(
    client: MoodleClient, submission_status_payload: dict[str, Any]
) -> None:
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=submission_status_payload))

    status = client.get_assignment_status(40393)

    assert status.submitted is True
    assert status.graded is True
    assert status.grade == "90.00000"
    # &nbsp; arrives HTML-escaped and is decoded, not left literal.
    assert status.gradefordisplay == "90.00\xa0/\xa0100.00"
    assert status.submitted_files == ["Entrega - Semana 1.pdf"]


@respx.mock
def test_a_null_optional_field_in_a_status_payload_does_not_crash(
    client: MoodleClient, submission_status_payload: dict[str, Any]
) -> None:
    """The campus sends null for an unset optional field instead of omitting it."""
    submission_status_payload["lastattempt"].update(
        gradingstatus=None, extensionduedate=None, submission=None
    )
    submission_status_payload["feedback"].update(gradefordisplay=None, grade=None)
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=submission_status_payload))

    status = client.get_assignment_status(40393)

    assert status.submitted is False
    assert status.graded is False
    assert status.grade is None
    assert status.gradefordisplay == ""
    assert status.extension_due_at is None
    assert status.submitted_files == []


@respx.mock
def test_a_released_grade_under_a_marking_workflow_counts_as_graded(
    client: MoodleClient, submission_status_payload: dict[str, Any]
) -> None:
    """Marking workflow replaces graded/notgraded with its own states; only one is visible."""
    submission_status_payload["lastattempt"]["gradingstatus"] = "released"
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=submission_status_payload))

    assert client.get_assignment_status(40393).graded is True


@respx.mock
def test_an_unreleased_grade_under_a_marking_workflow_counts_as_ungraded(
    client: MoodleClient, submission_status_payload: dict[str, Any]
) -> None:
    submission_status_payload["lastattempt"]["gradingstatus"] = "inmarking"
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=submission_status_payload))

    assert client.get_assignment_status(40393).graded is False


@respx.mock
def test_a_group_submission_is_read_from_the_team_record(
    client: MoodleClient, submission_status_payload: dict[str, Any]
) -> None:
    """A team assignment files the attempt under teamsubmission and leaves submission unset."""
    lastattempt = submission_status_payload["lastattempt"]
    lastattempt["teamsubmission"] = lastattempt.pop("submission")
    lastattempt["submission"] = None
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=submission_status_payload))

    status = client.get_assignment_status(40393)

    assert status.submitted is True
    assert status.submitted_files == ["Entrega - Semana 1.pdf"]


# -- quizzes ---------------------------------------------------------------------


@respx.mock
def test_get_quizzes_sends_no_courseids_when_none_are_given(
    client: MoodleClient, quizzes_payload: dict[str, Any]
) -> None:
    """Omitting courseids is what reaches enrolments the dashboard hides."""
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=quizzes_payload))

    quizzes = client.get_quizzes()

    assert len(quizzes) == 1
    assert quizzes[0].name == "Actividad semana 2"
    assert quizzes[0].course == 101
    assert len(route.calls) == 1
    params = posted_params(route.calls[0].request)
    assert not [k for k in params if k.startswith("courseids[")]


@respx.mock
def test_get_quiz_status_combines_attempts_and_best_grade(
    client: MoodleClient,
    quiz_attempts_payload: dict[str, Any],
    quiz_best_grade_payload: dict[str, Any],
    quizzes_payload: dict[str, Any],
) -> None:
    respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json=quiz_attempts_payload),
            httpx.Response(200, json=quiz_best_grade_payload),
            httpx.Response(200, json=quizzes_payload),
        ]
    )

    status = client.get_quiz_status(42628)

    assert status.attempt_count == 1
    assert status.last_state == "finished"
    assert status.has_grade is True
    assert status.grade == 6.925
    assert status.grade_to_pass == 4
    assert status.max_grade == 10


@respx.mock
def test_get_quiz_status_tolerates_an_attempt_without_a_state(client: MoodleClient) -> None:
    """Every member of an attempt is optional, so a missing one must not raise."""
    respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"attempts": [{"id": 883899}], "warnings": []}),
            httpx.Response(200, json={"hasgrade": False, "warnings": []}),
        ]
    )

    status = client.get_quiz_status(42628)

    assert status.attempt_count == 1
    assert status.last_state is None


@respx.mock
def test_get_quiz_status_handles_no_attempts_yet(client: MoodleClient) -> None:
    route = respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"attempts": [], "warnings": []}),
            httpx.Response(200, json={"hasgrade": False, "gradetopass": 60, "warnings": []}),
        ]
    )

    status = client.get_quiz_status(42628)

    assert status.attempt_count == 0
    assert status.last_state is None
    assert status.has_grade is False
    assert status.max_grade is None
    assert len(route.calls) == 2


@respx.mock
def test_get_quiz_status_narrows_the_grade_lookup_to_a_known_course(
    client: MoodleClient,
    quiz_attempts_payload: dict[str, Any],
    quiz_best_grade_payload: dict[str, Any],
    quizzes_payload: dict[str, Any],
) -> None:
    """A quiz id does not say which course holds it, so the lookup sweeps unless told.

    Walking one course's quizzes is the case that hurts: without the hint, each quiz costs
    a fetch of every quiz on the campus.
    """
    route = respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json=quiz_attempts_payload),
            httpx.Response(200, json=quiz_best_grade_payload),
            httpx.Response(200, json=quizzes_payload),
        ]
    )

    status = client.get_quiz_status(42628, course_id=101)

    assert status.max_grade == 10
    assert posted_params(route.calls[2].request)["courseids[0]"] == "101"


@respx.mock
def test_get_quiz_status_sweeps_when_the_course_is_unknown(
    client: MoodleClient,
    quiz_attempts_payload: dict[str, Any],
    quiz_best_grade_payload: dict[str, Any],
    quizzes_payload: dict[str, Any],
) -> None:
    """No hint means no filter: the quiz has to be found somewhere."""
    route = respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json=quiz_attempts_payload),
            httpx.Response(200, json=quiz_best_grade_payload),
            httpx.Response(200, json=quizzes_payload),
        ]
    )

    client.get_quiz_status(42628)

    params = posted_params(route.calls[2].request)
    assert not [key for key in params if key.startswith("courseids[")]


# -- grades ----------------------------------------------------------------------


@respx.mock
def test_get_grade_overview_uses_the_tokens_own_user_id(
    client: MoodleClient, grades_overview_payload: dict[str, Any]
) -> None:
    route = respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"userid": 63643, "functions": []}),
            httpx.Response(200, json=grades_overview_payload),
        ]
    )

    grades = client.get_grade_overview()

    assert posted_params(route.calls[1].request)["userid"] == "63643"
    assert [g.courseid for g in grades] == [101, 102]
    assert grades[0].grade == "85.00"
    # A course with nothing graded yet reports a null grade, not an empty string.
    assert grades[1].grade == ""


@respx.mock
def test_get_grade_items_returns_the_first_users_items(
    client: MoodleClient, grade_items_payload: dict[str, Any]
) -> None:
    respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"userid": 63643, "functions": []}),
            httpx.Response(200, json=grade_items_payload),
        ]
    )

    items = client.get_grade_items(101)

    assert [i.itemname for i in items] == ["TP1", None, None]
    assert items[0].gradeformatted == "10.00"


@respx.mock
def test_an_aggregate_grade_row_is_named_by_its_item_type(
    client: MoodleClient, grade_items_payload: dict[str, Any]
) -> None:
    """A course-total or category row carries no itemname and no itemmodule: both null."""
    respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"userid": 63643, "functions": []}),
            httpx.Response(200, json=grade_items_payload),
        ]
    )

    items = client.get_grade_items(101)

    assert [i.label for i in items] == ["TP1", "Category subtotal", "Course total"]
    assert all(i.itemmodule is None for i in items[1:])
    # A null feedback reaches a str field and must not raise.
    assert items[-1].feedback == ""


@respx.mock
def test_get_grade_items_surfaces_missing_gradebook_permission(client: MoodleClient) -> None:
    """A per-course setting, not a token-wide block: get_grade_overview is unaffected."""
    respx.post(REST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"userid": 63643, "functions": []}),
            httpx.Response(
                200,
                json={
                    "exception": "moodle_exception",
                    "errorcode": "nopermissiontoviewgrades",
                    "message": "No se pueden ver las calificaciones.",
                },
            ),
        ]
    )

    with pytest.raises(MoodleAPIError) as excinfo:
        client.get_grade_items(101)
    assert excinfo.value.errorcode == "nopermissiontoviewgrades"


# -- course resolution -----------------------------------------------------------


@pytest.fixture
def resolving_client(courses_payload: dict[str, Any]) -> MoodleClient:
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=courses_payload))
    return MoodleClient(BASE_URL, "test-token")


@respx.mock
def test_resolve_course_by_id(resolving_client: MoodleClient) -> None:
    assert resolving_client.resolve_course("102").shortname == "I312 - 106931"


@respx.mock
def test_resolve_course_by_shortname_prefix(resolving_client: MoodleClient) -> None:
    """ "IOS460" must find "IOS460 - 123246"; nobody types the internal course number."""
    assert resolving_client.resolve_course("IOS460").id == 101


@respx.mock
def test_resolve_course_is_case_insensitive(resolving_client: MoodleClient) -> None:
    assert resolving_client.resolve_course("ios460").id == 101


@respx.mock
def test_resolve_course_reports_ambiguity(resolving_client: MoodleClient) -> None:
    with pytest.raises(MoodleError, match="ambiguous"):
        resolving_client.resolve_course("I3")


@respx.mock
def test_resolve_course_reports_unknown_reference(resolving_client: MoodleClient) -> None:
    with pytest.raises(MoodleError, match="No enrolled course"):
        resolving_client.resolve_course("NOPE999")


@respx.mock
def test_resolve_course_reports_unknown_id(resolving_client: MoodleClient) -> None:
    with pytest.raises(MoodleError, match="id 4242"):
        resolving_client.resolve_course("4242")


# -- calendar --------------------------------------------------------------------


@respx.mock
def test_get_calendar_events_defaults_to_upcoming(
    client: MoodleClient, calendar_payload: dict[str, Any]
) -> None:
    """No `since` means "from now", which is what makes the default answer upcoming."""
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=calendar_payload))

    events = client.get_calendar_events(limit=3)

    assert [e.id for e in events] == [990117, 990118, 990119]
    params = posted_params(route.calls[0].request)
    assert params["wsfunction"] == "core_calendar_get_action_events_by_timesort"
    assert int(params["timesortfrom"]) > 0
    assert "courseid" not in params


@respx.mock
def test_get_calendar_events_switches_function_for_one_course(
    client: MoodleClient, calendar_payload: dict[str, Any]
) -> None:
    """A course is narrowed server-side, not by filtering a campus-wide answer."""
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=calendar_payload))

    client.get_calendar_events(course_id=101, limit=3)

    params = posted_params(route.calls[0].request)
    assert params["wsfunction"] == "core_calendar_get_action_events_by_course"
    assert params["courseid"] == "101"


@respx.mock
def test_get_calendar_events_reads_a_site_event_with_no_course(
    client: MoodleClient, calendar_payload: dict[str, Any]
) -> None:
    """A null course, modulename and action must not raise; they mean "none"."""
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=calendar_payload))

    site_event = next(e for e in client.get_calendar_events(limit=3) if e.id == 990119)

    assert site_event.course is None
    assert site_event.course_id == 0
    assert site_event.modulename == ""
    assert site_event.action_name == ""
    assert site_event.actionable is False


@respx.mock
def test_get_calendar_events_pages_until_a_short_page(
    client: MoodleClient, calendar_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moodle's cursor is the last event id seen, not an offset.

    The page size is shrunk rather than the fixture grown: what is under test is that a
    full page triggers another request and a short one ends the loop, and that reads the
    same at two per page as at fifty.
    """
    monkeypatch.setattr("moodle_cli.client._CALENDAR_PAGE_SIZE", 2)
    first = {"events": calendar_payload["events"][:2], "lastid": 990118}
    second = {"events": calendar_payload["events"][2:], "lastid": 990119}
    route = respx.post(REST_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )

    events = client.get_calendar_events(limit=10)

    assert [e.id for e in events] == [990117, 990118, 990119]
    assert len(route.calls) == 2
    assert "aftereventid" not in posted_params(route.calls[0].request)
    assert posted_params(route.calls[1].request)["aftereventid"] == "990118"


@respx.mock
def test_get_calendar_events_falls_back_to_the_last_event_id(
    client: MoodleClient, calendar_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response without `lastid` must still advance, or the loop repeats one page."""
    monkeypatch.setattr("moodle_cli.client._CALENDAR_PAGE_SIZE", 2)
    first = {"events": calendar_payload["events"][:2]}
    second = {"events": calendar_payload["events"][2:]}
    route = respx.post(REST_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )

    client.get_calendar_events(limit=10)

    assert posted_params(route.calls[1].request)["aftereventid"] == "990118"


@respx.mock
def test_get_calendar_events_stops_at_the_limit(
    client: MoodleClient, calendar_payload: dict[str, Any]
) -> None:
    """The limit is a cap on what is fetched, not a filter applied afterwards."""
    route = respx.post(REST_URL).mock(
        return_value=httpx.Response(200, json={"events": calendar_payload["events"][:1]})
    )

    client.get_calendar_events(limit=1)

    assert len(route.calls) == 1
    assert posted_params(route.calls[0].request)["limitnum"] == "1"


@respx.mock
def test_get_calendar_events_sends_an_upper_bound_only_when_given(
    client: MoodleClient, calendar_payload: dict[str, Any]
) -> None:
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=calendar_payload))

    client.get_calendar_events(since=1_700_000_000, until=1_800_000_000, limit=3)

    params = posted_params(route.calls[0].request)
    assert params["timesortfrom"] == "1700000000"
    assert params["timesortto"] == "1800000000"


# -- caching ---------------------------------------------------------------------


@respx.mock
def test_list_courses_is_fetched_once_per_view_and_sort(
    client: MoodleClient, courses_payload: dict[str, Any]
) -> None:
    """The repeat that a single command makes costs one request, not two."""
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=courses_payload))

    client.list_courses(view="all")
    client.list_courses(view="all")
    client.resolve_course("IOS460")

    assert len(route.calls) == 2  # "all" once, plus all-including-hidden for resolution


@respx.mock
def test_list_courses_caches_each_view_separately(
    client: MoodleClient, courses_payload: dict[str, Any]
) -> None:
    """A cache keyed on nothing would answer `starred` with the `all` list."""
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=courses_payload))

    client.list_courses(view="all")
    client.list_courses(view="starred")

    assert len(route.calls) == 2
    assert posted_params(route.calls[1].request)["classification"] == "favourites"


@respx.mock
def test_site_info_is_fetched_once(client: MoodleClient) -> None:
    route = respx.post(REST_URL).mock(
        return_value=httpx.Response(200, json={"userid": 1, "functions": []})
    )

    client.get_site_info()
    client.get_site_info()

    assert len(route.calls) == 1


# -- batching --------------------------------------------------------------------


@respx.mock
def test_call_many_sends_one_request_when_the_campus_supports_it(
    client: MoodleClient,
) -> None:
    """The mobile app's multi-call endpoint is what avoids a round trip per activity."""

    def responder(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(
                200,
                json={"userid": 1, "functions": [{"name": "tool_mobile_call_external_functions"}]},
            )
        return httpx.Response(
            200,
            json={
                "responses": [
                    {"error": False, "data": json.dumps({"discussions": [{"id": 1}]})},
                    {"error": False, "data": json.dumps({"discussions": [{"id": 2}]})},
                ]
            },
        )

    route = respx.post(REST_URL).mock(side_effect=responder)

    results = client.call_many(
        [
            ("mod_forum_get_forum_discussions", {"forumid": 501}),
            ("mod_forum_get_forum_discussions", {"forumid": 502}),
        ]
    )

    assert results == [{"discussions": [{"id": 1}]}, {"discussions": [{"id": 2}]}]
    batched = [
        call
        for call in route.calls
        if "tool_mobile_call_external_functions" in call.request.content.decode()
    ]
    assert len(batched) == 1
    params = posted_params(batched[0].request)
    assert json.loads(params["requests[0][arguments]"]) == {"forumid": 501}


@respx.mock
def test_call_many_falls_back_to_one_request_each(client: MoodleClient) -> None:
    """A campus without the endpoint must still get an answer, not an error."""

    def responder(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(200, json={"userid": 1, "functions": []})
        return httpx.Response(200, json={"discussions": []})

    route = respx.post(REST_URL).mock(side_effect=responder)

    results = client.call_many(
        [
            ("mod_forum_get_forum_discussions", {"forumid": 501}),
            ("mod_forum_get_forum_discussions", {"forumid": 502}),
        ]
    )

    assert results == [{"discussions": []}, {"discussions": []}]
    forum_calls = [
        c for c in route.calls if "mod_forum_get_forum_discussions" in c.request.content.decode()
    ]
    assert len(forum_calls) == 2


@respx.mock
def test_call_many_skips_the_capability_check_for_a_single_call(client: MoodleClient) -> None:
    """One call is never worth a batch, so it must not cost a site-info lookup."""
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json={"discussions": []}))

    client.call_many([("mod_forum_get_forum_discussions", {"forumid": 501})])

    assert len(route.calls) == 1


@respx.mock
def test_call_many_raises_the_error_one_batched_call_reported(client: MoodleClient) -> None:
    """A failure inside a batch must raise the way it would have raised on its own."""

    def responder(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(
                200,
                json={"userid": 1, "functions": [{"name": "tool_mobile_call_external_functions"}]},
            )
        return httpx.Response(
            200,
            json={
                "responses": [
                    {"error": False, "data": json.dumps({"discussions": []})},
                    {
                        "error": True,
                        "exception": json.dumps(
                            {"errorcode": "nopermissions", "message": "Denied"}
                        ),
                    },
                ]
            },
        )

    respx.post(REST_URL).mock(side_effect=responder)

    with pytest.raises(MoodleAPIError) as caught:
        client.call_many(
            [
                ("mod_forum_get_forum_discussions", {"forumid": 501}),
                ("mod_forum_get_forum_discussions", {"forumid": 502}),
            ]
        )

    assert caught.value.errorcode == "nopermissions"


@respx.mock
def test_call_many_rejects_a_short_answer(client: MoodleClient) -> None:
    """Fewer responses than calls would silently pair results with the wrong request."""

    def responder(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(
                200,
                json={"userid": 1, "functions": [{"name": "tool_mobile_call_external_functions"}]},
            )
        return httpx.Response(200, json={"responses": [{"error": False, "data": "{}"}]})

    respx.post(REST_URL).mock(side_effect=responder)

    with pytest.raises(MoodleError):
        client.call_many([("a", {}), ("b", {})])


@respx.mock
def test_get_announcements_batches_one_call_per_news_forum(
    client: MoodleClient,
    courses_payload: dict[str, Any],
    forums_payload: list[dict[str, Any]],
    discussions_payload: dict[str, Any],
) -> None:
    """Announcements cost one call per course; over an enrolment that is the round trips."""
    second_news_forum = {**forums_payload[0], "id": 503, "course": 102}

    def responder(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(
                200,
                json={"userid": 1, "functions": [{"name": "tool_mobile_call_external_functions"}]},
            )
        if "core_course_get_enrolled_courses" in body:
            return httpx.Response(200, json=courses_payload)
        if "mod_forum_get_forums_by_courses" in body:
            return httpx.Response(200, json=[*forums_payload, second_news_forum])
        return httpx.Response(
            200,
            json={
                "responses": [
                    {"error": False, "data": json.dumps(discussions_payload)},
                    {"error": False, "data": json.dumps({"discussions": [], "warnings": []})},
                ]
            },
        )

    route = respx.post(REST_URL).mock(side_effect=responder)

    announcements = client.get_announcements()

    assert announcements
    per_forum = [
        call
        for call in route.calls
        if "wsfunction=mod_forum_get_forum_discussions" in call.request.content.decode()
    ]
    assert per_forum == []  # they went inside the batch instead


# -- updates ---------------------------------------------------------------------


@respx.mock
def test_get_course_updates_drops_activities_that_did_not_change(
    client: MoodleClient, course_updates_payload: dict[str, Any]
) -> None:
    """Moodle lists every module it checked; a quiet one is not news."""
    route = respx.post(REST_URL).mock(return_value=httpx.Response(200, json=course_updates_payload))

    updates = client.get_course_updates(101, since=1_773_000_000)

    assert [u.id for u in updates] == [2, 5, 4041]
    params = posted_params(route.calls[0].request)
    assert params["wsfunction"] == "core_course_get_updates_since"
    assert params["courseid"] == "101"
    assert params["since"] == "1773000000"


@respx.mock
def test_get_course_updates_reports_the_most_recent_area(
    client: MoodleClient, course_updates_payload: dict[str, Any]
) -> None:
    """An activity with two changed areas is dated by the later of the two."""
    respx.post(REST_URL).mock(return_value=httpx.Response(200, json=course_updates_payload))

    first = client.get_course_updates(101, since=1_773_000_000)[0]

    assert first.area_names == ["configuration", "contentfiles"]
    assert first.latest_epoch == 1773511440


@respx.mock
def test_get_course_updates_raises_on_a_warning(client: MoodleClient) -> None:
    """A partial answer must not be indistinguishable from a quiet course."""
    respx.post(REST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "instances": [],
                "warnings": [{"warningcode": "errorcoursecontentnotavailable", "message": "No"}],
            },
        )
    )

    with pytest.raises(MoodleAPIError):
        client.get_course_updates(101, since=1_773_000_000)
