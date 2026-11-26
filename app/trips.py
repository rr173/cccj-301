"""Trip lifecycle: an anchored, revisable itinerary.

Rules implemented here
----------------------
* A trip is anchored either at the original origin + requested departure (no
  leg walked yet) or at the last stop the visitor actually reached, at the
  minute they actually reached it.  Re-planning only ever covers the part of
  the journey that has not happened yet; the visitor is never asked to walk
  back to a stop they have already left.
* After a closure change (or an actual-arrival update) the *unwalked tail* is
  kept verbatim when every one of its scheduled departures still satisfies the
  constraints.  Otherwise the tail is re-planned from the anchor.
* When the current state is unsolvable, the anchor and the walked history are
  preserved; a later closure removal can make the trip solvable again.
* Confirmations must refer to the next leg of the *current* plan and must not
  move backwards in time.  A confirmation naming a leg that was replaced by a
  re-route is rejected as stale.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Leg
from .network import Network, itinerary_still_valid, plan_itinerary


class TripError(Exception):
    def __init__(self, detail: str, reason: str = "invalid", status: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.status = status


@dataclass
class Trip:
    id: int
    origin: str
    destination: str
    requested_depart_min: int
    arrive_by_min: int
    max_wait_min: int

    # Anchor: where re-planning starts.
    anchor_stop: str
    anchor_min: int
    # The still-to-be-walked plan (empty when already at the destination).
    legs: list[Leg] = field(default_factory=list)
    # Legs the visitor has already reported walking.
    history: list[Leg] = field(default_factory=list)
    solved: bool = True
    note: str | None = None
    revisions: int = 0
    # Latest minute the visitor has attested to.
    last_actual_min: int = 0
    # Closure generation this trip's tail was last validated against.
    checked_version: int = -1

    def upcoming(self) -> Leg | None:
        return self.legs[0] if self.legs else None

    def arrival_min(self) -> int | None:
        if self.legs:
            return self.legs[-1].arrive_min
        if self.anchor_stop == self.destination:
            # Either the trip started at the destination, or the visitor has
            # confirmed reaching it.
            return self.anchor_min
        return None

    def replan(self, network: Network, reason: str, version: int) -> None:
        """Keep the tail if still feasible, else re-plan from the anchor.

        ``version`` is the closure generation being validated against; it is
        recorded so later reads can tell whether another check is needed.
        """
        if self.anchor_stop == self.destination:
            self.legs = []
            self.solved = True
            self.note = None
            self.checked_version = version
            return
        if self.legs and itinerary_still_valid(
            network,
            self.legs,
            self.anchor_stop,
            self.anchor_min,
            self.arrive_by_min,
            self.max_wait_min,
        ):
            self.solved = True
            self.note = f"{reason}; remaining plan unchanged"
            self.checked_version = version
            return
        new_legs = plan_itinerary(
            network,
            self.anchor_stop,
            self.destination,
            self.anchor_min,
            self.arrive_by_min,
            self.max_wait_min,
        )
        self.revisions += 1
        self.checked_version = version
        # Preserve anchor and history regardless of the outcome.
        if new_legs is None:
            self.legs = []
            self.solved = False
            self.note = f"{reason}; no accessible itinerary from current position"
        else:
            self.legs = new_legs
            self.solved = True
            self.note = f"{reason}; remaining plan re-routed"

    def sync(self, network: Network, version: int) -> bool:
        """Bring the trip up to date with the closure generation, if needed.

        Returns True when the trip's stored plan changed (or its state was
        re-evaluated) and should be persisted.
        """
        if self.checked_version == version:
            return False
        if not self.legs and self.anchor_stop == self.destination:
            self.checked_version = version
            return True
        self.replan(network, "closure change", version)
        return True

    # -------------------------------------------------------------- actions
    def confirm(self, network: Network, segment_id: str, arrival_min: int) -> None:
        """Visitor reports they walked the next planned segment.

        Raises TripError without mutating anything if the confirmation is
        stale (the leg has been re-routed away) or if time would go backwards.
        The caller is responsible for syncing against the current closure
        generation first.
        """
        leg = self.upcoming()
        if leg is None:
            raise TripError("no remaining segment to confirm", "stale_confirmation")
        if leg.segment_id != segment_id:
            raise TripError(
                f"segment {segment_id!r} is not the next planned segment "
                f"({leg.segment_id!r}); the plan may have been re-routed",
                "stale_confirmation",
            )
        if arrival_min < self.last_actual_min:
            raise TripError(
                "arrival minute is earlier than the last confirmed time",
                "time_went_backwards",
            )
        if arrival_min < leg.depart_min:
            raise TripError(
                "arrival minute is earlier than this segment's departure",
                "time_went_backwards",
            )

        walked = Leg(
            segment_id=leg.segment_id,
            from_stop=leg.from_stop,
            to_stop=leg.to_stop,
            depart_min=leg.depart_min,
            arrive_min=arrival_min,
        )
        self.history.append(walked)
        self.legs = self.legs[1:]
        self.anchor_stop = walked.to_stop
        self.anchor_min = arrival_min
        self.last_actual_min = arrival_min
        if self.anchor_stop == self.destination:
            self.legs = []
            self.solved = True
            self.note = "destination reached"
            self.revisions += 1
            return
        # Re-plan from the new anchor, validating the untouched tail first.
        new_legs = None
        if self.legs and itinerary_still_valid(
            network,
            self.legs,
            self.anchor_stop,
            self.anchor_min,
            self.arrive_by_min,
            self.max_wait_min,
        ):
            self.solved = True
            self.note = "actual arrival recorded; remaining plan unchanged"
        else:
            new_legs = plan_itinerary(
                network,
                self.anchor_stop,
                self.destination,
                self.anchor_min,
                self.arrive_by_min,
                self.max_wait_min,
            )
            self.revisions += 1
            if new_legs is None:
                self.legs = []
                self.solved = False
                self.note = "actual arrival recorded; no itinerary from current stop"
            else:
                self.legs = new_legs
                self.solved = True
                self.note = "actual arrival recorded; remaining plan re-routed"


def sync_trip(db, trip: Trip, network: Network | None = None) -> bool:
    """Validate a trip against the current closure generation if stale."""
    version = db.closure_version()
    if trip.checked_version == version:
        return False
    if network is None:
        network = db.load_network()
    changed = trip.sync(network, version)
    if changed:
        db.save_trip(trip)
    return changed


def start_trip(
    db,
    network: Network,
    origin: str,
    destination: str,
    depart_min: int,
    arrive_by_min: int,
    max_wait_min: int,
) -> Trip:
    legs = plan_itinerary(
        network, origin, destination, depart_min, arrive_by_min, max_wait_min
    )
    if legs is None:
        raise TripError(
            "no accessible itinerary satisfies the constraints", "no_solution", 422
        )
    trip = Trip(
        id=0,
        origin=origin,
        destination=destination,
        requested_depart_min=depart_min,
        arrive_by_min=arrive_by_min,
        max_wait_min=max_wait_min,
        anchor_stop=origin,
        anchor_min=depart_min,
        legs=legs,
        last_actual_min=depart_min,
        checked_version=db.closure_version(),
    )
    trip.id = db.insert_trip(trip)
    db.save_trip(trip)
    return trip
