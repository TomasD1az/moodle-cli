# moodle-cli

Command line client and MCP server for a Moodle campus. Lists your courses, reads their
contents and announcements, downloads the material, shows who is enrolled, and tracks
assignments, quizzes and grades — over Moodle's web-service API, with no browser and no
HTML scraping.

Works with any Moodle instance that has web services and the mobile service enabled.

- [Install](#install)
- [Authentication](#authentication)
- [MCP server](#mcp-server)
- [Plugins](#plugins)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [Exit codes](#exit-codes)
- [Things worth knowing](#things-worth-knowing)
- [Development](#development)

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/varbelaiz/moodle-cli
cd moodle-cli
uv sync
```

Commands below are written as `moodle …`. Without installing the package on your PATH,
prefix them with `uv run`, as in `uv run moodle courses list`.

To install it on your PATH instead of working from a checkout:

```bash
uv tool install "moodle-cli @ git+https://github.com/varbelaiz/moodle-cli"
```

That tracks the tip of the repo — "moodle-cli" is taken on PyPI by an unrelated project,
so there is no index to resolve a bare `moodle-cli` against. To pin a specific release
instead, append `@vX.Y.Z` for a tag from
[Releases](https://github.com/varbelaiz/moodle-cli/releases). See
[Updating](docs/updating.md) for details, and for `moodle update`, which moves an install
already on your PATH to the latest release.

`uv sync` in a checkout installs the core alone, on purpose: the default test run has to
prove that moodle-cli works with no plugins present.

## Authentication

Point the tool at your campus and log in once:

```bash
export MOODLE_URL=https://campus.example.edu
moodle auth login
```

`auth login` exchanges your password for a long-lived web-service token and stores it in
the system keyring. The password is prompted for, never taken as an argument, and is not
needed again afterwards.

If your campus uses SSO only and you have no local Moodle password, `login/token.php`
cannot mint a token for you. Where the site allows it, you can create one by hand under
Preferences → Security keys in your Moodle profile, then set `MOODLE_TOKEN`.

## MCP server

Exposes the same functionality to an agent over stdio:

```bash
claude mcp add moodle -- uv run --project /path/to/moodle-cli moodle-mcp
```

Each MCP tool and its CLI counterpart are documented together — see the command reference
below.

## Plugins

Campuses differ in ways that do not belong in this tool: a recordings platform one
university uses, a conversion step another wants. A plugin is a separate package that adds
its own command group and its own MCP tools. It cannot wrap, replace or modify anything
here, which is what keeps the behavior of every command below readable from this repository
alone.

```bash
moodle plugins list
moodle plugins install NAME
```

Nothing is installed by default, and a plugin that fails to load is skipped rather than
taken as fatal. See [docs/plugins.md](docs/plugins.md).

## Command reference

Every command that answers a question accepts `--json`, which prints machine-readable
output instead of a table. The ones that act rather than answer do not: `auth login`,
`auth status`, `auth logout` and `course download` report progress as they go.
Commands that take a course accept either its numeric id or a shortname prefix; a
prefix matching more than one course is an error listing the candidates.

Each group below links to a docs page with full options, MCP parameters and behavioral
notes. An installed plugin adds a group of its own; `moodle plugins list` shows which.

### [Authentication](docs/auth.md)

| CLI | Description |
| --- | --- |
| `moodle auth login` | Mint a token and store it in the keyring. |
| `moodle auth status` | Show who the stored token belongs to, and what it can do. |
| `moodle auth logout` | Delete the stored token. |

### [Updating](docs/updating.md)

| CLI | Description |
| --- | --- |
| `moodle version` | Show the installed version, optionally checking for a newer release. |
| `moodle update` | Upgrade this installation to the latest GitHub release. |

### [Plugins](docs/plugins.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle plugins list` | — | List the official plugins and what each one adds. |
| `moodle plugins install` | — | Install a plugin into this installation of moodle. |
| `moodle plugins uninstall` | — | Remove a plugin. |

### [Browsing and searching courses](docs/courses.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle courses list` | `list_courses` | List your enrolled courses. |
| `moodle course contents` | `get_course_contents` | Show one course's sections, files and links. |
| `moodle courses search` | `search_courses` | Search names and links across every enrolled course. |

### [Downloading files](docs/downloads.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle course download` | `download_course_files` | Download a course's files, narrowed by section, type, name or glob. |

### [Participants](docs/participants.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle course participants` | `list_participants` | List the people enrolled in a course. |

### [Announcements](docs/announcements.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle course announcements` | `get_course_announcements` | List a course's news-forum posts. |

### [Assignments](docs/assignments.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle courses assignments` | `get_assignments` | List assignments and due dates across every course. |
| `moodle course assignments` | `get_assignments` | List one course's assignments and due dates. |
| `moodle course assignment-status` | `get_assignment_status` | Show submission and grading status for one assignment. |

### [Quizzes](docs/quizzes.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle course quizzes` | `get_quizzes` | List a course's quizzes and their open/close windows. |
| `moodle course quiz-status` | `get_quiz_status` | Show attempt count and best grade for one quiz. |

### [Grades](docs/grades.md)

| CLI | MCP tool | Description |
| --- | --- | --- |
| `moodle courses grades` | `get_grade_summary` | Show a grade summary across every enrolled course. |
| `moodle course grades` | `get_grades` | Show the per-item grade breakdown for one course. |

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `MOODLE_URL` | yes | Campus base URL, e.g. `https://campus.example.edu`. |
| `MOODLE_TOKEN` | no | Use this token directly and skip the keyring. |
| `MOODLE_USER` | no | Username, so `auth login` does not prompt for it. |
| `MOODLE_PASS` | no | Password, for minting a token unattended. |

These can be set in the environment or in a `.env` file at the project root; see
[`.env.example`](.env.example). Token resolution order is `MOODLE_TOKEN`, then the keyring,
then minting a new one from `MOODLE_USER` and `MOODLE_PASS`.

Storing `MOODLE_PASS` is only needed for unattended use. After `auth login` the token lives
in the keyring and the password can be removed.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | API error, authentication failure, unknown course, unknown `--file` name, or one or more downloads failed. |
| `2` | Invalid command line arguments. |

Failures print to stderr, so `--json` output stays parseable.

## Things worth knowing

**Time-based course filters depend on your campus.** `--view in-progress`, `past` and
`future` are computed by Moodle from course start and end dates. Campuses that never set an
end date classify every course you have ever taken as "in progress", which makes
`in-progress` return everything and `past`/`future` return nothing. If that is your case,
use `--view all` or `--view starred` and read the `year` column.

**Filenames are not unique within a course.** Duplicating an activity produces two entries
with the same filename. Byte-identical copies are downloaded once; files that share a name
but differ in size are both kept, with the module id appended to the second.

**Downloads are verified, not assumed.** Moodle answers some failed requests with HTTP 200
and a JSON error body. Written to disk unchecked, that becomes a small JSON file wearing a
`.pdf` name. Every download is checked against its declared size and content type.

**Deadlines are moments, not days.** Moodle stores an instant, and it is read in the zone
of the machine running the tool. The default deadline is 23:59 campus time, so a bare date
names the following day for anyone east of the campus: 23:59 in Buenos Aires is 03:59 the
next morning in Rome. Every MCP timestamp therefore carries its offset, `due_at` and
`closes_at` included. The CLI tables still print a date, which is the right granularity to
read at a glance and the wrong one to compute a deadline from.

**Repeated calls in one command are batched where the campus allows it.** Where the
number of requests grows with the size of an enrolment — one per course's news forum, for
instance — they go out together through `tool_mobile_call_external_functions`, which is
the endpoint the official mobile app uses for the same reason. A campus that does not
expose it gets one request each instead, and nothing else about the answer changes.

**Course lists are read once per command, not once per question.** A single command often
needs the enrolment twice — to turn a shortname into an id, and to label rows that carry
only an id. That is cached for the life of one command and never written to disk, so a
course you enrol in shows up on the very next run.

**A broken plugin is skipped, not fatal.** A plugin that fails to import, targets a
different contract version, or claims a command name this tool owns is left out with a
warning while everything else keeps working. Set `MOODLE_NO_PLUGINS=1` to skip discovery
altogether, which is the way out if a plugin manages to break the command line itself.

See each docs page linked above for behavior specific to one command or tool.

## Development

```bash
uv run pytest           # unit tests, no network
uv run pytest --live    # integration tests against a real campus
uv run ruff check .
uv run ruff format .
uv run mypy
```

Unit-test fixtures are synthetic but shape-accurate, reproducing the quirks real campuses
return without committing anyone's personal data. The `--live` suite needs credentials in
the environment and is what catches a campus changing its API underneath the fixtures.

The suite runs with plugin discovery disabled, so whatever you have installed cannot change
what it proves. Tests that need a plugin construct a fake one.

### Releasing

The version comes from the git tag, not from a file — there is nothing to bump. From a
commit already green on `main`:

```bash
git tag vX.Y.Z
git push --tags
```

The tag push triggers a GitHub Release with generated notes. Only tag a commit that has
already passed CI on `main`: the release workflow builds and publishes, it does not
re-run lint or tests.
