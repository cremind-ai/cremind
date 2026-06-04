"""Thin wrapper around the `caldav` library.

Connects with username + password, discovers calendars via principal lookup,
and resolves calendar names → Calendar objects.
"""
from __future__ import annotations

from typing import Optional

import caldav
from caldav.lib.error import AuthorizationError, NotFoundError

from . import config


class CalDAVError(RuntimeError):
    pass


class CalDAVClient:
    """Lazy-connected CalDAV client. Use as a context manager."""

    def __init__(self) -> None:
        url, username, password = config.require_credentials()
        self._url = url
        self._username = username
        self._password = password
        self._client: Optional[caldav.DAVClient] = None
        self._principal: Optional[caldav.Principal] = None
        self._calendars: Optional[list[caldav.Calendar]] = None

    def __enter__(self) -> "CalDAVClient":
        self._connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _connect(self) -> None:
        try:
            self._client = caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=config.HTTP_TIMEOUT,
            )
            self._principal = self._client.principal()
        except AuthorizationError as e:
            raise CalDAVError(
                f"CalDAV login failed for {self._username} at {self._url}: {e}. "
                "Most providers require an app-specific password when 2FA is on. "
                "iCloud: generate at https://appleid.apple.com."
            ) from e
        except Exception as e:
            raise CalDAVError(
                f"Failed to connect to CalDAV server at {self._url}: {e}. "
                "Verify the URL is correct and the server is reachable."
            ) from e

    def close(self) -> None:
        # caldav's DAVClient holds an httpx.Client; close it if available.
        client = self._client
        if client is not None:
            try:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
        self._client = None
        self._principal = None
        self._calendars = None

    def calendars(self) -> list[caldav.Calendar]:
        if self._calendars is None:
            if self._principal is None:
                raise CalDAVError("Not connected")
            try:
                self._calendars = self._principal.calendars()
            except Exception as e:
                raise CalDAVError(f"Failed to list calendars: {e}") from e
        return self._calendars

    def calendar_info(self) -> list[dict]:
        """Return [{name, url, default}] for each calendar."""
        default_name = config.CALDAV_CALENDAR or None
        rows = []
        for cal in self.calendars():
            try:
                name = self._calendar_name(cal)
            except Exception:
                name = str(cal.url)
            rows.append({
                "name": name,
                "url": str(cal.url),
                "default": (name == default_name) if default_name else False,
            })
        # Mark first as default when no explicit env var is set.
        if not default_name and rows:
            rows[0]["default"] = True
        return rows

    def find_calendar(self, name: Optional[str] = None) -> caldav.Calendar:
        """Resolve `name` (CLI flag) → `CALDAV_CALENDAR` env → first calendar."""
        target = name or config.CALDAV_CALENDAR or None
        cals = self.calendars()
        if not cals:
            raise CalDAVError(
                "No calendars found on this account. Check that the account has at "
                "least one calendar configured (some providers don't create one by default)."
            )
        if target:
            for cal in cals:
                try:
                    if self._calendar_name(cal).lower() == target.lower():
                        return cal
                except Exception:
                    continue
            available = ", ".join(self._safe_name(c) for c in cals)
            raise CalDAVError(
                f"Calendar {target!r} not found. Available: {available}"
            )
        return cals[0]

    @staticmethod
    def _calendar_name(cal: caldav.Calendar) -> str:
        # Prefer the cached displayname; fall back to a server fetch.
        name = getattr(cal, "name", None)
        if name:
            return str(name)
        try:
            return str(cal.get_display_name() or cal.url)
        except Exception:
            return str(cal.url)

    @classmethod
    def _safe_name(cls, cal: caldav.Calendar) -> str:
        try:
            return cls._calendar_name(cal)
        except Exception:
            return "<unknown>"


def event_not_found(uid: str) -> CalDAVError:
    return CalDAVError(
        f"Event not found: {uid}. "
        "Use `list` to find the correct UID, or pass --calendar to target a different one."
    )


__all__ = ["CalDAVClient", "CalDAVError", "event_not_found", "AuthorizationError", "NotFoundError"]
