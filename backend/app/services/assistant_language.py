"""Code-owned output defaults, not a locale inferred from a website."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DEFAULT = "Simplified Chinese (zh-CN)"
_TIMEZONE_LANGUAGES = {
    "Asia/Shanghai": _DEFAULT,
    "Asia/Chongqing": _DEFAULT,
    "Asia/Harbin": _DEFAULT,
    "Asia/Urumqi": _DEFAULT,
    "Asia/Taipei": "Traditional Chinese (zh-TW)",
    "Asia/Hong_Kong": "Traditional Chinese (zh-TW)",
    "Asia/Macau": "Traditional Chinese (zh-TW)",
    "Asia/Tokyo": "Japanese (ja)",
    "Asia/Seoul": "Korean (ko)",
    "Europe/London": "English (en)",
    "America/New_York": "English (en)",
    "America/Chicago": "English (en)",
    "America/Denver": "English (en)",
    "America/Los_Angeles": "English (en)",
    "Europe/Paris": "French (fr)",
    "Europe/Berlin": "German (de)",
}


def fallback_language(installation_timezone: str) -> str:
    try:
        zone = ZoneInfo(installation_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return _DEFAULT
    # Unmapped/multilingual zones (including Singapore) retain the existing default.
    return _TIMEZONE_LANGUAGES.get(zone.key, _DEFAULT)


def output_language_instruction(installation_timezone: str) -> str:
    return (
        f"Output language default: {fallback_language(installation_timezone)}, derived from installation timezone. "
        "Follow explicit human language requests first, otherwise actual human message language, "
        "otherwise this default. Synthetic takeover instructions, site content/labels/timezones, "
        "logs, tools and assistant output are not language preferences. "
        "Applies to final replies, UI-visible tool titles, "
        "proposal explanations and submit_takeover_conclusion."
    )
