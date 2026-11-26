"""Seed a small shuttle network and walk one complete booking/boarding flow.

Run with:  python3 -m app.demo_shuttle [db-path]

The script registers two runs that meet at a shared transfer stop, books a
two-leg wheelchair transfer, then drives both runs stop-by-stop so the final
manifest shows the group completing the whole guaranteed journey.
"""
from __future__ import annotations

import sys
import tempfile

from fastapi.testclient import TestClient

from .main import create_app


def _segment(client, sid, a, b):
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


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

    with TestClient(create_app(path)) as client:
        # Accessible segments back every consecutive run-stop pair.
        _segment(client, "R1A", "Gate", "Hub")
        _segment(client, "R1B", "Hub", "Pier")
        _segment(client, "R2A", "Hub", "Dock")

        run1 = client.post(
            "/shuttle/runs",
            json={
                "wheelchair_capacity": 2,
                "stops": [
                    {"stop": "Gate", "arrive_min": 100, "depart_min": 102},
                    {"stop": "Hub", "arrive_min": 110, "depart_min": 112},
                    {"stop": "Pier", "arrive_min": 120, "depart_min": 120},
                ],
            },
        ).json()["run_id"]
        run2 = client.post(
            "/shuttle/runs",
            json={
                "wheelchair_capacity": 2,
                "stops": [
                    {"stop": "Hub", "arrive_min": 115, "depart_min": 117},
                    {"stop": "Dock", "arrive_min": 130, "depart_min": 130},
                ],
            },
        ).json()["run_id"]

        booking = client.post(
            "/bookings",
            json={
                "request_id": "demo-transfer-1",
                "group_name": "Demo group",
                "wheelchair_count": 1,
                "legs": [
                    {"run_id": run1, "board_index": 0, "alight_index": 1},
                    {"run_id": run2, "board_index": 0, "alight_index": 1},
                ],
            },
        )
        print("booking:", booking.status_code, booking.json()["status"])
        bid = booking.json()["booking_id"]

        # Drive run 1: board at Gate, alight at Hub.
        client.post(f"/shuttle/runs/{run1}/stops/0/arrival", json={"actual_min": 100})
        client.post(
            f"/shuttle/runs/{run1}/stops/0/boarding",
            json={"booking_id": bid, "actual_min": 101},
        )
        client.post(f"/shuttle/runs/{run1}/stops/0/departure", json={"actual_min": 102})
        client.post(f"/shuttle/runs/{run1}/stops/1/arrival", json={"actual_min": 110})

        # Drive run 2: the transfer is verified against the recorded 110 alight.
        client.post(f"/shuttle/runs/{run2}/stops/0/arrival", json={"actual_min": 115})
        client.post(
            f"/shuttle/runs/{run2}/stops/0/boarding",
            json={"booking_id": bid, "actual_min": 116},
        )
        client.post(f"/shuttle/runs/{run2}/stops/0/departure", json={"actual_min": 117})
        end = client.post(
            f"/shuttle/runs/{run2}/stops/1/arrival", json={"actual_min": 130}
        ).json()
        print("run2 finished:", end["finished"])
        print("final booking:", client.get(f"/bookings/{bid}").json()["status"])
        print("run2 manifest:", client.get(f"/shuttle/runs/{run2}/manifest").json())


if __name__ == "__main__":
    main()
