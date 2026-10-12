"""`for:` triggers: did the new value hold long enough to fire?

A state or numeric-state trigger with `for:` fires only if the value holds for that
long. The change happened at some unknown moment t in the downtime (start, end],
so from what the entity does after startup:

* it changed again at c with c < start + for: no t could have held long enough → ruled out;
* it still has the value at end + for: it held → it would have fired;
* anything in between can't be told.

"Changed again" means a change *from a real value*: an integration that reports late
(unavailable → value after startup) isn't the value changing. And if that first change
after startup is *from the pre-downtime value*, the value didn't change while HA was down
at all: HA saw the change itself → ruled out.

"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import State
from homeassistant.helpers import config_validation as cv

from .const import UNKNOWN_STATES

RULED_OUT = "ruled_out"  # changed again too soon: couldn't have fired
HELD = "held"  # held long enough: would have fired
CHANGED = "changed"  # changed again, but too late to rule out
WATCH = "watch"  # still holding; decide at the deadline


def for_seconds(value: Any) -> float | None:
    """`for:` as seconds; None for templates or anything unparseable."""
    if value is None:
        return None
    try:
        return cv.positive_time_period(value).total_seconds()
    except Exception:  # noqa: BLE001  (templates, odd values)
        return None


def current_value(state: State | None, attribute: str | None) -> Any:
    if state is None:
        return None
    return state.attributes.get(attribute) if attribute else state.state


def changed_at(state: State, attribute: str | None) -> datetime:
    """When the value last changed (attributes only move last_updated)."""
    return state.last_updated if attribute else state.last_changed


def first_real_change(
    changes: list[tuple[State | None, State | None]], attribute: str | None
) -> datetime | None:
    """When the value first changed from a real value (not unknown/unavailable)."""
    for old, new in changes:
        if old is None or new is None:
            continue
        before, after = current_value(old, attribute), current_value(new, attribute)
        if str(before) in UNKNOWN_STATES or str(before) == str(after):
            continue
        return changed_at(new, attribute)
    return None


def decide(changed: datetime, hold: float, start: datetime) -> str:
    """It changed again at `changed`: too soon to have fired, or can't tell."""
    return RULED_OUT if changed < start + timedelta(seconds=hold) else CHANGED


def verdict(
    state: State | None,
    attribute: str | None,
    after: Any,
    hold: float,
    start: datetime,
    end: datetime,
    now: datetime,
    changes: list[tuple[State | None, State | None]] = (),
    before: Any = None,
) -> tuple[str, datetime | None]:
    """(result, moment): the change that decided it, or the deadline to wait for.

    `changes`: (old, new) states of the entity since Home Assistant started;
    `before`: its value before the downtime.
    """
    if (moment := first_real_change(list(changes), attribute)) is not None:
        old = next(current_value(o, attribute) for o, n in changes if o is not None and n is not None
                   and changed_at(n, attribute) == moment)
        if before is not None and str(old) == str(before):
            return RULED_OUT, moment  # it still had the pre-downtime value after startup
        return decide(moment, hold, start), moment
    if state is None or str(current_value(state, attribute)) != str(after):
        moment = changed_at(state, attribute) if state is not None else now
        return decide(moment, hold, start), moment
    deadline = end + timedelta(seconds=hold)
    return (HELD if now >= deadline else WATCH), deadline
