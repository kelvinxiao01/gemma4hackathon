"""Small, testable policy decisions for the LiveKit outbound call lifecycle."""

from __future__ import annotations

AMD_TERMINAL_OUTCOMES = {
    "machine-vm": "voicemail",
    "machine-unavailable": "unavailable",
    "machine-ivr": "ivr",
}


def outcome_for_amd_category(category: str) -> str | None:
    """Return a terminal outcome, or ``None`` when conversation may begin."""

    if category in {"human", "uncertain"}:
        return None
    try:
        return AMD_TERMINAL_OUTCOMES[category]
    except KeyError as exc:
        raise ValueError(f"unknown AMD category: {category}") from exc
