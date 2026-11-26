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

-- ------------------------------------------------------------ shuttle runs
CREATE TABLE IF NOT EXISTS shuttle_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wheelchair_capacity INTEGER NOT NULL CHECK (wheelchair_capacity > 0),
    finished INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shuttle_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES shuttle_runs(id) ON DELETE CASCADE,
    stop_index INTEGER NOT NULL,
    stop_name TEXT NOT NULL,
    planned_arrive INTEGER NOT NULL,
    planned_depart INTEGER NOT NULL,
    actual_arrive INTEGER,
    actual_depart INTEGER,
    UNIQUE (run_id, stop_index)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Application order for waitlist promotion: the autoincrement id already
    -- reflects submission order, and request_id enforces idempotency.
    request_id TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    group_name TEXT,
    wheelchair_count INTEGER NOT NULL CHECK (wheelchair_count > 0),
    -- waiting = waitlisted (holds no space); confirmed = at least one leg
    -- still live; cancelled = visitor cancel; void = missed a boarding;
    -- completed = every leg alighted.
    status TEXT NOT NULL CHECK (
        status IN ('waiting', 'confirmed', 'cancelled', 'void', 'completed')
    )
);
CREATE TABLE IF NOT EXISTS booking_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    leg_position INTEGER NOT NULL,
    run_id INTEGER NOT NULL REFERENCES shuttle_runs(id) ON DELETE CASCADE,
    board_index INTEGER NOT NULL,
    alight_index INTEGER NOT NULL,
    -- reserved = holding space but not yet onboard; boarded = onboard now;
    -- alighted = ride finished at the planned stop; void = released (no-show).
    status TEXT NOT NULL CHECK (status IN ('reserved', 'boarded', 'alighted', 'void')),
    actual_board_min INTEGER,
    actual_alight_min INTEGER,
    UNIQUE (booking_id, leg_position)
);
CREATE INDEX IF NOT EXISTS idx_legs_run ON booking_legs(run_id, status);

-- ============================================================ evacuation
CREATE TABLE IF NOT EXISTS evac_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed'))
);
CREATE TABLE IF NOT EXISTS evac_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES evac_incidents(id) ON DELETE CASCADE,
    stop_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('zone', 'safe_point')),
    UNIQUE (incident_id, stop_name)
);
CREATE TABLE IF NOT EXISTS evac_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES evac_incidents(id) ON DELETE CASCADE,
    person_no TEXT NOT NULL,
    -- unknown = location to be verified; in_zone; in_transit; safe.
    status TEXT NOT NULL CHECK (
        status IN ('unknown', 'in_zone', 'in_transit', 'safe')
    ),
    zone TEXT,
    safe_point TEXT,
    current_handover_id INTEGER,
    UNIQUE (incident_id, person_no)
);
CREATE TABLE IF NOT EXISTS evac_handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES evac_incidents(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    from_zone TEXT NOT NULL,
    to_location TEXT NOT NULL,
    destination_kind TEXT NOT NULL
        CHECK (destination_kind IN ('zone', 'safe_point')),
    -- open while any person on it is still pending; complete otherwise.
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'complete'))
);
CREATE TABLE IF NOT EXISTS evac_handover_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id INTEGER NOT NULL
        REFERENCES evac_handovers(id) ON DELETE CASCADE,
    person_no TEXT NOT NULL,
    -- pending = in transit; received = receiver confirmed; returned = sender
    -- confirmed the person came back to the originating zone.
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'received', 'returned')),
    UNIQUE (handover_id, person_no)
);
CREATE INDEX IF NOT EXISTS idx_hp_person ON evac_handover_people(person_no, status);
-- Per-zone activity generation.  A search/clearance is valid only while the
-- version captured when the search started still matches.
CREATE TABLE IF NOT EXISTS evac_zone_versions (
    incident_id INTEGER NOT NULL REFERENCES evac_incidents(id) ON DELETE CASCADE,
    zone TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (incident_id, zone)
);
CREATE TABLE IF NOT EXISTS evac_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES evac_incidents(id) ON DELETE CASCADE,
    zone TEXT NOT NULL,
    started_version INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    valid INTEGER NOT NULL DEFAULT 1,
    cleared INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_searches_zone
    ON evac_searches(incident_id, zone, active, valid);
-- Idempotency ledger, scoped inside one incident.  A replay returns the stored
-- response verbatim; a reused request id with different content is rejected.
CREATE TABLE IF NOT EXISTS evac_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES evac_incidents(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    UNIQUE (incident_id, request_id)
);
"""

from contextlib import contextmanager


@contextmanager
def transaction(conn):
    """One SQLite transaction across several writes.

    The connection's own ``with conn:`` blocks cannot be nested: an inner block
    commits on exit, which would leave a multi-leg reservation half-written.
    Service code opens one of these around the whole operation; the individual
    persistence helpers assume the caller already holds the transaction, and
    any exception rolls back every statement issued inside the block.
    """
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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

    # ------------------------------------------------------------- transactions
    def begin(self):
        """One atomic write transaction for multi-table service operations."""
        return transaction(self._conn)

    # ---------------------------------------------------------- shuttle runs
    def insert_run(self, stops: list[dict], capacity: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO shuttle_runs(wheelchair_capacity) VALUES (?)",
            (capacity,),
        )
        run_id = int(cur.lastrowid)
        self._conn.executemany(
            "INSERT INTO shuttle_stops(run_id, stop_index, stop_name,"
            " planned_arrive, planned_depart) VALUES (?,?,?,?,?)",
            [
                (
                    run_id,
                    i,
                    s["stop"],
                    s["arrive_min"],
                    s["depart_min"],
                )
                for i, s in enumerate(stops)
            ],
        )
        return run_id

    def get_run(self, run_id: int):
        row = self._conn.execute(
            "SELECT * FROM shuttle_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        stops = self._conn.execute(
            "SELECT * FROM shuttle_stops WHERE run_id=? ORDER BY stop_index",
            (run_id,),
        ).fetchall()
        return {"run": row, "stops": stops}

    def list_runs(self) -> list[int]:
        rows = self._conn.execute("SELECT id FROM shuttle_runs ORDER BY id").fetchall()
        return [row["id"] for row in rows]

    def set_stop_arrival(self, run_id: int, index: int, actual_min: int) -> None:
        self._conn.execute(
            "UPDATE shuttle_stops SET actual_arrive=? WHERE run_id=? AND stop_index=?",
            (actual_min, run_id, index),
        )

    def set_stop_departure(self, run_id: int, index: int, actual_min: int) -> None:
        self._conn.execute(
            "UPDATE shuttle_stops SET actual_depart=? WHERE run_id=? AND stop_index=?",
            (actual_min, run_id, index),
        )

    def finish_run(self, run_id: int) -> None:
        self._conn.execute(
            "UPDATE shuttle_runs SET finished=1 WHERE id=?", (run_id,)
        )

    # -------------------------------------------------------------- bookings
    def get_request(self, request_id: str):
        return self._conn.execute(
            "SELECT * FROM bookings WHERE request_id=?", (request_id,)
        ).fetchone()

    def insert_booking(
        self,
        request_id: str,
        content_hash: str,
        group_name: str | None,
        wheelchair_count: int,
        status: str,
        legs: list[dict],
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO bookings(request_id, content_hash, group_name,"
            " wheelchair_count, status) VALUES (?,?,?,?,?)",
            (request_id, content_hash, group_name, wheelchair_count, status),
        )
        booking_id = int(cur.lastrowid)
        # Waitlisted orders hold no space: their legs start as 'void' and are
        # promoted to 'reserved' atomically when the whole order is confirmed.
        leg_status = "reserved" if status == "confirmed" else "void"
        self._conn.executemany(
            "INSERT INTO booking_legs(booking_id, leg_position, run_id, board_index,"
            " alight_index, status) VALUES (?,?,?,?,?,?)",
            [
                (
                    booking_id,
                    i,
                    leg["run_id"],
                    leg["board_index"],
                    leg["alight_index"],
                    leg_status,
                )
                for i, leg in enumerate(legs)
            ],
        )
        return booking_id

    def get_booking(self, booking_id: int):
        row = self._conn.execute(
            "SELECT * FROM bookings WHERE id=?", (booking_id,)
        ).fetchone()
        if row is None:
            return None
        legs = self._conn.execute(
            "SELECT * FROM booking_legs WHERE booking_id=? ORDER BY leg_position",
            (booking_id,),
        ).fetchall()
        return {"booking": row, "legs": legs}

    def list_waiting_bookings(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT id FROM bookings WHERE status='waiting' ORDER BY id"
        ).fetchall()
        return [row["id"] for row in rows]

    def set_booking_status(self, booking_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE bookings SET status=? WHERE id=?", (status, booking_id)
        )

    def promote_waiting_legs(self, booking_id: int) -> None:
        # Atomically turn the whole order's placeholder legs into real
        # reservations; only a fully confirmable order is promoted.
        self._conn.execute(
            "UPDATE booking_legs SET status='reserved'"
            " WHERE booking_id=? AND status='void'",
            (booking_id,),
        )

    def find_leg(self, booking_id: int, position: int):
        return self._conn.execute(
            "SELECT * FROM booking_legs WHERE booking_id=? AND leg_position=?",
            (booking_id, position),
        ).fetchone()

    def active_legs_for_run(self, run_id: int) -> list:
        # reserved + boarded are the only statuses that hold wheelchair space.
        return self._conn.execute(
            "SELECT bl.*, b.wheelchair_count FROM booking_legs bl"
            " JOIN bookings b ON b.id = bl.booking_id"
            " WHERE bl.run_id=? AND bl.status IN ('reserved', 'boarded')",
            (run_id,),
        ).fetchall()

    def all_boarded_legs(self, run_id: int) -> list:
        return self._conn.execute(
            "SELECT * FROM booking_legs WHERE run_id=? AND status='boarded'",
            (run_id,),
        ).fetchall()

    def legs_alighting_at_stop(self, run_id: int, index: int) -> list:
        return self._conn.execute(
            "SELECT * FROM booking_legs WHERE run_id=? AND alight_index=?"
            " AND status='boarded'",
            (run_id, index),
        ).fetchall()

    def legs_reserved_at_stop(self, run_id: int, index: int) -> list:
        return self._conn.execute(
            "SELECT * FROM booking_legs WHERE run_id=? AND board_index=?"
            " AND status='reserved'",
            (run_id, index),
        ).fetchall()

    def set_leg_boarded(self, leg_id: int, actual_min: int) -> None:
        self._conn.execute(
            "UPDATE booking_legs SET status='boarded', actual_board_min=?"
            " WHERE id=?",
            (actual_min, leg_id),
        )

    def set_leg_alighted(self, leg_id: int, actual_min: int) -> None:
        self._conn.execute(
            "UPDATE booking_legs SET status='alighted', actual_alight_min=?"
            " WHERE id=?",
            (actual_min, leg_id),
        )

    def set_leg_void(self, leg_id: int) -> None:
        self._conn.execute(
            "UPDATE booking_legs SET status='void' WHERE id=?", (leg_id,)
        )

    def waiting_legs_boarding_at_stop(self, run_id: int, index: int) -> list:
        return self._conn.execute(
            "SELECT bl.* FROM booking_legs bl"
            " JOIN bookings b ON b.id = bl.booking_id"
            " WHERE bl.run_id=? AND bl.board_index=? AND b.status='waiting'",
            (run_id, index),
        ).fetchall()

    def ridden_legs_for_run(self, run_id: int) -> list:
        """Legs that have physically touched this run, oldest boarding first."""
        return self._conn.execute(
            "SELECT bl.*, b.request_id, b.group_name, b.wheelchair_count"
            " FROM booking_legs bl JOIN bookings b ON b.id = bl.booking_id"
            " WHERE bl.run_id=? AND bl.status IN ('boarded', 'alighted')"
            " ORDER BY COALESCE(bl.actual_board_min, 1 << 30), bl.id",
            (run_id,),
        ).fetchall()

    # ============================================================ evacuation
    def stop_name_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM segments WHERE from_stop=? OR to_stop=? LIMIT 1",
            (name, name),
        ).fetchone()
        return row is not None

    def insert_incident(self, request_id: str, name: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO evac_incidents(request_id, name) VALUES (?,?)",
            (request_id, name),
        )
        return int(cur.lastrowid)

    def get_incident_by_request(self, request_id: str):
        return self._conn.execute(
            "SELECT * FROM evac_incidents WHERE request_id=?", (request_id,)
        ).fetchone()

    def get_incident(self, incident_id: int):
        return self._conn.execute(
            "SELECT * FROM evac_incidents WHERE id=?", (incident_id,)
        ).fetchone()

    def list_incident_ids(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT id FROM evac_incidents ORDER BY id"
        ).fetchall()
        return [row["id"] for row in rows]

    def insert_location(self, incident_id: int, stop_name: str, kind: str) -> None:
        self._conn.execute(
            "INSERT INTO evac_locations(incident_id, stop_name, kind)"
            " VALUES (?,?,?)",
            (incident_id, stop_name, kind),
        )

    def incident_locations(self, incident_id: int) -> list:
        return self._conn.execute(
            "SELECT * FROM evac_locations WHERE incident_id=? ORDER BY id",
            (incident_id,),
        ).fetchall()

    def init_zone_versions(self, incident_id: int, zones: list[str]) -> None:
        self._conn.executemany(
            "INSERT INTO evac_zone_versions(incident_id, zone, version)"
            " VALUES (?,?,0)",
            [(incident_id, zone) for zone in zones],
        )

    def bump_zone_version(self, incident_id: int, zone: str) -> None:
        # Any person entry/leave or handover change touching a zone advances its
        # generation, which invalidates the currently running search.
        self._conn.execute(
            "INSERT INTO evac_zone_versions(incident_id, zone, version)"
            " VALUES (?,?,1)"
            " ON CONFLICT(incident_id, zone) DO UPDATE SET version = version + 1",
            (incident_id, zone),
        )

    def zone_version(self, incident_id: int, zone: str) -> int:
        row = self._conn.execute(
            "SELECT version FROM evac_zone_versions"
            " WHERE incident_id=? AND zone=?",
            (incident_id, zone),
        ).fetchone()
        return int(row["version"]) if row is not None else 0

    def invalidate_zone_searches(self, incident_id: int, zone: str) -> None:
        # A search whose generation changed can neither confirm nor keep a
        # previous clearance alive.
        self._conn.execute(
            "UPDATE evac_searches SET valid=0 WHERE incident_id=? AND zone=?"
            " AND valid=1",
            (incident_id, zone),
        )

    def insert_person(
        self,
        incident_id: int,
        person_no: str,
        status: str,
        zone: str | None,
        safe_point: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO evac_people(incident_id, person_no, status, zone,"
            " safe_point, current_handover_id) VALUES (?,?,?,?,?,NULL)",
            (incident_id, person_no, status, zone, safe_point),
        )

    def get_person(self, incident_id: int, person_no: str):
        return self._conn.execute(
            "SELECT * FROM evac_people WHERE incident_id=? AND person_no=?",
            (incident_id, person_no),
        ).fetchone()

    def list_people(self, incident_id: int) -> list:
        return self._conn.execute(
            "SELECT * FROM evac_people WHERE incident_id=? ORDER BY id",
            (incident_id,),
        ).fetchall()

    def set_person_known(
        self, incident_id: int, person_no: str, status: str,
        zone: str | None, safe_point: str | None,
    ) -> None:
        self._conn.execute(
            "UPDATE evac_people SET status=?, zone=?, safe_point=?,"
            " current_handover_id=NULL WHERE incident_id=? AND person_no=?",
            (status, zone, safe_point, incident_id, person_no),
        )

    def set_person_transit(
        self, incident_id: int, person_no: str, handover_id: int
    ) -> None:
        self._conn.execute(
            "UPDATE evac_people SET status='in_transit', zone=NULL,"
            " safe_point=NULL, current_handover_id=? WHERE incident_id=? AND"
            " person_no=?",
            (handover_id, incident_id, person_no),
        )

    def insert_handover(
        self, incident_id: int, request_id: str, from_zone: str,
        to_location: str, destination_kind: str,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO evac_handovers(incident_id, request_id, from_zone,"
            " to_location, destination_kind) VALUES (?,?,?,?,?)",
            (incident_id, request_id, from_zone, to_location, destination_kind),
        )
        return int(cur.lastrowid)

    def insert_handover_people(
        self, handover_id: int, person_nos: list[str]
    ) -> None:
        self._conn.executemany(
            "INSERT INTO evac_handover_people(handover_id, person_no)"
            " VALUES (?,?)",
            [(handover_id, no) for no in person_nos],
        )

    def get_handover(self, incident_id: int, handover_id: int):
        return self._conn.execute(
            "SELECT * FROM evac_handovers WHERE incident_id=? AND id=?",
            (incident_id, handover_id),
        ).fetchone()

    def list_handovers(self, incident_id: int) -> list:
        return self._conn.execute(
            "SELECT * FROM evac_handovers WHERE incident_id=? ORDER BY id",
            (incident_id,),
        ).fetchall()

    def handover_people(self, handover_id: int) -> list:
        return self._conn.execute(
            "SELECT * FROM evac_handover_people WHERE handover_id=? ORDER BY id",
            (handover_id,),
        ).fetchall()

    def handover_person_row(self, handover_id: int, person_no: str):
        return self._conn.execute(
            "SELECT * FROM evac_handover_people WHERE handover_id=? AND person_no=?",
            (handover_id, person_no),
        ).fetchone()

    def set_handover_person_status(
        self, handover_id: int, person_no: str, status: str
    ) -> None:
        self._conn.execute(
            "UPDATE evac_handover_people SET status=? WHERE handover_id=?"
            " AND person_no=?",
            (status, handover_id, person_no),
        )

    def handover_has_pending(self, handover_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM evac_handover_people WHERE handover_id=?"
            " AND status='pending' LIMIT 1",
            (handover_id,),
        ).fetchone()
        return row is not None

    def complete_handover_if_resolved(self, handover_id: int) -> None:
        if not self.handover_has_pending(handover_id):
            self._conn.execute(
                "UPDATE evac_handovers SET status='complete' WHERE id=?",
                (handover_id,),
            )

    def open_handover_ids_for_zone(
        self, incident_id: int, zone: str
    ) -> list[int]:
        rows = self._conn.execute(
            "SELECT id FROM evac_handovers WHERE incident_id=?"
            " AND status='open' AND (from_zone=? OR to_location=?) ORDER BY id",
            (incident_id, zone, zone),
        ).fetchall()
        return [row["id"] for row in rows]

    def count_zone_occupants(self, incident_id: int, zone: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM evac_people WHERE incident_id=?"
            " AND status='in_zone' AND zone=?",
            (incident_id, zone),
        ).fetchone()
        return int(row["n"])

    def start_search(self, incident_id: int, zone: str, version: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO evac_searches(incident_id, zone, started_version,"
            " active, valid, cleared) VALUES (?,?,?,1,1,0)",
            (incident_id, zone, version),
        )
        return int(cur.lastrowid)

    def deactivate_zone_searches(self, incident_id: int, zone: str) -> None:
        self._conn.execute(
            "UPDATE evac_searches SET active=0 WHERE incident_id=? AND zone=?"
            " AND active=1",
            (incident_id, zone),
        )

    def active_search(self, incident_id: int, zone: str):
        return self._conn.execute(
            "SELECT * FROM evac_searches WHERE incident_id=? AND zone=?"
            " AND active=1 ORDER BY id DESC LIMIT 1",
            (incident_id, zone),
        ).fetchone()

    def mark_search_cleared(self, search_id: int) -> None:
        self._conn.execute(
            "UPDATE evac_searches SET cleared=1 WHERE id=?", (search_id,)
        )

    def close_incident(self, incident_id: int) -> None:
        self._conn.execute(
            "UPDATE evac_incidents SET status='closed' WHERE id=?",
            (incident_id,),
        )

    def get_evac_request(self, incident_id: int, request_id: str):
        return self._conn.execute(
            "SELECT * FROM evac_requests WHERE incident_id=? AND request_id=?",
            (incident_id, request_id),
        ).fetchone()

    def insert_evac_request(
        self, incident_id: int, request_id: str, operation: str,
        content_hash: str, response_status: int, response_body: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO evac_requests(incident_id, request_id, operation,"
            " content_hash, response_status, response_body) VALUES (?,?,?,?,?,?)",
            (
                incident_id,
                request_id,
                operation,
                content_hash,
                response_status,
                response_body,
            ),
        )
