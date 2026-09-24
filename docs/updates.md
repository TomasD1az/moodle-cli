# Course updates

CLI: `moodle course updates`
MCP: `get_course_updates`

```
moodle course updates COURSE [--days N] [--json]
```

| Option | Description |
| --- | --- |
| `--days N` | How far back to look. Default 7. |

MCP parameters: `course`, `days`.

Answers "is there anything new in this course" in two calls rather than by re-reading the
whole course and comparing it against a previous copy. Moodle tracks the change itself and
reports only the activities that moved.

The second call is `core_course_get_contents`, and it is what turns the course-module ids
the updates endpoint answers with into activity names. It is skipped entirely when nothing
changed, so a quiet course costs one request.

## What "changed" means

The areas are Moodle's own names, reported unchanged:

| Area | Meaning |
| --- | --- |
| `contentfiles` | A file in the activity was added or replaced. |
| `introfiles` | An attachment on the activity's description changed. |
| `configuration` | A setting changed — a due date, a name, visibility. |
| `gradeitems` | How the activity is graded changed. |
| `comments`, `ratings`, `completion` | The corresponding feature recorded something. |

The set is open-ended and module-specific, so treat an unfamiliar name as "this part of
the activity changed" rather than as an error. Nothing here is translated into a friendly
label on purpose: inventing one for the areas we happen to know would quietly hide every
area we do not.

**This reports movement, not news.** A teacher editing an activity marks it changed
whether or not anything you can see is different. To see what an activity now holds, run
`course contents` and read the named activity.

## Activities you cannot see

An activity can change and still not appear in the contents you are allowed to read. The
CLI prints `cmid 4041` for those and the MCP tool reports `"activity": null` — the change
is real, the name is simply not available to you.

Example CLI output:

```
        IOS460 - 123246: 2 activities changed, past 7 days
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ changed          ┃ activity               ┃ what                    ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2026-03-12 00:24 │ Programa de la materia │ configuration,          │
│                  │                        │ contentfiles            │
│ 2026-03-10 00:24 │ Trabajo Final          │ gradeitems              │
└──────────────────┴────────────────────────┴─────────────────────────┘
```

Example `--json` response, newest first:

```json
[
  {
    "cmid": 2,
    "activity": "Programa de la materia",
    "changed": ["configuration", "contentfiles"],
    "changed_at": "2026-03-12T00:24:00-03:00"
  }
]
```

An activity with two changed areas is dated by the later of the two.
