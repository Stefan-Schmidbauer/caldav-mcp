"""The tool layer: what reaches the CalDAV service, and what must not.

The service is replaced by a fake here (test_caldav.py runs the real one).
These pin the decisions the tools make on top of it — which calendar and which
URI they accept, what a date range means, and what is said when a calendar
cannot be read.
"""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import caldav
import pytest
from conftest import PRIVATE, WORK
from mcp.server.mcpserver.exceptions import ToolError

import server as server_module

BERLIN = ZoneInfo("Europe/Berlin")
EVENT = f"{PRIVATE}ABC.ics"


def at(*args) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


def event(title, start, calendar="Privat", location="", description=""):
    return {"uri": f"{PRIVATE}{title}.ics", "calendar": calendar, "title": title, "start": start,
            "end": start, "location": location, "description": description}


def test_all_tools_are_registered():
    names = {t.name for t in asyncio.run(server_module.mcp.list_tools())}
    assert names == {
        "list_calendars", "list_events", "search_events",
        "create_event", "update_event", "delete_event",
    }


def test_reading_tools_are_marked_read_only_and_delete_destructive():
    """Clients use these to decide what may run without asking."""
    tools = {t.name: t.annotations for t in asyncio.run(server_module.mcp.list_tools())}
    read_only = {name for name, a in tools.items() if a and a.read_only_hint}
    assert read_only == {"list_calendars", "list_events", "search_events"}
    assert tools["delete_event"].destructive_hint is True


# ── event URIs ───────────────────────────────────────────────────────────────
#
# The CalDAV client sends the account's credentials to whatever URL it is given,
# and the URL is a tool argument — so it comes from the model.

def test_a_uri_from_a_calendar_is_passed_through_with_its_calendar(service):
    server_module.delete_event(EVENT)
    server_module.delete_event(f"{WORK}XYZ.ics")
    assert service.deleted == [(PRIVATE, EVENT, None, False), (WORK, f"{WORK}XYZ.ics", None, False)]


def test_default_port_and_percent_spelling_are_the_same_place(service):
    server_module.delete_event("https://dav.test:443/caldav/privat/ABC.ics")
    server_module.delete_event("https://dav.test/caldav/%70rivat/ABC.ics")
    assert len(service.deleted) == 2


FOREIGN_URIS = {
    "other host": "https://evil.example/caldav/privat/ABC.ics",
    "host prefix": "https://dav.test.evil.example/caldav/privat/ABC.ics",
    "userinfo trick": "https://dav.test@evil.example/caldav/privat/ABC.ics",
    "other scheme": "http://dav.test/caldav/privat/ABC.ics",
    "other port": "https://dav.test:8443/caldav/privat/ABC.ics",
    "malformed port": "https://dav.test:x/caldav/privat/ABC.ics",
    "relative": "/caldav/privat/ABC.ics",
    "empty": "",
    # Same server, but not inside a calendar that holds events — a task list,
    # say, or somebody else's collection.
    "unlisted collection": "https://dav.test/caldav/aufgaben/ABC.ics",
    "name prefix": "https://dav.test/caldav/privat-alt/ABC.ics",
    "the calendar itself": PRIVATE,
    "nested": "https://dav.test/caldav/privat/sub/ABC.ics",
    "dot-dot": "https://dav.test/caldav/privat/../aufgaben/ABC.ics",
    "bare dot-dot": "https://dav.test/caldav/privat/..",
    "encoded slash": "https://dav.test/caldav/privat/..%2Faufgaben%2FABC.ics",
}


@pytest.mark.parametrize("uri", FOREIGN_URIS.values(), ids=list(FOREIGN_URIS))
def test_a_foreign_uri_never_reaches_the_client(service, uri):
    with pytest.raises(ToolError, match="Invalid event URI"):
        server_module.delete_event(uri)
    with pytest.raises(ToolError, match="Invalid event URI"):
        server_module.update_event(uri, title="x")
    assert service.deleted == [] and service.updated == []


def test_an_event_in_a_calendar_added_since_is_found(service):
    """The calendar list is kept between calls, so a miss looks once more."""
    family = "https://dav.test/caldav/familie/"
    service.appears_on_refresh = [{"url": family, "name": "Familie"}]
    server_module.delete_event(f"{family}ABC.ics")
    assert service.deleted == [(family, f"{family}ABC.ics", None, False)]


# ── calendars ────────────────────────────────────────────────────────────────

def test_a_calendar_is_named_by_url_or_by_name(service):
    server_module.list_events(calendar=WORK)
    server_module.list_events(calendar="arbeit")
    assert [s[0] for s in service.searched] == [WORK, WORK]


def test_an_unknown_calendar_names_the_ones_there_are(service):
    with pytest.raises(ToolError, match="'Privat', 'Arbeit'"):
        server_module.list_events(calendar="https://evil.example/caldav/privat/")
    assert service.searched == []


def test_a_name_two_calendars_share_is_not_guessed(service):
    service.listed.append({"url": "https://dav.test/caldav/privat-2/", "name": "Privat"})
    with pytest.raises(ToolError, match="Several calendars"):
        server_module.create_event("Zahnarzt", "2026-10-12", calendar="Privat")
    assert service.created == []


def test_the_configured_calendar_is_the_default(service, monkeypatch):
    monkeypatch.setattr(server_module, "CALDAV_CALENDAR_URL", "https://dav.test/caldav/arbeit")
    assert server_module.list_calendars() == [
        {"url": PRIVATE, "name": "Privat", "default": False},
        {"url": WORK, "name": "Arbeit", "default": True},
    ]
    server_module.create_event("Zahnarzt", "2026-10-12")
    assert service.created[0][0] == WORK


def test_a_single_calendar_needs_no_default(service):
    service.listed = [{"url": PRIVATE, "name": "Privat"}]
    server_module.create_event("Zahnarzt", "2026-10-12")
    assert service.created[0][0] == PRIVATE


def test_without_a_default_create_asks_which_calendar(service):
    with pytest.raises(ToolError, match="'Privat', 'Arbeit'"):
        server_module.create_event("Zahnarzt", "2026-10-12")
    assert service.created == []


# ── listing ──────────────────────────────────────────────────────────────────

def test_list_defaults_to_seven_days_from_today(service, monkeypatch):
    monkeypatch.setattr(server_module, "_today", lambda: date(2026, 10, 10))
    server_module.list_events()
    assert service.searched == [
        (PRIVATE, at(2026, 10, 10), at(2026, 10, 17)),
        (WORK, at(2026, 10, 10), at(2026, 10, 17)),
    ]


def test_the_last_day_of_a_range_is_included(service):
    server_module.list_events("2026-10-12", "2026-10-18", calendar="Privat")
    assert service.searched == [(PRIVATE, at(2026, 10, 12), at(2026, 10, 19))]


def test_a_range_can_be_given_to_the_minute(service):
    server_module.list_events("2026-10-12T08:00", "2026-10-12T12:00", calendar="Privat")
    assert service.searched == [(PRIVATE, at(2026, 10, 12, 8), at(2026, 10, 12, 12))]


BAD_RANGES = {
    "reversed": ("2026-10-18", "2026-10-12", "before from_date"),
    "too long": ("2026-01-01", "2027-06-01", "longer than"),
    "not a date": ("next week", "", "Invalid from_date"),
}


@pytest.mark.parametrize("from_date,to_date,message", BAD_RANGES.values(), ids=list(BAD_RANGES))
def test_a_bad_range_is_refused(service, from_date, to_date, message):
    with pytest.raises(ToolError, match=message):
        server_module.list_events(from_date, to_date)
    assert service.searched == []


def test_events_of_all_calendars_come_in_order_all_day_first(service):
    service.events = {
        PRIVATE: [event("Zahnarzt", "2026-10-12T10:00+02:00"), event("Kino", "2026-10-13T20:00+02:00")],
        WORK: [
            event("Messe", "2026-10-12", "Arbeit"),
            event("Standup", "2026-10-12T09:00+02:00", "Arbeit"),
        ],
    }
    result = server_module.list_events("2026-10-12", "2026-10-18")
    assert [e["title"] for e in result["events"]] == ["Messe", "Standup", "Zahnarzt", "Kino"]
    assert result["warnings"] == []


def test_a_calendar_that_cannot_be_read_is_named_not_dropped(service):
    """Otherwise "nothing on that day" would be a guess."""
    service.events = {PRIVATE: [event("Zahnarzt", "2026-10-12T10:00+02:00")]}
    service.broken = {WORK}
    result = server_module.list_events("2026-10-12", "2026-10-18")
    assert [e["title"] for e in result["events"]] == ["Zahnarzt"]
    assert result["warnings"] == ["Calendar 'Arbeit' could not be read: 503 Service Unavailable"]


def test_when_no_calendar_can_be_read_that_is_an_error(service):
    service.broken = {PRIVATE, WORK}
    with pytest.raises(ToolError, match="'Privat' could not be read.*'Arbeit' could not be read"):
        server_module.list_events("2026-10-12", "2026-10-18")


def test_events_that_could_not_be_parsed_are_counted(service):
    service.unreadable = {PRIVATE: 2}
    result = server_module.list_events("2026-10-12", "2026-10-18")
    assert result["warnings"] == ["2 event(s) in 'Privat' could not be read and are missing here."]


def test_a_long_list_is_cut_and_says_so(service, monkeypatch):
    monkeypatch.setattr(server_module, "_MAX_EVENTS", 2)
    service.events = {PRIVATE: [event(f"E{i}", f"2026-10-1{i}") for i in (3, 1, 2)]}
    result = server_module.list_events("2026-10-10", "2026-10-18")
    assert [e["title"] for e in result["events"]] == ["E1", "E2"]
    assert result["warnings"] == [
        "Showing the first 2 of 3 events — ask for a shorter range to see the rest."
    ]


def test_an_account_without_calendars_says_so(service):
    service.listed = []
    assert server_module.list_events("2026-10-12", "2026-10-18") == {
        "events": [], "warnings": ["The account has no calendar that holds events."],
    }


# ── searching ────────────────────────────────────────────────────────────────

def test_search_looks_at_title_location_and_description(service):
    service.events = {PRIVATE: [
        event("Zahnarzt", "2026-10-12T10:00+02:00"),
        event("Termin", "2026-10-13T10:00+02:00", location="Praxis ZAHNARZT Müller"),
        event("Vorsorge", "2026-10-14T10:00+02:00", description="beim Zahnarzt"),
        event("Kino", "2026-10-15T20:00+02:00"),
    ]}
    result = server_module.search_events("zahnarzt ", calendar="Privat")
    assert [e["title"] for e in result["events"]] == ["Zahnarzt", "Termin", "Vorsorge"]


def test_search_defaults_to_a_month_back_and_a_year_ahead(service, monkeypatch):
    monkeypatch.setattr(server_module, "_today", lambda: date(2026, 10, 10))
    server_module.search_events("x", calendar="Privat")
    assert service.searched == [(PRIVATE, at(2026, 9, 10), at(2027, 10, 10))]


def test_an_empty_query_is_refused(service):
    with pytest.raises(ToolError, match="query is empty"):
        server_module.search_events("  ")
    assert service.searched == []


# ── creating ─────────────────────────────────────────────────────────────────

def test_a_day_makes_an_all_day_event_of_that_day(service):
    server_module.create_event("Urlaub", "2026-10-14", calendar="Privat")
    assert service.created == [(PRIVATE, "Urlaub", date(2026, 10, 14), date(2026, 10, 14), "", "")]


def test_a_time_makes_an_hour_long_event_in_local_time(service):
    server_module.create_event(" Zahnarzt ", "2026-10-12T10:00", location="Praxis",
                               description="Karte", calendar="Privat")
    assert service.created == [
        (PRIVATE, "Zahnarzt", at(2026, 10, 12, 10), at(2026, 10, 12, 11), "Praxis", "Karte")
    ]


def test_an_explicit_offset_is_the_same_instant_locally(service):
    server_module.create_event("Call", "2026-10-12T08:00Z", "2026-10-12T09:30+00:00", calendar="Privat")
    _, _, start, end, *_ = service.created[0]
    assert (start, end) == (at(2026, 10, 12, 10), at(2026, 10, 12, 11, 30))
    assert start.utcoffset() == end.utcoffset() == at(2026, 10, 12).utcoffset()


BAD_EVENTS = {
    "empty title": (" ", "2026-10-12", "", "title is empty"),
    "day and time mixed": ("X", "2026-10-12", "2026-10-12T11:00", "of one kind"),
    "ends before it starts": ("X", "2026-10-12T10:00", "2026-10-12T09:00", "end before it starts"),
    "not a time": ("X", "tomorrow at ten", "", "Invalid start"),
}


@pytest.mark.parametrize("title,start,end,message", BAD_EVENTS.values(), ids=list(BAD_EVENTS))
def test_a_bad_event_is_refused(service, title, start, end, message):
    with pytest.raises(ToolError, match=message):
        server_module.create_event(title, start, end, calendar="Privat")
    assert service.created == []


# ── changing and deleting ────────────────────────────────────────────────────

def test_update_passes_only_what_was_given(service):
    server_module.update_event(EVENT, start="2026-10-13T15:30")
    assert service.updated == [(PRIVATE, EVENT, {
        "title": "", "start": at(2026, 10, 13, 15, 30), "end": None,
        "location": None, "description": None, "occurrence": None, "whole_series": False,
    })]


def test_clear_wins_over_a_new_text(service):
    server_module.update_event(EVENT, location="keep me", clear_location=True,
                               description="new", whole_series=True)
    assert service.updated[0][2] == {
        "title": "", "start": None, "end": None,
        "location": "", "description": "new", "occurrence": None, "whole_series": True,
    }


def test_an_update_that_changes_nothing_is_refused(service):
    with pytest.raises(ToolError, match="Nothing to change"):
        server_module.update_event(EVENT, whole_series=True)
    assert service.updated == []


def test_delete_passes_the_series_flag_and_names_what_went(service):
    assert server_module.delete_event(EVENT, whole_series=True) == "Event deleted: Zahnarzt"
    assert service.deleted == [(PRIVATE, EVENT, None, True)]


# ── one occurrence of a series ───────────────────────────────────────────────
#
# All occurrences share the uri of their series; `occurrence`, as list_events
# shows it, says which one is meant.

def test_an_occurrence_is_passed_on_as_the_moment_it_names(service):
    server_module.update_event(EVENT, occurrence="2026-10-13T18:00+02:00", start="2026-10-15T18:00")
    server_module.delete_event(EVENT, occurrence="2026-10-13T16:00Z")
    assert service.updated[0][2]["occurrence"] == at(2026, 10, 13, 18)
    assert service.updated[0][2]["whole_series"] is False
    assert service.deleted == [(PRIVATE, EVENT, at(2026, 10, 13, 18), False)]


def test_an_occurrence_of_an_all_day_series_is_a_day(service):
    assert server_module.delete_event(EVENT, occurrence="2026-10-14") == (
        "Event deleted: Zahnarzt (2026-10-14)")
    assert service.deleted == [(PRIVATE, EVENT, date(2026, 10, 14), False)]


def test_deleting_an_occurrence_says_which_one_went(service):
    assert server_module.delete_event(EVENT, occurrence="2026-10-13T16:00Z") == (
        "Event deleted: Zahnarzt (2026-10-13T18:00+02:00)")


def test_one_occurrence_and_the_whole_series_at_once_is_refused(service):
    """One of the two is a mistake, and guessing which could delete a series."""
    with pytest.raises(ToolError, match="either occurrence or whole_series"):
        server_module.delete_event(EVENT, occurrence="2026-10-13T18:00+02:00", whole_series=True)
    with pytest.raises(ToolError, match="either occurrence or whole_series"):
        server_module.update_event(
            EVENT, title="x", occurrence="2026-10-13T18:00+02:00", whole_series=True)
    assert service.deleted == [] and service.updated == []


def test_an_occurrence_that_is_not_a_date_is_refused(service):
    with pytest.raises(ToolError, match="Invalid occurrence"):
        server_module.delete_event(EVENT, occurrence="the second one")
    assert service.deleted == []


# ── what the server says no to ───────────────────────────────────────────────
#
# Only a ToolError's text reaches the model; anything else is a bare "Error
# executing tool" to it.

def test_a_vanished_event_points_back_to_list_events(service):
    service.fails_with = caldav.error.NotFoundError("404")
    with pytest.raises(ToolError, match="Event not found.*list_events"):
        server_module.update_event(EVENT, title="x")
    with pytest.raises(ToolError, match="Event not found"):
        server_module.delete_event(EVENT)


def test_a_refusal_by_the_server_is_passed_on(service):
    """A read-only calendar, for one — the birthdays calendar some servers generate."""
    service.fails_with = caldav.error.PutError("403 Forbidden")
    with pytest.raises(ToolError, match="refused the request.*403 Forbidden"):
        server_module.create_event("Zahnarzt", "2026-10-12", calendar="Privat")
