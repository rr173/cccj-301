"""Network model: directed accessible segments, opening windows, closures,
and the multi-criteria itinerary planner.
"""
from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass

from .model import Leg, Window


@dataclass(frozen=True)
class Segment:
    id: str
    from_stop: str
    to_stop: str
    minutes: int
    windows: tuple[Window, ...]
    wheelchair_accessible: bool = True


def merge_windows(windows: tuple[Window, ...]) -> tuple[Window, ...]:
    """Coalesce overlapping/adjacent windows into maximal open intervals.

    [0,5) followed by [5,20) is one continuous open period [0,20); without
    this a traversal spanning the seam would be wrongly rejected.
    """
    ordered = sorted(windows, key=lambda w: (w.open_min, w.close_min))
    merged: list[Window] = []
    for window in ordered:
        if merged and window.open_min <= merged[-1].close_min:
            last = merged[-1]
            merged[-1] = Window(
                open_min=last.open_min,
                close_min=max(last.close_min, window.close_min),
            )
        else:
            merged.append(window)
    return tuple(merged)


@dataclass(frozen=True)
class Network:
    segments: dict[str, Segment]
    closures: dict[str, tuple[tuple[int, int], ...]]
    outgoing: dict[str, tuple[Segment, ...]]
    # stops from which the destination can be reached using accessible segments
    can_reach: frozenset[str]

    @classmethod
    def build(
        cls,
        segments: list[Segment],
        closures: list[tuple[str, int, int]],
    ) -> "Network":
        by_id = {s.id: s for s in segments}
        closure_map: dict[str, list[tuple[int, int]]] = {}
        for segment_id, start, end in closures:
            closure_map.setdefault(segment_id, []).append((start, end))

        accessible = [s for s in segments if s.wheelchair_accessible]
        outgoing_lists: dict[str, list[Segment]] = {}
        for seg in accessible:
            outgoing_lists.setdefault(seg.from_stop, []).append(seg)
        outgoing = {
            stop: tuple(sorted(segs, key=lambda s: s.id))
            for stop, segs in outgoing_lists.items()
        }
        return cls(
            segments=by_id,
            closures={k: tuple(v) for k, v in closure_map.items()},
            outgoing=outgoing,
            can_reach=frozenset(),  # replaced in with_destination_reachability
        )

    def with_destination_reachability(self, destination: str) -> "Network":
        accessible = [s for s in self.segments.values() if s.wheelchair_accessible]
        reverse: dict[str, set[str]] = {}
        for seg in accessible:
            reverse.setdefault(seg.to_stop, set()).add(seg.from_stop)
        reachable = {destination}
        todo = [destination]
        while todo:
            stop = todo.pop()
            for prev in reverse.get(stop, ()):
                if prev not in reachable:
                    reachable.add(prev)
                    todo.append(prev)
        return Network(
            segments=self.segments,
            closures=self.closures,
            outgoing=self.outgoing,
            can_reach=frozenset(reachable),
        )

    # ---------------------------------------------------------------- checks
    def segment(self, segment_id: str) -> Segment | None:
        return self.segments.get(segment_id)

    def traversal_ok(self, segment: Segment, depart_min: int) -> bool:
        """True iff [depart, depart+minutes) is fully open and closure-free.

        Windows are [open, close): a traversal may end exactly at close.
        Closures are [start, end): any non-empty overlap blocks the traversal.
        """
        if depart_min < 0:
            return False
        arrive = depart_min + segment.minutes
        windows = merge_windows(segment.windows)
        if not any(w.open_min <= depart_min and arrive <= w.close_min for w in windows):
            return False
        for start, end in self.closures.get(segment.id, ()):
            if depart_min < end and start < arrive:
                return False
        return True

    def departure_choices(self, segment: Segment, lo: int, hi: int) -> list[int]:
        """All integer departure minutes in [lo, hi] that are traversable."""
        if lo > hi:
            return []
        choices: list[int] = []
        intervals: list[tuple[int, int]] = []
        for window in merge_windows(segment.windows):
            a = max(lo, window.open_min)
            b = min(hi, window.close_min - segment.minutes)
            if a <= b:
                intervals.append((a, b))
        if not intervals:
            return []
        # d is blocked by closure [s,e) iff [d,d+min) overlaps it, i.e.
        # s - minutes + 1 <= d <= e - 1.
        blocked = sorted(
            (max(lo, s - segment.minutes + 1), min(hi, e - 1))
            for s, e in self.closures.get(segment.id, ())
            if max(lo, s - segment.minutes + 1) <= min(hi, e - 1)
        )
        for a, b in intervals:
            pieces = [(a, b)]
            for c, d in blocked:
                next_pieces: list[tuple[int, int]] = []
                for x, y in pieces:
                    if d < x or c > y:
                        next_pieces.append((x, y))
                    else:
                        if x < c:
                            next_pieces.append((x, c - 1))
                        if d < y:
                            next_pieces.append((d + 1, y))
                pieces = next_pieces
            for x, y in pieces:
                choices.extend(range(x, y + 1))
        return sorted(choices)


# -------------------------------------------------------------------- planner
DecisionKey = tuple[int, int, tuple[str, ...], tuple[int, ...]]
"""(arrival, leg count, segment-id sequence, leg departure sequence).

The tuple order is exactly the required priority: earliest arrival, then
fewest legs, then lexicographically smallest segment-id sequence, then
lexicographically smallest departure-minute sequence.
"""

_MISSING = object()


class _History:
    """Step function over minutes, built while scanning time backwards.

    Entries record the largest minute at which a value starts.  Negated
    minutes stay ascending as the scan moves backwards, so lookup is a bisect.
    """

    __slots__ = ("neg_minutes", "values")

    def __init__(self) -> None:
        self.neg_minutes: list[int] = []
        self.values: list[DecisionKey | None] = []

    def set(self, minute: int, value: DecisionKey | None) -> None:
        if self.values and self.values[-1] == value:
            return
        if self.neg_minutes and self.neg_minutes[-1] == -minute:
            self.neg_minutes.pop()
            self.values.pop()
        self.neg_minutes.append(-minute)
        self.values.append(value)

    def at(self, minute: int) -> DecisionKey | None:
        # An entry recorded at scan minute m covers lookups in
        # (previous_smaller_entry_minute, m]; so pick the smallest recorded
        # minute that is >= the lookup minute.
        idx = bisect.bisect_right(self.neg_minutes, -minute) - 1
        if idx < 0:
            return None
        return self.values[idx]


def plan_itinerary(
    network: Network,
    origin: str,
    destination: str,
    depart_min: int,
    arrive_by_min: int,
    max_wait_min: int,
) -> list[Leg] | None:
    """Best wheelchair-accessible itinerary, or None if there is no solution.

    Waiting is only allowed immediately before boarding a segment, and each
    wait (including the one at the origin) is at most ``max_wait_min``.

    Backward dynamic program over (stop, minute) states: ``best[(stop, m)]``
    is the optimal continuation key when standing at ``stop`` at minute ``m``.
    For each segment the valid boardings at ``m`` are the feasible departures
    in ``[m, m + max_wait]``; as ``m`` moves backwards that window slides, so
    each segment keeps its candidate boardings in a monotonic deque keyed by
    decision rank.  A stop is re-settled only at the minutes when one of its
    segments gains or loses a candidate, and each stop's value is stored as a
    step function rather than one entry per minute.  Runtime therefore ~
    (feasible boardings) x (candidates per segment), not (minutes x stops).
    """
    if depart_min > arrive_by_min:
        return None
    if origin == destination:
        return []
    network = network.with_destination_reachability(destination)
    if origin not in network.can_reach:
        return None

    lo = depart_min
    hi = arrive_by_min
    waiting = max_wait_min

    # A segment is only worth expanding when both ends can still contribute:
    # its head must reach the destination and its tail must be able to reach
    # the head (checked transitively by the outer DP via can_reach).
    departures: dict[str, list[int]] = {}
    segments: list[Segment] = []
    for seg in network.segments.values():
        if not seg.wheelchair_accessible or seg.to_stop not in network.can_reach:
            continue
        allowed = network.departure_choices(seg, lo, hi - seg.minutes)
        if not allowed:
            continue
        segments.append(seg)
        departures[seg.id] = allowed

    if not segments:
        return None

    # Best value per stop, as a step function over minutes.
    history: dict[str, _History] = {}
    current: dict[str, DecisionKey | None] = {}

    def state_key(stop: str, minute: int) -> DecisionKey | None:
        if minute > hi:
            return None
        if stop == destination:
            return (minute, 0, (), ())
        hist = history.get(stop)
        return None if hist is None else hist.at(minute)

    # Per-segment candidate boardings.  For minute m the valid boardings are
    # the feasible departures in [m, m + waiting].  Candidates are inserted in
    # descending departure order and expire in the same order, so an ordinary
    # queue suffices; a monotonic deque over the composite rank then keeps the
    # best live candidate at the front in O(1) amortised time.
    queues: dict[str, deque] = {seg.id: deque() for seg in segments}

    # A stop can only change at a minute when some candidate opens at it, or
    # when the oldest candidate at one of its segments becomes too far in the
    # future (expiry).  Between those minutes nothing changes.
    openings_at: dict[int, list[Segment]] = {}
    expiries_at: dict[int, list[Segment]] = {}
    events: set[int] = {lo}
    for seg in segments:
        for b in departures[seg.id]:
            openings_at.setdefault(b, []).append(seg)
            events.add(b)
            expiry = b - waiting - 1
            if expiry >= lo:
                expiries_at.setdefault(expiry, []).append(seg)
                events.add(expiry)

    def current_boarding(seg: Segment, minute: int):
        queue = queues[seg.id]
        limit = minute + waiting
        while queue and queue[0][0] > limit:
            queue.popleft()  # too far in the future, forever
        if not queue:
            return None
        return queue[0][0], queue[0][2]

    def settle(stop: str, minute: int) -> None:
        local: DecisionKey | None = None
        for seg in network.outgoing.get(stop, ()):
            if seg.id not in queues:
                continue
            found = current_boarding(seg, minute)
            if found is None:
                continue
            boarding, suffix = found
            candidate: DecisionKey = (
                suffix[0],
                suffix[1] + 1,
                (seg.id,) + suffix[2],
                (boarding,) + suffix[3],
            )
            if local is None or candidate < local:
                local = candidate
        if current.get(stop, _MISSING) != local:
            current[stop] = local
            history.setdefault(stop, _History()).set(minute, local)

    for minute in sorted(events, reverse=True):
        dirty: set[str] = set()
        for seg in expiries_at.get(minute, ()):
            dirty.add(seg.from_stop)
        for seg in openings_at.get(minute, ()):
            suffix = state_key(seg.to_stop, minute + seg.minutes)
            if suffix is None:
                continue
            rank = (suffix[0], suffix[1], suffix[2], minute, suffix[3])
            queue = queues[seg.id]
            while queue and rank <= queue[-1][1]:
                queue.pop()
            queue.append((minute, rank, suffix))
            dirty.add(seg.from_stop)
        for stop in dirty:
            settle(stop, minute)

    # Values are recorded only at event minutes; an entry at minute m covers
    # lookups in (previous event minute, m].  Every stop that has a value must
    # also have an entry covering the origin's departure minute, so re-settle
    # them one last time at ``lo``.
    for stop in list(current):
        settle(stop, lo)

    origin_key = state_key(origin, depart_min)
    if origin_key is None:
        return None

    legs: list[Leg] = []
    stop, ready = origin, depart_min
    while stop != destination:
        key = state_key(stop, ready)
        if key is None or not key[2]:
            return None
        seg_id = key[2][0]
        boarding = key[3][0]
        seg = network.segments[seg_id]
        arrive = boarding + seg.minutes
        legs.append(
            Leg(
                segment_id=seg_id,
                from_stop=seg.from_stop,
                to_stop=seg.to_stop,
                depart_min=boarding,
                arrive_min=arrive,
            )
        )
        stop, ready = seg.to_stop, arrive
    return legs


def itinerary_still_valid(
    network: Network,
    legs: list[Leg],
    anchor_stop: str,
    anchor_min: int,
    arrive_by_min: int,
    max_wait_min: int,
) -> bool:
    """Can the existing unwalked legs keep *all* their scheduled departures?

    Used after a closure change or an actual-arrival update: if yes, the tail
    is preserved verbatim; if no, the tail is re-planned from the anchor.
    """
    stop = anchor_stop
    ready = anchor_min
    for leg in legs:
        seg = network.segment(leg.segment_id)
        if seg is None or not seg.wheelchair_accessible:
            return False
        if seg.from_stop != stop or seg.to_stop != leg.to_stop:
            return False
        depart = leg.depart_min
        if depart < ready:
            return False
        if depart - ready > max_wait_min:
            return False
        if not network.traversal_ok(seg, depart):
            return False
        arrive = depart + seg.minutes
        if arrive > arrive_by_min:
            return False
        stop, ready = seg.to_stop, arrive
    return True
