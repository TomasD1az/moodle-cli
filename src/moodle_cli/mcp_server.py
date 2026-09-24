"""MCP server exposing the campus to an agent.

Two rules shape every signature here:

1. **No file contents cross the wire.** Downloads land on disk and the tool returns a
   manifest of paths. A course can hold tens of megabytes of PDFs; returning bytes or
   extracted text would blow up the caller's context for no benefit.
2. **Emails are opt-in.** The participants endpoint returns an address for every enrolled
   person. That is not something to place in a model's context by default.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

from moodle_cli.client import MoodleClient
from moodle_cli.downloads import (
    DownloadStatus,
    download_file,
    iter_course_files,
    plan_downloads,
    sanitize,
)
from moodle_cli.errors import MoodleError
from moodle_cli.models import Module, html_to_text
from moodle_cli.plugins import register_tools
from moodle_cli.search import search_contents
from moodle_cli.session import open_client

View = Literal["all", "all-including-hidden", "in-progress", "future", "past", "starred", "hidden"]
Sort = Literal["name", "last-accessed"]

mcp = FastMCP("moodle-campus")


def _course_names(client: MoodleClient) -> dict[int, str]:
    """Shortname per course id, for labelling rows that carry only an id.

    Includes courses hidden from the dashboard: the grade and assignment endpoints answer
    for every enrolment, so anything narrower leaves a bare id in the output.
    """
    return {c.id: c.shortname for c in client.list_courses(view="all-including-hidden")}


@mcp.tool()
def list_courses(view: View = "all", sort: Sort = "name") -> list[dict[str, Any]]:
    """List the enrolled courses.

    Note: this campus never sets course end dates, so the time-based views are not
    meaningful here. 'in-progress' returns every course and 'past'/'future' return none.
    Use 'all' or 'starred', and read `starts_at` to judge recency.

    `starts_at` is a full timestamp carrying its UTC offset, as every instant here is: a
    bare date is read in whichever zone the tool runs in, which names the wrong day for
    anyone reading from a different zone than the campus.
    """
    client = open_client()
    with client:
        return [
            {
                "id": c.id,
                "shortname": c.shortname,
                "fullname": c.fullname,
                "category": c.category,
                "starred": c.isfavourite,
                "starts_at": c.started_at.isoformat() if c.started_at else None,
                "url": c.viewurl,
            }
            for c in client.list_courses(view=view, sort=sort)
        ]


def _module_payload(module: Module) -> dict[str, Any]:
    """A module's display name plus any teacher-written instructions attached to it.

    A ``label`` module carries its entire text in ``description``; ``name`` is a preview
    Moodle itself truncates to about 50 characters, so ``description`` replaces it there.
    Other module types keep a real title in ``name`` and, when a teacher enabled "show
    description", pair it with instructions Moodle displays below the title on the course
    page (e.g. a submission window, or which of two file versions is current) — reported
    separately so a real title is never dropped in favor of added text.
    """
    is_label = module.modname == "label"
    name = (module.description_text if is_label else module.name) or module.name
    payload = {
        "name": name,
        "type": module.modname,
        "url": module.url,
        "files": [
            {"filename": f.filename, "size": f.filesize, "mimetype": f.mimetype}
            for f in module.files
        ],
        "links": [{"name": link.filename, "url": link.fileurl} for link in module.links],
    }
    if not is_label and module.description_text and module.description_text != name:
        payload["description"] = module.description_text
    return payload


@mcp.tool()
def get_course_contents(course: str) -> dict[str, Any]:
    """Show a course's sections, activities, available files and external links.

    `course` accepts a numeric id or a shortname prefix such as "IOS460".
    File entries list names, sizes and MIME types; to fetch them, call
    download_course_files, optionally narrowing by section number or module type.
    Link entries (a "url" module's actual destination, e.g. a recorded-class or
    external-site link) are informational only — nothing downloads them.
    A section's `summary` and a module's `description` carry free-text instructions a
    teacher wrote (a due window, which file version is current, etc.) — read them, they
    are often the only place that information appears.
    """
    client = open_client()
    with client:
        resolved = client.resolve_course(course)
        sections = client.get_course_contents(resolved.id)

    return {
        "course": {
            "id": resolved.id,
            "shortname": resolved.shortname,
            "fullname": resolved.fullname,
        },
        "sections": [
            {
                "number": section.section,
                "name": section.name,
                "summary": section.summary_text,
                "modules": [_module_payload(module) for module in section.modules],
            }
            for section in sections
        ],
    }


@mcp.tool()
def search_courses(query: str) -> dict[str, Any]:
    """Search section, activity, file and link names across every enrolled course.

    Case-insensitive substring match on names and on link destinations — it does not read
    file contents. One call here replaces one get_course_contents call per course, which
    matters when the question is "which course has X" rather than "what's in course X".

    Every record names what the query hit under `match`: "section", "module", "file" or
    "link". A "section" record carries no module fields. On a "module" hit, `files` and
    `links` are the activity's complete contents; on a "file" or "link" hit they hold only
    the entries that matched, so an empty list never means "this activity is empty".
    `section_number` feeds download_course_files' `sections` selector directly.

    `truncated` is true when more matched than the response carries: narrow the query.
    """
    client = open_client()
    with client:
        return search_contents(client, query).as_payload()


@mcp.tool()
def download_course_files(
    course: str,
    output_dir: str | None = None,
    sections: list[int] | None = None,
    module_types: list[str] | None = None,
    files: list[str] | None = None,
    match: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Download a course's files to disk, mirroring its section structure.

    Returns a manifest of file paths and sizes, never file contents. Read the downloaded
    files afterwards with normal filesystem tools if their contents are needed.

    Selectors narrow what is fetched and compose by intersection: `sections` and
    `module_types` by structure, `files` and `match` by filename. `files` holds exact
    filenames as returned by get_course_contents; a name that matches nothing is an error,
    so a wrong name fails instead of silently downloading nothing. `match` holds
    case-insensitive globs such as "*.pdf".

    `output_dir` defaults to ./<course shortname>/. Set `dry_run` to preview the plan.
    """
    with open_client() as client:
        token = client.token
        resolved = client.resolve_course(course)
        contents = client.get_course_contents(resolved.id)

    root = (
        Path(output_dir)
        if output_dir
        else Path(sanitize(resolved.shortname, fallback=str(resolved.id)))
    )
    planned = plan_downloads(
        contents,
        root,
        only_sections=set(sections) if sections else None,
        only_modtypes=set(module_types) if module_types else None,
        only_names=set(files) if files else None,
        only_patterns=match or None,
    )

    if files:
        selected = {p.file.filename for p in planned}
        unknown = [name for name in files if name not in selected]
        if unknown:
            in_course = {f.filename for _, _, f in iter_course_files(contents)}

            def reason(name: str) -> str:
                return "excluded by another selector" if name in in_course else "not in this course"

            detail = ", ".join(f"{name!r} ({reason(name)})" for name in unknown)
            raise ValueError(f"No such file: {detail}")

    manifest: list[dict[str, Any]] = []
    if dry_run:
        for item in planned:
            manifest.append(
                {
                    "path": str(item.destination),
                    "size": item.file.filesize,
                    "module_type": item.module.modname,
                    "status": "planned",
                }
            )
        return {
            "course": resolved.shortname,
            "directory": str(root),
            "dry_run": True,
            "files": manifest,
        }

    downloaded = skipped = failed = 0
    with httpx.Client(timeout=300, follow_redirects=True) as http:
        for item in planned:
            entry: dict[str, Any] = {
                "path": str(item.destination),
                "size": item.file.filesize,
                "module_type": item.module.modname,
            }
            try:
                result = download_file(http, item.file, token, item.destination)
            except MoodleError as exc:
                failed += 1
                entry.update(status="failed", error=str(exc))
            else:
                if result.status is DownloadStatus.SKIPPED:
                    skipped += 1
                else:
                    downloaded += 1
                entry.update(status=result.status.value, size=result.size)
            manifest.append(entry)

    return {
        "course": resolved.shortname,
        "directory": str(root),
        "summary": {"downloaded": downloaded, "already_present": skipped, "failed": failed},
        "files": manifest,
    }


@mcp.tool()
def list_participants(
    course: str,
    role: str | None = None,
    include_emails: bool = False,
) -> list[dict[str, Any]]:
    """List the people enrolled in a course.

    Email addresses are omitted unless `include_emails` is true. Only set it when the task
    actually requires contacting someone; otherwise leave these out of context.

    `role` filters by role shortname, e.g. "student" or "editingteacher".

    `last_course_access` is a full timestamp carrying its UTC offset, as every instant here
    is: a bare date is read in whichever zone the tool runs in, so a late-evening access
    reads as the next day to anyone east of the campus.
    """
    client = open_client()
    with client:
        resolved = client.resolve_course(course)
        participants = client.get_participants(resolved.id)

    if role:
        needle = role.casefold()
        participants = [p for p in participants if needle in {r.casefold() for r in p.role_names}]

    people: list[dict[str, Any]] = []
    for person in participants:
        entry: dict[str, Any] = {
            "id": person.id,
            "fullname": person.fullname,
            "roles": person.role_names,
            "last_course_access": (
                person.last_course_access.isoformat() if person.last_course_access else None
            ),
        }
        if include_emails:
            entry["email"] = person.email
        people.append(entry)
    return people


@mcp.tool()
def get_course_announcements(course: str | None = None) -> list[dict[str, Any]]:
    """List announcements from a course's news forum, newest first.

    `course` accepts a numeric id or a shortname prefix; omit it to check every enrolled
    course, dashboard-hidden ones included. Only a forum Moodle marks as "news" carries
    announcements — a course's regular discussion forums are not included, and a course
    without a news forum returns nothing. `message` is plain text; `posted_at` keeps the
    time of day, which is what tells two posts of the same day apart.
    """
    client = open_client()
    with client:
        # The course list is fetched once and reused as both the sweep's scope and its
        # id -> shortname map; letting get_announcements enumerate again would double the
        # round trip on the default path.
        courses = (
            [client.resolve_course(course)]
            if course
            else client.list_courses(view="all-including-hidden")
        )
        names = {c.id: c.shortname for c in courses}
        announcements = client.get_announcements([c.id for c in courses])

    return [
        {
            "id": a.id,
            "course": names.get(a.courseid, str(a.courseid)),
            "subject": a.subject,
            "message": a.message_text,
            "author": a.userfullname,
            "posted_at": a.posted_at.isoformat() if a.posted_at else None,
            "replies": a.numreplies,
            "pinned": a.pinned,
        }
        for a in announcements
    ]


@mcp.tool()
def get_calendar(
    course: str | None = None,
    days: int = 14,
    overdue: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """What is due, across every enrolled course or within one.

    This is the cheapest and broadest deadline view available: one call covers the whole
    campus and every activity type, where get_assignments and get_quizzes each cover one.
    Prefer it for "what do I have this week"; use the other two when you need
    assignment- or quiz-specific fields such as submission status or attempt counts.

    `days` bounds the window and `overdue` flips it: false covers the next `days` days,
    true covers the `days` days just past, which is where an already-missed deadline is.
    Both ends are bounded on purpose, so a year-out final exam does not crowd out this
    week.

    Only activities that publish a deadline to the calendar appear here. A due date a
    teacher wrote into a page or an announcement does not, so treat this as the floor of
    what is owed rather than the complete list.

    `due_at` is a full timestamp carrying its UTC offset, because a deadline is a moment
    and not a day — the same reason it is one in get_assignments.

    `instance_id` is the activity's own id, which is what get_assignment_status and
    get_quiz_status take; `activity` says which of the two applies.
    """
    now = int(time.time())
    span = days * 86_400
    since, until = (now - span, now) if overdue else (now, now + span)

    client = open_client()
    with client:
        if course:
            resolved = client.resolve_course(course)
            course_names = {resolved.id: resolved.shortname}
            events = client.get_calendar_events(
                course_id=resolved.id, since=since, until=until, limit=limit
            )
        else:
            events = client.get_calendar_events(since=since, until=until, limit=limit)
            course_names = _course_names(client) if events else {}

    return [
        {
            "id": e.id,
            "name": e.name,
            "course": course_names.get(e.course_id, str(e.course_id)),
            "activity": e.modulename or None,
            "instance_id": e.instance or None,
            "due_at": e.sorts_at.isoformat() if e.sorts_at else None,
            "overdue": e.overdue,
            "action": e.action_name or None,
            "actionable": e.actionable,
            "url": e.url or e.viewurl or None,
        }
        for e in events
    ]


@mcp.tool()
def get_course_updates(course: str, days: int = 7) -> list[dict[str, Any]]:
    """Which of a course's activities changed in the last `days` days.

    Use this to check for new material instead of re-reading get_course_contents and
    comparing: Moodle tracks the change itself, so this answers in two calls and returns
    only what moved.

    `changed` carries Moodle's own area names, untranslated: `contentfiles` is a new or
    replaced file, `introfiles` an attachment on the description, `configuration` a
    setting such as a due date, `gradeitems` a change to grading. The set is open-ended
    and module-specific, so treat an unfamiliar name as "this part of the activity
    changed" rather than as an error.

    A teacher editing an activity marks it changed whether or not anything visible
    differs, so this reports movement, not news. To see what the activity now holds, call
    get_course_contents and read the named activity.
    """
    since = int(time.time()) - days * 86_400
    client = open_client()
    with client:
        resolved = client.resolve_course(course)
        updates = client.get_course_updates(resolved.id, since)
        module_names = _module_names(client, resolved.id) if updates else {}

    updates.sort(key=lambda u: u.latest_epoch, reverse=True)
    return [
        {
            "cmid": u.id,
            "activity": module_names.get(u.id),
            "changed": u.area_names,
            "changed_at": u.last_updated.isoformat() if u.last_updated else None,
        }
        for u in updates
    ]


def _module_names(client: MoodleClient, course_id: int) -> dict[int, str]:
    """Activity name per course-module id; the updates endpoint answers with ids alone."""
    names: dict[int, str] = {}
    for section in client.get_course_contents(course_id):
        for module in section.modules:
            is_label = module.modname == "label"
            names[module.id] = (module.description_text if is_label else module.name) or module.name
    return names


@mcp.tool()
def get_assignments(course: str | None = None) -> list[dict[str, Any]]:
    """List assignments and their due dates.

    `course` accepts a numeric id or a shortname prefix; omit it to check every enrolled
    course. Pass an assignment's `id` from this list to get_assignment_status for
    submission and grading detail.

    `max_grade` is null when `scale_graded` is true: the assignment is marked with a named
    scale rather than out of a number of points, and this endpoint does not name the scale.
    It is also null for an assignment that carries no grade at all.

    `due_at` is a full timestamp carrying its UTC offset, because a deadline is a moment
    and not a day: most fall at 23:59, where the date alone both hides how many hours are
    left and names the wrong day for anyone reading from a different zone than the campus.
    """
    client = open_client()
    with client:
        if course:
            resolved = client.resolve_course(course)
            course_names = {resolved.id: resolved.shortname}
            assignments = client.get_assignments([resolved.id])
        else:
            course_names = _course_names(client)
            assignments = client.get_assignments()

    return [
        {
            "id": a.id,
            "course": course_names.get(a.course, str(a.course)),
            "name": a.name,
            "due_at": a.due_at.isoformat() if a.due_at else None,
            "max_grade": a.max_grade,
            "scale_graded": a.scale_graded,
        }
        for a in assignments
    ]


@mcp.tool()
def get_assignment_status(assignment_id: int) -> dict[str, Any]:
    """Submission and grading status for one assignment.

    `assignment_id` is the `id` from get_assignments — not a course-module id, which
    this call rejects.

    `grade` is the raw value to compute with; `grade_display` is how the campus renders it,
    which is the only form that names a scale grade such as "Aprobado".

    `extension_due_at` is a full timestamp carrying its UTC offset, for the same reason
    `due_at` is one in get_assignments: an extension is a deadline, not a day.
    """
    client = open_client()
    with client:
        status = client.get_assignment_status(assignment_id)

    return {
        "submitted": status.submitted,
        "submission_status": status.status,
        "submitted_files": status.submitted_files,
        "graded": status.graded,
        "grade": status.grade,
        "grade_display": status.gradefordisplay,
        "extension_due_at": (
            status.extension_due_at.isoformat() if status.extension_due_at else None
        ),
    }


@mcp.tool()
def get_quizzes(course: str | None = None) -> list[dict[str, Any]]:
    """List quizzes and their open/close windows.

    `course` accepts a numeric id or a shortname prefix; omit it to check every enrolled
    course. Pass a quiz's `id` from this list to get_quiz_status for attempt and grade
    detail, along with the same `course`. A null `attempt_limit` means the quiz can be
    attempted any number of times.

    `opens_at` and `closes_at` are full timestamps carrying their UTC offset: a quiz window
    closes at a moment, and the date alone both hides how many hours are left and names the
    wrong day for anyone reading from a different zone than the campus.
    """
    client = open_client()
    with client:
        if course:
            resolved = client.resolve_course(course)
            course_names = {resolved.id: resolved.shortname}
            quizzes = client.get_quizzes([resolved.id])
        else:
            course_names = _course_names(client)
            quizzes = client.get_quizzes()

    return [
        {
            "id": q.id,
            "course": course_names.get(q.course, str(q.course)),
            "name": q.name,
            "opens_at": q.opens_at.isoformat() if q.opens_at else None,
            "closes_at": q.closes_at.isoformat() if q.closes_at else None,
            # Moodle encodes "unlimited" as 0 attempts, which reads as "none allowed" to
            # anyone taking the number at face value.
            "attempt_limit": q.attempts or None,
            "max_grade": q.grade,
        }
        for q in quizzes
    ]


@mcp.tool()
def get_quiz_status(quiz_id: int, course: str | None = None) -> dict[str, Any]:
    """Attempt count and best grade for one quiz.

    `quiz_id` is the `id` from get_quizzes. `grade` and `grade_to_pass` are scaled to
    `max_grade`. A false `grade_available` means the grade cannot be read — the quiz may
    be unattempted, awaiting manual grading, or graded with the marks hidden.

    Pass the `course` the quiz came from whenever it is known. Reading `max_grade` means
    finding the quiz, and a quiz id alone does not say which course holds it, so without
    this the lookup sweeps every enrolment: checking a course's quizzes one by one pulls
    the whole campus once per quiz.
    """
    client = open_client()
    with client:
        course_id = client.resolve_course(course).id if course else None
        status = client.get_quiz_status(quiz_id, course_id=course_id)

    return {
        "attempts_used": status.attempt_count,
        "last_attempt_state": status.last_state,
        "grade_available": status.has_grade,
        "grade": status.grade,
        "grade_to_pass": status.grade_to_pass,
        "max_grade": status.max_grade,
    }


@mcp.tool()
def get_grade_summary() -> list[dict[str, Any]]:
    """Course-level grade summary across every enrolled course.

    Works even for a course whose gradebook is not open to students. For a per-item
    breakdown of one course — individual assignments, quizzes, etc. — use get_grades.
    """
    client = open_client()
    with client:
        course_names = _course_names(client)
        overview = client.get_grade_overview()

    return [
        {"course": course_names.get(g.courseid, str(g.courseid)), "grade": g.grade}
        for g in overview
    ]


@mcp.tool()
def get_grades(course: str) -> list[dict[str, Any]]:
    """Per-item grade breakdown for one course: assignments, quizzes, etc.

    Raises if the instructor has not opened the gradebook to students in this course;
    get_grade_summary still works in that case, just without the per-item detail.

    Aggregate rows are included: `item` reads "Course total" or "Category subtotal" for the
    rows Moodle sends unnamed.
    """
    client = open_client()
    with client:
        resolved = client.resolve_course(course)
        items = client.get_grade_items(resolved.id)

    return [
        {
            "item": item.label,
            "grade": item.gradeformatted,
            "max": item.grademax,
            "percentage": item.percentageformatted,
            "feedback": html_to_text(item.feedback),
        }
        for item in items
    ]


# After every core tool, so the core has already claimed its names: FastMCP keeps the first
# registration of a name and only warns about a second. At import rather than in main(),
# because the tests import the tool functions directly and never call main().
register_tools(mcp)


def main() -> None:
    """Run the server over stdio."""
    # httpx logs every request at INFO, which buries real server diagnostics on stderr.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
