"""The service against a real CalDAV server.

Radicale, served in-process from a temporary directory — nothing leaves the
machine. This is where the things a fake cannot show are pinned: that a series
comes back one occurrence at a time, and that changing an event leaves alone
what the tools know nothing about.

Skipped when Radicale is not installed (it is in requirements-dev.txt).
"""
import threading
import uuid
from wsgiref.simple_server import WSGIRequestHandler, make_server

import caldav
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server as server_module

radicale = pytest.importorskip("radicale")

SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:training
DTSTAMP:20261001T000000Z
DTSTART;TZID=Europe/Berlin:20261006T180000
DURATION:PT90M
SUMMARY:Training
RRULE:FREQ=WEEKLY
EXDATE;TZID=Europe/Berlin:20261020T180000
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:training
DTSTAMP:20261001T000000Z
RECURRENCE-ID;TZID=Europe/Berlin:20261013T180000
DTSTART;TZID=Europe/Berlin:20261013T190000
DURATION:PT60M
SUMMARY:Training (verschoben)
END:VEVENT
END:VCALENDAR
"""

# The same series as a server may hand it out: the changed occurrence first.
_HEAD, _RULE, _CHANGED = SERIES.split("BEGIN:VEVENT")
SERIES_CHANGED_FIRST = "BEGIN:VEVENT".join(
    [_HEAD, _CHANGED.replace("END:VCALENDAR\n", ""), _RULE + "END:VCALENDAR\n"])

ALL_DAY_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:market
DTSTAMP:20261001T000000Z
DTSTART;VALUE=DATE:20261003
DTEND;VALUE=DATE:20261004
SUMMARY:Flohmarkt
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR
"""

INVITATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:dinner
DTSTAMP:20261001T000000Z
DTSTART;TZID=Europe/Berlin:20261015T190000
DTEND;TZID=Europe/Berlin:20261015T210000
SUMMARY:Abendessen
ORGANIZER:mailto:anna@example.net
ATTENDEE;PARTSTAT=ACCEPTED:mailto:stefan@example.net
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT30M
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
END:VCALENDAR
"""


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def radicale_url(tmp_path_factory):
    from radicale import Application, config

    configuration = config.load()
    storage = {"filesystem_folder": str(tmp_path_factory.mktemp("radicale"))}
    # delay: Radicale answers a first login slowly on purpose — a second per
    # test here, for a brute-force guard that has nothing to guard.
    configuration.update({"storage": storage, "auth": {"type": "none", "delay": "0"}}, "test")
    httpd = make_server("127.0.0.1", 0, Application(configuration), handler_class=_Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def account(radicale_url, monkeypatch):
    """A fresh account with two calendars and a task list; the tools wired to it.

    One server for the whole run, one user per test: Radicale keeps every
    user's collections apart, so no test sees what another left behind.
    """
    service = server_module.CalDAVService()
    service.connect(radicale_url, f"user-{uuid.uuid4().hex[:8]}", "irrelevant")
    for name, holds in (("Privat", "VEVENT"), ("Familie", "VEVENT"), ("Aufgaben", "VTODO")):
        service._principal.make_calendar(
            name=name, cal_id=name.lower(), supported_calendar_component_set=[holds])
    monkeypatch.setattr(server_module, "_service", service)
    monkeypatch.setattr(server_module, "_connected", True)
    return service


def stored(service, uri: str) -> str:
    """The object as the server holds it now."""
    calendar_url = server_module._calendar_of(uri)
    return service._load(calendar_url, uri).data


def put(service, calendar: str, ical: str) -> str:
    """Store an object as it is, the way another calendar app would have."""
    url = server_module._pick_calendar(calendar)["url"]
    obj = caldav.Event(client=service._client, data=ical, parent=service._calendars[url])
    obj.save(only_this_recurrence=False)
    return str(obj.url)


def titles(result) -> list[tuple[str, str]]:
    return [(e["start"], e["title"]) for e in result["events"]]


def october(calendar="Privat") -> dict:
    return server_module.list_events("2026-10-05", "2026-11-01", calendar=calendar)


def count(data: str, line_start: str) -> int:
    return sum(line.startswith(line_start) for line in data.splitlines())


WEEKLY = "RRULE:FREQ=WEEKLY"  # the series' own rule; the time zone definition has rules too


def test_a_task_list_is_not_offered_as_a_calendar(account):
    assert sorted(c["name"] for c in server_module.list_calendars()) == ["Familie", "Privat"]


def test_what_is_created_is_what_is_listed(account):
    timed = server_module.create_event(
        "Zahnarzt", "2026-10-12T10:00", location="Hauptstr. 1, München",
        description="Karte\nmitnehmen", calendar="Privat")
    days = server_module.create_event("Urlaub", "2026-10-14", "2026-10-16", calendar="Familie")
    assert (timed["start"], timed["end"]) == ("2026-10-12T10:00+02:00", "2026-10-12T11:00+02:00")
    assert (days["start"], days["end"], days["all_day"]) == ("2026-10-14", "2026-10-16", True)

    result = server_module.list_events("2026-10-12", "2026-10-18")
    assert result == {"events": [timed, days], "warnings": []}
    # The last day of the holiday is still inside it; the day after is not.
    assert titles(server_module.list_events("2026-10-16", "2026-10-16")) == [("2026-10-14", "Urlaub")]
    assert titles(server_module.list_events("2026-10-17", "2026-10-17")) == []


def test_a_created_time_carries_its_zone(account):
    """Otherwise a calendar app would have to guess what 10:00 meant."""
    created = server_module.create_event("Zahnarzt", "2026-10-12T10:00", calendar="Privat")
    data = stored(account, created["uri"])
    assert "DTSTART;TZID=Europe/Berlin:20261012T100000" in data
    assert "BEGIN:VTIMEZONE" in data


def test_a_series_is_listed_one_occurrence_at_a_time(account):
    uri = put(account, "Privat", SERIES)
    result = server_module.list_events("2026-10-05", "2026-11-01", calendar="Privat")
    assert titles(result) == [
        ("2026-10-06T18:00+02:00", "Training"),
        # Moved by an hour on its own, the 20th left out, and the 27th is
        # after the end of summer time.
        ("2026-10-13T19:00+02:00", "Training (verschoben)"),
        ("2026-10-27T18:00+01:00", "Training"),
    ]
    assert {e["uri"] for e in result["events"]} == {uri}
    assert all(e["recurring"] for e in result["events"])
    # What tells them apart is where the rule puts each one — for the moved
    # occurrence that is not where it now starts.
    assert [e["occurrence"] for e in result["events"]] == [
        "2026-10-06T18:00+02:00", "2026-10-13T18:00+02:00", "2026-10-27T18:00+01:00"]


def test_search_finds_by_text_across_calendars(account):
    put(account, "Privat", SERIES)
    server_module.create_event(
        "Elternabend", "2026-10-20T19:00", location="Turnhalle", calendar="Familie")
    found = server_module.search_events("TRAINING", "2026-10-05", "2026-10-14")
    assert [e["title"] for e in found["events"]] == ["Training", "Training (verschoben)"]
    assert [e["title"] for e in server_module.search_events("turnhalle", "2026-10-01")["events"]] == [
        "Elternabend"]


def test_moving_an_event_keeps_everything_else(account):
    uri = put(account, "Privat", INVITATION)
    moved = server_module.update_event(uri, start="2026-10-16T18:30", location="Bei Anna")
    assert (moved["start"], moved["end"], moved["location"]) == (
        "2026-10-16T18:30+02:00", "2026-10-16T20:30+02:00", "Bei Anna")
    data = stored(account, uri)
    assert "DTSTART;TZID=Europe/Berlin:20261016T183000" in data
    for kept in ("BEGIN:VALARM", "TRIGGER:-PT30M", "ORGANIZER:mailto:anna@example.net",
                 "ATTENDEE;PARTSTAT=ACCEPTED:mailto:stefan@example.net", "SUMMARY:Abendessen"):
        assert kept in data, kept
    assert titles(server_module.list_events("2026-10-15", "2026-10-16")) == [
        ("2026-10-16T18:30+02:00", "Abendessen")]


def test_a_text_can_be_set_and_removed(account):
    uri = server_module.create_event(
        "Urlaub", "2026-10-14", description="Südtirol", calendar="Familie")["uri"]
    assert server_module.update_event(uri, clear_description=True, title="Ferien")["description"] == ""
    data = stored(account, uri)
    assert "DESCRIPTION" not in data and "SUMMARY:Ferien" in data


def test_a_series_is_not_changed_by_accident(account):
    uri = put(account, "Privat", SERIES)
    before = stored(account, uri)
    with pytest.raises(ToolError, match="recurring event.*whole_series=True"):
        server_module.update_event(uri, title="Yoga")
    with pytest.raises(ToolError, match="recurring event.*whole_series=True"):
        server_module.delete_event(uri)
    with pytest.raises(ToolError, match="time of a whole series"):
        server_module.update_event(uri, start="2026-10-06T19:00", whole_series=True)
    assert stored(account, uri) == before


def test_a_series_changed_on_purpose_keeps_its_rule_and_exceptions(account):
    uri = put(account, "Privat", SERIES)
    server_module.update_event(uri, title="Yoga", location="Halle 2", whole_series=True)
    data = stored(account, uri)
    for kept in ("RRULE:FREQ=WEEKLY", "EXDATE;TZID=Europe/Berlin:20261020T180000", "BEGIN:VALARM",
                 "RECURRENCE-ID;TZID=Europe/Berlin:20261013T180000", "SUMMARY:Training (verschoben)"):
        assert kept in data, kept
    assert titles(server_module.list_events("2026-10-05", "2026-10-14", calendar="Privat")) == [
        ("2026-10-06T18:00+02:00", "Yoga"),
        ("2026-10-13T19:00+02:00", "Training (verschoben)"),
    ]


# ── one occurrence of a series ───────────────────────────────────────────────

def test_one_occurrence_is_moved_and_the_others_stay(account):
    uri = put(account, "Privat", SERIES)
    moved = server_module.update_event(
        uri, occurrence="2026-10-27T18:00+01:00", start="2026-10-29T19:00", location="Halle 2")
    assert (moved["start"], moved["end"], moved["occurrence"], moved["location"]) == (
        "2026-10-29T19:00+01:00", "2026-10-29T20:30+01:00", "2026-10-27T18:00+01:00", "Halle 2")
    assert [(e["start"], e["title"], e["location"]) for e in october()["events"]] == [
        ("2026-10-06T18:00+02:00", "Training", ""),
        ("2026-10-13T19:00+02:00", "Training (verschoben)", ""),
        ("2026-10-29T19:00+01:00", "Training", "Halle 2"),
    ]
    data = stored(account, uri)
    # The rule and what was left out before are untouched; the moved occurrence
    # is a second exception next to the first, with the reminder of the series.
    for kept in ("RRULE:FREQ=WEEKLY", "EXDATE;TZID=Europe/Berlin:20261020T180000",
                 "RECURRENCE-ID;TZID=Europe/Berlin:20261013T180000",
                 "RECURRENCE-ID;TZID=Europe/Berlin:20261027T180000"):
        assert kept in data, kept
    assert (count(data, "BEGIN:VEVENT"), count(data, WEEKLY), count(data, "BEGIN:VALARM")) == (3, 1, 2)
    assert count(data, "EXDATE") == 1


def test_an_occurrence_changed_before_is_changed_again_in_place(account):
    """Not given a second VEVENT — two for one date, and apps show either."""
    uri = put(account, "Privat", SERIES)
    again = server_module.update_event(uri, occurrence="2026-10-13T18:00+02:00", title="Training kurz")
    assert (again["start"], again["end"], again["title"]) == (
        "2026-10-13T19:00+02:00", "2026-10-13T20:00+02:00", "Training kurz")
    data = stored(account, uri)
    assert (count(data, "BEGIN:VEVENT"), count(data, "RECURRENCE-ID")) == (2, 1)
    assert "SUMMARY:Training (verschoben)" not in data


def test_only_the_title_of_one_occurrence_changes(account):
    uri = put(account, "Privat", SERIES)
    server_module.update_event(uri, occurrence="2026-10-06T18:00+02:00", title="Probetraining")
    assert titles(october()) == [
        ("2026-10-06T18:00+02:00", "Probetraining"),
        ("2026-10-13T19:00+02:00", "Training (verschoben)"),
        ("2026-10-27T18:00+01:00", "Training"),
    ]


def test_one_occurrence_is_cancelled_and_the_series_goes_on(account):
    uri = put(account, "Privat", SERIES)
    assert server_module.delete_event(uri, occurrence="2026-10-27T18:00+01:00") == (
        "Event deleted: Training (2026-10-27T18:00+01:00)")
    assert titles(server_module.list_events("2026-10-05", "2026-11-08", calendar="Privat")) == [
        ("2026-10-06T18:00+02:00", "Training"),
        ("2026-10-13T19:00+02:00", "Training (verschoben)"),
        ("2026-11-03T18:00+01:00", "Training"),
    ]
    data = stored(account, uri)
    # Left out the way the series writes its own start, next to the one that
    # was left out before.
    assert "EXDATE;TZID=Europe/Berlin:20261027T180000" in data
    assert "EXDATE;TZID=Europe/Berlin:20261020T180000" in data
    assert "RRULE:FREQ=WEEKLY" in data


def test_cancelling_a_changed_occurrence_does_not_bring_the_original_back(account):
    uri = put(account, "Privat", SERIES)
    assert server_module.delete_event(uri, occurrence="2026-10-13T18:00+02:00") == (
        "Event deleted: Training (verschoben) (2026-10-13T18:00+02:00)")
    assert titles(october()) == [
        ("2026-10-06T18:00+02:00", "Training"),
        ("2026-10-27T18:00+01:00", "Training"),
    ]
    data = stored(account, uri)
    assert (count(data, "BEGIN:VEVENT"), count(data, "RECURRENCE-ID")) == (1, 0)


NOT_AN_OCCURRENCE = {
    "a day the rule skips": "2026-10-14T18:00+02:00",
    "another time of day": "2026-10-27T19:00+01:00",
    "left out before": "2026-10-20T18:00+02:00",
    "where a moved one now starts": "2026-10-13T19:00+02:00",
    "a day for a series with a time": "2026-10-27",
}


@pytest.mark.parametrize("occurrence", NOT_AN_OCCURRENCE.values(), ids=list(NOT_AN_OCCURRENCE))
def test_what_is_no_occurrence_of_the_series_changes_nothing(account, occurrence):
    uri = put(account, "Privat", SERIES)
    before = stored(account, uri)
    with pytest.raises(ToolError, match="no occurrence at.*exactly as list_events"):
        server_module.update_event(uri, occurrence=occurrence, title="x")
    with pytest.raises(ToolError, match="no occurrence at"):
        server_module.delete_event(uri, occurrence=occurrence)
    assert stored(account, uri) == before


def test_one_day_of_an_all_day_series(account):
    uri = put(account, "Familie", ALL_DAY_SERIES)
    listed = server_module.list_events("2026-10-05", "2026-10-25", calendar="Familie")["events"]
    assert [e["occurrence"] for e in listed] == ["2026-10-10", "2026-10-17", "2026-10-24"]
    server_module.delete_event(uri, occurrence="2026-10-17")
    moved = server_module.update_event(uri, occurrence="2026-10-24", start="2026-10-25")
    assert (moved["start"], moved["end"], moved["occurrence"]) == (
        "2026-10-25", "2026-10-25", "2026-10-24")
    assert titles(server_module.list_events("2026-10-05", "2026-10-25", calendar="Familie")) == [
        ("2026-10-10", "Flohmarkt"), ("2026-10-25", "Flohmarkt")]
    assert "EXDATE;VALUE=DATE:20261017" in stored(account, uri)


def test_a_series_stored_with_the_changed_occurrence_first_is_written_back_whole(account):
    """The CalDAV client, left to its default, would take such an object for a
    single occurrence and merge it into what is on the server."""
    uri = put(account, "Privat", SERIES_CHANGED_FIRST)
    server_module.update_event(uri, title="Yoga", whole_series=True)
    server_module.delete_event(uri, occurrence="2026-10-27T18:00+01:00")
    data = stored(account, uri)
    assert (count(data, "BEGIN:VEVENT"), count(data, WEEKLY), count(data, "RECURRENCE-ID")) == (2, 1, 1)
    assert titles(october()) == [
        ("2026-10-06T18:00+02:00", "Yoga"),
        ("2026-10-13T19:00+02:00", "Training (verschoben)"),
    ]


def test_an_occurrence_named_for_a_plain_event_is_just_that_event(account):
    uri = server_module.create_event("Zahnarzt", "2026-10-12T10:00", calendar="Privat")["uri"]
    server_module.update_event(uri, occurrence="2026-10-12T10:00+02:00", title="Zahnärztin")
    assert server_module.delete_event(uri, occurrence="2026-10-12T10:00+02:00").startswith(
        "Event deleted: Zahnärztin")
    assert october()["events"] == []


def test_delete_removes_one_event_or_a_whole_series(account):
    series = put(account, "Privat", SERIES)
    single = server_module.create_event("Zahnarzt", "2026-10-12T10:00", calendar="Privat")["uri"]
    assert server_module.delete_event(single) == "Event deleted: Zahnarzt"
    assert server_module.delete_event(series, whole_series=True) == "Event deleted: Training"
    assert server_module.list_events("2026-10-05", "2026-11-01") == {"events": [], "warnings": []}


def test_an_event_that_is_gone_says_so(account):
    uri = server_module.create_event("Zahnarzt", "2026-10-12T10:00", calendar="Privat")["uri"]
    server_module.delete_event(uri)
    with pytest.raises(ToolError, match="Event not found"):
        server_module.update_event(uri, title="x")
    with pytest.raises(ToolError, match="Event not found"):
        server_module.delete_event(uri)


def test_a_task_cannot_be_reached_through_an_event_uri(account):
    """The task list is on the same server, but it is not one of the calendars."""
    tasks = next(c for c in account._principal.get_calendars() if "aufgaben" in str(c.url))
    todo = tasks.add_todo(summary="Steuererklärung")
    with pytest.raises(ToolError, match="Invalid event URI"):
        server_module.delete_event(str(todo.url))
    assert caldav.Todo(client=account._client, url=str(todo.url), parent=tasks).load().data
