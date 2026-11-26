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
    ] | None = None
