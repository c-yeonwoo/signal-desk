"""Company-guidance comparisons, not analyst consensus. Research only."""

from __future__ import annotations

import calendar
import datetime as dt
import math
import re
from collections import Counter, defaultdict

from signal_desk import market_clock
from signal_desk.signals.contract_event import ZONE, decide, run_path


def amount(value: str) -> tuple[float, float]:
    text = value.replace(",", "").replace(" ", "")
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:[~～](-?\d+(?:\.\d+)?))?", text)
    if not match:
        raise ValueError("financial amount ambiguous")
    lo, hi = float(match[1]), float(match[2] or match[1])
    if not math.isfinite(lo + hi) or lo > hi:
        raise ValueError("invalid financial range")
    return lo, hi


def facts(rows: list[list[str]], kind: str) -> list[dict]:
    normalized = ["".join(row).replace(" ", "") for row in rows]
    if any(word in " ".join(normalized) for word in ("관리연결", "인적분할", "물적분할", "합병전", "합병후")):
        raise ValueError("changing or management-adjusted reporting perimeter")
    if any("자회사의주요경영" in text or text.startswith("종속회사인") for text in normalized):
        raise ValueError("subsidiary scope")
    reason = next((row[-1] for row in rows if row and "정정사유" in row[0]), "")
    if any(word in reason for word in ("오기", "오류", "단위", "단순", "착오")):
        raise ValueError("clerical correction is not a fresh expectation")
    starts = [i for i, row in enumerate(rows) if row
              and re.fullmatch(r"1\.(?:연결)?(?:영업)?실적(?:전망)?내용", row[0].replace(" ", ""))
              and (len(row) == 1 or len(row) == 2 and "단위" in row[1])]
    if len(starts) != 1:
        raise ValueError("financial section ambiguous")
    start = starts[0]
    stop = next((i for i in range(start + 1, len(rows)) if normalized[i].startswith("2.")), len(rows))
    main = rows[start:stop]
    scope = "consolidated" if "연결" in normalized[start] else "separate"
    units = set()
    for index, row in enumerate(main):
        for cell in row:
            if "단위" in cell and ("구분" in cell or index == 0):
                units.update(re.findall(r"(?<![가-힣])(백만원|천원|억원|원)(?![가-힣])", cell))
                if any(word in cell for word in ("달러", "USD", "미화")):
                    raise ValueError("non-KRW financial unit")
    if len(units) != 1:
        raise ValueError("financial unit ambiguous")
    unit = units.pop()
    multiplier = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "억원": 100_000_000}[unit]

    def after(marker):
        found = []
        for row in main:
            for i, cell in enumerate(row):
                if cell.replace(" ", "") == marker:
                    found.append(row[i + 1:])
        if len(found) != 1:
            raise ValueError("financial row ambiguous: " + marker)
        return found[0]

    if kind == "guidance":
        starts, ends, revenues = after("시작일"), after("종료일"), after("매출액")
        if len(starts) != len(ends) or len(ends) != len(revenues):
            raise ValueError("guidance period columns differ")
        result = []
        for date_start, date_end, revenue in zip(starts, ends, revenues):
            if revenue in ("-", "", "해당사항없음"):
                continue
            a, b = dt.date.fromisoformat(date_start), dt.date.fromisoformat(date_end)
            if a > b or (b-a).days > 366:
                raise ValueError("guidance period invalid")
            lo, hi = amount(revenue)
            # A displayed midpoint must not overwrite an explicitly disclosed range.
            notes = [row[-1] for row in rows[stop:] if "매출" in row[-1] and ("~" in row[-1] or "～" in row[-1])]
            if notes:
                first_unit = "(?:억원|억)" if unit == "억원" else unit
                ranges = re.findall(r"매출액\s*[:：]?\s*(?:약\s*)?"
                                    r"(?:[\d,.]+\s*" + unit + r"\s*\(범위\s*:\s*)?"
                                    r"([\d,.]+)\s*(?:" + first_unit + r")?\s*[~～]\s*"
                                    r"([\d,.]+)\s*" + unit, " ".join(notes))
                if len(result) or len(ranges) != 1 or sum(v not in ("-", "") for v in revenues) != 1:
                    raise ValueError("guidance range/period attribution ambiguous")
                range_lo, range_hi = amount(ranges[0][0] + "~" + ranges[0][1])
                if not range_lo <= lo <= hi <= range_hi:
                    raise ValueError("guidance midpoint differs from notes range")
                lo, hi = range_lo, range_hi
            result.append({"scope": scope, "start": a.isoformat(), "end": b.isoformat(),
                           "revenue_low_krw": lo * multiplier, "revenue_high_krw": hi * multiplier,
                           "source_unit": unit})
        if not result:
            raise ValueError("no numeric revenue guidance")
        return result
    if kind != "earnings":
        raise ValueError("unknown financial event type")
    headers = [row for row in main if any("당기실적" in cell.replace(" ", "") for cell in row)]
    if len(headers) != 1:
        raise ValueError("current earnings column missing")
    # Use the explicit current period before the section, never the filing date
    # or the cumulative/previous-year column.
    period_rows = [row for row in rows[:start] if row and row[0].replace(" ", "") == "당기실적"]
    explicit = []
    for row in period_rows:
        dates = re.findall(r"20\d{2}-\d{2}-\d{2}", " ".join(row[1:]))
        if len(dates) == 2:
            explicit.append(tuple(dt.date.fromisoformat(value) for value in dates))
    header_at = main.index(headers[0])
    period_cells = [row[0] for row in main[header_at + 1:header_at + 2] if row]
    quarter = []
    for cell in period_cells:
        quarter.extend(re.findall(r"(20\d{2})\s*년?\s*(?:제\s*)?([1-4])\s*(?:/4\s*)?분기", cell))
        quarter.extend(re.findall(r"(20\d{2})\.([1-4])Q", cell))
    if len(explicit) == 1:
        begin, end = explicit[0]
        if begin > end or (end-begin).days > 366:
            raise ValueError("actual current period invalid")
    elif not explicit and len(quarter) == 1:
        year, q = map(int, quarter[0])
        begin = dt.date(year, (q-1)*3+1, 1)
        end_month = q*3
        end = dt.date(year, end_month, calendar.monthrange(year, end_month)[1])
    else:
        raise ValueError("explicit current quarter missing")
    if len(quarter) == 1:
        year, q = map(int, quarter[0])
        q_start, end_month = dt.date(year, (q-1)*3+1, 1), q*3
        q_end = dt.date(year, end_month, calendar.monthrange(year, end_month)[1])
        if (begin, end) != (q_start, q_end):
            raise ValueError("current period and quarter header conflict")

    def current_value(marker):
        value = after(marker)
        if not value or value[0].replace(" ", "") != "당해실적":
            raise ValueError("standalone quarter row not explicit")
        return None if value[1] in ("-", "") else amount(value[1])

    revenue = current_value("매출액")
    operating = current_value("영업이익")
    if not revenue or revenue[0] != revenue[1] or operating and operating[0] != operating[1]:
        raise ValueError("actual earnings cannot be a forecast range")
    return [{"scope": scope, "start": begin.isoformat(), "end": end.isoformat(),
             "revenue_low_krw": revenue[0]*multiplier, "revenue_high_krw": revenue[1]*multiplier,
             "operating_profit_krw": operating[0]*multiplier if operating else None, "source_unit": unit}]


def trading_clock(event: dict) -> dict:
    published = dt.datetime.fromisoformat(event["published_at"])
    if published.utcoffset() is None:
        raise ValueError("publication timezone missing")
    for offset in range(11):
        day = published.astimezone(ZONE).date() + dt.timedelta(days=offset)
        probe = dt.datetime.combine(day, dt.time(12), ZONE)
        window = market_clock.regular_window("kr", probe)
        if not window:
            continue
        start, end = (stamp.astimezone(ZONE) for stamp in window)
        cutoff = min(dt.datetime.combine(day, dt.time(13, 30), ZONE), end-dt.timedelta(minutes=90))
        if offset == 0 and published > cutoff:
            continue
        anchor = max(published, start + dt.timedelta(minutes=8))
        if day.isoformat() > "2026-07-31":
            raise ValueError("execution beyond frozen period")
        return {"execution_session": day.isoformat(), "trade_anchor_at": anchor.isoformat(),
                "execution_timing": "same_session" if day == published.astimezone(ZONE).date() and anchor == published
                else "next_regular_opening_window"}
    raise ValueError("regular execution session unavailable")


def execution_event(event: dict) -> dict:
    return {**event, "official_published_at": event["published_at"], "official_session": event["session"],
            "published_at": event["trade_anchor_at"], "session": event["execution_session"]}


def select_events(ledger: list[dict], parsed: dict[str, list[dict]], session_check) -> tuple[list[dict], list[dict]]:
    history, selected, audits = defaultdict(list), [], []
    days, months = set(), Counter()
    for event in sorted(ledger, key=lambda e: (e["published_at"], e["receipt"])):
        records = parsed.get(event["receipt"], [])
        if event.get("source_group") == "baseline_guidance":
            history[event["ticker"]].append({**event, "facts": records})
            audits.append({"receipt": event["receipt"], "ticker": event["ticker"], "kind": event["kind"],
                           "published_at": event["published_at"], "reason": "baseline_only_not_an_event",
                           "parsed_periods": len(records), "upgraded_periods": 0})
            continue
        candidates, reasons = [], []
        for current in records:
            if event["kind"] == "earnings" and current["end"] > event["session"]:
                reasons.append("actual_period_has_not_closed")
                continue
            key = (current["scope"], current["start"], current["end"])
            earlier = history[event["ticker"]]
            matching = [(old, point) for old in earlier for point in old["facts"]
                        if (point["scope"], point["start"], point["end"]) == key]
            if not matching:
                reasons.append("no_same_period_prior_company_guidance")
                continue
            previous, basis = max(matching, key=lambda pair: (pair[0]["published_at"], pair[0]["receipt"]))
            if previous["session"] >= event["session"]:
                reasons.append("latest_guidance_not_at_least_one_day_old")
                continue
            if any(not old["facts"] and old["published_at"] >= previous["published_at"] for old in earlier):
                reasons.append("ambiguous_intervening_guidance")
                continue
            if any(old["receipt"] != previous["receipt"] and old["published_at"] == previous["published_at"]
                   for old in earlier):
                reasons.append("same_minute_guidance_order_ambiguous")
                continue
            if basis["revenue_high_krw"] <= 0 or current["revenue_low_krw"] <= 0:
                reasons.append("nonpositive_revenue_basis")
                continue
            improvement = (current["revenue_low_krw"] / basis["revenue_high_krw"] - 1) * 100
            if improvement < 5 - 1e-10:
                reasons.append("below_fixed_five_percent_upgrade")
                continue
            if event["kind"] == "earnings" and (current["operating_profit_krw"] is None or current["operating_profit_krw"] <= 0):
                reasons.append("actual_operating_profit_not_positive")
                continue
            candidates.append({**event, "financials": current, "prior_guidance": basis,
                               "prior_receipt": previous["receipt"],
                               "prior_published_at": previous["published_at"],
                               "prior_source_sha256": previous.get("source_sha256"),
                               "improvement_pct": improvement, "expectation_basis": "issuer_prior_guidance"})
        clock, clock_error = None, None
        if len(candidates) == 1:
            try:
                clock = trading_clock(event)
            except ValueError as exc:
                clock_error = str(exc)
        decision = (dt.datetime.fromisoformat(clock["trade_anchor_at"]) + dt.timedelta(minutes=2)) if clock else None
        superseded = bool(clock and any(other["ticker"] == event["ticker"]
                                       and event["published_at"] < other["published_at"] <= decision.isoformat()
                                       for other in ledger))
        if len(candidates) > 1:
            reason = "multiple_upgraded_periods_ambiguous"
        elif not candidates:
            reason = ";".join(sorted(set(reasons))) or event.get("parse_error") or event["status"]
        elif clock_error:
            reason = clock_error
        elif superseded:
            reason = "newer_financial_publication_before_decision"
        elif not session_check("kr", clock["execution_session"]):
            reason = "execution_session_unverified"
        elif clock["execution_session"] in days:
            reason = "later_same_day_event"
        elif months[clock["execution_session"][:7]] >= 8:
            reason = "monthly_sample_cap"
        else:
            selected.append({**candidates[0], **clock})
            days.add(clock["execution_session"])
            months[clock["execution_session"][:7]] += 1
            reason = "selected_before_prices"
        audits.append({"receipt": event["receipt"], "ticker": event["ticker"], "kind": event["kind"],
                       "published_at": event["published_at"], "reason": reason,
                       "parsed_periods": len(records), "upgraded_periods": len(candidates)})
        if event["kind"] == "guidance":
            history[event["ticker"]].append({**event, "facts": records})
    return sorted(selected, key=lambda event: event["execution_session"]), audits


def replay(records: list[dict], policy: str, **kwargs) -> dict:
    if policy == "fundamental":
        return run_path(records, "immediate", **kwargs)
    if policy == "reaction":
        return run_path(records, "confirmed", **kwargs)
    if policy == "cash":
        return run_path(records, "cash", **kwargs)
    if policy != "market":
        raise ValueError("unknown earnings policy")
    controls = []
    for record in records:
        valid = not record.get("source_error") and decide(record["event"], record["stock"], immediate=True)["state"] == "planned"
        controls.append({**record, "stock": record.get("control", {}) if valid else {}})
    return run_path(controls, "immediate", **kwargs)
