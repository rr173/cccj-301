"""Core end-to-end flows for shuttle-run booking and live boarding.

Covers: run registration on accessible segments, whole-order confirmation and
waitlisting, interval-scoped space reuse, two-leg transfers, idempotency,
stop-by-stop staff events, transfer verification, no-show voiding, waitlist
promotion in application order, restart persistence, and concurrency safety.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


# --------------------------------------------------------------------- setup
@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "shuttle.db")
    with TestClient(app) as c:
        yield c


def add_segment(client, sid, a, b, minutes=5, accessible=True):
    r = client.post(
        "/segments",
        json={
            "id": sid,
            "from_stop": a,
            "to_stop": b,
            "minutes": minutes,
            "windows": [{"open_min": 0, "close_min": 1440}],
            "wheelchair_accessible": accessible,
        },
    )
    assert r.status_code == 201, r.text


def make_run(client, stops, capacity=2, base=100):
    """stops: list of (name, arrive, depart); falls back to a simple schedule."""
    payload_stops = []
    for i, item in enumerate(stops):
        if isinstance(item, tuple):
            name, arrive, depart = item
        else:
            arrive, depart = base + i * 10, base + i * 10 + 2
            name = item
        payload_stops.append(
            {"stop": name, "arrive_min": arrive, "depart_min": depart}
        )
    r = client.post(
        "/shuttle/runs",
        json={"stops": payload_stops, "wheelchair_capacity": capacity},
    )
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def book(client, request_id, legs, count=1, group_name=None):
    return client.post(
        "/bookings",
        json={
            "request_id": request_id,
            "group_name": group_name,
            "wheelchair_count": count,
            "legs": legs,
        },
    )


def arrive(client, run_id, index, minute):
    r = client.post(
        f"/shuttle/runs/{run_id}/stops/{index}/arrival",
        json={"actual_min": minute},
    )
    assert r.status_code == 200, r.text
    return r.json()


def board(client, run_id, index, booking_id, minute):
    r = client.post(
        f"/shuttle/runs/{run_id}/stops/{index}/boarding",
        json={"booking_id": booking_id, "actual_min": minute},
    )
    assert r.status_code == 200, r.text
    return r.json()


def depart(client, run_id, index, minute):
    r = client.post(
        f"/shuttle/runs/{run_id}/stops/{index}/departure",
        json={"actual_min": minute},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- registration
def test_run_must_follow_accessible_segments(client):
    add_segment(client, "S1", "Gate", "Plaza")
    r = client.post(
        "/shuttle/runs",
        json={
            "stops": [
                {"stop": "Gate", "arrive_min": 100, "depart_min": 101},
                {"stop": "Nowhere", "arrive_min": 110, "depart_min": 111},
            ],
            "wheelchair_capacity": 2,
        },
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "no_accessible_path"


def test_run_rejects_inaccessible_only_path(client):
    add_segment(client, "S1", "Gate", "Plaza", accessible=False)
    r = client.post(
        "/shuttle/runs",
        json={
            "stops": [
                {"stop": "Gate", "arrive_min": 100, "depart_min": 101},
                {"stop": "Plaza", "arrive_min": 110, "depart_min": 111},
            ],
            "wheelchair_capacity": 2,
        },
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "no_accessible_path"


def test_planned_schedule_cannot_go_backwards(client):
    add_segment(client, "S1", "A", "B")
    add_segment(client, "S2", "B", "C")
    r = client.post(
        "/shuttle/runs",
        json={
            "stops": [
                {"stop": "A", "arrive_min": 100, "depart_min": 120},
                {"stop": "B", "arrive_min": 110, "depart_min": 112},
                {"stop": "C", "arrive_min": 130, "depart_min": 130},
            ],
            "wheelchair_capacity": 2,
        },
    )
    assert r.status_code == 422


def test_same_stop_name_can_recur_and_is_indexed(client):
    add_segment(client, "S1", "Loop", "Loop")
    rid = make_run(
        client,
        [("Loop", 100, 101), ("Loop", 110, 111), ("Loop", 120, 120)],
    )
    body = client.get(f"/shuttle/runs/{rid}").json()
    assert [s["stop"] for s in body["stops"]] == ["Loop", "Loop", "Loop"]
    r = book(
        client,
        "loop-1",
        [{"run_id": rid, "board_index": 0, "alight_index": 2}],
    )
    assert r.status_code == 201
    assert r.json()["legs"][0]["board_index"] == 0


# ------------------------------------------------------------- confirmation
def _three_stop_run(client, capacity=2):
    add_segment(client, "S1", "A", "B")
    add_segment(client, "S2", "B", "C")
    return make_run(
        client,
        [("A", 100, 102), ("B", 110, 112), ("C", 120, 120)],
        capacity=capacity,
    )


def test_direct_booking_confirms_when_space_available(client):
    rid = _three_stop_run(client)
    r = book(
        client,
        "g1",
        [{"run_id": rid, "board_index": 0, "alight_index": 2}],
        count=2,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["legs"][0]["status"] == "reserved"
    run = client.get(f"/shuttle/runs/{rid}").json()
    # Two spaces held on both covered intervals.
    assert [i["occupied_wheelchairs"] for i in run["intervals"]] == [2, 2]


def test_whole_order_waits_when_any_leg_lacks_space(client):
    rid = _three_stop_run(client, capacity=2)
    # Fill both spaces on the first interval only.
    ok = book(
        client,
        "full",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
        count=2,
    )
    assert ok.json()["status"] == "confirmed"
    waiting = book(
        client,
        "late",
        [{"run_id": rid, "board_index": 0, "alight_index": 2}],
    )
    assert waiting.status_code == 201
    body = waiting.json()
    assert body["status"] == "waiting"
    assert all(leg["status"] == "void" for leg in body["legs"])
    # A waitlist order holds no space: interval 1->2 stays empty.
    run = client.get(f"/shuttle/runs/{rid}").json()
    assert run["intervals"][1]["occupied_wheelchairs"] == 0
    assert body["waitlist_reason"] is not None


def test_space_is_reused_after_alighting(client):
    rid = _three_stop_run(client, capacity=1)
    first = book(
        client,
        "g1",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    ).json()
    # Second group rides only the freed interval: confirmable immediately even
    # though g1 is still holding the first interval.
    second = book(
        client,
        "g2",
        [{"run_id": rid, "board_index": 1, "alight_index": 2}],
    )
    assert second.json()["status"] == "confirmed"
    run = client.get(f"/shuttle/runs/{rid}").json()
    assert [i["occupied_wheelchairs"] for i in run["intervals"]] == [1, 1]
    # A group covering both intervals cannot fit while g1 holds interval 0->1.
    third = book(
        client,
        "g3",
        [{"run_id": rid, "board_index": 0, "alight_index": 2}],
    )
    assert third.json()["status"] == "waiting"

    # Drive the run: g1 alights at B, freeing interval 0->1 for the next group.
    arrive(client, rid, 0, 100)
    board(client, rid, 0, first["booking_id"], 101)
    depart(client, rid, 0, 102)
    # g3's boarding stop (index 0) has now departed: the waitlisted order is
    # dead even though interval 0->1 just freed up.
    assert (
        client.get(f"/bookings/{third.json()['booking_id']}").json()["status"]
        == "void"
    )
    arrive(client, rid, 1, 110)  # g1 alights; g2 boards at B
    manifest = client.get(f"/shuttle/runs/{rid}/manifest").json()
    assert [e["booking_id"] for e in manifest["history"]] == [first["booking_id"]]
    board(client, rid, 1, second.json()["booking_id"], 111)
    depart(client, rid, 1, 112)
    arrive(client, rid, 2, 120)  # g2 alights; run ends
    assert (
        client.get(f"/bookings/{second.json()['booking_id']}").json()["status"]
        == "completed"
    )


def test_departed_boarding_stop_cannot_be_booked(client):
    rid = _three_stop_run(client)
    arrive(client, rid, 0, 100)
    depart(client, rid, 0, 102)
    r = book(
        client,
        "late",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "departed"


# ------------------------------------------------------------- idempotency
def test_repeated_request_id_is_idempotent(client):
    rid = _three_stop_run(client)
    payload = {
        "request_id": "idem-1",
        "wheelchair_count": 1,
        "legs": [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    }
    first = client.post("/bookings", json=payload)
    assert first.status_code == 201
    replay = client.post("/bookings", json=payload)
    assert replay.status_code == 200
    assert replay.json()["booking_id"] == first.json()["booking_id"]


def test_request_id_reuse_with_different_content_is_rejected(client):
    rid = _three_stop_run(client)
    base = {
        "request_id": "idem-2",
        "wheelchair_count": 1,
        "legs": [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    }
    assert client.post("/bookings", json=base).status_code == 201
    changed = dict(base, wheelchair_count=2)
    r = client.post("/bookings", json=changed)
    assert r.status_code == 409
    assert r.json()["reason"] == "duplicate_request"
    # The original order is untouched.
    got = client.get("/bookings/by-request/idem-2").json()
    assert got["wheelchair_count"] == 1


# ------------------------------------------------------------- transfers
def _two_runs_transfer(client, capacity=2):
    # Run 1: A -> Hub -> P ; Run 2: Hub -> Q.
    add_segment(client, "R1a", "A", "Hub")
    add_segment(client, "R1b", "Hub", "P")
    add_segment(client, "R2a", "Hub", "Q")
    run1 = make_run(
        client,
        [("A", 100, 102), ("Hub", 110, 112), ("P", 120, 120)],
        capacity=capacity,
    )
    run2 = make_run(
        client,
        [("Hub", 115, 117), ("Q", 130, 130)],
        capacity=capacity,
    )
    return run1, run2


def test_two_leg_transfer_confirms_atomically(client):
    run1, run2 = _two_runs_transfer(client)
    r = book(
        client,
        "xfer-1",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run2, "board_index": 0, "alight_index": 1},
        ],
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "confirmed"
    assert [leg["status"] for leg in body["legs"]] == ["reserved", "reserved"]


def test_transfer_requires_same_stop_and_feasible_time(client):
    add_segment(client, "R1a", "A", "Hub")
    add_segment(client, "R2a", "Other", "Q")
    run1 = make_run(client, [("A", 100, 102), ("Hub", 110, 110)])
    run2 = make_run(client, [("Other", 115, 117), ("Q", 130, 130)])
    r = book(
        client,
        "xfer-bad-stop",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run2, "board_index": 0, "alight_index": 1},
        ],
    )
    assert r.status_code == 422

    # Same stop name but the connecting run leaves before run 1 arrives.
    add_segment(client, "R3a", "Hub", "Z")
    run3 = make_run(client, [("Hub", 105, 106), ("Z", 108, 108)])
    r = book(
        client,
        "xfer-bad-time",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run3, "board_index": 0, "alight_index": 1},
        ],
    )
    assert r.status_code == 422


def test_partial_capacity_never_confirms_half_a_transfer(client):
    run1, run2 = _two_runs_transfer(client, capacity=1)
    # Fill run 2's only space: the transfer order must wait entirely.
    blocker = book(
        client,
        "block",
        [{"run_id": run2, "board_index": 0, "alight_index": 1}],
    )
    assert blocker.json()["status"] == "confirmed"
    waiting = book(
        client,
        "xfer-wait",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run2, "board_index": 0, "alight_index": 1},
        ],
    )
    body = waiting.json()
    assert body["status"] == "waiting"
    # No half reservation exists on run 1.
    run1_view = client.get(f"/shuttle/runs/{run1}").json()
    assert all(i["occupied_wheelchairs"] == 0 for i in run1_view["intervals"])


def test_connecting_leg_boarding_stop_departed_is_rejected(client):
    run1, run2 = _two_runs_transfer(client)
    # The connecting run's Hub stop (index 0) leaves before any booking.
    arrive(client, run2, 0, 115)
    depart(client, run2, 0, 117)
    r = book(
        client,
        "xfer-dead-connection",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run2, "board_index": 0, "alight_index": 1},
        ],
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "departed"
    # The first leg alone is still bookable.
    ok = book(
        client,
        "first-only",
        [{"run_id": run1, "board_index": 0, "alight_index": 1}],
    )
    assert ok.status_code == 201


def test_connecting_boarding_requires_recorded_alighting(client):
    run1, run2 = _two_runs_transfer(client)
    order = book(
        client,
        "xfer-live",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run2, "board_index": 0, "alight_index": 1},
        ],
    ).json()
    bid = order["booking_id"]

    # Run 2 reaches Hub first; boarding without a run-1 alighting is refused.
    arrive(client, run2, 0, 115)
    r = client.post(
        f"/shuttle/runs/{run2}/stops/0/boarding",
        json={"booking_id": bid, "actual_min": 116},
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "transfer_not_verified"

    # Drive run 1 through the Hub.
    arrive(client, run1, 0, 100)
    board(client, run1, 0, bid, 101)
    depart(client, run1, 0, 102)
    arrive(client, run1, 1, 110)  # auto-alighting recorded at minute 110

    # Actual alighting (110) <= connecting boarding (116): now accepted.
    board(client, run2, 0, bid, 116)
    manifest = client.get(f"/shuttle/runs/{run2}/manifest").json()
    assert [e["booking_id"] for e in manifest["onboard"]] == [bid]


def test_transfer_rejected_when_alight_is_later_than_connecting_board(client):
    run1, run2 = _two_runs_transfer(client)
    order = book(
        client,
        "xfer-late",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run2, "board_index": 0, "alight_index": 1},
        ],
    ).json()
    bid = order["booking_id"]
    arrive(client, run1, 0, 100)
    board(client, run1, 0, bid, 101)
    depart(client, run1, 0, 102)
    arrive(client, run1, 1, 118)  # actual alight 118
    arrive(client, run2, 0, 115)
    r = client.post(
        f"/shuttle/runs/{run2}/stops/0/boarding",
        json={"booking_id": bid, "actual_min": 116},
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "transfer_not_verified"


# ------------------------------------------------------------- staff events
def test_event_order_and_monotone_actual_minutes(client):
    rid = _three_stop_run(client)
    # Arrival before the predecessor departed.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/1/arrival", json={"actual_min": 110}
    )
    assert r.status_code == 409 and r.json()["reason"] == "not_at_stop"

    arrive(client, rid, 0, 100)
    # Boarding before arrival is impossible (arrival done), but departure
    # before arrival on another stop fails.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/1/departure", json={"actual_min": 112}
    )
    assert r.status_code == 409
    # Time cannot move backwards within a run.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/0/departure", json={"actual_min": 99}
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "schedule_went_backwards"
    depart(client, rid, 0, 102)
    # No boarding after departure.
    order = book(
        client,
        "g",
        [{"run_id": rid, "board_index": 1, "alight_index": 2}],
    ).json()
    r = client.post(
        f"/shuttle/runs/{rid}/stops/0/boarding",
        json={"booking_id": order["booking_id"], "actual_min": 103},
    )
    assert r.status_code == 409 and r.json()["reason"] == "departed"


def test_repeated_events_are_rejected_and_illegal_ops_change_nothing(client):
    rid = _three_stop_run(client)
    arrive(client, rid, 0, 100)
    # A second arrival at the same stop is illegal.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/0/arrival", json={"actual_min": 100}
    )
    assert r.status_code == 409 and r.json()["reason"] == "illegal_event"
    depart(client, rid, 0, 102)
    # Re-departing a departed stop is illegal.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/0/departure", json={"actual_min": 103}
    )
    assert r.status_code == 409 and r.json()["reason"] == "illegal_event"
    # Stored actual minutes are the first, legal values.
    run = client.get(f"/shuttle/runs/{rid}").json()
    assert run["stops"][0]["actual_arrive_min"] == 100
    assert run["stops"][0]["actual_depart_min"] == 102


def test_boarding_is_whole_group_and_needs_a_reservation(client):
    rid = _three_stop_run(client)
    arrive(client, rid, 0, 100)
    # Unknown booking.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/0/boarding",
        json={"booking_id": 999, "actual_min": 101},
    )
    assert r.status_code == 404
    order = book(
        client,
        "g",
        [{"run_id": rid, "board_index": 1, "alight_index": 2}],
    ).json()
    # The reservation boards at B, not A.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/0/boarding",
        json={"booking_id": order["booking_id"], "actual_min": 101},
    )
    assert r.status_code == 409 and r.json()["reason"] == "not_boardable"
    # Double boarding at the right stop is rejected.
    depart(client, rid, 0, 102)
    arrive(client, rid, 1, 110)
    board(client, rid, 1, order["booking_id"], 111)
    r = client.post(
        f"/shuttle/runs/{rid}/stops/1/boarding",
        json={"booking_id": order["booking_id"], "actual_min": 111},
    )
    assert r.status_code == 409 and r.json()["reason"] == "already_boarded"


def test_last_stop_arrival_ends_run_and_alights_everyone(client):
    rid = _three_stop_run(client)
    order = book(
        client,
        "g",
        [{"run_id": rid, "board_index": 0, "alight_index": 2}],
    ).json()
    arrive(client, rid, 0, 100)
    board(client, rid, 0, order["booking_id"], 101)
    depart(client, rid, 0, 102)
    arrive(client, rid, 1, 110)
    depart(client, rid, 1, 112)
    end = arrive(client, rid, 2, 120)
    assert end["finished"] is True
    assert end["stops"][-1]["state"] == "ended"
    manifest = client.get(f"/shuttle/runs/{rid}/manifest").json()
    assert manifest["onboard"] == []
    assert [e["booking_id"] for e in manifest["history"]] == [order["booking_id"]]
    got = client.get(f"/bookings/{order['booking_id']}").json()
    assert got["status"] == "completed"
    # A departure at the last stop is illegal.
    r = client.post(
        f"/shuttle/runs/{rid}/stops/2/departure", json={"actual_min": 121}
    )
    assert r.status_code == 409 and r.json()["reason"] == "illegal_event"


# ------------------------------------------------------------- no-show
def test_no_show_voids_remaining_reservation_but_kept_onboard(client):
    # Capacity 1, run A->B->C. Group 1 rides A->B and connects nowhere; group 2
    # rides B->C. Group 3 books A->C but misses A.
    rid = _three_stop_run(client, capacity=1)
    g1 = book(
        client,
        "g1",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    ).json()
    g2 = book(
        client,
        "g2",
        [{"run_id": rid, "board_index": 1, "alight_index": 2}],
    ).json()
    g3 = book(
        client,
        "g3",
        [{"run_id": rid, "board_index": 0, "alight_index": 2}],
    )
    assert g3.json()["status"] == "waiting"

    arrive(client, rid, 0, 100)
    board(client, rid, 0, g1["booking_id"], 101)
    # Departing A: nobody confirmed at A is a no-show (g1 boarded); g3 is
    # waitlisted and its boarding stop leaves, so g3 dies.
    depart(client, rid, 0, 102)
    g3_view = client.get(f"/bookings/{g3.json()['booking_id']}").json()
    assert g3_view["status"] == "void"

    arrive(client, rid, 1, 110)  # g1 alights
    manifest_before = client.get(f"/shuttle/runs/{rid}/manifest").json()
    assert manifest_before["onboard"] == []
    board(client, rid, 1, g2["booking_id"], 111)
    depart(client, rid, 1, 112)
    # g2 never boards B: simulate a confirmed no-show by making a fresh
    # confirmed group at B that misses boarding.
    arrive(client, rid, 2, 120)
    assert client.get(f"/bookings/{g2['booking_id']}").json()["status"] == "completed"


def test_confirmed_no_show_at_second_boarding_voids_only_remaining_leg(client):
    run1, run2 = _two_runs_transfer(client, capacity=1)
    order = book(
        client,
        "xfer-noshow",
        [
            {"run_id": run1, "board_index": 0, "alight_index": 1},
            {"run_id": run2, "board_index": 0, "alight_index": 1},
        ],
    ).json()
    bid = order["booking_id"]
    # First leg is ridden and alighted.
    arrive(client, run1, 0, 100)
    board(client, run1, 0, bid, 101)
    depart(client, run1, 0, 102)
    arrive(client, run1, 1, 110)
    # Miss the run-2 boarding: its Hub stop departs without boarding.
    arrive(client, run2, 0, 115)
    depart(client, run2, 0, 117)
    view = client.get(f"/bookings/{bid}").json()
    assert view["legs"][0]["status"] == "alighted"
    assert view["legs"][1]["status"] == "void"
    assert view["status"] == "void"
    # The ridden leg remains in run 1's history, not on any onboard list.
    m1 = client.get(f"/shuttle/runs/{run1}/manifest").json()
    assert m1["onboard"] == [] and [e["booking_id"] for e in m1["history"]] == [bid]
    m2 = client.get(f"/shuttle/runs/{run2}/manifest").json()
    assert m2["onboard"] == [] and m2["history"] == []


# ------------------------------------------------------------- waitlist
def test_waitlist_promotes_in_application_order_and_skips_unfit(client):
    rid = _three_stop_run(client, capacity=2)
    # Fill the bus with one group of 2 over both intervals.
    anchor = book(
        client,
        "anchor",
        [{"run_id": rid, "board_index": 0, "alight_index": 2}],
        count=2,
    ).json()
    # Application order: big (needs 2, never fits until anchor fully leaves),
    # then small (needs 1, fits as soon as one space frees).
    big = book(
        client,
        "big",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
        count=2,
    ).json()
    small = book(
        client,
        "small",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
        count=1,
    ).json()
    assert client.get(f"/bookings/{big['booking_id']}").json()["status"] == "waiting"
    assert client.get(f"/bookings/{small['booking_id']}").json()["status"] == "waiting"

    # Cancelling the anchor releases both spaces: big (first in line) confirms.
    r = client.post(f"/bookings/{anchor['booking_id']}/cancel")
    assert r.status_code == 200
    assert client.get(f"/bookings/{big['booking_id']}").json()["status"] == "confirmed"
    assert client.get(f"/bookings/{small['booking_id']}").json()["status"] == "waiting"


def test_promotion_evaluates_each_group_against_remaining_space(client):
    rid = _three_stop_run(client, capacity=2)
    # Anchor fills both spaces on the first interval, so both later groups wait.
    anchor = book(
        client,
        "anchor",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
        count=2,
    ).json()
    # Three groups queued for the two freed interval-0 spaces; evaluation must
    # continue against what is left after each promotion.
    first = book(
        client,
        "first",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    ).json()
    second = book(
        client,
        "second",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    ).json()
    third = book(
        client,
        "third",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    ).json()
    assert client.get(f"/bookings/{first['booking_id']}").json()["status"] == "waiting"
    client.post(f"/bookings/{anchor['booking_id']}/cancel")
    assert client.get(f"/bookings/{first['booking_id']}").json()["status"] == "confirmed"
    assert client.get(f"/bookings/{second['booking_id']}").json()["status"] == "confirmed"
    # Two spaces freed and both consumed; third in line keeps waiting.
    assert client.get(f"/bookings/{third['booking_id']}").json()["status"] == "waiting"


def test_promoted_group_can_board_at_a_stop_that_is_still_open(client):
    rid = _three_stop_run(client, capacity=1)
    g1 = book(
        client,
        "g1",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    ).json()
    g2 = book(
        client,
        "g2",
        [{"run_id": rid, "board_index": 1, "alight_index": 2}],
    ).json()
    g3 = book(
        client,
        "g3",
        [{"run_id": rid, "board_index": 1, "alight_index": 2}],
    ).json()
    assert client.get(f"/bookings/{g3['booking_id']}").json()["status"] == "waiting"

    arrive(client, rid, 0, 100)
    board(client, rid, 0, g1["booking_id"], 101)
    depart(client, rid, 0, 102)
    # B arrival records g1 alighting; g3 is still blocked by g2.  Cancelling g2
    # promotes g3 while B has not departed, so g3 can board at B.
    arrive(client, rid, 1, 110)
    assert client.post(f"/bookings/{g2['booking_id']}/cancel").status_code == 200
    assert (
        client.get(f"/bookings/{g3['booking_id']}").json()["status"] == "confirmed"
    )
    board(client, rid, 1, g3["booking_id"], 111)


def test_cancellation_only_before_first_boarding(client):
    rid = _three_stop_run(client)
    order = book(
        client,
        "g",
        [{"run_id": rid, "board_index": 0, "alight_index": 1}],
    ).json()
    arrive(client, rid, 0, 100)
    board(client, rid, 0, order["booking_id"], 101)
    r = client.post(f"/bookings/{order['booking_id']}/cancel")
    assert r.status_code == 409 and r.json()["reason"] == "already_boarded"
    # Double cancel is also rejected.
    other = book(
        client,
        "g2",
        [{"run_id": rid, "board_index": 1, "alight_index": 2}],
    ).json()
    assert client.post(f"/bookings/{other['booking_id']}/cancel").status_code == 200
    r = client.post(f"/bookings/{other['booking_id']}/cancel")
    assert r.status_code == 409


# ------------------------------------------------------------- persistence
def test_state_survives_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        rid = _three_stop_run(client)
        order = book(
            client,
            "persist",
            [{"run_id": rid, "board_index": 0, "alight_index": 2}],
        ).json()
        waiting = book(
            client,
            "persist-wait",
            [{"run_id": rid, "board_index": 0, "alight_index": 1}],
            count=2,
        ).json()
        arrive(client, rid, 0, 100)
        board(client, rid, 0, order["booking_id"], 101)

    # Reopen the same database file with a fresh application.
    app2 = create_app(db_path)
    with TestClient(app2) as client:
        run = client.get(f"/shuttle/runs/{rid}").json()
        assert run["stops"][0]["actual_depart_min"] is None
        assert run["stops"][0]["actual_arrive_min"] == 100
        manifest = client.get(f"/shuttle/runs/{rid}/manifest").json()
        assert [e["booking_id"] for e in manifest["onboard"]] == [order["booking_id"]]
        got = client.get(f"/bookings/{order['booking_id']}").json()
        assert got["legs"][0]["status"] == "boarded"
        assert got["legs"][0]["actual_board_min"] == 101
        assert (
            client.get(f"/bookings/{waiting['booking_id']}").json()["status"]
            == "waiting"
        )
        # Idempotency still enforced after restart.
        replay = client.post(
            "/bookings",
            json={
                "request_id": "persist",
                "wheelchair_count": 1,
                "legs": [{"run_id": rid, "board_index": 0, "alight_index": 2}],
            },
        )
        assert replay.status_code == 200
        assert replay.json()["booking_id"] == order["booking_id"]


# ------------------------------------------------------------- concurrency
def test_concurrent_bookings_never_oversell(tmp_path):
    db_path = tmp_path / "concurrent.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        rid = _three_stop_run(client, capacity=1)
        barrier = threading.Barrier(6)
        results: list[int] = []
        lock = threading.Lock()

        def worker(i):
            barrier.wait()
            r = client.post(
                "/bookings",
                json={
                    "request_id": f"parallel-{i}",
                    "wheelchair_count": 1,
                    "legs": [
                        {"run_id": rid, "board_index": 0, "alight_index": 1}
                    ],
                },
            )
            with lock:
                results.append(r.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(code in (200, 201) for code in results)
        run = client.get(f"/shuttle/runs/{rid}").json()
        assert run["intervals"][0]["occupied_wheelchairs"] <= 1
        confirmed = [
            client.get(f"/bookings/by-request/parallel-{i}").json()
            for i in range(6)
        ]
        assert sum(1 for b in confirmed if b["status"] == "confirmed") == 1
        assert sum(1 for b in confirmed if b["status"] == "waiting") == 5
