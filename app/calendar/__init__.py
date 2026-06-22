"""Calendar & Schedule system.

The home of Cremind's internal calendar and the time-based "Schedule Events"
engine. Layout:

- :mod:`app.calendar.feature`     — per-profile on/off flag for the feature.
- :mod:`app.calendar.recurrence`  — next-occurrence math (rolling pointer).
- :mod:`app.calendar.provider`    — CalendarProvider seam (internal default;
                                     Google Calendar in a later phase).

The time-based trigger engine itself lives in :mod:`app.events.schedule_manager`
(alongside the other event managers) and reuses the per-conversation event queue.
"""
