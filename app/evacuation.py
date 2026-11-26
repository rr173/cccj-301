"""Evacuation headcount, handover chain of custody, and zone clearance.

Domain rules
------------
* An incident registers disjoint zones to clear and safe assembly points, both
  identified by existing stop names.  Routing suggestions and shuttle bookings
  are never evacuation evidence: only the findings and handover confirmations
  in this module move a person.
* People are registered with an incident-unique person number, either with a
  location still to be verified (``unknown``) or already known to be in a zone.
  Registration stays open while the incident is open.  Only ``unknown`` people
  can be given a finding; once located, a person must travel through handovers
  and cannot be "found" again to bypass one.
* A handover is one manifest: the sender lists the people that actually left a
  zone, which puts them in transit.  The receiver confirms the people actually
  received (a subset is allowed) and the sender can confirm people who turned
  back (also a subset).  Unresolved people stay in transit and cannot be used by
  another handover; each person is received or returned at most once on that
  manifest.  A handover to a safe point makes a person safe only on receipt.
  People already at a safe point never take part in another transfer.
* A zone can be searched only while empty and with no open handover touching
  it.  Every person entry/leave and every handover change touching the zone
  advances that zone's activity version, which invalidates the running search
  and any clearance it produced; the zone must be searched again even if it
  later becomes empty.  Other zones' activity never touches this version.
* Closure requires every person safe, every handover fully processed, and a
  valid clearance for every zone.  A closed incident keeps its results and
  rejects every further change.
* Every mutation carries an incident-scoped request id.  An identical replay
  returns the original result without redoing the work; reusing an id with
  different content is rejected.  Receive and return can never both win for the
  same person, and a batch request applies to all of its people or none.
"""
from __future__ import annotations

import hashlib
import json

from .model import (
    CloseIn,
    FindPersonIn,
    HandoverActionIn,
    HandoverIn,
    HandoverOut,
    HandoverPersonOut,
    IncidentIn,
    IncidentOut,
    PersonOut,
    RegisterPeopleIn,
    SearchIn,
    ZoneOut,
)


class EvacError(Exception):
    def __init__(
        self,
        detail: str,
        reason: str = "invalid",
        status: int = 409,
        extra: dict | None = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.status = status
        # Extra structured fields for the response body (e.g. closure blockers).
        self.extra = extra


# --------------------------------------------------------------------- helpers
def _content_hash(operation: str, payload: dict) -> str:
    blob = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_incident(db, incident_id: int):
    row = db.get_incident(incident_id)
    if row is None:
        raise EvacError(
            f"incident {incident_id} not found", "incident_not_found", 404
        )
    return row


def _location_kinds(db, incident_id: int) -> dict[str, str]:
    return {
        row["stop_name"]: row["kind"]
        for row in db.incident_locations(incident_id)
    }


def _bump_zone(db, incident_id: int, zone: str) -> None:
    """Advance a zone's activity generation and invalidate its searches."""
    db.bump_zone_version(incident_id, zone)
    db.invalidate_zone_searches(incident_id, zone)


def _replay(db, incident_id: int, request_id: str, operation: str, content: dict):
    """Return the stored response for a replay, or None for a new request."""
    existing = db.get_evac_request(incident_id, request_id)
    if existing is None:
        return None
    if existing["content_hash"] != _content_hash(operation, content):
        raise EvacError(
            f"request_id {request_id!r} was already used for a different"
            f" {existing['operation']} request; choose a new request id",
            "duplicate_request",
        )
    return json.loads(existing["response_body"])


def _store_request(
    db, incident_id: int, request_id: str, operation: str,
    content: dict, body: dict,
) -> None:
    db.insert_evac_request(
        incident_id,
        request_id,
        operation,
        _content_hash(operation, content),
        201,
        json.dumps(body, ensure_ascii=False),
    )


# ------------------------------------------------------------------ presenters
def _handover_out(db, handover_row) -> HandoverOut:
    people_rows = db.handover_people(handover_row["id"])
    people = [
        HandoverPersonOut(
            person_no=row["person_no"],
            status=row["status"],
            resolved_at_safe=(
                row["status"] == "received"
                and handover_row["destination_kind"] == "safe_point"
            ),
        )
        for row in people_rows
    ]
    return HandoverOut(
        handover_id=handover_row["id"],
        request_id=handover_row["request_id"],
        from_zone=handover_row["from_zone"],
        to_location=handover_row["to_location"],
        destination_kind=handover_row["destination_kind"],
        status=handover_row["status"],
        people=people,
        pending=[r["person_no"] for r in people_rows if r["status"] == "pending"],
        received=[r["person_no"] for r in people_rows if r["status"] == "received"],
        returned=[r["person_no"] for r in people_rows if r["status"] == "returned"],
    )


def _blockers(db, incident_id: int, zones: list[str]) -> dict[str, list[str]]:
    people_not_safe = [
        row["person_no"]
        for row in db.list_people(incident_id)
        if row["status"] != "safe"
    ]
    open_handovers = [
        str(row["id"])
        for row in db.list_handovers(incident_id)
        if row["status"] == "open"
    ]
    uncleared_zones = []
    for zone in zones:
        search = db.active_search(incident_id, zone)
        valid_clearance = (
            search is not None
            and search["valid"] == 1
            and search["cleared"] == 1
        )
        if not valid_clearance:
            uncleared_zones.append(zone)
    return {
        "people_not_at_safe_point": people_not_safe,
        "open_handovers": open_handovers,
        "zones_without_valid_clearance": uncleared_zones,
    }


def incident_view(db, incident_id: int) -> IncidentOut:
    """Build the whole ledger from the stored facts, in one consistent read."""
    incident = _load_incident(db, incident_id)
    locations = db.incident_locations(incident_id)
    zones = [r["stop_name"] for r in locations if r["kind"] == "zone"]
    safe_points = [r["stop_name"] for r in locations if r["kind"] == "safe_point"]

    people_rows = db.list_people(incident_id)
    people = [
        PersonOut(
            person_no=row["person_no"],
            status=row["status"],
            zone=row["zone"],
            safe_point=row["safe_point"],
            current_handover_id=row["current_handover_id"],
        )
        for row in people_rows
    ]
    safe_occupants: dict[str, list[str]] = {point: [] for point in safe_points}
    for row in people_rows:
        if row["status"] == "safe" and row["safe_point"] is not None:
            safe_occupants.setdefault(row["safe_point"], []).append(row["person_no"])

    handovers = [
        _handover_out(db, row) for row in db.list_handovers(incident_id)
    ]

    zone_states = []
    for zone in zones:
        occupants = [
            row["person_no"]
            for row in people_rows
            if row["status"] == "in_zone" and row["zone"] == zone
        ]
        search = db.active_search(incident_id, zone)
        zone_states.append(
            ZoneOut(
                zone=zone,
                occupants=occupants,
                open_handovers=db.open_handover_ids_for_zone(incident_id, zone),
                search_started=search is not None,
                search_valid=search is not None and search["valid"] == 1,
                cleared=(
                    search is not None
                    and search["valid"] == 1
                    and search["cleared"] == 1
                ),
                activity_version=db.zone_version(incident_id, zone),
            )
        )

    blockers = _blockers(db, incident_id, zones)
    ready = all(not values for values in blockers.values())
    return IncidentOut(
        incident_id=incident["id"],
        request_id=incident["request_id"],
        name=incident["name"],
        zones=zones,
        safe_points=safe_points,
        status=incident["status"],
        people=people,
        handovers=handovers,
        zone_states=zone_states,
        safe_occupants=safe_occupants,
        ready_to_close=ready,
        blockers=blockers,
        people_safe=sum(1 for row in people_rows if row["status"] == "safe"),
        people_total=len(people_rows),
    )


def list_incident_ids(db) -> list[int]:
    with db.lock:
        return db.list_incident_ids()


# ------------------------------------------------------------------- creation
def create_incident(db, body: IncidentIn) -> tuple[dict, bool]:
    """Returns (incident view, created).  Replays are idempotent."""
    with db.lock:
        existing = db.get_incident_by_request(body.request_id)
        if existing is not None:
            view = incident_view(db, existing["id"]).model_dump()
            # An incident's name and location sets are immutable after creation,
            # so the current ledger fully identifies the original request.
            if (
                view["name"] != body.name
                or view["zones"] != body.zones
                or view["safe_points"] != body.safe_points
            ):
                raise EvacError(
                    f"request_id {body.request_id!r} was already used for a"
                    " different incident; choose a new request id",
                    "duplicate_request",
                )
            return view, False

        unknown = [
            name
            for name in (*body.zones, *body.safe_points)
            if not db.stop_name_exists(name)
        ]
        if unknown:
            raise EvacError(
                f"unknown stop names (not used by any segment): {unknown}",
                "unknown_location",
                422,
            )

        with db.begin():
            incident_id = db.insert_incident(body.request_id, body.name)
            for zone in body.zones:
                db.insert_location(incident_id, zone, "zone")
            for point in body.safe_points:
                db.insert_location(incident_id, point, "safe_point")
            db.init_zone_versions(incident_id, list(body.zones))
        return incident_view(db, incident_id).model_dump(), True


# ------------------------------------------------------------- registration
def register_people(
    db, incident_id: int, body: RegisterPeopleIn
) -> tuple[dict, bool]:
    content = body.model_dump()
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "register", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError(
                f"incident {incident_id} is closed; no changes are accepted",
                "incident_closed",
            )

        kinds = _location_kinds(db, incident_id)
        # Validate the whole batch first: a request registers all or none.
        for entry in body.people:
            if db.get_person(incident_id, entry.person_no) is not None:
                raise EvacError(
                    f"person_no {entry.person_no!r} is already registered in"
                    f" incident {incident_id}",
                    "person_already_registered",
                )
            if entry.zone is not None and kinds.get(entry.zone) != "zone":
                raise EvacError(
                    f"{entry.zone!r} is not a zone of incident {incident_id}",
                    "zone_not_found",
                    404,
                )

        with db.begin():
            touched_zones: set[str] = set()
            for entry in body.people:
                if entry.zone is None:
                    db.insert_person(incident_id, entry.person_no, "unknown", None, None)
                else:
                    db.insert_person(
                        incident_id, entry.person_no, "in_zone", entry.zone, None
                    )
                    touched_zones.add(entry.zone)
            # A person entering a zone is activity that invalidates its search.
            for zone in touched_zones:
                _bump_zone(db, incident_id, zone)
            view = incident_view(db, incident_id).model_dump()
            _store_request(
                db, incident_id, body.request_id, "register", content, view
            )
        return view, True


# ------------------------------------------------------------------ findings
def record_finding(
    db, incident_id: int, body: FindPersonIn
) -> tuple[dict, bool]:
    content = body.model_dump()
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "find", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError("incident is closed", "incident_closed")

        person = db.get_person(incident_id, body.person_no)
        if person is None:
            raise EvacError(
                f"person_no {body.person_no!r} is not registered",
                "unknown_person",
                404,
            )
        kind = _location_kinds(db, incident_id).get(body.location)
        if kind is None:
            raise EvacError(
                f"{body.location!r} is not a location of incident {incident_id}",
                "location_not_found",
                404,
            )
        if person["status"] != "unknown":
            # A located person cannot be re-"found" to skip a handover.
            raise EvacError(
                f"person {body.person_no!r} already has a recorded location"
                f" ({person['status']}); use a handover to move them",
                "person_not_unknown",
            )

        with db.begin():
            if kind == "zone":
                db.set_person_known(
                    incident_id, body.person_no, "in_zone", body.location, None
                )
                _bump_zone(db, incident_id, body.location)
            else:
                # Found already at a safe point: safe without any transfer.
                db.set_person_known(
                    incident_id, body.person_no, "safe", None, body.location
                )
            view = incident_view(db, incident_id).model_dump()
            _store_request(db, incident_id, body.request_id, "find", content, view)
        return view, True


# ------------------------------------------------------------------ handovers
def create_handover(
    db, incident_id: int, body: HandoverIn
) -> tuple[dict, bool]:
    content = body.model_dump()
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "handover", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError("incident is closed", "incident_closed")

        kinds = _location_kinds(db, incident_id)
        if kinds.get(body.from_zone) != "zone":
            raise EvacError(
                f"{body.from_zone!r} is not a zone of incident {incident_id}",
                "zone_not_found",
                404,
            )
        destination_kind = kinds.get(body.to_location)
        if destination_kind is None:
            raise EvacError(
                f"{body.to_location!r} is not a location of incident {incident_id}",
                "location_not_found",
                404,
            )
        if body.to_location == body.from_zone:
            raise EvacError(
                "a handover must go to a different location", "illegal_transfer", 422
            )

        # Whole-batch validation before any write.
        for person_no in body.person_nos:
            person = db.get_person(incident_id, person_no)
            if person is None:
                raise EvacError(
                    f"person_no {person_no!r} is not registered",
                    "unknown_person",
                    404,
                )
            if person["status"] == "in_transit":
                raise EvacError(
                    f"person {person_no!r} is already in transit on handover"
                    f" {person['current_handover_id']} and cannot be listed again",
                    "person_in_transit",
                )
            if person["status"] == "safe":
                raise EvacError(
                    f"person {person_no!r} is already at a safe point and does"
                    " not take part in transfers",
                    "person_already_safe",
                )
            if person["status"] != "in_zone" or person["zone"] != body.from_zone:
                where = (
                    "still awaiting a location finding"
                    if person["status"] == "unknown"
                    else f"in zone {person['zone']!r}"
                )
                raise EvacError(
                    f"person {person_no!r} is not physically in zone"
                    f" {body.from_zone!r} ({where})",
                    "person_not_in_zone",
                )

        with db.begin():
            handover_id = db.insert_handover(
                incident_id,
                body.request_id,
                body.from_zone,
                body.to_location,
                destination_kind,
            )
            db.insert_handover_people(handover_id, body.person_nos)
            for person_no in body.person_nos:
                db.set_person_transit(incident_id, person_no, handover_id)
            # Creating the manifest changes both endpoints: people leave the
            # origin and a pending handover now touches the destination.
            _bump_zone(db, incident_id, body.from_zone)
            if destination_kind == "zone":
                _bump_zone(db, incident_id, body.to_location)
            view = incident_view(db, incident_id).model_dump()
            _store_request(
                db, incident_id, body.request_id, "handover", content, view
            )
        return view, True


def _load_pending_handover(db, incident_id: int, handover_id: int):
    handover = db.get_handover(incident_id, handover_id)
    if handover is None:
        raise EvacError(
            f"handover {handover_id} not found in incident {incident_id}",
            "handover_not_found",
            404,
        )
    return handover


def _resolve_batch(db, incident_id: int, handover, person_nos: list[str],
                   resolution: str) -> None:
    """Validate a receive/return batch, then apply it as one transaction body."""
    # Validate every person first so the request can never half-apply.
    for person_no in person_nos:
        row = db.handover_person_row(handover["id"], person_no)
        if row is None:
            raise EvacError(
                f"person {person_no!r} is not on handover {handover['id']}",
                "not_handover_party",
            )
        if row["status"] != "pending":
            # Receive and return are mutually exclusive: whichever request is
            # processed first resolves the person, the other loses.
            raise EvacError(
                f"person {person_no!r} was already {row['status']} on handover"
                f" {handover['id']}; each person is resolved at most once",
                "already_resolved",
            )

    if resolution == "received":
        for person_no in person_nos:
            db.set_handover_person_status(handover["id"], person_no, "received")
            if handover["destination_kind"] == "safe_point":
                db.set_person_known(
                    incident_id, person_no, "safe", None, handover["to_location"]
                )
            else:
                db.set_person_known(
                    incident_id, person_no, "in_zone",
                    handover["to_location"], None,
                )
    else:  # returned: the person is physically back at the originating zone.
        for person_no in person_nos:
            db.set_handover_person_status(handover["id"], person_no, "returned")
            db.set_person_known(
                incident_id, person_no, "in_zone", handover["from_zone"], None
            )

    db.complete_handover_if_resolved(handover["id"])


def receive_handover(
    db, incident_id: int, handover_id: int, body: HandoverActionIn
) -> tuple[dict, bool]:
    content = {"handover_id": handover_id, **body.model_dump()}
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "receive", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError("incident is closed", "incident_closed")
        handover = _load_pending_handover(db, incident_id, handover_id)

        with db.begin():
            _resolve_batch(db, incident_id, handover, body.person_nos, "received")
            # Physical arrival at a zone is activity there; a safe point has no
            # search version to disturb.
            if handover["destination_kind"] == "zone":
                _bump_zone(db, incident_id, handover["to_location"])
            view = incident_view(db, incident_id).model_dump()
            _store_request(
                db, incident_id, body.request_id, "receive", content, view
            )
        return view, True


def return_handover(
    db, incident_id: int, handover_id: int, body: HandoverActionIn
) -> tuple[dict, bool]:
    content = {"handover_id": handover_id, **body.model_dump()}
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "return", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError("incident is closed", "incident_closed")
        handover = _load_pending_handover(db, incident_id, handover_id)

        with db.begin():
            _resolve_batch(db, incident_id, handover, body.person_nos, "returned")
            # People re-enter the originating zone: that invalidates its search.
            _bump_zone(db, incident_id, handover["from_zone"])
            view = incident_view(db, incident_id).model_dump()
            _store_request(
                db, incident_id, body.request_id, "return", content, view
            )
        return view, True


# ------------------------------------------------------------- search/clearance
def start_search(
    db, incident_id: int, zone: str, body: SearchIn
) -> tuple[dict, bool]:
    content = {"zone": zone, **body.model_dump()}
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "search", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError("incident is closed", "incident_closed")
        if _location_kinds(db, incident_id).get(zone) != "zone":
            raise EvacError(
                f"{zone!r} is not a zone of incident {incident_id}",
                "zone_not_found",
                404,
            )
        if db.count_zone_occupants(incident_id, zone) > 0:
            raise EvacError(
                f"zone {zone!r} still has people present; it must be empty"
                " before a search can start",
                "zone_not_empty",
            )
        open_handovers = db.open_handover_ids_for_zone(incident_id, zone)
        if open_handovers:
            raise EvacError(
                f"zone {zone!r} has unfinished handovers {open_handovers};"
                " resolve them before searching",
                "open_handover",
            )
        active = db.active_search(incident_id, zone)
        if active is not None and active["valid"] == 1:
            raise EvacError(
                f"a search of zone {zone!r} is already in progress",
                "illegal_event",
            )

        with db.begin():
            # An invalidated attempt is retired before the fresh one starts.
            db.deactivate_zone_searches(incident_id, zone)
            db.start_search(
                incident_id, zone, db.zone_version(incident_id, zone)
            )
            view = incident_view(db, incident_id).model_dump()
            _store_request(
                db, incident_id, body.request_id, "search", content, view
            )
        return view, True


def confirm_clearance(
    db, incident_id: int, zone: str, body: SearchIn
) -> tuple[dict, bool]:
    content = {"zone": zone, **body.model_dump()}
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "clearance", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError("incident is closed", "incident_closed")
        if _location_kinds(db, incident_id).get(zone) != "zone":
            raise EvacError(
                f"{zone!r} is not a zone of incident {incident_id}",
                "zone_not_found",
                404,
            )
        search = db.active_search(incident_id, zone)
        if search is None:
            raise EvacError(
                f"no active search of zone {zone!r}; start one first",
                "no_active_search",
            )
        if search["valid"] == 0:
            raise EvacError(
                f"the search of zone {zone!r} was invalidated by activity after"
                " it started; search the zone again",
                "search_stale",
            )
        # Defence in depth: the version guarantee should already imply both.
        if db.count_zone_occupants(incident_id, zone) > 0:
            raise EvacError(
                f"zone {zone!r} is no longer empty", "search_stale"
            )
        if db.open_handover_ids_for_zone(incident_id, zone):
            raise EvacError(
                f"zone {zone!r} has a new unfinished handover", "search_stale"
            )

        with db.begin():
            db.mark_search_cleared(search["id"])
            view = incident_view(db, incident_id).model_dump()
            _store_request(
                db, incident_id, body.request_id, "clearance", content, view
            )
        return view, True


# --------------------------------------------------------------------- closure
def close_incident(
    db, incident_id: int, body: CloseIn
) -> tuple[dict, bool]:
    content = body.model_dump()
    with db.lock:
        incident = _load_incident(db, incident_id)
        replay = _replay(db, incident_id, body.request_id, "close", content)
        if replay is not None:
            return replay, False
        if incident["status"] == "closed":
            raise EvacError("incident is already closed", "incident_closed")

        view = incident_view(db, incident_id)
        if not view.ready_to_close:
            raise EvacError(
                "incident cannot be closed yet",
                "not_ready_to_close",
                extra={"blockers": view.blockers},
            )

        with db.begin():
            db.close_incident(incident_id)
            out = incident_view(db, incident_id).model_dump()
            _store_request(
                db, incident_id, body.request_id, "close", content, out
            )
        return out, True
