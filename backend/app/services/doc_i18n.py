"""Language of server-made documents: PDF reports, CSV exports, e-mails, notifications.

The web UI is translated by the frontend; documents built by the server are not. Rule
(decided 2026-11-11): a document is written in the **profile language** (`cms_users.locale`)
of the person it is made for —
- a download: the user who asked for it;
- a scheduled send: the user who created the schedule;
- a notification channel: the user who created the channel (stored in its `config`).
Missing or unknown locale → English, the product default.

Each generator keeps its own table `{locale: {key: text}}` next to its code (as in
`approval_emails.py`); this module only normalises the locale and looks the text up.
"""

from __future__ import annotations

SUPPORTED_LOCALES = ("en", "fr", "es", "pt", "de")
DEFAULT_LOCALE = "en"


def norm_locale(locale: str | None) -> str:
    """'fr-FR' / 'FR' / None → a supported product locale (fallback English)."""
    if not locale:
        return DEFAULT_LOCALE
    base = str(locale).strip().lower().replace("_", "-").split("-")[0]
    return base if base in SUPPORTED_LOCALES else DEFAULT_LOCALE


def tr(table: dict[str, dict[str, str]], locale: str | None, key: str, **kwargs) -> str:
    """Text `key` in `locale`, falling back to English, then to the key itself.

    `kwargs` are applied with `str.format`; a table entry without placeholders is returned
    as is.
    """
    loc = norm_locale(locale)
    text = table.get(loc, {}).get(key)
    if text is None:
        text = table.get(DEFAULT_LOCALE, {}).get(key, key)
    return text.format(**kwargs) if kwargs else text
