"""What a campus exposes to this token, and what that means for this tool.

Every campus enables a different slice of Moodle's web-service API, and the difference is
not cosmetic: a token that cannot call ``gradereport_user_get_grade_items`` makes
``course grades`` fail no matter how the command is written. ``get_site_info`` already
reports the list, so the answer to "will this work on my campus" is knowable before
anything is tried — this maps that list onto the commands it backs.

The table is deliberately wider than what is implemented today. A feature listed here with
no command yet reads as a roadmap entry that the campus has already agreed to, which is a
more useful thing to learn from a real token than from a specification.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    """Whether a feature's functions are all there, some, or none."""

    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Feature:
    """A capability, the commands that need it, and the functions it is built on."""

    name: str
    #: The command(s) this backs. Empty for a feature with no command of its own, which is
    #: either one not built yet or one the tool applies on its own; `built` tells them
    #: apart, since "no command" alone would report transport plumbing as missing work.
    commands: str
    functions: tuple[str, ...]
    summary: str
    built: bool = True


@dataclass(frozen=True)
class FeatureStatus:
    feature: Feature
    missing: tuple[str, ...]

    @property
    def state(self) -> State:
        if not self.missing:
            return State.OK
        if len(self.missing) < len(self.feature.functions):
            return State.PARTIAL
        return State.UNAVAILABLE


#: Ordered so that what is implemented comes first and the rest reads as what is possible.
FEATURES: tuple[Feature, ...] = (
    Feature(
        "courses",
        "courses list, courses search",
        ("core_course_get_enrolled_courses_by_timeline_classification",),
        "List your enrolled courses.",
    ),
    Feature(
        "contents",
        "course contents",
        ("core_course_get_contents",),
        "Read a course's sections, activities and files.",
    ),
    Feature(
        "participants",
        "course participants",
        ("core_enrol_get_enrolled_users",),
        "List who is enrolled in a course.",
    ),
    Feature(
        "announcements",
        "course announcements",
        ("mod_forum_get_forums_by_courses", "mod_forum_get_forum_discussions"),
        "Read a course's news-forum posts.",
    ),
    Feature(
        "assignments",
        "course assignments, course assignment-status",
        ("mod_assign_get_assignments", "mod_assign_get_submission_status"),
        "Track assignments, due dates and submission status.",
    ),
    Feature(
        "quizzes",
        "course quizzes, course quiz-status",
        (
            "mod_quiz_get_quizzes_by_courses",
            "mod_quiz_get_user_attempts",
            "mod_quiz_get_user_best_grade",
        ),
        "Track quizzes, attempt counts and best grades.",
    ),
    Feature(
        "grades",
        "courses grades, course grades",
        ("gradereport_overview_get_course_grades", "gradereport_user_get_grade_items"),
        "Read the gradebook, per course and per item.",
    ),
    Feature(
        "calendar",
        "courses calendar, course calendar",
        (
            "core_calendar_get_action_events_by_timesort",
            "core_calendar_get_action_events_by_course",
        ),
        "See what is due, across every course or one.",
    ),
    Feature(
        "updates",
        "course updates",
        ("core_course_check_updates", "core_course_get_updates_since"),
        "Ask what changed in a course since a given moment.",
    ),
    Feature(
        "quiz review",
        "course quiz-review",
        ("mod_quiz_get_attempt_review",),
        "Read back a finished attempt with its marks and feedback.",
    ),
    Feature(
        "batching",
        "",
        ("tool_mobile_call_external_functions",),
        "Several calls in one request, used automatically where it helps.",
    ),
    Feature(
        "forum posting",
        "",
        ("mod_forum_add_discussion", "mod_forum_add_discussion_post"),
        "Start a discussion or reply to one.",
        built=False,
    ),
    Feature(
        "assignment submission",
        "",
        ("mod_assign_save_submission", "mod_assign_submit_for_grading"),
        "Upload and submit work for grading.",
        built=False,
    ),
    Feature(
        "quiz attempts",
        "",
        (
            "mod_quiz_get_attempt_access_information",
            "mod_quiz_start_attempt",
            "mod_quiz_get_attempt_data",
            "mod_quiz_process_attempt",
        ),
        "Start, answer and submit a quiz attempt.",
        built=False,
    ),
    Feature(
        "campus search",
        "",
        ("core_search_get_results",),
        "Search course content site-wide, not only names.",
        built=False,
    ),
    Feature(
        "messaging",
        "",
        ("core_message_get_conversations", "core_message_send_instant_messages"),
        "Read and send campus messages.",
        built=False,
    ),
)


def evaluate(available: set[str]) -> list[FeatureStatus]:
    """Match every known feature against the functions this token may call.

    Order is preserved from :data:`FEATURES` so the output reads the same on every
    campus; sorting by state would move a row between two runs of the same command.
    """
    return [
        FeatureStatus(feature, tuple(f for f in feature.functions if f not in available))
        for feature in FEATURES
    ]


def group_by_component(available: set[str]) -> list[tuple[str, int]]:
    """Function counts per Moodle component, e.g. ``("mod_quiz", 18)``.

    The component is the function name minus its trailing verb, which Moodle does not
    publish as a separate field. Splitting on the first two underscores recovers it for
    every name that follows the convention (``mod_quiz_…``, ``core_course_…``) and leaves
    anything shorter as its own group rather than dropping it.
    """
    counts: dict[str, int] = {}
    for name in available:
        parts = name.split("_")
        component = "_".join(parts[:2]) if len(parts) > 2 else name
        counts[component] = counts.get(component, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
