"""Read facts shared only within one synchronous takeover transaction phase."""

from dataclasses import dataclass, field


@dataclass
class TakeoverPhaseCache:
    """Discard before any await, commit, or authority/configuration mutation.

    Never use this cache for live execution/import authorization. It contains
    plain facts, not ORM objects, and belongs to one session and policy view.
    """

    run_sources: dict[int, bool] = field(default_factory=dict)
    waiting_sources: dict[int, bool] = field(default_factory=dict)
    site_allowed: dict[int, bool] = field(default_factory=dict)
    site_uncertain: dict[int, bool] = field(default_factory=dict)
    site_clear: dict[int, bool] = field(default_factory=dict)
