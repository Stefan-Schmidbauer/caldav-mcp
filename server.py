"""CalDAV MCP Server — calendar events (VEVENT) as MCP tools."""

import logging
import os
import threading
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from hmac import compare_digest
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import caldav
import icalendar
import recurring_ical_events
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


def _env_flag(name: str) -> bool:
    """An on/off switch from the environment.

    Not `bool(os.getenv(...))`: a variable set to "0" or "false" reads as *off*
    the way anyone writing it into a compose file expects, and anything the list
    does not name falls to the safe side rather than counting as on because it is
    non-empty.
    """
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


CALDAV_URL = os.getenv("CALDAV_URL", "")
CALDAV_USERNAME = os.getenv("CALDAV_USERNAME", "")
CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD", "")
# The calendar new events go to when create_event names none. Optional: an
# account with a single calendar needs no default, and reading always covers
# every calendar.
CALDAV_CALENDAR_URL = os.getenv("CALDAV_CALENDAR_URL", "")
# IANA name (e.g. Europe/Berlin) of the zone the tools speak in: "10:00" in a
# tool call is 10:00 there, and results are shown in it. Read from the standard
# TZ variable, which sets the container's local time as well. A container runs
# in UTC unless told otherwise, which would put every event an hour or two off.
TIMEZONE = os.getenv("TZ", "").removeprefix(":")

# Transport: "stdio" (default, the client spawns the server) or
# "streamable-http" (one shared instance, which is how the container runs it).
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")
# Serving HTTP without MCP_AUTH_TOKEN is refused rather than warned about, because
# reaching the endpoint means reaching the CalDAV account. This is the deliberate
# opt-out for a deployment that authenticates one layer further out.
MCP_ALLOW_UNAUTHENTICATED = _env_flag("MCP_ALLOW_UNAUTHENTICATED")
# Hosts/origins accepted by the SDK's DNS-rebinding protection. Comma-separated;
# needed because we are reached through a reverse proxy under its own hostname.
MCP_ALLOWED_HOSTS = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
MCP_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]

if not (CALDAV_URL and CALDAV_USERNAME and CALDAV_PASSWORD):
    raise OSError(
        "CALDAV_URL, CALDAV_USERNAME and CALDAV_PASSWORD must all be set.\n"
        "Put them in the env file next to the compose file (deploy/env.example is\n"
        "the template) — see the Configuration section of the README."
    )

logger = logging.getLogger("caldav-mcp")

# Raises ZoneInfoNotFoundError on a misspelt name — at startup, not on the first
# create_event.
_TZ = ZoneInfo(TIMEZONE) if TIMEZONE else None

mcp = MCPServer(
    "caldav",
    version=os.environ.get("MCP_SERVER_VERSION", ""),
    instructions=(
        "Calendar events on a CalDAV server. A day is YYYY-MM-DD, a time is "
        "YYYY-MM-DDTHH:MM; a time without a UTC offset is local time "
        f"({TIMEZONE or 'the zone of the machine the server runs on'}). "
        "An event's uri, from list_events or search_events, is its handle for "
        "update_event and delete_event; the occurrences of a recurring event share "
        "one uri and are told apart by their occurrence value."
    ),
)

_PRODID = "-//caldav-mcp//EN"
# What an event lasts when create_event is given a start time and no end.
_DEFAULT_LENGTH = timedelta(hours=1)
# Every occurrence of every series in the window is expanded, so the window is
# bounded — a little over a year, enough for "when is the next …".
_MAX_RANGE = timedelta(days=400)
_MAX_EVENTS = 200

# Failures the model can act on are raised as ToolError: only its text reaches
# the model. Any other exception is a crash to the SDK — the model sees a bare
# "Error executing tool <name>" and the traceback goes to the server log.
_SERIES = (
    "This is a recurring event. For one occurrence alone, pass its occurrence "
    "value from list_events; for every occurrence, pass whole_series=True."
)


# ── Days and times ───────────────────────────────────────────────────────────
#
# The tools speak in days (date) and local times (aware datetime). An all-day
# event ends on its last day; iCalendar stores the day after, which reads as one
# day too long everywhere but in the file — _span and _set_span convert.

def _local(dt: datetime) -> datetime:
    """A time in the configured zone; one that names no zone is read as local."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_TZ) if _TZ else dt.astimezone()
    return dt.astimezone(_TZ)


def _today() -> date:
    return datetime.now(_TZ).date()


def _parse_when(value: str, what: str) -> date | datetime:
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return _local(datetime.fromisoformat(text))
    except ValueError as e:
        raise ToolError(
            f"Invalid {what} {value!r} — use YYYY-MM-DD for a day or YYYY-MM-DDTHH:MM for a time."
        ) from e


def _day_start(day: date) -> datetime:
    return _local(datetime.combine(day, time()))


def _range(
    from_date: str, to_date: str, default_from: date, default_days: int
) -> tuple[datetime, datetime]:
    """The window a listing covers, as two instants."""
    start = _parse_when(from_date, "from_date") if from_date else default_from
    if not isinstance(start, datetime):
        start = _day_start(start)
    if to_date:
        end = _parse_when(to_date, "to_date")
        if not isinstance(end, datetime):
            # A day named as the end is included: the window closes at the
            # midnight after it.
            end = _day_start(end + timedelta(days=1))
    else:
        end = start + timedelta(days=default_days)
    if end <= start:
        raise ToolError("to_date is before from_date.")
    if end - start > _MAX_RANGE:
        raise ToolError(f"The range is longer than {_MAX_RANGE.days} days — ask for a shorter one.")
    return start, end


def _span(component) -> tuple[date | datetime, date | datetime]:
    """An event's start and end the way the tools speak of them."""
    start, end = component.start, component.end
    if isinstance(start, datetime):
        return _local(start), _local(end)
    return start, max(start, end - timedelta(days=1))


def _resolve_span(start, end, old=None):
    """The span an event gets, from what the caller passed and what it had.

    `start` and `end` are None where the caller left them out; `old` is the
    existing span when an event is being changed.
    """
    moved = start is not None
    if not moved:
        start = old[0]
    timed = isinstance(start, datetime)
    if end is None:
        if old and not moved:
            end = old[1]
        elif old and isinstance(old[0], datetime) == timed:
            # Moving an event keeps its length.
            end = start + (old[1] - old[0])
        else:
            end = start + _DEFAULT_LENGTH if timed else start
    if isinstance(end, datetime) != timed:
        raise ToolError(
            "start and end must be of one kind — both a day (YYYY-MM-DD) or both a "
            "time (YYYY-MM-DDTHH:MM)."
        )
    if end < start:
        raise ToolError("The event would end before it starts.")
    return start, end


def _set_span(component, start, end) -> None:
    for name in ("DTSTART", "DTEND", "DURATION"):
        component.pop(name, None)
    if isinstance(start, datetime):
        # With a zone name the time is written with its TZID, which is what a
        # calendar app would do; without one, UTC is the only unambiguous form.
        zone = _TZ or UTC
        component.add("DTSTART", start.astimezone(zone))
        component.add("DTEND", end.astimezone(zone))
    else:
        component.add("DTSTART", start)
        component.add("DTEND", end + timedelta(days=1))


def _set_text(component, name: str, value: str) -> None:
    component.pop(name, None)
    if value:
        component.add(name, value)


def _touch(component) -> None:
    now = datetime.now(UTC)
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        component.pop(name, None)
        component.add(name, now)


def _iso(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return _local(value).isoformat(timespec="minutes")
    return value.isoformat()


def _event_dict(component, uri: str, calendar: str) -> dict:
    start, end = _span(component)
    recurrence_id = component.get("RECURRENCE-ID")
    return {
        "uri": _without_default_port(uri),
        "calendar": calendar,
        "title": str(component.get("SUMMARY", "Untitled")),
        "start": _iso(start),
        "end": _iso(end),
        "all_day": not isinstance(start, datetime),
        "location": str(component.get("LOCATION", "")),
        "description": str(component.get("DESCRIPTION", "")),
        # An occurrence of an expanded series carries RECURRENCE-ID in place of
        # the rule it came from.
        "recurring": any(name in component for name in ("RRULE", "RDATE", "RECURRENCE-ID")),
        # Where the rule puts this occurrence. That is what names it, and it
        # stays the same when the occurrence itself is moved.
        "occurrence": _iso(recurrence_id.dt) if recurrence_id is not None else "",
        "cancelled": str(component.get("STATUS", "")).upper() == "CANCELLED",
    }


def _master(ical) -> tuple[icalendar.Event, bool]:
    """The event a stored object stands for, and whether it is a series.

    A series is one object: the event with the rule, plus one further VEVENT
    for every occurrence that was changed on its own.
    """
    events = ical.walk("VEVENT")
    if not events:
        raise ToolError("That is not an event — pass a uri exactly as list_events returns it.")
    master = next((e for e in events if "RECURRENCE-ID" not in e), events[0])
    return master, len(events) > 1 or "RRULE" in master or "RDATE" in master


def _same_start(a, b) -> bool:
    """Whether two starts are the same day, or the same instant."""
    if isinstance(a, datetime) != isinstance(b, datetime):
        return False
    return _local(a) == _local(b) if isinstance(a, datetime) else a == b


def _occurrence(ical, occurrence: date | datetime) -> tuple[icalendar.Event, bool]:
    """One occurrence of a series, and whether it has a VEVENT of its own yet.

    An occurrence that was changed before is stored as its own VEVENT; any
    other exists only through the rule and is made here, as a copy of the
    series' event at that date, ready to be added to the object.
    """
    for event in ical.walk("VEVENT"):
        recurrence_id = event.get("RECURRENCE-ID")
        if recurrence_id is not None and _same_start(recurrence_id.dt, occurrence):
            return event, True
    # Not changed before, so it starts where the rule puts it.
    window = timedelta(minutes=1) if isinstance(occurrence, datetime) else timedelta(days=1)
    for event in recurring_ical_events.of(ical).between(occurrence, window):
        recurrence_id = event.get("RECURRENCE-ID")
        if recurrence_id is not None and _same_start(recurrence_id.dt, occurrence):
            return event, False
    raise ToolError(
        f"The recurring event has no occurrence at {_iso(occurrence)} — pass occurrence "
        "exactly as list_events returns it."
    )


def _as_written(start: date | datetime, moment: date | datetime) -> date | datetime:
    """A moment in the form the series writes its own start in.

    An EXDATE names an occurrence the way DTSTART would have: same kind of
    value, same zone. Calendar apps differ in how forgiving they are of less.
    """
    if not isinstance(start, datetime):
        return moment
    if start.tzinfo is None:
        return _local(moment).replace(tzinfo=None)
    return moment.astimezone(start.tzinfo)


# ── CalDAV ───────────────────────────────────────────────────────────────────

class CalDAVService:
    def __init__(self):
        self._client: caldav.DAVClient | None = None
        self._principal: caldav.Principal | None = None
        # url → calendar and url → display name, for the calendars that hold
        # events. Kept, because every tool call needs them and asking costs a
        # round trip per calendar.
        self._calendars: dict[str, caldav.Calendar] | None = None
        self._names: dict[str, str] = {}

    def connect(self, url: str, username: str, password: str) -> bool:
        try:
            # A timeout, because this is a long-running server: without one a
            # CalDAV server that stops answering holds a worker thread per call.
            self._client = caldav.DAVClient(url=url, username=username, password=password, timeout=30)
            self._principal = self._client.principal()
            logger.info("Connected to CalDAV server: %s", url)
            return True
        except Exception as e:
            logger.error("Connection failed: %s", e)
            raise ConnectionError(str(e)) from e

    def calendars(self, refresh: bool = False) -> list[dict]:
        if not self._principal:
            return []
        if refresh or self._calendars is None:
            calendars, names = {}, {}
            for cal in self._principal.get_calendars():
                if self._holds_events(cal):
                    url = str(cal.url)
                    calendars[url] = cal
                    names[url] = cal.get_display_name() or url
            self._calendars, self._names = calendars, names
        return [{"url": url, "name": self._names[url]} for url in self._calendars]

    @staticmethod
    def _holds_events(cal: caldav.Calendar) -> bool:
        """False for a pure task list, which a CalDAV account lists as a calendar too."""
        try:
            components = cal.get_supported_components()
        except Exception as e:
            logger.warning("Cannot tell what %s holds, assuming events: %s", cal.url, e)
            return True
        return not components or "VEVENT" in components

    def search(self, calendar_url: str, start: datetime, end: datetime) -> tuple[list[dict], int]:
        """Events between two instants, and how many more could not be read.

        A series is expanded: each occurrence in the window is an event of its
        own, all of them sharing the uri of the series.
        """
        name = self._names[calendar_url]
        events, unreadable = [], 0
        found = self._calendars[calendar_url].search(event=True, start=start, end=end, expand=True)
        for obj in found:
            try:
                events.append(_event_dict(obj.icalendar_component, str(obj.url), name))
            except Exception as e:
                logger.warning("Failed to parse event %s: %s", obj.url, e)
                unreadable += 1
        return events, unreadable

    def create(self, calendar_url: str, title: str, start, end,
               location: str = "", description: str = "") -> dict:
        event = icalendar.Event.new(summary=title)
        _set_span(event, start, end)
        _set_text(event, "LOCATION", location)
        _set_text(event, "DESCRIPTION", description)
        ical = icalendar.Calendar.new(subcomponents=[event], prodid=_PRODID)
        ical.add_missing_timezones()
        saved = self._calendars[calendar_url].add_event(ical.to_ical().decode())
        logger.info("Event created: %s", saved.url)
        return _event_dict(event, str(saved.url), self._names[calendar_url])

    def update(self, calendar_url: str, uri: str, *, title: str = "", start=None, end=None,
               location: str | None = None, description: str | None = None,
               occurrence=None, whole_series: bool = False) -> dict:
        """Change an event in place.

        The stored object is edited rather than rebuilt, so whatever the tools
        know nothing about — reminders, attendees, the rule of a series —
        survives the change. For location and description, None keeps the
        text, "" removes it.

        Of a series, either the one `occurrence` is changed — it gets a VEVENT
        of its own next to the rule, which is how iCalendar stores an
        exception — or, with `whole_series`, the event that carries the rule.
        """
        obj = self._load(calendar_url, uri)
        with obj.edit_icalendar_instance() as ical:
            event, series = _master(ical)
            retimed = start is not None or end is not None
            if series and occurrence is not None:
                event, stored = _occurrence(ical, occurrence)
                if not stored:
                    ical.add_component(event)
            elif series and not whole_series:
                raise ToolError(_SERIES)
            elif series and retimed:
                # The occurrences changed or left out on their own are keyed by
                # their original start; moving the rule under them breaks that.
                raise ToolError(
                    "The time of a whole series cannot be changed here — move single "
                    "occurrences (pass occurrence), or change the series in a calendar app."
                )
            if title:
                _set_text(event, "SUMMARY", title)
            if retimed:
                _set_span(event, *_resolve_span(start, end, _span(event)))
            if location is not None:
                _set_text(event, "LOCATION", location)
            if description is not None:
                _set_text(event, "DESCRIPTION", description)
            _touch(event)
            ical.add_missing_timezones()
            result = _event_dict(event, uri, self._names[calendar_url])
        self._save(obj)
        logger.info("Event updated: %s", uri)
        return result

    def delete(self, calendar_url: str, uri: str, occurrence=None, whole_series: bool = False) -> str:
        """Delete an event, or one occurrence of a series; returns its title."""
        obj = self._load(calendar_url, uri)
        with obj.edit_icalendar_instance() as ical:
            event, series = _master(ical)
            one_of_many = series and occurrence is not None
            if one_of_many:
                master = event
                event, stored = _occurrence(ical, occurrence)
                if stored:
                    ical.subcomponents.remove(event)
                # Leaving an occurrence out is a note on the rule. Without it
                # the rule would bring the occurrence back, unchanged.
                if "RRULE" in master or "RDATE" in master:
                    master.add("EXDATE", _as_written(master.start, occurrence))
                    _touch(master)
            elif series and not whole_series:
                raise ToolError(_SERIES)
        if one_of_many:
            self._save(obj)
        else:
            obj.delete()
        logger.info("Event deleted: %s%s", uri, f" at {_iso(occurrence)}" if one_of_many else "")
        return str(event.get("SUMMARY", "Untitled"))

    def _load(self, calendar_url: str, uri: str) -> caldav.Event:
        obj = caldav.Event(client=self._client, url=uri, parent=self._calendars[calendar_url])
        obj.load()
        return obj

    @staticmethod
    def _save(obj: caldav.Event) -> None:
        # Stored exactly as edited. By default the client looks at the first
        # VEVENT, and if that is a changed occurrence — servers do not promise
        # to put the rule first — it fetches the series again and merges ours
        # into it as if it were that one occurrence.
        obj.save(only_this_recurrence=False)


_service = CalDAVService()
_connected = False
# The SDK runs sync tools in a threadpool under the HTTP transport, so two first
# calls can arrive together; without the lock both would connect.
_connect_lock = threading.Lock()


def _ensure_connected() -> None:
    global _connected
    with _connect_lock:
        if _connected:
            return
        try:
            _service.connect(CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD)
        except ConnectionError as e:
            raise ToolError(f"Cannot connect to the CalDAV server: {e}") from e
        _connected = True


@contextmanager
def _caldav_errors():
    """Turn what the CalDAV server says no to into something the model can act on."""
    try:
        yield
    except caldav.error.NotFoundError as e:
        raise ToolError(
            "Event not found — it may have been deleted or moved. Call list_events for the "
            "current URIs."
        ) from e
    except caldav.error.DAVError as e:
        raise ToolError(f"The CalDAV server refused the request: {e}") from e


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _parts(url: str) -> tuple[tuple[str, str | None, int | None], str]:
    """A URL's server and path, each spelled one way."""
    split = urlsplit(url)
    origin = (split.scheme, split.hostname, split.port or _DEFAULT_PORTS.get(split.scheme))
    return origin, unquote(split.path).rstrip("/")


def _without_default_port(url: str) -> str:
    """A URL without the port its scheme implies.

    The client spells the URL of a new event with `:443`, a listing spells it
    without — the model would take them for two events.
    """
    split = urlsplit(url)
    if split.port is None or split.port != _DEFAULT_PORTS.get(split.scheme):
        return url
    return split._replace(netloc=split.netloc.rsplit(":", 1)[0]).geturl()


def _calendar_names(calendars: list[dict]) -> str:
    return ", ".join(repr(c["name"]) for c in calendars) or "none"


def _pick_calendar(calendar: str) -> dict:
    """The calendar a tool argument names, by url or by name.

    Only ever one the account lists, never "any URL": the client would send the
    account's credentials to whatever it is pointed at.
    """
    wanted = calendar.strip()
    calendars: list[dict] = []
    # The list is kept; a miss is looked up once more in case the calendar is new.
    for refresh in (False, True):
        calendars = _service.calendars(refresh=refresh)
        matches = [c for c in calendars if c["url"] == wanted] or [
            c for c in calendars if c["name"].casefold() == wanted.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ToolError(
                f"Several calendars are named {wanted!r} — pass the url from list_calendars."
            )
    raise ToolError(f"Unknown calendar {calendar!r} — the calendars are: {_calendar_names(calendars)}.")


def _default_calendar(calendars: list[dict]) -> dict | None:
    if CALDAV_CALENDAR_URL:
        wanted = _parts(CALDAV_CALENDAR_URL)
        return next((c for c in calendars if _parts(c["url"]) == wanted), None)
    return calendars[0] if len(calendars) == 1 else None


def _calendar_of(uri: str) -> str:
    """The calendar an event URI lies in — refusing a URI that lies in none.

    Tool arguments come from the model, and the CalDAV client attaches the
    account's credentials to whatever URL it is handed. A description is free
    text the model reads, so without this an event could talk it into sending
    those credentials anywhere. URIs from list_events are absolute and sit
    directly inside one of the account's calendars; anything else is not one of
    ours.
    """
    try:
        origin, path = _parts(uri)
    except ValueError:  # urlsplit: malformed port
        origin, path = None, ""
    for refresh in (False, True):
        for cal in _service.calendars(refresh=refresh):
            cal_origin, cal_path = _parts(cal["url"])
            name = path[len(cal_path) + 1:]
            if (origin == cal_origin and path.startswith(cal_path + "/")
                    and name not in ("", ".", "..") and "/" not in name):
                return cal["url"]
    raise ToolError(f"Invalid event URI {uri!r} — pass a uri exactly as list_events returns it.")


def _find(start: datetime, end: datetime, calendar: str, query: str = "") -> dict:
    calendars = [_pick_calendar(calendar)] if calendar else _service.calendars()
    events, warnings, failed = [], [], 0
    for cal in calendars:
        try:
            found, unreadable = _service.search(cal["url"], start, end)
        except Exception as e:
            # One calendar that cannot be read must not hide the others — but it
            # must not go unsaid either, or "nothing on that day" is a guess.
            logger.warning("Failed to read calendar %s: %s", cal["url"], e)
            warnings.append(f"Calendar {cal['name']!r} could not be read: {e}")
            failed += 1
            continue
        events += found
        if unreadable:
            warnings.append(
                f"{unreadable} event(s) in {cal['name']!r} could not be read and are missing here."
            )
    if not calendars:
        warnings.append("The account has no calendar that holds events.")
    elif failed == len(calendars):
        raise ToolError(" ".join(warnings))
    if query:
        events = [
            e for e in events
            if query in f"{e['title']}\n{e['location']}\n{e['description']}".casefold()
        ]
    # All in one zone, so the text sorts by time — and a day sorts before the
    # times on it, which puts all-day events first.
    events.sort(key=lambda e: e["start"])
    if len(events) > _MAX_EVENTS:
        warnings.append(
            f"Showing the first {_MAX_EVENTS} of {len(events)} events — ask for a shorter "
            "range to see the rest."
        )
        events = events[:_MAX_EVENTS]
    return {"events": events, "warnings": warnings}


def _which_occurrence(occurrence: str, whole_series: bool) -> date | datetime | None:
    if not occurrence:
        return None
    if whole_series:
        raise ToolError("Pass either occurrence or whole_series, not both.")
    return _parse_when(occurrence, "occurrence")


# ── Tools ────────────────────────────────────────────────────────────────────

_READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool(annotations=_READ_ONLY)
def list_calendars() -> list[dict]:
    """List the calendars of the account that hold events.

    `default` marks the calendar create_event uses when it is given none.
    """
    _ensure_connected()
    calendars = _service.calendars(refresh=True)
    default = _default_calendar(calendars)
    return [{**c, "default": c is default} for c in calendars]


@mcp.tool(annotations=_READ_ONLY)
def list_events(from_date: str = "", to_date: str = "", calendar: str = "") -> dict:
    """List the events in a date range, from all calendars unless one is named.

    Returns `events`, earliest first, and `warnings` — not empty when a calendar
    could not be read, so the list may be incomplete. For an all-day event,
    `end` is its last day. A recurring event appears once per occurrence, all
    with the same uri; `occurrence` tells them apart.

    Args:
        from_date: First day (YYYY-MM-DD) or exact time (YYYY-MM-DDTHH:MM), defaults to today
        to_date: Last day, included, or exact time; defaults to 7 days from from_date
        calendar: Name or url of one calendar (from list_calendars), empty = all calendars
    """
    _ensure_connected()
    start, end = _range(from_date, to_date, _today(), 7)
    return _find(start, end, calendar)


@mcp.tool(annotations=_READ_ONLY)
def search_events(query: str, from_date: str = "", to_date: str = "", calendar: str = "") -> dict:
    """Find events whose title, location or description contains a text.

    Case does not matter. Without dates it looks from 30 days back to a year
    ahead. Returns the same as list_events.

    Args:
        query: The text to look for
        from_date: First day (YYYY-MM-DD) or exact time, defaults to 30 days ago
        to_date: Last day, included, or exact time
        calendar: Name or url of one calendar (from list_calendars), empty = all calendars
    """
    _ensure_connected()
    needle = query.strip().casefold()
    if not needle:
        raise ToolError("query is empty — use list_events to see everything in a range.")
    start, end = _range(from_date, to_date, _today() - timedelta(days=30), 395)
    return _find(start, end, calendar, needle)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False))
def create_event(
    title: str,
    start: str,
    end: str = "",
    location: str = "",
    description: str = "",
    calendar: str = "",
) -> dict:
    """Create an event. Returns it, with the uri it got.

    A day (YYYY-MM-DD) as start makes an all-day event, a time
    (YYYY-MM-DDTHH:MM) a timed one. A time without a UTC offset is local time.

    Args:
        title: What the event is called
        start: First day (YYYY-MM-DD) or start time (YYYY-MM-DDTHH:MM)
        end: Last day, included, or end time; defaults to the same day, or to one hour after start
        location: Where it takes place, empty = no location
        description: Free-text note, empty = none
        calendar: Name or url of the calendar (from list_calendars), empty = the default calendar
    """
    _ensure_connected()
    if not title.strip():
        raise ToolError("title is empty.")
    span = _resolve_span(_parse_when(start, "start"), _parse_when(end, "end") if end else None)
    if calendar:
        target = _pick_calendar(calendar)
    else:
        calendars = _service.calendars()
        target = _default_calendar(calendars)
        if target is None:
            raise ToolError(
                "No default calendar — pass calendar, one of: "
                f"{_calendar_names(calendars)}. (CALDAV_CALENDAR_URL sets a default.)"
            )
    with _caldav_errors():
        return _service.create(target["url"], title.strip(), *span, location, description)


@mcp.tool()
def update_event(
    uri: str,
    title: str = "",
    start: str = "",
    end: str = "",
    location: str = "",
    description: str = "",
    clear_location: bool = False,
    clear_description: bool = False,
    occurrence: str = "",
    whole_series: bool = False,
) -> dict:
    """Change an event. The uri comes from list_events or search_events. Returns the event as it is now.

    Only pass what should change; the rest — reminders and attendees included —
    is kept. A new start without an end moves the event and keeps its length.

    The occurrences of a recurring event share one uri. To change one of them,
    pass its occurrence value; to change all of them, set whole_series=True —
    that covers title, location and description, not the time. Without either,
    a recurring event is not touched.

    Args:
        uri: Event URI (from list_events)
        title: New title, empty = keep
        start: New first day (YYYY-MM-DD) or start time (YYYY-MM-DDTHH:MM), empty = keep
        end: New last day, included, or end time, empty = keep
        location: New location, empty = keep. To remove it, set clear_location=True.
        description: New description, empty = keep. To remove it, set clear_description=True.
        clear_location: True = remove the location. Takes precedence over location.
        clear_description: True = remove the description. Takes precedence over description.
        occurrence: Of a recurring event, the one occurrence to change (occurrence from list_events)
        whole_series: True = change every occurrence of a recurring event
    """
    _ensure_connected()
    changes = (title.strip(), start, end, location, description, clear_location, clear_description)
    if not any(changes):
        raise ToolError("Nothing to change — pass at least one field.")
    which = _which_occurrence(occurrence, whole_series)
    calendar_url = _calendar_of(uri)
    with _caldav_errors():
        return _service.update(
            calendar_url,
            uri,
            title=title.strip(),
            start=_parse_when(start, "start") if start else None,
            end=_parse_when(end, "end") if end else None,
            location="" if clear_location else location or None,
            description="" if clear_description else description or None,
            occurrence=which,
            whole_series=whole_series,
        )


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
def delete_event(uri: str, occurrence: str = "", whole_series: bool = False) -> str:
    """Delete an event for good. The uri comes from list_events or search_events.

    The occurrences of a recurring event share one uri. To cancel one of them,
    pass its occurrence value; the others stay. whole_series=True deletes the
    event with every occurrence, past and future. Without either, a recurring
    event is not touched.

    Args:
        uri: Event URI (from list_events)
        occurrence: Of a recurring event, the one occurrence to delete (occurrence from list_events)
        whole_series: True = delete a recurring event with all its occurrences
    """
    _ensure_connected()
    which = _which_occurrence(occurrence, whole_series)
    calendar_url = _calendar_of(uri)
    with _caldav_errors():
        title = _service.delete(calendar_url, uri, which, whole_series)
    if which is not None:
        return f"Event deleted: {title} ({_iso(which)})"
    return f"Event deleted: {title}"


# ── HTTP transport ───────────────────────────────────────────────────────────

class HealthzMiddleware:
    """Answer /healthz, always and without authentication.

    Its own layer *outside* the bearer check: a health probe carries no token,
    and deploy.sh and the image's HEALTHCHECK both poll it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await _plain_response(send, 200, b"ok")
            return
        await self.app(scope, receive, send)


class BearerAuthMiddleware:
    """Pure-ASGI bearer check.

    Deliberately not a BaseHTTPMiddleware subclass: that one buffers the
    response and would break the streaming/SSE responses of the MCP transport.
    """

    def __init__(self, app, token: str):
        self.app = app
        # Compared as bytes, exactly as the header arrives: decoding first means
        # a non-UTF-8 header raises UnicodeDecodeError, and compare_digest on str
        # rejects non-ASCII with a TypeError — either turns a request anyone can
        # send into a 500 with a traceback instead of a clean 401.
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        if not compare_digest(provided, self.expected):
            logger.warning("Rejected unauthenticated request to %s", scope.get("path"))
            await _plain_response(send, 401, b"unauthorized")
            return
        await self.app(scope, receive, send)


async def _plain_response(send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _bearer_check_or_refuse(token: str, allow_unauthenticated: bool) -> bool:
    """Decide whether an HTTP run may start, and whether it is guarded.

    A refusal rather than a warning: in HTTP mode reaching the endpoint means
    reaching the CalDAV account, so an unauthenticated endpoint is a decision,
    not a default one can slip into by leaving a variable unset.

    Returns True when the bearer check will be installed, False when the
    operator opted out explicitly. Raises OSError when neither is the case.
    """
    if token:
        return True
    if not allow_unauthenticated:
        raise OSError(
            "MCP_AUTH_TOKEN is not set, so the HTTP endpoint would be unauthenticated —\n"
            "and reaching it means reading, changing and deleting the events of the\n"
            "configured CalDAV account; this is the only check in front of it.\n"
            "Set MCP_AUTH_TOKEN to a random secret (openssl rand -hex 32), or set\n"
            "MCP_ALLOW_UNAUTHENTICATED=1 if something in front of the server does the\n"
            "authentication and you accept that. Neither replaces a network boundary."
        )
    logger.warning(
        "MCP_ALLOW_UNAUTHENTICATED is set — the endpoint is served without a bearer "
        "check. Anyone who can reach it has full access to the calendars."
    )
    return False


def _run_http() -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    # Before anything binds a port: an unauthenticated endpoint must not come up
    # far enough to answer a single request.
    guarded = _bearer_check_or_refuse(MCP_AUTH_TOKEN, MCP_ALLOW_UNAUTHENTICATED)
    # Leaving transport_security None keeps the SDK's localhost-only default,
    # which is what makes a proxied request fail closed with 421 until its
    # hostname is listed — see MCP_ALLOWED_HOSTS in the README.
    transport_security = None
    if MCP_ALLOWED_HOSTS or MCP_ALLOWED_ORIGINS:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=MCP_ALLOWED_HOSTS,
            allowed_origins=MCP_ALLOWED_ORIGINS,
        )
        logger.info("DNS-rebinding protection allows hosts: %s", MCP_ALLOWED_HOSTS)
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=transport_security,
    )
    if guarded:
        app = BearerAuthMiddleware(app, MCP_AUTH_TOKEN)
    # Outermost, so the probe answers with or without a bearer token.
    app = HealthzMiddleware(app)
    logger.info("Serving MCP over HTTP on %s:%s%s", MCP_HOST, MCP_PORT, MCP_PATH)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    if MCP_TRANSPORT == "streamable-http":
        logging.basicConfig(level=logging.INFO)
        _run_http()
    else:
        mcp.run()
