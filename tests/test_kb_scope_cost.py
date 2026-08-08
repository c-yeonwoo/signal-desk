"""KB(LLM) 대상 범위와 모델 선택 — 대상 수가 곧 비용이다.

2026-08-08 실측: 월 $64.81/$100 중 Haiku 23,006회 · $30.21이 KB 종목 뉴스였다
(`naver_news` 수락 5,322건). 종목 뉴스는 **1건마다** LLM이 붙으므로 대상 종목 수가 곧 비용이고,
예전 `_kb_targets`는 외부후보 + 주도섹터(10+4) + 매수권(16) + 점수상위(24) + 보유 + 관심으로
50종목이 넘었다.

**그런데 매수권만으로 좁히면 안 된다** — 뉴스는 소급 수집이 안 되므로 오늘 매수권에 든 종목의
지난주 기사는 이미 못 받는다. 매수권만 모으면 전환된 그 날 이력이 0이고 "왜 갑자기 올랐나"에
답할 수 없다. 그래서 **뽑을 자리(k)** 까지 넣고, 그 k는 엔진과 같은 `rank_slots` 로 센다.
"""

from __future__ import annotations

import inspect

from signal_desk import api, kb, llm


# ─────────────────────────── 대상 범위 ───────────────────────────

def test_kb_llm_targets_are_bounded_by_the_rank_window():
    """뽑을 자리는 **엔진과 같은 함수**로 센다 — 상수를 새로 만들면 창과 조용히 어긋난다."""
    src = inspect.getsource(api._kb_targets)
    assert "rank_slots(" in src, "뽑을 자리를 엔진 함수로 세지 않는다"
    assert "signalcfg.get_config().rank_top_pct" in src, \
        "화면·봇이 쓰는 설정이 아니라 소스 상수를 쟀다면 창과 갈라진다"
    # 예전의 무관한 상수 24가 다시 들어오면 안 된다.
    assert "near_limit" not in src, "창과 무관한 상수로 상위 N을 자른다"


def test_kb_llm_targets_exclude_the_unbounded_sets():
    """외부후보·주도섹터는 "언젠가 볼 수도 있는" 집합이라 상한이 없다 — LLM 경로에서 뺀다.

    판단(매수·veto)에 닿지 않는 종목의 뉴스에 LLM을 쓰는 것이 비용의 대부분이었다.
    """
    src = inspect.getsource(api._kb_targets)
    assert "external_watch" not in src, "외부후보 워치리스트가 LLM 대상에 남아 있다"
    assert "tickers_for_lead_tags" not in src, "주도섹터가 LLM 대상에 남아 있다"
    # 판단에 닿는 집합은 반드시 남아 있어야 한다.
    for must in ("is_buy(", "bot_position_tickers_all", "fav_tickers_all"):
        assert must in src, f"{must} — 판단에 닿는 대상이 빠졌다"


def test_free_disclosure_poll_stays_wide():
    """**무료 경로는 좁히지 않는다.** 악재 veto는 "살 수 있게 될 것"까지 봐야 한다.

    `_kb_lite_targets`(DART lite)는 종목당 1콜이고 LLM이 0원이다 — 비용 이유로 좁히면
    veto 커버리지만 잃는다. LLM 경로와 무료 경로를 같은 이유로 좁히는 실수를 막는다.
    """
    src = inspect.getsource(api._kb_lite_targets)
    assert "rank" in src or "순위" in src, "순위 상위로 채우는 로직이 사라졌다"
    assert "kb_dart_lite_max_tickers" in src, "상한이 설정에서 오지 않는다"


# ─────────────────────────── 모델 선택 ───────────────────────────

def test_per_item_compression_uses_the_cheap_model():
    """**항목별 압축은 Haiku다.** 이 함수는 수집한 기사 수만큼 불린다.

    `collect_fanding`·`collect_outstanding` 의 기사 루프가 `import_document` → `_summarize_text`
    로 들어온다(실측 수락 fanding 178 · outstanding 163). Sonnet을 여기 두면 '항목 압축'에
    품질비를 내는 것이고, 시장 톤 해석은 배치 끝 `build_macro_digest`(Sonnet) 1회가 맡는다.
    """
    seen = {}

    def fake(system, user, **kw):
        seen["model"] = kw.get("model")
        return {"summary": "요약", "points": []}

    import pytest
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(llm, "available", lambda: True)
        mp.setattr(llm, "complete_json", fake)
        kb._summarize_text("삼성전자", "제목", "본문" * 200)
        assert seen["model"] == llm.DIGEST_MODEL, \
            f"항목별 압축에 {seen['model']} 를 쓴다 — 기사 수만큼 곱해진다"
        # 사람이 올린 문서 1건은 품질 모델을 쓴다(볼륨 1).
        kb._summarize_text("삼성전자", "제목", "본문" * 200, quality=True)
        assert seen["model"] == llm.DIGEST_QUALITY_MODEL
    finally:
        mp.undo()


def test_market_tone_synthesis_keeps_the_quality_model():
    """거시 종합은 **하루 1회**라 Sonnet을 유지한다 — 여기를 내리면 경제 맥락이 얕아진다."""
    src = inspect.getsource(kb.build_macro_digest)
    assert "DIGEST_QUALITY_MODEL" in src, "거시 종합까지 값싼 모델로 내렸다"


def test_adverse_event_extraction_keeps_the_quality_model():
    """악재 후보 추출은 **매수 veto에 닿는 안전 게이트**다 — 비용 이유로 내리지 않는다.

    약한 모델은 실제 악재를 **놓치는** 방향으로 틀리고, 그러면 veto가 안 걸려 사면 안 되는 것을
    산다. 대상이 50종목 → 매수권+k(실측 8)로 줄어 볼륨은 이미 ~85% 감소했다.
    """
    src = inspect.getsource(kb._extract_candidate_event)
    assert "DIGEST_QUALITY_MODEL" in src, "안전 게이트를 값싼 모델로 내렸다"


# ─────────────────── 하루 가드: "시도했나"가 아니라 "했나" ───────────────────

def _kv(monkeypatch, initial=None):
    store = dict(initial or {})
    monkeypatch.setattr(api.db, "kv_get", lambda k: store.get(k))
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: store.__setitem__(k, v))
    return store


def test_turning_the_flag_on_takes_effect_the_same_day(monkeypatch):
    """**플래그를 켜는 날 바로 돌아야 한다.**

    예전엔 `kb_collect_date` 하나로 KB 수집과 US 재무 백필을 같이 막았고, 그 키를 **KB가 꺼져
    있어도** 찍었다. 그래서 켜는 날은 이미 오늘 키가 있어 다음 날까지 무동작이었다 —
    켠 사람에게는 고장으로 보인다. 가드는 "시도했나"가 아니라 **"실제로 했나"** 를 기록해야 한다.
    """
    ran = []
    # 오늘 이미 US 백필이 돌아 `kb_collect_date` 가 찍힌 상태 = 플래그를 켜기 직전의 상황
    kv = _kv(monkeypatch, {"kb_collect_date": api._kst_today()})
    monkeypatch.setattr(api.config, "kb_auto_collect", lambda: True)
    monkeypatch.setattr(api, "_kb_targets", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.kb, "refresh", lambda t: ran.append("refresh") or {"updated": 1})
    for fn in ("collect_fanding", "collect_outstanding", "collect_youtube", "collect_rss_macro"):
        monkeypatch.setattr(api.kb, fn, lambda *a, **k: {"imported": 0})
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])

    api._daily_kb_collect()
    assert ran == ["refresh"], "US 백필 키가 KB 수집까지 막았다 — 켠 날 아무 일도 안 일어난다"
    assert kv[api._KB_LLM_COLLECT_KEY] == api._kst_today(), "실제로 돌았는데 가드를 안 찍었다"


def test_off_day_does_not_consume_the_kb_slot(monkeypatch):
    """꺼져 있던 날은 KB 슬롯을 **소진하지 않는다** — 안 했으니까."""
    kv = _kv(monkeypatch)
    monkeypatch.setattr(api.config, "kb_auto_collect", lambda: False)
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    api._daily_kb_collect()
    assert api._KB_LLM_COLLECT_KEY not in kv, "안 돌았는데 오늘을 완료로 찍었다"
    assert kv.get("kb_collect_date") == api._kst_today(), "US 백필 가드는 찍혀야 한다"


def test_kb_collect_runs_once_per_day(monkeypatch):
    """켜져 있어도 하루 한 번이다 — 30분 루프가 하루 종일 Sonnet을 부르면 안 된다."""
    ran = []
    _kv(monkeypatch)
    monkeypatch.setattr(api.config, "kb_auto_collect", lambda: True)
    monkeypatch.setattr(api, "_kb_targets", lambda: [{"ticker": "A", "name": "가"}])
    monkeypatch.setattr(api.kb, "refresh", lambda t: ran.append(1) or {"updated": 1})
    for fn in ("collect_fanding", "collect_outstanding", "collect_youtube", "collect_rss_macro"):
        monkeypatch.setattr(api.kb, fn, lambda *a, **k: {"imported": 0})
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])

    api._daily_kb_collect()
    api._daily_kb_collect()
    assert len(ran) == 1, f"하루에 {len(ran)}번 돌았다"
