"""Source-dated editorial market card; no image model or inferred news causality.

The composition makes *observed companies and a verified disclosure* the image,
not an abstract weather metaphor. It never feeds the trading engine.
"""

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


def _short(value: object, limit: int) -> str:
    value = str(value or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


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


def _move(value: object) -> float | None:
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def _text(x: int, y: int, value: object, *, size: int, color: str,
          weight: int = 600, extra: str = "") -> str:
    return (f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
            f'font-weight="{weight}" {extra} {FONT}>{_e(value)}</text>')


def _sector_motif(sector: object) -> str:
    """A small, sector-specific illustration, never a claim about the company."""
    label = str(sector or "").lower()
    if any(word in label for word in ("반도체", "전자", "chip", "semiconductor")):
        return ('<g data-motif="semiconductor" fill="none" stroke="#9AE0BE" stroke-width="11" '
                'opacity=".31" stroke-linecap="round" stroke-linejoin="round">'
                '<rect x="505" y="258" width="124" height="124" rx="13"/>'
                '<rect x="531" y="284" width="72" height="72" rx="5"/>'
                '<path d="M526 236v22m40-22v22m40-22v22m-80 124v22m40-22v22m40-22v22'
                'M483 279h22m-22 41h22m-22 41h22m124-82h22m-22 41h22m-22 41h22"/></g>')
    if any(word in label for word in ("자동차", "완성차", "auto", "vehicle")):
        return ('<g data-motif="automobile" fill="none" stroke="#9AE0BE" stroke-width="12" '
                'opacity=".32" stroke-linecap="round" stroke-linejoin="round">'
                '<path d="M474 343l25-54h127l35 54v42H472v-42zM503 343h131M507 289l23-37h73l24 37"/>'
                '<circle cx="510" cy="388" r="15"/><circle cx="628" cy="388" r="15"/></g>')
    if any(word in label for word in ("조선", "해운", "ship", "marine")):
        return ('<g data-motif="ship" fill="none" stroke="#9AE0BE" stroke-width="11" '
                'opacity=".32" stroke-linecap="round" stroke-linejoin="round">'
                '<path d="M465 353h203l-29 55H500l-35-55zM517 352v-70h112v70M558 282v-38h45v38'
                'M462 428q28 17 55 0 27 17 54 0 27 17 54 0 27 17 54 0"/></g>')
    if any(word in label for word in ("에너지", "전력", "배터리", "energy", "utility")):
        return ('<g data-motif="energy" fill="none" stroke="#9AE0BE" stroke-width="11" '
                'opacity=".32" stroke-linecap="round" stroke-linejoin="round">'
                '<path d="M558 237l-66 96h64l-21 101 113-128h-67l38-69z"/></g>')
    if any(word in label for word in ("금융", "은행", "bank", "financial")):
        return ('<g data-motif="finance" fill="none" stroke="#9AE0BE" stroke-width="10" '
                'opacity=".32" stroke-linecap="round" stroke-linejoin="round">'
                '<path d="M475 296l98-52 98 52H475zM489 405h168M480 427h186'
                'M509 311v89m43-89v89m43-89v89m43-89v89"/></g>')
    if any(word in label for word in ("바이오", "제약", "의약", "bio", "pharma")):
        return ('<g data-motif="biotech" fill="none" stroke="#9AE0BE" stroke-width="10" '
                'opacity=".32" stroke-linecap="round">'
                '<path d="M515 245q107 60 0 166m113-166q-107 60 0 166M529 271h85m-102 39h119'
                'M512 352h119m-101 39h83"/></g>')
    return ""


def render(card: dict, *, include_copy: bool = True) -> str:
    """Render a dated market snapshot; withhold movers/news when coverage is poor."""
    market = "해외" if card.get("market") == "us" else "국내"
    scene = card.get("scene") or {}
    fresh = card.get("status") in {"ready", "partial"}
    compared, universe = _count(scene.get("compared")), _count(scene.get("universe"))
    up, down, flat = (_count(scene.get(key)) for key in ("up", "down", "flat"))
    direction = scene.get("direction") if fresh else "unknown"
    if (direction not in {"up", "down", "mixed"} or compared == 0
            or compared != up + down + flat or universe < compared):
        direction = "unknown"
    movers = card.get("movers") if direction != "unknown" else None
    movers = [row for row in movers if isinstance(row, dict) and _move(row.get("change_pct")) is not None][:3] if isinstance(movers, list) else []
    news = card.get("brief_news") if direction != "unknown" else None
    news = [row for row in news if isinstance(row, dict) and row.get("title")][:1] if isinstance(news, list) else []
    accent = {"up": "#187257", "down": "#AF3A35", "mixed": "#335CAB", "unknown": "#596577"}[direction]
    as_of = str(card.get("price_as_of") or "미확인")
    title = (str(card.get("today_headline") or card.get("headline") or "시장 자료 확인 중")
             if direction != "unknown" else "시장 자료를 확인하고 있어요")
    selection = card.get("selection") if direction != "unknown" and card.get("status") == "ready" else None
    selection_time = _kst_time(selection.get("computed_at")) if isinstance(selection, dict) else None
    selection_label = "종가 참고" if isinstance(selection, dict) and selection.get("reference_only") else "매수 판정"
    selection_text = (f"{selection_label} {_count(selection.get('buy_count'))}개 · {selection_time}"
                      if selection_time else "매수 판정 건수 보류 · 가격 또는 판정 시각 확인 전")
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" data-scene="{direction}" '
        + ('role="img" aria-labelledby="title desc">' if include_copy
           else 'aria-hidden="true" focusable="false">'),
    ]
    if include_copy:
        names = ", ".join(str(row.get("name") or row.get("ticker")) for row in movers)
        svg.extend([
            f'<title id="title">{_e(market)} 시장 요약: {_e(title)}</title>',
            f'<desc id="desc">{_e(as_of)} 종가 기준. 등락 폭 큰 종목: {_e(names or "확인 전")}. '
            '공시는 등락의 원인으로 단정하지 않으며 매수 추천이 아닙니다.</desc>',
        ])
    svg.extend([
        '<rect width="1200" height="700" rx="26" fill="#F5F7F5"/>',
        '<rect x="1" y="1" width="1198" height="698" rx="25" fill="none" stroke="#CAD5D1" stroke-width="2"/>',
        '<rect x="32" y="32" width="1136" height="636" rx="20" fill="#FFFFFF"/>',
        '<rect x="32" y="32" width="9" height="636" rx="4" fill="' + accent + '"/>',
        '<path d="M72 122 H1128" stroke="#DCE4E0" stroke-width="2"/>',
        _text(72, 79, "시그널 데스크  ·  종가 기록", size=27, color=accent, weight=800),
        _text(1127, 80, f"{market}  ·  {as_of} 종가", size=27, color="#45534E",
              extra='text-anchor="end"'),
    ])
    if direction == "unknown":
        svg.extend([
            _text(75, 240, "가격을 더 확인해야 해요", size=58, color="#1B2825", weight=800),
            _text(75, 315, "종가가 늦거나 비교할 종목이 부족합니다.", size=31, color="#4B5A55"),
            _text(75, 390, "이전 자료로 종목 순위를 만들지 않겠습니다.", size=30, color="#4B5A55"),
            '<rect x="75" y="492" width="1045" height="90" rx="16" fill="#EDF1F0"/>',
            _text(105, 547, "확인 전  ·  종목과 공시 요약을 잠시 보류", size=29, color="#4B5A55"),
        ])
    else:
        svg.extend([
            _text(75, 175, "등락 폭 큰 종목 · 매수 추천 아님", size=28, color="#263630", weight=800),
            _text(1127, 175, f"비교 {compared}/{universe}개  ·  상승 {up}  하락 {down}",
                  size=25, color="#52615B", extra='text-anchor="end"'),
        ])
        if movers:
            lead = movers[0]
            value = _move(lead.get("change_pct")) or 0.0
            change = f"{value:+.2f}%"
            svg.extend([
                '<rect x="73" y="198" width="650" height="299" rx="24" fill="#162A27"/>',
                _sector_motif(lead.get("sector")),
                _text(106, 251, "종가 등락 폭  ·  관찰 종목", size=26, color="#A6C6BA"),
                _text(106, 333, _short(lead.get("name") or lead.get("ticker"), 12),
                      size=61 if len(str(lead.get("name") or lead.get("ticker") or "")) <= 8 else 48,
                      color="#FFFFFF", weight=800),
                _text(106, 433, change, size=82, color="#9AE0BE" if value >= 0 else "#F5B2A9", weight=800),
                _text(106, 469, _short(lead.get("sector") or lead.get("ticker"), 22),
                      size=24, color="#C6D7CF"),
            ])
            for index, row in enumerate(movers[1:3]):
                y = 198 + 151 * index
                pct = _move(row.get("change_pct")) or 0.0
                color = "#187257" if pct >= 0 else "#AF3A35"
                svg.extend([
                    f'<rect x="742" y="{y}" width="384" height="138" rx="20" fill="#F0F4F1"/>',
                    _text(769, y + 38, f"{index + 2:02d}  ·  {_short(row.get('sector') or '관찰 종목', 12)}",
                          size=22, color="#52615B"),
                    _text(769, y + 85, _short(row.get("name") or row.get("ticker"), 10),
                          size=31, color="#1B2825", weight=800),
                    _text(1101, y + 119, f"{pct:+.2f}%", size=30, color=color,
                          weight=800, extra='text-anchor="end"'),
                ])
            if len(movers) == 1:
                svg.extend([_text(765, 273, "다른 종목은", size=31, color="#52615B"),
                            _text(765, 317, "확인 중", size=34, color="#1B2825", weight=800)])
        else:
            svg.extend([_text(75, 303, "비교한 종목의 종가 변동 없음", size=43,
                              color="#263630", weight=800),
                        _text(75, 378, "종가만으로 다음 움직임을 알 수는 없습니다.",
                              size=29, color="#52615B")])
        news_source = "DART 공시" if news and news[0].get("source") == "DART" else "확인된 기사"
        svg.extend([
            '<path d="M73 523 H1126" stroke="#DCE4E0" stroke-width="2"/>',
            _text(75, 561, f"같은 날 {news_source}" if news else "같은 날 기업 소식", size=24,
                  color=accent, weight=800),
        ])
        if news:
            item = news[0]
            svg.extend([
                _text(75, 610, f"{_short(item.get('name'), 10)}  ·  {_short(item.get('title'), 22)}",
                      size=30, color="#1B2825", weight=700),
                _text(75, 646, f"{news[0].get('source')} 원문 기준 · 주가 변동의 원인으로 단정하지 않음", size=21,
                      color="#52615B"),
            ])
        else:
            message = (card.get("brief_news_error") or
                       "이 카드에 연결된 당일 기업 소식 없음 · 등락 이유를 추정하지 않음")
            svg.append(_text(75, 614, _short(message, 55), size=29, color="#52615B"))
    if include_copy:
        svg.extend([
            _text(72, 684, _short(title, 20), size=19, color="#4B5A55"),
            _text(1128, 684, selection_text, size=19, color="#4B5A55",
                  extra='text-anchor="end"'),
        ])
    svg.append('</svg>')
    return "".join(svg)
