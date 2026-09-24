# Calendar

What is due, across every enrolled course or within one. This is the broadest deadline
view the API offers: one call covers the whole campus and every activity type, where
[assignments](assignments.md) and [quizzes](quizzes.md) each cover one.

**Only activities that publish a deadline to the calendar appear.** A due date a teacher
wrote into a page, a label or an announcement is not one of them. Read this as the floor
of what you owe, not the ceiling.

## Across every course

CLI: `moodle courses calendar`
MCP: `get_calendar`

```
moodle courses calendar [--days N] [--overdue] [--limit N] [--json]
```

| Option | Description |
| --- | --- |
| `--days N` | How many days the window covers. Default 14. |
| `--overdue` | Cover the `N` days just past instead of the `N` ahead. |
| `--limit N` | Maximum events to return. Default 200. |

MCP parameters: `course` (optional — omit for every course), `days`, `overdue`, `limit`.

Both ends of the window are bounded on purpose: an unbounded future returns next year's
final exam alongside tomorrow's problem set, which is not what "what is due" means.

`--overdue` flips the window rather than filtering the result, so it is the same single
call. An overdue item's deadline is in the past, which is why it needs the other window
to be found at all.

## One course

CLI: `moodle course calendar`
MCP: `get_calendar` with `course`

```
moodle course calendar COURSE [--days N] [--overdue] [--limit N] [--json]
```

Narrowed server-side rather than by filtering a campus-wide answer, so a course with a
busy week is not crowded out of `--limit` by the rest of your enrolment.

## Reading the output

The CLI table prints the deadline **to the minute**, unlike every other table here, which
prints a bare date. A calendar is the one place where the hour is the whole point: a quiz
closing at 23:59 and one closing at 09:00 on the same day are not the same row. Overdue
rows are printed in red. See
[Deadlines are moments](../README.md#things-worth-knowing).

The event's action — "Add submission", "Attempt quiz now" — is in `--json` and not in the
table: it is the longest field by far, and `type` already says which activity it belongs
to.

Example CLI output:

```
                      3 due, next 14 days
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ due              ┃ course          ┃ what                 ┃ type  ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ 2026-03-19 18:00 │ IOS460 - 123246 │ Actividad semana 2…  │ quiz  │
│ 2026-02-26 22:00 │ I312 - 106931   │ Trabajo Práctico 1…  │ assign│
└──────────────────┴─────────────────┴──────────────────────┴───────┘
```

Example `--json` response:

```json
[
  {
    "id": 990117,
    "name": "Actividad semana 2 se cierra",
    "course": "IOS460 - 123246",
    "activity": "quiz",
    "instance_id": 42628,
    "due_at": "2026-03-19T18:00:00-03:00",
    "overdue": false,
    "action": "Intente resolver el cuestionario ahora",
    "actionable": true,
    "url": "https://campus.example.edu/mod/quiz/view.php?id=866948"
  }
]
```

`instance_id` is the activity's own id, which is what
[`course assignment-status`](assignments.md) and [`course quiz-status`](quizzes.md) take;
`activity` says which of the two applies. A site-level event belongs to no course and no
activity: its `course` is `"0"` and `activity`, `instance_id` and `action` are `null`.

`actionable` is Moodle's own answer to "can this still be acted on". An assignment past
its cutoff still carries an "Add submission" action, and only this flag tells it apart
from one you can still submit to.
