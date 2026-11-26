"""End-to-end tests over the HTTP API, including the two required scenarios."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as c:
        yield c


def add_segment(client, sid, a, b, minutes, windows, accessible=True):
    r = client.post(
        "/segments",
        json={
            "id": sid,
            "from_stop": a,
            "to_stop": b,
            "minutes": minutes,
            "windows": [{"open_min": o, "close_min": c} for o, c in windows],
            "wheelchair_accessible": accessible,
        },
    )
    assert r.status_code == 201, r.text
    return r


def plan(client, **kw):
    return client.post("/plans", json=kw)


# ---------------------------------------------------------------------------
# Scenario 1: parallel first legs.  Route A reaches the transfer earlier but
# then blows the maximum wait; route B arrives later and makes the connection.
# ---------------------------------------------------------------------------

def _parallel_network(client):
    # Both first legs can only leave O at 100.
    add_segment(client, "A1", "O", "XA", 5, [(100, 105)])   # arrives XA at 105
    add_segment(client, "B1", "O", "XB", 8, [(100, 108)])   # arrives XB at 108
    # The only onward accessible edge from XA departs at 130: 25 min wait > 20,
    # so the route that reaches the interchange *earlier* cannot connect.
    add_segment(client, "A2", "XA", "D", 4, [(130, 200)])
    # From XB it departs at 110: only a 2 min wait, within the cap.
    add_segment(client, "B2", "XB", "D", 4, [(110, 200)])


def test_earlier_transfer_misses_connection_later_one_catches_it(client):
    _parallel_network(client)
    r = plan(
        client,
        origin="O",
        destination="D",
        depart_min=100,
        arrive_by_min=200,
        max_wait_min=20,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [leg["segment_id"] for leg in body["legs"]] == ["B1", "B2"]
    assert body["arrival_min"] == 114
    b1, b2 = body["legs"]
    assert b1["depart_min"] == 100 and b1["arrive_min"] == 108
    assert b2["depart_min"] == 110 and b2["arrive_min"] == 114
    assert b2["depart_min"] - b1["arrive_min"] <= 20


def test_earlier_route_is_not_used_when_connection_is_out_of_reach(client):
    _parallel_network(client)
    r = plan(
        client,
        origin="O",
        destination="D",
        depart_min=100,
        arrive_by_min=200,
        max_wait_min=20,
    )
    legs = r.json()["legs"]
    # Reaching XA at 105 is earlier than reaching D at 114, but its only
    # continuation needs a 25-minute wait, so the route is not a solution.
    assert all(leg["segment_id"] != "A1" for leg in legs)


def test_origin_wait_is_capped(client):
    add_segment(client, "S", "O", "D", 5, [(100, 200)])
    # depart at 50; the first (only) boarding is at 100 = 50 min of waiting.
    r = plan(
        client,
        origin="O",
        destination="D",
        depart_min=50,
        arrive_by_min=200,
        max_wait_min=20,
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "no_solution"


# ---------------------------------------------------------------------------
# Scenario 2: walk one leg, then a closure reroutes only the remainder.
# ---------------------------------------------------------------------------

def _linear_plus_detour(client):
    # Preferred route: A -> B -> C -> E.
    add_segment(client, "L1", "A", "B", 10, [(60, 180)])
    add_segment(client, "L2", "B", "C", 10, [(60, 180)])
    add_segment(client, "L3", "C", "E", 10, [(60, 180)])
    # Alternative continuation from C if L3 is closed.
    add_segment(client, "G1", "C", "G", 15, [(60, 180)])
    add_segment(client, "G2", "G", "E", 15, [(60, 180)])


def _plan_trip(client, **overrides):
    kw = dict(
        origin="A",
        destination="E",
        depart_min=60,
        arrive_by_min=180,
        max_wait_min=30,
    )
    kw.update(overrides)
    r = plan(client, **kw)
    assert r.status_code == 201, r.text
    return r.json()


def test_walked_leg_is_kept_and_closure_only_changes_tail(client):
    _linear_plus_detour(client)
    body = _plan_trip(client)
    trip_id = body["trip_id"]
    assert [leg["segment_id"] for leg in body["legs"]] == ["L1", "L2", "L3"]
    assert [leg["depart_min"] for leg in body["legs"]] == [60, 70, 80]

    # Visitor walks L1 and reports arriving at B at 67.
    c = client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "L1", "arrival_min": 67}
    )
    assert c.status_code == 200, c.text
    after = c.json()
    assert after["anchor_stop"] == "B" and after["anchor_min"] == 67
    assert [leg["segment_id"] for leg in after["legs"]] == ["L2", "L3"]
    assert after["history"][0]["segment_id"] == "L1"
    # The tail is unchanged: L2 still leaves at its scheduled 70.
    assert after["legs"][0]["depart_min"] == 70

    # Staff close L3: only the remainder is rerouted, from B (not A).
    cl = client.post(
        "/closures", json={"segment_id": "L3", "start_min": 60, "end_min": 180}
    )
    assert cl.status_code == 201, cl.text

    read = client.get(f"/trips/{trip_id}").json()
    assert read["anchor_stop"] == "B" and read["anchor_min"] == 67
    assert [leg["segment_id"] for leg in read["history"]] == ["L1"]
    assert [leg["segment_id"] for leg in read["legs"]] == ["L2", "G1", "G2"]
    # The old tail (L2@70, L3@80) no longer works because L3 is closed, so the
    # remainder is planned afresh from B at minute 67 (earliest arrival wins).
    assert [leg["depart_min"] for leg in read["legs"]] == [67, 77, 92]
    # Never asks the visitor to return to A.
    assert read["legs"][0]["from_stop"] == "B"
    assert all(leg["from_stop"] != "A" for leg in read["legs"])


def test_unrelated_closure_preserves_the_tail_verbatim(client):
    _linear_plus_detour(client)
    add_segment(client, "SPARE", "Z", "Y", 5, [(60, 180)])
    trip_id = _plan_trip(client)["trip_id"]
    client.post(f"/trips/{trip_id}/confirm", json={"segment_id": "L1", "arrival_min": 67})
    before = client.get(f"/trips/{trip_id}").json()["legs"]

    client.post("/closures", json={"segment_id": "SPARE", "start_min": 0, "end_min": 60})
    after = client.get(f"/trips/{trip_id}").json()["legs"]
    assert after == before


def test_closure_restored_reuses_earlier_anchor_and_history(client):
    _linear_plus_detour(client)
    trip_id = _plan_trip(client)["trip_id"]
    client.post(f"/trips/{trip_id}/confirm", json={"segment_id": "L1", "arrival_min": 67})

    # Close both continuations out of C => no solution from B.
    c1 = client.post(
        "/closures", json={"segment_id": "L3", "start_min": 60, "end_min": 180}
    ).json()
    c2 = client.post(
        "/closures", json={"segment_id": "G1", "start_min": 60, "end_min": 180}
    ).json()
    read = client.get(f"/trips/{trip_id}").json()
    assert read["solved"] is False
    assert read["legs"] == []
    assert read["anchor_stop"] == "B" and read["history"][0]["segment_id"] == "L1"

    # Lifting the detour closure makes the trip solvable again, from B.
    client.delete(f"/closures/{c2['id']}")
    read = client.get(f"/trips/{trip_id}").json()
    assert read["solved"] is True
    assert [leg["segment_id"] for leg in read["legs"]] == ["L2", "G1", "G2"]
    assert read["legs"][0]["depart_min"] == 67
    assert read["anchor_stop"] == "B"


def _simple(client):
    add_segment(client, "S1", "A", "B", 5, [(0, 100)])
    add_segment(client, "S2", "B", "C", 5, [(0, 100)])


# ---------------------------------------------------------------------------
# Confirmation semantics.
# ---------------------------------------------------------------------------

def test_stale_confirmation_after_reroute_is_rejected(client):
    add_segment(client, "S1", "A", "B", 5, [(0, 100)])
    add_segment(client, "OLD", "B", "C", 5, [(0, 40)])
    add_segment(client, "NEW", "B", "C", 5, [(0, 100)])
    body = plan(
        client, origin="A", destination="C", depart_min=0, arrive_by_min=100,
        max_wait_min=50,
    ).json()
    trip_id = body["trip_id"]
    assert [leg["segment_id"] for leg in body["legs"]] == ["S1", "NEW"]

    # Close NEW so the trip re-routes onto OLD.
    client.post("/closures", json={"segment_id": "NEW", "start_min": 0, "end_min": 100})
    read = client.get(f"/trips/{trip_id}").json()
    assert [leg["segment_id"] for leg in read["legs"]] == ["S1", "OLD"]

    # A stale confirmation naming NEW is refused.
    r = client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "NEW", "arrival_min": 20}
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "stale_confirmation"

    # The rejected confirmation did not move the trip.
    read = client.get(f"/trips/{trip_id}").json()
    assert read["anchor_stop"] == "A"

    # Confirming the right leg works.
    ok = client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S1", "arrival_min": 5}
    )
    assert ok.status_code == 200


def test_completing_a_trip_then_confirming_is_stale(client):
    _simple(client)
    trip_id = plan(
        client, origin="A", destination="C", depart_min=0, arrive_by_min=100,
        max_wait_min=50,
    ).json()["trip_id"]
    assert client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S1", "arrival_min": 5}
    ).status_code == 200
    done = client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S2", "arrival_min": 10}
    )
    assert done.status_code == 200
    assert done.json()["legs"] == [] and done.json()["arrival_min"] == 10
    extra = client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S2", "arrival_min": 11}
    )
    assert extra.status_code == 409 and extra.json()["reason"] == "stale_confirmation"


def test_time_cannot_go_backwards(client):
    add_segment(client, "S1", "A", "B", 5, [(0, 100)])
    add_segment(client, "S2", "B", "C", 5, [(0, 100)])
    trip_id = plan(
        client, origin="A", destination="C", depart_min=0, arrive_by_min=100,
        max_wait_min=50,
    ).json()["trip_id"]
    assert client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S1", "arrival_min": 10}
    ).status_code == 200
    # S2 departs at 10; arriving "at 9" would rewind the clock.
    r = client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S2", "arrival_min": 9}
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "time_went_backwards"
    # Still pending, not advanced.
    read = client.get(f"/trips/{trip_id}").json()
    assert read["anchor_min"] == 10 and len(read["history"]) == 1


def test_arrival_before_planned_departure_is_rejected(client):
    add_segment(client, "S1", "A", "B", 5, [(10, 100)])
    trip_id = plan(
        client, origin="A", destination="B", depart_min=10, arrive_by_min=100,
        max_wait_min=50,
    ).json()["trip_id"]
    r = client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S1", "arrival_min": 12}
    )
    assert r.status_code == 200  # arrival 12 >= departure 10
    assert client.post(
        f"/trips/{trip_id}/confirm", json={"segment_id": "S1", "arrival_min": 16}
    ).status_code == 409


def test_open_interval_is_half_open_and_closure_any_overlap_blocks(client):
    # Traversal may end exactly at close_min.
    add_segment(client, "H", "A", "B", 10, [(0, 10)])
    body = plan(
        client, origin="A", destination="B", depart_min=0, arrive_by_min=20, max_wait_min=5
    ).json()
    assert body["legs"][0]["depart_min"] == 0

    # Any non-empty overlap with a closure blocks it; a closure starting
    # exactly at the arrival minute does not overlap [0,10).
    client.post("/closures", json={"segment_id": "H", "start_min": 10, "end_min": 12})
    still = plan(
        client, origin="A", destination="B", depart_min=0, arrive_by_min=20, max_wait_min=5
    )
    assert still.status_code == 201 and still.json()["legs"][0]["depart_min"] == 0

    client.post("/closures", json={"segment_id": "H", "start_min": 9, "end_min": 12})
    blocked = plan(
        client, origin="A", destination="B", depart_min=0, arrive_by_min=20, max_wait_min=5
    )
    assert blocked.status_code == 422


def test_inaccessible_segments_are_never_used(client):
    add_segment(client, "SHORT", "A", "B", 5, [(0, 100)], accessible=False)
    add_segment(client, "LONG", "A", "B", 20, [(0, 100)])
    body = plan(
        client, origin="A", destination="B", depart_min=0, arrive_by_min=100, max_wait_min=50
    ).json()
    assert [l["segment_id"] for l in body["legs"]] == ["LONG"]


def test_tie_breaks(client):
    # Same arrival, same leg count: lexicographic segment ids win.
    add_segment(client, "Z", "A", "B", 10, [(0, 100)])
    add_segment(client, "Y", "A", "B", 10, [(0, 100)])
    body = plan(
        client, origin="A", destination="B", depart_min=0, arrive_by_min=100, max_wait_min=50
    ).json()
    assert [l["segment_id"] for l in body["legs"]] == ["Y"]


def test_unsolved_trip_resumes_after_closure_lifted(client):
    _simple(client)
    trip_id = plan(
        client, origin="A", destination="C", depart_min=0, arrive_by_min=100, max_wait_min=50
    ).json()["trip_id"]
    client.post(f"/trips/{trip_id}/confirm", json={"segment_id": "S1", "arrival_min": 5})
    cid = client.post("/closures", json={"segment_id": "S2", "start_min": 0, "end_min": 100}).json()["id"]
    read = client.get(f"/trips/{trip_id}").json()
    assert read["solved"] is False and read["legs"] == []
    assert read["anchor_stop"] == "B" and read["history"][0]["segment_id"] == "S1"
    client.delete(f"/closures/{cid}")
    read = client.get(f"/trips/{trip_id}").json()
    assert read["solved"] is True
    assert [l["segment_id"] for l in read["legs"]] == ["S2"]
    assert read["legs"][0]["depart_min"] == 5
