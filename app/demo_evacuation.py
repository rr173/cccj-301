"""Seed an evacuation incident and walk one complete headcount/closure flow.

Run with:  python3 -m app.demo_evacuation [db-path]

The script opens an incident over three halls and two safe assembly points,
registers visitors, finds an initially unlocated one, moves people with
explicit handover manifests (including a partial receive and a turn-back),
searches and clears each empty zone, and finally closes the incident.  It uses
only evacuation records: routing plans and shuttle bookings are never treated
as evidence that anyone reached safety.
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
        # Stops used as zone / safe-point names must already exist.
        for sid, a, b in [
            ("E1", "HallA", "HallB"),
            ("E2", "HallB", "HallC"),
            ("E3", "HallC", "Plaza"),
            ("E4", "HallA", "Gate"),
        ]:
            _segment(client, sid, a, b)

        incident = client.post(
            "/evac/incidents",
            json={
                "request_id": "demo-incident",
                "name": "Hall evacuation drill",
                "zones": ["HallA", "HallB", "HallC"],
                "safe_points": ["Plaza", "Gate"],
            },
        ).json()
        iid = incident["incident_id"]
        print("incident opened:", iid, incident["zones"], "->", incident["safe_points"])

        # Two located visitors and one whose spot still has to be verified.
        client.post(
            f"/evac/incidents/{iid}/people",
            json={
                "request_id": "register-1",
                "people": [
                    {"person_no": "V001", "zone": "HallA"},
                    {"person_no": "V002", "zone": "HallA"},
                    {"person_no": "V003"},
                ],
            },
        )
        found = client.post(
            f"/evac/incidents/{iid}/findings",
            json={"request_id": "find-v003", "person_no": "V003", "location": "HallB"},
        )
        print("V003 found:", found.json()["people"][-1]["status"])

        # HallA sends both visitors toward Plaza on one manifest.
        first = client.post(
            f"/evac/incidents/{iid}/handovers",
            json={
                "request_id": "handover-1",
                "from_zone": "HallA",
                "to_location": "Plaza",
                "person_nos": ["V001", "V002"],
            },
        ).json()
        hid = first["handovers"][-1]["handover_id"]

        # Plaza confirms only V001; V002 is still in transit.
        client.post(
            f"/evac/incidents/{iid}/handovers/{hid}/receive",
            json={"request_id": "receive-1", "person_nos": ["V001"]},
        )
        # V002 turns back; HallA confirms the return.
        client.post(
            f"/evac/incidents/{iid}/handovers/{hid}/return",
            json={"request_id": "return-1", "person_nos": ["V002"]},
        )
        # V002 goes out again, this time all the way to safety.
        second = client.post(
            f"/evac/incidents/{iid}/handovers",
            json={
                "request_id": "handover-2",
                "from_zone": "HallA",
                "to_location": "Gate",
                "person_nos": ["V002"],
            },
        ).json()
        hid2 = second["handovers"][-1]["handover_id"]
        client.post(
            f"/evac/incidents/{iid}/handovers/{hid2}/receive",
            json={"request_id": "receive-2", "person_nos": ["V002"]},
        )

        # V003 walks HallB -> HallC -> Plaza with explicit handovers.
        h3 = client.post(
            f"/evac/incidents/{iid}/handovers",
            json={
                "request_id": "handover-3",
                "from_zone": "HallB",
                "to_location": "HallC",
                "person_nos": ["V003"],
            },
        ).json()["handovers"][-1]["handover_id"]
        client.post(
            f"/evac/incidents/{iid}/handovers/{h3}/receive",
            json={"request_id": "receive-3", "person_nos": ["V003"]},
        )
        h4 = client.post(
            f"/evac/incidents/{iid}/handovers",
            json={
                "request_id": "handover-4",
                "from_zone": "HallC",
                "to_location": "Plaza",
                "person_nos": ["V003"],
            },
        ).json()["handovers"][-1]["handover_id"]
        client.post(
            f"/evac/incidents/{iid}/handovers/{h4}/receive",
            json={"request_id": "receive-4", "person_nos": ["V003"]},
        )

        # Every zone is now empty: search, then confirm clearance.
        for zone in ("HallA", "HallB", "HallC"):
            client.post(
                f"/evac/incidents/{iid}/zones/{zone}/search",
                json={"request_id": f"search-{zone}"},
            )
            client.post(
                f"/evac/incidents/{iid}/zones/{zone}/clearance",
                json={"request_id": f"clear-{zone}"},
            )

        ledger = client.get(f"/evac/incidents/{iid}").json()
        print(
            "safe:",
            ledger["people_safe"],
            "/",
            ledger["people_total"],
            "| ready:",
            ledger["ready_to_close"],
        )
        closed = client.post(
            f"/evac/incidents/{iid}/close",
            json={"request_id": "close-1"},
        )
        print("closed:", closed.json()["status"])
        print("safe point occupants:", ledger["safe_occupants"] | closed.json()["safe_occupants"])
        print("final handovers:", len(closed.json()["handovers"]))


if __name__ == "__main__":
    main()
