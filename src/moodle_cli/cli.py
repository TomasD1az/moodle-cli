"""Typer command line interface."""

from __future__ import annotations

import functools
import json
import textwrap
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from moodle_cli import __version__ as __version__
from moodle_cli.auth import TokenStore, mint_token
from moodle_cli.capabilities import State, evaluate, group_by_component
from moodle_cli.client import MoodleClient
from moodle_cli.config import load_config
from moodle_cli.downloads import (
    DownloadResult,
    DownloadStatus,
    PlannedDownload,
    PlannedLink,
    download_file,
    download_link,
    iter_course_files,
    iter_course_links,
    plan_downloads,
    plan_link_downloads,
    sanitize,
)
from moodle_cli.errors import MoodleAPIError, MoodleError
from moodle_cli.models import (
    Announcement,
    Assignment,
    CalendarEvent,
    Participant,
    Section,
    epoch_to_datetime,
)
from moodle_cli.plugins import (
    CatalogEntry,
    catalog,
    extra_distributions,
    installed_extras,
    mount_commands,
    requirement_name,
)
from moodle_cli.search import SearchHit, search_contents
from moodle_cli.session import open_client
from moodle_cli.toolenv import (
    Environment,
    detect,
    git_spec,
    injected_packages,
    install_command,
    pip_command,
    resolved_ref,
    run,
)
from moodle_cli.update import is_newer, latest_release

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help=(
        "Access a Moodle campus: courses, contents, downloads, participants, "
        "announcements, assignments, quizzes and grades."
    ),
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(help="Manage authentication.", no_args_is_help=True)
courses_app = typer.Typer(help="Work with your enrolled courses.", no_args_is_help=True)
course_app = typer.Typer(help="Work with a single course.", no_args_is_help=True)
plugins_app = typer.Typer(help="Manage optional plugins.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(courses_app, name="courses")
app.add_typer(course_app, name="course")
app.add_typer(plugins_app, name="plugins")

# Mounted at import rather than in main(), because the tests and any other embedder import
# `app` directly; wiring only the entry point would give the plugin surface to a shell and
# to nothing else that drives this app. Core groups are added first so they own their names.
mount_commands(app)


class View(StrEnum):
    ALL = "all"
    ALL_INCLUDING_HIDDEN = "all-including-hidden"
    IN_PROGRESS = "in-progress"
    FUTURE = "future"
    PAST = "past"
    STARRED = "starred"
    HIDDEN = "hidden"


class Sort(StrEnum):
    NAME = "name"
    LAST_ACCESSED = "last-accessed"


CourseArg = Annotated[str, typer.Argument(help="Course id or shortname, e.g. 29272 or IOS460.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")]

P = ParamSpec("P")
R = TypeVar("R")


def handle_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Turn library errors into a clean message and a non-zero exit.

    This lives on the commands rather than only in `main()` so the behaviour is the same
    however the app is invoked, including from tests.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except MoodleError as exc:
            err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
            raise typer.Exit(1) from exc

    return wrapper


def _emit_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_epoch(value: int, fmt: str = "%Y-%m-%d") -> str:
    """Render a Moodle timestamp, going through the one shared conversion.

    Converting here rather than from the raw epoch is what keeps this surface and the MCP
    server on the same calendar day for an evening event.
    """
    moment = epoch_to_datetime(value)
    return moment.strftime(fmt) if moment else "-"


def _plural(count: int, noun: str) -> str:
    """Pluralise a table's unit noun.

    The -y rule is here because "activity" is the first unit this tool counts that is not
    regular, and "3 activitys" is the kind of wrong that gets read as a broken command.
    A consonant before the y is what distinguishes it from "day", which keeps its own.
    """
    if count == 1:
        return noun
    if noun.endswith("y") and len(noun) > 1 and noun[-2] not in "aeiou":
        return f"{noun[:-1]}ies"
    return f"{noun}s"


def _format_moment(value: int) -> str:
    """Render a timestamp to the minute, for a deadline rather than a date.

    Every other table here prints a bare date, which is the right granularity to scan a
    list of activities. A calendar is the one place where the hour is the whole point: a
    quiz closing at 23:59 and one closing at 09:00 on the same day are not the same row.
    """
    return _format_epoch(value, "%Y-%m-%d %H:%M")


def _format_year(value: int) -> str:
    """Start year is the only recency signal available: no course here sets an end date."""
    return _format_epoch(value, "%Y")


# -- auth ------------------------------------------------------------------------


@auth_app.command("login")
@handle_errors
def auth_login(
    username: Annotated[
        str | None, typer.Option("--username", "-u", help="Campus username.")
    ] = None,
) -> None:
    """Mint a web-service token and store it in the system keyring.

    The password is prompted for and never accepted as an argument, which would leave it in
    your shell history. Once the token is stored you can remove MOODLE_PASS from .env.
    """
    config = load_config()
    user = username or config.username or typer.prompt("Username")
    password = config.password or typer.prompt("Password", hide_input=True)

    token = mint_token(config.base_url, user, password)
    stored = TokenStore().set(config.keyring_key, token)

    # The one place that builds a client directly: the token being verified is the one just
    # minted, so resolving would consult the environment and the keyring instead and could
    # answer with a different token than the login produced.
    with MoodleClient(config.base_url, token) as client:
        info = client.get_site_info()

    console.print(f"[green]Logged in[/green] as {info.fullname} (id {info.userid})")
    console.print(f"  site: {info.sitename}  ({info.release})")
    if stored:
        console.print("  token stored in the system keyring")
    else:
        console.print(
            "[yellow]  no keyring backend available; set MOODLE_TOKEN to reuse this token[/yellow]"
        )


@auth_app.command("status")
@handle_errors
def auth_status() -> None:
    """Show whether a usable token exists, and who it belongs to."""
    with open_client(allow_mint=False) as client:
        info = client.get_site_info()
    console.print(f"[green]Authenticated[/green] as {info.fullname} (id {info.userid})")
    console.print(f"  site: {info.sitename}")
    console.print(f"  functions available: {len(info.function_names)}")
    console.print(f"  file downloads allowed: {info.downloadfiles}")


@auth_app.command("capabilities")
@handle_errors
def auth_capabilities(
    all_functions: Annotated[
        bool,
        typer.Option("--functions", help="List every function name instead of a per-feature view."),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show what this campus lets this token do.

    Campuses enable different slices of Moodle's web-service API, so a command failing
    here is as likely to be a campus setting as a bug. This answers which ones will work
    before you run them, and names the functions a missing one needs — which is what you
    would ask a Moodle administrator to enable.

    Features listed with no command are ones this campus supports and this tool does not
    implement yet.
    """
    with open_client(allow_mint=False) as client:
        info = client.get_site_info()

    available = info.function_names
    if not available:
        # A campus can answer get_site_info without a function list. Reporting that as
        # "nothing works" would be a confident wrong answer; every command below might
        # still succeed.
        if as_json:
            _emit_json(
                {
                    "site": info.sitename,
                    "release": info.release,
                    "functions_available": 0,
                    "file_downloads_allowed": info.downloadfiles,
                    "features": [],
                    "components": [],
                    "functions": sorted(available) if all_functions else None,
                }
            )
        else:
            err_console.print(
                "[yellow]This campus did not report a function list, so nothing can be "
                "checked against it.[/yellow]"
            )
        return

    statuses = evaluate(available)

    if as_json:
        _emit_json(
            {
                "site": info.sitename,
                "release": info.release,
                "functions_available": len(available),
                "file_downloads_allowed": info.downloadfiles,
                "features": [
                    {
                        "name": s.feature.name,
                        "state": s.state.value,
                        "commands": s.feature.commands,
                        "built": s.feature.built,
                        "summary": s.feature.summary,
                        "missing": list(s.missing),
                    }
                    for s in statuses
                ],
                "components": [
                    {"component": name, "functions": count}
                    for name, count in group_by_component(available)
                ],
                "functions": sorted(available) if all_functions else None,
            }
        )
        return

    if all_functions:
        for name in sorted(available):
            console.print(name)
        console.print(f"\n{len(available)} functions available to this token")
        return

    table = Table(title=f"{info.sitename or 'Campus'} — {len(available)} functions available")
    table.add_column("feature", no_wrap=True)
    table.add_column("", justify="center", no_wrap=True)
    table.add_column("command", no_wrap=True, style="dim")
    table.add_column("needs", ratio=1, overflow="fold", style="dim")
    for status in statuses:
        mark = {State.OK: "[green]yes[/green]", State.PARTIAL: "[yellow]part[/yellow]"}.get(
            status.state, "[red]no[/red]"
        )
        if status.feature.commands:
            where = status.feature.commands
        else:
            where = "-- not built yet --" if not status.feature.built else "(automatic)"
        table.add_row(
            escape(status.feature.name),
            mark,
            escape(where),
            escape(", ".join(status.missing)),
        )
    console.print(table)
    console.print(f"file downloads allowed: {info.downloadfiles}")


@auth_app.command("logout")
@handle_errors
def auth_logout() -> None:
    """Delete the stored token from the keyring."""
    config = load_config()
    if TokenStore().delete(config.keyring_key):
        console.print("[green]Token deleted from the keyring.[/green]")
    else:
        console.print("[yellow]No stored token to delete.[/yellow]")


# -- courses ---------------------------------------------------------------------


@courses_app.command("list")
@handle_errors
def courses_list(
    view: Annotated[View, typer.Option("--view", help="Which courses to include.")] = View.ALL,
    sort: Annotated[Sort, typer.Option("--sort", help="Ordering.")] = Sort.NAME,
    as_json: JsonOpt = False,
) -> None:
    """List your enrolled courses.

    Note on --view: the time-based filters depend on courses having an end date, and this
    campus never sets one. In practice 'in-progress' returns everything and 'past'/'future'
    return nothing; only 'all', 'starred' and 'hidden' discriminate.
    """
    with open_client() as client:
        courses = client.list_courses(view=view.value, sort=sort.value)

    if as_json:
        _emit_json([c.model_dump(mode="json") for c in courses])
        return

    # Only `name` flexes; everything else is fixed and no-wrap, so a narrow terminal
    # ellipsizes the name instead of crushing the columns that identify the course.
    # Category is dropped for width and lives in --json.
    table = Table(title=f"{len(courses)} courses ({view.value}, by {sort.value})", expand=True)
    table.add_column("id", justify="right", style="dim", no_wrap=True)
    table.add_column("shortname", no_wrap=True)
    table.add_column("name", ratio=1, min_width=20, no_wrap=True, overflow="ellipsis")
    table.add_column("year", justify="right", style="dim", no_wrap=True)
    table.add_column("*", justify="center", no_wrap=True)
    for course in courses:
        table.add_row(
            str(course.id),
            course.shortname,
            _short_name(course.fullname, course.shortname),
            _format_year(course.startdate),
            "*" if course.isfavourite else "",
        )
    console.print(table)


@courses_app.command("grades")
@handle_errors
def courses_grades(as_json: JsonOpt = False) -> None:
    """Show a grade summary across every enrolled course.

    Works even for a course whose gradebook is not open to students. For a per-item
    breakdown of one course, use `course grades` instead.
    """
    with open_client() as client:
        course_names = _course_names(client)
        overview = client.get_grade_overview()

    if as_json:
        _emit_json(
            [
                {"course": course_names.get(g.courseid, str(g.courseid)), "grade": g.grade}
                for g in overview
            ]
        )
        return

    table = Table(title=f"Grade summary ({len(overview)} {_plural(len(overview), 'course')})")
    table.add_column("course")
    table.add_column("grade", justify="right")
    for g in overview:
        table.add_row(escape(course_names.get(g.courseid, str(g.courseid))), g.grade or "-")
    console.print(table)


@courses_app.command("assignments")
@handle_errors
def courses_assignments(as_json: JsonOpt = False) -> None:
    """List assignments and due dates across every enrolled course.

    Ordered by due date, undated last, so the next deadline is at the top. For one course,
    use `course assignments`.
    """
    with open_client() as client:
        course_names = _course_names(client)
        assignments = sorted(client.get_assignments(), key=_by_due_date)

    if as_json:
        _emit_json([a.model_dump(mode="json") for a in assignments])
        return

    count = len(assignments)
    table = Table(title=f"{count} {_plural(count, 'assignment')}")
    table.add_column("id", justify="right", style="dim")
    table.add_column("course", no_wrap=True)
    table.add_column("name")
    table.add_column("due", justify="right", style="dim")
    table.add_column("grade", justify="right", style="dim")
    for a in assignments:
        table.add_row(
            str(a.id),
            escape(course_names.get(a.course, str(a.course))),
            escape(a.name),
            _format_epoch(a.duedate),
            _grade_cell(a),
        )
    console.print(table)


DaysOpt = Annotated[
    int, typer.Option("--days", min=1, help="How many days of the window to cover.")
]
OverdueOpt = Annotated[
    bool,
    typer.Option(
        "--overdue", help="Show the window that has already passed instead of the one ahead."
    ),
]
LimitOpt = Annotated[int, typer.Option("--limit", min=1, help="Maximum events to return.")]


def _window_label(days: int, overdue: bool) -> str:
    """The window in words, named once so both calendar commands phrase it the same."""
    return f"{'past' if overdue else 'next'} {days} {_plural(days, 'day')}"


def _event_window(days: int, overdue: bool) -> tuple[int, int]:
    """The (since, until) epoch bounds a calendar request covers.

    Both directions are bounded. An unbounded past would reach back to whatever the
    campus has ever recorded, and an unbounded future returns next year's exam alongside
    tomorrow's problem set, which is not what "what is due" means to anyone.
    """
    now = int(time.time())
    span = days * 86_400
    return (now - span, now) if overdue else (now, now + span)


def _event_payload(event: CalendarEvent, course: str) -> dict[str, Any]:
    """One event, with the instant carrying its offset and the course named, not numbered."""
    return {
        "id": event.id,
        "name": event.name,
        "course": course,
        "activity": event.modulename or None,
        "instance_id": event.instance or None,
        "due_at": event.sorts_at.isoformat() if event.sorts_at else None,
        "overdue": event.overdue,
        "action": event.action_name or None,
        "actionable": event.actionable,
        "url": event.url or event.viewurl or None,
    }


def _print_events(events: list[CalendarEvent], course_names: dict[int, str], title: str) -> None:
    """Render events in the calendar's own order, marking the ones already past.

    Only `what` flexes; the rest are fixed and no-wrap, so a narrow terminal ellipsizes
    the activity name rather than crushing the deadline that identifies the row. The
    event's action ("Add submission", "Attempt quiz now") is left to --json: it is the
    longest field by far, `type` already says which activity it belongs to, and keeping
    it would take the width away from the name.
    """
    table = Table(title=title, expand=True)
    table.add_column("due", justify="left", no_wrap=True)
    table.add_column("course", no_wrap=True)
    table.add_column("what", ratio=1, min_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("type", no_wrap=True, style="dim")
    for event in events:
        due = _format_moment(event.timesort)
        table.add_row(
            f"[red]{due}[/red]" if event.overdue else due,
            escape(course_names.get(event.course_id, "-")),
            escape(event.name),
            escape(event.modulename or "-"),
        )
    console.print(table)


@courses_app.command("calendar")
@handle_errors
def courses_calendar(
    days: DaysOpt = 14,
    overdue: OverdueOpt = False,
    limit: LimitOpt = 200,
    as_json: JsonOpt = False,
) -> None:
    """Show what is due across every enrolled course.

    One call answers for the whole campus, so this is the cheapest view of a week there
    is — cheaper than listing assignments and quizzes separately, and it covers every
    activity type rather than those two.

    Only activities that publish a deadline to the calendar appear. A due date a teacher
    wrote into a page or an announcement is not one of them, so this is the floor of what
    you owe, not the ceiling.

    Times are printed to the minute, because a deadline is an instant: see "Deadlines are
    moments" in the README.
    """
    since, until = _event_window(days, overdue)
    with open_client() as client:
        events = client.get_calendar_events(since=since, until=until, limit=limit)
        course_names = _course_names(client) if events else {}

    if as_json:
        _emit_json(
            [_event_payload(e, course_names.get(e.course_id, str(e.course_id))) for e in events]
        )
        return

    window = _window_label(days, overdue)
    if not events:
        console.print(f"Nothing due in the {window}.")
        return
    _print_events(events, course_names, f"{len(events)} due, {window}")


def _by_due_date(assignment: Assignment) -> tuple[bool, int]:
    """Sort undated assignments after dated ones: 0 means "no due date", not "the epoch"."""
    return (assignment.duedate == 0, assignment.duedate)


def _course_names(client: MoodleClient) -> dict[int, str]:
    """Shortname per course id, for labelling rows that carry only an id.

    Includes courses hidden from the dashboard: the grade and assignment endpoints answer
    for every enrolment, so anything narrower leaves a bare id in the output.
    """
    return {c.id: c.shortname for c in client.list_courses(view="all-including-hidden")}


def _short_name(fullname: str, shortname: str) -> str:
    """Drop the course code the fullname repeats from the shortname.

    Campus fullnames read "I406 - Criptografía y Ciberseguridad (grupo 2) G:2 Teó 1 - ...",
    where the leading code is already its own column.
    """
    code = shortname.split(" - ")[0].strip()
    if code and fullname.startswith(f"{code} - "):
        return fullname[len(code) + 3 :]
    return fullname


@courses_app.command("download")
@handle_errors
def courses_download(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Parent directory. Default: the current one."),
    ] = None,
    view: Annotated[View, typer.Option("--view", help="Which courses to include.")] = View.ALL,
    types: Annotated[
        list[str] | None,
        typer.Option("--type", help="Only these module types, e.g. resource. Repeatable."),
    ] = None,
    patterns: Annotated[
        list[str] | None,
        typer.Option("--match", help="Glob on the filename, e.g. '*.pdf'. Repeatable."),
    ] = None,
    links: Annotated[
        bool,
        typer.Option(
            "--links", help="Also fetch Google Slides/Docs/Sheets and Drive-hosted links."
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be downloaded, write nothing.")
    ] = False,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Re-download files that already exist.")
    ] = False,
) -> None:
    """Download every enrolled course's files into one directory per course.

    The same download as `course download`, swept across your enrolment: each course
    lands in its own subdirectory named after its shortname, mirroring its sections
    inside. Re-running is as cheap as it is for one course, since a file already on disk
    at the expected size is skipped.

    --file and --section are not offered here on purpose: a filename or a section number
    identifies something inside one course, and asking for it across every course would
    either fail on the first course that lacks it or silently mean something different in
    each. Use --type and --match, which describe files rather than positions.

    A course that cannot be read is reported and skipped rather than ending the run; over
    a whole enrolment, an archived or restricted course is ordinary rather than
    exceptional.
    """
    selectors = _Selectors(
        modtypes=set(types) if types else None,
        patterns=patterns or None,
    )
    parent = output or Path()

    plans: list[tuple[str, Path, list[PlannedDownload], list[PlannedLink]]] = []
    with open_client() as client:
        token = client.token
        for found in client.list_courses(view=view.value):
            root = parent / sanitize(found.shortname, fallback=str(found.id))
            try:
                contents = client.get_course_contents(found.id)
            except MoodleAPIError as exc:
                err_console.print(
                    f"  [yellow]skip[/yellow] {escape(found.shortname)}: {escape(str(exc))}"
                )
                continue
            planned, planned_links = _plan_course(contents, root, selectors, links=links)
            if planned or planned_links:
                plans.append((found.shortname, root, planned, planned_links))

    if not plans:
        console.print("[yellow]No matching files in any course.[/yellow]")
        return

    downloaded = skipped = attempted = 0
    with httpx.Client(timeout=300, follow_redirects=True) as http:
        for shortname, root, planned, planned_links in plans:
            _print_course_header(shortname, root, planned, planned_links, links=links)
            if dry_run:
                _print_plan(planned, root)
                if planned_links:
                    _print_link_plan(planned_links, root)
                continue
            attempted += len(planned) + len(planned_links)
            course_downloaded, course_skipped = _execute_plan(
                http, token, root, planned, planned_links, overwrite=overwrite
            )
            downloaded += course_downloaded
            skipped += course_skipped

    if dry_run:
        return

    failed = attempted - downloaded - skipped
    courses_done = len(plans)
    summary = (
        f"\n{downloaded} downloaded, {skipped} already present "
        f"across {courses_done} {_plural(courses_done, 'course')}"
    )
    if failed:
        summary += f", [red]{failed} failed[/red]"
    console.print(summary)
    if failed:
        raise typer.Exit(1)


@courses_app.command("search")
@handle_errors
def courses_search(
    query: Annotated[str, typer.Argument(help="Text to look for in names, case-insensitive.")],
    as_json: JsonOpt = False,
) -> None:
    """Search section, activity, file and link names across every enrolled course.

    A link matches on its destination as well as on its label, so a bare domain such as
    github.com finds it. The match column names what was hit; when it is an activity name,
    the files and links shown are the activity's whole contents rather than a filtered set.
    """
    with open_client() as client:
        results = search_contents(client, query)

    if as_json:
        _emit_json(results.as_payload())
        return

    if not results.hits:
        console.print("[yellow]No matches.[/yellow]")
        return

    count = len(results.hits)
    table = Table(title=f"{count} {_plural(count, 'result')} for {query!r}", expand=True)
    table.add_column("course", no_wrap=True)
    table.add_column("section", style="dim", no_wrap=True, overflow="ellipsis")
    table.add_column("match", style="dim", no_wrap=True)
    table.add_column("activity", ratio=1, min_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("files and links", ratio=1, min_width=16, overflow="fold")
    for hit in results.hits:
        table.add_row(
            hit.course,
            f"{hit.section_number}. {hit.section}",
            hit.kind.value,
            hit.module or "-",
            _hit_contents(hit),
        )
    console.print(table)
    if results.truncated:
        console.print("[yellow]More matches than shown; narrow the query.[/yellow]")


def _hit_contents(hit: SearchHit) -> str:
    names = [f.filename for f in hit.files] + [link.fileurl or "" for link in hit.links]
    return "\n".join(names) or "-"


# -- course ----------------------------------------------------------------------


@course_app.command("contents")
@handle_errors
def course_contents(course: CourseArg, as_json: JsonOpt = False) -> None:
    """Show a course's sections, activities, downloadable files and external links."""
    with open_client() as client:
        resolved = client.resolve_course(course)
        sections = client.get_course_contents(resolved.id)

    if as_json:
        _emit_json([s.model_dump(mode="json") for s in sections])
        return

    console.print(f"[bold]{escape(resolved.shortname)}[/bold] — {escape(resolved.fullname)}\n")
    for section in sections:
        files = sum(len(m.files) for m in section.modules)
        links = sum(len(m.links) for m in section.modules)
        counts = [c for c in [_count(files, "file"), _count(links, "link")] if c]
        suffix = f"  [dim]({', '.join(counts)})[/dim]" if counts else ""
        console.print(
            f"[bold cyan]{section.section:>2}. {escape(section.name)}[/bold cyan]{suffix}"
        )
        if section.summary_text:
            _print_block("     [dim italic]", 5, section.summary_text, "[/dim italic]")
        for module in section.modules:
            marker = "" if module.uservisible else " [dim](hidden)[/dim]"
            # A label's name is a ~50-char preview Moodle itself truncates; its
            # description carries the full text, which is what a reader wants.
            is_label = module.modname == "label"
            title = (module.description_text if is_label else module.name) or module.name
            _print_block(f"     [dim]{module.modname:<10}[/dim] ", 16, title, marker)
            # Other module types keep a real title in `name`; a description here is a
            # teacher's added instructions shown below the title on the course page.
            if not is_label and module.description_text and module.description_text != title:
                _print_block("                ", 16, module.description_text)
            for file in module.files:
                # Size leads so a long filename wrapping cannot orphan it on its own line.
                size = _human_size(file.filesize).rjust(9)
                console.print(f"       [dim]{size}[/dim]  {escape(file.filename)}")
            for link in module.links:
                console.print(f"       [dim]{'link':>9}[/dim]  {escape(link.fileurl or '')}")
        console.print()


def _print_block(prefix: str, indent_width: int, text: str, suffix: str = "") -> None:
    """Print multi-line text with ``prefix`` on the first line, aligned continuation after.

    Moodle stores prose as one HTML blob; a plain ``console.print`` would run every line
    together at the console's left edge instead of under where the text started. ``prefix``
    may carry Rich markup, so its rendered width has to be passed separately as
    ``indent_width`` rather than derived from ``len(prefix)``.
    """
    lines = text.splitlines() or [""]
    console.print(f"{prefix}{escape(lines[0])}{suffix}")
    indent = " " * indent_width
    for line in lines[1:]:
        console.print(f"{indent}{escape(line)}")


def _count(n: int, noun: str) -> str:
    return f"{n} {_plural(n, noun)}" if n else ""


@course_app.command("download")
@handle_errors
def course_download(
    course: CourseArg,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Destination directory. Default: ./<shortname>/"),
    ] = None,
    sections: Annotated[
        list[int] | None,
        typer.Option("--section", help="Only this section number. Repeatable."),
    ] = None,
    types: Annotated[
        list[str] | None,
        typer.Option("--type", help="Only these module types, e.g. resource. Repeatable."),
    ] = None,
    names: Annotated[
        list[str] | None,
        typer.Option(
            "--file",
            help="Exact filename, as shown by `course contents`. Repeatable. "
            "Fails if a name matches nothing.",
        ),
    ] = None,
    patterns: Annotated[
        list[str] | None,
        typer.Option("--match", help="Glob on the filename, e.g. '*.pdf'. Repeatable."),
    ] = None,
    links: Annotated[
        bool,
        typer.Option(
            "--links", help="Also fetch Google Slides/Docs/Sheets and Drive-hosted links."
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be downloaded, write nothing.")
    ] = False,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Re-download files that already exist.")
    ] = False,
) -> None:
    """Download a course's files, mirroring its section structure.

    Selectors compose: --section and --type narrow by structure, --file and --match by
    filename. A --file name that matches nothing is an error rather than a silent
    zero-file download, so a typo fails loudly. --links only reaches Google-hosted links
    (Slides/Docs/Sheets exports, Drive files, Colab notebooks); other hosts stay listed-only.
    """
    selectors = _Selectors(
        sections=set(sections) if sections else None,
        modtypes=set(types) if types else None,
        names=set(names) if names else None,
        patterns=patterns or None,
    )
    with open_client() as client:
        token = client.token
        resolved = client.resolve_course(course)
        contents = client.get_course_contents(resolved.id)

    root = output or Path(sanitize(resolved.shortname, fallback=str(resolved.id)))
    planned, planned_links = _plan_course(contents, root, selectors, links=links)

    if names:
        _reject_unknown_names(names, planned, planned_links, contents, links=links)

    if not planned and not planned_links:
        console.print("[yellow]No matching files.[/yellow]")
        return

    _print_course_header(resolved.shortname, root, planned, planned_links, links=links)

    if dry_run:
        _print_plan(planned, root)
        if planned_links:
            _print_link_plan(planned_links, root)
        return

    with httpx.Client(timeout=300, follow_redirects=True) as http:
        downloaded, skipped = _execute_plan(
            http, token, root, planned, planned_links, overwrite=overwrite
        )

    failed = len(planned) + len(planned_links) - downloaded - skipped
    summary = f"\n{downloaded} downloaded, {skipped} already present"
    if failed:
        summary += f", [red]{failed} failed[/red]"
    console.print(summary)
    if failed:
        raise typer.Exit(1)


@dataclass(frozen=True)
class _Selectors:
    """The four ways a download can be narrowed, carried as one value.

    They travel together through planning and never apart: passing them as four
    parameters made every caller restate the same `set(x) if x else None` conversion,
    which is exactly where a single-course and an all-courses path would drift.
    """

    sections: set[int] | None = None
    modtypes: set[str] | None = None
    names: set[str] | None = None
    patterns: list[str] | None = None


def _plan_course(
    contents: list[Section],
    root: Path,
    selectors: _Selectors,
    *,
    links: bool,
) -> tuple[list[PlannedDownload], list[PlannedLink]]:
    """What one course's download would fetch, files and Google-hosted links alike."""
    planned = plan_downloads(
        contents,
        root,
        only_sections=selectors.sections,
        only_modtypes=selectors.modtypes,
        only_names=selectors.names,
        only_patterns=selectors.patterns,
    )
    planned_links = (
        plan_link_downloads(
            contents,
            root,
            only_sections=selectors.sections,
            only_modtypes=selectors.modtypes,
            only_names=selectors.names,
            only_patterns=selectors.patterns,
        )
        if links
        else []
    )
    return planned, planned_links


def _print_course_header(
    shortname: str,
    root: Path,
    planned: list[PlannedDownload],
    planned_links: list[PlannedLink],
    *,
    links: bool,
) -> None:
    total = sum(p.file.filesize for p in planned)
    parts = [f"{len(planned)} {_plural(len(planned), 'file')}, {_human_size(total)}"]
    if links:
        parts.append(f"{len(planned_links)} {_plural(len(planned_links), 'link')}")
    console.print(f"[bold]{escape(shortname)}[/bold]: {', '.join(parts)} -> {root}/")


def _execute_plan(
    http: httpx.Client,
    token: str,
    root: Path,
    planned: list[PlannedDownload],
    planned_links: list[PlannedLink],
    *,
    overwrite: bool,
) -> tuple[int, int]:
    """Fetch a planned course, returning (downloaded, already present).

    Failures are reported as they happen and counted by difference rather than returned:
    a caller needs the total to decide its exit code, and one course failing must not
    stop the next one in an all-courses run.
    """
    downloaded = skipped = 0
    for item in planned:
        fetch = functools.partial(
            download_file, http, item.file, token, item.destination, overwrite=overwrite
        )
        downloaded, skipped = _run_download(root, item.destination, fetch, downloaded, skipped)

    for link_item in planned_links:
        fetch = functools.partial(
            download_link, http, link_item.link, link_item.destination, overwrite=overwrite
        )
        downloaded, skipped = _run_download(
            root, link_item.destination, fetch, downloaded, skipped, label="[dim][link][/dim] "
        )
    return downloaded, skipped


def _run_download(
    root: Path,
    destination: Path,
    fetch: Callable[[], DownloadResult],
    downloaded: int,
    skipped: int,
    *,
    label: str = "",
) -> tuple[int, int]:
    """Run one planned download, print its outcome, and return the updated counters."""
    relative = destination.relative_to(root)
    try:
        result = fetch()
    except MoodleError as exc:
        err_console.print(f"  [red]FAIL[/red] {label}{escape(str(relative))}: {escape(str(exc))}")
        return downloaded, skipped
    relative = result.path.relative_to(root)  # a link download may rename the placeholder
    if result.status is DownloadStatus.SKIPPED:
        console.print(f"  [dim]skip[/dim] {label}{escape(str(relative))}")
        return downloaded, skipped + 1
    console.print(
        f"  [green]ok[/green]   {label}{escape(str(relative))} "
        f"[dim]({_human_size(result.size)})[/dim]"
    )
    return downloaded + 1, skipped


def _reject_unknown_names(
    requested: list[str],
    planned: list[PlannedDownload],
    planned_links: list[PlannedLink],
    contents: list[Section],
    *,
    links: bool,
) -> None:
    """Fail on a --file name that selected nothing, across files and links alike.

    Distinguishes a typo from a name excluded by another filter, because the two need
    different fixes and the symptom is identical. A name that only belongs to a link is
    "no such file" when `--links` wasn't passed -- it's excluded by that, not by
    --section/--type -- so link names only count as in-course when `links` is true.
    """
    selected = {p.file.filename for p in planned} | {p.link.filename for p in planned_links}
    missing = [name for name in requested if name not in selected]
    if not missing:
        return

    in_course = {f.filename for _, _, f in iter_course_files(contents)}
    if links:
        in_course |= {link.filename for _, _, link in iter_course_links(contents)}
    for name in missing:
        reason = (
            "excluded by --section/--type" if name in in_course else "no such file in this course"
        )
        err_console.print(f"[red]Error:[/red] {escape(name)}: {reason}")
    raise typer.Exit(1)


def _print_plan(planned: list[PlannedDownload], root: Path) -> None:
    table = Table(show_header=True)
    table.add_column("size", justify="right")
    table.add_column("type", style="dim")
    table.add_column("destination")
    for item in planned:
        table.add_row(
            _human_size(item.file.filesize),
            item.module.modname,
            str(item.destination.relative_to(root)),
        )
    console.print(table)


def _print_link_plan(planned: list[PlannedLink], root: Path) -> None:
    """Like `_print_plan`, but sizes are unknown until fetched and destinations may still
    be missing their extension for an opaque Drive file."""
    table = Table(show_header=True)
    table.add_column("type", style="dim")
    table.add_column("destination")
    for item in planned:
        table.add_row(item.module.modname, str(item.destination.relative_to(root)))
    console.print(table)


@course_app.command("participants")
@handle_errors
def course_participants(
    course: CourseArg,
    role: Annotated[
        str | None, typer.Option("--role", help="Filter by role shortname, e.g. student.")
    ] = None,
    emails: Annotated[
        bool, typer.Option("--emails", help="Include email addresses (hidden by default).")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """List the people enrolled in a course.

    Email addresses are withheld unless --emails is passed: the API returns them for every
    participant, and they are not something to spill into a terminal or a log by default.
    """
    with open_client() as client:
        resolved = client.resolve_course(course)
        participants = client.get_participants(resolved.id)

    if role:
        needle = role.casefold()
        participants = [p for p in participants if needle in {r.casefold() for r in p.role_names}]

    if as_json:
        _emit_json([_participant_payload(p, emails) for p in participants])
        return

    table = Table(title=f"{len(participants)} participants in {resolved.shortname}")
    table.add_column("name")
    table.add_column("roles", style="dim")
    if emails:
        table.add_column("email")
    table.add_column("last access", justify="right", style="dim")
    for person in participants:
        row = [person.fullname, ", ".join(person.role_names) or "-"]
        if emails:
            row.append(person.email or "-")
        row.append(_format_epoch(person.lastcourseaccess))
        table.add_row(*row)
    console.print(table)


def _participant_payload(person: Participant, emails: bool) -> dict[str, Any]:
    payload = person.model_dump(mode="json")
    if not emails:
        payload.pop("email", None)
    return payload


@course_app.command("announcements")
@handle_errors
def course_announcements(course: CourseArg, as_json: JsonOpt = False) -> None:
    """List announcements from a course's news forum, newest first.

    Only a forum Moodle marks as "news" carries announcements; a course without one
    prints nothing.
    """
    with open_client() as client:
        resolved = client.resolve_course(course)
        announcements = client.get_announcements([resolved.id])

    if as_json:
        _emit_json([_announcement_payload(a, resolved.shortname) for a in announcements])
        return

    if not announcements:
        console.print("[yellow]No announcements.[/yellow]")
        return

    for a in announcements:
        pin = " [yellow](pinned)[/yellow]" if a.pinned else ""
        console.print(f"[bold]{escape(a.subject)}[/bold]{pin}")
        console.print(
            f"  [dim]{_format_epoch(a.created, '%Y-%m-%d %H:%M')} — {escape(a.userfullname)}[/dim]"
        )
        console.print(textwrap.indent(escape(a.message_text), "  "))
        console.print()


def _announcement_payload(announcement: Announcement, course: str) -> dict[str, Any]:
    """Build the JSON body field by field, matching the MCP tool key for key.

    ``message`` carries plain text on both surfaces; a model dump would ship raw HTML
    here and drop the derived fields, so a consumer that learned the schema from one
    surface would silently mis-read the other.
    """
    return {
        "id": announcement.id,
        "course": course,
        "subject": announcement.subject,
        "message": announcement.message_text,
        "author": announcement.userfullname,
        "posted_at": announcement.posted_at.isoformat() if announcement.posted_at else None,
        "replies": announcement.numreplies,
        "pinned": announcement.pinned,
    }


@course_app.command("calendar")
@handle_errors
def course_calendar(
    course: CourseArg,
    days: DaysOpt = 14,
    overdue: OverdueOpt = False,
    limit: LimitOpt = 200,
    as_json: JsonOpt = False,
) -> None:
    """Show what is due in one course.

    The same view as `courses calendar`, narrowed server-side rather than by filtering a
    campus-wide answer, so a course with a busy week is not crowded out of the limit by
    the rest of your enrolment.
    """
    since, until = _event_window(days, overdue)
    with open_client() as client:
        found = client.resolve_course(course)
        events = client.get_calendar_events(
            course_id=found.id, since=since, until=until, limit=limit
        )

    if as_json:
        _emit_json([_event_payload(e, found.shortname) for e in events])
        return

    window = _window_label(days, overdue)
    if not events:
        console.print(f"Nothing due in {found.shortname} in the {window}.")
        return
    _print_events(
        events, {found.id: found.shortname}, f"{found.shortname}: {len(events)} due, {window}"
    )


@course_app.command("updates")
@handle_errors
def course_updates(
    course: CourseArg,
    days: DaysOpt = 7,
    as_json: JsonOpt = False,
) -> None:
    """Show which of a course's activities changed recently.

    Answers "is there anything new" in two calls rather than by re-reading the whole
    course and comparing: Moodle tracks the change itself and reports only the activities
    that moved. The second call is `course contents`, which is what turns the
    course-module ids it answers with into activity names.

    The areas are Moodle's own and are reported unchanged: `contentfiles` is a new or
    replaced file, `introfiles` an attachment on the description, `configuration` a
    setting such as a due date, `gradeitems` a change to how it is graded.

    A teacher editing an activity updates it whether or not anything you can see changed,
    so this reports movement rather than news.
    """
    since = int(time.time()) - days * 86_400
    with open_client() as client:
        resolved = client.resolve_course(course)
        updates = client.get_course_updates(resolved.id, since)
        # Only worth a second round trip when something actually changed.
        module_names = _module_names(client, resolved.id) if updates else {}

    updates.sort(key=lambda u: u.latest_epoch, reverse=True)

    if as_json:
        _emit_json(
            [
                {
                    "cmid": u.id,
                    "activity": module_names.get(u.id),
                    "changed": u.area_names,
                    "changed_at": u.last_updated.isoformat() if u.last_updated else None,
                }
                for u in updates
            ]
        )
        return

    window = _window_label(days, overdue=True)
    if not updates:
        console.print(f"Nothing changed in {escape(resolved.shortname)} in the {window}.")
        return

    count = len(updates)
    table = Table(
        title=f"{resolved.shortname}: {count} {_plural(count, 'activity')} changed, {window}",
        expand=True,
    )
    table.add_column("changed", no_wrap=True)
    table.add_column("activity", ratio=1, min_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("what", ratio=1, min_width=16, overflow="fold", style="dim")
    for update in updates:
        moment = update.last_updated
        table.add_row(
            moment.strftime("%Y-%m-%d %H:%M") if moment else "-",
            escape(module_names.get(update.id) or f"cmid {update.id}"),
            escape(", ".join(update.area_names) or "-"),
        )
    console.print(table)


def _module_names(client: MoodleClient, course_id: int) -> dict[int, str]:
    """Activity name per course-module id.

    The updates endpoint answers with ids alone, and a course-module id means nothing to
    a reader. A label carries its text in the description rather than the name, the same
    way `course contents` reads it.
    """
    names: dict[int, str] = {}
    for section in client.get_course_contents(course_id):
        for module in section.modules:
            is_label = module.modname == "label"
            names[module.id] = (module.description_text if is_label else module.name) or module.name
    return names


@course_app.command("assignments")
@handle_errors
def course_assignments(course: CourseArg, as_json: JsonOpt = False) -> None:
    """List a course's assignments and due dates.

    For every course at once, use `courses assignments`.
    """
    with open_client() as client:
        resolved = client.resolve_course(course)
        assignments = client.get_assignments([resolved.id])

    if as_json:
        _emit_json([a.model_dump(mode="json") for a in assignments])
        return

    count = len(assignments)
    table = Table(title=f"{count} {_plural(count, 'assignment')} in {escape(resolved.shortname)}")
    table.add_column("id", justify="right", style="dim")
    table.add_column("name")
    table.add_column("due", justify="right", style="dim")
    table.add_column("grade", justify="right", style="dim")
    for a in assignments:
        table.add_row(str(a.id), escape(a.name), _format_epoch(a.duedate), _grade_cell(a))
    console.print(table)


def _grade_cell(assignment: Assignment) -> str:
    """The grade column holds a point maximum, and a scale-graded assignment has none.

    Moodle encodes "graded by scale N" as a negative ``grade``; printed as a number it
    reads as a maximum of -N. The scale's name is not in this payload, so the column can
    only say which kind of grading applies.
    """
    if assignment.scale_graded:
        return "scale"
    return f"{assignment.max_grade:g}" if assignment.max_grade else "-"


AssignmentIdArg = Annotated[
    int, typer.Argument(help="Assignment id, as shown by `course assignments`.")
]


@course_app.command("assignment-status")
@handle_errors
def course_assignment_status(assignment_id: AssignmentIdArg, as_json: JsonOpt = False) -> None:
    """Show submission and grading status for one assignment.

    `assignment_id` is the id from `course assignments` — not a course-module id, which
    this call rejects.
    """
    with open_client() as client:
        status = client.get_assignment_status(assignment_id)

    if as_json:
        _emit_json(status.model_dump(mode="json"))
        return

    console.print(f"submitted: {'yes' if status.submitted else 'no'} ({status.status or '-'})")
    console.print(f"graded: {'yes' if status.graded else 'no'}")
    if status.gradefordisplay:
        console.print(f"grade: {escape(status.gradefordisplay)}")
    if status.submitted_files:
        console.print("files:")
        for name in status.submitted_files:
            console.print(f"  {escape(name)}")
    if status.extensionduedate:
        console.print(f"extension until: {_format_epoch(status.extensionduedate)}")


@course_app.command("quizzes")
@handle_errors
def course_quizzes(course: CourseArg, as_json: JsonOpt = False) -> None:
    """List a course's quizzes and their open/close windows."""
    with open_client() as client:
        resolved = client.resolve_course(course)
        quizzes = client.get_quizzes([resolved.id])

    if as_json:
        _emit_json([q.model_dump(mode="json") for q in quizzes])
        return

    table = Table(title=f"{len(quizzes)} quizzes in {resolved.shortname}")
    table.add_column("id", justify="right", style="dim")
    table.add_column("name")
    table.add_column("closes", justify="right", style="dim")
    table.add_column("attempts", justify="right", style="dim")
    table.add_column("max grade", justify="right", style="dim")
    for q in quizzes:
        attempts = str(q.attempts) if q.attempts else "unlimited"
        table.add_row(
            str(q.id),
            escape(q.name),
            _format_epoch(q.timeclose),
            attempts,
            str(q.grade),
        )
    console.print(table)


QuizIdArg = Annotated[int, typer.Argument(help="Quiz id, as shown by `course quizzes`.")]


@course_app.command("quiz-status")
@handle_errors
def course_quiz_status(
    quiz_id: QuizIdArg,
    course: Annotated[
        str | None,
        typer.Option("--course", help="Course the quiz belongs to. Narrows the grade lookup."),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Show attempt count and best grade for one quiz.

    `quiz_id` is the id from `course quizzes`. Passing `--course` keeps the maximum-grade
    lookup to that course; without it every enrolled course's quizzes are fetched, since a
    quiz id alone does not say which course holds it.
    """
    with open_client() as client:
        course_id = client.resolve_course(course).id if course else None
        status = client.get_quiz_status(quiz_id, course_id=course_id)

    if as_json:
        _emit_json(status.model_dump(mode="json"))
        return

    console.print(f"attempts used: {status.attempt_count}")
    if status.last_state:
        console.print(f"last attempt: {status.last_state}")
    if status.attempt_ids:
        ids = ", ".join(str(i) for i in status.attempt_ids)
        console.print(f"attempt ids: {ids}  [dim](for `course quiz-review`)[/dim]")
    if status.has_grade:
        # The grade arrives already scaled to the quiz maximum, so it reads as a
        # proportion only next to that maximum.
        scale = f" / {status.max_grade}" if status.max_grade else ""
        pass_note = f" (pass: {status.grade_to_pass})" if status.grade_to_pass else ""
        console.print(f"grade: {status.grade}{scale}{pass_note}")
    else:
        # One flag covers never attempted, awaiting manual grading, and graded with the
        # marks hidden by the quiz's review options — so report availability, not grading.
        console.print("grade: not available (not graded yet, or hidden by the quiz)")


@course_app.command("quiz-review")
@handle_errors
def course_quiz_review(
    attempt_id: Annotated[
        int, typer.Argument(help="Attempt id, as shown by `course quiz-status`.")
    ],
    questions: Annotated[
        bool, typer.Option("--questions", help="Print each question's full text.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Read a finished quiz attempt back, with its questions and any visible marks.

    This is the campus's own "Review" page, read from the command line. It is read-only:
    the attempt is already over and nothing here changes it.

    What comes back is governed by the quiz's review options, which the teacher sets. The
    same attempt yields more after the quiz closes than while it is still open, and marks
    can be withheld entirely — an attempt with questions and no marks is a real answer,
    not a failure.

    Moodle returns each question as rendered HTML, so `--questions` prints it stripped to
    text. Expect the wording, the options and — where the review options allow it — the
    feedback, laid out as one block per question rather than as structured fields.
    """
    with open_client() as client:
        review = client.get_quiz_attempt_review(attempt_id)

    if as_json:
        _emit_json(
            {
                "attempt_id": attempt_id,
                "state": review.attempt.state if review.attempt else None,
                "grade": review.grade,
                "marks_visible": review.marks_visible,
                "questions": [
                    {
                        "number": q.number,
                        "type": q.type,
                        "status": q.status,
                        "mark": q.mark_value,
                        "max_mark": q.maxmark,
                        "flagged": q.flagged,
                        "text": q.text,
                    }
                    for q in review.questions
                ],
            }
        )
        return

    attempt = review.attempt
    if attempt:
        finished = _format_moment(attempt.timefinish) if attempt.timefinish else "-"
        console.print(f"attempt {attempt.attempt} ({attempt.state}), finished {finished}")
    if review.grade is not None:
        console.print(f"grade: {review.grade}")
    if not review.questions:
        console.print("[yellow]No questions returned; the quiz may not allow review yet.[/yellow]")
        return
    if not review.marks_visible:
        console.print("[yellow]Marks are hidden by this quiz's review options.[/yellow]")

    count = len(review.questions)
    table = Table(title=f"{count} {_plural(count, 'question')}", expand=True)
    table.add_column("#", justify="right", no_wrap=True, style="dim")
    table.add_column("type", no_wrap=True, style="dim")
    table.add_column("status", ratio=1, min_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("mark", justify="right", no_wrap=True)
    for question in review.questions:
        mark = question.mark_value
        out_of = question.maxmark
        cell = "-" if mark is None else f"{mark:g}" + (f" / {out_of:g}" if out_of else "")
        table.add_row(question.number, question.type, escape(question.status or "-"), cell)
    console.print(table)

    if questions:
        for question in review.questions:
            console.print(f"\n[bold cyan]{question.number}.[/bold cyan] [dim]{question.type}[/dim]")
            _print_block("   ", 3, question.text)


@course_app.command("grades")
@handle_errors
def course_grades(course: CourseArg, as_json: JsonOpt = False) -> None:
    """Show the per-item grade breakdown for one course: assignments, quizzes, etc.

    Fails if the instructor has not opened the gradebook to students in this course;
    `courses grades` still works in that case, just without per-item detail.
    """
    with open_client() as client:
        resolved = client.resolve_course(course)
        items = client.get_grade_items(resolved.id)

    if as_json:
        _emit_json([i.model_dump(mode="json") for i in items])
        return

    table = Table(title=f"Grades for {resolved.shortname}")
    table.add_column("item")
    table.add_column("grade", justify="right")
    table.add_column("max", justify="right", style="dim")
    for i in items:
        table.add_row(escape(i.label), i.gradeformatted or "-", str(i.grademax))
    console.print(table)


# -- plugins ---------------------------------------------------------------------


def _catalog_payload(entry: CatalogEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "distribution": entry.distribution,
        "official": entry.official,
        "status": "error" if entry.problem else "installed" if entry.installed else "available",
        "version": entry.version,
        "summary": entry.summary,
        "command_group": entry.mounted_as,
        "mcp_tools": list(entry.tools),
        "problem": entry.problem,
    }


@plugins_app.command("list")
@handle_errors
def plugins_list(as_json: JsonOpt = False) -> None:
    """List the official plugins and what each one adds.

    Never reaches the network: a plugin you have not installed is listed from the extras
    this release declares, so the command works offline and tells no index what you are
    looking at. That is also why an uninstalled plugin has no description — the summary
    lives in that package's own metadata, which is not on this machine yet.

    Third-party plugins are listed too, marked as such. They cannot be installed from here,
    but a command group appearing from nowhere is what this command exists to explain.
    """
    entries = catalog()
    if as_json:
        _emit_json([_catalog_payload(e) for e in entries])
        return

    if not entries:
        console.print("[yellow]No plugins in the catalog for this release.[/yellow]")
        return

    table = Table(title=f"{len(entries)} plugins")
    table.add_column("name", no_wrap=True)
    table.add_column("source", no_wrap=True, style="dim")
    table.add_column("status", no_wrap=True)
    table.add_column("version", justify="right", style="dim", no_wrap=True)
    table.add_column("adds", no_wrap=True)
    table.add_column("description", ratio=1, overflow="ellipsis")
    for entry in entries:
        if entry.problem:
            status = "[red]error[/red]"
        elif entry.installed:
            status = "[green]installed[/green]"
        else:
            status = "available"
        adds = ", ".join(filter(None, [entry.mounted_as, *entry.tools])) or "-"
        table.add_row(
            entry.name,
            "official" if entry.official else "third-party",
            status,
            entry.version or "-",
            adds,
            escape(entry.summary or "-"),
        )
    console.print(table)

    # A plugin that is not installed here has no metadata to describe it, so the hint
    # replaces the description rather than being squeezed into the column beside it.
    if any(not entry.installed for entry in entries):
        console.print("Install one with `moodle plugins install NAME`.")

    for entry in entries:
        if entry.problem:
            err_console.print(f"[red]{entry.name}:[/red] {escape(entry.problem)}")


def _known(name: str) -> CatalogEntry:
    """The official catalog entry for `name`, or an error naming what is installable."""
    for entry in catalog():
        if entry.name != name:
            continue
        if entry.official:
            return entry
        raise MoodleError(
            f"{name} is a third-party plugin, not one this CLI installs. Remove it with "
            f"`uv pip uninstall {entry.distribution}`, or reinstall it the way you added it."
        )
    known = ", ".join(e.name for e in catalog() if e.official) or "none in this release"
    raise MoodleError(f"No such plugin: {name!r}. Known plugins: {known}.")


def _foreign_injected(injected: Sequence[str]) -> list[str]:
    """The hand-injected `--with` packages that are not one of this release's own plugins.

    Only an official plugin can come back as an extra, so only an official one may be
    dropped from the injected set. A third-party plugin is in the catalog too, and
    filtering against the whole catalog would drop it from `--with` without `wanted` ever
    restating it, uninstalling it in silence.
    """
    installable = set(extra_distributions().values())
    return [p for p in injected if requirement_name(p) not in installable]


def _require_uv(env: Environment, extras: Iterable[str], ref: str) -> str:
    """`env.uv`, or the "uv is not on PATH" error naming the command to run by hand."""
    if env.uv is None:
        spec = git_spec(extras, ref)
        raise MoodleError(
            f"uv is not on PATH, so this cannot change the installation itself. "
            f"Install {spec!r} with the packaging tool you used for moodle-cli."
        )
    return env.uv


def _uv_tool_reinstall(uv: str, extras: Iterable[str], ref: str) -> list[str]:
    """The `uv tool install --reinstall` argv, restating hand-injected `--with` packages.

    Shared by `plugins install`/`uninstall` and `update`: both rebuild the whole tool
    environment from an (extras, ref) pair, which is the only thing that differs between
    them.
    """
    injected = injected_packages(uv)
    if injected is None:
        spec = git_spec(extras, ref)
        raise MoodleError(
            "Could not read the packages injected into this tool environment, and "
            "reinstalling without them would remove them. Run "
            f'`uv tool install --reinstall "{spec}"` yourself, '
            "restating every --with package."
        )
    return install_command(uv, extras, _foreign_injected(injected), ref)


def _plan(name: str, *, keep: bool) -> tuple[Environment, list[str]]:
    """The environment and the command that leaves it with `name` installed or removed."""
    env = detect()
    if env.kind == "editable":
        raise MoodleError(
            "This moodle is an editable install from a checkout. Change its extras there "
            f"instead, with `uv sync --extra {name}`."
        )
    # Pins the core to its current commit or tag: changing extras must never move the core.
    ref = resolved_ref(__version__)
    wanted = installed_extras() | {name} if keep else installed_extras() - {name}
    uv = _require_uv(env, wanted, ref)

    if env.kind == "uv-tool":
        return env, _uv_tool_reinstall(uv, wanted, ref)

    spec = git_spec({name}, ref) if keep else _known(name).distribution
    return env, pip_command(uv, env.python, spec, uninstall=not keep)


@plugins_app.command("install")
@handle_errors
def plugins_install(
    name: Annotated[str, typer.Argument(help="Plugin name, as shown by `plugins list`.")],
    as_json: JsonOpt = False,
) -> None:
    """Install an official plugin into this installation of moodle.

    Extras and hand-injected packages belong to the environment rather than to a package,
    so on a `uv tool` installation this rebuilds the whole install command from the current
    state; anything added by hand with --with is carried across.
    """
    entry = _known(name)
    if entry.installed:
        message = f"{name} is already installed ({entry.distribution} {entry.version})."
        if as_json:
            _emit_json({"plugin": name, "action": "already-installed", "command": None})
        else:
            console.print(f"[green]{escape(message)}[/green]")
        return

    env, argv = _plan(name, keep=True)
    output = run(argv, capture=as_json)

    if as_json:
        _emit_json(
            {
                "plugin": name,
                "action": "installed",
                "environment": env.kind,
                "command": argv,
                "output": output,
            }
        )
        return
    console.print(f"[green]Installed[/green] {name}. Its commands appear on the next run.")


@plugins_app.command("uninstall")
@handle_errors
def plugins_uninstall(
    name: Annotated[str, typer.Argument(help="Plugin name, as shown by `plugins list`.")],
    as_json: JsonOpt = False,
) -> None:
    """Remove an official plugin from this installation of moodle."""
    entry = _known(name)
    if not entry.installed:
        if as_json:
            _emit_json({"plugin": name, "action": "not-installed", "command": None})
        else:
            console.print(f"[yellow]{name} is not installed.[/yellow]")
        return

    env, argv = _plan(name, keep=False)
    output = run(argv, capture=as_json)

    if as_json:
        _emit_json(
            {
                "plugin": name,
                "action": "uninstalled",
                "environment": env.kind,
                "command": argv,
                "output": output,
            }
        )
        return
    console.print(f"[green]Removed[/green] {name}.")
    if env.kind == "uv-managed":
        spec = git_spec({name}, f"v{__version__}")
        console.print(
            f"  the `{name}` extra still declares it, so installing `{spec}` again brings it back"
        )


# -- self-update -------------------------------------------------------------------


@app.command("version")
@handle_errors
def version_cmd(
    check: Annotated[
        bool, typer.Option("--check", help="Also check GitHub for a newer release.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show the installed version of moodle-cli.

    Never reaches the network unless `--check` is passed.
    """
    latest: str | None = None
    update_available: bool | None = None
    if check:
        latest = latest_release()
        update_available = is_newer(latest, __version__)

    if as_json:
        payload: dict[str, Any] = {"version": __version__}
        if check:
            payload["latest"] = latest
            payload["update_available"] = update_available
        _emit_json(payload)
        return

    console.print(f"moodle-cli {__version__}")
    if not check:
        return
    if update_available:
        console.print(f"[yellow]{latest} is available.[/yellow] Run `moodle update`.")
    else:
        console.print("Up to date.")


@app.command("update")
@handle_errors
def update_cmd(as_json: JsonOpt = False) -> None:
    """Upgrade this installation of moodle-cli to the latest GitHub release."""
    latest = latest_release()
    if not is_newer(latest, __version__):
        if as_json:
            _emit_json({"action": "up-to-date", "version": __version__})
        else:
            console.print(f"[green]Already up to date[/green] ({__version__}).")
        return

    env = detect()
    extras = installed_extras()
    if env.kind == "editable":
        raise MoodleError(
            "This moodle is an editable install from a checkout. Update it with `git pull` instead."
        )
    uv = _require_uv(env, extras, latest)

    if env.kind == "uv-tool":
        argv = _uv_tool_reinstall(uv, extras, latest)
    else:
        argv = pip_command(uv, env.python, git_spec(extras, latest))

    output = run(argv, capture=as_json)
    if as_json:
        _emit_json(
            {
                "action": "updated",
                "from": __version__,
                "to": latest,
                "environment": env.kind,
                "command": argv,
                "output": output,
            }
        )
        return
    console.print(f"[green]Updated[/green] {__version__} -> {latest}.")


def main() -> None:
    """Entry point that turns library errors into clean CLI failures."""
    try:
        app()
    except MoodleError as exc:
        err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
