"""Deterministic, self-contained market landscape from dated close observations."""

from __future__ import annotations

import datetime as dt
import math
from html import escape
from zoneinfo import ZoneInfo

WIDTH = 1200
HEIGHT = 700
FONT = 'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif"'


def _e(value: object) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _kst_time(value: object) -> str | None:
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(ZoneInfo("Asia/Seoul")).strftime("%m-%d %H:%M KST")
    except (TypeError, ValueError):
        return None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def render(card: dict) -> str:
    """Render observed direction as atmosphere, with no prediction or model call."""
    market = "해외" if card.get("market") == "us" else "국내"
    fresh = card.get("status") in {"ready", "partial"}
    scene = card.get("scene") or {}
    compared, universe = _count(scene.get("compared")), _count(scene.get("universe"))
    up, down, flat = (_count(scene.get(key)) for key in ("up", "down", "flat"))
    direction = scene.get("direction") if fresh else "unknown"
    if (direction not in {"up", "down", "mixed"} or compared == 0
            or compared != up + down + flat or universe < compared):
        direction = "unknown"
    title = (str(card.get("today_headline") or card.get("headline") or "오늘 시장을 아직 요약할 수 없어요")
             if direction != "unknown" else "오늘 시장 방향은 확인 중이에요")
    sky, horizon, light, accent = {
        "up": ("#103B46", "#286F72", "#FFCF79", "#74DBC0"),
        "down": ("#27243E", "#755577", "#D9A6AE", "#F0A9A1"),
        "mixed": ("#193650", "#537E99", "#F4C88E", "#B0C8F6"),
        "unknown": ("#283543", "#687783", "#BAC5C9", "#C8D4D7"),
    }[direction]
    as_of = str(card.get("price_as_of") or "미확인")
    date_label = f"{as_of} 종가" if fresh else "종가 확인 전"
    selection = card.get("selection") if fresh and card.get("status") != "partial" else None
    selection_time = _kst_time(selection.get("computed_at")) if isinstance(selection, dict) else None
    selection_text = (f"매수 판정 {_count(selection.get('buy_count'))}개 · "
                      f"강력매수 {_count(selection.get('strong_buy_count'))}개 · {selection_time}"
                      if selection_time and direction != "unknown" else
                      "매수 판정 건수 보류 · 가격 또는 판정 시각 확인 전")
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc" data-scene="{direction}">',
        f'<title id="title">{_e(market)} 시장 한 장 요약: {_e(title)}</title>',
        f'<desc id="desc">{_e(date_label)}로 확인한 관찰 종목의 장면입니다. '
        f'{_e(card.get("summary"))} 다음 가격 예측이 아닙니다.</desc>',
        '<defs>',
        f'<linearGradient id="sky" x2="0" y2="1"><stop stop-color="{sky}"/>'
        f'<stop offset="1" stop-color="{horizon}"/></linearGradient>',
        '<linearGradient id="ground" x2="0" y2="1"><stop stop-color="#183547"/>'
        '<stop offset="1" stop-color="#0B1D2C"/></linearGradient>',
        '<clipPath id="frame"><rect width="1200" height="700" rx="26"/></clipPath>',
        '</defs><g clip-path="url(#frame)">',
        '<rect width="1200" height="700" fill="url(#sky)"/>',
        '<path d="M0 345 Q140 310 300 350 T610 338 T930 355 T1200 330 V700 H0Z" '
        'fill="#1C4350" opacity=".42"/>',
        f'<circle cx="952" cy="235" r="88" fill="{light}" opacity=".14"/>',
        f'<circle cx="952" cy="235" r="51" fill="{light}" opacity=".84"/>',
        '<path d="M0 468H125V377H188V468H232V325H298V468H345V394H410V468H460V347H537V468'
        'H587V407H653V468H716V361H787V468H836V398H898V468H941V337H1003V468H1049V380H1122'
        'V468H1200V700H0Z" fill="#173B4D" opacity=".75"/>',
        '<path d="M0 510 Q250 472 462 505 T900 493 T1200 508 V700H0Z" fill="url(#ground)"/>',
        '<path d="M0 545 Q260 515 505 550 T990 540 T1200 548" fill="none" stroke="#78A4A0" '
        'stroke-width="3" opacity=".28"/>',
    ]
    if direction == "up":
        svg.extend([
            '<path d="M95 415 Q260 385 420 405 M625 330 Q740 290 830 316" fill="none" '
            'stroke="#9BE2D1" stroke-width="7" opacity=".55" stroke-linecap="round"/>',
            '<path d="M71 430 Q86 370 105 430 M112 438 Q136 355 158 438 M1021 448 Q1042 375 1065 448" '
            'fill="none" stroke="#92D8A8" stroke-width="8" stroke-linecap="round"/>',
        ])
    elif direction == "down":
        svg.extend([
            '<path d="M0 272 Q112 216 226 272 Q338 213 454 270 Q588 207 714 273 Q859 219 1038 272 '
            'Q1120 241 1200 276 V335 H0Z" fill="#A895B3" opacity=".56"/>',
            '<path d="M240 318l-20 35 M370 300l-20 35 M652 322l-20 35 M804 305l-20 35 '
            'M1055 315l-20 35" stroke="#CEB8D2" stroke-width="5" opacity=".66"/>',
        ])
    elif direction == "mixed":
        svg.extend([
            '<path d="M30 288 Q150 225 280 285 Q405 239 514 285 V318H30Z" fill="#B0C3D0" opacity=".46"/>',
            '<path d="M130 414 Q240 389 330 414 M694 338 Q795 309 858 329" fill="none" '
            'stroke="#A7D4D1" stroke-width="5" opacity=".5"/>',
        ])
    else:
        svg.extend([
            '<rect y="210" width="1200" height="365" fill="#C7D3D6" opacity=".27"/>',
            '<path d="M0 310 Q150 263 300 316 T620 308 T900 315 T1200 300" fill="none" '
            'stroke="#E2E8E8" stroke-width="38" opacity=".30"/>',
        ])
    svg.extend([
        '<rect x="42" y="34" width="1116" height="110" rx="18" fill="#0A1D2B" opacity=".82"/>',
        f'<text x="68" y="76" fill="{accent}" font-size="19" font-weight="700" {FONT}>'
        'SIGNAL DESK · 오늘의 시장</text>',
        f'<text x="68" y="122" fill="#FFFFFF" font-size="36" font-weight="800" {FONT}>{_e(title)}</text>',
        f'<text x="1130" y="77" text-anchor="end" fill="#D3E0E3" font-size="21" {FONT}>'
        f'{_e(market)} · {_e(date_label)}</text>',
        '<rect x="42" y="488" width="1116" height="168" rx="20" fill="#0A1D2B" opacity=".91"/>',
    ])
    if direction != "unknown":
        svg.append(f'<text x="68" y="532" fill="#F3F7F6" font-size="25" font-weight="700" {FONT}>'
                   f'오늘 비교 {compared}/{universe}종목 · 상승 {up} · 하락 {down} · 보합 {flat}</text>')
    else:
        expected = card.get("expected_as_of") or "미확인"
        svg.append(f'<text x="68" y="532" fill="#F3F7F6" font-size="24" font-weight="700" {FONT}>'
                   f'오래된 가격이나 부족한 자료로 시장 상태를 판단하지 않아요 · 최근 완료 {_e(expected)}</text>')
    sectors = scene.get("sectors") if direction != "unknown" and card.get("market") == "kr" else None
    labels = []
    if isinstance(sectors, list):
        for sector in sectors[:2]:
            if not isinstance(sector, dict):
                continue
            change = sector.get("median_change_pct")
            if (not isinstance(change, (int, float)) or isinstance(change, bool)
                    or not math.isfinite(change)):
                continue
            label = str(sector.get("sector") or "")
            label = label if len(label) <= 12 else label[:11] + "…"
            labels.append(f"{label} {change:+.2f}%")
    svg.append(f'<text x="68" y="574" fill="#C9DBDE" font-size="21" {FONT}>'
               f'{_e(" · ".join(labels) if labels else "업종별 비교는 아직 확인 중")}</text>')
    svg.append(f'<text x="68" y="617" fill="{accent}" font-size="20" font-weight="700" {FONT}>'
               f'{_e(selection_text)}</text>')
    svg.extend([
        f'<text x="44" y="685" fill="#D7E3E4" font-size="17" {FONT}>'
        '관찰 종목 기준 · 종가로 확인한 과거만 표현 · 다음 가격 예측 아님</text>',
        '</g></svg>',
    ])
    return "".join(svg)
