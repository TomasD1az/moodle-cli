"""HTTP client for the Moodle web-service API."""

from __future__ import annotations

import html
import json
import time
from collections.abc import Sequence
from types import TracebackType
from typing import Any

import httpx

from moodle_cli.errors import MoodleAPIError, MoodleError
from moodle_cli.models import (
    Announcement,
    Assignment,
    AssignmentStatus,
    AttemptReview,
    CalendarEvent,
    Course,
    CourseGrade,
    CourseUpdate,
    Forum,
    GradeItem,
    Participant,
    Quiz,
    QuizStatus,
    Section,
    SiteInfo,
)

REST_PATH = "/webservice/rest/server.php"

#: The mobile app's multi-call endpoint. Not every campus exposes it, so every use of it
#: goes through :meth:`MoodleClient.call_many`, which falls back to one request each.
BATCH_FUNCTION = "tool_mobile_call_external_functions"

#: Public filter name -> Moodle ``classification`` value, read off the dashboard dropdown.
VIEWS: dict[str, str] = {
    "all": "all",
    "all-including-hidden": "allincludinghidden",
    "in-progress": "inprogress",
    "future": "future",
    "past": "past",
    "starred": "favourites",
    "hidden": "hidden",
}

#: Public sort name -> Moodle ``sort`` value. The last-accessed value is a raw SQL ORDER BY
#: fragment that Moodle whitelists server-side; it stays encapsulated here.
SORTS: dict[str, str] = {
    "name": "fullname",
    "last-accessed": "ul.timeaccess desc",
}

_PARTICIPANT_PAGE_SIZE = 250
_CALENDAR_PAGE_SIZE = 50


def _flatten_params(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Encode nested structures the way Moodle's REST server expects.

    ``{"options": [{"name": "limitnumber", "value": 5}]}`` becomes
    ``{"options[0][name]": "limitnumber", "options[0][value]": "5"}``.
    """
    flat: dict[str, str] = {}
    for key, value in params.items():
        full_key = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten_params(value, full_key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                indexed = f"{full_key}[{index}]"
                if isinstance(item, dict):
                    flat.update(_flatten_params(item, indexed))
                else:
                    flat[indexed] = _scalar(item)
        elif value is not None:
            flat[full_key] = _scalar(value)
    return flat


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class MoodleClient:
    """Typed access to the subset of the Moodle API this tool needs.

    Every response is inspected for an error payload before use: the campus answers failed
    calls with HTTP 200, so status codes alone would let errors through as data.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=timeout, follow_redirects=True)
        # Keyed by (view, sort), because those are what change the answer. Scoped to this
        # client and never written to disk: one command's lifetime is short enough that a
        # course list cannot go stale within it, which is the property that makes caching
        # here safe and caching across invocations a correctness question instead.
        self._course_cache: dict[tuple[str, str], list[Course]] = {}
        self._site_info: SiteInfo | None = None

    def __enter__(self) -> MoodleClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    # -- transport ---------------------------------------------------------------

    def _call(self, function: str, **params: Any) -> Any:
        payload = {
            "wstoken": self.token,
            "moodlewsrestformat": "json",
            "wsfunction": function,
            **params,
        }
        response = self._http.post(f"{self.base_url}{REST_PATH}", data=_flatten_params(payload))
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise MoodleError(f"{function} returned a non-JSON response") from exc
        check_api_error(body, function=function)
        return body

    def call(self, function: str, **params: Any) -> Any:
        """Invoke any web-service function and return its parsed body.

        The escape hatch for a function this class does not wrap, which is what a plugin
        reaching a part of the campus the core does not cover needs. It goes through the
        same error check as every wrapped call, because the campus signals failure with
        HTTP 200 and a body: a plugin must not be able to opt out of that.
        """
        return self._call(function, **params)

    @property
    def supports_batching(self) -> bool:
        """Whether this campus exposes the mobile app's multi-call endpoint.

        Reading it costs nothing extra in practice: ``get_site_info`` is cached, and any
        command large enough to want batching makes more than one call anyway.
        """
        return BATCH_FUNCTION in self.get_site_info().function_names

    def call_many(self, calls: Sequence[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Run several calls in one HTTP request, falling back to one request each.

        ``tool_mobile_call_external_functions`` is what the official mobile app uses to
        avoid a round trip per activity. It is worth reaching for wherever the number of
        calls grows with the size of a course or an enrolment — a campus-wide sweep over
        sequential requests is dominated by latency, not by work.

        Results come back in the order asked. A single call that failed raises, exactly as
        it would have on its own: batching is a transport detail, and a caller must not
        have to check for errors differently depending on how its request was carried.

        Not every campus exposes the endpoint, and the fallback is a plain loop, so a
        caller never has to ask whether it is available.
        """
        if len(calls) <= 1 or not self.supports_batching:
            return [self._call(function, **params) for function, params in calls]

        body = self._call(
            BATCH_FUNCTION,
            requests=[
                # Arguments travel as a JSON string here, not as Moodle's usual bracketed
                # form encoding: the batch endpoint decodes each one itself.
                {"function": function, "arguments": json.dumps(params)}
                for function, params in calls
            ],
        )
        responses = body.get("responses") or []
        if len(responses) != len(calls):
            raise MoodleError(f"{BATCH_FUNCTION} answered {len(responses)} of {len(calls)} calls")
        return [
            _unwrap_batched(response, function)
            for response, (function, _) in zip(responses, calls, strict=True)
        ]

    # -- endpoints ---------------------------------------------------------------

    def get_site_info(self) -> SiteInfo:
        """Who the token belongs to, and what the campus exposes to it.

        Cached for the life of the client: three separate commands read it to learn the
        user id alone, and it does not change mid-command.
        """
        if self._site_info is None:
            self._site_info = SiteInfo.model_validate(self._call("core_webservice_get_site_info"))
        return self._site_info

    def list_courses(self, view: str = "all", sort: str = "name") -> list[Course]:
        """List enrolled courses.

        ``view`` and ``sort`` take the public names in :data:`VIEWS` and :data:`SORTS`.

        The answer is cached per (view, sort) on this client. A single command routinely
        asks twice — once through :meth:`resolve_course` to turn a shortname into an id,
        once to label rows that carry only an id — and reading one quiz's maximum used to
        re-list every course per quiz.
        """
        classification = _lookup(VIEWS, view, "view")
        sort_value = _lookup(SORTS, sort, "sort")
        cached = self._course_cache.get((classification, sort_value))
        if cached is not None:
            return cached
        body = self._call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification=classification,
            limit=0,
            offset=0,
            sort=sort_value,
        )
        courses = [Course.model_validate(c) for c in body.get("courses", [])]
        self._course_cache[(classification, sort_value)] = courses
        return courses

    def get_course_contents(self, course_id: int) -> list[Section]:
        body = self._call("core_course_get_contents", courseid=course_id)
        return [Section.model_validate(s) for s in body]

    def get_participants(self, course_id: int) -> list[Participant]:
        """Fetch every enrolled user, paging so large courses are not silently truncated."""
        participants: list[Participant] = []
        offset = 0
        while True:
            body = self._call(
                "core_enrol_get_enrolled_users",
                courseid=course_id,
                options=[
                    {"name": "limitfrom", "value": offset},
                    {"name": "limitnumber", "value": _PARTICIPANT_PAGE_SIZE},
                ],
            )
            page = [Participant.model_validate(u) for u in body]
            participants.extend(page)
            if len(page) < _PARTICIPANT_PAGE_SIZE:
                return participants
            offset += _PARTICIPANT_PAGE_SIZE

    def get_announcements(self, course_ids: Sequence[int] | None = None) -> list[Announcement]:
        """Posts from each course's news forum, newest first.

        Only a forum with ``type == "news"`` carries announcements; a course's regular
        discussion forums are skipped. Passing no ids sweeps every enrolled course,
        dashboard-hidden ones included, so the sweep covers exactly what
        :meth:`resolve_course` accepts. Uses ``mod_forum_get_forum_discussions``, not the
        similarly-named ``mod_forum_get_discussions``: the latter is not exposed by this
        campus's token.
        """
        ids = (
            list(course_ids)
            if course_ids is not None
            else [c.id for c in self.list_courses(view="all-including-hidden")]
        )
        if not ids:
            return []

        body = self._call("mod_forum_get_forums_by_courses", courseids=ids)
        forums = [f for f in (Forum.model_validate(f) for f in body) if f.type == "news"]

        # One call per news forum, which is one per course: over a full enrolment that is
        # where the time goes, and it is exactly the shape the batch endpoint exists for.
        bodies = self.call_many(
            [("mod_forum_get_forum_discussions", {"forumid": forum.id}) for forum in forums]
        )

        announcements: list[Announcement] = []
        for forum, discussions in zip(forums, bodies, strict=True):
            check_warnings(discussions, function="mod_forum_get_forum_discussions")
            for discussion in discussions.get("discussions") or []:
                announcements.append(
                    Announcement.model_validate({**discussion, "courseid": forum.course})
                )

        announcements.sort(key=lambda a: a.created, reverse=True)
        return announcements

    def get_assignments(self, course_ids: list[int] | None = None) -> list[Assignment]:
        """Assignments across courses; every enrolled course if ``course_ids`` is omitted."""
        params: dict[str, Any] = {"courseids": course_ids} if course_ids else {}
        body = self._call("mod_assign_get_assignments", **params)
        return [
            Assignment.model_validate(a)
            for course in body.get("courses") or []
            for a in course.get("assignments") or []
        ]

    def get_assignment_status(self, assignment_id: int) -> AssignmentStatus:
        """Submission and grading status for one assignment.

        ``assignment_id`` is the assignment's own ``id`` (from :meth:`get_assignments`),
        not the course-module id: passing a cmid raises ``invalidrecordunknown``.
        """
        body = self._call("mod_assign_get_submission_status", assignid=assignment_id)
        lastattempt = body.get("lastattempt") or {}
        # A team assignment records the group's attempt under its own key; the shapes match.
        submission = lastattempt.get("submission") or lastattempt.get("teamsubmission") or {}
        feedback = body.get("feedback") or {}
        grade = feedback.get("grade") or {}

        files = [
            file["filename"]
            for plugin in submission.get("plugins") or []
            if plugin.get("type") == "file"
            for area in plugin.get("fileareas") or []
            for file in area.get("files") or []
        ]

        # Every read below is `or default`, never `get(key, default)`: this campus sends an
        # unset field as null, which a default cannot displace.
        return AssignmentStatus(
            status=submission.get("status"),
            gradingstatus=lastattempt.get("gradingstatus") or "",
            grade=grade.get("grade"),
            gradefordisplay=html.unescape(feedback.get("gradefordisplay") or ""),
            extensionduedate=lastattempt.get("extensionduedate") or 0,
            submitted_files=files,
        )

    def get_quizzes(self, course_ids: list[int] | None = None) -> list[Quiz]:
        """Quizzes across courses; every enrolment if ``course_ids`` is omitted.

        Omitting ``courseids`` lets Moodle fall back to the full enrolment list, which
        includes courses the dashboard hides; listing courses here would not.
        """
        params: dict[str, Any] = {"courseids": course_ids} if course_ids else {}
        body = self._call("mod_quiz_get_quizzes_by_courses", **params)
        return [Quiz.model_validate(q) for q in body.get("quizzes", [])]

    def get_quiz_status(self, quiz_id: int, *, course_id: int | None = None) -> QuizStatus:
        """Attempt history and best grade for one quiz.

        Three sources, because none alone answers "did I take this and how did it go":
        ``mod_quiz_get_user_attempts`` carries no grade, ``mod_quiz_get_user_best_grade``
        carries no attempt history, and neither reports the maximum the grade is already
        scaled to — that lives on the quiz, and is fetched only when there is a grade to
        scale.

        ``course_id`` narrows that last lookup to one course. Without it the lookup has no
        way to know which course to ask about and has to sweep every enrolment, so a caller
        walking the quizzes of one course pays for the whole campus once per quiz.
        """
        attempts_body = self._call(
            "mod_quiz_get_user_attempts", quizid=quiz_id, status="all", includepreviews=0
        )
        # Moodle orders attempts ascending, so the last entry is the most recent one.
        attempts = attempts_body.get("attempts", [])
        grade_body = self._call("mod_quiz_get_user_best_grade", quizid=quiz_id)
        has_grade = grade_body.get("hasgrade", False)

        return QuizStatus(
            attempt_count=len(attempts),
            attempt_ids=[int(a["id"]) for a in attempts if "id" in a],
            last_state=attempts[-1].get("state") if attempts else None,
            has_grade=has_grade,
            grade=grade_body.get("grade"),
            grade_to_pass=grade_body.get("gradetopass"),
            max_grade=self._quiz_max_grade(quiz_id, course_id) if has_grade else None,
        )

    def _quiz_max_grade(self, quiz_id: int, course_id: int | None = None) -> float | None:
        """The maximum a quiz grades out of; ``None`` when the quiz is no longer listed.

        A known course keeps this to one course's quizzes; otherwise every enrolment is
        swept, because a quiz id alone does not say which course to ask about.
        """
        course_ids = [course_id] if course_id is not None else None
        return next((q.grade for q in self.get_quizzes(course_ids) if q.id == quiz_id), None)

    def get_quiz_attempt_review(self, attempt_id: int, *, page: int = -1) -> AttemptReview:
        """Read a finished attempt back, with its questions and any visible marks.

        This is the endpoint behind the campus's own "Review" page, and it is read-only:
        it reports an attempt that is already over and changes nothing. What it returns is
        governed by the quiz's review options, so the same attempt yields more detail
        after the quiz closes than while it is open.

        ``page=-1`` asks for every page at once, which is what reading a whole attempt
        wants; a real page number is for walking one screen at a time.

        Questions arrive as rendered HTML rather than as structured data. That is Moodle's
        own shape — the official mobile app renders the same markup — so any reader that
        wants text has to strip it, which :attr:`AttemptQuestion.text` does.
        """
        body = self._call("mod_quiz_get_attempt_review", attemptid=attempt_id, page=page)
        check_warnings(body, function="mod_quiz_get_attempt_review")
        return AttemptReview.model_validate(body)

    def get_calendar_events(
        self,
        *,
        course_id: int | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 200,
    ) -> list[CalendarEvent]:
        """Dated, actionable items — what is due, and when.

        The one endpoint that answers "what is coming up" for every course in a single
        call, which is why it does not take a list of course ids: omitting ``course_id``
        sweeps every enrolment server-side, including courses the dashboard hides.

        ``since`` and ``until`` bound the window as epoch seconds; ``since`` defaults to
        now, making the default answer "upcoming". Passing ``until=now`` instead is how a
        caller asks for what is already overdue, since an overdue item's ``timesort`` is
        in the past.

        Moodle returns these a page at a time and identifies the next page by the last
        event id seen, not by an offset. Paging here rather than in the caller is what
        keeps a busy week from being silently cut off at the default of 20.
        """
        function = (
            "core_calendar_get_action_events_by_course"
            if course_id is not None
            else "core_calendar_get_action_events_by_timesort"
        )
        events: list[CalendarEvent] = []
        after_event_id = 0
        timesort_from = int(time.time()) if since is None else since
        while len(events) < limit:
            page_size = min(_CALENDAR_PAGE_SIZE, limit - len(events))
            params: dict[str, Any] = {
                "timesortfrom": timesort_from,
                "limitnum": page_size,
            }
            if until is not None:
                params["timesortto"] = until
            if after_event_id:
                params["aftereventid"] = after_event_id
            if course_id is not None:
                params["courseid"] = course_id

            body = self._call(function, **params)
            page = [CalendarEvent.model_validate(e) for e in body.get("events") or []]
            events.extend(page)
            if len(page) < page_size:
                break
            # `lastid` is Moodle's own cursor; falling back to the last event's id keeps
            # paging working on a response that omits it rather than looping forever.
            after_event_id = int(body.get("lastid") or page[-1].id)
        return events

    def get_course_updates(self, course_id: int, since: int) -> list[CourseUpdate]:
        """What changed in a course's activities since ``since`` (epoch seconds).

        The cheap way to ask "is there anything new": one call per course returns only
        the activities that moved, where noticing the same thing by diffing course
        contents means fetching every section, file and description each time.

        Moodle answers with course-module ids and area names, not with activity names or
        file lists — ``core_course_get_contents`` is what turns an id into something a
        reader recognises, and the caller joins the two.

        Activities with no changes are dropped rather than returned empty: Moodle lists an
        instance for every module it checked, so keeping them would report a quiet course
        as dozens of rows saying nothing happened.
        """
        body = self._call("core_course_get_updates_since", courseid=course_id, since=since)
        check_warnings(body, function="core_course_get_updates_since")
        updates = [CourseUpdate.model_validate(i) for i in body.get("instances") or []]
        return [u for u in updates if u.updates]

    def get_grade_overview(self) -> list[CourseGrade]:
        """Course-level grade summary across every enrolled course.

        Unlike :meth:`get_grade_items`, this works regardless of whether an instructor
        has enabled the gradebook for students in a given course.
        """
        info = self.get_site_info()
        body = self._call("gradereport_overview_get_course_grades", userid=info.userid)
        return [CourseGrade.model_validate(g) for g in body.get("grades") or []]

    def get_grade_items(self, course_id: int) -> list[GradeItem]:
        """Per-item grade breakdown for one course.

        Raises :class:`MoodleAPIError` with ``errorcode == "nopermissiontoviewgrades"``
        when the instructor has not enabled the gradebook for students in this course —
        this is a per-course setting, not a blanket token restriction.
        """
        info = self.get_site_info()
        body = self._call(
            "gradereport_user_get_grade_items", courseid=course_id, userid=info.userid
        )
        usergrades = body.get("usergrades") or [{}]
        return [GradeItem.model_validate(item) for item in usergrades[0].get("gradeitems") or []]

    # -- convenience -------------------------------------------------------------

    def resolve_course(self, reference: str) -> Course:
        """Resolve a course id or shortname to a course.

        Shortnames are matched case-insensitively, exactly first and then by prefix, so
        ``IOS460`` finds ``IOS460 - 123246``.
        """
        courses = self.list_courses(view="all-including-hidden")
        if reference.isdigit():
            wanted = int(reference)
            for course in courses:
                if course.id == wanted:
                    return course
            raise MoodleError(f"No enrolled course with id {reference}")

        needle = reference.casefold().strip()
        for course in courses:
            if course.shortname.casefold() == needle:
                return course

        matches = [c for c in courses if c.shortname.casefold().startswith(needle)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MoodleError(f"No enrolled course matching {reference!r}")
        names = ", ".join(sorted(c.shortname for c in matches))
        raise MoodleError(f"{reference!r} is ambiguous; matches: {names}")


def _unwrap_batched(response: dict[str, Any], function: str) -> Any:
    """One entry of a batched answer, raised or decoded as if it had been sent alone.

    The batch endpoint answers 200 for the request as a whole and reports each call's
    outcome inside it, with both the data and the exception JSON-encoded as strings. An
    unread ``error`` flag here is the same failure mode as an unread error body on a
    single call: a failure that reaches the caller wearing the shape of data.
    """
    if response.get("error"):
        raw = response.get("exception") or "{}"
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = {}
        raise MoodleAPIError(
            errorcode=str(detail.get("errorcode", "unknown")),
            message=str(detail.get("message") or "Request failed"),
            function=function,
        )
    data = response.get("data")
    if data is None:
        return None
    try:
        decoded = json.loads(data)
    except ValueError as exc:
        raise MoodleError(f"{function} returned a non-JSON response in a batch") from exc
    # A batched call can still answer with an ordinary error payload rather than by
    # setting the error flag, so it goes through the same check as an unbatched one.
    check_api_error(decoded, function=function)
    return decoded


def check_api_error(body: Any, *, function: str | None = None) -> None:
    """Raise if a decoded response body is an error payload rather than data."""
    if isinstance(body, dict) and ("exception" in body or "errorcode" in body):
        raise MoodleAPIError(
            errorcode=str(body.get("errorcode", "unknown")),
            message=str(body.get("message") or body.get("error") or "Request failed"),
            function=function,
        )


def check_warnings(body: Any, *, function: str) -> None:
    """Raise if a decoded response carries a non-empty ``warnings`` array.

    Moodle reports a per-item failure there while still answering 200 with whatever it
    could read, so a warnings array left unread turns a partial result into one that
    cannot be told apart from an empty one.
    """
    warnings = body.get("warnings") if isinstance(body, dict) else None
    if not warnings:
        return
    first = warnings[0] if isinstance(warnings[0], dict) else {}
    raise MoodleAPIError(
        errorcode=str(first.get("warningcode", "warning")),
        message=str(first.get("message") or "The request completed with warnings"),
        function=function,
    )


def _lookup(table: dict[str, str], key: str, label: str) -> str:
    try:
        return table[key]
    except KeyError:
        options = ", ".join(sorted(table))
        raise ValueError(f"Unknown {label} {key!r}. Valid options: {options}") from None
