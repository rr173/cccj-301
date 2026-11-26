"""Randomised end-to-end invariant checks over the HTTP API."""
from __future__ import annotations

import random

from fastapi.testclient import TestClient

from app.main import create_app


def test_random_mutations_keep_plans_consistent(tmp_path):
    rng = random.Random(42)
    app = create_app(tmp_path / "random.db")
    with TestClient(app) as client:
        stops = [f"S{i}" for i in range(8)]
        created = []
        for i in range(14):
            a, b = rng.sample(stops, 2)
            sid = f"E{i:02d}"
            created.append(sid)
            r = client.post(
                "/segments",
                json={
                    "id": sid,
                    "from_stop": a,
                    "to_stop": b,
                    "minutes": rng.randint(1, 6),
                    "windows": [{"open_min": 0, "close_min": 200}],
                    "wheelchair_accessible": rng.random() > 0.1,
                },
            )
            assert r.status_code == 201

        trips = []
        for _ in range(40):
            o, d = rng.sample(stops, 2)
            dep = rng.randint(0, 100)
            r = client.post(
                "/plans",
                json={
                    "origin": o,
                    "destination": d,
                    "depart_min": dep,
                    "arrive_by_min": dep + 90,
                    "max_wait_min": rng.randint(1, 40),
                },
            )
            assert r.status_code in (201, 422)
            if r.status_code == 201:
                trips.append(r.json()["trip_id"])
        assert trips

        for _ in range(80):
            trip_id = rng.choice(trips)
            if rng.random() < 0.5:
                sid = rng.choice(created)
                start = rng.randint(0, 180)
                r = client.post(
                    "/closures",
                    json={
                        "segment_id": sid,
                        "start_min": start,
                        "end_min": start + rng.randint(1, 20),
                    },
                )
                assert r.status_code == 201
            else:
                journey = client.get(f"/trips/{trip_id}").json()
                upcoming = journey["upcoming_leg"]
                if upcoming:
                    arrival = max(upcoming["arrive_min"], journey["anchor_min"])
                    r = client.post(
                        f"/trips/{trip_id}/confirm",
                        json={
                            "segment_id": upcoming["segment_id"],
                            "arrival_min": arrival,
                        },
                    )
                    assert r.status_code == 200

            journey = client.get(f"/trips/{trip_id}").json()
            legs = journey["legs"]
            if legs:
                # The remaining plan starts exactly where the visitor is.
                assert legs[0]["from_stop"] == journey["anchor_stop"]
                t = journey["anchor_min"]
                for leg in legs:
                    assert leg["depart_min"] >= t
                    assert leg["depart_min"] - t <= journey["max_wait_min"]
                    t = leg["arrive_min"]
                assert t <= journey["arrive_by_min"]
                assert journey["arrival_min"] == t
            # Walked legs are never shown again as something still to do.
            walked = {leg["segment_id"] for leg in journey["history"]}
            for leg in legs:
                assert leg["segment_id"] not in walked
