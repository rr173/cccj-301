"""SQLite persistence for the network, closures, and live trips."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .model import Leg, Window
from .network import Network, Segment

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    from_stop TEXT NOT NULL,
    to_stop TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK (minutes > 0),
    wheelchair_accessible INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS segment_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    open_min INTEGER NOT NULL,
    close_min INTEGER NOT NULL,
    CHECK (close_min > open_min)
);
CREATE TABLE IF NOT EXISTS closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    CHECK (end_min > start_min)
);
CREATE INDEX IF NOT EXISTS idx_closures_segment ON closures(segment_id, active);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    requested_depart_min INTEGER NOT NULL,
    arrive_by_min INTEGER NOT NULL,
    max_wait_min INTEGER NOT NULL,
    anchor_stop TEXT NOT NULL,
    anchor_min INTEGER NOT NULL,
    last_actual_min INTEGER NOT NULL,
    solved INTEGER NOT NULL,
    note TEXT,
    revisions INTEGER NOT NULL DEFAULT 0,
    -- closure_version at which this trip's tail was last validated
    checked_version INTEGER NOT NULL DEFAULT -1
);
CREATE TABLE IF NOT EXISTS trip_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    walked INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    from_stop TEXT NOT NULL,
    to_stop TEXT NOT NULL,
    depart_min INTEGER NOT NULL,
    arrive_min INTEGER NOT NULL,
    UNIQUE (trip_id, walked, position)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('closure_version', 0);
"""


def _leg_from_row(row: sqlite3.Row) -> Leg:
    return Leg(
        segment_id=row["segment_id"],
        from_stop=row["from_stop"],
        to_stop=row["to_stop"],
        depart_min=row["depart_min"],
        arrive_min=row["arrive_min"],
    )


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    # ---------------------------------------------------------------- segments
    def add_segment(self, seg: Segment) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO segments(id, from_stop, to_stop, minutes,"
                " wheelchair_accessible) VALUES (?,?,?,?,?)",
                (
                    seg.id,
                    seg.from_stop,
                    seg.to_stop,
                    seg.minutes,
                    int(seg.wheelchair_accessible),
                ),
            )
            self._conn.executemany(
                "INSERT INTO segment_windows(segment_id, open_min, close_min)"
                " VALUES (?,?,?)",
                [(seg.id, w.open_min, w.close_min) for w in seg.windows],
            )

    def list_segments(self) -> list[Segment]:
        with self.lock:
            rows = self._conn.execute("SELECT * FROM segments ORDER BY id").fetchall()
            windows = self._conn.execute(
                "SELECT * FROM segment_windows ORDER BY open_min"
            ).fetchall()
        by_segment: dict[str, list[Window]] = {}
        for row in windows:
            by_segment.setdefault(row["segment_id"], []).append(
                Window(open_min=row["open_min"], close_min=row["close_min"])
            )
        return [
            Segment(
                id=row["id"],
                from_stop=row["from_stop"],
                to_stop=row["to_stop"],
                minutes=row["minutes"],
                windows=tuple(by_segment.get(row["id"], [])),
                wheelchair_accessible=bool(row["wheelchair_accessible"]),
            )
            for row in rows
        ]

    def segment_exists(self, segment_id: str) -> bool:
        with self.lock:
            row = self._conn.execute(
                "SELECT 1 FROM segments WHERE id = ?", (segment_id,)
            ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- closures
    def add_closure(self, segment_id: str, start_min: int, end_min: int) -> int:
        with self.lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO closures(segment_id, start_min, end_min, active)"
                " VALUES (?,?,?,1)",
                (segment_id, start_min, end_min),
            )
            return int(cur.lastrowid)

    def lift_closure(self, closure_id: int) -> bool:
        with self.lock, self._conn:
            cur = self._conn.execute(
                "UPDATE closures SET active = 0 WHERE id = ? AND active = 1",
                (closure_id,),
            )
            return cur.rowcount > 0

    def list_closures(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM closures"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id"
        with self.lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            {
                "id": row["id"],
                "segment_id": row["segment_id"],
                "start_min": row["start_min"],
                "end_min": row["end_min"],
            }
            for row in rows
        ]

    # ---------------------------------------------------------------- network
    def load_network(self) -> Network:
        with self.lock:
            closures = [
                (row["segment_id"], row["start_min"], row["end_min"])
                for row in self._conn.execute(
                    "SELECT * FROM closures WHERE active = 1"
                ).fetchall()
            ]
        return Network.build(self.list_segments(), closures)

    def closure_version(self) -> int:
        with self.lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'closure_version'"
            ).fetchone()
        return int(row["value"])

    def bump_closure_version(self) -> int:
        with self.lock, self._conn:
            self._conn.execute(
                "UPDATE meta SET value = value + 1 WHERE key = 'closure_version'"
            )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'closure_version'"
            ).fetchone()
        return int(row["value"])

    # ------------------------------------------------------------------- trips
    def insert_trip(self, trip) -> int:
        with self.lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO trips(origin, destination, requested_depart_min,"
                " arrive_by_min, max_wait_min, anchor_stop, anchor_min,"
                " last_actual_min, solved, note, revisions, checked_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trip.origin,
                    trip.destination,
                    trip.requested_depart_min,
                    trip.arrive_by_min,
                    trip.max_wait_min,
                    trip.anchor_stop,
                    trip.anchor_min,
                    trip.last_actual_min,
                    int(trip.solved),
                    trip.note,
                    trip.revisions,
                    trip.checked_version,
                ),
            )
            return int(cur.lastrowid)

    def save_trip(self, trip) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "UPDATE trips SET anchor_stop=?, anchor_min=?, last_actual_min=?,"
                " solved=?, note=?, revisions=?, checked_version=? WHERE id=?",
                (
                    trip.anchor_stop,
                    trip.anchor_min,
                    trip.last_actual_min,
                    int(trip.solved),
                    trip.note,
                    trip.revisions,
                    trip.checked_version,
                    trip.id,
                ),
            )
            self._conn.execute("DELETE FROM trip_legs WHERE trip_id=?", (trip.id,))
            self._conn.executemany(
                "INSERT INTO trip_legs(trip_id, position, walked, segment_id,"
                " from_stop, to_stop, depart_min, arrive_min)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        trip.id,
                        i,
                        1,
                        leg.segment_id,
                        leg.from_stop,
                        leg.to_stop,
                        leg.depart_min,
                        leg.arrive_min,
                    )
                    for i, leg in enumerate(trip.history)
                ]
                + [
                    (
                        trip.id,
                        i,
                        0,
                        leg.segment_id,
                        leg.from_stop,
                        leg.to_stop,
                        leg.depart_min,
                        leg.arrive_min,
                    )
                    for i, leg in enumerate(trip.legs)
                ],
            )

    def load_trip(self, trip_id: int):
        # Imported here to avoid a module cycle with trips.py.
        from .trips import Trip

        with self.lock:
            row = self._conn.execute(
                "SELECT * FROM trips WHERE id=?", (trip_id,)
            ).fetchone()
            if row is None:
                return None
            legs = self._conn.execute(
                "SELECT * FROM trip_legs WHERE trip_id=? ORDER BY walked, position",
                (trip_id,),
            ).fetchall()
        return Trip(
            id=row["id"],
            origin=row["origin"],
            destination=row["destination"],
            requested_depart_min=row["requested_depart_min"],
            arrive_by_min=row["arrive_by_min"],
            max_wait_min=row["max_wait_min"],
            anchor_stop=row["anchor_stop"],
            anchor_min=row["anchor_min"],
            legs=[_leg_from_row(r) for r in legs if r["walked"] == 0],
            history=[_leg_from_row(r) for r in legs if r["walked"] == 1],
            solved=bool(row["solved"]),
            note=row["note"],
            revisions=row["revisions"],
            last_actual_min=row["last_actual_min"],
            checked_version=row["checked_version"],
        )

    def all_trip_ids(self) -> list[int]:
        with self.lock:
            rows = self._conn.execute("SELECT id FROM trips ORDER BY id").fetchall()
        return [row["id"] for row in rows]
