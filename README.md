# CalDAV MCP

[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](https://www.python.org)
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed)](https://modelcontextprotocol.io)

An MCP server that lets an AI assistant work with the appointments on any
**CalDAV** server (Nextcloud, mailbox.org, Radicale, …) in natural language:
see what is coming up, find an event, enter, move, change and delete one.

It is the calendar counterpart of
[MiniTask MCP](https://github.com/Stefan-Schmidbauer/minitask-mcp), which does
the same for tasks, and is built and deployed the same way.

## Concepts

- **Events live on a CalDAV server** as `VEVENT` items. The server connects on
  the first tool call, with the account from its environment.
- **Reading covers every calendar** of the account that holds events — pure
  task lists are left out. A tool call can name one calendar, by its name or
  its `url`.
- **`uri`** is the handle for an event. Always obtain it from `list_events` or
  `search_events` before changing or deleting an event.
- **A day** is `YYYY-MM-DD`, **a time** is `YYYY-MM-DDTHH:MM`. A day as start
  makes an all-day event. A time without a UTC offset is local time (`TZ`);
  results are shown in that zone, with the offset.
- **An all-day event ends on its last day.** `2026-10-14` to `2026-10-16` is
  three days — the tools convert to and from iCalendar's "day after".
- **A recurring event is one event with a rule.** A listing shows every
  occurrence in the range on its own, including ones that were moved or left
  out. They all share one `uri`; **`occurrence`** tells them apart — it is where
  the rule puts that occurrence, and stays the same when the occurrence is
  moved.
- **Changing a recurring event means saying which.** Pass an `occurrence` to
  move, change or cancel that one alone, or `whole_series` for all of them.
  Without either, `update_event` and `delete_event` leave a recurring event
  alone rather than guess.
- **A change edits the stored event** rather than rebuilding it, so reminders,
  attendees and anything else the tools do not know about are kept.

## Tools

| Tool | Description |
|---|---|
| `list_events` | Events in a date range (`from_date`, `to_date`; default: the next 7 days), from all calendars or one. Returns `events` and `warnings` |
| `search_events` | Events whose title, location or description contains `query`. Default range: 30 days back to a year ahead |
| `create_event` | Create an event — `title`, `start`, optional `end` (default: the same day, or one hour), `location`, `description`, `calendar` |
| `update_event` | Change an event by `uri` — pass only what should change (`title`, `start`, `end`, `location`, `description`). A new `start` without `end` moves the event and keeps its length. Of a recurring event: one `occurrence`, or `whole_series` |
| `delete_event` | Delete an event by `uri`. Of a recurring event: cancel one `occurrence`, or delete the `whole_series` |
| `list_calendars` | The calendars as `{name, url, default}` |

An event looks like this:

```json
{
  "uri": "https://cloud.example.net/remote.php/dav/calendars/me/personal/9F2C.ics",
  "calendar": "Personal",
  "title": "Dentist",
  "start": "2026-10-12T10:00+02:00",
  "end": "2026-10-12T11:00+02:00",
  "all_day": false,
  "location": "Main St 1",
  "description": "",
  "recurring": false,
  "occurrence": "",
  "cancelled": false
}
```

For an occurrence of a recurring event, `recurring` is `true` and `occurrence`
is set, e.g. `"2026-10-13T18:00+02:00"`.

`warnings` is not empty when something could not be read — a calendar the
server refused, or a list cut at 200 events. The assistant then knows the
answer may be incomplete instead of reporting a free day that is not one.

To remove a location or description, use `clear_location` / `clear_description`
rather than an empty string: some MCP clients drop empty-string arguments
before they reach the server.

Typical prompts and what they map to:

- *"What's on next week?"* → `list_events`
- *"When is the next parents' evening?"* → `search_events`
- *"Dentist on Tuesday at 10, Main St 1"* → `create_event`
- *"Move the dentist to Thursday 3:30 pm"* → `search_events` → `update_event` (start only)
- *"We're away from the 14th to the 16th"* → `create_event` (two days, all-day)
- *"Cancel the cinema on Friday"* → `list_events` → `delete_event`
- *"No training this Thursday"* → `list_events` → `delete_event` with that `occurrence`
- *"This week's training is on Friday at 7 instead"* → `list_events` → `update_event` with that `occurrence` and a new `start`
- *"Rename the training series to Yoga"* → `update_event` with `whole_series`

### Limits

- **The time of a whole series cannot be changed**, and neither can "this and
  all following". Single occurrences can be moved; title, location and
  description can be changed for the whole series. For a new weekly time, use
  a calendar app.
- **No new recurring events, no reminders, no invitations.** `create_event`
  makes plain single events. Existing reminders and attendees survive a change
  — a changed occurrence takes them over from its series — but nobody is
  notified of it.
- **A date range is at most 400 days**, and a listing at most 200 events.

## Configuration

Everything is configured through environment variables. Account settings and
secrets go into the env file on the server,
[`deploy/env.example`](deploy/env.example) is the template; the deployment
wiring is set in [`deploy/docker-compose.snippet.yml`](deploy/docker-compose.snippet.yml).

| Variable | |
|---|---|
| `CALDAV_URL` | **Required.** The CalDAV endpoint, e.g. `https://cloud.example.net/remote.php/dav` |
| `CALDAV_USERNAME` | **Required.** |
| `CALDAV_PASSWORD` | **Required.** Use an app password where the server offers one |
| `CALDAV_CALENDAR_URL` | The calendar new events go to when `create_event` names none — a `url` from `list_calendars`. Not needed with a single calendar |
| `TZ` | IANA name (e.g. `Europe/Berlin`) of the zone the tools speak in. Set in the snippet — without it the container runs in UTC and every event lands an hour or two off |
| `MCP_AUTH_TOKEN` | **Required.** Clients send `Authorization: Bearer <token>`. Without it the server refuses to start. Generate with `openssl rand -hex 32` |
| `MCP_PATH` | The path the endpoint is served under — `/caldav-mcp` in the snippet |
| `MCP_ALLOWED_HOSTS` | Comma-separated. The hostname the reverse proxy answers on, or every request is answered `421 Invalid Host header`. Setting it *replaces* the localhost defaults, so the snippet lists `127.0.0.1:9153` as well |
| `MCP_ALLOWED_ORIGINS` | Comma-separated. A client that sends an `Origin` header is answered `403` unless it is listed |
| `MCP_ALLOW_UNAUTHENTICATED` | `1` starts without a token — only for a deployment that authenticates further out |
| `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` | Set by the snippet (`streamable-http`, `0.0.0.0`, `8000`); no need to change them |

## Deployment

The server runs as a container on a Docker host and is reached over HTTPS.
`deploy.sh` builds the image *on that host*, through a Docker context over SSH —
no registry, no source checkout on the server — and recreates the container
through the compose file that already lives there.

**Prerequisites**

- A server with Docker Compose and a compose stack, reachable by SSH key as a
  user in the `docker` group, with `curl` installed.
- Docker with the `buildx` plugin on your machine (`docker buildx version`).
  The build runs on the server, but the builder is chosen locally, and the
  `Dockerfile`'s `COPY --chmod` needs BuildKit.
- A reverse proxy with TLS on the server — Tailscale, Caddy, nginx with
  Let's Encrypt, whatever is already there.

### 1. Clone

```bash
git clone https://github.com/Stefan-Schmidbauer/caldav-mcp.git
cd caldav-mcp
```

### 2. Prepare the server (once)

- Paste [`deploy/docker-compose.snippet.yml`](deploy/docker-compose.snippet.yml)
  under `services:` in the compose file on the server and adjust what its
  header names. The port is published on localhost only (`127.0.0.1:9153`).
- Create `.env.caldav-mcp` next to the compose file from
  [`deploy/env.example`](deploy/env.example), mode `640`, and fill it in.
- `CALDAV_CALENDAR_URL` may stay empty for the first start: ask the assistant
  to list your calendars, then set it and run `./deploy.sh --no-build`.

`deploy.sh` never creates the compose file and never copies secrets: both stay
on the server and belong to its admin, who can operate the service (`up -d`,
`logs`) without this repository. The cost is that `deploy/` holds *copies* that
nothing keeps in sync.

### 3. Deploy

```bash
cp deploy/deploy.env.example deploy.env    # set DEPLOY_HOST
./deploy.sh
```

It builds the image on the server, recreates this one service — with
`--no-deps`, so nothing else in the stack is touched — and waits for `/healthz`.

| `deploy.env` | Default |
|---|---|
| `DEPLOY_HOST` | none — **required**, e.g. `admin@your-server.example.net`. Passed to `ssh` verbatim, so it must be the name your `~/.ssh/config` matches |
| `DOCKER_CTX` | the host from `DEPLOY_HOST`, e.g. `your-server.example.net` — the Docker context, created on the first run and shared by every service on that host. `deploy.sh` stops if an existing context of that name points elsewhere |
| `REMOTE_COMPOSE` | `/opt/docker/docker-compose.yml` |
| `HEALTH_URL` | `http://127.0.0.1:9153/healthz` — resolved on the server |

`deploy.env` is gitignored; exported variables of the same name take precedence.

### 4. Put TLS in front

The endpoint has no encryption of its own. Point the reverse proxy at
`http://127.0.0.1:9153/caldav-mcp`, keep the path unchanged, and put the hostname
it answers on into `MCP_ALLOWED_HOSTS` (and `https://<hostname>` into
`MCP_ALLOWED_ORIGINS`). With Tailscale, for example, inside the tailnet only:

```bash
tailscale serve --bg --set-path /caldav-mcp http://127.0.0.1:9153/caldav-mcp
```

Check it from your machine:

```bash
curl -s https://mcp.example.net/caldav-mcp -H 'Authorization: Bearer <MCP_AUTH_TOKEN>'
```

`Bad Request: Missing session ID` is the right answer: proxy, path, token and
host check are all in order, and the server is asking for an MCP session.
`Invalid Host header` means the hostname is missing from `MCP_ALLOWED_HOSTS`,
`unauthorized` that the token is wrong.

### 5. Connect a client

The endpoint is `https://mcp.example.net/caldav-mcp`, with the header
`Authorization: Bearer <MCP_AUTH_TOKEN>`. [`.mcp.json.example`](.mcp.json.example)
shows it for Claude Code; any MCP client that speaks streamable HTTP works the
same way.

### Updates and rollback

```bash
./deploy.sh              # after a code change: build, recreate, health check
./deploy.sh --no-build   # after a change to the env file: recreate only
ssh admin@your-server.example.net docker logs --tail 50 caldav-mcp
```

A changed env file takes effect only when the container is *recreated*;
`docker compose restart` keeps the old environment.

Each build is tagged twice: `:local`, which the compose file references, and a
version tag from `git describe` — `v1.2.0` on a release tag, `v1.2.0-3-gafe0bf9`
three commits past it, `-dirty` for uncommitted changes — that stays behind for
a rollback. It is also the version the server reports to clients
(`serverInfo.version`), so a client can tell which build answers. To roll back:

```bash
docker --context your-server.example.net images caldav-mcp      # what is on the server

ssh admin@your-server.example.net \
  'docker tag caldav-mcp:v1.2.0 caldav-mcp:local && \
   docker compose -f /opt/docker/docker-compose.yml up -d --no-deps caldav-mcp'
```

The image exists only in the server's local store, so the service definition
must keep `pull_policy: never` — without it, a routine `docker compose pull`
fails on this service and aborts the pull for the whole stack.

### Troubleshooting

| What you see | Cause and fix |
|---|---|
| The container restarts in a loop | It exits at startup — `docker logs caldav-mcp` has the reason, usually a missing value in the env file |
| A tool answers `Cannot connect to the CalDAV server: …` | URL, username or password are wrong, or the CalDAV server is not reachable from the container. The rest of the message is the underlying error |
| It exits with `ZoneInfoNotFoundError` | `TZ` is not an IANA name such as `Europe/Berlin` |
| `Invalid event URI` or `Unknown calendar` | The value was not taken from `list_events` / `list_calendars`. The server accepts nothing else — see [Security model](#security-model) |
| `No default calendar` | Several calendars and no `CALDAV_CALENDAR_URL` — set it, or name the calendar in the request |
| Events are an hour or two off | `TZ` is not set |
| It exits with `MCP_AUTH_TOKEN is not set` | The server refuses to run without a token. Set one in the env file |
| `401 unauthorized` | Token missing or wrong. The header is exactly `Authorization: Bearer <token>` |
| `421 Invalid Host header` | The hostname the request arrives under is not in `MCP_ALLOWED_HOSTS` |
| `403 Invalid Origin header` | The client sends an `Origin` that is not in `MCP_ALLOWED_ORIGINS` |
| `404 Not Found` with a valid token | The URL path is not `MCP_PATH`, or the proxy rewrites it |
| A bare `Error executing tool <name>` | An unexpected failure; the traceback is in `docker logs caldav-mcp` |
| *healthy*, but every tool call fails | `/healthz` says the process is alive, nothing more — the server only connects to the CalDAV server on the first tool call |

## Security model

- The server holds the CalDAV credentials and acts with them. Whoever can call
  its tools can read, change and delete the events of that account.
- `MCP_AUTH_TOKEN` is a static shared secret — no rotation, no per-client
  identity, no rate limiting. It is a second layer behind a network boundary,
  not a replacement for one: run the endpoint inside an internal network or a
  VPN, not on the public internet.
- Event URIs and calendars are tool arguments and therefore come from the
  model. The server only accepts a calendar the account lists, and only an
  event URI that sits directly inside one of them — so the credentials are
  never sent to a host named in a tool call, and a task list on the same
  server cannot be reached through an event URI.
- Titles, locations and descriptions reach the model as text it reads, so an
  event — an invitation from someone else, say — can carry instructions aimed
  at the model. The mitigation is the MCP client's tool-approval prompts; the
  reading tools are marked read-only and `delete_event` destructive so a client
  can tell them apart.

Vulnerabilities: see [SECURITY.md](SECURITY.md).

## Development

```bash
python3 -m venv venv
venv/bin/pip install --require-hashes -r requirements.txt
venv/bin/pip install -r requirements-dev.txt
venv/bin/ruff check .
venv/bin/pytest -q
```

| Test | What it pins |
|---|---|
| `tests/test_tools.py` | The tool layer against a fake service: which calendar and URI are accepted, what a date range means, what is said when a calendar cannot be read |
| `tests/test_events.py` | Days and times, to and from iCalendar; what counts as a series, and finding one occurrence of it |
| `tests/test_caldav.py` | The real service against [Radicale](https://radicale.org), run in-process from a temporary directory: series and single occurrences, changes that keep reminders and attendees. Skipped if Radicale is not installed |
| `tests/test_auth.py` | The HTTP front door: bearer check, health endpoint, startup guard |

Without `MCP_TRANSPORT` the server speaks stdio, which is the quickest way to
try a change before deploying it — with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector), passing the
variables with `-e`:

```bash
npx @modelcontextprotocol/inspector --cli venv/bin/python server.py \
  --method tools/list \
  -e CALDAV_URL=… -e CALDAV_USERNAME=… -e CALDAV_PASSWORD=…
```

### Dependencies

`requirements.in` holds the direct dependencies; `requirements.txt` is generated
from it with hashes and is what the image installs (`--require-hashes`). To bump
a version, edit `requirements.in`, regenerate, and commit both together:

```bash
venv/bin/pip-compile --generate-hashes --strip-extras requirements.in
```

CI runs lint and tests, fails if `requirements.txt` no longer matches
`requirements.in`, and runs `pip-audit` against the pinned set. Dependabot
proposes the bumps monthly.

## License

MIT — see [LICENSE](LICENSE).

**Stefan Schmidbauer** — [GitHub](https://github.com/Stefan-Schmidbauer).
Built with [Claude Code](https://claude.com/claude-code) as co-author.
