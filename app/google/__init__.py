"""Backend-side management of the Google Suite skills' OAuth links.

The five Google skills (``gmail``, ``gcalendar``, ``gdrive``, ``gsheets``,
``gdocs``) each mint their own Google credential during ``link`` and store it
under ``<profile>/skills/<skill>/scripts/.google_token.json``. This package is
the operator-facing other half: it reports what is linked and takes a link apart
again.
"""

from app.google.registry import GOOGLE_SKILLS, GoogleSkill, by_name, names

__all__ = ["GOOGLE_SKILLS", "GoogleSkill", "by_name", "names"]
