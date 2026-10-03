"""SEC EDGAR 13F 수집 — 미국 기관투자자의 분기 보유내역(공개 공시) 파싱.

13F-HR은 운용자산 $100M 이상 기관이 분기마다 롱 포지션(미국 상장분)을 공시하는 서식이다.
키·인증 불필요(공개 데이터). 자동 접근에는 실제 SEC_CONTACT_EMAIL이 필요하다.

한계(반드시 UI에 명시): 분기 스냅샷 + 공시까지 최대 45일 지연, 롱·미국상장분만(현금·채권·
숏·해외주식 제외). '지금 이 순간의 포지션'이 아니라 '직전 분기말 공시 스냅샷'이다.

표준 라이브러리(urllib + xml)만 사용. 실패 시 None/빈 값(그레이스풀 폴백).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

log = logging.getLogger("signal_desk.ingest.edgar")

_TIMEOUT = 20
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_REQUEST_INTERVAL = 0.2  # 단일 프로세스에서 공식 최대 속도보다 충분히 낮게 유지
_request_lock = threading.Lock()
_last_request_at = 0.0


def _contact() -> str | None:
    """No placeholder identity: SEC_CONTACT_EMAIL must be configured by the operator."""
    value = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    domain = value.rsplit("@", 1)[-1].lower()
    if (not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)
            or domain in {"example.com", "example.org", "example.net"}
            or domain.endswith((".local", ".invalid", ".example", ".test"))):
        return None
    return value


def available() -> bool:
    return _contact() is not None


def _get(url: str) -> bytes | None:
    contact = _contact()
    parsed = urllib.parse.urlsplit(url)
    if not contact or parsed.scheme != "https" or parsed.netloc not in {"www.sec.gov", "data.sec.gov"}:
        return None

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    req = urllib.request.Request(url, headers={
        "User-Agent": f"signal-desk/0.1 ({contact})", "Accept-Encoding": "identity"})
    try:
        # All legacy EDGAR paths share this one limiter. No SEC request is made
        # when the configured contact is absent or invalid.
        global _last_request_at
        with _request_lock:
            delay = _REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
            if delay > 0:
                time.sleep(delay)
            _last_request_at = time.monotonic()
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=_TIMEOUT) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("EDGAR response too large")
        return raw
    except Exception as e:
        log.warning("EDGAR 요청 실패: %s", type(e).__name__)
        return None


_cik_map: dict[str, str] | None = None


def _ticker_cik_map() -> dict[str, str]:
    """미국 티커 → CIK(10자리) 맵(SEC 공식). 1회 로드 후 캐시. 실패 시 빈 dict."""
    global _cik_map
    if _cik_map is not None:
        return _cik_map
    if not available():
        return {}
    _cik_map = {}
    raw = _get("https://www.sec.gov/files/company_tickers.json")
    if raw:
        try:
            for row in json.loads(raw).values():
                _cik_map[str(row["ticker"]).upper()] = f"{int(row['cik_str']):010d}"
        except Exception as e:
            log.warning("EDGAR ticker→CIK 맵 파싱 실패: %s", type(e).__name__)
    return _cik_map


def _latest_annual(facts: dict, keys: list[str], unit: str = "USD") -> float | None:
    """us-gaap 컨셉(keys 우선순위) 중 최신 '연간(FY, 10-K/20-F)' 값. unit=단위(USD, 'USD/shares' 등)."""
    usgaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for k in keys:
        units = ((usgaap.get(k) or {}).get("units") or {}).get(unit) or []
        annuals = [u for u in units if u.get("fp") == "FY" and u.get("form") in ("10-K", "20-F")] or units
        if annuals:
            best = max(annuals, key=lambda u: (u.get("fy") or 0, u.get("end") or ""))
            if best.get("val") is not None:
                return float(best["val"])
    return None


def _dividend_months(facts: dict) -> list[int]:
    """최근 ~13개월 '기간형(분기/월) 배당' 공시의 종료월(1~12)을 추정 지급월 패턴으로 반환.
    연간(FY, 기간>100일) 항목은 제외 → 분기배당은 4개월, 월배당 ETF는 최대 12개월이 잡힌다.
    EDGAR엔 정확한 지급일이 약해 '공시 기간 종료월' 근사(캘린더에 추정 표기)."""
    import datetime as _dt
    usgaap = (facts.get("facts") or {}).get("us-gaap") or {}
    units = (((usgaap.get("CommonStockDividendsPerShareDeclared") or {}).get("units") or {}).get("USD/shares")
             or ((usgaap.get("CommonStockDividendsPerShareCashPaid") or {}).get("units") or {}).get("USD/shares") or [])
    ends: list[_dt.date] = []
    for u in units:
        end, start = u.get("end"), u.get("start")
        if not end:
            continue
        try:
            e = _dt.date.fromisoformat(end)
            if start and (e - _dt.date.fromisoformat(start)).days > 100:  # 연간 항목 제외
                continue
        except (ValueError, TypeError):
            continue
        ends.append(e)
    if not ends:
        return []
    latest = max(ends)
    return sorted({e.month for e in ends if (latest - e).days <= 400})


def fundamentals(ticker: str) -> dict | None:
    """티커의 최신 연간 순이익·자기자본·주당배당·배당지급월(EDGAR XBRL companyfacts, DART의 미국판).
    없으면 None. PER=시총/순이익, PBR=시총/자기자본, 배당수익률=주당배당/현재가(시총·현재가는 별도)."""
    cik = _ticker_cik_map().get(ticker.upper())
    if not cik:
        return None
    raw = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    if not raw:
        return None
    try:
        facts = json.loads(raw)
    except Exception:
        return None
    ni = _latest_annual(facts, ["NetIncomeLoss", "ProfitLoss"])
    eq = _latest_annual(facts, ["StockholdersEquity",
                                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"])
    dps = _latest_annual(facts, ["CommonStockDividendsPerShareDeclared",
                                 "CommonStockDividendsPerShareCashPaid"], unit="USD/shares")
    if ni is None and eq is None and dps is None:
        return None
    return {"net_income": ni, "equity": eq, "dps": dps,
            "div_months": _dividend_months(facts) if dps else []}


def _latest_13f(cik: str) -> tuple[str, str] | None:
    """CIK의 최신 13F-HR 공시 (accession, 보고분기말일) 반환. 없으면 None."""
    raw = _get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
    if not raw:
        return None
    try:
        rec = json.loads(raw)["filings"]["recent"]
    except Exception:
        return None
    for form, acc, period in zip(rec.get("form", []), rec.get("accessionNumber", []),
                                 rec.get("reportDate", [])):
        if form == "13F-HR":  # 원본만(정정본 13F-HR/A 제외 — 최신 원본 우선)
            return acc, period
    return None


def _parse_info_table(xml_bytes: bytes) -> list[dict]:
    """13F info table XML → [{name, value_usd}] (issuer별 합산 전 원자료)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    ns = {"n": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    tag = "n:infoTable" if ns else "infoTable"
    out = []
    for it in root.findall(tag, ns):
        def txt(name):
            return it.findtext(f"n:{name}" if ns else name, "", ns)
        name = (txt("nameOfIssuer") or "").strip()
        val = txt("value")
        if not name or not val:
            continue
        try:
            out.append({"name": name, "value_usd": float(val)})
        except ValueError:
            continue
    return out


def holdings_13f(cik: str, top: int = 10) -> dict | None:
    """CIK의 최신 13F 보유내역 요약. 반환:
    {period, total_usd, n_holdings, holdings:[{name, value_usd, pct}]}(비중순 top개). 실패 시 None.

    SEC 13F 금액 단위는 공시 시점에 따라 천달러/달러가 섞여 왔었다(2023 이후는 달러) — 절대액보다
    '비중(pct)'을 주 지표로 쓰므로 단위 차이는 표시에만 영향(총액 표기는 근사)."""
    latest = _latest_13f(cik)
    if not latest:
        return None
    acc, period = latest
    acc_nodash = acc.replace("-", "")
    idx = _get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/index.json")
    if not idx:
        return None
    try:
        files = [it["name"] for it in json.loads(idx)["directory"]["item"] if it["name"].endswith(".xml")]
    except Exception:
        return None

    rows = []
    for fname in sorted(files, key=lambda f: f == "primary_doc.xml"):  # info table(비primary) 먼저 시도
        xml_bytes = _get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{fname}")
        if xml_bytes:
            rows = _parse_info_table(xml_bytes)
            if rows:
                break
    if not rows:
        return None

    agg: dict[str, float] = defaultdict(float)
    for r in rows:
        agg[r["name"]] += r["value_usd"]
    total = sum(agg.values())
    ranked = sorted(agg.items(), key=lambda x: -x[1])
    holdings = [{"name": n, "value_usd": v, "pct": round(v / total * 100, 1) if total else 0.0}
                for n, v in ranked[:top]]
    return {"period": period, "total_usd": total, "n_holdings": len(agg), "holdings": holdings}
