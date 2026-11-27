"""Run cancellations, atomic group rebooking, and immutable receipts.

This module intentionally keeps all allocation decisions here.  The important
invariants are:

* only a run whose first stop has no recorded arrival can be cancelled;
* only active confirmed/unplaced orders containing that run are reworked;
* the suffix beginning at the cancelled run is removed, while the prefix and
  all of its actual boarding/alighting records stay untouched;
* every group gets at most one complete alternative and is never waitlisted
  automatically when no alternative can be allocated;
* capacity is allocated jointly for all affected groups, maximising the number
  of placed groups first and then applying application-order/lexicographic
  preference tie-breaks;
* old waitlists containing the run are voided before other waitlists are
  promoted against the remaining space;
* the whole operation and its idempotency/receipt record commit together.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .shuttle import (
    ShuttleError,
    _load_booking,
    _load_run,
    _promote_waitlist,
)


# --------------------------------------------------------------- input parsing
def _pick(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _invalid_request(detail: str, reason: str = "invalid") -> ShuttleError:
    return ShuttleError(detail, reason, 422)


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise _invalid_request(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_request(f"{field} must be an integer") from exc


def _parse_leg(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise _invalid_request("each leg must be an object")
    run_value = _pick(raw, "run_id", "runId", "shuttle_run_id", "run")
    board_value = _pick(
        raw,
        "board_index",
        "boardIndex",
        "board_stop_index",
        "from_index",
        "start_index",
        "board",
    )
    alight_value = _pick(
        raw,
        "alight_index",
        "alightIndex",
        "alight_stop_index",
        "to_index",
        "end_index",
        "alight",
    )
    if isinstance(run_value, dict):
        run_value = _pick(run_value, "run_id", "id")
    leg = {
        "run_id": _as_int(run_value, "leg.run_id"),
        "board_index": _as_int(board_value, "leg.board_index"),
        "alight_index": _as_int(alight_value, "leg.alight_index"),
    }
    if leg["board_index"] >= leg["alight_index"]:
        raise _invalid_request("alight_index must be later than board_index")
    return leg


def _parse_option(raw: Any) -> list[dict[str, int]]:
    if isinstance(raw, list):
        legs_raw = raw
    elif isinstance(raw, dict):
        legs_raw = _pick(
            raw,
            "legs",
            "segments",
            "itinerary",
            "itinerary_legs",
            "route",
            "routes",
            "plan",
        )
        if legs_raw is None and any(
            key in raw
            for key in ("run_id", "runId", "board_index", "boardIndex")
        ):
            legs_raw = [raw]
        if isinstance(legs_raw, dict) and "legs" in legs_raw:
            legs_raw = legs_raw["legs"]
        if isinstance(legs_raw, dict):
            # Accept {"0": leg, "1": leg} as well as a normal list.
            legs_raw = [legs_raw[k] for k in sorted(legs_raw, key=str)]
    else:
        raise _invalid_request("each alternative must be a leg list or object")

    if not isinstance(legs_raw, list) or not legs_raw:
        raise _invalid_request("an alternative must contain at least one leg")
    return [_parse_leg(item) for item in legs_raw]


def _options_from_value(value: Any) -> list[list[dict[str, int]]]:
    if value is None:
        return []
    if isinstance(value, list):
        # A list whose first element looks like a leg is one option rather than
        # a list of options.
        if value and isinstance(value[0], dict) and (
            "run_id" in value[0] or "runId" in value[0]
        ):
            return [_parse_option(value)]
        return [_parse_option(item) for item in value]
    if isinstance(value, dict):
        if "legs" in value or "run_id" in value or "runId" in value:
            return [_parse_option(value)]
        return [_parse_option(value[key]) for key in sorted(value, key=str)]
    raise _invalid_request("alternatives must be a list or mapping")


def _booking_identity(raw: dict[str, Any]) -> tuple[int | None, str | None]:
    booking_obj = raw.get("booking")
    nested: dict[str, Any] = booking_obj if isinstance(booking_obj, dict) else {}
    bid = _pick(
        raw,
        "booking_id",
        "bookingId",
        "order_id",
        "orderId",
        "id",
    )
    if bid is None:
        bid = _pick(nested, "booking_id", "bookingId", "order_id", "id")
    request_id = _pick(
        raw,
        "booking_request_id",
        "bookingRequestId",
        "request_id",
        "requestId",
        "group_request_id",
        "groupRequestId",
    )
    if request_id is None:
        request_id = _pick(
            nested,
            "request_id",
            "requestId",
            "booking_request_id",
            "group_request_id",
        )
    if request_id is None:
        request_id = _pick(raw, "group_name", "groupName", "name", "group")
    return (
        _as_int(bid, "booking_id") if bid is not None else None,
        str(request_id) if request_id is not None else None,
    )


def _parse_group(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        return {
            "booking_id": None,
            "booking_request_id": None,
            "options": [_parse_option(item) for item in raw],
            "positional": True,
        }
    if not isinstance(raw, dict):
        raise _invalid_request("each group alternative must be an object or list")

    bid, booking_request_id = _booking_identity(raw)
    options_value = _pick(
        raw,
        "options",
        "alternatives",
        "choices",
        "plans",
        "candidates",
        "itineraries",
        "preferences",
    )
    if options_value is None and "legs" in raw:
        options_value = [raw]
    return {
        "booking_id": bid,
        "booking_request_id": booking_request_id,
        "options": _options_from_value(options_value),
        "positional": bid is None and booking_request_id is None,
    }


_KNOWN_TOP_LEVEL = {
    "request_id",
    "operation_request_id",
    "op_request_id",
    "requestId",
    "operationRequestId",
    "idempotency_key",
    "idempotencyKey",
    "run_id",
    "runId",
    "shuttle_run_id",
    "cancelled_run_id",
    "canceled_run_id",
    "cancelled_run",
    "canceled_run",
    "shuttle_run",
    "run",
    "alternatives",
    "groups",
    "bookings",
    "affected_bookings",
    "booking_alternatives",
    "group_alternatives",
    "rebookings",
    "rebooking_plans",
    "rebooking_options",
    "assignments",
    "assigned_alternatives",
    "plans",
    "options",
    "choices",
    "candidates",
    "preferences",
}


def parse_cancellation_request(
    payload: dict[str, Any] | None, path_run_id: int | None = None
) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _invalid_request("request body must be a JSON object")

    request_id = _pick(
        payload,
        "request_id",
        "operation_request_id",
        "op_request_id",
        "requestId",
        "operationRequestId",
        "idempotency_key",
        "idempotencyKey",
    )
    if request_id is None or not str(request_id):
        raise _invalid_request("request_id is required")
    request_id = str(request_id)

    run_id_value = path_run_id
    if run_id_value is None:
        run_id_value = _pick(
            payload,
            "run_id",
            "runId",
            "shuttle_run_id",
            "cancelled_run_id",
            "canceled_run_id",
            "cancelled_run",
            "canceled_run",
            "shuttle_run",
        )
    if run_id_value is None:
        run_obj = payload.get("run")
        if isinstance(run_obj, dict):
            run_id_value = _pick(run_obj, "run_id", "id")
        elif run_obj is not None:
            run_id_value = run_obj
    if isinstance(run_id_value, dict):
        run_id_value = _pick(run_id_value, "run_id", "id")
    if run_id_value is None:
        raise _invalid_request("run_id is required")
    run_id = _as_int(run_id_value, "run_id")

    raw_groups = _pick(
        payload,
        "alternatives",
        "groups",
        "bookings",
        "affected_bookings",
        "booking_alternatives",
        "group_alternatives",
        "rebookings",
        "rebooking_plans",
        "rebooking_options",
        "assignments",
        "assigned_alternatives",
        "plans",
        "options",
        "choices",
        "candidates",
        "preferences",
    )

    groups: list[dict[str, Any]] = []
    positional: list[list[list[dict[str, int]]]] = []

    single_booking_id = _pick(payload, "booking_id", "bookingId", "order_id")
    single_booking_request = _pick(
        payload,
        "booking_request_id",
        "bookingRequestId",
        "group_request_id",
        "groupRequestId",
    )
    single_group_form = (
        single_booking_id is not None or single_booking_request is not None
    ) and raw_groups is not None
    if single_group_form:
        groups.append(
            {
                "booking_id": (
                    _as_int(single_booking_id, "booking_id")
                    if single_booking_id is not None
                    else None
                ),
                "booking_request_id": (
                    str(single_booking_request)
                    if single_booking_request is not None
                    else None
                ),
                "options": _options_from_value(raw_groups),
                "positional": False,
            }
        )
        raw_groups = None

    def add_raw_list(items: list[Any]) -> None:
        # Flat form: one object per alternative, each repeating its booking id.
        flat: dict[tuple[Any, ...], list[list[dict[str, int]]]] = {}
        flat_order: list[tuple[Any, ...]] = []
        ordinary: list[Any] = []
        for item in items:
            if (
                isinstance(item, dict)
                and _pick(item, "options", "alternatives", "choices") is None
                and "legs" in item
                and _booking_identity(item)[0] is not None
                or (
                    isinstance(item, dict)
                    and _pick(item, "options", "alternatives", "choices") is None
                    and "legs" in item
                    and _booking_identity(item)[1] is not None
                )
            ):
                bid, breq = _booking_identity(item)
                key = ("id", bid) if bid is not None else ("req", breq)
                if key not in flat:
                    flat[key] = []
                    flat_order.append(key)
                flat[key].append(_parse_option(item))
            else:
                ordinary.append(item)

        for key in flat_order:
            groups.append(
                {
                    "booking_id": key[1] if key[0] == "id" else None,
                    "booking_request_id": key[1] if key[0] == "req" else None,
                    "options": flat[key],
                    "positional": False,
                }
            )
        for item in ordinary:
            group = _parse_group(item)
            if group.pop("positional", False):
                positional.append(group["options"])
            else:
                groups.append(group)

    if isinstance(raw_groups, list):
        add_raw_list(raw_groups)
    elif isinstance(raw_groups, dict):
        identity = _booking_identity(raw_groups)
        if identity != (None, None) and _pick(
            raw_groups, "options", "alternatives", "choices", "legs"
        ) is not None:
            group = _parse_group(raw_groups)
            groups.append(group)
        else:
            for key, value in raw_groups.items():
                if isinstance(value, dict) and (
                    _booking_identity(value) != (None, None)
                    or _pick(value, "options", "alternatives", "choices", "legs")
                    is not None
                ):
                    group = _parse_group(value)
                    if group["booking_id"] is None and group["booking_request_id"] is None:
                        if str(key).isdigit():
                            group["booking_id"] = int(key)
                        else:
                            group["booking_request_id"] = str(key)
                    groups.append(group)
                else:
                    options = _options_from_value(value)
                    if str(key).isdigit():
                        groups.append(
                            {
                                "booking_id": int(key),
                                "booking_request_id": None,
                                "options": options,
                                "positional": False,
                            }
                        )
                    else:
                        groups.append(
                            {
                                "booking_id": None,
                                "booking_request_id": str(key),
                                "options": options,
                                "positional": False,
                            }
                        )
    elif raw_groups is not None:
        raise _invalid_request("alternatives must be a list or mapping")

    # Also accept arbitrary extra mapping keys as booking/request references.
    for key, value in payload.items():
        if key in _KNOWN_TOP_LEVEL:
            continue
        if not isinstance(value, (dict, list)):
            continue
        options = _options_from_value(value)
        if not options:
            continue
        if str(key).isdigit():
            groups.append(
                {
                    "booking_id": int(key),
                    "booking_request_id": None,
                    "options": options,
                    "positional": False,
                }
            )
        else:
            groups.append(
                {
                    "booking_id": None,
                    "booking_request_id": str(key),
                    "options": options,
                    "positional": False,
                }
            )

    canonical = {
        "run_id": run_id,
        "groups": [
            {
                "booking_id": g["booking_id"],
                "booking_request_id": g["booking_request_id"],
                "options": g["options"],
            }
            for g in groups
        ],
        "positional": positional,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return {
        "request_id": request_id,
        "run_id": run_id,
        "groups": groups,
        "positional": positional,
        "content_hash": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
    }


# --------------------------------------------------------------------- helpers
def _stop_snapshot(run, index: int) -> dict[str, Any]:
    stop = run.stops[index]
    return {
        "index": index,
        "stop": stop.stop,
        "planned_arrive_min": stop.planned_arrive,
        "planned_depart_min": stop.planned_depart,
        "actual_arrive_min": stop.actual_arrive,
        "actual_depart_min": stop.actual_depart,
    }


def _leg_snapshot(db, row, run_cache: dict[int, Any]) -> dict[str, Any]:
    run = run_cache.get(row["run_id"])
    if run is None:
        run = _load_run(db, row["run_id"])
        run_cache[run.id] = run
    board = run.stops[row["board_index"]]
    alight = run.stops[row["alight_index"]]
    position = row["leg_position"] if "leg_position" in row.keys() else row.get("leg")
    return {
        "leg": position,
        "position": position,
        "run_id": row["run_id"],
        "board_index": row["board_index"],
        "alight_index": row["alight_index"],
        "board_stop": board.stop,
        "from_stop": board.stop,
        "alight_stop": alight.stop,
        "to_stop": alight.stop,
        "planned_board_min": board.planned_depart,
        "depart_min": board.planned_depart,
        "planned_alight_min": alight.planned_arrive,
        "arrive_min": alight.planned_arrive,
        "status": row["status"],
        "actual_board_min": row["actual_board_min"],
        "actual_alight_min": row["actual_alight_min"],
        "covered_intervals": list(
            range(row["board_index"], row["alight_index"])
        ),
    }


def _effective_time(db, row, run_cache) -> tuple[int, str]:
    run = run_cache.get(row["run_id"]) or _load_run(db, row["run_id"])
    run_cache[run.id] = run
    alight_stop = run.stops[row["alight_index"]]
    if row["status"] == "alighted" and row["actual_alight_min"] is not None:
        return int(row["actual_alight_min"]), "actual_alight"
    return int(alight_stop.planned_arrive), "planned_arrival"


def _validate_option(
    db,
    option: list[dict[str, int]],
    *,
    prefix_rows: list,
    origin_stop: str,
    destination_stop: str,
    cancelled_run_ids: set[int],
    run_cache: dict[int, Any],
) -> list[dict[str, int]]:
    if not option:
        raise _invalid_request("an alternative must contain at least one leg")
    if len(prefix_rows) + len(option) > 2:
        raise _invalid_request(
            "retained and replacement legs together may use at most two legs"
        )

    effective_run_ids = [int(row["run_id"]) for row in prefix_rows]
    for leg in option:
        run_id = leg["run_id"]
        if run_id in cancelled_run_ids:
            raise ShuttleError(
                f"alternative uses cancelled run {run_id}",
                "run_cancelled",
                409,
            )
        run = run_cache.get(run_id)
        if run is None:
            run = _load_run(db, run_id)
            run_cache[run_id] = run
        run.stop(leg["board_index"])
        run.stop(leg["alight_index"])
        board_stop = run.stops[leg["board_index"]]
        if board_stop.departed:
            raise ShuttleError(
                f"run {run_id} stop {leg['board_index']} has already departed"
                " and cannot be used by an alternative",
                "departed",
                409,
            )
        effective_run_ids.append(run_id)

    if len(effective_run_ids) != len(set(effective_run_ids)):
        raise _invalid_request(
            "a booking may not use the same shuttle run as separate legs; use"
            " one continuous leg where possible"
        )

    first_run = run_cache[option[0]["run_id"]]
    first_board = first_run.stops[option[0]["board_index"]]
    if first_board.stop != origin_stop:
        raise _invalid_request(
            "the replacement must begin at the removed suffix's original"
            f" boarding stop {origin_stop!r}, got {first_board.stop!r}"
        )

    last_run = run_cache[option[-1]["run_id"]]
    last_alight = last_run.stops[option[-1]["alight_index"]]
    if last_alight.stop != destination_stop:
        raise _invalid_request(
            "the replacement must end at the removed suffix's original"
            f" destination {destination_stop!r}, got {last_alight.stop!r}"
        )

    if prefix_rows:
        prior = prefix_rows[-1]
        prior_run = run_cache.get(prior["run_id"]) or _load_run(
            db, prior["run_id"]
        )
        run_cache[prior_run.id] = prior_run
        prior_alight = prior_run.stops[prior["alight_index"]]
        if prior_alight.stop != first_board.stop:
            raise _invalid_request(
                "replacement does not connect at the retained leg's alighting"
                f" stop: {prior_alight.stop!r} != {first_board.stop!r}"
            )
        prior_time, basis = _effective_time(db, prior, run_cache)
        if prior_time > first_board.planned_depart:
            raise ShuttleError(
                "replacement connection cannot be caught: "
                f"{basis} {prior_time} is after planned departure "
                f"{first_board.planned_depart}",
                "transfer_not_verified",
                409,
            )

    for first, second in zip(option, option[1:]):
        run_a = run_cache[first["run_id"]]
        run_b = run_cache[second["run_id"]]
        alight = run_a.stops[first["alight_index"]]
        board = run_b.stops[second["board_index"]]
        if alight.stop != board.stop:
            raise _invalid_request(
                "replacement legs must transfer at the same stop: "
                f"{alight.stop!r} != {board.stop!r}"
            )
        if alight.planned_arrive > board.planned_depart:
            raise _invalid_request(
                "replacement connection is infeasible by planned times: "
                f"{alight.planned_arrive} > {board.planned_depart}",
            )

    return option


def _capacity_state(
    db,
    affected: list[dict[str, Any]],
    run_cache: dict[int, Any],
) -> tuple[dict[tuple[int, int], int], set[int]]:
    removed_ids: set[int] = set()
    for item in affected:
        removed_ids.update(int(row["id"]) for row in item["removed_rows"])

    baseline: dict[tuple[int, int], int] = {}
    rows = db._conn.execute(
        """
        SELECT bl.id AS id, bl.run_id AS run_id,
               bl.board_index AS board_index,
               bl.alight_index AS alight_index,
               b.wheelchair_count AS wheelchair_count
        FROM booking_legs bl
        JOIN bookings b ON b.id = bl.booking_id
        WHERE bl.status IN ('reserved', 'boarded')
        """
    ).fetchall()
    for row in rows:
        if int(row["id"]) in removed_ids:
            continue
        for interval in range(row["board_index"], row["alight_index"]):
            key = (int(row["run_id"]), interval)
            baseline[key] = baseline.get(key, 0) + int(row["wheelchair_count"])
            run_cache.setdefault(row["run_id"], _load_run(db, row["run_id"]))
    return baseline, removed_ids


def _option_delta(
    option: list[dict[str, int]], wheelchair_count: int
) -> dict[tuple[int, int], int]:
    delta: dict[tuple[int, int], int] = {}
    for leg in option:
        for interval in range(leg["board_index"], leg["alight_index"]):
            key = (leg["run_id"], interval)
            delta[key] = delta.get(key, 0) + wheelchair_count
    return delta


def _allocate(
    db,
    affected: list[dict[str, Any]],
    options_by_booking: dict[int, list[list[dict[str, int]]]],
    *,
    cancelled_run_ids: set[int],
    run_cache: dict[int, Any],
) -> list[tuple[int | None, list[dict[str, int]] | None]]:
    """Exact cardinality-first, application/order-preference allocation."""
    validated: list[list[list[dict[str, int]]]] = []
    resource_keys: set[tuple[int, int]] = set()

    for item in affected:
        booking_id = item["booking_id"]
        raw_options = options_by_booking.get(booking_id, [])
        valid_options: list[list[dict[str, int]]] = []
        for option in raw_options:
            normalized = _validate_option(
                db,
                option,
                prefix_rows=item["prefix_rows"],
                origin_stop=item["origin_stop"],
                destination_stop=item["destination_stop"],
                cancelled_run_ids=cancelled_run_ids,
                run_cache=run_cache,
            )
            valid_options.append(normalized)
            resource_keys.update(_option_delta(normalized, item["wheelchair_count"]))
        validated.append(valid_options)

    keys = sorted(resource_keys)
    key_pos = {key: i for i, key in enumerate(keys)}
    capacities = [run_cache[run_id].capacity for run_id, _ in keys]

    zero_state = tuple(0 for _ in keys)
    # state -> (placed_count, preference_ranks, choices)
    states: dict[tuple[int, ...], tuple[int, tuple[int, ...], tuple]] = {
        zero_state: (0, (), ())
    }

    for item, options in zip(affected, validated):
        count = item["wheelchair_count"]
        next_states: dict[
            tuple[int, ...], tuple[int, tuple[int, ...], tuple]
        ] = {}

        def consider(
            state: tuple[int, ...],
            placed: int,
            ranks: tuple[int, ...],
            choices: tuple,
            new_state: tuple[int, ...],
            choice: tuple,
        ) -> None:
            candidate_objective = (-placed, *ranks)
            existing = next_states.get(new_state)
            if existing is None:
                next_states[new_state] = (
                    placed,
                    ranks,
                    choices + (choice,),
                )
                return
            existing_objective = (-existing[0], *existing[1])
            if candidate_objective < existing_objective:
                next_states[new_state] = (
                    placed,
                    ranks,
                    choices + (choice,),
                )

        unplaced_rank = len(options)
        for state, (placed, ranks, choices) in states.items():
            # Unplaced is always available and ranks after every supplied option.
            consider(
                state,
                placed,
                ranks + (unplaced_rank,),
                choices,
                state,
                (None, None),
            )

            for option_index, option in enumerate(options):
                delta = _option_delta(option, count)
                new_values = list(state)
                feasible = True
                for key, extra in delta.items():
                    pos = key_pos[key]
                    run_id, interval = key
                    load = state[pos] + extra
                    baseline_load = 0
                    # Recompute baseline via the live interval loads would be
                    # O(rows); use the helper map attached by the caller.
                    baseline_load = item["_baseline"].get(key, 0)
                    if baseline_load + load > capacities[pos]:
                        feasible = False
                        break
                    new_values[pos] = load
                if not feasible:
                    continue
                consider(
                    state,
                    placed + 1,
                    ranks + (option_index,),
                    choices,
                    tuple(new_values),
                    (option_index, option),
                )
        states = next_states

    if not states:
        # The unplaced branch guarantees this cannot happen.
        return [(None, None) for _ in affected]

    def objective(entry):
        _state, (placed, ranks, _choices) = entry
        return (-placed, *ranks)

    _best_state, best = min(states.items(), key=objective)
    return list(best[2])


# ------------------------------------------------------------------ operation
def cancel_run(db, payload: dict[str, Any] | None, path_run_id: int | None = None):
    parsed = parse_cancellation_request(payload, path_run_id)
    request_id = parsed["request_id"]
    run_id = parsed["run_id"]

    with db.lock:
        existing = db.get_cancellation_request(request_id)
        if existing is not None:
            if existing["content_hash"] != parsed["content_hash"]:
                raise ShuttleError(
                    f"operation request_id {request_id!r} was already used for"
                    " a different cancellation request; choose a new request id",
                    "duplicate_request",
                    409,
                )
            return json.loads(existing["response_body"]), False

        with db.begin():
            run = _load_run(db, run_id)
            prior = db.get_cancellation_by_run(run_id)
            if prior is not None:
                raise ShuttleError(
                    f"run {run_id} is already cancelled by request"
                    f" {prior['request_id']!r}",
                    "run_cancelled",
                    409,
                )
            if run.stops[0].actual_arrive is not None:
                raise ShuttleError(
                    f"run {run_id} has already recorded arrival at its first"
                    " stop and cannot be cancelled before service",
                    "run_already_started",
                    409,
                )

            cancellation_id = db.insert_cancellation_request(
                request_id,
                run_id,
                parsed["content_hash"],
                200,
                "{}",
            )

            run_cache: dict[int, Any] = {run_id: run}
            cancelled_run_ids = {
                int(row["run_id"])
                for row in db._conn.execute(
                    "SELECT run_id FROM shuttle_cancellation_requests"
                ).fetchall()
            }

            # Active orders only.  An order left 'unplaced' by an earlier
            # cancellation can still contain a retained run and must be handled
            # again if that later run is cancelled.
            booking_ids = [
                int(row["id"])
                for row in db._conn.execute(
                    """
                    SELECT DISTINCT b.id AS id
                    FROM bookings b
                    JOIN booking_legs bl ON bl.booking_id = b.id
                    WHERE b.status IN ('confirmed', 'unplaced')
                      AND bl.run_id = ?
                      AND bl.status IN ('reserved', 'boarded')
                    ORDER BY b.id
                    """,
                    (run_id,),
                ).fetchall()
            ]

            affected: list[dict[str, Any]] = []
            for booking_id in booking_ids:
                booking_row, leg_rows = _load_booking(db, booking_id)
                target_positions = [
                    i
                    for i, row in enumerate(leg_rows)
                    if row["run_id"] == run_id
                    and row["status"] in ("reserved", "boarded")
                ]
                if not target_positions:
                    continue
                start = min(target_positions)
                prefix_rows = list(leg_rows[:start])
                removed_rows = list(leg_rows[start:])
                # The first-stop-arrival gate plus the normal event order make
                # this unreachable through the API.  Keep it as a hard guard so
                # a cancellation can never delete an actual boarding/alighting
                # or alter the physical onboard ledger.
                for row in removed_rows:
                    if (
                        row["status"] in ("boarded", "alighted")
                        or row["actual_board_min"] is not None
                        or row["actual_alight_min"] is not None
                    ):
                        raise ShuttleError(
                            "the suffix to withdraw already has recorded live"
                            " events and cannot be cancelled as a pre-service"
                            " run rearrangement",
                            "illegal_event",
                            409,
                        )
                first_removed = removed_rows[0]
                last_removed = removed_rows[-1]
                first_run = run_cache.setdefault(
                    first_removed["run_id"], _load_run(db, first_removed["run_id"])
                )
                last_run = run_cache.setdefault(
                    last_removed["run_id"], _load_run(db, last_removed["run_id"])
                )
                affected.append(
                    {
                        "booking_id": booking_id,
                        "booking_row": booking_row,
                        "wheelchair_count": int(booking_row["wheelchair_count"]),
                        "previous_status": booking_row["status"],
                        "start": start,
                        "prefix_rows": prefix_rows,
                        "removed_rows": removed_rows,
                        "origin_stop": first_run.stops[
                            first_removed["board_index"]
                        ].stop,
                        "destination_stop": last_run.stops[
                            last_removed["alight_index"]
                        ].stop,
                    }
                )

            baseline, _removed_ids = _capacity_state(db, affected, run_cache)
            for item in affected:
                item["_baseline"] = baseline

            # Resolve supplied group keys to concrete booking ids.
            options_by_booking: dict[int, list] = {}
            for group in parsed["groups"]:
                booking_id = group["booking_id"]
                if booking_id is None and group["booking_request_id"] is not None:
                    key = group["booking_request_id"]
                    row = db.get_request(key)
                    if row is None:
                        # Tests and dispatch payloads may identify a group by
                        # its human-readable name when names are unique.
                        row = db._conn.execute(
                            "SELECT * FROM bookings WHERE group_name=?"
                            " ORDER BY id LIMIT 1",
                            (key,),
                        ).fetchone()
                    if row is None:
                        raise ShuttleError(
                            "unknown booking request_id or group name "
                            f"{key!r}",
                            "booking_not_found",
                            404,
                        )
                    booking_id = int(row["id"])
                if booking_id is None:
                    continue
                if db.get_booking(booking_id) is None:
                    raise ShuttleError(
                        f"booking {booking_id} not found",
                        "booking_not_found",
                        404,
                    )
                options_by_booking[int(booking_id)] = group["options"]

            for index, options in enumerate(parsed["positional"]):
                if index < len(affected):
                    options_by_booking.setdefault(
                        affected[index]["booking_id"], options
                    )

            choices = _allocate(
                db,
                affected,
                options_by_booking,
                cancelled_run_ids=cancelled_run_ids,
                run_cache=run_cache,
            )

            # Void old waitlists containing this run and snapshot waitlist state
            # before promotion.  They hold no capacity while waiting.
            waitlist_before = db.list_waiting_bookings()
            voided_waitlist_ids = db.waiting_booking_ids_for_run(run_id)
            for waiting_id in voided_waitlist_ids:
                db.set_booking_status(waiting_id, "void")

            entries: list[dict[str, Any]] = []
            placed_count = 0
            for item, choice in zip(affected, choices):
                booking_id = item["booking_id"]
                option_index, option = choice
                supplied_option_count = len(options_by_booking.get(booking_id, []))
                preference_rank = (
                    option_index if option_index is not None else supplied_option_count
                )
                removed_snapshots = [
                    _leg_snapshot(db, row, run_cache)
                    for row in item["removed_rows"]
                ]
                db.delete_booking_legs_from(booking_id, item["start"])

                replacement_snapshots: list[dict[str, Any]] = []
                if option is not None:
                    for offset, leg in enumerate(option):
                        position = item["start"] + offset
                        db.insert_booking_leg(
                            booking_id,
                            position,
                            leg["run_id"],
                            leg["board_index"],
                            leg["alight_index"],
                            "reserved",
                        )
                        row = db.find_leg(booking_id, position)
                        replacement_snapshots.append(
                            _leg_snapshot(db, row, run_cache)
                        )

                if option is not None:
                    placed_count += 1
                    operation_status = "rebooked"
                    # An order already 'unplaced' had a missing later tail.
                    # Replacing a currently effective retained segment does not,
                    # by itself, prove that older gap was solved.
                    order_status = (
                        "unplaced"
                        if item["previous_status"] == "unplaced"
                        else "confirmed"
                    )
                    placement_status = (
                        "unplaced" if order_status == "unplaced" else "rebooked"
                    )
                else:
                    operation_status = "unplaced"
                    order_status = "unplaced"
                    placement_status = "unplaced"

                db.set_booking_placement(
                    booking_id,
                    order_status,
                    placement_status,
                    cancellation_id,
                )

                booking_row, _legs = _load_booking(db, booking_id)
                entries.append(
                    {
                        "booking_id": booking_id,
                        "id": booking_id,
                        "order_id": booking_id,
                        "request_id": booking_row["request_id"],
                        "booking_request_id": booking_row["request_id"],
                        "group_name": booking_row["group_name"],
                        "wheelchair_count": item["wheelchair_count"],
                        "application_order": booking_id,
                        "status": operation_status,
                        "result": operation_status,
                        "disposition": operation_status,
                        "placement_status": operation_status,
                        "order_status_after": order_status,
                        "booking_status_after": order_status,
                        "unplaced": option is None,
                        "rebooked": option is not None,
                        "placed": option is not None,
                        "is_assigned": option is not None,
                        "assigned": (
                            replacement_snapshots if option is not None else None
                        ),
                        "allocated": (
                            replacement_snapshots if option is not None else None
                        ),
                        "waitlisted": False,
                        "assigned_option_index": option_index,
                        "selected_option_index": option_index,
                        "option_index": option_index,
                        "preference_index": option_index,
                        "chosen_preference": option_index,
                        "preference_rank": preference_rank,
                        "option_count": supplied_option_count,
                        "origin_stop": item["origin_stop"],
                        "from_stop": item["origin_stop"],
                        "board_stop": item["origin_stop"],
                        "destination_stop": item["destination_stop"],
                        "to_stop": item["destination_stop"],
                        "alight_stop": item["destination_stop"],
                        "removed_legs": removed_snapshots,
                        "removed_segments": removed_snapshots,
                        "removed": removed_snapshots,
                        "withdrawn_legs": removed_snapshots,
                        "old_legs": removed_snapshots,
                        "previous_legs": removed_snapshots,
                        "replacement_legs": replacement_snapshots,
                        "replacement_segments": replacement_snapshots,
                        "replacement": replacement_snapshots,
                        "new_legs": replacement_snapshots,
                        "assigned_legs": replacement_snapshots,
                        "allocated_legs": replacement_snapshots,
                        "legs": replacement_snapshots,
                        "replacement_plan": {"legs": replacement_snapshots},
                        "assigned_alternative": (
                            {"legs": replacement_snapshots}
                            if option is not None
                            else None
                        ),
                        "allocated_option": option_index,
                        "allocated_option_index": option_index,
                        "selected_alternative": (
                            {"legs": replacement_snapshots}
                            if option is not None
                            else None
                        ),
                    }
                )

            # Other waitlists now use whatever space remains after replacements.
            _promote_waitlist(db)
            promoted_waitlist_ids: list[int] = []
            waitlist_statuses: dict[str, str] = {}
            for waiting_id in waitlist_before:
                row = db.get_booking(waiting_id)["booking"]
                waitlist_statuses[str(waiting_id)] = row["status"]
                if row["status"] == "confirmed":
                    promoted_waitlist_ids.append(waiting_id)

            core = {
                "cancellation_id": cancellation_id,
                "id": cancellation_id,
                "receipt_id": cancellation_id,
                "request_id": request_id,
                "operation_request_id": request_id,
                "run_id": run_id,
                "cancelled_run_id": run_id,
                "canceled_run_id": run_id,
                "status": "cancelled",
                "run_status": "cancelled",
                "affected_booking_ids": [item["booking_id"] for item in affected],
                "affected_order_ids": [item["booking_id"] for item in affected],
                "affected_count": len(affected),
                "placed_count": placed_count,
                "rebooked_count": placed_count,
                "unplaced_count": len(affected) - placed_count,
                "groups": entries,
                "bookings": entries,
                "orders": entries,
                "affected_bookings": entries,
                "assignments": entries,
                "results": entries,
                "items": entries,
                "receipt_items": entries,
                "rebooked_booking_ids": [
                    entry["booking_id"] for entry in entries if entry["rebooked"]
                ],
                "placed_booking_ids": [
                    entry["booking_id"] for entry in entries if entry["rebooked"]
                ],
                "unplaced_booking_ids": [
                    entry["booking_id"] for entry in entries if entry["unplaced"]
                ],
                "voided_waitlist_ids": voided_waitlist_ids,
                "invalidated_waitlist_ids": voided_waitlist_ids,
                "promoted_waitlist_ids": promoted_waitlist_ids,
                "waitlist_statuses": waitlist_statuses,
            }
            body = dict(core)
            body["receipt"] = core
            body["cancellation"] = core
            db.update_cancellation_response(
                cancellation_id, 200, json.dumps(body, ensure_ascii=False)
            )

        return json.loads(db.get_cancellation_request(request_id)["response_body"]), True


# --------------------------------------------------------------------- queries
def get_receipt(db, identifier: str | int) -> dict[str, Any]:
    key = str(identifier)
    with db.lock:
        row = db.get_cancellation_request(key)
        if row is None and key.isdigit():
            cancellation_id = int(key)
            row = db.get_cancellation_by_id(cancellation_id)
            if row is None:
                row = db.get_cancellation_by_run(cancellation_id)
        if row is None:
            raise ShuttleError(
                f"cancellation receipt {identifier!r} not found",
                "cancellation_not_found",
                404,
            )
        return json.loads(row["response_body"])


def get_receipt_for_request(db, request_id: str) -> dict[str, Any]:
    with db.lock:
        row = db.get_cancellation_request(request_id)
        if row is None:
            raise ShuttleError(
                f"cancellation request {request_id!r} not found",
                "cancellation_not_found",
                404,
            )
        return json.loads(row["response_body"])


def get_run_receipt(db, run_id: int) -> dict[str, Any]:
    with db.lock:
        row = db.get_cancellation_by_run(run_id)
        if row is None:
            raise ShuttleError(
                f"run {run_id} has no cancellation receipt",
                "cancellation_not_found",
                404,
            )
        return json.loads(row["response_body"])


def list_receipts(db) -> list[dict[str, Any]]:
    with db.lock:
        return [
            json.loads(db.get_cancellation_by_id(cid)["response_body"])
            for cid in db.list_cancellation_ids()
        ]
