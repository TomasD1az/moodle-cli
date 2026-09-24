# Authentication

CLI only — there is no MCP equivalent, since an agent talks over a token that's already
in place.

## `moodle auth login`

```
moodle auth login [--username, -u USERNAME]
```

| Option | Description |
| --- | --- |
| `--username`, `-u` | Campus username. Prompted for if omitted. |

Mints a web-service token and stores it in the system keyring. The password is always
prompted for, never taken as an argument — that would leave it in shell history. Once the
token is stored, `MOODLE_PASS` can be removed from `.env`.

Example output:

```
Logged in as Jane Doe (id 42)
  site: Example University  (4.4.1)
  token stored in the system keyring
```

## `moodle auth status`

```
moodle auth status
```

Shows whether a usable token exists and who it belongs to: full name, user id, site name,
number of web-service functions available, and whether file downloads are allowed.

Example output:

```
Authenticated as Jane Doe (id 42)
  site: Example University
  functions available: 187
  file downloads allowed: True
```

## `moodle auth capabilities`

```
moodle auth capabilities [--functions] [--json]
```

Shows which of this tool's features the campus actually exposes to your token, and names
the web-service functions a missing one would need. See
[Campus capabilities](capabilities.md).

## `moodle auth logout`

```
moodle auth logout
```

Deletes the stored token from the keyring.

Example output:

```
Token deleted from the keyring.
```
