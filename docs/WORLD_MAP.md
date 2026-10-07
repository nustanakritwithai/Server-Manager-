# World map

The admin Map tab is a god view of the live world. It shows every city, every army, and every in-progress movement at the current game time. There is no fog of war and no per-player filter unless the operator asks for one. The tab is read-only: it does not send commands, edit resources, or run SQL.

The server is the source of truth. The browser draws the JSON from `GET /v1/admin/world-map` and does not invent coordinates.

## Where coordinates come from

They are already stored. This feature does not add a migration, a world seed, or a placement formula.

| Row | Stored fields | What the map uses |
| --- | --- | --- |
| City | integer `x`, `y` | The city marker |
| Movement | `origin_x`, `origin_y`, `destination_x`, `destination_y`, `depart_at`, `arrive_at` | The travel line, and the current point |
| Army | `location_city_id`, `status` | Garrisoned armies sit on that city's stored coordinates |

An in-progress leg (outbound `move` or `attack`, or `return`) is placed with the same functions the rest of the server uses:

```
progress = travel_progress(depart_at, arrive_at, now)   # clamped to 0..1
x, y     = interpolate(origin, destination, progress)
```

`now` is the game clock (`world_state.offset_seconds` added to the process clock). At departure the point is the origin. At the midpoint it is halfway along the segment. At and after `arrive_at`, while the leg is still `in_progress`, the point is the destination and `eta_seconds` is 0. The worker, not this endpoint, resolves the arrival.

A recall stores the interpolated turn-around point as the return leg's origin. The map reports that stored origin; it does not recompute the recall.

Coordinates in the response are rounded to 4 decimal places, matching `present._position`. The progress fraction is rounded to 6 decimal places. The position used for the marker is the interpolated point, then rounded.

There is no neutral or NPC table. `cities.player_id` and `armies.player_id` are required foreign keys. `neutral_entities` is `NONE`, meaning the schema has nothing else to plot. `coordinate_source` is `stored`. `fog_of_war` is false.

If an army is not garrisoned on a known city and has no in-progress movement (a destroyed army, or a row the server cannot place), `position` is null and `position_state` is `UNKNOWN`. The response does not substitute `(0, 0)`.

## HTTP API

`GET /v1/admin/world-map`

Same admin auth as the other `/v1/admin` routes: a bearer session from `POST /v1/admin/login`, or `X-Admin-Token`. A missing or wrong token is `401`. The route does not write. It does not accrue production, claim events, or bump `world_version`.

| Query | Default | Meaning |
| --- | --- | --- |
| `player_id` | omitted | Limit cities, armies, and movements to that owner. Omit it for every player. Unknown id is `404`. |
| `city_limit` | 2000 | 1..5000. Rows are oldest id first. |
| `army_limit` | 2000 | 1..5000. |
| `movement_limit` | 1000 | 1..2000. Only `in_progress` legs are returned. |

`limits` reports `returned`, `total`, and `truncated` for cities, armies, movements, and the player directory. Bounds cover the **returned** rows, not rows left off by a limit. An army that is returned is still placed from its own in-progress leg even when that leg did not fit in the movement page.

The player directory (`players`) stays unfiltered, up to 2000 rows, so the Map tab can switch owners. It is not a second copy of the entity lists.

### Response

| Field | Meaning |
| --- | --- |
| `server_time` | Game time used for interpolation |
| `read_only` | `true` |
| `fog_of_war` | `false` |
| `coordinate_source` | `stored` |
| `neutral_entities` | `NONE` |
| `notes` | Short statement of the rules above |
| `filter.player_id` | The query filter, or null |
| `bounds` | `min_x`, `min_y`, `max_x`, `max_y` of returned points, or null when there is nothing to plot |
| `players` | `{id, name}` directory |
| `cities` | Stored position, owner, garrison army ids among returned armies, `trace_state: NONE` |
| `armies` | `status` as stored (`garrisoned`, `marching`, `returning`, `destroyed`), position, `position_state` |
| `movements` | `mission`, `status` (`in_progress`), origin, destination, current `position`, `progress`, `progress_percent`, `eta_seconds`, `direction`, `trace_id`, `event_id` |
| `limits` | Page sizes and truncation flags |

`position_state` is `garrisoned`, `interpolated`, or `UNKNOWN`.

`trace_state` is `AVAILABLE` when `trace_id` is set, `LEGACY` when an in-progress movement has a null `trace_id`, and `NONE` when there is no leg to carry a trace (garrisoned and destroyed armies, and cities, which are not commands).

`direction` is `{dx, dy}` from the stored origin to the stored destination, or null when that vector is zero.

An empty world returns empty arrays, `bounds: null`, and `neutral_entities: NONE`. It does not invent a map.

OpenAPI repeats this on `GET /v1/admin/world-map` in `/docs`.

## Admin page

`web/admin/` gains a Map tab. `map.js` and `map.css` are the tab. Shared files only gain the nav button, the section, the script tag, and the route hook.

The page shows world time, when the response was fetched, owner colors, status rings, and arrows along each in-progress leg. Attack is a solid arrow, move is dotted, return is dashed. The current point is the server position. Grid lines are a camera guide, not entities. The SVG Y axis is flipped so larger game `y` is toward the top of the screen; the detail panel still shows the server `x` and `y`.

Auto-refresh calls the same endpoint every 4 seconds. Pause stops that timer only. It does not pause the worker. Zoom, fit, and pan (including a two-finger pinch) move the camera. They do not change coordinates. There is no client-side animation between refreshes.

Tap a marker, a travel line, or a row in the list for details. Links go to the existing city, army, and movement inspectors and, when `trace_state` is `AVAILABLE`, to the Phase 4 event trace. Empty lists say `EMPTY`. Missing fields say `UNKNOWN`, except the trace states above, which are the server's own words. A destroyed army with no position is listed under "Not plotted" and is not drawn at the origin.

The tab has no command buttons.

## Deploy

No migration. No new environment variable.

On the VPS, back up `C:\simcore\app\.env.prod`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File C:\simcore\app\deploy\windows\update.ps1
```

`update.ps1` pulls `main`, installs, migrates (this change adds no revision), and restarts the services. The Map tab ships with the GitHub Pages workflow when `main` updates. It calls the API already configured in `web/config.js`.
