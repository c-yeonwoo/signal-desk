"""Deterministic, shareable market image from the same read-only brief JSON.

The SVG contains no external resources, scripts, or LLM text. It withholds
market numbers on stale bars and full selection counts on partial coverage.
"""

from __future__ import annotations

import datetime as dt
from html import escape
from textwrap import wrap
from zoneinfo import ZoneInfo


WIDTH = 1200
HEIGHT = 700


def _e(value: object) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _lines(value: object, *, width: int = 43, limit: int = 2) -> str:
    parts = wrap(str(value or ""), width=width, break_long_words=True,
                 break_on_hyphens=False)
    if len(parts) > limit:
        parts = parts[:limit]
        parts[-1] = parts[-1].rstrip(" .") + "…"
    return "".join(f'<tspan x="64" dy="{0 if index == 0 else 40}">{_e(part)}</tspan>'
                   for index, part in enumerate(parts))


def _kst_time(value: object) -> str | None:
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(ZoneInfo("Asia/Seoul")).strftime("%m-%d %H:%M KST")
    except (TypeError, ValueError):
        return None


def render(card: dict) -> str:
    """Return a 1200×700 infographic; never claim a fresh reading from stale bars."""
    market = "해외" if card.get("market") == "us" else "국내"
    fresh = card.get("status") in {"ready", "partial"}
    partial = card.get("status") == "partial"
    title = str(card.get("headline") or "오늘 시장을 아직 요약할 수 없어요")
    summary = str(card.get("summary") or "종가를 확인한 뒤 다시 보여드릴게요.")
    tone = ("#F5B971" if not fresh or "약해요" in title else
            "#6DD7A4" if "견조해요" in title else "#9BB9FF")
    as_of = card.get("price_as_of") or "미확인"
    date_label = f"{_e(as_of)} 종가 기준" if fresh else "종가 확인 전"
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{_e(market)} 시장 한 장 요약: {_e(title)}</title>',
        f'<desc id="desc">{_e(summary)} 매매 추천이나 다음 가격 예측이 아닙니다.</desc>',
        '<rect width="1200" height="700" rx="28" fill="#0B1220"/>',
        '<rect x="0" y="0" width="1200" height="10" rx="5" fill="' + tone + '"/>',
        '<text x="64" y="72" fill="#9BB9AE" font-size="24" font-weight="700" '
        'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">SIGNAL DESK · 하루 시장 한눈에</text>',
        f'<text x="1136" y="72" text-anchor="end" fill="#B9C5CF" font-size="22" '
        f'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">{_e(market)} · {date_label}</text>',
        f'<text x="64" y="163" fill="{tone}" font-size="56" font-weight="800" '
        f'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">{_e(title)}</text>',
        '<text x="64" y="222" fill="#E4E9ED" font-size="27" '
        'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
        f'{_lines(summary)}</text>',
    ]
    if fresh:
        facts = card.get("facts") or []
        for index, y in enumerate((337, 451)):
            if len(facts) <= index:
                break
            fact = facts[index]
            pct = fact.get("percent")
            try:
                ratio = max(0.0, min(1.0, float(pct) / 100))
            except (TypeError, ValueError):
                ratio = 0.0
            svg.extend([
                f'<text x="64" y="{y}" fill="#CCD6DE" font-size="26" '
                f'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">{_e(fact.get("label"))}</text>',
                f'<text x="1136" y="{y}" text-anchor="end" fill="#FFFFFF" font-size="32" '
                f'font-weight="800" font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
                f'{_e(fact.get("value"))}</text>',
                f'<rect x="64" y="{y + 20}" width="1072" height="17" rx="8" fill="#283546"/>',
                f'<rect x="64" y="{y + 20}" width="{round(1072 * ratio)}" height="17" '
                f'rx="8" fill="{tone}"/>',
            ])
        if len(facts) > 2:
            third = facts[2]
            svg.append('<rect x="64" y="515" width="1072" height="56" rx="12" fill="#172436"/>')
            svg.append(f'<text x="87" y="552" fill="#CED8DF" font-size="24" '
                       f'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
                       f'{_e(third.get("label"))} · {_e(third.get("value"))} '
                       f'({_e(third.get("as_of"))} 확인)</text>')
        else:
            svg.append('<text x="64" y="551" fill="#9AA9B5" font-size="21" '
                       'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
                       '추가 지표는 해당 날짜의 자료가 확인될 때만 표시해요.</text>')
        selection = card.get("selection") if not partial else None
        selection_time = _kst_time(selection.get("computed_at")) if selection else None
        selection_text = (f'매수 판정 {selection.get("buy_count", 0)}개 · 강력매수 '
                          f'{selection.get("strong_buy_count", 0)}개 · {selection_time}'
                          if selection and selection_time else
                          '매수 판정 건수 보류 · 가격 또는 판정 시각 확인 전')
        svg.append(f'<text x="64" y="617" fill="#E9F0F4" font-size="25" font-weight="700" '
                   f'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
                   f'{_e(selection_text)}</text>')
    else:
        expected = card.get("expected_as_of") or "미확인"
        svg.extend([
            '<rect x="64" y="295" width="1072" height="220" rx="18" fill="#172436"/>',
            '<text x="94" y="370" fill="#F5B971" font-size="36" font-weight="700" '
            'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
            '오래된 가격으로 시장 상태를 판단하지 않아요</text>',
            f'<text x="94" y="425" fill="#CED8DF" font-size="25" '
            f'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
            f'최근 완료 거래일 {_e(expected)} · 저장된 주된 날짜 {_e(as_of)}</text>',
        ])
    unknown = card.get("unknown") or []
    if partial:
        unknown_line = "아직 확인 전: 일부 종목의 종가 · 내일의 방향"
    elif not fresh:
        unknown_line = "아직 확인 전: 최신 종가 · 시장 상태 · 매수 판정"
    elif len(unknown) > 1:
        unknown_line = "아직 확인 전: 일부 참고 지표 · 공시·뉴스 영향 · 내일의 방향"
    else:
        unknown_line = "아직 확인 전: 공시·뉴스 영향 · 내일의 방향"
    svg.extend([
        '<line x1="64" y1="639" x2="1136" y2="639" stroke="#344353"/>',
        f'<text x="64" y="674" fill="#AFC0CC" font-size="20" '
        f'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">{_e(unknown_line)}</text>',
        '<text x="1136" y="674" text-anchor="end" fill="#8D9BA8" font-size="18" '
        'font-family="Apple SD Gothic Neo,Noto Sans KR,sans-serif">'
        '관찰 종목 기준 · 다음 가격 예측 아님</text>',
        '</svg>',
    ])
    return "".join(svg)
