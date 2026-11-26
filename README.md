# Accessible Exhibition Routing (FastAPI + SQLite)

Backend for a large exhibition site: visitors ask for a wheelchair-accessible
walking/shuttle itinerary, staff record and lift temporary segment closures, and
visitors confirm the segments they actually walked so only the remaining part of
a trip is ever re-planned.

Times are integer minutes within **one service day** (`0..1440`). No maps,
positioning, or external navigation — just the routing service.

## Rules

**Availability**

- A segment is directed, has a positive duration, one or more open windows, and
  a wheelchair flag (`wheelchair_accessible`).
- Windows are half-open `[open_min, close_min)`. A traversal `[d, d+duration)`
  must fit inside a *single* open window (touching windows are merged), and may
  end exactly at `close_min`.
- Closures are `[start_min, end_min)`. **Any** non-empty overlap with the
  traversal blocks it; a closure starting exactly at the arrival minute does
  not.
- The same segment may carry many independent closures, each with its own id.

**Planning**

A request supplies origin, destination, departure minute, latest-arrival
minute, and `max_wait_min`. The wait cap applies before *every* boarding,
including the one at the origin. Only accessible segments are considered.

Solutions are ranked, in order, by:

1. earliest arrival,
2. fewest legs,
3. lexicographically smallest sequence of segment ids,
4. lexicographically smallest sequence of leg departure minutes.

If nothing satisfies the constraints the API answers `422` with
`reason: "no_solution"`.

**Live trips**

- `POST /plans` creates a trip and returns its itinerary.
- `POST /trips/{id}/confirm` reports that the *next planned* segment was walked,
  with the actual arrival minute at its end stop.
- `GET /trips/{id}` reads the trip. It always shows only the **unwalked** part;
  the visitor is never asked to return to a stop they have already left.

A trip is anchored at the original origin + departure minute until a leg is
confirmed, then at the last stop actually reached, at the minute actually
reached. Whenever closures change (or an arrival is confirmed) the untouched
tail is kept verbatim if every scheduled departure still satisfies the
constraints; otherwise the tail is re-planned from the anchor. If no itinerary
exists right now, the anchor and walked history are preserved (`solved: false`)
and the trip resumes automatically when the network becomes usable again.

Confirmations naming a segment that is no longer the next planned leg — because
the plan was re-routed — are rejected with `409 stale_confirmation`, and
confirmations that would move time backwards are rejected with
`409 time_went_backwards`. Rejected confirmations never mutate the trip.

Closure changes are handled lazily: the closure version is bumped immediately,
and each trip re-validates on its next read/confirm, so a closure never makes an
unrelated request wait for a large re-plan.

## API

| Method   | Path                     | Purpose                                   |
|----------|--------------------------|-------------------------------------------|
| `POST`   | `/segments`              | add an accessible directed segment        |
| `GET`    | `/segments`              | list segments and their open windows      |
| `POST`   | `/closures`              | register a closure event on a segment     |
| `DELETE` | `/closures/{id}`         | lift a single closure event               |
| `GET`    | `/closures`              | list active closures                      |
| `POST`   | `/plans`                 | plan an itinerary, creating a trip        |
| `GET`    | `/trips/{id}`            | read the remaining plan and history       |
| `POST`   | `/trips/{id}/confirm`    | confirm the next leg and its arrival time |
| `POST`   | `/shuttle/runs`          | register a shuttle run and its stops      |
| `GET`    | `/shuttle/runs`          | list registered runs                      |
| `GET`    | `/shuttle/runs/{id}`     | run state, per-interval space and times   |
| `GET`    | `/shuttle/runs/{id}/manifest` | who is onboard now and ride history   |
| `POST`   | `/shuttle/runs/{id}/stops/{i}/arrival`   | record actual arrival at stop `i` |
| `POST`   | `/shuttle/runs/{id}/stops/{i}/boarding`  | board one whole confirmed group   |
| `POST`   | `/shuttle/runs/{id}/stops/{i}/departure` | record actual departure at stop `i` |
| `POST`   | `/bookings`              | submit/confirm a group booking (idempotent) |
| `GET`    | `/bookings/{id}`         | read one booking by its id                |
| `GET`    | `/bookings/by-request/{request_id}` | read one booking by request id   |
| `POST`   | `/bookings/{id}/cancel`  | cancel before first boarding              |

Example:

```bash
curl -s localhost:8000/segments -X POST -H 'content-type: application/json' -d '{
  "id": "B1", "from_stop": "Entrance", "to_stop": "SouthPier",
  "minutes": 8, "windows": [{"open_min": 600, "close_min": 1080}],
  "wheelchair_accessible": true }'

curl -s localhost:8000/plans -X POST -H 'content-type: application/json' -d '{
  "origin": "Entrance", "destination": "Exit",
  "depart_min": 600, "arrive_by_min": 720, "max_wait_min": 10 }'

curl -s localhost:8000/trips/1/confirm -X POST -H 'content-type: application/json' -d '{
  "segment_id": "B1", "arrival_min": 610 }'
```

## Shuttle runs and wheelchair-space booking

The routing API tells a visitor how to walk; the shuttle API guarantees they
can actually get *on* a vehicle.  Staff register runs over the same accessible
segments, groups book whole-journey wheelchair spaces, and staff drive each
run stop-by-stop.  All times are still integer minutes in the one service day.

### Registering a run

`POST /shuttle/runs` takes an ordered stop list.  Every consecutive stop pair
must already be joined by at least one `wheelchair_accessible` segment; the
run does not invent new paths.  Each stop carries planned arrival/departure
minutes, which must never go backwards in stop order (equal minutes at a
boundary are allowed).  The same stop name may appear more than once —
bookings always name a concrete **stop index**, never just a name.

```bash
curl -s localhost:8000/shuttle/runs -X POST -H 'content-type: application/json' -d '{
  "wheelchair_capacity": 2,
  "stops": [
    {"stop": "Gate",  "arrive_min": 600, "depart_min": 602},
    {"stop": "Plaza", "arrive_min": 610, "depart_min": 612},
    {"stop": "Dock",  "arrive_min": 620, "depart_min": 620}
  ]}'
```

The response includes, for each adjacent-stop **interval**, the occupied
wheelchair count, capacity, and remaining spaces.  A wheelchair space is held
only on the intervals a booking covers and is reusable by the next group once
the first group alights.

### Booking as a group

`POST /bookings` names a client-generated `request_id`, the group's
wheelchair-space count, and one or two legs.  A one-leg request is a direct
ride; a two-leg request is a transfer between two different runs.  Each leg
gives `run_id`, `board_index`, and `alight_index`.  For a transfer the
alighting stop of leg 1 and the boarding stop of leg 2 must be the same stop,
and the planned arrival must be no later than the connecting departure.

```bash
curl -s localhost:8000/bookings -X POST -H 'content-type: application/json' -d '{
  "request_id": "group-77",
  "group_name": "River family",
  "wheelchair_count": 2,
  "legs": [
    {"run_id": 1, "board_index": 0, "alight_index": 1},
    {"run_id": 2, "board_index": 0, "alight_index": 1}
  ]}'
```

- **Whole-order confirmation.** Every leg must have enough space on every
  interval it covers.  If any leg is short, the whole order is returned with
  `status: "waiting"` and holds no space at all — a two-leg request is never
  half-reserved.  The response's `waitlist_reason` names the first interval
  that blocks it.
- **No booking onto a departed stop.** A stop whose departure is already
  recorded cannot be chosen as a boarding stop (on either leg).
- **Idempotency.** Re-posting the same `request_id` with identical content
  returns the original order with HTTP `200` (a new order returns `201`).
  Reusing a `request_id` with different content is rejected with `409
  duplicate_request`; change the request id to change the booking.
- **Cancellation.** `POST /bookings/{id}/cancel` is allowed only before the
  group's first boarding.  It releases every reserved interval and immediately
  re-runs waitlist promotion.

### Waitlist promotion

After every new order, cancellation, departure, or an arrival that frees
intervals (groups alighting), still-valid waitlisted groups are considered in
original application order.  A group that still cannot fit is skipped; the
first group whose whole order fits is confirmed; evaluation then continues
with the next group against the space that remains.  A waitlisted order whose
boarding stop departs is marked `void` and can never be promoted.

### Driving a run (staff)

For each stop, staff call arrival, then any boardings, then departure; the
last stop ends on arrival (it has no departure).  Actual minutes on one run
must never go backwards.

- `POST /shuttle/runs/{id}/stops/{i}/arrival` — records the actual minute.
  Groups onboard whose ride ends here are automatically marked alighted.  At
  the last stop any group still onboard alights and the run is `finished`.
- `POST /shuttle/runs/{id}/stops/{i}/boarding` — boards one **whole group**
  (`booking_id` + `actual_min`).  It requires a confirmed, unused reservation
  boarding at this stop.  For a connecting leg it additionally verifies the
  first run already recorded this group alighting at the transfer stop, no
  later than this boarding minute (`409 transfer_not_verified` otherwise).
  Boarding after departure is refused.
- `POST /shuttle/runs/{id}/stops/{i}/departure` — records the actual minute.
  A confirmed group that reserved boarding here but never boarded is a
  no-show: its remaining un-ridden reservation is voided immediately, while
  any leg already ridden stays on the onboard list until its own alighting
  stop.  Waitlisted groups boarding here are voided, then the waitlist is
  promoted.

`GET /shuttle/runs/{id}/manifest` returns the live ledger: `onboard` is
exactly the groups physically on the vehicle now, and `history` lists every
completed alighting.  `GET /bookings/{id}` shows the per-leg status
(`reserved` / `boarded` / `alighted` / `void`) and the order status
(`waiting` / `confirmed` / `cancelled` / `void` / `completed`).

### Concurrency and persistence

Every mutating operation runs under one process-wide lock and a single SQLite
transaction, so concurrent bookings cannot oversell an interval and a reader
can never observe a two-leg reservation with only one leg written.  All runs,
bookings, per-leg boarding/alighting minutes, and the onboard ledger are
persisted; restarting the server against the same database restores every
status and the current manifest.  Illegal operations are rejected before any
write and never change stored results.

The shuttle service is deliberately separate from the planner: it never
chooses runs for a visitor, never re-books because an accessible segment is
closed, and performs no payment or UI work.  The original routing and
live-trip endpoints are unchanged.

## Planner

Backward dynamic program over `(stop, minute)` states. For each accessible
segment the feasible boarding minutes are precomputed from its windows and
closures, then a monotonic deque keeps the best boarding still inside the
`max_wait` window as time sweeps backwards. Only candidate boardings and their
expiries are visited, and per-stop values are stored as step functions, so a
plan on a realistic exhibition network stays well under a second. The ordering
of the DP value is exactly the four-level ranking above.

## Running

```bash
pip install -r requirements-dev.txt
./run.sh                       # http://127.0.0.1:8000  (SQLite: exhibition.db)
python3 -m app.demo            # seed a tiny demo network, print a plan
python3 -m app.demo_shuttle    # book and ride a two-leg shuttle transfer
python3 -m pytest              # full test suite
```

The database path defaults to `exhibition.db` in the working directory;
`create_app(path)` accepts any SQLite path (tests use a temp file).

## Tests

`tests/test_planner.py` cross-checks the planner against an exhaustive search on
hundreds of random networks, plus focused cases for the half-open rules, closure
overlap, waiting caps, accessibility filtering and each tie-break.

`tests/test_api.py` covers the two required scenarios end to end:

- **Parallel first legs.** Two routes leave at the same minute; the one that
  reaches the interchange earlier would blow the wait cap on its connection,
  while the later one connects and is chosen.
- **Walk, then re-route.** The visitor confirms one leg, a closure then takes out
  the rest of the original route, and only the unwalked tail is re-planned from
  the stop actually reached — the walked leg is never repeated.

It also covers stale-confirmation rejection, the no-backwards-time rule,
unsolvable-then-recovered trips, and that unrelated closures leave a plan
untouched.

`tests/test_shuttle.py` covers the shuttle booking core end to end:

- run registration constrained to accessible segments, monotone schedules,
  and repeated stop names addressed by index;
- whole-order confirmation vs. all-or-nothing waitlisting, interval-scoped
  space that frees on alighting, and no half-written two-leg reservations;
- two-leg transfers including same-stop and timing rules, plus connecting
  boarding that requires a recorded, on-time alighting on the first run;
- request-id idempotency and rejection of reused ids with changed content;
- the arrive → board → depart event order, monotone actual minutes, whole-group
  boarding, last-stop completion, and the live onboard manifest;
- no-show voiding of remaining reservations while ridden legs stay onboard,
  waitlist invalidation when a boarding stop departs, and in-order promotion
  after cancellation/arrival/departure;
- state surviving a full application restart against the same SQLite file, and
  concurrent bookings that never oversell a single wheelchair space.
