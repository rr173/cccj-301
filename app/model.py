"""Domain types and Pydantic request/response schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# All times are within a single service day, so every minute value lives in
# [0, 1440].  Keeping the domain small also bounds the planner's DP table.
DAY_END = 1440


# ---------------------------------------------------------------- availability
# Windows are [open_min, close_min): open at open_min, no longer traversable
# at or after close_min.  A traversal started at s runs [s, s + duration); it is
# allowed iff it lies inside an open window and no closure overlaps it.
# Ending exactly at the right edge is fine: s + duration may equal close_min.

class Window(BaseModel):
    open_min: int = Field(ge=0, le=DAY_END)
    close_min: int = Field(ge=0, le=DAY_END)

    @model_validator(mode="after")
    def _ordered(self):
        if self.close_min <= self.open_min:
            raise ValueError("open window must be non-empty: close_min > open_min")
        return self


class SegmentIn(BaseModel):
    """A directed walking/shuttle segment between two stops."""

    id: str = Field(min_length=1)
    from_stop: str = Field(min_length=1)
    to_stop: str = Field(min_length=1)
    minutes: int = Field(gt=0, le=DAY_END, description="positive traversal duration")
    windows: list[Window] = Field(min_length=1)
    wheelchair_accessible: bool = True


class ClosureIn(BaseModel):
    segment_id: str
    start_min: int = Field(ge=0, le=DAY_END)
    end_min: int = Field(ge=0, le=DAY_END)

    @model_validator(mode="after")
    def _ordered(self):
        if self.end_min <= self.start_min:
            raise ValueError("closure must be non-empty: end_min > start_min")
        return self


class ClosureOut(BaseModel):
    id: int
    segment_id: str
    start_min: int
    end_min: int


# ---------------------------------------------------------------- planning

class PlanRequest(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    depart_min: int = Field(ge=0, le=DAY_END)
    arrive_by_min: int = Field(ge=0, le=DAY_END)
    max_wait_min: int = Field(
        gt=0,
        le=DAY_END,
        description="maximum wait before each ride, including waiting at origin",
    )


class Leg(BaseModel):
    segment_id: str
    from_stop: str
    to_stop: str
    depart_min: int
    arrive_min: int


class PlanOut(BaseModel):
    trip_id: int
    origin: str
    destination: str
    requested_depart_min: int
    arrive_by_min: int
    max_wait_min: int
    anchor_stop: str
    anchor_min: int
    legs: list[Leg]
    arrival_min: int
    revisions: int


# ---------------------------------------------------------------- journey state

class ConfirmIn(BaseModel):
    """Report that the next planned segment was actually walked.

    ``arrival_min`` is the minute the visitor reached the segment's end stop.
    """

    segment_id: str = Field(min_length=1)
    arrival_min: int = Field(ge=0, le=DAY_END)


class JourneyOut(BaseModel):
    trip_id: int
    origin: str
    destination: str
    requested_depart_min: int
    arrive_by_min: int
    max_wait_min: int
    anchor_stop: str
    anchor_min: int
    # Only the still-unridden part of the plan.  It never asks the visitor to
    # return to a stop they have already left.
    legs: list[Leg]
    upcoming_leg: Leg | None
    arrival_min: int | None
    solved: bool
    note: str | None
    revisions: int
    history: list[Leg]


class ErrorBody(BaseModel):
    detail: str
    reason: Literal[
        "stale_confirmation",
        "no_solution",
        "time_went_backwards",
        "not_found",
        "invalid",
        "duplicate_request",
        "run_not_found",
        "stop_not_found",
        "booking_not_found",
        "no_accessible_path",
        "schedule_went_backwards",
        "departed",
        "not_waitlisted",
        "not_confirmable",
        "not_at_stop",
        "already_boarded",
        "transfer_not_verified",
        "not_boardable",
        "illegal_event",
    ] | None = None


# ------------------------------------------------------------- shuttle runs
# Staff register a shuttle run as an ordered list of stops, each with planned
# arrival/departure minutes and a fleet-wide wheelchair-space capacity.  The
# same stop name may appear more than once; bookings always name a concrete
# stop index, never just a name.

class RunStopIn(BaseModel):
    stop: str = Field(min_length=1)
    arrive_min: int = Field(ge=0, le=DAY_END)
    depart_min: int = Field(ge=0, le=DAY_END)

    @model_validator(mode="after")
    def _ordered(self):
        if self.depart_min < self.arrive_min:
            raise ValueError("depart_min must not be earlier than arrive_min")
        return self


class RunIn(BaseModel):
    stops: list[RunStopIn] = Field(min_length=2)
    wheelchair_capacity: int = Field(gt=0)

    @model_validator(mode="after")
    def _schedule_monotone(self):
        for earlier, later in zip(self.stops, self.stops[1:]):
            # Planned times follow the stop order and never go backwards.
            if earlier.depart_min > later.arrive_min:
                raise ValueError(
                    "planned times must not go backwards: stop "
                    f"{earlier.stop!r} departs at {earlier.depart_min} but a later"
                    f" stop arrives at {later.arrive_min}"
                )
        return self


class RunStopOut(BaseModel):
    index: int
    stop: str
    planned_arrive_min: int
    planned_depart_min: int
    actual_arrive_min: int | None
    actual_depart_min: int | None
    state: Literal["scheduled", "arrived", "departed", "ended"]


class RunIntervalOut(BaseModel):
    from_index: int
    to_index: int
    from_stop: str
    to_stop: str
    occupied_wheelchairs: int
    capacity: int
    remaining: int


class RunOut(BaseModel):
    run_id: int
    stops: list[RunStopOut]
    wheelchair_capacity: int
    finished: bool
    intervals: list[RunIntervalOut]


class ActualEventIn(BaseModel):
    """Staff record an actual arrival/departure minute at a stop index."""

    actual_min: int = Field(ge=0, le=DAY_END)


class BoardIn(BaseModel):
    """Staff board a whole confirmed group at a stop index."""

    booking_id: int
    actual_min: int = Field(ge=0, le=DAY_END)


class ManifestEntry(BaseModel):
    booking_id: int
    request_id: str
    group_name: str | None
    wheelchair_count: int
    board_index: int
    board_stop: str
    board_min: int
    alight_index: int
    alight_stop: str


class ManifestOut(BaseModel):
    run_id: int
    onboard: list[ManifestEntry]
    # Everyone recorded alighting so far, in actual event order.
    history: list[ManifestEntry]


# ---------------------------------------------------------------- bookings
class BookingLegIn(BaseModel):
    run_id: int
    board_index: int = Field(ge=0)
    alight_index: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered_stops(self):
        if self.board_index >= self.alight_index:
            raise ValueError("alight_index must be a later stop than board_index")
        return self


class BookingIn(BaseModel):
    request_id: str = Field(min_length=1)
    group_name: str | None = None
    wheelchair_count: int = Field(gt=0)
    legs: list[BookingLegIn] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _transfer_shape(self):
        if len(self.legs) == 2:
            first, second = self.legs
            if first.run_id == second.run_id:
                raise ValueError(
                    "a two-leg booking must use two different runs; book the"
                    " whole ride as one leg if they share a run"
                )
        return self


class BookingLegOut(BaseModel):
    leg: int
    run_id: int
    board_index: int
    board_stop: str
    alight_index: int
    alight_stop: str
    planned_board_min: int
    planned_alight_min: int
    status: Literal["reserved", "boarded", "alighted", "void"]
    actual_board_min: int | None
    actual_alight_min: int | None


class BookingOut(BaseModel):
    booking_id: int
    request_id: str
    group_name: str | None
    wheelchair_count: int
    status: Literal["waiting", "confirmed", "cancelled", "void", "completed"]
    legs: list[BookingLegOut]
    # Present when the whole order could not be confirmed right now.
    waitlist_reason: str | None = None
