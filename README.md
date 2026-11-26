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
