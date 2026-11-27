"""End-to-end tests for run cancellation and joint rebooking."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "cancellations.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "restart-cancellations.db"


def add_segment(client, sid, a, b, minutes=5):
    r = client.post(
        "/segments",
        json={
            "id": sid,
            "from_stop": a,
            "to_stop": b,
            "minutes": minutes,
            "windows": [{"open_min": 0, "close_min": 1440}],
            "wheelchair_accessible": True,
        },
    )
    assert r.status_code == 201, r.text


def make_run(client, stops, capacity=2):
    payload = []
    for item in stops:
        if isinstance(item, tuple):
            name, arrive, depart = item
        else:
            name, arrive, depart = item, 100, 101
        payload.append({"stop": name, "arrive_min": arrive, "depart_min": depart})
    r = client.post(
        "/shuttle/runs",
        json={"stops": payload, "wheelchair_capacity": capacity},
    )
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def book(client, request_id, legs, count=1):
    return client.post(
        "/bookings",
        json={
            "request_id": request_id,
            "group_name": request_id,
            "wheelchair_count": count,
            "legs": legs,
        },
    )


def cancel(client, run_id, request_id, alternatives):
    return client.post(
        f"/shuttle/runs/{run_id}/cancel",
        json={"request_id": request_id, "alternatives": alternatives},
    )


def option(*legs):
    return {"legs": [
        {"run_id": run_id, "board_index": board, "alight_index": alight}
        for run_id, board, alight in legs
    ]}


def basic_network(client):
    for sid, a, b in [
        ("AB", "A", "B"),
        ("BD", "B", "D"),
        ("AD", "A", "D"),
        ("AX", "A", "X"),
        ("XD", "X", "D"),
    ]:
        add_segment(client, sid, a, b)


def test_direct_booking_is_rebooked_and_receipt_is_idempotent(client):
    basic_network(client)
    old = make_run(
        client,
        [("A", 100, 102), ("B", 110, 112), ("D", 120, 120)],
        capacity=2,
    )
    new = make_run(client, [("A", 105, 107), ("D", 125, 125)], capacity=2)
    order = book(
        client, "g1", [{"run_id": old, "board_index": 0, "alight_index": 2}]
    ).json()

    alternatives = [
        {"booking_id": order["booking_id"], "options": [option((new, 0, 1))]}
    ]
    r = cancel(client, old, "cancel-1", alternatives)
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["run_id"] == old
    assert receipt["affected_booking_ids"] == [order["booking_id"]]
    item = receipt["groups"][0]
    assert [l["run_id"] for l in item["removed_legs"]] == [old]
    assert [l["run_id"] for l in item["replacement_legs"]] == [new]
    assert item["status"] == "rebooked"

    current = client.get(f"/bookings/{order['booking_id']}").json()
    assert current["status"] == "confirmed"
    assert [l["run_id"] for l in current["legs"]] == [new]

    run = client.get(f"/shuttle/runs/{old}").json()
    assert run["cancelled"] is True
    assert run["cancellation_request_id"] == "cancel-1"

    # Exact replay returns the same receipt.
    replay = cancel(client, old, "cancel-1", alternatives)
    assert replay.status_code == 200
    assert replay.json()["cancellation_id"] == receipt["cancellation_id"]

    # Reusing the operation id with different logical content is a conflict.
    changed_payload = {
        "request_id": "cancel-1",
        "run_id": old,
        "alternatives": [],
    }
    rr = client.post("/shuttle/cancellations", json=changed_payload)
    assert rr.status_code == 409
    assert rr.json()["reason"] == "duplicate_request"

    got = client.get("/shuttle/cancellations/by-request/cancel-1")
    assert got.status_code == 200
    assert got.json()["cancellation_id"] == receipt["cancellation_id"]


def test_only_run_without_first_arrival_can_be_cancelled(client):
    basic_network(client)
    old = make_run(client, [("A", 100, 102), ("D", 120, 120)])
    new = make_run(client, [("A", 105, 107), ("D", 125, 125)])
    order = book(
        client, "g", [{"run_id": old, "board_index": 0, "alight_index": 1}]
    ).json()

    r = client.post(
        f"/shuttle/runs/{old}/stops/0/arrival", json={"actual_min": 100}
    )
    assert r.status_code == 200
    r = cancel(
        client,
        old,
        "too-late",
        [{"booking_id": order["booking_id"], "options": [option((new, 0, 1))]}],
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "run_already_started"
    # Failed operation id was not stored and can be reused after state allows?
    # Here run remains started, but the important assertion is no receipt.
    assert client.get("/shuttle/cancellations/by-request/too-late").status_code == 404


def test_unplaced_group_keeps_retained_leg_and_is_not_waitlisted(client):
    add_segment(client, "AB", "A", "B")
    add_segment(client, "BD", "B", "D")
    add_segment(client, "AD", "A", "D")
    first = make_run(client, [("A", 100, 102), ("B", 110, 112)])
    dead = make_run(client, [("B", 115, 116), ("D", 125, 125)])
    crowded = make_run(
        client, [("B", 116, 117), ("D", 126, 126)], capacity=1
    )
    # Fill the only alternative.
    blocker = book(
        client, "block", [{"run_id": crowded, "board_index": 0, "alight_index": 1}]
    )
    assert blocker.json()["status"] == "confirmed"
    order = book(
        client,
        "two-leg",
        [
            {"run_id": first, "board_index": 0, "alight_index": 1},
            {"run_id": dead, "board_index": 0, "alight_index": 1},
        ],
    ).json()

    r = cancel(
        client,
        dead,
        "cancel-unplaced",
        [
            {
                "booking_id": order["booking_id"],
                "options": [option((crowded, 0, 1))],
            }
        ],
    )
    assert r.status_code == 200, r.text
    item = r.json()["groups"][0]
    assert item["status"] == "unplaced"
    assert [l["run_id"] for l in item["removed_legs"]] == [dead]
    assert item["replacement_legs"] == []

    current = client.get(f"/bookings/{order['booking_id']}").json()
    assert current["status"] == "unplaced"
    assert current["unplaced"] is True
    assert [l["run_id"] for l in current["legs"]] == [first]
    # It is not waiting and has no waitlist reason.
    assert current["waitlist_reason"] is None

    # The retained first leg can still be boarded.
    arr = client.post(
        f"/shuttle/runs/{first}/stops/0/arrival", json={"actual_min": 100}
    )
    assert arr.status_code == 200
    board_r = client.post(
        f"/shuttle/runs/{first}/stops/0/boarding",
        json={"booking_id": order["booking_id"], "actual_min": 101},
    )
    assert board_r.status_code == 200, board_r.text


def test_allocation_maximises_placements_then_application_order_preference(client):
    basic_network(client)
    target = make_run(client, [("A", 90, 91), ("D", 95, 95)], capacity=5)
    alt1 = make_run(client, [("A", 100, 101), ("D", 110, 110)], capacity=1)
    alt2 = make_run(client, [("A", 102, 103), ("D", 112, 112)], capacity=1)

    g1 = book(client, "g1", [{"run_id": target, "board_index": 0, "alight_index": 1}]).json()
    g2 = book(client, "g2", [{"run_id": target, "board_index": 0, "alight_index": 1}]).json()

    # Both list alt1 first; application order gives g1 that preference and g2
    # falls through to alt2, so both are placed.
    alts = [
        {
            "booking_id": g1["booking_id"],
            "options": [option((alt1, 0, 1)), option((alt2, 0, 1))],
        },
        {
            "booking_id": g2["booking_id"],
            "options": [option((alt1, 0, 1)), option((alt2, 0, 1))],
        },
    ]
    receipt = cancel(client, target, "prefs", alts).json()
    by_id = {x["booking_id"]: x for x in receipt["groups"]}
    assert by_id[g1["booking_id"]]["assigned_option_index"] == 0
    assert by_id[g2["booking_id"]]["assigned_option_index"] == 1
    assert receipt["placed_count"] == 2


def test_global_max_count_can_override_early_preference_greediness(client):
    basic_network(client)
    target = make_run(client, [("A", 80, 81), ("D", 90, 90)], capacity=5)
    shared = make_run(client, [("A", 100, 101), ("D", 110, 110)], capacity=1)
    g1_only = make_run(client, [("A", 102, 103), ("D", 112, 112)], capacity=1)

    g1 = book(client, "g1", [{"run_id": target, "board_index": 0, "alight_index": 1}]).json()
    g2 = book(client, "g2", [{"run_id": target, "board_index": 0, "alight_index": 1}]).json()

    # g1 can use shared or its own run.  Maximum count requires g1 to take its
    # own run, leaving shared for g2.  Preference index alone would greedily
    # give g1 shared and leave g2 unplaced.
    alts = [
        {
            "booking_id": g1["booking_id"],
            "options": [option((shared, 0, 1)), option((g1_only, 0, 1))],
        },
        {
            "booking_id": g2["booking_id"],
            "options": [option((shared, 0, 1))],
        },
    ]
    receipt = cancel(client, target, "max-count", alts).json()
    by_id = {x["booking_id"]: x for x in receipt["groups"]}
    assert receipt["placed_count"] == 2
    assert by_id[g1["booking_id"]]["replacement_legs"][0]["run_id"] == g1_only
    assert by_id[g2["booking_id"]]["replacement_legs"][0]["run_id"] == shared


def test_actual_alighting_controls_connection_feasibility(client):
    add_segment(client, "AH", "A", "H")
    add_segment(client, "HD", "H", "D")
    first = make_run(
        client, [("A", 100, 102), ("H", 110, 112)], capacity=1
    )
    dead = make_run(client, [("H", 115, 116), ("D", 125, 125)])
    rescue = make_run(client, [("H", 116, 117), ("D", 126, 126)])
    order = book(
        client,
        "x",
        [
            {"run_id": first, "board_index": 0, "alight_index": 1},
            {"run_id": dead, "board_index": 0, "alight_index": 1},
        ],
    ).json()

    # The first run reaches H late and auto-alights the group at 118.
    client.post(f"/shuttle/runs/{first}/stops/0/arrival", json={"actual_min": 100})
    client.post(
        f"/shuttle/runs/{first}/stops/0/boarding",
        json={"booking_id": order["booking_id"], "actual_min": 101},
    )
    client.post(f"/shuttle/runs/{first}/stops/0/departure", json={"actual_min": 102})
    arr = client.post(
        f"/shuttle/runs/{first}/stops/1/arrival", json={"actual_min": 118}
    )
    assert arr.status_code == 200

    bad = cancel(
        client,
        dead,
        "late-connection",
        [{"booking_id": order["booking_id"], "options": [option((rescue, 0, 1))]}],
    )
    # rescue planned departure 116 is before actual alighting 118.
    assert bad.status_code == 409
    assert bad.json()["reason"] == "transfer_not_verified"
    unchanged = client.get(f"/bookings/{order['booking_id']}").json()
    assert [leg["run_id"] for leg in unchanged["legs"]] == [first, dead]

    later = make_run(client, [("H", 120, 121), ("D", 130, 130)])
    ok = cancel(
        client,
        dead,
        "late-connection-ok",
        [{"booking_id": order["booking_id"], "options": [option((later, 0, 1))]}],
    )
    assert ok.status_code == 200, ok.text
    current = client.get(f"/bookings/{order['booking_id']}").json()
    assert [l["status"] for l in current["legs"]] == ["alighted", "reserved"]

    # On-site boarding still verifies the actual prior alighting time.
    client.post(f"/shuttle/runs/{later}/stops/0/arrival", json={"actual_min": 120})
    board = client.post(
        f"/shuttle/runs/{later}/stops/0/boarding",
        json={"booking_id": order["booking_id"], "actual_min": 120},
    )
    assert board.status_code == 200, board.text


def test_candidate_cannot_use_departed_boarding_stop(client):
    add_segment(client, "A", "H") if False else None
    add_segment(client, "AH2", "A", "H")
    add_segment(client, "HD2", "H", "D")
    target = make_run(client, [("A", 90, 91), ("H", 95, 96), ("D", 100, 100)])
    rescue = make_run(client, [("H", 100, 101), ("D", 110, 110)])
    order = book(
        client, "g", [{"run_id": target, "board_index": 0, "alight_index": 2}]
    ).json()

    # Rescue's H boarding stop has already departed.
    client.post(f"/shuttle/runs/{rescue}/stops/0/arrival", json={"actual_min": 100})
    client.post(f"/shuttle/runs/{rescue}/stops/0/departure", json={"actual_min": 101})

    r = cancel(
        client,
        target,
        "departed-candidate",
        [{"booking_id": order["booking_id"], "options": [option((rescue, 0, 1))]}],
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "departed"


def test_waitlists_for_cancelled_run_void_others_promote_after_release(client):
    add_segment(client, "AH", "A", "H")
    add_segment(client, "HD", "H", "D")
    add_segment(client, "AD", "A", "D")

    target = make_run(client, [("A", 90, 91), ("H", 100, 101)], capacity=1)
    connecting = make_run(client, [("H", 105, 106), ("D", 115, 115)], capacity=2)
    direct = make_run(client, [("A", 95, 96), ("D", 120, 120)], capacity=1)

    # Fill one of connecting's two spaces with an unaffected H->D group.
    anchor = book(
        client,
        "anchor",
        [{"run_id": connecting, "board_index": 0, "alight_index": 1}],
    )
    assert anchor.json()["status"] == "confirmed"
    affected = book(
        client,
        "affected",
        [
            {"run_id": target, "board_index": 0, "alight_index": 1},
            {"run_id": connecting, "board_index": 0, "alight_index": 1},
        ],
    ).json()
    waiting_target = book(
        client,
        "waiting-target",
        [{"run_id": target, "board_index": 0, "alight_index": 1}],
    )
    assert waiting_target.json()["status"] == "waiting"

    # connecting capacity 2: anchor(1)+affected(1) currently full.  This
    # waitlist does not contain the cancelled run and can promote once the old
    # suffix's connecting reservation is released.
    waiting_other = book(
        client,
        "waiting-other",
        [{"run_id": connecting, "board_index": 0, "alight_index": 1}],
    )
    assert waiting_other.json()["status"] == "waiting"

    response = cancel(
        client,
        target,
        "waitlist-cancel",
        [
            {
                "booking_id": affected["booking_id"],
                "options": [option((direct, 0, 1))],
            }
        ],
    )
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert waiting_target.json()["booking_id"] in receipt["voided_waitlist_ids"]

    assert (
        client.get(f"/bookings/{waiting_target.json()['booking_id']}").json()[
            "status"
        ]
        == "void"
    )
    promoted = client.get(
        f"/bookings/{waiting_other.json()['booking_id']}"
    ).json()
    assert promoted["status"] == "confirmed"


def test_later_cancellation_uses_current_arrangement_and_first_receipt_is_fixed(
    client,
):
    basic_network(client)
    first_run = make_run(client, [("A", 90, 91), ("D", 100, 100)])
    second_run = make_run(client, [("A", 110, 111), ("D", 120, 120)])
    third_run = make_run(client, [("A", 130, 131), ("D", 140, 140)])
    order = book(
        client, "g", [{"run_id": first_run, "board_index": 0, "alight_index": 1}]
    ).json()

    first_receipt = cancel(
        client,
        first_run,
        "op-1",
        [{"booking_id": order["booking_id"], "options": [option((second_run, 0, 1))]}],
    ).json()
    assert [l["run_id"] for l in first_receipt["groups"][0]["replacement_legs"]] == [
        second_run
    ]

    second_receipt = cancel(
        client,
        second_run,
        "op-2",
        [{"booking_id": order["booking_id"], "options": [option((third_run, 0, 1))]}],
    ).json()
    item = second_receipt["groups"][0]
    assert [l["run_id"] for l in item["removed_legs"]] == [second_run]
    assert [l["run_id"] for l in item["replacement_legs"]] == [third_run]

    # The first dispatcher receipt is immutable and still says second_run was
    # the replacement allocated at that time.
    again = client.get("/shuttle/cancellations/by-request/op-1").json()
    assert [l["run_id"] for l in again["groups"][0]["replacement_legs"]] == [
        second_run
    ]
    current = client.get(f"/bookings/{order['booking_id']}").json()
    assert [l["run_id"] for l in current["legs"]] == [third_run]


def test_restart_preserves_receipt_and_booking(tmp_path):
    db_file = tmp_path / "restart-cancel.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        basic_network(client)
        old = make_run(client, [("A", 90, 91), ("D", 100, 100)])
        new = make_run(client, [("A", 110, 111), ("D", 120, 120)])
        order = book(
            client, "g", [{"run_id": old, "board_index": 0, "alight_index": 1}]
        ).json()
        receipt = cancel(
            client,
            old,
            "restart-op",
            [{"booking_id": order["booking_id"], "options": [option((new, 0, 1))]}],
        ).json()
        cancel_id = receipt["cancellation_id"]

    app2 = create_app(db_file)
    with TestClient(app2) as client:
        got = client.get("/shuttle/cancellations/by-request/restart-op")
        assert got.status_code == 200
        assert got.json()["cancellation_id"] == cancel_id
        current = client.get(f"/bookings/{order['booking_id']}").json()
        assert [l["run_id"] for l in current["legs"]] == [new]
        replay = cancel(
            client,
            old,
            "restart-op",
            [{"booking_id": order["booking_id"], "options": [option((new, 0, 1))]}],
        )
        assert replay.status_code == 200
        assert replay.json()["cancellation_id"] == cancel_id

        # The replacement reservation remains actionable after restart.
        arrive_new = client.post(
            f"/shuttle/runs/{new}/stops/0/arrival", json={"actual_min": 110}
        )
        assert arrive_new.status_code == 200, arrive_new.text
        board_new = client.post(
            f"/shuttle/runs/{new}/stops/0/boarding",
            json={"booking_id": order["booking_id"], "actual_min": 111},
        )
        assert board_new.status_code == 200, board_new.text


def test_concurrent_cancellation_and_bookings_never_oversell(tmp_path):
    db_file = tmp_path / "concurrent-cancel.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        basic_network(client)
        target = make_run(client, [("A", 80, 81), ("D", 90, 90)], capacity=5)
        rescue = make_run(client, [("A", 100, 101), ("D", 110, 110)], capacity=1)
        g1 = book(
            client,
            "concurrent-g1",
            [{"run_id": target, "board_index": 0, "alight_index": 1}],
        ).json()

        barrier = threading.Barrier(3)
        results = []
        lock = threading.Lock()

        def do_cancel():
            barrier.wait()
            r = cancel(
                client,
                target,
                "concurrent-op",
                [
                    {
                        "booking_id": g1["booking_id"],
                        "options": [option((rescue, 0, 1))],
                    }
                ],
            )
            with lock:
                results.append(("cancel", r.status_code))

        def do_book(i):
            barrier.wait()
            r = book(
                client,
                f"race-{i}",
                [{"run_id": rescue, "board_index": 0, "alight_index": 1}],
            )
            with lock:
                results.append((f"book-{i}", r.status_code))

        threads = [
            threading.Thread(target=do_cancel),
            threading.Thread(target=do_book, args=(1,)),
            threading.Thread(target=do_book, args=(2,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(code < 500 for _, code in results)
        rescue_view = client.get(f"/shuttle/runs/{rescue}").json()
        assert rescue_view["intervals"][0]["occupied_wheelchairs"] <= 1
