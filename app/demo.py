"""Seed a small demo exhibition network and print a sample itinerary.

Run with:  python3 -m app.demo [db-path]
"""
from __future__ import annotations

import sys
import tempfile

from fastapi.testclient import TestClient

from .main import create_app


def seed(client: TestClient) -> None:
    def segment(sid, a, b, minutes, windows, accessible=True):
        r = client.post(
            "/segments",
            json={
                "id": sid,
                "from_stop": a,
                "to_stop": b,
                "minutes": minutes,
                "windows": [
                    {"open_min": o, "close_min": c} for o, c in windows
                ],
                "wheelchair_accessible": accessible,
            },
        )
        assert r.status_code == 201, r.text

    # Two first legs leave the entrance at the same minute.  The red shuttle
    # (R1) reaches its interchange earlier, but the onward R2 leaves too late
    # to honour the 10-minute wait cap; the blue line connects.
    segment("R1", "Entrance", "NorthPier", 5, [(600, 1080)])
    segment("R2", "NorthPier", "HallA", 6, [(620, 1080)])   # 605 -> 620 is 15 min
    segment("B1", "Entrance", "SouthPier", 8, [(600, 1080)])
    segment("B2", "SouthPier", "HallA", 4, [(615, 1080)])   # 608 -> 615 is 7 min

    # A longer accessible detour from HallA onward.
    segment("A1", "HallA", "HallC", 7, [(600, 1080)])
    segment("C1", "HallC", "Exit", 6, [(600, 1080)])
    segment("D1", "HallA", "HallB", 12, [(600, 1080)])
    segment("D2", "HallB", "Exit", 9, [(600, 1080)])


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    app = create_app(path)
    with TestClient(app) as client:
        seed(client)
        plan = client.post(
            "/plans",
            json={
                "origin": "Entrance",
                "destination": "Exit",
                "depart_min": 600,
                "arrive_by_min": 720,
                "max_wait_min": 10,
            },
        )
        print("plan:", plan.status_code)
        print(plan.json())


if __name__ == "__main__":
    main()
