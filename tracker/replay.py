from datetime import timedelta
from decimal import Decimal
from heapq import heappop, heappush

from .db import now
from .operations import duplicate_groups, reference
from .store import events, utc
from .value import assess, dec, liquidity, rounded


def replay(session, mode, start, end, capital, strategy, latency_minutes, assume_available, assume_days):
    stream = [row for row in events(session, mode, "offer", cutoff=end)]
    stream.sort(key=lambda item: (utc(item.known_at), item.id))
    pending, timeline, funds, holdings = [], [], capital, {}
    seen, attempted_units = {}, set()
    minimum = Decimal(0)
    missed = realized = serial = 0
    committed = Decimal(0)

    def add(moment, kind, value):
        nonlocal serial
        serial += 1
        heappush(pending, (moment, serial, kind, value))

    def point(moment):
        timeline.append({"at": moment.isoformat(), "cash": rounded(funds),
                         "locked": rounded(sum((item["purchase"] for item in holdings.values()), Decimal(0)))})

    def process(moment, inclusive=True):
        nonlocal funds, missed, realized, committed
        while pending and (pending[0][0] <= moment if inclusive else pending[0][0] < moment):
            timestamp, _, kind, value = heappop(pending)
            if kind == "sale":
                item = holdings.pop(value)
                funds += item["net"]
                realized += item["net"] - item["purchase"]
                point(timestamp)
                continue
            row, best, delay = value
            key = reference(row)
            latest = seen.get(key)
            checked = latest is not None and utc(latest.known_at) >= timestamp
            invalid = latest is None or latest.data["status"] != "available" or (
                latest.data.get("quantity") == 0 or latest.data["price"] != row.data["price"] or
                latest.data["shipping"] != row.data["shipping"] or latest.data["currency"] != row.data["currency"]
            )
            groups = duplicate_groups(session, mode, timestamp)
            group = groups.get(key, key)
            purchase = dec(best["margin"]["purchase"])
            already_used = any(groups.get(unit, unit) == group for unit in attempted_units)
            if invalid or (not checked and not assume_available) or purchase > funds or already_used:
                missed += 1
                continue
            attempted_units.add(key)
            funds -= purchase
            committed += purchase
            holdings[group] = {"title": row.data["title"], "purchase": purchase,
                               "net": dec(best["margin"]["net"]), "availability": "checked" if checked else "assumed"}
            if delay is not None:
                add(timestamp + timedelta(days=delay), "sale", group)
            point(timestamp)

    point(start)
    for row in stream:
        clock = utc(row.known_at)
        process(clock, inclusive=False)
        seen[reference(row)] = row
        process(clock)
        if clock < start or utc(row.effective_at) > clock or row.data["status"] != "available":
            continue
        value = assess(session, mode, row, clock)
        best = next(iter(value["estimates"]), None)
        if not best or not best["margin"] or dec(best["margin"]["profit"]) <= minimum:
            continue
        delay = assume_days
        if strategy == "liquidity":
            probability = liquidity(session, mode, value["matching"]["product_id"], best["channel"],
                                    dec(best["margin"]["gross"]), clock, row.data["condition"], row.data["market"])["probability"]
            if probability is None or dec(probability) < Decimal(".5"):
                missed += 1
                continue
        add(clock + timedelta(minutes=latency_minutes), "buy", (row, best, delay))
    process(end)
    point(end)
    return {"strategy": strategy, "pnl": rounded(Decimal(realized)), "cash": rounded(funds), "missed": missed,
            "committed": rounded(committed), "held": [{"title": item["title"], "purchase": rounded(item["purchase"]),
                                                      "availability": item["availability"]} for item in holdings.values()],
            "curve": timeline, "assumptions": {"latency_minutes": latency_minutes,
                                                "assume_available": assume_available, "assume_sale_days": assume_days}}


def default_window():
    finish = now()
    return finish - timedelta(days=60), finish
