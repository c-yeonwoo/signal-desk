"""DART 공시 API — 재무제표 원시 항목에서 ROE/부채비율/매출성장을 계산.

DART_API_KEY가 없으면 모든 함수가 조용히 None/빈 값을 반환한다(그레이스풀 폴백 — engine이
재무데이터 없음으로 처리). apt-signal 컨벤션대로 표준 라이브러리(urllib)만 사용, 추가 HTTP
의존성 없음.

PER/PBR은 DART 재무제표만으로는 계산 불가(시가·발행주식수 필요, KRX 시세와 결합 필요) —
이번 1차 구현은 ROE·부채비율·매출성장만 채운다. PER/PBR은 KRX 종가 연동 후 후속 작업.

실제 DART_API_KEY로 검증 완료(2026-07-02). 계정명이 회사마다 갈릴 수 있어(예: "당기순이익" vs
"당기순이익(손실)") `_find()`가 접두 일치로 폴백한다 — 실제 응답에서 확인된 케이스.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.dart")

BASE = "https://opendart.fss.or.kr/api"
_TIMEOUT = 30


def _get_json(path: str, params: dict) -> dict | None:
    key = config.dart_key()
    if not key:
        return None
    qs = urllib.parse.urlencode({**params, "crtfc_key": key})
    try:
        with urllib.request.urlopen(f"{BASE}/{path}?{qs}", timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.error("DART 요청 실패(%s): %s", path, e)
        return None
    if body.get("status") != "000":
        log.warning("DART 응답 오류(%s): %s %s", path, body.get("status"), body.get("message"))
        return None
    return body


def corp_codes() -> dict[str, str]:
    """stock_code(6자리) -> corp_code(8자리) 매핑. corpCode.xml(zip) 1회 다운로드."""
    key = config.dart_key()
    if not key:
        return {}
    url = f"{BASE}/corpCode.xml?{urllib.parse.urlencode({'crtfc_key': key})}"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
            raw = resp.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        log.error("DART corpCode 다운로드 실패: %s", e)
        return {}

    mapping: dict[str, str] = {}
    for item in root.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code:
            mapping[stock_code] = corp_code
    return mapping


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _find(by_name: dict[str, dict], prefix: str) -> dict | None:
    """정확히 일치하는 계정명이 없으면 접두 일치로 폴백 — DART는 회사마다 '당기순이익' /
    '당기순이익(손실)' 등으로 계정명 표기가 갈린다(실제 응답으로 확인됨)."""
    if prefix in by_name:
        return by_name[prefix]
    for name, item in by_name.items():
        if name.startswith(prefix):
            return item
    return None


def _derive_metrics(items: list[dict]) -> dict:
    """DART fnlttSinglAcnt 응답의 list 항목에서 ROE/부채비율/매출성장(+PER/PBR 계산용 원자재인
    net_income/equity)을 계산 (순수함수, 테스트용 분리).

    응답에 연결재무제표(CFS)와 별도재무제표(OFS)가 같은 계정명으로 섞여 온다(실응답 fs_div
    필드로 확인, 삼성전자 기준 자본총계 CFS 436.3조 vs OFS 254.3조로 값 자체가 다름) — 투자자가
    보는 헤드라인 수치는 보통 연결 기준이라 CFS를 우선하고, 없으면 OFS로 폴백한다."""
    by_name: dict[str, dict] = {}
    for fs_div in ("OFS", "CFS"):  # CFS를 나중에 덮어써서 최종적으로 CFS 우선
        for it in items:
            if it.get("fs_div") == fs_div and it.get("account_nm"):
                by_name[it["account_nm"]] = it
    if not by_name:  # fs_div 필드가 없는 응답 형태 대비 폴백(최초 등장 값 사용)
        for it in items:
            name = it.get("account_nm")
            if name and name not in by_name:
                by_name[name] = it

    equity = _to_float((_find(by_name, "자본총계") or {}).get("thstrm_amount"))
    liabilities = _to_float((_find(by_name, "부채총계") or {}).get("thstrm_amount"))
    net_income = _to_float((_find(by_name, "당기순이익") or {}).get("thstrm_amount"))
    revenue_cur = _to_float((_find(by_name, "매출액") or {}).get("thstrm_amount"))
    revenue_prev = _to_float((_find(by_name, "매출액") or {}).get("frmtrm_amount"))

    metrics: dict = {}
    if net_income is not None and equity:
        metrics["roe"] = round(net_income / equity * 100, 2)
    if liabilities is not None and equity:
        metrics["debt_ratio"] = round(liabilities / equity * 100, 2)
    if revenue_cur is not None and revenue_prev:
        metrics["revenue_growth"] = round((revenue_cur - revenue_prev) / revenue_prev * 100, 2)
    # PER/PBR은 시가총액이 필요해 여기선 계산 못 함 — store.py가 KRX 시가총액과 결합해 채운다.
    if net_income is not None:
        metrics["net_income"] = net_income
    if equity is not None:
        metrics["equity"] = equity
    return metrics


def fundamentals(stock_code: str, corp_code: str, bsns_year: str) -> dict | None:
    """종목의 연간 재무 스코어 원료(ROE/부채비율/매출성장). 키 없거나 조회 실패 시 None."""
    body = _get_json("fnlttSinglAcnt.json", {
        "corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": "11011",  # 사업보고서(연간)
    })
    if not body:
        return None
    return _derive_metrics(body.get("list", []))
