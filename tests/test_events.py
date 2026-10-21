"""Days, times and the iCalendar they are stored as.

The tools speak in days and local times; the file speaks in DTSTART, DTEND or
DURATION, in whatever zone the event was written in, and ends an all-day event
on the day after its last. These pin the translation in both directions.
"""
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server as server_module

BERLIN = ZoneInfo("Europe/Berlin")


def at(*args) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


def vevents(*bodies: str) -> icalendar.Calendar:
    events = "".join(
        f"BEGIN:VEVENT\r\nUID:u1\r\nDTSTAMP:20261001T000000Z\r\n{body.strip()}\r\nEND:VEVENT\r\n"
        for body in bodies
    )
    return icalendar.Calendar.from_ical(
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n{events}END:VCALENDAR\r\n")


def vevent(body: str) -> icalendar.Event:
    return vevents(body).walk("VEVENT")[0]


# ── reading what the model writes ────────────────────────────────────────────

WHEN = {
    "a day": ("2026-10-12", date(2026, 10, 12)),
    "a time is local": ("2026-10-12T10:00", at(2026, 10, 12, 10)),
    "a space works too": ("2026-10-12 10:00", at(2026, 10, 12, 10)),
    "winter time": ("2026-11-12T10:00", at(2026, 11, 12, 10)),
    "utc": ("2026-10-12T08:00Z", at(2026, 10, 12, 10)),
    "another offset": ("2026-10-12T17:00+09:00", at(2026, 10, 12, 10)),
}


@pytest.mark.parametrize("text,expected", WHEN.values(), ids=list(WHEN))
def test_parse_when(text, expected):
    parsed = server_module._parse_when(text, "start")
    assert parsed == expected and type(parsed) is type(expected)
    if isinstance(parsed, datetime):
        assert parsed.utcoffset() == expected.utcoffset()


def test_winter_and_summer_get_their_own_offset():
    summer = server_module._parse_when("2026-10-12T10:00", "start")
    winter = server_module._parse_when("2026-11-12T10:00", "start")
    assert summer.isoformat() == "2026-10-12T10:00:00+02:00"
    assert winter.isoformat() == "2026-11-12T10:00:00+01:00"


@pytest.mark.parametrize("text", ["", "morgen", "12.10.2026", "2026-13-01", "2026-10-12T25:00"])
def test_what_is_not_a_date_names_the_format(text):
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        server_module._parse_when(text, "start")


def test_today_follows_the_configured_timezone(monkeypatch):
    """Just after midnight in Berlin it is still yesterday in UTC — which is
    what a container reports unless TZ says otherwise."""
    instant = datetime(2026, 10, 9, 22, 30, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(server_module, "datetime", FrozenDatetime)
    assert server_module._today() == date(2026, 10, 10)
    monkeypatch.setattr(server_module, "_TZ", UTC)
    assert server_module._today() == date(2026, 10, 9)


# ── reading what the calendar holds ──────────────────────────────────────────

SPANS = {
    "utc becomes local": (
        "DTSTART:20261012T080000Z\nDTEND:20261012T090000Z",
        ("2026-10-12T10:00+02:00", "2026-10-12T11:00+02:00", False)),
    "another zone becomes local": (
        "DTSTART;TZID=America/New_York:20261012T090000\nDTEND;TZID=America/New_York:20261012T100000",
        ("2026-10-12T15:00+02:00", "2026-10-12T16:00+02:00", False)),
    "no zone is read as local": (
        "DTSTART:20261012T100000\nDTEND:20261012T110000",
        ("2026-10-12T10:00+02:00", "2026-10-12T11:00+02:00", False)),
    "a duration instead of an end": (
        "DTSTART;TZID=Europe/Berlin:20261012T100000\nDURATION:PT90M",
        ("2026-10-12T10:00+02:00", "2026-10-12T11:30+02:00", False)),
    "one day": (
        "DTSTART;VALUE=DATE:20261014\nDTEND;VALUE=DATE:20261015",
        ("2026-10-14", "2026-10-14", True)),
    "several days end on the last one": (
        "DTSTART;VALUE=DATE:20261014\nDTEND;VALUE=DATE:20261017",
        ("2026-10-14", "2026-10-16", True)),
    "a day without an end": (
        "DTSTART;VALUE=DATE:20261014",
        ("2026-10-14", "2026-10-14", True)),
    "a day that ends on itself": (
        "DTSTART;VALUE=DATE:20261014\nDTEND;VALUE=DATE:20261014",
        ("2026-10-14", "2026-10-14", True)),
}


@pytest.mark.parametrize("body,expected", SPANS.values(), ids=list(SPANS))
def test_start_and_end_as_the_tools_show_them(body, expected):
    shown = server_module._event_dict(vevent(body), "uri", "Privat")
    assert (shown["start"], shown["end"], shown["all_day"]) == expected


def test_an_event_as_the_tools_show_it():
    shown = server_module._event_dict(vevent(
        "DTSTART:20261012T080000Z\nDTEND:20261012T090000Z\nSUMMARY:Zahnarzt\\; Dr. Müller\n"
        "LOCATION:Hauptstr. 1\\, München\nDESCRIPTION:Karte\\nmitnehmen"
    ), "https://dav.test/caldav/privat/u1.ics", "Privat")
    assert shown == {
        "uri": "https://dav.test/caldav/privat/u1.ics",
        "calendar": "Privat",
        "title": "Zahnarzt; Dr. Müller",
        "start": "2026-10-12T10:00+02:00",
        "end": "2026-10-12T11:00+02:00",
        "all_day": False,
        "location": "Hauptstr. 1, München",
        "description": "Karte\nmitnehmen",
        "recurring": False,
        "occurrence": "",
        "cancelled": False,
    }


URIS = {
    "the default port is dropped": (
        "https://dav.test:443/caldav/privat/u1.ics", "https://dav.test/caldav/privat/u1.ics"),
    "of http as well": ("http://dav.test:80/caldav/u1.ics", "http://dav.test/caldav/u1.ics"),
    "another port is kept": (
        "https://dav.test:8443/caldav/u1.ics", "https://dav.test:8443/caldav/u1.ics"),
    "no port stays none": ("https://dav.test/caldav/u1.ics", "https://dav.test/caldav/u1.ics"),
}


@pytest.mark.parametrize("uri,expected", URIS.values(), ids=list(URIS))
def test_an_event_has_one_uri_however_the_client_spells_it(uri, expected):
    shown = server_module._event_dict(vevent("DTSTART;VALUE=DATE:20261014"), uri, "Privat")
    assert shown["uri"] == expected


def test_an_event_with_nothing_but_a_start_is_still_shown():
    shown = server_module._event_dict(vevent("DTSTART;VALUE=DATE:20261014"), "uri", "Privat")
    assert (shown["title"], shown["location"], shown["description"]) == ("Untitled", "", "")


RECURRING = {
    "a rule": "RRULE:FREQ=WEEKLY",
    "listed dates": "RDATE:20261019T080000Z",
    "an occurrence of an expanded series": "RECURRENCE-ID:20261012T080000Z",
}


@pytest.mark.parametrize("line", RECURRING.values(), ids=list(RECURRING))
def test_recurring_is_flagged(line):
    body = f"DTSTART:20261012T080000Z\nDTEND:20261012T090000Z\n{line}"
    assert server_module._event_dict(vevent(body), "uri", "Privat")["recurring"] is True


def test_cancelled_is_flagged():
    body = "DTSTART:20261012T080000Z\nDTEND:20261012T090000Z\nSTATUS:CANCELLED"
    assert server_module._event_dict(vevent(body), "uri", "Privat")["cancelled"] is True


# ── the span an event gets ───────────────────────────────────────────────────

def test_a_new_timed_event_lasts_an_hour():
    assert server_module._resolve_span(at(2026, 10, 12, 10), None) == (
        at(2026, 10, 12, 10), at(2026, 10, 12, 11))


def test_a_new_all_day_event_lasts_that_day():
    day = date(2026, 10, 14)
    assert server_module._resolve_span(day, None) == (day, day)


OLD_TIMED = (at(2026, 10, 12, 10), at(2026, 10, 12, 11, 30))
OLD_DAYS = (date(2026, 10, 14), date(2026, 10, 16))

MOVES = {
    "a new start keeps the length": (
        at(2026, 10, 13, 15), None, OLD_TIMED, (at(2026, 10, 13, 15), at(2026, 10, 13, 16, 30))),
    "a new first day keeps the number of days": (
        date(2026, 10, 20), None, OLD_DAYS, (date(2026, 10, 20), date(2026, 10, 22))),
    "a new end leaves the start": (
        None, at(2026, 10, 12, 12), OLD_TIMED, (at(2026, 10, 12, 10), at(2026, 10, 12, 12))),
    "a new last day leaves the first": (
        None, date(2026, 10, 18), OLD_DAYS, (date(2026, 10, 14), date(2026, 10, 18))),
    "both": (
        at(2026, 10, 13, 9), at(2026, 10, 13, 17), OLD_TIMED,
        (at(2026, 10, 13, 9), at(2026, 10, 13, 17))),
    # The old length means nothing in the other kind, so the default applies.
    "days become a time": (
        at(2026, 10, 14, 9), None, OLD_DAYS, (at(2026, 10, 14, 9), at(2026, 10, 14, 10))),
    "a time becomes a day": (
        date(2026, 10, 12), None, OLD_TIMED, (date(2026, 10, 12), date(2026, 10, 12))),
    "across the end of summer time the clock time stays": (
        at(2026, 10, 26, 10), None, OLD_TIMED, (at(2026, 10, 26, 10), at(2026, 10, 26, 11, 30))),
}


@pytest.mark.parametrize("start,end,old,expected", MOVES.values(), ids=list(MOVES))
def test_changing_a_span(start, end, old, expected):
    assert server_module._resolve_span(start, end, old) == expected


BAD_SPANS = {
    "a time for a day": (None, at(2026, 10, 15, 12), OLD_DAYS, "of one kind"),
    "a day for a time": (None, date(2026, 10, 13), OLD_TIMED, "of one kind"),
    "moved past its own end": (
        at(2026, 10, 13, 9), at(2026, 10, 12, 9), OLD_TIMED, "end before it starts"),
    "last day before the first": (None, date(2026, 10, 13), OLD_DAYS, "end before it starts"),
}


@pytest.mark.parametrize("start,end,old,message", BAD_SPANS.values(), ids=list(BAD_SPANS))
def test_a_span_that_makes_no_sense_is_refused(start, end, old, message):
    with pytest.raises(ToolError, match=message):
        server_module._resolve_span(start, end, old)


# ── writing ──────────────────────────────────────────────────────────────────

def lines(component) -> list[str]:
    return component.to_ical().decode().splitlines()


def test_days_are_stored_ending_the_day_after():
    event = vevent("DTSTART:20261012T080000Z\nDURATION:PT1H")
    server_module._set_span(event, date(2026, 10, 14), date(2026, 10, 16))
    assert "DTSTART;VALUE=DATE:20261014" in lines(event)
    assert "DTEND;VALUE=DATE:20261017" in lines(event)
    # DURATION and DTEND must not both be there.
    assert not [line for line in lines(event) if line.startswith("DURATION")]


def test_times_are_stored_in_the_configured_zone():
    event = vevent("DTSTART;VALUE=DATE:20261014")
    start = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    server_module._set_span(event, start, start + timedelta(hours=1))
    assert "DTSTART;TZID=Europe/Berlin:20261012T100000" in lines(event)
    assert "DTEND;TZID=Europe/Berlin:20261012T110000" in lines(event)


def test_without_a_configured_zone_times_are_stored_in_utc(monkeypatch):
    monkeypatch.setattr(server_module, "_TZ", None)
    event = vevent("DTSTART;VALUE=DATE:20261014")
    server_module._set_span(event, at(2026, 10, 12, 10), at(2026, 10, 12, 11))
    assert "DTSTART:20261012T080000Z" in lines(event)
    assert "DTEND:20261012T090000Z" in lines(event)


def test_what_is_stored_reads_back_the_same():
    event = vevent("DTSTART;VALUE=DATE:20261014")
    for span in (OLD_TIMED, OLD_DAYS):
        server_module._set_span(event, *span)
        assert server_module._span(event) == span


def test_text_is_replaced_or_removed_never_doubled():
    event = vevent("DTSTART;VALUE=DATE:20261014\nLOCATION:Alt")
    server_module._set_text(event, "LOCATION", "Halle 2, Eingang B")
    assert [line for line in lines(event) if line.startswith("LOCATION")] == [
        "LOCATION:Halle 2\\, Eingang B"]
    server_module._set_text(event, "LOCATION", "")
    assert not [line for line in lines(event) if line.startswith("LOCATION")]


# ── series ───────────────────────────────────────────────────────────────────

RULE = "DTSTART:20261006T160000Z\nDURATION:PT90M\nSUMMARY:Training\nRRULE:FREQ=WEEKLY"
CHANGED = "RECURRENCE-ID:20261013T160000Z\nDTSTART:20261013T170000Z\nDURATION:PT1H\nSUMMARY:Verschoben"


def test_a_plain_event_is_no_series():
    master, series = server_module._master(vevents("DTSTART:20261012T080000Z\nSUMMARY:Zahnarzt"))
    assert (str(master["SUMMARY"]), series) == ("Zahnarzt", False)


def test_a_rule_makes_a_series():
    assert server_module._master(vevents(RULE))[1] is True


def test_the_event_with_the_rule_is_the_one_edited_wherever_it_stands():
    """Servers do not promise to put it first."""
    for ical in (vevents(RULE, CHANGED), vevents(CHANGED, RULE)):
        master, series = server_module._master(ical)
        assert (str(master["SUMMARY"]), series) == ("Training", True)


def test_a_lone_occurrence_is_edited_as_what_it_is():
    """An invitation to one date of somebody else's series arrives like this."""
    master, series = server_module._master(vevents(CHANGED))
    assert (str(master["SUMMARY"]), series) == ("Verschoben", False)


def test_an_object_without_an_event_is_refused():
    todo = icalendar.Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VTODO\r\nUID:t1\r\n"
        "DTSTAMP:20261001T000000Z\r\nSUMMARY:Task\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    with pytest.raises(ToolError, match="not an event"):
        server_module._master(todo)


# ── one occurrence of a series ───────────────────────────────────────────────
#
# All occurrences share the object of their series. One is named by where the
# rule puts it; changed before, it has a VEVENT of its own in the object.

ALARM = "BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT15M\nDESCRIPTION:Reminder\nEND:VALARM"
RULE_AND_MORE = f"{RULE}\nEXDATE:20261027T170000Z\nLOCATION:Halle 1\n{ALARM}"
FLOATING = "DTSTART:20261006T180000\nDTEND:20261006T193000\nSUMMARY:Training\nRRULE:FREQ=WEEKLY"
TWO_DAYS = "DTSTART;VALUE=DATE:20261003\nDTEND;VALUE=DATE:20261005\nSUMMARY:Markt\nRRULE:FREQ=WEEKLY"


def test_an_occurrence_is_shown_by_where_the_rule_puts_it():
    """Not by where it starts now — that changes when it is moved."""
    moved = server_module._event_dict(vevent(CHANGED), "uri", "Privat")
    assert (moved["start"], moved["occurrence"]) == ("2026-10-13T19:00+02:00", "2026-10-13T18:00+02:00")
    day = vevent("DTSTART;VALUE=DATE:20261018\nRECURRENCE-ID;VALUE=DATE:20261017")
    assert server_module._event_dict(day, "uri", "Familie")["occurrence"] == "2026-10-17"


def test_an_occurrence_changed_before_is_the_one_in_the_object():
    ical = vevents(RULE, CHANGED)
    event, stored = server_module._occurrence(ical, at(2026, 10, 13, 18))
    assert stored is True
    assert event is ical.walk("VEVENT")[1]


def test_any_other_occurrence_is_made_from_the_rule():
    ical = vevents(RULE_AND_MORE, CHANGED)
    event, stored = server_module._occurrence(ical, at(2026, 10, 20, 18))
    assert stored is False
    made = lines(event)
    # Its own date, in the form the series writes its start in …
    for expected in ("RECURRENCE-ID:20261020T160000Z", "DTSTART:20261020T160000Z",
                     "DTEND:20261020T173000Z", "UID:u1", "SUMMARY:Training", "LOCATION:Halle 1",
                     "BEGIN:VALARM", "TRIGGER:-PT15M"):
        assert expected in made, expected
    # … and nothing that would make it a series of its own.
    assert not [line for line in made if line.startswith(("RRULE", "EXDATE", "RDATE", "DURATION"))]
    # Finding it does not add it.
    assert len(ical.walk("VEVENT")) == 2


NOT_OCCURRENCES = {
    "a day the rule skips": (vevents(RULE, CHANGED), at(2026, 10, 14, 18)),
    "another time of day": (vevents(RULE, CHANGED), at(2026, 10, 20, 19)),
    "left out before": (vevents(RULE_AND_MORE), at(2026, 10, 27, 18)),
    "where a moved one starts now": (vevents(RULE, CHANGED), at(2026, 10, 13, 19)),
    "before the series began": (vevents(RULE), at(2026, 9, 29, 18)),
    "a day for a series with a time": (vevents(RULE), date(2026, 10, 20)),
    "a time for a series of days": (vevents(TWO_DAYS), at(2026, 10, 10, 0)),
    "the second day of an occurrence": (vevents(TWO_DAYS), date(2026, 10, 11)),
}


@pytest.mark.parametrize("ical,moment", NOT_OCCURRENCES.values(), ids=list(NOT_OCCURRENCES))
def test_what_the_rule_does_not_produce_is_no_occurrence(ical, moment):
    with pytest.raises(ToolError, match="no occurrence at.*exactly as list_events"):
        server_module._occurrence(ical, moment)


def test_an_occurrence_of_a_series_without_a_zone_is_found_by_local_time():
    event, stored = server_module._occurrence(vevents(FLOATING), at(2026, 10, 13, 18))
    assert stored is False
    assert "RECURRENCE-ID:20261013T180000" in lines(event)


def test_an_occurrence_of_a_series_of_days_is_found_by_its_first_day():
    event, stored = server_module._occurrence(vevents(TWO_DAYS), date(2026, 10, 10))
    assert stored is False
    assert "RECURRENCE-ID;VALUE=DATE:20261010" in lines(event)
    assert server_module._span(event) == (date(2026, 10, 10), date(2026, 10, 11))


NEW_YORK = ZoneInfo("America/New_York")
AS_WRITTEN = {
    "a series of days": (date(2026, 10, 3), date(2026, 10, 17), date(2026, 10, 17)),
    "a series in utc": (
        datetime(2026, 10, 6, 16, tzinfo=UTC), at(2026, 10, 20, 18),
        datetime(2026, 10, 20, 16, tzinfo=UTC)),
    "a series in another zone": (
        datetime(2026, 10, 6, 12, tzinfo=NEW_YORK), at(2026, 10, 20, 18),
        datetime(2026, 10, 20, 12, tzinfo=NEW_YORK)),
    "a series without a zone": (
        datetime(2026, 10, 6, 18), datetime(2026, 10, 20, 16, tzinfo=UTC), datetime(2026, 10, 20, 18)),
}


@pytest.mark.parametrize("start,moment,expected", AS_WRITTEN.values(), ids=list(AS_WRITTEN))
def test_a_left_out_date_is_written_the_way_the_series_writes_its_start(start, moment, expected):
    written = server_module._as_written(start, moment)
    assert written == expected
    assert getattr(written, "tzinfo", None) == getattr(expected, "tzinfo", None)
