# Quizzes

## List a course's quizzes

CLI: `moodle course quizzes`
MCP: `get_quizzes`

```
moodle course quizzes COURSE [--json]
```

MCP parameter: `course`, optional — omit it to check every enrolled course.

Lists a course's quizzes and their open/close windows, attempt limit and max grade.
`attempts` (CLI) / `attempt_limit` (MCP) is unlimited when the quiz sets no cap — shown as
`unlimited` on the CLI and `null` over MCP.

Over MCP, `opens_at` and `closes_at` are full timestamps carrying their offset; the CLI
table prints the date alone, which is the right granularity to scan and the wrong one to
compute a deadline from. See [Deadlines are moments](../README.md#things-worth-knowing).

The `id` column/field feeds into quiz status below.

Example `--json` response — the raw quiz fields:

```json
[
  {
    "id": 3305,
    "course": 101,
    "name": "Quiz 1: Variables and Loops",
    "timeopen": 1707868800,
    "timeclose": 1708473600,
    "attempts": 2,
    "grade": 10.0
  }
]
```

Example `get_quizzes` response — the same quiz, curated: `opens_at`/`closes_at` as dates
and `attempt_limit` in place of the raw `attempts`:

```json
[
  {
    "id": 3305,
    "course": "CS101",
    "name": "Quiz 1: Variables and Loops",
    "opens_at": "2024-02-14T00:00:00-03:00",
    "closes_at": "2024-02-21T23:59:00-03:00",
    "attempt_limit": 2,
    "max_grade": 10.0
  }
]
```

## Show one quiz's status

CLI: `moodle course quiz-status`
MCP: `get_quiz_status`

```
moodle course quiz-status QUIZ_ID [--course COURSE]
```

MCP parameters: `quiz_id`; `course`, optional.

`QUIZ_ID`/`quiz_id` is the id from the listing above, not a course-module id.

Pass the course whenever you know it. Reading the maximum a quiz grades out of means
finding the quiz, and a quiz id alone does not say which course holds it, so without the
hint every enrolled course's quizzes are fetched to locate one. Checking a course's
quizzes one at a time is the ordinary case, and it pulls the whole campus once per quiz.

Shows attempt count and best grade for one quiz. The grade is scaled to the quiz maximum,
which is why it's always printed alongside it. When no grade can be read, the response
says so without claiming the quiz is ungraded: the same flag covers an unattempted quiz,
one awaiting manual grading, and one whose marks the instructor hides.

`attempt_ids` are oldest first and are what `course quiz-review` below takes.

Example `--json` response:

```json
{
  "attempt_count": 1,
  "attempt_ids": [883899],
  "last_state": "finished",
  "has_grade": true,
  "grade": 8.5,
  "grade_to_pass": 5.0,
  "max_grade": 10.0
}
```

Example `get_quiz_status` response — same attempt, renamed fields:

```json
{
  "attempts_used": 1,
  "attempt_ids": [883899],
  "last_attempt_state": "finished",
  "grade_available": true,
  "grade": 8.5,
  "grade_to_pass": 5.0,
  "max_grade": 10.0
}
```

## Review a finished attempt

CLI: `moodle course quiz-review`
MCP: `get_quiz_review`

```
moodle course quiz-review ATTEMPT_ID [--questions] [--json]
```

| Option | Description |
| --- | --- |
| `--questions` | Print each question's full text, not just the summary table. |

MCP parameter: `attempt_id`.

`ATTEMPT_ID`/`attempt_id` is an id from `attempt_ids` above — an *attempt* id, not a quiz
id and not a course-module id.

This is the campus's own "Review" page read from the command line, and it is read-only:
the attempt is already over and nothing here changes it.

**What comes back depends on the quiz's review options**, which the teacher sets. The same
attempt yields more after the quiz closes than while it is still open. Three outcomes are
all normal rather than failures:

- no questions at all — review is not permitted yet;
- questions with no marks — the marks are withheld (`marks_visible` is `false`);
- one question with no mark among others that have them — it is awaiting manual grading,
  which is not the same as a mark of zero. `mark` is `null` there, never `0`.

**Questions arrive as rendered HTML, not as structured data.** That is Moodle's own shape:
there is no endpoint that returns a question's stem, its options and its correct answer as
separate fields, and the official mobile app renders the same markup. `--questions` and
the MCP `text` field strip it to plain text, which gives you the whole question as the
browser would draw it — wording, options, and (where the review options allow it)
feedback — as one block of prose per question.

Example CLI output:

```
attempt 1 (finished), finished 2026-03-18 14:56
grade: 6.93
                          3 questions
┏━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┓
┃ # ┃ type        ┃ status                     ┃   mark ┃
┡━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━┩
│ 1 │ multichoice │ Correcta                   │  1 / 1 │
│ 2 │ shortanswer │ Incorrecta                 │  0 / 1 │
│ 3 │ essay       │ Pendiente de calificación  │      - │
└───┴─────────────┴────────────────────────────┴────────┘
```

Example `get_quiz_review` response:

```json
{
  "attempt_id": 883899,
  "attempt_number": 1,
  "state": "finished",
  "finished_at": "2026-03-18T14:56:09-03:00",
  "grade": "6.93",
  "marks_visible": true,
  "questions": [
    {
      "number": "1",
      "type": "multichoice",
      "status": "Correcta",
      "mark": 1.0,
      "max_mark": 1.0,
      "flagged": false,
      "text": "¿Cuál de estas es una estructura de repetición?\na. if\nb. while\nLa respuesta correcta es: while"
    }
  ]
}
```

**Assignments and quizzes are separate Moodle activity types**, read through entirely
different web-service functions. An "Attempt quiz now" button is a quiz, not an
assignment — it won't appear in the assignments commands, and vice versa.
