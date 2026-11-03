"""Operator-controlled, fail-safe NFL BUY publication hold.

This is a containment control, not a sportsbook-odds model enhancement.
It affects customer-facing NFL BUY publication and alerts only.  It does
not alter Elo forecasts, captured prospective observations, or other sports.

Set PARALLAX_NFL_BUY_PUBLICATION_HOLD=1 in the existing runner environment
as part of an explicitly approved production change.  Any nonempty value
other than 0/false/no/off activates the hold (including misspellings).
"""

from __future__ import annotations

import os
from collections.abc import Mapping


ENV_NAME = "PARALLAX_NFL_BUY_PUBLICATION_HOLD"
_DISABLED_VALUES = frozenset(("", "0", "false", "no", "off"))


def nfl_buy_publication_hold_active(environ: Mapping[str, str] | None = None) -> bool:
    """Return True unless the operator explicitly left the hold disabled."""
    values = os.environ if environ is None else environ
    value = str(values.get(ENV_NAME, "")).strip().casefold()
    return value not in _DISABLED_VALUES
