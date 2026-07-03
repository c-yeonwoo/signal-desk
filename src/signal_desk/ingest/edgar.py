"""SEC EDGAR 13F 수집 — 미국 기관투자자의 분기 보유내역(공개 공시) 파싱.

13F-HR은 운용자산 $100M 이상 기관이 분기마다 롱 포지션(미국 상장분)을 공시하는 서식이다.
키·인증 불필요(공개 데이터). SEC는 식별 가능한 User-Agent를 요구한다.

한계(반드시 UI에 명시): 분기 스냅샷 + 공시까지 최대 45일 지연, 롱·미국상장분만(현금·채권·
숏·해외주식 제외). '지금 이 순간의 포지션'이 아니라 '직전 분기말 공시 스냅샷'이다.

표준 라이브러리(urllib + xml)만 사용. 실패 시 None/빈 값(그레이스풀 폴백).
"""

from __future__ import annotations

import json
import logging
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

log = logging.getLogger("signal_desk.ingest.edgar")

_UA = "signal-desk/0.1 (research contact: admin@signal-desk.local)"  # SEC는 식별 UA 요구
_TIMEOUT = 20


def _get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return raw
    except Exception as e:
        log.warning("EDGAR 요청 실패(%s): %s", url.split("/")[-1], type(e).__name__)
        return None


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
