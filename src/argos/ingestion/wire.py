"""Shared wire-format parsing helpers for the CLOB REST and WebSocket adapters.

``docs/STATUS.md`` records that duplicated normalization logic is a defect
class this repository has already been bitten by twice independently (the
``Decimal`` trailing-zero and negative-zero cases across
``argos.domain.observation`` and ``argos.domain.orderbook``). The millisecond
wire timestamp is the same shape of hazard for a different field: both the
CLOB REST `/book` response and the CLOB WebSocket market-channel frames
(``docs/research/m2-clob-rest-book.md``, ``docs/research/m2-clob-websocket.md``)
carry an identical ``timestamp`` string, so the parser lives here, once, and
both :mod:`argos.ingestion.clob_book` and :mod:`argos.ingestion.clob_price_change`
import it rather than each defining their own copy that could silently drift
apart.

This module holds no domain knowledge beyond "how do I read this one wire
shape" — no envelope, no rejection ledger, no store. Callers decide what a
parse failure means for their own record.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final

_TIMESTAMP_MS_PATTERN: Final = re.compile(r"-?[0-9]+")


def parse_event_time(raw: Any) -> tuple[datetime | None, str | None]:
    """Parse the wire `timestamp` (milliseconds-since-epoch, as a string).

    Never raises and never substitutes the current time
    (``.claude/rules/data-integrity.md``). Returns ``(event_time, None)`` when
    parseable, ``(None, event_time_raw)`` when the source sent something that
    did not parse, or ``(None, None)`` when nothing was sent at all —
    matching exactly the three-way distinction
    :func:`argos.domain.observation.build_observation_envelope` infers
    ``event_time_status`` from.

    An empty string is folded into "nothing sent" rather than "unparseable":
    ``ObservationEnvelopeV1.event_time_raw`` requires at least one character
    (it is part of the observation identity and a bare empty string carries
    no diagnostic content to preserve), so there is no way to represent an
    empty wire value as a *distinguishable* unparseable case, and treating it
    as equivalent to "no timestamp offered" is the closest honest reading.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        # Both source endpoints' own evidence is that this field is always a
        # string; a non-string value is a schema surprise worth preserving
        # for diagnosis, not worth guessing a conversion for.
        return None, repr(raw)
    if raw == "":
        return None, None
    if not _TIMESTAMP_MS_PATTERN.fullmatch(raw):
        return None, raw
    try:
        milliseconds = int(raw)
        seconds, millis = divmod(milliseconds, 1000)
        event_time = datetime.fromtimestamp(seconds, tz=UTC) + timedelta(milliseconds=millis)
    except (ValueError, OverflowError, OSError):
        return None, raw
    return event_time, None
