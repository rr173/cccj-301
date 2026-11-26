"""Planner unit tests, including a brute-force cross-check of the DP."""
from __future__ import annotations

import itertools
import random

from app.model import Window
from app.network import Network, Segment, plan_itinerary


def seg(sid, a, b, minutes, windows, accessible=True):
    return Segment(
        id=sid,
        from_stop=a,
        to_stop=b,
        minutes=minutes,
        windows=tuple(Window(open_min=o, close_min=c) for o, c in windows),
        wheelchair_accessible=accessible,
    )


# ---------------------------------------------------------------- reference
def naive_plan(network, origin, destination, depart, arrive_by, max_wait):
    """Straightforward exhaustive search; returns the best key or None."""
    if origin == destination:
        return (depart, 0, (), ())

    best = None

    def dfs(stop, ready, key):
        nonlocal best
        arrival, count, ids, deps = key
        if arrival > arrive_by:
            return
        if stop == destination:
            if best is None or key < best:
                best = key
            return
        for s in network.segments.values():
            if not s.wheelchair_accessible or s.from_stop != stop:
                continue
            for d in range(ready, min(ready + max_wait, arrive_by - s.minutes) + 1):
                if not network.traversal_ok(s, d):
                    continue
                dfs(
                    s.to_stop,
                    d + s.minutes,
                    (d + s.minutes, count + 1, ids + (s.id,), deps + (d,)),
                )

    dfs(origin, depart, (depart, 0, (), ()))
    return best


def key_of(legs):
    if legs is None:
        return None
    if not legs:
        return ()
    return (
        legs[-1].arrive_min,
        len(legs),
        tuple(leg.segment_id for leg in legs),
        tuple(leg.depart_min for leg in legs),
    )


def check(network, origin, destination, depart, arrive_by, max_wait):
    got = key_of(plan_itinerary(network, origin, destination, depart, arrive_by, max_wait))
    expected = naive_plan(network, origin, destination, depart, arrive_by, max_wait)
    assert got == expected, (got, expected)


# ---------------------------------------------------------------- fixed cases
def test_earliest_arrival_beats_fewer_legs():
    network = Network.build(
        [
            seg("DIRECT", "A", "B", 30, [(0, 100)]),          # 1 leg, arrives 30
            seg("X1", "A", "X", 1, [(0, 100)]),               # 2 legs, arrives 3
            seg("X2", "X", "B", 2, [(0, 100)]),
        ],
        [],
    )
    check(network, "A", "B", 0, 100, 10)
    legs = plan_itinerary(network, "A", "B", 0, 100, 10)
    assert [leg.segment_id for leg in legs] == ["X1", "X2"]
    assert legs[-1].arrive_min == 3


def test_fewest_legs_breaks_arrival_ties():
    network = Network.build(
        [
            seg("DIRECT", "A", "B", 10, [(0, 100)]),
            seg("M1", "A", "X", 4, [(0, 100)]),
            seg("M2", "X", "B", 6, [(0, 100)]),
        ],
        [],
    )
    check(network, "A", "B", 0, 100, 10)
    assert [leg.segment_id for leg in plan_itinerary(network, "A", "B", 0, 100, 10)] == [
        "DIRECT"
    ]


def test_segment_id_lexicographic_tiebreak():
    network = Network.build(
        [
            seg("B", "A", "D", 10, [(0, 100)]),
            seg("A", "A", "D", 10, [(0, 100)]),
        ],
        [],
    )
    check(network, "A", "D", 0, 100, 10)
    assert [leg.segment_id for leg in plan_itinerary(network, "A", "D", 0, 100, 10)] == [
        "A"
    ]


def test_departure_minute_lexicographic_tiebreak():
    # Two identical parallel segments deliver the same arrival and leg-count
    # only if they depart at the same time; the id tie-break then decides.
    # For departure tie-break, use one segment with a broad window: the
    # planner should leave as early as allowed (smallest departure tuple).
    network = Network.build([seg("S", "A", "B", 10, [(50, 100)])], [])
    check(network, "A", "B", 50, 100, 10)
    legs = plan_itinerary(network, "A", "B", 50, 100, 10)
    assert legs[0].depart_min == 50


def test_origin_wait_limit():
    network = Network.build([seg("S", "A", "B", 5, [(100, 200)])], [])
    check(network, "A", "B", 50, 200, 20)
    assert plan_itinerary(network, "A", "B", 50, 200, 20) is None
    assert plan_itinerary(network, "A", "B", 50, 200, 50) is not None


def test_transfer_wait_limit_changes_route():
    # Route A reaches the transfer earlier but needs a 6-minute wait; route B
    # arrives later with only a 2-minute wait.  With a 5-minute cap only B can
    # connect; with a 10-minute cap A wins on arrival time.
    network = Network.build(
        [
            seg("A1", "O", "XA", 5, [(100, 105)]),   # only departure is 100, reaches XA 105
            seg("A2", "XA", "D", 2, [(111, 200)]),   # departs 111 (6 min wait), arrives 113
            seg("B1", "O", "XB", 8, [(100, 108)]),   # only departure is 100, reaches XB 108
            seg("B2", "XB", "D", 4, [(110, 200)]),   # departs 110 (2 min wait), arrives 114
        ],
        [],
    )
    check(network, "O", "D", 100, 200, 5)
    legs = plan_itinerary(network, "O", "D", 100, 200, 5)
    assert [leg.segment_id for leg in legs] == ["B1", "B2"]
    assert legs[-1].arrive_min == 114
    # With a larger wait budget the earlier transfer route becomes valid and
    # wins on arrival time.
    check(network, "O", "D", 100, 200, 10)
    legs = plan_itinerary(network, "O", "D", 100, 200, 10)
    assert [leg.segment_id for leg in legs] == ["A1", "A2"]
    assert legs[-1].arrive_min == 113


def test_half_open_windows_and_closure_overlap():
    network = Network.build([seg("S", "A", "B", 10, [(0, 10)])], [])
    assert network.traversal_ok(network.segments["S"], 0)  # ends exactly at close
    assert not network.traversal_ok(network.segments["S"], 1)  # would end at 11
    # [0,10) vs closure [10,12): no overlap.
    network = Network.build([seg("S", "A", "B", 10, [(0, 10)])], [("S", 10, 12)])
    assert network.traversal_ok(network.segments["S"], 0)
    # [0,10) vs closure [9,12): overlaps.
    network = Network.build([seg("S", "A", "B", 10, [(0, 10)])], [("S", 9, 12)])
    assert not network.traversal_ok(network.segments["S"], 0)


def test_traversal_cannot_span_a_window_gap():
    # Two windows that touch count as one continuous open period.
    network = Network.build([seg("S", "A", "B", 10, [(0, 5), (5, 20)])], [])
    assert network.traversal_ok(network.segments["S"], 0)   # ends exactly at 10
    assert network.traversal_ok(network.segments["S"], 10)  # ends exactly at 20
    # With a real gap, a traversal must fit inside a single window.
    network = Network.build([seg("S", "A", "B", 10, [(0, 8), (12, 22)])], [])
    assert not network.traversal_ok(network.segments["S"], 4)   # [4,14) spans gap
    assert network.traversal_ok(network.segments["S"], 12)      # [12,22) fits



def test_inaccessible_segments_excluded():
    network = Network.build(
        [
            seg("FAST", "A", "B", 1, [(0, 100)], accessible=False),
            seg("SLOW", "A", "B", 50, [(0, 100)]),
        ],
        [],
    )
    check(network, "A", "B", 0, 100, 10)
    assert [leg.segment_id for leg in plan_itinerary(network, "A", "B", 0, 100, 10)] == [
        "SLOW"
    ]


def test_random_networks_match_brute_force():
    rng = random.Random(20261126)
    for _ in range(250):
        stops = ["A", "B", "C", "D", "E"]
        segments = []
        for i in range(rng.randint(1, 9)):
            a, b = rng.sample(stops, 2)
            windows = []
            for _ in range(rng.randint(1, 2)):
                o = rng.randint(0, 20)
                windows.append((o, o + rng.randint(1, 15)))
            windows.sort()
            segments.append(
                seg(
                    f"S{i}",
                    a,
                    b,
                    rng.randint(1, 7),
                    windows,
                    accessible=rng.random() > 0.15,
                )
            )
        closures = []
        for _ in range(rng.randint(0, 3)):
            if not segments:
                break
            s = rng.choice(segments)
            c = rng.randint(0, 30)
            closures.append((s.id, c, c + rng.randint(1, 10)))
        network = Network.build(segments, closures)
        origin, destination = rng.sample(stops, 2)
        depart = rng.randint(0, 25)
        arrive_by = depart + rng.randint(0, 25)
        max_wait = rng.randint(1, 15)
        check(network, origin, destination, depart, arrive_by, max_wait)


def test_origin_equals_destination():
    network = Network.build([seg("S", "A", "B", 5, [(0, 100)])], [])
    assert plan_itinerary(network, "A", "A", 10, 20, 5) == []
