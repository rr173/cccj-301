"""FastAPI application: accessible walking + shuttle itineraries."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import Database
from .model import (
    ActualEventIn,
    BoardIn,
    BookingIn,
    ClosureIn,
    ClosureOut,
    CloseIn,
    ConfirmIn,
    ErrorBody,
    FindPersonIn,
    HandoverActionIn,
    HandoverIn,
    IncidentIn,
    JourneyOut,
    PlanOut,
    PlanRequest,
    RegisterPeopleIn,
    RunIn,
    SearchIn,
    SegmentIn,
    Window,
)
from .network import Segment as DomainSegment
from . import evacuation, shuttle
from .evacuation import EvacError
from .shuttle import ShuttleError
from .trips import Trip, TripError, start_trip, sync_trip


def _journey_out(trip: Trip) -> JourneyOut:
    return JourneyOut(
        trip_id=trip.id,
        origin=trip.origin,
        destination=trip.destination,
        requested_depart_min=trip.requested_depart_min,
        arrive_by_min=trip.arrive_by_min,
        max_wait_min=trip.max_wait_min,
        anchor_stop=trip.anchor_stop,
        anchor_min=trip.anchor_min,
        legs=list(trip.legs),
        upcoming_leg=trip.upcoming(),
        arrival_min=trip.arrival_min(),
        solved=trip.solved,
        note=trip.note,
        revisions=trip.revisions,
        history=list(trip.history),
    )


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = db_path if db_path is not None else Path("exhibition.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(path)
        app.state.db = db
        try:
            yield
        finally:
            db.close()

    app = FastAPI(
        title="Accessible Exhibition Routing",
        version="1.0.0",
        lifespan=lifespan,
    )

    def get_db(request: Request) -> Database:
        return request.app.state.db

    def require_trip(db: Database, trip_id: int) -> Trip:
        trip = db.load_trip(trip_id)
        if trip is None:
            raise HTTPException(404, f"trip {trip_id} not found")
        return trip

    @app.exception_handler(TripError)
    async def _trip_error(_request: Request, exc: TripError) -> JSONResponse:
        body = ErrorBody(detail=exc.detail, reason=exc.reason)  # type: ignore[arg-type]
        return JSONResponse(status_code=exc.status, content=body.model_dump())

    @app.exception_handler(ShuttleError)
    async def _shuttle_error(_request: Request, exc: ShuttleError) -> JSONResponse:
        body = ErrorBody(detail=exc.detail, reason=exc.reason)  # type: ignore[arg-type]
        return JSONResponse(status_code=exc.status, content=body.model_dump())

    @app.exception_handler(EvacError)
    async def _evac_error(_request: Request, exc: EvacError) -> JSONResponse:
        body = ErrorBody(detail=exc.detail, reason=exc.reason)  # type: ignore[arg-type]
        content = body.model_dump()
        if exc.extra:
            content.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=content)

    # ---------------------------------------------------------------- network
    @app.post("/segments", status_code=201)
    def add_segment(seg: SegmentIn, db: Database = Depends(get_db)):
        try:
            db.add_segment(
                DomainSegment(
                    id=seg.id,
                    from_stop=seg.from_stop,
                    to_stop=seg.to_stop,
                    minutes=seg.minutes,
                    windows=tuple(seg.windows),
                    wheelchair_accessible=seg.wheelchair_accessible,
                )
            )
        except Exception as exc:  # sqlite3.IntegrityError and friends
            raise HTTPException(409, f"could not add segment: {exc}") from exc
        return {"id": seg.id}

    @app.get("/segments")
    def list_segments(db: Database = Depends(get_db)):
        return [
            {
                "id": s.id,
                "from_stop": s.from_stop,
                "to_stop": s.to_stop,
                "minutes": s.minutes,
                "windows": [
                    Window(open_min=w.open_min, close_min=w.close_min)
                    for w in s.windows
                ],
                "wheelchair_accessible": s.wheelchair_accessible,
            }
            for s in db.list_segments()
        ]

    @app.post("/closures", status_code=201)
    def add_closure(body: ClosureIn, db: Database = Depends(get_db)):
        if not db.segment_exists(body.segment_id):
            raise HTTPException(404, f"unknown segment {body.segment_id!r}")
        with db.lock:
            closure_id = db.add_closure(body.segment_id, body.start_min, body.end_min)
            # Trips re-validate lazily on their next read/confirm, not here:
            # a closure must never make an unrelated request pay for a large
            # re-plan.
            db.bump_closure_version()
        return ClosureOut(
            id=closure_id,
            segment_id=body.segment_id,
            start_min=body.start_min,
            end_min=body.end_min,
        )

    @app.delete("/closures/{closure_id}", status_code=204)
    def lift_closure(closure_id: int, db: Database = Depends(get_db)):
        with db.lock:
            if not db.lift_closure(closure_id):
                raise HTTPException(404, "active closure not found")
            db.bump_closure_version()
        return None

    @app.get("/closures")
    def list_closures(db: Database = Depends(get_db)):
        return db.list_closures()

    # ---------------------------------------------------------------- planning
    @app.post("/plans", response_model=PlanOut, status_code=201)
    def create_plan(body: PlanRequest, db: Database = Depends(get_db)):
        with db.lock:
            trip = start_trip(
                db,
                db.load_network(),
                body.origin,
                body.destination,
                body.depart_min,
                body.arrive_by_min,
                body.max_wait_min,
            )
        arrival = trip.arrival_min()
        return PlanOut(
            trip_id=trip.id,
            origin=trip.origin,
            destination=trip.destination,
            requested_depart_min=trip.requested_depart_min,
            arrive_by_min=trip.arrive_by_min,
            max_wait_min=trip.max_wait_min,
            anchor_stop=trip.anchor_stop,
            anchor_min=trip.anchor_min,
            legs=trip.legs,
            arrival_min=trip.anchor_min if arrival is None else arrival,
            revisions=trip.revisions,
        )

    @app.get("/trips/{trip_id}", response_model=JourneyOut)
    def read_trip(trip_id: int, db: Database = Depends(get_db)):
        with db.lock:
            trip = require_trip(db, trip_id)
            sync_trip(db, trip)
            return _journey_out(trip)

    # ---------------------------------------------------------------- journey
    @app.post("/trips/{trip_id}/confirm", response_model=JourneyOut)
    def confirm_leg(trip_id: int, body: ConfirmIn, db: Database = Depends(get_db)):
        with db.lock:
            trip = require_trip(db, trip_id)
            network = db.load_network()
            # Bring the plan up to date before judging staleness, so a leg
            # removed by a closure is reported as stale, not silently walked.
            sync_trip(db, trip, network)
            trip.confirm(network, body.segment_id, body.arrival_min)
            trip.checked_version = db.closure_version()
            db.save_trip(trip)
            return _journey_out(trip)

    # ----------------------------------------------------------- shuttle runs
    @app.post("/shuttle/runs", status_code=201)
    def add_run(body: RunIn, db: Database = Depends(get_db)):
        run_id = shuttle.register_run(db, db.load_network(), body)
        return shuttle.get_run_out(db, run_id)

    @app.get("/shuttle/runs")
    def list_runs(db: Database = Depends(get_db)):
        return [shuttle.get_run_out(db, run_id) for run_id in shuttle.list_run_ids(db)]

    @app.get("/shuttle/runs/{run_id}")
    def read_run(run_id: int, db: Database = Depends(get_db)):
        return shuttle.get_run_out(db, run_id)

    @app.get("/shuttle/runs/{run_id}/manifest")
    def read_manifest(run_id: int, db: Database = Depends(get_db)):
        # Validates the run exists before producing a ledger.
        shuttle.get_run_out(db, run_id)
        return shuttle.manifest(db, run_id)

    @app.post("/shuttle/runs/{run_id}/stops/{index}/arrival")
    def arrive(run_id: int, index: int, body: ActualEventIn, db: Database = Depends(get_db)):
        return shuttle.record_arrival(db, run_id, index, body.actual_min)

    @app.post("/shuttle/runs/{run_id}/stops/{index}/boarding")
    def board(run_id: int, index: int, body: BoardIn, db: Database = Depends(get_db)):
        return shuttle.record_boarding(
            db, run_id, index, body.booking_id, body.actual_min
        )

    @app.post("/shuttle/runs/{run_id}/stops/{index}/departure")
    def depart(run_id: int, index: int, body: ActualEventIn, db: Database = Depends(get_db)):
        return shuttle.record_departure(db, run_id, index, body.actual_min)

    # -------------------------------------------------------------- bookings
    @app.post("/bookings")
    def create_booking(body: BookingIn, db: Database = Depends(get_db)):
        view, created = shuttle.submit_booking(db, body)
        return JSONResponse(
            status_code=201 if created else 200,
            content=view.model_dump(),
        )

    @app.get("/bookings/by-request/{request_id}")
    def read_booking_by_request(request_id: str, db: Database = Depends(get_db)):
        return shuttle.get_booking_by_request(db, request_id)

    @app.get("/bookings/{booking_id}")
    def read_booking(booking_id: int, db: Database = Depends(get_db)):
        return shuttle.get_booking_out(db, booking_id)

    @app.post("/bookings/{booking_id}/cancel")
    def cancel_booking(booking_id: int, db: Database = Depends(get_db)):
        return shuttle.cancel_booking(db, booking_id)

    # ============================================================== evacuation
    def _created(view, created: bool) -> JSONResponse:
        # A fresh mutation answers 201; an identical request-id replay answers
        # 200 with the stored result, mirroring the booking endpoints.
        return JSONResponse(status_code=201 if created else 200, content=view)

    @app.post("/evac/incidents")
    def open_incident(body: IncidentIn, db: Database = Depends(get_db)):
        view, created = evacuation.create_incident(db, body)
        return _created(view, created)

    @app.get("/evac/incidents")
    def list_incidents(db: Database = Depends(get_db)):
        return [
            evacuation.incident_view(db, incident_id).model_dump()
            for incident_id in evacuation.list_incident_ids(db)
        ]

    @app.get("/evac/incidents/{incident_id}")
    def read_incident(incident_id: int, db: Database = Depends(get_db)):
        with db.lock:
            return evacuation.incident_view(db, incident_id).model_dump()

    @app.post("/evac/incidents/{incident_id}/people")
    def register_people(
        incident_id: int, body: RegisterPeopleIn, db: Database = Depends(get_db)
    ):
        view, created = evacuation.register_people(db, incident_id, body)
        return _created(view, created)

    @app.post("/evac/incidents/{incident_id}/findings")
    def record_finding(
        incident_id: int, body: FindPersonIn, db: Database = Depends(get_db)
    ):
        view, created = evacuation.record_finding(db, incident_id, body)
        return _created(view, created)

    @app.post("/evac/incidents/{incident_id}/handovers")
    def create_handover(
        incident_id: int, body: HandoverIn, db: Database = Depends(get_db)
    ):
        view, created = evacuation.create_handover(db, incident_id, body)
        return _created(view, created)

    @app.get("/evac/incidents/{incident_id}/handovers")
    def list_handovers(incident_id: int, db: Database = Depends(get_db)):
        with db.lock:
            view = evacuation.incident_view(db, incident_id)
        return [h.model_dump() for h in view.handovers]

    @app.post("/evac/incidents/{incident_id}/handovers/{handover_id}/receive")
    def receive_handover(
        incident_id: int,
        handover_id: int,
        body: HandoverActionIn,
        db: Database = Depends(get_db),
    ):
        view, created = evacuation.receive_handover(
            db, incident_id, handover_id, body
        )
        return _created(view, created)

    @app.post("/evac/incidents/{incident_id}/handovers/{handover_id}/return")
    def return_handover(
        incident_id: int,
        handover_id: int,
        body: HandoverActionIn,
        db: Database = Depends(get_db),
    ):
        view, created = evacuation.return_handover(
            db, incident_id, handover_id, body
        )
        return _created(view, created)

    @app.post("/evac/incidents/{incident_id}/zones/{zone}/search")
    def start_search(
        incident_id: int, zone: str, body: SearchIn, db: Database = Depends(get_db)
    ):
        view, created = evacuation.start_search(db, incident_id, zone, body)
        return _created(view, created)

    @app.post("/evac/incidents/{incident_id}/zones/{zone}/clearance")
    def confirm_clearance(
        incident_id: int, zone: str, body: SearchIn, db: Database = Depends(get_db)
    ):
        view, created = evacuation.confirm_clearance(db, incident_id, zone, body)
        return _created(view, created)

    @app.post("/evac/incidents/{incident_id}/close")
    def close_incident(
        incident_id: int, body: CloseIn, db: Database = Depends(get_db)
    ):
        view, created = evacuation.close_incident(db, incident_id, body)
        return _created(view, created)

    return app


app = create_app()
