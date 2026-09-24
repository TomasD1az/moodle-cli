"""Domain models mirroring the Moodle web-service response shapes.

Field selection is deliberate: Moodle returns far more than we need, so every model
ignores unknown keys rather than tracking the full upstream schema.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


def epoch_to_datetime(value: int) -> datetime | None:
    """The one epoch conversion, resolved to the reader's local zone.

    Moodle uses 0, not null, for unset timestamps. Every surface converts here: a second
    conversion pinned to another zone reports a different calendar day for the same
    evening event, and the campus web UI shows local time.
    """
    return datetime.fromtimestamp(value, tz=UTC).astimezone() if value else None


#: Tags that end a line of prose. Everything else is stripped without a separator.
_BLOCK_TAG = re.compile(
    r"</?(?:br|p|div|li|ul|ol|tr|table|h[1-6]|blockquote)\b[^>]*>", re.IGNORECASE
)
_ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    """Plain text from a Moodle HTML fragment, one line per block element.

    Moodle stores authored prose as HTML. Dropping tags without putting a separator in
    their place runs the last word of a block into the first word of the next, and an
    entity left encoded reaches the reader as ``&nbsp;``.
    """
    stripped = _ANY_TAG.sub("", _BLOCK_TAG.sub("\n", markup))
    lines = (" ".join(line.split()) for line in unescape(stripped).split("\n"))
    return "\n".join(line for line in lines if line)


def _default_if_null(default: Any) -> BeforeValidator:
    """Read a null as "unset".

    The campus sends ``null`` for an optional field instead of omitting it, so a plain
    default never applies and a non-optional annotation raises before the caller sees the
    response. Fields carrying a real absent/present distinction stay explicitly nullable.
    """
    return BeforeValidator(lambda value: default if value is None else value)


_Text = Annotated[str, _default_if_null("")]
_Epoch = Annotated[int, _default_if_null(0)]
_Number = Annotated[float, _default_if_null(0.0)]


class Course(_Base):
    id: int
    shortname: str
    fullname: str
    category: str = Field(default="", alias="coursecategory")
    startdate: int = 0
    enddate: int = 0
    isfavourite: bool = False
    hidden: bool = False
    progress: float | None = None
    viewurl: str = ""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @property
    def started_at(self) -> datetime | None:
        return epoch_to_datetime(self.startdate)

    @property
    def ended_at(self) -> datetime | None:
        """None on this campus: course end dates are never configured."""
        return epoch_to_datetime(self.enddate)


class CourseFile(_Base):
    """A file entry inside a module's ``contents``.

    Several of these fields arrive as ``null`` rather than absent (``filepath`` on url
    modules, for one), so they are optional rather than defaulted strings.
    """

    filename: str
    filepath: str | None = None
    filesize: int = 0
    fileurl: str | None = None
    mimetype: str | None = None
    timemodified: int = 0
    type: str | None = None
    isexternalfile: bool = False

    @property
    def is_downloadable(self) -> bool:
        """Whether this entry is a real file rather than a link.

        ``url`` modules carry entries with ``type == "url"`` that point at external sites.
        ``isexternalfile`` is deliberately *not* excluded: it marks a file stored in an
        external repository, which Moodle still serves through ``pluginfile.php`` (the
        course bibliography folders are all flagged this way).
        """
        return self.type == "file" and bool(self.fileurl)


class Module(_Base):
    id: int
    name: str
    modname: str
    url: str | None = None
    visible: bool = True
    uservisible: bool = True
    #: Full HTML body. ``name`` is a preview Moodle itself truncates to ~50 chars for
    #: modules like ``label`` that carry their entire content in this field.
    description: _Text = ""
    contents: list[CourseFile] = Field(default_factory=list)

    @property
    def files(self) -> list[CourseFile]:
        return [c for c in self.contents if c.is_downloadable]

    @property
    def links(self) -> list[CourseFile]:
        """External link entries, e.g. a ``url``-type module's actual target.

        These carry no bytes to download, but the destination itself is real information
        that ``files`` deliberately excludes.
        """
        return [c for c in self.contents if c.type == "url" and c.fileurl]

    @property
    def description_text(self) -> str:
        return html_to_text(self.description)


class Section(_Base):
    id: int
    name: str
    section: int = 0
    visible: bool = True
    uservisible: bool = True
    summary: _Text = ""
    modules: list[Module] = Field(default_factory=list)

    @property
    def summary_text(self) -> str:
        return html_to_text(self.summary)


class Role(_Base):
    roleid: int = 0
    name: str = ""
    shortname: str = ""


class Participant(_Base):
    id: int
    fullname: str
    firstname: str = ""
    lastname: str = ""
    email: str | None = None
    roles: list[Role] = Field(default_factory=list)
    lastcourseaccess: int = 0

    @property
    def role_names(self) -> list[str]:
        return [r.shortname for r in self.roles]

    @property
    def last_course_access(self) -> datetime | None:
        return epoch_to_datetime(self.lastcourseaccess)


class Announcement(_Base):
    """A post in a course's news forum.

    ``courseid`` is not part of the raw discussion payload; ``MoodleClient.get_announcements``
    injects it so callers aggregating across courses can tell posts apart.
    """

    id: int
    courseid: int = 0
    subject: _Text = ""
    message: _Text = ""
    userfullname: _Text = ""
    created: _Epoch = 0
    numreplies: int = 0
    pinned: bool = False

    @property
    def posted_at(self) -> datetime | None:
        return epoch_to_datetime(self.created)

    @property
    def message_text(self) -> str:
        """``message`` as plain text; forum posts arrive as HTML."""
        return html_to_text(self.message)


class Forum(_Base):
    """A forum activity. Only ``type == "news"`` carries a course's announcements."""

    id: int
    course: int
    type: str = ""
    name: str = ""


class Assignment(_Base):
    id: int
    cmid: int = 0
    course: int = 0
    name: _Text = ""
    duedate: _Epoch = 0
    allowsubmissionsfromdate: _Epoch = 0
    cutoffdate: _Epoch = 0
    grade: _Number = 0

    @property
    def due_at(self) -> datetime | None:
        return epoch_to_datetime(self.duedate)

    @property
    def scale_graded(self) -> bool:
        """A negative ``grade`` is a scale id, not a maximum: -52 means "graded by scale 52".

        The scale's name is not in this payload, so a caller can say the assignment is
        scale-graded but not which scale; ``get_assignment_status`` resolves the awarded
        value through ``gradefordisplay``.
        """
        return self.grade < 0

    @property
    def max_grade(self) -> float | None:
        """Point maximum, or None when the assignment is scale-graded or ungraded."""
        return self.grade if self.grade > 0 else None


#: ``gradingstatus`` values meaning the student can see a grade.
_GRADED_STATUSES = frozenset({"graded", "released"})


class AssignmentStatus(_Base):
    """Curated view of ``mod_assign_get_submission_status``.

    The raw response nests submission data under plugin-specific shapes that vary with
    which submission methods (file, online text, ...) the assignment enables. Rather than
    model that whole tree, ``MoodleClient.get_assignment_status`` extracts the fields
    below by hand.
    """

    status: str | None = None
    gradingstatus: _Text = ""
    grade: str | None = None
    gradefordisplay: _Text = ""
    extensionduedate: _Epoch = 0
    submitted_files: list[str] = Field(default_factory=list)

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"

    @property
    def graded(self) -> bool:
        """A course using a marking workflow says "released" where a plain one says "graded".

        The other workflow states (in marking, ready for review, ...) are stages the
        student cannot see a grade from, so they read as ungraded.
        """
        return self.gradingstatus in _GRADED_STATUSES

    @property
    def extension_due_at(self) -> datetime | None:
        return epoch_to_datetime(self.extensionduedate)


class Quiz(_Base):
    id: int
    course: int = 0
    name: _Text = ""
    timeopen: _Epoch = 0
    timeclose: _Epoch = 0
    #: Attempts allowed, where 0 is Moodle's encoding for "unlimited".
    attempts: int = 0
    #: The maximum a grade for this quiz is scaled to.
    grade: _Number = 0

    @property
    def opens_at(self) -> datetime | None:
        return epoch_to_datetime(self.timeopen)

    @property
    def closes_at(self) -> datetime | None:
        return epoch_to_datetime(self.timeclose)


class QuizStatus(_Base):
    """Curated view of one quiz's attempt history and grade.

    No single endpoint answers "did I take this and how did it go", so
    ``MoodleClient.get_quiz_status`` assembles this from several.
    """

    attempt_count: int = 0
    last_state: str | None = None
    #: Whether a grade is available to read; a hidden or pending grade is also False.
    has_grade: bool = False
    grade: float | None = None
    grade_to_pass: float | None = None
    #: The maximum ``grade`` and ``grade_to_pass`` are scaled to.
    max_grade: float | None = None


class EventAction(_Base):
    """What a calendar event is asking you to do, and whether it is still open.

    ``actionable`` is Moodle's own answer to "can this be acted on right now": a quiz
    outside its window and an assignment past its cutoff both still carry an action, and
    only this flag tells them apart from one you can still act on.
    """

    name: _Text = ""
    url: _Text = ""
    itemcount: int = 0
    actionable: bool = False


class EventCourse(_Base):
    """The course an event belongs to, as the calendar endpoints nest it.

    A site-level event carries no course at all, which is why the parent field is
    nullable rather than defaulted: "no course" and "course 0" are different answers.
    """

    id: int = 0
    shortname: _Text = ""
    fullname: _Text = ""


class CalendarEvent(_Base):
    """A dated, actionable item: an assignment due, a quiz closing, a chat starting.

    These are *action* events, not the whole calendar. Moodle builds them per activity
    module, so an event exists only where a module chose to publish one — which is what
    makes this the one endpoint that answers "what is due" across every course at once,
    and also why a deadline a teacher wrote into a page rather than into a due date will
    never appear here.

    ``timesort`` is the field to order by, not ``timestart``: for an event with a
    duration the two differ, and the calendar's own ordering is by ``timesort``.
    """

    id: int
    name: _Text = ""
    description: _Text = ""
    eventtype: _Text = ""
    #: The activity type, e.g. ``assign`` or ``quiz``. Empty for a non-module event.
    modulename: _Text = ""
    #: The activity instance id, which is what the per-activity endpoints take.
    instance: int = 0
    timestart: _Epoch = 0
    timesort: _Epoch = 0
    timeduration: _Epoch = 0
    overdue: bool = False
    url: _Text = ""
    viewurl: _Text = ""
    course: EventCourse | None = None
    action: EventAction | None = None

    @property
    def starts_at(self) -> datetime | None:
        return epoch_to_datetime(self.timestart)

    @property
    def sorts_at(self) -> datetime | None:
        """The instant the calendar orders this event by; the deadline, for a deadline."""
        return epoch_to_datetime(self.timesort)

    @property
    def description_text(self) -> str:
        """``description`` as plain text; the calendar stores it as HTML."""
        return html_to_text(self.description)

    @property
    def course_id(self) -> int:
        """0 for a site-level event, which belongs to no course."""
        return self.course.id if self.course else 0

    @property
    def action_name(self) -> str:
        """What to do about it — "Add submission", "Attempt quiz now" — or "" if nothing."""
        return self.action.name if self.action else ""

    @property
    def actionable(self) -> bool:
        """Whether the action can still be taken; an expired deadline still has an action."""
        return bool(self.action and self.action.actionable)


class CourseGrade(_Base):
    courseid: int
    grade: _Text = ""


#: ``itemtype`` -> name for the rows Moodle sends with a null ``itemname``.
_ITEM_TYPE_LABELS = {"course": "Course total", "category": "Category subtotal"}


class GradeItem(_Base):
    """A row in a course's per-item grade breakdown.

    A category subtotal row carries no ``itemname`` and no ``itemmodule`` — both arrive
    ``null`` rather than absent or empty. ``itemtype`` is what identifies such a row.
    """

    itemname: str | None = None
    itemtype: str | None = None
    itemmodule: str | None = None
    graderaw: float | None = None
    grademax: _Number = 0
    gradeformatted: _Text = ""
    percentageformatted: _Text = ""
    feedback: _Text = ""

    @property
    def label(self) -> str:
        """A name for the row, including the aggregate rows Moodle leaves unnamed."""
        return self.itemname or _ITEM_TYPE_LABELS.get(self.itemtype or "", "-")


class SiteInfo(_Base):
    sitename: str = ""
    username: str = ""
    fullname: str = ""
    userid: int = 0
    release: str = ""
    downloadfiles: bool = False
    functions: list[dict[str, object]] = Field(default_factory=list)

    @property
    def function_names(self) -> set[str]:
        return {str(f["name"]) for f in self.functions if "name" in f}
