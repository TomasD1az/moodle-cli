"""CLI tests, driven through the HTTP layer so the whole stack is exercised."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.main import get_command
from typer.testing import CliRunner

from moodle_cli.cli import app
from moodle_cli.models import epoch_to_datetime
from tests.conftest import BASE_URL, REST_URL, route_by_function

runner = CliRunner()

pytestmark = pytest.mark.usefixtures("configured_env")


@respx.mock
def test_courses_list_renders_a_table(courses_payload: dict[str, Any]) -> None:
    route_by_function(core_course_get_enrolled_courses_by_timeline_classification=courses_payload)

    result = runner.invoke(app, ["courses", "list"])

    assert result.exit_code == 0
    assert "IOS460 - 123246" in result.stdout
    assert "3 courses" in result.stdout


@respx.mock
def test_courses_list_json_is_machine_readable(courses_payload: dict[str, Any]) -> None:
    route_by_function(core_course_get_enrolled_courses_by_timeline_classification=courses_payload)

    result = runner.invoke(app, ["courses", "list", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert [c["shortname"] for c in data] == ["IOS460 - 123246", "I312 - 106931", "I310 - 106934"]


@respx.mock
def test_courses_list_rejects_an_invalid_view() -> None:
    result = runner.invoke(app, ["courses", "list", "--view", "bogus"])
    assert result.exit_code != 0


@respx.mock
def test_participants_hide_emails_by_default(
    courses_payload: dict[str, Any], participants_payload: list[dict[str, Any]]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_enrol_get_enrolled_users=participants_payload,
    )

    result = runner.invoke(app, ["course", "participants", "IOS460"])

    assert result.exit_code == 0
    assert "Ada Docente" in result.stdout
    assert "@example.edu" not in result.stdout


@respx.mock
def test_participants_json_omits_the_email_key_by_default(
    courses_payload: dict[str, Any], participants_payload: list[dict[str, Any]]
) -> None:
    """The privacy default has to hold in the machine-readable path too."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_enrol_get_enrolled_users=participants_payload,
    )

    result = runner.invoke(app, ["course", "participants", "IOS460", "--json"])

    data = json.loads(result.stdout)
    assert all("email" not in person for person in data)


@respx.mock
def test_participants_include_emails_on_request(
    courses_payload: dict[str, Any], participants_payload: list[dict[str, Any]]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_enrol_get_enrolled_users=participants_payload,
    )

    result = runner.invoke(app, ["course", "participants", "IOS460", "--emails", "--json"])

    data = json.loads(result.stdout)
    assert data[0]["email"] == "ada.docente@example.edu"


@respx.mock
def test_participants_filter_by_role(
    courses_payload: dict[str, Any], participants_payload: list[dict[str, Any]]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_enrol_get_enrolled_users=participants_payload,
    )

    result = runner.invoke(app, ["course", "participants", "IOS460", "--role", "student", "--json"])

    data = json.loads(result.stdout)
    assert [p["fullname"] for p in data] == ["Grace Estudiante"]


@respx.mock
def test_download_dry_run_writes_nothing(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "download", "IOS460", "--dry-run"])

    assert result.exit_code == 0
    assert "3 files" in result.stdout
    assert list(tmp_cwd.iterdir()) == []


@respx.mock
def test_download_writes_into_a_directory_named_after_the_course(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )
    respx.get(url__startswith=f"{BASE_URL}/webservice/pluginfile.php").mock(
        side_effect=lambda request: httpx.Response(200, content=b"x" * _expected_size(request))
    )

    result = runner.invoke(app, ["course", "download", "IOS460", "--type", "resource"])

    assert result.exit_code == 0
    written = list((tmp_cwd / "IOS460 - 123246").rglob("*.pdf"))
    assert [p.name for p in written] == ["Programa - Taller.pdf"]


def _expected_size(request: httpx.Request) -> int:
    """Serve the exact byte count the fixture declares, so validation passes."""
    return 171850 if "Programa" in str(request.url) else 2048


@respx.mock
def test_download_reports_failure_and_exits_nonzero(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    """A JSON error body served as 200 must surface as a failure, not a silent success."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )
    respx.get(url__startswith=f"{BASE_URL}/webservice/pluginfile.php").mock(
        return_value=httpx.Response(200, json={"errorcode": "missingparam", "error": "no token"})
    )

    result = runner.invoke(app, ["course", "download", "IOS460", "--type", "resource"])

    assert result.exit_code == 1
    assert "1 failed" in result.stdout
    assert list((tmp_cwd / "IOS460 - 123246").rglob("*.pdf")) == []


@respx.mock
def test_download_selects_an_exact_filename(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(
        app, ["course", "download", "IOS460", "--file", "Cap 1.pdf", "--dry-run"]
    )

    # Asserted on the count and size rather than the path: the dry-run table wraps long
    # destinations, so a filename can be split across lines.
    assert result.exit_code == 0
    assert "1 file," in result.stdout
    assert "2.0 KB" in result.stdout  # the size only "Cap 1.pdf" has


@respx.mock
def test_download_fails_loudly_on_an_unknown_filename(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    """A typo must be an error, not a silent zero-file success."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "download", "IOS460", "--file", "Cap 9.pdf"])

    assert result.exit_code == 1
    assert "no such file in this course" in result.output
    assert list(tmp_cwd.iterdir()) == []


@respx.mock
def test_download_distinguishes_a_filtered_out_name_from_a_typo(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    """The two failures need different fixes, so they must not read the same."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(
        app, ["course", "download", "IOS460", "--file", "Cap 1.pdf", "--section", "1"]
    )

    assert result.exit_code == 1
    assert "excluded by --section/--type" in result.output


@respx.mock
def test_download_selects_by_glob(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "download", "IOS460", "--match", "_Car*", "--dry-run"])

    assert result.exit_code == 0
    assert "1 file," in result.stdout
    assert "Carátula" in result.stdout


def _add_url_module(contents_payload: list[dict[str, Any]], module_id: int, url: str) -> None:
    contents_payload[0]["modules"].append(
        {
            "id": module_id,
            "name": f"Clase {module_id}",
            "instance": module_id,
            "modname": "url",
            "url": f"https://campus.example.edu/mod/url/view.php?id={module_id}",
            "visible": 1,
            "uservisible": True,
            "contents": [
                {
                    "type": "url",
                    "filename": f"Clase {module_id}",
                    "filepath": None,
                    "filesize": 0,
                    "fileurl": url,
                    "timemodified": 0,
                    "mimetype": None,
                    "isexternalfile": False,
                }
            ],
        }
    )


@respx.mock
def test_download_links_are_filtered_by_file_selector(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    """--file must narrow --links the same way it narrows regular files."""
    _add_url_module(contents_payload, 10, "https://docs.google.com/presentation/d/DOC1/edit")
    _add_url_module(contents_payload, 11, "https://docs.google.com/presentation/d/DOC2/edit")
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(
        app, ["course", "download", "IOS460", "--file", "Clase 10", "--links", "--dry-run"]
    )

    assert result.exit_code == 0
    assert "1 link" in result.stdout


@respx.mock
def test_api_error_is_reported_cleanly(courses_payload: dict[str, Any]) -> None:
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

    result = runner.invoke(app, ["courses", "list"])

    assert result.exit_code == 1
    assert "invalidtoken" in result.output


# -- links, announcements, assignments and grades ---------------------------------


@respx.mock
def test_contents_shows_a_url_modules_target_as_a_link(
    courses_payload: dict[str, Any], contents_payload: list[dict[str, Any]]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "contents", "IOS460"])

    assert result.exit_code == 0
    assert "1 link" in result.output
    assert "Slack de la Materia" in result.output
    assert "https://slack.example.com/join" in result.output


@respx.mock
def test_contents_shows_the_section_summary_and_a_labels_full_text(
    courses_payload: dict[str, Any], contents_payload: list[dict[str, Any]]
) -> None:
    """A section's date range and a label's full body live outside ``name``.

    ``name`` is a preview Moodle itself truncates for label modules; the section's date
    range lives in ``summary``, a separate field ``course contents`` used to drop
    entirely. Both are what a student actually needs to know what is due this week.
    """
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "contents", "IOS460"])

    assert result.exit_code == 0
    assert "1 de marzo - 7 de marzo" in result.output
    assert "Repasar el apunte de la unidad 2." in result.output


@respx.mock
def test_contents_appends_a_non_labels_description_below_its_real_title(
    courses_payload: dict[str, Any], contents_payload: list[dict[str, Any]]
) -> None:
    """A resource, unlike a label, has a real title — the description is extra context.

    Both must show: the title identifies which file this is, and a description a teacher
    attached (e.g. "there's a newer version") is easy to miss if only the title prints.
    """
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "contents", "IOS460"])

    assert result.exit_code == 0
    assert "Ejercicios Unidad 2 v1" in result.output
    assert "Hay una versión más nueva en la carpeta de la semana 5." in result.output


# -- search ---------------------------------------------------------------------------


@respx.mock
def test_courses_search_renders_a_table(
    courses_payload: dict[str, Any], contents_payload: list[dict[str, Any]]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["courses", "search", "Material Bibliogr"])

    # Asserted on the short filename rather than the activity name or a URL: the table
    # ellipsizes the activity column and folds long links across lines.
    assert result.exit_code == 0
    assert "3 results" in result.stdout
    assert "module" in result.stdout
    assert "Cap 1.pdf" in result.stdout


@respx.mock
def test_courses_search_json_matches_the_mcp_payload(
    courses_payload: dict[str, Any], contents_payload: list[dict[str, Any]]
) -> None:
    """One search, one shape: the table and the tool must not diverge on what was found."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["courses", "search", "Cap 1", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["truncated"] is False
    assert [r["match"] for r in data["results"]] == ["file"] * 3
    assert all(r["files"] == ["Cap 1.pdf"] for r in data["results"])
    assert all(r["section_number"] == 0 for r in data["results"])


# -- announcements --------------------------------------------------------------------


@respx.mock
def test_announcements_strip_html_and_show_the_newest_first(
    courses_payload: dict[str, Any],
    forums_payload: list[dict[str, Any]],
    discussions_payload: dict[str, Any],
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_forum_get_forums_by_courses=forums_payload,
        mod_forum_get_forum_discussions=discussions_payload,
    )

    result = runner.invoke(app, ["course", "announcements", "IOS460"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "Cambio de aula para la clase del jueves (pinned)"
    assert "<strong>" not in result.output
    assert "S004" in result.output
    assert "&iacute;" not in result.output
    assert "  Traer la guía de ejercicios." in result.output


@respx.mock
def test_announcements_json_carries_plain_text_and_a_full_timestamp(
    courses_payload: dict[str, Any],
    forums_payload: list[dict[str, Any]],
    discussions_payload: dict[str, Any],
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_forum_get_forums_by_courses=forums_payload,
        mod_forum_get_forum_discussions=discussions_payload,
    )

    result = runner.invoke(app, ["course", "announcements", "IOS460", "--json"])

    assert result.exit_code == 0
    newest = json.loads(result.stdout)[0]
    assert newest["course"] == "IOS460 - 123246"
    assert "<p>" not in newest["message"]
    assert newest["message"].startswith("Estimados,\n")
    posted_at = epoch_to_datetime(1783108601)
    assert posted_at is not None
    assert newest["posted_at"] == posted_at.isoformat()


@respx.mock
def test_announcements_report_nothing_for_a_course_whose_forums_are_all_general(
    courses_payload: dict[str, Any],
    forums_payload: list[dict[str, Any]],
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_forum_get_forums_by_courses=[f for f in forums_payload if f["type"] != "news"],
    )

    result = runner.invoke(app, ["course", "announcements", "IOS460"])

    assert result.exit_code == 0
    assert "No announcements" in result.output


@respx.mock
def test_assignments_lists_due_dates(
    courses_payload: dict[str, Any], assignments_payload: dict[str, Any]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_assign_get_assignments=assignments_payload,
    )

    result = runner.invoke(app, ["course", "assignments", "IOS460"])

    assert result.exit_code == 0
    assert "Actividad semana 1" in result.output
    assert "40393" in result.output
    assert "2026-03-11" in result.output


@respx.mock
def test_a_scale_graded_assignment_shows_no_numeric_maximum(
    courses_payload: dict[str, Any], assignments_payload: dict[str, Any]
) -> None:
    """A negative grade names a scale; printed as a number it reads as a maximum of -52."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_assign_get_assignments=assignments_payload,
    )

    result = runner.invoke(app, ["course", "assignments", "IOS460"])

    assert result.exit_code == 0
    assert "-52" not in result.output
    assert "scale" in result.output


@respx.mock
def test_courses_assignments_spans_every_course_by_due_date(
    courses_payload: dict[str, Any], assignments_payload: dict[str, Any]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_assign_get_assignments=assignments_payload,
    )

    result = runner.invoke(app, ["courses", "assignments"])

    assert result.exit_code == 0
    assert "IOS460 - 123246" in result.output
    # The earlier deadline comes first, whatever order the campus listed them in.
    assert result.output.index("2026-02-25") < result.output.index("2026-03-11")


@respx.mock
def test_assignment_status_decodes_the_grade_and_lists_submitted_files(
    submission_status_payload: dict[str, Any],
) -> None:
    route_by_function(mod_assign_get_submission_status=submission_status_payload)

    result = runner.invoke(app, ["course", "assignment-status", "40393"])

    assert result.exit_code == 0
    assert "submitted: yes" in result.output
    assert "graded: yes" in result.output
    assert "90.00\xa0/\xa0100.00" in result.output  # &nbsp; decoded to U+00A0, not literal
    assert "Entrega - Semana 1.pdf" in result.output


@respx.mock
def test_a_null_optional_field_fails_as_a_message_not_a_traceback(
    submission_status_payload: dict[str, Any],
) -> None:
    """A null in an optional field must not reach the user as a validation traceback."""
    submission_status_payload["lastattempt"].update(gradingstatus=None, extensionduedate=None)
    submission_status_payload["feedback"]["gradefordisplay"] = None
    route_by_function(mod_assign_get_submission_status=submission_status_payload)

    result = runner.invoke(app, ["course", "assignment-status", "40393"])

    assert result.exit_code == 0
    assert result.exception is None
    assert "graded: no" in result.output


@respx.mock
def test_an_extension_date_reads_in_the_same_zone_as_every_other_date(
    submission_status_payload: dict[str, Any],
) -> None:
    """2026-03-12 01:00Z is still the 11th locally, which is the day the campus shows."""
    submission_status_payload["lastattempt"]["extensionduedate"] = 1773277200
    route_by_function(mod_assign_get_submission_status=submission_status_payload)

    result = runner.invoke(app, ["course", "assignment-status", "40393"])

    assert result.exit_code == 0
    assert "extension until: 2026-03-11" in result.output


@respx.mock
def test_quizzes_lists_close_dates_and_attempt_limits(
    courses_payload: dict[str, Any], quizzes_payload: dict[str, Any]
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_quiz_get_quizzes_by_courses=quizzes_payload,
    )

    result = runner.invoke(app, ["course", "quizzes", "IOS460"])

    assert result.exit_code == 0
    assert "Actividad semana 2" in result.output
    assert "42628" in result.output
    assert "2026-03-19" in result.output
    assert "10.0" in result.output


@respx.mock
def test_quizzes_render_a_zero_attempt_limit_as_unlimited(
    courses_payload: dict[str, Any], quizzes_payload: dict[str, Any]
) -> None:
    quizzes_payload["quizzes"][0]["attempts"] = 0
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        mod_quiz_get_quizzes_by_courses=quizzes_payload,
    )

    result = runner.invoke(app, ["course", "quizzes", "IOS460"])

    assert result.exit_code == 0
    assert "unlimited" in result.output


@respx.mock
def test_quiz_status_reports_attempts_and_grade(
    quiz_attempts_payload: dict[str, Any],
    quiz_best_grade_payload: dict[str, Any],
    quizzes_payload: dict[str, Any],
) -> None:
    route_by_function(
        mod_quiz_get_user_attempts=quiz_attempts_payload,
        mod_quiz_get_user_best_grade=quiz_best_grade_payload,
        mod_quiz_get_quizzes_by_courses=quizzes_payload,
    )

    result = runner.invoke(app, ["course", "quiz-status", "42628"])

    assert result.exit_code == 0
    assert "attempts used: 1" in result.output
    assert "last attempt: finished" in result.output
    assert "grade: 6.925 / 10.0 (pass: 4.0)" in result.output


@respx.mock
def test_quiz_status_reports_no_attempts_before_the_quiz_is_taken() -> None:
    route_by_function(
        mod_quiz_get_user_attempts={"attempts": [], "warnings": []},
        mod_quiz_get_user_best_grade={"hasgrade": False, "warnings": []},
    )

    result = runner.invoke(app, ["course", "quiz-status", "42628"])

    assert result.exit_code == 0
    assert "attempts used: 0" in result.output
    assert "last attempt" not in result.output


@respx.mock
def test_quiz_status_reports_an_attempt_still_in_progress(
    quiz_attempts_payload: dict[str, Any],
) -> None:
    """An unfinished attempt is a state an assignment's submitted/not binary cannot hold."""
    quiz_attempts_payload["attempts"][0]["state"] = "inprogress"
    quiz_attempts_payload["attempts"][0]["timefinish"] = 0
    route_by_function(
        mod_quiz_get_user_attempts=quiz_attempts_payload,
        mod_quiz_get_user_best_grade={"hasgrade": False, "warnings": []},
    )

    result = runner.invoke(app, ["course", "quiz-status", "42628"])

    assert result.exit_code == 0
    assert "attempts used: 1" in result.output
    assert "last attempt: inprogress" in result.output


@respx.mock
def test_quiz_status_reports_a_grade_as_unavailable_rather_than_ungraded(
    quiz_attempts_payload: dict[str, Any],
) -> None:
    """A finished attempt with no readable grade may still be graded, only hidden."""
    route_by_function(
        mod_quiz_get_user_attempts=quiz_attempts_payload,
        mod_quiz_get_user_best_grade={"hasgrade": False, "warnings": []},
    )

    result = runner.invoke(app, ["course", "quiz-status", "42628"])

    assert result.exit_code == 0
    assert "attempts used: 1" in result.output
    assert "grade: not available" in result.output


@respx.mock
def test_quiz_status_omits_the_pass_mark_when_the_grade_is_unavailable() -> None:
    """A pass mark alone tells the student nothing about how they did."""
    route_by_function(
        mod_quiz_get_user_attempts={"attempts": [], "warnings": []},
        mod_quiz_get_user_best_grade={"hasgrade": False, "gradetopass": 60, "warnings": []},
    )

    result = runner.invoke(app, ["course", "quiz-status", "42628"])

    assert result.exit_code == 0
    assert "60" not in result.output
    assert "pass" not in result.output


@respx.mock
def test_courses_grades_shows_the_summary_across_courses(
    courses_payload: dict[str, Any],
    grades_overview_payload: dict[str, Any],
    site_info_payload: dict[str, Any],
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_webservice_get_site_info=site_info_payload,
        gradereport_overview_get_course_grades=grades_overview_payload,
    )

    result = runner.invoke(app, ["courses", "grades"])

    assert result.exit_code == 0
    assert "IOS460 - 123246" in result.output
    assert "85.00" in result.output


@respx.mock
def test_a_course_hidden_from_the_dashboard_is_still_named(
    courses_payload: dict[str, Any],
    hidden_course: dict[str, Any],
    grades_overview_payload: dict[str, Any],
    site_info_payload: dict[str, Any],
) -> None:
    """The overview covers every enrolment, so a narrower course lookup leaves a bare id."""
    grades_overview_payload["grades"].append({"courseid": 104, "grade": "70.00"})
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=lambda body: (
            {"courses": [*courses_payload["courses"], hidden_course]}
            if "classification=allincludinghidden" in body
            else courses_payload
        ),
        core_webservice_get_site_info=site_info_payload,
        gradereport_overview_get_course_grades=grades_overview_payload,
    )

    result = runner.invoke(app, ["courses", "grades"])

    assert result.exit_code == 0
    assert "I204 - 101313" in result.output
    assert "104" not in result.output


@respx.mock
def test_a_shortname_with_brackets_survives_the_table(
    courses_payload: dict[str, Any],
    grades_overview_payload: dict[str, Any],
    site_info_payload: dict[str, Any],
) -> None:
    """Server-controlled text is data, not Rich markup: an unescaped [tag] is swallowed."""
    courses_payload["courses"][0]["shortname"] = "IOS460 [grupo 2]"
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_webservice_get_site_info=site_info_payload,
        gradereport_overview_get_course_grades=grades_overview_payload,
    )

    result = runner.invoke(app, ["courses", "grades"])

    assert result.exit_code == 0
    assert "IOS460 [grupo 2]" in result.output


@respx.mock
def test_a_single_course_is_counted_in_the_singular(
    courses_payload: dict[str, Any],
    grades_overview_payload: dict[str, Any],
    site_info_payload: dict[str, Any],
) -> None:
    grades_overview_payload["grades"] = grades_overview_payload["grades"][:1]
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_webservice_get_site_info=site_info_payload,
        gradereport_overview_get_course_grades=grades_overview_payload,
    )

    result = runner.invoke(app, ["courses", "grades"])

    assert result.exit_code == 0
    assert "Grade summary (1 course)" in result.output


@respx.mock
def test_course_grades_shows_the_per_item_breakdown(
    courses_payload: dict[str, Any],
    grade_items_payload: dict[str, Any],
    site_info_payload: dict[str, Any],
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_webservice_get_site_info=site_info_payload,
        gradereport_user_get_grade_items=grade_items_payload,
    )

    result = runner.invoke(app, ["course", "grades", "IOS460"])

    assert result.exit_code == 0
    assert "TP1" in result.output
    assert "10.00" in result.output


@respx.mock
def test_an_aggregate_grade_row_is_named_by_its_item_type(
    courses_payload: dict[str, Any],
    grade_items_payload: dict[str, Any],
    site_info_payload: dict[str, Any],
) -> None:
    """The course total arrives with a null itemname; rendered as "-" it reads as blank."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_webservice_get_site_info=site_info_payload,
        gradereport_user_get_grade_items=grade_items_payload,
    )

    result = runner.invoke(app, ["course", "grades", "IOS460"])

    assert result.exit_code == 0
    assert "Course total" in result.output
    assert "Category subtotal" in result.output


@respx.mock
def test_course_grades_fails_loudly_without_gradebook_permission(
    courses_payload: dict[str, Any],
    site_info_payload: dict[str, Any],
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_webservice_get_site_info=site_info_payload,
        gradereport_user_get_grade_items={
            "exception": "moodle_exception",
            "errorcode": "nopermissiontoviewgrades",
            "message": "No se pueden ver las calificaciones.",
        },
    )

    result = runner.invoke(app, ["course", "grades", "IOS460"])

    assert result.exit_code == 1
    assert "nopermissiontoviewgrades" in result.output


# -- the README's promise about --json -----------------------------------------------

#: Core commands that act rather than answer, so they stream progress instead of JSON.
#: The README names exactly these; a new command landing without `--json` has to either
#: grow one or be added here and there in the same change.
CORE_COMMANDS_WITHOUT_JSON = {
    ("auth", "login"),
    ("auth", "status"),
    ("auth", "logout"),
    ("course", "download"),
    ("courses", "download"),
}

CORE_GROUPS = ("auth", "courses", "course", "plugins")


def test_only_the_documented_commands_lack_json_output() -> None:
    """The README promises `--json` on every command that answers a question.

    Read off the command tree rather than out of `--help`: the rendered help is Rich's
    to lay out, and how it wraps depends on the terminal it believes it has, so parsing
    it makes this assert something different on a developer's machine than on a runner.
    Plugin groups are out of scope; their own docs make their own promises.
    """
    groups = get_command(app).commands  # type: ignore[attr-defined]

    missing: set[tuple[str, str]] = set()
    for group_name in CORE_GROUPS:
        assert group_name in groups, f"{group_name} is not a command group"
        for command_name, command in groups[group_name].commands.items():
            flags = {opt for param in command.params for opt in param.opts}
            if "--json" not in flags:
                missing.add((group_name, command_name))

    assert missing == CORE_COMMANDS_WITHOUT_JSON


# -- calendar --------------------------------------------------------------------


@respx.mock
def test_courses_calendar_renders_deadlines_with_their_hour(
    courses_payload: dict[str, Any], calendar_payload: dict[str, Any]
) -> None:
    """A calendar prints the minute; every other table here prints the day."""
    route_by_function(
        core_calendar_get_action_events_by_timesort=calendar_payload,
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
    )

    result = runner.invoke(app, ["courses", "calendar"])

    assert result.exit_code == 0
    assert "Actividad semana 2" in result.output
    assert "2026-03-19 18:00" in result.output
    assert "IOS460 - 123246" in result.output
    assert "next 14 days" in result.output


@respx.mock
def test_courses_calendar_labels_a_site_event_with_no_course(
    courses_payload: dict[str, Any], calendar_payload: dict[str, Any]
) -> None:
    """An event belonging to no course must render, not crash on a missing shortname."""
    route_by_function(
        core_calendar_get_action_events_by_timesort=calendar_payload,
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
    )

    result = runner.invoke(app, ["courses", "calendar"])

    assert result.exit_code == 0
    assert "Charla" in result.output


@respx.mock
def test_courses_calendar_overdue_asks_for_the_window_already_passed(
    courses_payload: dict[str, Any], calendar_payload: dict[str, Any]
) -> None:
    route = route_by_function(
        core_calendar_get_action_events_by_timesort=calendar_payload,
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
    )

    result = runner.invoke(app, ["courses", "calendar", "--overdue", "--days", "7"])

    assert result.exit_code == 0
    assert "past 7 days" in result.output
    params = {
        k: v
        for k, v in (
            pair.split("=", 1)
            for pair in route.calls[0].request.content.decode().split("&")
            if "=" in pair
        )
    }
    assert int(params["timesortto"]) >= int(params["timesortfrom"])


@respx.mock
def test_courses_calendar_json_names_the_course_and_keeps_the_offset(
    courses_payload: dict[str, Any], calendar_payload: dict[str, Any]
) -> None:
    route_by_function(
        core_calendar_get_action_events_by_timesort=calendar_payload,
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
    )

    result = runner.invoke(app, ["courses", "calendar", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    quiz_event = next(e for e in data if e["id"] == 990117)
    assert quiz_event["course"] == "IOS460 - 123246"
    assert quiz_event["activity"] == "quiz"
    assert quiz_event["instance_id"] == 42628
    assert quiz_event["due_at"] == epoch_to_datetime(1773954000).isoformat()  # type: ignore[union-attr]
    assert data[1]["overdue"] is True
    assert data[1]["actionable"] is False


@respx.mock
def test_courses_calendar_reports_an_empty_window_plainly(
    courses_payload: dict[str, Any],
) -> None:
    route_by_function(
        core_calendar_get_action_events_by_timesort={"events": []},
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
    )

    result = runner.invoke(app, ["courses", "calendar"])

    assert result.exit_code == 0
    assert "Nothing due" in result.output


@respx.mock
def test_course_calendar_narrows_server_side(
    courses_payload: dict[str, Any], calendar_payload: dict[str, Any]
) -> None:
    """One course goes through the by-course function, not a filtered campus sweep."""
    route = route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_calendar_get_action_events_by_course=calendar_payload,
    )

    result = runner.invoke(app, ["course", "calendar", "IOS460"])

    assert result.exit_code == 0
    assert "IOS460 - 123246" in result.output
    bodies = [call.request.content.decode() for call in route.calls]
    assert any(
        "core_calendar_get_action_events_by_course" in body and "courseid=101" in body
        for body in bodies
    )


@respx.mock
def test_course_calendar_rejects_a_zero_day_window(courses_payload: dict[str, Any]) -> None:
    """`--days 0` is a window with no width, which Typer rejects before any request."""
    route_by_function(core_course_get_enrolled_courses_by_timeline_classification=courses_payload)

    result = runner.invoke(app, ["course", "calendar", "IOS460", "--days", "0"])

    assert result.exit_code == 2


# -- downloading every course ----------------------------------------------------


@respx.mock
def test_courses_download_makes_one_directory_per_course(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    """Every enrolled course is swept, each into a subdirectory of its own."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )
    respx.get(url__startswith=f"{BASE_URL}/webservice/pluginfile.php").mock(
        side_effect=lambda request: httpx.Response(200, content=b"x" * _expected_size(request))
    )

    result = runner.invoke(app, ["courses", "download", "--type", "resource"])

    assert result.exit_code == 0
    directories = sorted(p.name for p in tmp_cwd.iterdir() if p.is_dir())
    assert directories == ["I310 - 106934", "I312 - 106931", "IOS460 - 123246"]
    assert "across 3 courses" in result.stdout


@respx.mock
def test_courses_download_honours_an_output_parent(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["courses", "download", "-o", "campus", "--dry-run"])

    assert result.exit_code == 0
    assert "campus/IOS460 - 123246" in result.stdout.replace("\\", "/")
    assert list(tmp_cwd.iterdir()) == []


@respx.mock
def test_courses_download_skips_a_course_it_cannot_read(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    """One unreadable course must not end a sweep of the whole enrolment."""
    answers = itertools.chain(
        [httpx.Response(200, json={"errorcode": "nopermissions", "message": "Denied"})],
        itertools.repeat(httpx.Response(200, json=contents_payload)),
    )

    def responder(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "core_course_get_contents" in body:
            return next(answers)
        return httpx.Response(200, json=courses_payload)

    respx.post(REST_URL).mock(side_effect=responder)

    result = runner.invoke(app, ["courses", "download", "--dry-run"])

    assert result.exit_code == 0
    assert "skip" in result.output
    assert "IOS460 - 123246" not in result.stdout
    assert "I312 - 106931" in result.stdout


@respx.mock
def test_courses_download_reports_an_empty_sweep(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    tmp_cwd: Path,
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["courses", "download", "--match", "*.nothing"])

    assert result.exit_code == 0
    assert "No matching files in any course" in result.stdout


@pytest.mark.parametrize("flag", ["--file", "--section"])
def test_courses_download_rejects_per_course_selectors(flag: str) -> None:
    """A filename or a section number identifies something inside one course only."""
    result = runner.invoke(app, ["courses", "download", flag, "1"])

    assert result.exit_code == 2


# -- updates ---------------------------------------------------------------------


@respx.mock
def test_course_updates_names_activities_from_the_contents(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    course_updates_payload: dict[str, Any],
) -> None:
    """The endpoint answers with course-module ids; the names come from the contents."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_updates_since=course_updates_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "updates", "IOS460"])

    assert result.exit_code == 0
    assert "Programa de la materia" in result.output
    assert "contentfiles" in result.output
    assert "2 activities changed" not in result.output  # three changed, one was quiet
    assert "3 activities changed" in result.output


@respx.mock
def test_course_updates_falls_back_to_the_cmid_when_unnamed(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    course_updates_payload: dict[str, Any],
) -> None:
    """An activity can change and still not appear in the contents you may read."""
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_updates_since=course_updates_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "updates", "IOS460"])

    assert result.exit_code == 0
    assert "cmid 4041" in result.output


@respx.mock
def test_course_updates_skips_the_contents_call_when_nothing_changed(
    courses_payload: dict[str, Any],
) -> None:
    """A quiet course costs one call, not two."""
    route = route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_updates_since={"instances": [], "warnings": []},
    )

    result = runner.invoke(app, ["course", "updates", "IOS460"])

    assert result.exit_code == 0
    assert "Nothing changed" in result.output
    bodies = [call.request.content.decode() for call in route.calls]
    assert not any("core_course_get_contents" in body for body in bodies)


@respx.mock
def test_course_updates_json_orders_newest_first(
    courses_payload: dict[str, Any],
    contents_payload: list[dict[str, Any]],
    course_updates_payload: dict[str, Any],
) -> None:
    route_by_function(
        core_course_get_enrolled_courses_by_timeline_classification=courses_payload,
        core_course_get_updates_since=course_updates_payload,
        core_course_get_contents=contents_payload,
    )

    result = runner.invoke(app, ["course", "updates", "IOS460", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert [row["cmid"] for row in data] == [2, 5, 4041]
    assert data[0]["activity"] == "Programa de la materia"
    assert data[0]["changed"] == ["configuration", "contentfiles"]
    assert data[0]["changed_at"] == epoch_to_datetime(1773511440).isoformat()  # type: ignore[union-attr]
    assert data[2]["activity"] is None


@pytest.mark.parametrize(
    ("count", "noun", "expected"),
    [
        (1, "course", "course"),
        (2, "course", "courses"),
        (2, "day", "days"),
        (1, "activity", "activity"),
        (3, "activity", "activities"),
    ],
)
def test_plural_handles_the_units_this_tool_counts(count: int, noun: str, expected: str) -> None:
    """ "3 activitys" reads as a broken command rather than as a count."""
    from moodle_cli.cli import _plural

    assert _plural(count, noun) == expected


# -- quiz review -----------------------------------------------------------------


@respx.mock
def test_quiz_review_lists_questions_with_their_marks(
    quiz_attempt_review_payload: dict[str, Any],
) -> None:
    route_by_function(mod_quiz_get_attempt_review=quiz_attempt_review_payload)

    result = runner.invoke(app, ["course", "quiz-review", "883899"])

    assert result.exit_code == 0
    assert "attempt 1 (finished)" in result.output
    assert "grade: 6.93" in result.output
    assert "multichoice" in result.output
    assert "1 / 1" in result.output
    assert "3 questions" in result.output


@respx.mock
def test_quiz_review_prints_question_text_only_when_asked(
    quiz_attempt_review_payload: dict[str, Any],
) -> None:
    """The rendered HTML is long; the table stays scannable without it."""
    route_by_function(mod_quiz_get_attempt_review=quiz_attempt_review_payload)

    without = runner.invoke(app, ["course", "quiz-review", "883899"])
    with_text = runner.invoke(app, ["course", "quiz-review", "883899", "--questions"])

    assert "estructura de repetición" not in without.output
    assert "estructura de repetición" in with_text.output
    assert "<div" not in with_text.output


@respx.mock
def test_quiz_review_says_so_when_review_is_not_permitted(
    quiz_attempt_review_payload: dict[str, Any],
) -> None:
    """No questions is a review option, not a failure."""
    route_by_function(mod_quiz_get_attempt_review={**quiz_attempt_review_payload, "questions": []})

    result = runner.invoke(app, ["course", "quiz-review", "883899"])

    assert result.exit_code == 0
    assert "may not allow review yet" in result.output


@respx.mock
def test_quiz_review_warns_when_marks_are_hidden(
    quiz_attempt_review_payload: dict[str, Any],
) -> None:
    """An attempt with questions and no marks is a real state."""
    hidden = [
        {k: v for k, v in q.items() if k not in {"mark", "maxmark"}}
        for q in quiz_attempt_review_payload["questions"]
    ]
    route_by_function(
        mod_quiz_get_attempt_review={**quiz_attempt_review_payload, "questions": hidden}
    )

    result = runner.invoke(app, ["course", "quiz-review", "883899"])

    assert result.exit_code == 0
    assert "Marks are hidden" in result.output


@respx.mock
def test_quiz_review_json_reports_an_ungraded_question_as_null(
    quiz_attempt_review_payload: dict[str, Any],
) -> None:
    route_by_function(mod_quiz_get_attempt_review=quiz_attempt_review_payload)

    result = runner.invoke(app, ["course", "quiz-review", "883899", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["state"] == "finished"
    assert data["questions"][0]["mark"] == 1.0
    assert data["questions"][1]["flagged"] is True
    assert data["questions"][2]["mark"] is None


@respx.mock
def test_quiz_status_points_at_the_attempt_ids(
    quiz_attempts_payload: dict[str, Any],
    quiz_best_grade_payload: dict[str, Any],
    quizzes_payload: dict[str, Any],
) -> None:
    """An attempt count with no ids leaves nothing to review."""
    route_by_function(
        mod_quiz_get_user_attempts=quiz_attempts_payload,
        mod_quiz_get_user_best_grade=quiz_best_grade_payload,
        mod_quiz_get_quizzes_by_courses=quizzes_payload,
    )

    result = runner.invoke(app, ["course", "quiz-status", "42628"])

    assert result.exit_code == 0
    assert "attempt ids: 883899" in result.output
