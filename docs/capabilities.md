# Campus capabilities

CLI only — there is no MCP equivalent, since an agent talks over a token whose reach is
already fixed.

```
moodle auth capabilities [--functions] [--json]
```

| Option | Description |
| --- | --- |
| `--functions` | List every function name instead of the per-feature view. |

Every campus enables a different slice of Moodle's web-service API, and the difference is
not cosmetic: a token that cannot call `gradereport_user_get_grade_items` makes
`course grades` fail no matter how the command is written. `get_site_info` already reports
which functions a token may call, so this answers "will this work on my campus" before
anything is tried — and names the functions a missing feature needs, which is what you
would ask a Moodle administrator to enable.

Features listed with no command are ones the campus supports and this tool does not
implement yet. That is deliberate: a roadmap entry the campus has already agreed to is a
more useful thing to learn from a real token than from a specification.

Example output:

```
              Example University — 187 functions available
┏━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ feature        ┃      ┃ command                 ┃ needs                ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ courses        │ yes  │ courses list, courses…  │                      │
│ calendar       │ yes  │ courses calendar, cou…  │                      │
│ grades         │ part │ courses grades, cours…  │ gradereport_user_g…  │
│ batching       │ yes  │ (automatic)             │                      │
│ quiz attempts  │ no   │ -- not built yet --     │ mod_quiz_start_att…  │
└────────────────┴──────┴─────────────────────────┴──────────────────────┘
file downloads allowed: True
```

`part` means some of a feature's functions are exposed and some are not; the `needs`
column names the missing ones.

The `command` column distinguishes three cases: a command name, `(automatic)` for
something the tool applies on its own without a command of its own — batching is the one
today — and `-- not built yet --` for a feature the campus supports and this tool does not
implement. In `--json` the last two are told apart by `built`.

Example `--json` response:

```json
{
  "site": "Example University",
  "release": "4.4.1",
  "functions_available": 187,
  "file_downloads_allowed": true,
  "features": [
    {
      "name": "quizzes",
      "state": "partial",
      "commands": "course quizzes, course quiz-status",
      "built": true,
      "summary": "Track quizzes, attempt counts and best grades.",
      "missing": ["mod_quiz_get_user_attempts"]
    }
  ],
  "components": [{"component": "mod_quiz", "functions": 18}],
  "functions": null
}
```

`functions` is `null` unless `--functions` was passed, in which case it carries every
name. `components` groups the count by Moodle component, which is the quickest way to see
whether a module is exposed at all.

## An empty function list

A campus can answer `get_site_info` without a function list. Reporting that as "nothing
works" would be a confident wrong answer — every command might still succeed — so this
says so plainly instead and checks nothing.
