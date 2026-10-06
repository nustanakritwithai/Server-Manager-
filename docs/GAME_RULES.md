# Game rules (MVP)

These are the rules the server actually applies. Numbers live in `src/simcore/game/catalog.py`, `travel.py`, `combat.py`, and `economy.py`. If this document and the code disagree, the code is what the worker runs; update this file in the same change.

The map is a flat plane of integer city coordinates. There is no fog of war, no alliances, no city capture, and no market. A battle loots resources and kills units. The defender keeps the city.

## Time

The simulation clock is wall time plus `world_state.offset_seconds`. Admin tools and tests add to that offset; they never rewrite stored timestamps. Every command and every event is stamped with this clock.

The client should call `GET /v1/time`, compute `offset = server_time - local_now`, and count down with `arrive_at - (local_now + offset)`. The countdown is cosmetic. The battle, the loot, and the march home exist only after the worker has processed the matching event.

## Cities

A city has an owner, a name, a position `(x, y)`, four resource stockpiles, an hourly production rate for each, a building map, and `last_updated`.

Resources are integers: **wood**, **food**, **iron**, **gold**. Stockpiles never go below zero.

The garrison is every army with `location_city_id` equal to the city and status `garrisoned`. There is no separate garrison table.

Buildings are a JSON map of name to level. The MVP catalog is `lumber_camp`, `farm`, `iron_mine`, `warehouse`, and `barracks`. Issuing BUILD queues a 30 minute `BUILD_COMPLETE` event. On completion the level increases by one. Builds do not cost resources yet.

## Armies and units

An army has an owner, a home city, a status, and stacks of units (`type` + `count`).

| Status | Meaning |
| --- | --- |
| `garrisoned` | Standing in `location_city_id` |
| `marching` | On a move or an attack |
| `returning` | Walking back to its home city |
| `destroyed` | No units left. It does not travel and holds no ground |

| Unit | ATK | DEF | HP | Speed (tiles/hour) | Carry | Food upkeep / hour |
| --- | --- | --- | --- | --- | --- | --- |
| militia | 4 | 3 | 20 | 7 | 25 | 1 |
| infantry | 10 | 8 | 40 | 6 | 30 | 2 |
| archer | 12 | 4 | 25 | 7 | 20 | 2 |
| cavalry | 16 | 6 | 50 | 12 | 45 | 4 |

Upkeep is charged only while the army is garrisoned. Armies on the road eat nothing. This MVP does not desert units when food hits zero; food simply stops at zero.

## Travel

```
distance = hypot(x2 - x1, y2 - y1)
speed    = minimum speed among unit types that still have a count > 0
seconds  = ceil(distance / speed * 3600)
```

Speed is tiles per hour. A mixed army is limited by its slowest unit. Results that land within `1e-6` of an integer (binary floating point) are snapped to that integer; every other value is rounded up, so an army never arrives early. Travel of zero distance is rejected. The minimum positive trip is 1 second.

Recall from the exact tile the army is already on does not start a new trip: the march is cancelled and the army is garrisoned at home immediately (`depart_at == arrive_at`).

## Commands

The client sends intent. The server checks ownership and state, then inserts the `Movement` and the event. The client never supplies `depart_at` or `arrive_at`.

| Command | Who may issue it | What it schedules |
| --- | --- | --- |
| `MOVE_ARMY` | Your garrisoned army, to another city you own | `move` leg, event `ARMY_ARRIVE` |
| `ATTACK_CITY` | Your garrisoned army, to a city you do not own | `attack` leg, event `ARMY_ARRIVE` |
| `RECALL_ARMY` | Your army that is marching, or garrisoned away from home | Cancels the outbound event if it is still pending, then a `return` leg and `ARMY_RETURN` |
| `BUILD` | Your city, known building name | `BUILD_COMPLETE` in 30 minutes |
| `RESEARCH` | You, known tech name | `RESEARCH_COMPLETE` in 60 minutes |

`MOVE_ARMY` reinforces the destination: on arrival the army's location changes and its home city stays, unless `relocate` is true, in which case the home city changes too.

`RECALL_ARMY` while the army is already walking home is rejected. Recall interpolates the current point on the segment and marches from there back to the home city at the army's current speed. An arrival the worker has already started processing cannot be cancelled.

Research names: `forestry`, `husbandry`, `metallurgy`, `logistics`. Completion increments that tech's level on the player. Research does not cost resources yet.

An army can have only one `in_progress` movement. A second order is a conflict.

## Arrival

**Move.** Production on the destination is accrued first (the new army was not there during that window). The army becomes garrisoned there.

**Attack.**

1. Accrue the target city's production and upkeep up to the event's `due_at`, while the defenders are still standing.
2. Fight `resolve_battle` with every garrisoned army merged together. Losses come off those armies in ascending army id, lowest-DEF unit types first inside the battle itself.
3. Write one `BattleReport`, including the seed.
4. Remove the looted resources from the city immediately (ledger reason `loot_lost`).
5. If the attacker still has units, schedule a return leg to the home city carrying that loot. If the attacker was wiped out, the army is `destroyed` and the loot stays in the city (it was never taken, because a wipe is not an attacker victory).

**Return.** Accrue the home city (the army is not garrisoned yet, so it adds no upkeep for the trip), credit carried loot (`loot_gained`), then garrison the army at home.

A late worker still resolves the event **as of `due_at`**, not as of the moment it woke up. The march home therefore departs at the original ETA. If that return is already due, the same tick processes it.

## Combat

`resolve_battle(attacker, defender, seed)` is pure. Same stacks, same defender resources, same seed: same result, including the round log. The RNG is a 32-bit numerical-recipes LCG, not Python's `random` module, so a replay does not depend on the interpreter's RNG version.

Each round, both sides strike at once, for at most 8 rounds:

```
variance_bp = 9000 + LCG.randbelow(2000)     # 0.9000x .. 1.0999x
damage      = atk * variance_bp // (100 * (100 + opponent_defense))
```

`atk` and `defense` are the sums of `count * stat` over living stacks. Damage removes whole units, lowest DEF, then lowest ATK, then unit name. HP that is not enough to remove another unit is discarded. There are no wounded.

Winner:

- Only the attacker has units left: attacker.
- Only the defender has units left: defender.
- Neither does: draw.
- Both do: higher remaining HP. Equal HP is a draw.

Loot is carried home only when the winner is the attacker and the attacker has survivors.

```
carry remaining starts at sum(survivors * carry)
for resource in (gold, iron, wood, food):
    take = min(stock * 30 / 100, carry remaining)
    carry remaining -= take
```

All of that division is integer division. The report stores the seed, both sides before and after, casualties, the defender stockpile used for loot, and the loot itself. Replaying `resolve_battle` with those inputs reproduces the report.

An empty garrison is an immediate attacker victory with no rounds and no attacker casualties. Loot still respects carry and the 30% cap.

## Resources

Production is lazy. Whenever a city is read or a garrison is about to change, the server applies the whole gap since `last_updated`:

```
produced = rate_per_hour * elapsed_seconds // 3600
```

Food then pays garrison upkeep with the same formula. Each non-zero delta is one row in `transactions`, keyed by `accrue:{city}:{resource}:{previous last_updated}:production` (and `:upkeep` for food). The marker then moves to `now`. The fractional resource inside the current hour is dropped. Splitting one long gap into many short updates would drop more, so callers accrue the full gap in one shot.

Loot leaves the defender at battle time and enters the attacker's home city when the army arrives home. Both sides are ledger rows tied to the source event:

- `event:{id}:loot_lost:{resource}`
- `event:{id}:loot_gained:{resource}`

The unique key is the idempotency guarantee. Processing an event is also one database transaction: a crash before commit rolls the ledger, the report, and the event status back together. A later retry inserts the same keys, finds them, and does not grant the resources again. Reprocessing an arrival that already has a `BattleReport` does not fight a second time.

Build and research record their own ledger rows (`building:…`, `research:…`) so a replay does not increment the level twice.

## What is deliberately not here

City capture, wall HP, wounded troops, morale, supply lines on the march, unit desertion, build costs, training timers beyond the stub events, fog of war, alliances, and a market. See the README for the server features left for later (real auth, backups, monitoring, a full dead-letter queue, rate limits, horizontal scaling).
