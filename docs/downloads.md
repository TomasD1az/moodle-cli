# Downloading files

## One course

CLI: `moodle course download`
MCP: `download_course_files`

```
moodle course download COURSE [--output, -o PATH] [--section N] [--type TYPE]
                        [--file NAME] [--match GLOB] [--dry-run] [--overwrite]
```

| Option | Description |
| --- | --- |
| `--output`, `-o PATH` | Destination directory. Default `./<shortname>/`. |
| `--section N` | Only this section number. Repeatable. |
| `--type TYPE` | Only these module types, e.g. `resource`, `folder`. Repeatable. |
| `--file NAME` | Exact filename, as shown by `course contents`. Repeatable. |
| `--match GLOB` | Case-insensitive glob on the filename, e.g. `'*.pdf'`. Repeatable. |
| `--dry-run` | List what would be downloaded and write nothing. |
| `--overwrite` | Re-download files that already exist. |

MCP parameters: `course`, `output_dir`, `sections[]`, `module_types[]`, `files[]`,
`match[]`, `dry_run`. There is no `overwrite` on the MCP side.

Downloads a course's files, mirroring its section structure; Moodle folders become nested
directories. `--section`/`sections` and `--type`/`module_types` narrow by structure,
`--file`/`files` and `--match`/`match` by filename — all four compose by intersection. A
`--file`/`files` name that matches nothing is an error, not a silent zero-file download,
and the message distinguishes a typo from a name excluded by another selector.

Re-running is safe and cheap: a file already on disk at the expected size is skipped, so
an interrupted download resumes by re-running the same command. Every transfer is
verified against the size the API declares; a truncated or bogus response is reported as
a failure and nothing partial is left behind.

The MCP tool writes to disk and returns a manifest of paths, sizes and per-file status —
never file contents. A course can hold hundreds of megabytes; read whatever is needed
from disk afterwards instead.

Example CLI output (not `--json`; there is no JSON mode for this command):

```
CS101: 2 files, 4.9 MB -> CS101/
  ok    Week 1/syllabus.pdf (200.0 KB)
  skip  Week 1/slides.pdf

2 downloaded, 1 already present
```

Example `download_course_files` response with `dry_run: true`:

```json
{
  "course": "CS101",
  "directory": "CS101",
  "dry_run": true,
  "files": [
    {"path": "CS101/Week 1/syllabus.pdf", "size": 204800, "module_type": "resource", "status": "planned"}
  ]
}
```

A real run replaces `dry_run` with a `summary`, and each file's `status` becomes
`downloaded`, `skipped`, or `failed` (with an `error` message):

```json
{
  "course": "CS101",
  "directory": "CS101",
  "summary": {"downloaded": 1, "already_present": 0, "failed": 0},
  "files": [
    {"path": "CS101/Week 1/syllabus.pdf", "size": 204800, "module_type": "resource", "status": "downloaded"}
  ]
}
```

## Every course

CLI: `moodle courses download`
MCP: none — an agent asks per course, which is also how it names what it got.

```
moodle courses download [--output, -o PATH] [--view VIEW] [--type TYPE]
                        [--match GLOB] [--links] [--dry-run] [--overwrite]
```

| Option | Description |
| --- | --- |
| `--output`, `-o PATH` | Parent directory. Default: the current one. |
| `--view VIEW` | Which courses to include. Same values as `courses list`. Default `all`. |

The same download as above, swept across your enrolment: each course lands in its own
subdirectory named after its shortname, mirroring its sections inside. Re-running is as
cheap as it is for one course, since a file already on disk at the expected size is
skipped.

`--file` and `--section` are not offered here, and the command rejects them. A filename or
a section number identifies something inside *one* course; asked across every course it
would either fail on the first course that lacks it or mean something different in each.
`--type` and `--match` describe files rather than positions, so they carry across.

A course that cannot be read is reported and skipped rather than ending the run — over a
whole enrolment, an archived or restricted course is ordinary rather than exceptional.

```
  skip  I204 - 101313: No tiene permiso [nopermissions]
I310 - 106934: 12 files, 31.2 MB -> I310 - 106934/
  ok    01 - Unidad 1/apunte.pdf (2.1 MB)
IOS460 - 123246: 3 files, 4.9 MB -> IOS460 - 123246/
  skip  01 - Presentación/Programa - Taller.pdf

14 downloaded, 1 already present across 2 courses
```
