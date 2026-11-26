"""End-to-end tests for evacuation headcount, handovers, clearance and closure.

Covers: incident setup on existing stops with disjoint locations, registration
(including later additions and location-to-be-verified), findings that only
apply to unlocated people, handover chain of custody with partial receive /
partial return and mutual exclusion, in-transit people locked out of other
manifests, safe-point semantics, search/clearance invalidation on any touching
activity (and independence between zones), closure blockers, request-id
idempotency, restart persistence, concurrency safety, and the all-or-nothing
batch rule.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


# --------------------------------------------------------------------- setup
@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "evac.db")
    with TestClient(app) as c:
        yield c


def add_stop(client, sid, a, b):
    r = client.post(
        "/segments",
        json={
            "id": sid,
            "from_stop": a,
            "to_stop": b,
            "minutes": 5,
            "windows": [{"open_min": 0, "close_min": 1440}],
            "wheelchair_accessible": True,
        },
    )
    assert r.status_code == 201, r.text


def seed_stops(client):
    # Every incident location must be a stop name already known to the network.
    add_stop(client, "S1", "HallA", "HallB")
    add_stop(client, "S2", "HallB", "HallC")
    add_stop(client, "S3", "HallC", "Plaza")
    add_stop(client, "S4", "HallA", "Gate")


def open_incident(client, request_id="inc-1", name="Drill",
                  zones=("HallA", "HallB", "HallC"),
                  safe_points=("Plaza", "Gate")):
    r = client.post(
        "/evac/incidents",
        json={
            "request_id": request_id,
            "name": name,
            "zones": list(zones),
            "safe_points": list(safe_points),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["incident_id"]


def get_incident(client, iid):
    return client.get(f"/evac/incidents/{iid}").json()


def person(client, iid, no):
    return next(p for p in get_incident(client, iid)["people"] if p["person_no"] == no)


def zone_state(client, iid, zone):
    return next(
        z for z in get_incident(client, iid)["zone_states"] if z["zone"] == zone
    )


def register(client, iid, request_id, people):
    return client.post(
        f"/evac/incidents/{iid}/people",
        json={"request_id": request_id, "people": people},
    )


def find(client, iid, request_id, no, location):
    return client.post(
        f"/evac/incidents/{iid}/findings",
        json={"request_id": request_id, "person_no": no, "location": location},
    )


def handover(client, iid, request_id, from_zone, to_location, nos):
    return client.post(
        f"/evac/incidents/{iid}/handovers",
        json={
            "request_id": request_id,
            "from_zone": from_zone,
            "to_location": to_location,
            "person_nos": nos,
        },
    )


def latest_handover(client, iid):
    return client.get(f"/evac/incidents/{iid}/handovers").json()[-1]


def receive(client, iid, hid, request_id, nos):
    return client.post(
        f"/evac/incidents/{iid}/handovers/{hid}/receive",
        json={"request_id": request_id, "person_nos": nos},
    )


def send_back(client, iid, hid, request_id, nos):
    return client.post(
        f"/evac/incidents/{iid}/handovers/{hid}/return",
        json={"request_id": request_id, "person_nos": nos},
    )


def search(client, iid, zone, request_id):
    return client.post(
        f"/evac/incidents/{iid}/zones/{zone}/search",
        json={"request_id": request_id},
    )


def clear(client, iid, zone, request_id):
    return client.post(
        f"/evac/incidents/{iid}/zones/{zone}/clearance",
        json={"request_id": request_id},
    )


# ------------------------------------------------------------- incident setup
def test_incident_locations_must_be_known_and_disjoint(client):
    seed_stops(client)
    r = client.post(
        "/evac/incidents",
        json={
            "request_id": "bad-1",
            "name": "x",
            "zones": ["HallA", "Nowhere"],
            "safe_points": ["Plaza"],
        },
    )
    assert r.status_code == 422 and r.json()["reason"] == "unknown_location"

    r = client.post(
        "/evac/incidents",
        json={
            "request_id": "bad-2",
            "name": "x",
            "zones": ["HallA"],
            "safe_points": ["HallA"],
        },
    )
    assert r.status_code == 422

    r = client.post(
        "/evac/incidents",
        json={
            "request_id": "bad-3",
            "name": "x",
            "zones": ["HallA", "HallA"],
            "safe_points": ["Plaza"],
        },
    )
    assert r.status_code == 422

    iid = open_incident(client)
    body = get_incident(client, iid)
    assert body["zones"] == ["HallA", "HallB", "HallC"]
    assert body["safe_points"] == ["Plaza", "Gate"]
    assert body["status"] == "open"
    assert body["people_total"] == 0


def test_incident_creation_is_idempotent(client):
    seed_stops(client)
    payload = {
        "request_id": "inc-x",
        "name": "Drill",
        "zones": ["HallA"],
        "safe_points": ["Plaza"],
    }
    first = client.post("/evac/incidents", json=payload)
    assert first.status_code == 201
    replay = client.post("/evac/incidents", json=payload)
    assert replay.status_code == 200
    assert replay.json()["incident_id"] == first.json()["incident_id"]

    changed = dict(payload, name="Other")
    r = client.post("/evac/incidents", json=changed)
    assert r.status_code == 409 and r.json()["reason"] == "duplicate_request"


# ------------------------------------------------------------- registration
def test_registration_states_and_late_additions(client):
    seed_stops(client)
    iid = open_incident(client)
    r = register(
        client,
        iid,
        "reg-1",
        [
            {"person_no": "001", "zone": "HallA"},
            {"person_no": "002"},  # location to be verified
        ],
    )
    assert r.status_code == 201
    assert person(client, iid, "001")["status"] == "in_zone"
    assert person(client, iid, "001")["zone"] == "HallA"
    assert person(client, iid, "002")["status"] == "unknown"

    # Registration stays open while the incident is open.
    r = register(client, iid, "reg-2", [{"person_no": "003", "zone": "HallB"}])
    assert r.status_code == 201
    assert get_incident(client, iid)["people_total"] == 3

    # Person numbers are unique within the incident.
    r = register(client, iid, "reg-3", [{"person_no": "001"}])
    assert r.status_code == 409
    assert r.json()["reason"] == "person_already_registered"

    # A named starting location must be one of the zones, not a safe point.
    r = register(client, iid, "reg-4", [{"person_no": "004", "zone": "Plaza"}])
    assert r.status_code == 404 and r.json()["reason"] == "zone_not_found"

    # The whole batch fails or applies together: nothing from it landed.
    r = register(
        client,
        iid,
        "reg-5",
        [{"person_no": "005"}, {"person_no": "001"}],
    )
    assert r.status_code == 409
    assert all(p["person_no"] != "005" for p in get_incident(client, iid)["people"])


def test_findings_only_apply_to_unlocated_people(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}, {"person_no": "2"}])

    # An unknown person can be found in a zone...
    r = find(client, iid, "f1", "2", "HallB")
    assert r.status_code == 201
    assert person(client, iid, "2")["zone"] == "HallB"

    # ...or directly at a safe point (safe without any handover).
    register(client, iid, "r3", [{"person_no": "3"}])
    r = find(client, iid, "f2", "3", "Gate")
    assert r.status_code == 201
    p3 = person(client, iid, "3")
    assert p3["status"] == "safe" and p3["safe_point"] == "Gate"

    # A located person cannot be re-"found" to bypass the handover process.
    r = find(client, iid, "f3", "1", "Gate")
    assert r.status_code == 409 and r.json()["reason"] == "person_not_unknown"

    # Findings must name a location of this incident.
    r = find(client, iid, "f4", "2", "Elsewhere")
    assert r.status_code == 404 and r.json()["reason"] == "location_not_found"

    # Unknown person number.
    r = find(client, iid, "f5", "999", "HallA")
    assert r.status_code == 404 and r.json()["reason"] == "unknown_person"


# ---------------------------------------------------------------- handovers
def test_partial_receive_keeps_remainder_in_transit(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [
        {"person_no": "1", "zone": "HallA"},
        {"person_no": "2", "zone": "HallA"},
        {"person_no": "3", "zone": "HallA"},
    ])
    r = handover(client, iid, "h1", "HallA", "HallB", ["1", "2", "3"])
    assert r.status_code == 201
    hid = latest_handover(client, iid)["handover_id"]
    for no in ("1", "2", "3"):
        assert person(client, iid, no)["status"] == "in_transit"

    # Receive only part of the manifest.
    r = receive(client, iid, hid, "recv-1", ["1", "2"])
    assert r.status_code == 201
    ho = next(h for h in r.json()["handovers"] if h["handover_id"] == hid)
    assert ho["status"] == "open"
    assert ho["received"] == ["1", "2"] and ho["pending"] == ["3"]
    assert person(client, iid, "1")["zone"] == "HallB"
    assert person(client, iid, "3")["status"] == "in_transit"

    # The still-in-transit person cannot be put on another handover.
    r = handover(client, iid, "h2", "HallA", "HallC", ["3"])
    assert r.status_code == 409 and r.json()["reason"] == "person_in_transit"


def test_safe_point_handover_makes_safe_only_on_receipt(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])
    r = handover(client, iid, "h", "HallA", "Plaza", ["1"])
    hid = latest_handover(client, iid)["handover_id"]
    # In transit: not safe yet.
    assert person(client, iid, "1")["status"] == "in_transit"

    r = receive(client, iid, hid, "recv", ["1"])
    p = person(client, iid, "1")
    assert p["status"] == "safe" and p["safe_point"] == "Plaza"

    # Safe people never take part in another transfer (the origin zone is the
    # valid field here; safe people are not physically in any zone).
    r = handover(client, iid, "h2", "HallA", "Gate", ["1"])
    assert r.status_code == 409 and r.json()["reason"] == "person_already_safe"


def test_return_brings_people_back_to_originating_zone(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [
        {"person_no": "1", "zone": "HallA"},
        {"person_no": "2", "zone": "HallA"},
    ])
    hid = handover(client, iid, "h", "HallA", "HallB", ["1", "2"]).json()["handovers"][-1]["handover_id"]

    # Receiver takes one; sender confirms the other turned back.
    assert receive(client, iid, hid, "recv", ["1"]).status_code == 201
    r = send_back(client, iid, hid, "back", ["2"])
    assert r.status_code == 201
    assert person(client, iid, "2")["zone"] == "HallA"
    ho = next(h for h in r.json()["handovers"] if h["handover_id"] == hid)
    assert ho["status"] == "complete"
    assert ho["returned"] == ["2"] and ho["pending"] == []

    # A returned person can be sent out again on a fresh handover.
    r = handover(client, iid, "h2", "HallA", "Plaza", ["2"])
    assert r.status_code == 201


def test_each_person_is_resolved_at_most_once_on_a_manifest(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])
    hid = handover(client, iid, "h", "HallA", "Plaza", ["1"]).json()["handovers"][-1]["handover_id"]

    assert receive(client, iid, hid, "recv", ["1"]).status_code == 201
    r = receive(client, iid, hid, "recv-again", ["1"])
    assert r.status_code == 409 and r.json()["reason"] == "already_resolved"
    r = send_back(client, iid, hid, "back-late", ["1"])
    assert r.status_code == 409 and r.json()["reason"] == "already_resolved"


def test_receive_and_return_conflict_has_a_single_winner(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])
    hid = handover(client, iid, "h", "HallA", "Plaza", ["1"]).json()["handovers"][-1]["handover_id"]

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def do_receive():
        barrier.wait()
        r = receive(client, iid, hid, "recv", ["1"])
        with lock:
            outcomes.append(r.status_code)

    def do_return():
        barrier.wait()
        r = send_back(client, iid, hid, "back", ["1"])
        with lock:
            outcomes.append(r.status_code)

    threads = [threading.Thread(target=do_receive), threading.Thread(target=do_return)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == [201, 409]
    status = person(client, iid, "1")["status"]
    assert status in ("safe", "in_zone")  # exactly one resolution applied


def test_batch_request_never_half_applies(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [
        {"person_no": "1", "zone": "HallA"},
        {"person_no": "2", "zone": "HallA"},
    ])
    hid = handover(client, iid, "h", "HallA", "Plaza", ["1", "2"]).json()["handovers"][-1]["handover_id"]
    receive(client, iid, hid, "recv-1", ["1"])

    # This batch mixes a resolved person and a pending one: it must fail whole.
    r = receive(client, iid, hid, "recv-bad", ["1", "2"])
    assert r.status_code == 409 and r.json()["reason"] == "already_resolved"
    ho = latest_handover(client, iid)
    assert ho["pending"] == ["2"]  # person 2 untouched by the failed batch


def test_return_batch_never_half_applies(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [
        {"person_no": "1", "zone": "HallA"},
        {"person_no": "2", "zone": "HallA"},
        {"person_no": "3", "zone": "HallA"},
    ])
    hid = handover(client, iid, "h", "HallA", "HallB", ["1", "2", "3"]).json()["handovers"][-1]["handover_id"]
    # Person 1 already returned; a return batch mixing them with pending people
    # must fail as a whole and leave the pending people in transit.
    send_back(client, iid, hid, "back-1", ["1"])
    r = send_back(client, iid, hid, "back-bad", ["1", "2"])
    assert r.status_code == 409 and r.json()["reason"] == "already_resolved"
    ho = latest_handover(client, iid)
    assert ho["returned"] == ["1"] and ho["pending"] == ["2", "3"]


def test_zone_to_zone_custody_path(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])
    first = handover(client, iid, "h1", "HallA", "HallB", ["1"]).json()["handovers"][-1]["handover_id"]
    receive(client, iid, first, "recv-1", ["1"])
    assert person(client, iid, "1")["zone"] == "HallB"
    second = handover(client, iid, "h2", "HallB", "HallC", ["1"]).json()["handovers"][-1]["handover_id"]
    receive(client, iid, second, "recv-2", ["1"])
    assert person(client, iid, "1")["zone"] == "HallC"
    third = handover(client, iid, "h3", "HallC", "Plaza", ["1"]).json()["handovers"][-1]["handover_id"]
    receive(client, iid, third, "recv-3", ["1"])
    p = person(client, iid, "1")
    assert p["status"] == "safe" and p["safe_point"] == "Plaza"
    # All three manifests are fully processed.
    assert all(h["status"] == "complete" for h in client.get(f"/evac/incidents/{iid}/handovers").json())


def test_handover_validation(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [
        {"person_no": "1", "zone": "HallA"},
        {"person_no": "2"},
        {"person_no": "3", "zone": "HallB"},
    ])
    # Origin must be a zone.
    r = handover(client, iid, "h0", "Plaza", "Gate", ["1"])
    assert r.status_code == 404 and r.json()["reason"] == "zone_not_found"
    # Destination must belong to the incident.
    r = handover(client, iid, "h1", "HallA", "Elsewhere", ["1"])
    assert r.status_code == 404 and r.json()["reason"] == "location_not_found"
    # Same-zone transfer.
    r = handover(client, iid, "h2", "HallA", "HallA", ["1"])
    assert r.status_code == 422 and r.json()["reason"] == "illegal_transfer"
    # Sender must physically hold every listed person in the origin zone.
    r = handover(client, iid, "h3", "HallA", "HallB", ["1", "3"])
    assert r.status_code == 409 and r.json()["reason"] == "person_not_in_zone"
    # An unlocated person cannot be shipped.
    r = handover(client, iid, "h4", "HallA", "HallB", ["2"])
    assert r.status_code == 409 and r.json()["reason"] == "person_not_in_zone"


# ------------------------------------------------------------- search/clearance
def test_search_requires_empty_zone_without_open_handover(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])

    r = search(client, iid, "HallA", "s1")
    assert r.status_code == 409 and r.json()["reason"] == "zone_not_empty"

    hid = handover(client, iid, "h", "HallA", "HallB", ["1"]).json()["handovers"][-1]["handover_id"]
    # HallA is empty now but the open handover still touches it.
    r = search(client, iid, "HallA", "s2")
    assert r.status_code == 409 and r.json()["reason"] == "open_handover"

    receive(client, iid, hid, "recv", ["1"])
    r = search(client, iid, "HallA", "s3")
    assert r.status_code == 201
    z = zone_state(client, iid, "HallA")
    assert z["search_started"] and z["search_valid"] and not z["cleared"]


def test_clearance_is_invalidated_by_later_activity(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])

    # HallC is empty from the start: search and clear it.
    assert search(client, iid, "HallC", "sc").status_code == 201
    assert clear(client, iid, "HallC", "cc").status_code == 201
    assert zone_state(client, iid, "HallC")["cleared"] is True

    # A person entering HallC invalidates the clearance immediately.
    hid = handover(client, iid, "h", "HallA", "HallC", ["1"]).json()["handovers"][-1]["handover_id"]
    receive(client, iid, hid, "recv", ["1"])
    z = zone_state(client, iid, "HallC")
    assert z["search_valid"] is False and z["cleared"] is False

    # The stale search can no longer confirm anything.
    r = clear(client, iid, "HallC", "cc2")
    assert r.status_code == 409 and r.json()["reason"] == "search_stale"

    # Even after the zone empties again, the invalidated search is still the
    # active one and must be re-done before a clearance can be confirmed.
    hid2 = handover(client, iid, "h2", "HallC", "Plaza", ["1"]).json()["handovers"][-1]["handover_id"]
    receive(client, iid, hid2, "recv2", ["1"])
    r = clear(client, iid, "HallC", "cc3")
    assert r.status_code == 409 and r.json()["reason"] == "search_stale"
    assert search(client, iid, "HallC", "sc2").status_code == 201
    assert clear(client, iid, "HallC", "cc4").status_code == 201
    assert zone_state(client, iid, "HallC")["cleared"] is True


def test_other_zone_activity_does_not_disturb_a_clearance(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])

    assert search(client, iid, "HallB", "sb").status_code == 201
    assert clear(client, iid, "HallB", "cb").status_code == 201
    version_before = zone_state(client, iid, "HallB")["activity_version"]

    # All activity happens in HallA; HallB's generation must not move.
    hid = handover(client, iid, "h", "HallA", "Plaza", ["1"]).json()["handovers"][-1]["handover_id"]
    receive(client, iid, hid, "recv", ["1"])
    z = zone_state(client, iid, "HallB")
    assert z["activity_version"] == version_before
    assert z["cleared"] is True and z["search_valid"] is True


def test_handover_creation_touches_both_endpoint_zones(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])
    # Clear the destination zone before the transfer.
    search(client, iid, "HallB", "s-HallB")
    clear(client, iid, "HallB", "c-HallB")
    assert zone_state(client, iid, "HallB")["cleared"] is True

    hid = handover(client, iid, "h", "HallA", "HallB", ["1"]).json()["handovers"][-1]["handover_id"]
    # Destination clearance is invalidated as soon as the manifest exists,
    # before anyone is received.
    assert zone_state(client, iid, "HallB")["search_valid"] is False
    receive(client, iid, hid, "recv", ["1"])
    assert zone_state(client, iid, "HallB")["cleared"] is False


def test_any_entry_or_leave_invalidates_an_active_search(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}, {"person_no": "2"}])

    # A finding that places an unknown person into an already-searched zone
    # invalidates that zone's search.
    search(client, iid, "HallB", "sb")
    assert zone_state(client, iid, "HallB")["search_valid"] is True
    find(client, iid, "fb", "2", "HallB")
    assert zone_state(client, iid, "HallB")["search_valid"] is False

    # A late registration directly into a zone is likewise activity.
    search(client, iid, "HallC", "sc")
    register(client, iid, "late", [{"person_no": "3", "zone": "HallC"}])
    assert zone_state(client, iid, "HallC")["search_valid"] is False

    # A turn-back re-enters the origin zone and invalidates its fresh search.
    # Two people: both leave HallA, one is received (clearing the open-handover
    # block once the other is also resolved), then the other turns back.
    register(client, iid, "r2", [{"person_no": "4", "zone": "HallA"}])
    hid = handover(
        client, iid, "h", "HallA", "Plaza", ["1", "4"]
    ).json()["handovers"][-1]["handover_id"]
    # Empty but an open handover touches it: searching is refused.
    assert search(client, iid, "HallA", "sa-blocked").status_code == 409
    receive(client, iid, hid, "recv-1", ["1"])
    # Resolve person 4 as a return: the manifest completes and HallA is
    # occupied again, so a search still cannot start until they leave.
    send_back(client, iid, hid, "back-4", ["4"])
    assert person(client, iid, "4")["zone"] == "HallA"
    hid2 = handover(client, iid, "h2", "HallA", "Plaza", ["4"]).json()["handovers"][-1]["handover_id"]
    # HallA is now empty and the manifest is the only thing touching it; once
    # it is received the zone is fully clear to search.
    receive(client, iid, hid2, "recv-4", ["4"])
    assert search(client, iid, "HallA", "sa").status_code == 201
    assert zone_state(client, iid, "HallA")["search_valid"] is True

    # A later return into HallA (a new manifest with a turn-back) invalidates
    # that active search as soon as the person re-enters the zone.
    register(client, iid, "r3", [{"person_no": "5", "zone": "HallA"}])
    hid3 = handover(client, iid, "h3", "HallA", "Plaza", ["5"]).json()["handovers"][-1]["handover_id"]
    send_back(client, iid, hid3, "back-5", ["5"])
    assert person(client, iid, "5")["zone"] == "HallA"
    assert zone_state(client, iid, "HallA")["search_valid"] is False
    assert clear(client, iid, "HallA", "ca").status_code == 409


def test_handover_list_tracks_manifest_lifecycle(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])
    hid = handover(client, iid, "h", "HallA", "Plaza", ["1"]).json()["handovers"][-1]["handover_id"]
    listing = client.get(f"/evac/incidents/{iid}/handovers").json()
    assert len(listing) == 1 and listing[0]["status"] == "open"
    receive(client, iid, hid, "recv", ["1"])
    listing = client.get(f"/evac/incidents/{iid}/handovers").json()
    assert listing[0]["status"] == "complete"
    assert listing[0]["people"][0]["resolved_at_safe"] is True


# ------------------------------------------------------------- idempotency
def test_request_id_replay_returns_stored_result_without_redo(client):
    seed_stops(client)
    iid = open_incident(client)
    payload = {"request_id": "reg", "people": [{"person_no": "1", "zone": "HallA"}]}
    first = register(client, iid, "reg", payload["people"])
    assert first.status_code == 201
    replay = client.post(f"/evac/incidents/{iid}/people", json=payload)
    assert replay.status_code == 200
    # No duplicate person was created.
    assert get_incident(client, iid)["people_total"] == 1

    changed = {"request_id": "reg", "people": [{"person_no": "2"}]}
    r = client.post(f"/evac/incidents/{iid}/people", json=changed)
    assert r.status_code == 409 and r.json()["reason"] == "duplicate_request"


def test_request_id_is_scoped_per_incident(client):
    seed_stops(client)
    iid1 = open_incident(client, "inc-a", zones=("HallA",), safe_points=("Plaza",))
    iid2 = open_incident(client, "inc-b", name="Second", zones=("HallB",), safe_points=("Gate",))
    # The same request id in two incidents is independent.
    assert register(client, iid1, "shared", [{"person_no": "1"}]).status_code == 201
    assert register(client, iid2, "shared", [{"person_no": "1"}]).status_code == 201

    # Unknown incident.
    r = register(client, 9999, "x", [{"person_no": "1"}])
    assert r.status_code == 404 and r.json()["reason"] == "incident_not_found"


# ------------------------------------------------------------- closure
def _evacuate_everyone(client, iid):
    body = get_incident(client, iid)
    for p in list(body["people"]):
        if p["status"] == "safe":
            continue
        if p["status"] == "unknown":
            find(client, iid, f"find-{p['person_no']}", p["person_no"], "HallA")
            p = person(client, iid, p["person_no"])
        zone = p["zone"]
        h = handover(client, iid, f"out-{p['person_no']}", zone, "Plaza", [p["person_no"]])
        hid = next(
            x["handover_id"] for x in h.json()["handovers"]
            if x["request_id"] == f"out-{p['person_no']}"
        )
        receive(client, iid, hid, f"recv-{p['person_no']}", [p["person_no"]])


def test_close_requires_all_conditions_and_lists_blockers(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [
        {"person_no": "1", "zone": "HallA"},
        {"person_no": "2"},
    ])
    r = client.post(f"/evac/incidents/{iid}/close", json={"request_id": "close"})
    assert r.status_code == 409 and r.json()["reason"] == "not_ready_to_close"
    blockers = r.json()["blockers"]
    assert set(blockers) == {
        "people_not_at_safe_point",
        "open_handovers",
        "zones_without_valid_clearance",
    }
    assert set(blockers["people_not_at_safe_point"]) == {"1", "2"}
    assert blockers["zones_without_valid_clearance"] == ["HallA", "HallB", "HallC"]


def test_full_closure_then_immutable(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [
        {"person_no": "1", "zone": "HallA"},
        {"person_no": "2", "zone": "HallB"},
    ])
    _evacuate_everyone(client, iid)
    for z in ("HallA", "HallB", "HallC"):
        assert search(client, iid, z, f"s-{z}").status_code == 201
        assert clear(client, iid, z, f"c-{z}").status_code == 201

    r = client.post(f"/evac/incidents/{iid}/close", json={"request_id": "close"})
    assert r.status_code == 201 and r.json()["status"] == "closed"
    assert r.json()["ready_to_close"] is True

    # Every further mutation is rejected, but reads remain available.
    r = register(client, iid, "late", [{"person_no": "9"}])
    assert r.status_code == 409 and r.json()["reason"] == "incident_closed"
    r = find(client, iid, "late-f", "1", "HallA")
    assert r.status_code == 409
    assert get_incident(client, iid)["status"] == "closed"

    # Closing again with the same request id replays the stored result.
    replay = client.post(f"/evac/incidents/{iid}/close", json={"request_id": "close"})
    assert replay.status_code == 200 and replay.json()["status"] == "closed"


def test_readiness_tracks_each_blocker_category(client):
    seed_stops(client)
    iid = open_incident(client)
    register(client, iid, "r", [{"person_no": "1", "zone": "HallA"}])

    # One open handover keeps both the handover and person blockers alive.
    hid = handover(client, iid, "h", "HallA", "Plaza", ["1"]).json()["handovers"][-1]["handover_id"]
    body = get_incident(client, iid)
    assert body["ready_to_close"] is False
    assert body["blockers"]["open_handovers"] == [str(hid)]
    assert body["blockers"]["people_not_at_safe_point"] == ["1"]

    receive(client, iid, hid, "recv", ["1"])
    body = get_incident(client, iid)
    assert body["blockers"]["open_handovers"] == []
    assert body["blockers"]["people_not_at_safe_point"] == []
    # Only zone clearances remain.
    assert body["blockers"]["zones_without_valid_clearance"] == [
        "HallA", "HallB", "HallC"
    ]
    for zone in body["zones"]:
        search(client, iid, zone, f"s-{zone}")
        clear(client, iid, zone, f"c-{zone}")
    assert get_incident(client, iid)["ready_to_close"] is True


# ------------------------------------------------------------- persistence
def test_full_state_survives_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    with TestClient(create_app(db_path)) as c:
        seed_stops(c)
        iid = open_incident(c)
        register(c, iid, "r", [
            {"person_no": "1", "zone": "HallA"},
            {"person_no": "2", "zone": "HallA"},
        ])
        hid = handover(c, iid, "h", "HallA", "Plaza", ["1", "2"]).json()["handovers"][-1]["handover_id"]
        receive(c, iid, hid, "recv", ["1"])
        search(c, iid, "HallC", "sc")
        clear(c, iid, "HallC", "cc")

    with TestClient(create_app(db_path)) as c:
        body = get_incident(c, iid)
        assert body["people_total"] == 2
        assert person(c, iid, "1")["status"] == "safe"
        assert person(c, iid, "2")["status"] == "in_transit"
        ho = next(h for h in body["handovers"] if h["handover_id"] == hid)
        assert ho["pending"] == ["2"] and ho["received"] == ["1"]
        assert zone_state(c, iid, "HallC")["cleared"] is True

        # The in-transit person can still be resolved after restart.
        assert send_back(c, iid, hid, "back", ["2"]).status_code == 201
        assert person(c, iid, "2")["zone"] == "HallA"

        # Idempotency ledger survived as well.
        replay = receive(c, iid, hid, "recv", ["1"])
        assert replay.status_code == 200
        changed = receive(c, iid, hid, "recv", ["2"])
        assert changed.status_code == 409
        assert changed.json()["reason"] == "duplicate_request"


def test_original_business_endpoints_remain_available(client):
    # The evacuation feature must not disturb planning or shuttle booking.
    add_stop(client, "W1", "O", "D")
    plan = client.post(
        "/plans",
        json={
            "origin": "O",
            "destination": "D",
            "depart_min": 0,
            "arrive_by_min": 100,
            "max_wait_min": 20,
        },
    )
    assert plan.status_code == 201
    run = client.post(
        "/shuttle/runs",
        json={
            "wheelchair_capacity": 1,
            "stops": [
                {"stop": "O", "arrive_min": 0, "depart_min": 2},
                {"stop": "D", "arrive_min": 10, "depart_min": 10},
            ],
        },
    )
    assert run.status_code == 201
    booking = client.post(
        "/bookings",
        json={
            "request_id": "b1",
            "wheelchair_count": 1,
            "legs": [{"run_id": run.json()["run_id"], "board_index": 0, "alight_index": 1}],
        },
    )
    assert booking.status_code in (200, 201)
