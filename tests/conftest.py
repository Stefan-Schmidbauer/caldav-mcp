"""Shared fixtures.

`server.py` reads every setting from the environment at module scope and raises
`OSError` without credentials, so the environment has to be set up *before* the
import — not inside a fixture. That is why the assignments below sit at module
level and the import follows them.
"""
import os

os.environ.setdefault("CALDAV_URL", "https://dav.test/caldav/")
os.environ.setdefault("CALDAV_USERNAME", "tester")
os.environ.setdefault("CALDAV_PASSWORD", "test-password")
# Assigned, not defaulted: the expected offsets below are Berlin's, whatever the
# machine running the suite is set to.
os.environ["TZ"] = "Europe/Berlin"
os.environ.pop("CALDAV_CALENDAR_URL", None)

import pytest

import server as _server

PRIVATE = "https://dav.test/caldav/privat/"
WORK = "https://dav.test/caldav/arbeit/"


class FakeService:
    """Stands in for CalDAVService and records what the tools ask of it.

    The tools are a thin layer over the service, so what is worth pinning is
    what they pass down — and, for the URI check, that they pass nothing at all.
    """

    def __init__(self):
        self.listed = [
            {"url": PRIVATE, "name": "Privat"},
            {"url": WORK, "name": "Arbeit"},
        ]
        # What a refresh brings to light: a calendar added after the last look.
        self.appears_on_refresh: list[dict] = []
        self.events: dict[str, list[dict]] = {}
        self.unreadable: dict[str, int] = {}
        self.broken: set[str] = set()
        self.searched: list[tuple] = []
        self.created: list[tuple] = []
        self.updated: list[tuple] = []
        self.deleted: list[tuple] = []
        self.fails_with: Exception | None = None

    def calendars(self, refresh=False):
        if refresh:
            self.listed += self.appears_on_refresh
            self.appears_on_refresh = []
        return list(self.listed)

    def search(self, calendar_url, start, end):
        self.searched.append((calendar_url, start, end))
        if calendar_url in self.broken:
            raise RuntimeError("503 Service Unavailable")
        return list(self.events.get(calendar_url, [])), self.unreadable.get(calendar_url, 0)

    def create(self, calendar_url, title, start, end, location="", description=""):
        if self.fails_with:
            raise self.fails_with
        self.created.append((calendar_url, title, start, end, location, description))
        return {"uri": f"{calendar_url}NEW.ics", "title": title}

    def update(self, calendar_url, uri, **changes):
        if self.fails_with:
            raise self.fails_with
        self.updated.append((calendar_url, uri, changes))
        return {"uri": uri}

    def delete(self, calendar_url, uri, occurrence=None, whole_series=False):
        if self.fails_with:
            raise self.fails_with
        self.deleted.append((calendar_url, uri, occurrence, whole_series))
        return "Zahnarzt"


@pytest.fixture
def service(monkeypatch):
    """The server module wired to a FakeService, already 'connected'."""
    fake = FakeService()
    monkeypatch.setattr(_server, "_service", fake)
    monkeypatch.setattr(_server, "_connected", True)
    return fake
