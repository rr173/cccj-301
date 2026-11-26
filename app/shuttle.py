"""Shuttle run registration, wheelchair-space booking, and live boarding.

Domain rules
------------
* A run is an ordered sequence of stops reusing the existing accessible
  segments: every consecutive stop pair must be linked by at least one
  wheelchair-accessible segment.  Planned arrival/departure minutes follow the
  stop order and never go backwards.  The same stop name may recur; bookings
  always reference a concrete stop index.
* A booking is one group needing N wheelchair spaces, on either one direct leg
  or two legs on different runs that transfer at one shared stop, with the
  planned arrival no later than the connecting departure.
* Space is held only on the adjacent stop intervals a leg actually covers, and
  is reusable by the next group after alighting.  A whole order is confirmed
  only if every leg has space on every interval; otherwise the whole order is
  waitlisted and holds nothing.  A boarding stop whose stop has already
  departed cannot be booked.
* Reusing a request id with identical content returns the original order;
  reusing it with different content is rejected.
* Staff drive each run stop-by-stop (arrive, board, depart) with monotone
  actual minutes.  Boarding is whole-group only, needs an unused confirmed
  reservation at that stop, and a connecting leg additionally requires the
  first run to have recorded the group alighting at the transfer stop no later
  than the connecting boarding.  No boarding is accepted after departure.
* A confirmed group missing any boarding departure has its remaining
  reservation voided immediately; legs already ridden stay on the onboard list
  until their own alighting stop.  Waitlisted orders die once any of their
  boarding stops departs.
* After every new order, cancellation, departure, or an arrival that frees
  intervals (groups alighting), still-valid waitlisted groups are promoted in
  original application order: groups that still cannot fit are skipped, the
  first fully fitting order is confirmed, then evaluation continues with the
  next group.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .model import (
    BookingIn,
    BookingLegOut,
    BookingOut,
    ManifestEntry,
    ManifestOut,
    RunIn,
    RunIntervalOut,
    RunOut,
    RunStopOut,
)


class ShuttleError(Exception):
    def __init__(self, detail: str, reason: str = "invalid", status: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.status = status


# ------------------------------------------------------------------ helpers
def content_hash(body: BookingIn) -> str:
    payload = {
        "group_name": body.group_name,
        "wheelchair_count": body.wheelchair_count,
        "legs": [
            {
                "run_id": leg.run_id,
                "board_index": leg.board_index,
                "alight_index": leg.alight_index,
            }
            for leg in body.legs
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class StopView:
    index: int
    stop: str
    planned_arrive: int
    planned_depart: int
    actual_arrive: int | None
    actual_depart: int | None

    @property
    def arrived(self) -> bool:
        return self.actual_arrive is not None

    @property
    def departed(self) -> bool:
        return self.actual_depart is not None


@dataclass
class RunView:
    id: int
    capacity: int
    finished: bool
    stops: list[StopView] = field(default_factory=list)

    def stop(self, index: int) -> StopView:
        if not 0 <= index < len(self.stops):
            raise ShuttleError(
                f"run {self.id} has no stop index {index}",
                "stop_not_found",
                404,
            )
        return self.stops[index]

    def last_actual(self) -> int | None:
        latest = None
        for stop in self.stops:
            for value in (stop.actual_arrive, stop.actual_depart):
                if value is not None and (latest is None or value > latest):
                    latest = value
        return latest


def _load_run(db, run_id: int) -> RunView:
    bundle = db.get_run(run_id)
    if bundle is None:
        raise ShuttleError(f"run {run_id} not found", "run_not_found", 404)
    run_row, stop_rows = bundle["run"], bundle["stops"]
    return RunView(
        id=run_row["id"],
        capacity=run_row["wheelchair_capacity"],
        finished=bool(run_row["finished"]),
        stops=[
            StopView(
                index=row["stop_index"],
                stop=row["stop_name"],
                planned_arrive=row["planned_arrive"],
                planned_depart=row["planned_depart"],
                actual_arrive=row["actual_arrive"],
                actual_depart=row["actual_depart"],
            )
            for row in stop_rows
        ],
    )


def _load_booking(db, booking_id: int):
    bundle = db.get_booking(booking_id)
    if bundle is None:
        raise ShuttleError(f"booking {booking_id} not found", "booking_not_found", 404)
    return bundle["booking"], bundle["legs"]


# ------------------------------------------------------------- run registry
def register_run(db, network, body: RunIn) -> int:
    segments = network.segments
    for earlier, later in zip(body.stops, body.stops[1:]):
        if not any(
            seg.wheelchair_accessible
            and seg.from_stop == earlier.stop
            and seg.to_stop == later.stop
            for seg in segments.values()
        ):
            raise ShuttleError(
                f"no wheelchair-accessible segment from {earlier.stop!r} to"
                f" {later.stop!r}; shuttle stops must reuse accessible segments",
                "no_accessible_path",
                422,
            )
    with db.lock, db.begin():
        return db.insert_run(
            [
                {
                    "stop": s.stop,
                    "arrive_min": s.arrive_min,
                    "depart_min": s.depart_min,
                }
                for s in body.stops
            ],
            body.wheelchair_capacity,
        )


# --------------------------------------------------------------- capacity
def _interval_loads(db, run: RunView) -> list[int]:
    """Reserved + onboard wheelchair spaces on each adjacent-stop interval."""
    loads = [0] * (len(run.stops) - 1)
    for row in db.active_legs_for_run(run.id):
        for interval in range(row["board_index"], row["alight_index"]):
            loads[interval] += row["wheelchair_count"]
    return loads


def _order_fits(db, run_views: dict[int, RunView], legs, wheelchair_count: int) -> bool:
    """True iff every leg has space across every interval it covers."""
    for leg in legs:
        run = run_views[leg["run_id"]]
        loads = _interval_loads(db, run)
        for interval in range(leg["board_index"], leg["alight_index"]):
            if loads[interval] + wheelchair_count > run.capacity:
                return False
    return True


def _any_boarding_departed(run_views: dict[int, RunView], legs) -> bool:
    # A waitlisted order is impossible once any of its boarding stops has left.
    return any(
        run_views[leg["run_id"]].stops[leg["board_index"]].departed for leg in legs
    )


def _run_views_for(db, legs) -> dict[int, RunView]:
    return {leg["run_id"]: _load_run(db, leg["run_id"]) for leg in legs}


def _promote_waitlist(db) -> None:
    """Promote waitlisted groups in application order where the whole order fits.

    Runs once per triggering event.  A promotion consumes space, never frees
    it, so a single ascending pass is enough; groups that still do not fit are
    left waiting for a later trigger.
    """
    for booking_id in db.list_waiting_bookings():
        booking_row, leg_rows = _load_booking(db, booking_id)
        if booking_row["status"] != "waiting":
            continue
        legs = [
            {
                "run_id": row["run_id"],
                "board_index": row["board_index"],
                "alight_index": row["alight_index"],
            }
            for row in leg_rows
        ]
        run_views = _run_views_for(db, legs)
        # A waitlisted order whose boarding stop has left is dead and is swept
        # here as well as at departure time.
        if _any_boarding_departed(run_views, legs):
            db.set_booking_status(booking_id, "void")
            continue
        if _order_fits(db, run_views, legs, booking_row["wheelchair_count"]):
            db.promote_waiting_legs(booking_id)
            db.set_booking_status(booking_id, "confirmed")


# ------------------------------------------------------------- submission
def submit_booking(db, body: BookingIn) -> tuple[BookingOut, bool]:
    """Returns (booking view, created).  Replays are idempotent."""
    with db.lock:
        existing = db.get_request(body.request_id)
        if existing is not None:
            if existing["content_hash"] != content_hash(body):
                raise ShuttleError(
                    f"request_id {body.request_id!r} was already used for a"
                    " different booking; choose a new request id to change it",
                    "duplicate_request",
                    409,
                )
            return _booking_out(db, existing["id"]), False

        leg_inputs = [
            {
                "run_id": leg.run_id,
                "board_index": leg.board_index,
                "alight_index": leg.alight_index,
            }
            for leg in body.legs
        ]
        with db.begin():
            run_views: dict[int, RunView] = {}
            for leg in leg_inputs:
                run = _load_run(db, leg["run_id"])
                run_views[run.id] = run
                # Index validity is enforced by stop(); the alighting stop must
                # exist as a real stop, and a covered interval needs a start.
                run.stop(leg["board_index"])
                run.stop(leg["alight_index"])
                # No leg may be booked onto a boarding stop that already left,
                # including the connecting leg of a two-leg order.
                if run.stops[leg["board_index"]].departed:
                    raise ShuttleError(
                        f"run {run.id} stop {leg['board_index']} has already"
                        " departed and cannot be booked",
                        "departed",
                        409,
                    )
            if len(leg_inputs) == 2:
                first, second = leg_inputs
                run_a, run_b = run_views[first["run_id"]], run_views[second["run_id"]]
                if run_a.id == run_b.id:
                    raise ShuttleError(
                        "two legs must use different runs", "invalid", 422
                    )
                transfer_a = run_a.stops[first["alight_index"]]
                transfer_b = run_b.stops[second["board_index"]]
                if transfer_a.stop != transfer_b.stop:
                    raise ShuttleError(
                        "the two legs must transfer at the same stop: got"
                        f" {transfer_a.stop!r} then {transfer_b.stop!r}",
                        "invalid",
                        422,
                    )
                if transfer_a.planned_arrive > transfer_b.planned_depart:
                    raise ShuttleError(
                        "the connecting run departs before the first run is"
                        f" planned to arrive ({transfer_a.planned_arrive} >"
                        f" {transfer_b.planned_depart})",
                        "invalid",
                        422,
                    )

            confirmable = _order_fits(db, run_views, leg_inputs, body.wheelchair_count)
            status = "confirmed" if confirmable else "waiting"
            booking_id = db.insert_booking(
                body.request_id,
                content_hash(body),
                body.group_name,
                body.wheelchair_count,
                status,
                leg_inputs,
            )
            if not confirmable:
                _promote_waitlist(db)
        return _booking_out(db, booking_id), True


def cancel_booking(db, booking_id: int) -> BookingOut:
    with db.lock, db.begin():
        booking_row, leg_rows = _load_booking(db, booking_id)
        if any(row["status"] == "boarded" for row in leg_rows):
            raise ShuttleError(
                "the group has already boarded; cancellation is only allowed"
                " before first boarding",
                "already_boarded",
            )
        if booking_row["status"] in ("cancelled", "void", "completed"):
            raise ShuttleError(
                f"booking is already {booking_row['status']}",
                "not_waitlisted",
            )
        db.set_booking_status(booking_id, "cancelled")
        # Confirmed reservations are released by voiding every still-live leg;
        # waiting placeholder legs are already 'void' on the leg table.
        for row in leg_rows:
            if row["status"] == "reserved":
                db.set_leg_void(row["id"])
        _promote_waitlist(db)
    return _booking_out(db, booking_id)


# --------------------------------------------------------- staff events
def _assert_after_last_actual(run: RunView, actual_min: int) -> None:
    latest = run.last_actual()
    if latest is not None and actual_min < latest:
        raise ShuttleError(
            f"actual minute {actual_min} is earlier than the latest recorded"
            f" actual minute {latest} on run {run.id}",
            "schedule_went_backwards",
        )


def record_arrival(db, run_id: int, index: int, actual_min: int) -> RunOut:
    with db.lock, db.begin():
        run = _load_run(db, run_id)
        stop = run.stop(index)
        if stop.arrived:
            raise ShuttleError(
                f"run {run_id} stop {index} has already arrived",
                "illegal_event",
            )
        if index > 0 and not run.stops[index - 1].departed:
            raise ShuttleError(
                f"the previous stop {index - 1} must depart before stop"
                f" {index} can arrive",
                "not_at_stop",
            )
        _assert_after_last_actual(run, actual_min)
        db.set_stop_arrival(run_id, index, actual_min)

        # Everyone onboard whose ride ends here alights as part of the arrival.
        for row in db.legs_alighting_at_stop(run_id, index):
            db.set_leg_alighted(row["id"], actual_min)
            _refresh_booking_status(db, row["booking_id"])

        if index == len(run.stops) - 1:
            # Last-stop arrival finishes the run; any group still onboard
            # alights here even if their nominal stop was later.
            for row in db.all_boarded_legs(run_id):
                db.set_leg_alighted(row["id"], actual_min)
                _refresh_booking_status(db, row["booking_id"])
            db.finish_run(run_id)
        else:
            # Alighting frees intervals starting here; groups boarding at this
            # stop must be able to take that space before boarding closes.
            _promote_waitlist(db)
        return _run_out(db, _load_run(db, run_id))


def record_boarding(db, run_id: int, index: int, booking_id: int, actual_min: int) -> RunOut:
    with db.lock, db.begin():
        run = _load_run(db, run_id)
        stop = run.stop(index)
        if not stop.arrived:
            raise ShuttleError(
                f"stop {index} must record arrival before boarding",
                "not_at_stop",
            )
        if stop.departed:
            raise ShuttleError(
                f"stop {index} has already departed; boarding is closed",
                "departed",
            )
        _assert_after_last_actual(run, actual_min)

        booking_row, leg_rows = _load_booking(db, booking_id)
        if booking_row["status"] != "confirmed":
            raise ShuttleError(
                f"booking {booking_id} is {booking_row['status']}, not confirmed",
                "not_confirmable",
            )
        candidates = [
            row for row in leg_rows
            if row["run_id"] == run_id and row["board_index"] == index
        ]
        if not candidates:
            raise ShuttleError(
                f"booking {booking_id} has no confirmed reservation boarding"
                f" run {run_id} at stop {index}",
                "not_boardable",
            )
        leg = candidates[0]
        if leg["status"] != "reserved":
            raise ShuttleError(
                f"the reservation for this stop is already {leg['status']}",
                "already_boarded",
            )

        # Connecting leg: the first run must have recorded the group alighting
        # at the transfer stop, and no later than this boarding minute.
        position = leg["leg_position"]
        if position > 0:
            prior = db.find_leg(booking_id, position - 1)
            prior_run = _load_run(db, prior["run_id"])
            transfer = prior_run.stops[prior["alight_index"]]
            if prior["status"] != "alighted":
                raise ShuttleError(
                    "the first run has not recorded this group alighting at"
                    " the transfer stop",
                    "transfer_not_verified",
                )
            if prior["actual_alight_min"] > actual_min:
                raise ShuttleError(
                    "the group alighted the first run later than this boarding"
                    f" minute ({prior['actual_alight_min']} > {actual_min})",
                    "transfer_not_verified",
                )

        db.set_leg_boarded(leg["id"], actual_min)
        return _run_out(db, _load_run(db, run_id))


def record_departure(db, run_id: int, index: int, actual_min: int) -> RunOut:
    with db.lock, db.begin():
        run = _load_run(db, run_id)
        stop = run.stop(index)
        if not stop.arrived:
            raise ShuttleError(
                f"stop {index} must record arrival before departure",
                "not_at_stop",
            )
        if stop.departed:
            raise ShuttleError(
                f"stop {index} has already departed", "illegal_event"
            )
        if index == len(run.stops) - 1:
            raise ShuttleError(
                "the last stop ends on arrival; it does not depart",
                "illegal_event",
            )
        _assert_after_last_actual(run, actual_min)

        # No-shows: confirmed reservations boarding here that were never used
        # lose every remaining (reserved) leg.  Already-ridden legs stay
        # onboard until their own alighting stop.
        for row in list(db.legs_reserved_at_stop(run_id, index)):
            booking_row, all_legs = _load_booking(db, row["booking_id"])
            for other in all_legs:
                if other["status"] == "reserved":
                    db.set_leg_void(other["id"])
            _refresh_booking_status(db, row["booking_id"])

        # Waitlisted orders whose first boarding stop leaves can never happen.
        for row in list(db.waiting_legs_boarding_at_stop(run_id, index)):
            booking_row, _ = _load_booking(db, row["booking_id"])
            if booking_row["status"] == "waiting":
                db.set_booking_status(row["booking_id"], "void")

        db.set_stop_departure(run_id, index, actual_min)
        _promote_waitlist(db)
        return _run_out(db, _load_run(db, run_id))


def _refresh_booking_status(db, booking_id: int) -> None:
    booking_row, leg_rows = _load_booking(db, booking_id)
    if booking_row["status"] in ("cancelled", "void"):
        return
    if all(row["status"] == "alighted" for row in leg_rows):
        db.set_booking_status(booking_id, "completed")
    elif any(row["status"] == "void" for row in leg_rows) and not any(
        row["status"] in ("reserved", "boarded") for row in leg_rows
    ):
        db.set_booking_status(booking_id, "void")


# ------------------------------------------------------------- presenters
def _refresh_pending_bookings(db) -> None:
    """Sweep waitlisted orders whose boarding stop departed while idle."""
    for booking_id in db.list_waiting_bookings():
        booking_row, leg_rows = _load_booking(db, booking_id)
        if booking_row["status"] != "waiting":
            continue
        run_views = _run_views_for(
            db,
            [
                {
                    "run_id": row["run_id"],
                    "board_index": row["board_index"],
                    "alight_index": row["alight_index"],
                }
                for row in leg_rows
            ],
        )
        legs = [
            {
                "run_id": row["run_id"],
                "board_index": row["board_index"],
                "alight_index": row["alight_index"],
            }
            for row in leg_rows
        ]
        if _any_boarding_departed(run_views, legs):
            db.set_booking_status(booking_id, "void")


def _stop_state(stop: StopView, is_last: bool) -> str:
    if stop.departed:
        return "departed"
    if stop.arrived:
        return "ended" if is_last else "arrived"
    return "scheduled"


def _run_out(db, run: RunView) -> RunOut:
    loads = _interval_loads(db, run)
    intervals = []
    for i in range(len(run.stops) - 1):
        intervals.append(
            RunIntervalOut(
                from_index=i,
                to_index=i + 1,
                from_stop=run.stops[i].stop,
                to_stop=run.stops[i + 1].stop,
                occupied_wheelchairs=loads[i],
                capacity=run.capacity,
                remaining=run.capacity - loads[i],
            )
        )
    last = len(run.stops) - 1
    return RunOut(
        run_id=run.id,
        stops=[
            RunStopOut(
                index=s.index,
                stop=s.stop,
                planned_arrive_min=s.planned_arrive,
                planned_depart_min=s.planned_depart,
                actual_arrive_min=s.actual_arrive,
                actual_depart_min=s.actual_depart,
                state=_stop_state(s, s.index == last),
            )
            for s in run.stops
        ],
        wheelchair_capacity=run.capacity,
        finished=run.finished,
        intervals=intervals,
    )


def get_run_out(db, run_id: int) -> RunOut:
    with db.lock:
        run = _load_run(db, run_id)
        _refresh_pending_bookings(db)
        return _run_out(db, run)


def list_run_ids(db) -> list[int]:
    with db.lock:
        return db.list_runs()


def _booking_out(db, booking_id: int) -> BookingOut:
    booking_row, leg_rows = _load_booking(db, booking_id)
    run_cache: dict[int, RunView] = {}
    legs_out = []
    waitlist_reason = None
    for row in leg_rows:
        run = run_cache.get(row["run_id"])
        if run is None:
            run = _load_run(db, row["run_id"])
            run_cache[row["run_id"]] = run
        board = run.stops[row["board_index"]]
        alight = run.stops[row["alight_index"]]
        legs_out.append(
            BookingLegOut(
                leg=row["leg_position"],
                run_id=row["run_id"],
                board_index=row["board_index"],
                board_stop=board.stop,
                alight_index=row["alight_index"],
                alight_stop=alight.stop,
                planned_board_min=board.planned_depart,
                planned_alight_min=alight.planned_arrive,
                status=row["status"],
                actual_board_min=row["actual_board_min"],
                actual_alight_min=row["actual_alight_min"],
            )
        )
    status = booking_row["status"]
    if status == "waiting":
        # Explain the blockage on the first interval that runs out of space.
        for row in leg_rows:
            run = run_cache[row["run_id"]]
            loads = _interval_loads(db, run)
            for interval in range(row["board_index"], row["alight_index"]):
                if loads[interval] + booking_row["wheelchair_count"] > run.capacity:
                    waitlist_reason = (
                        f"run {run.id} interval {interval}->{interval + 1} has"
                        f" {run.capacity - loads[interval]} spaces free,"
                        f" {booking_row['wheelchair_count']} requested"
                    )
                    break
            if waitlist_reason:
                break
    return BookingOut(
        booking_id=booking_row["id"],
        request_id=booking_row["request_id"],
        group_name=booking_row["group_name"],
        wheelchair_count=booking_row["wheelchair_count"],
        status=status,
        legs=legs_out,
        waitlist_reason=waitlist_reason,
    )


def get_booking_out(db, booking_id: int) -> BookingOut:
    with db.lock:
        _refresh_pending_bookings(db)
        return _booking_out(db, booking_id)


def get_booking_by_request(db, request_id: str) -> BookingOut:
    with db.lock:
        row = db.get_request(request_id)
        if row is None:
            raise ShuttleError(
                f"request_id {request_id!r} not found", "booking_not_found", 404
            )
        _refresh_pending_bookings(db)
        return _booking_out(db, row["id"])


def manifest(db, run_id: int) -> ManifestOut:
    with db.lock:
        run = _load_run(db, run_id)
        onboard: list[ManifestEntry] = []
        history: list[ManifestEntry] = []
        # Rebuild the ride ledger from every leg that touched this run.
        rows = db.ridden_legs_for_run(run_id)
        for row in rows:
            entry = ManifestEntry(
                booking_id=row["booking_id"],
                request_id=row["request_id"],
                group_name=row["group_name"],
                wheelchair_count=row["wheelchair_count"],
                board_index=row["board_index"],
                board_stop=run.stops[row["board_index"]].stop,
                board_min=row["actual_board_min"],
                alight_index=row["alight_index"],
                alight_stop=run.stops[row["alight_index"]].stop,
            )
            (onboard if row["status"] == "boarded" else history).append(entry)
        return ManifestOut(run_id=run_id, onboard=onboard, history=history)
