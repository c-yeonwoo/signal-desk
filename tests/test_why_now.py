"""왜 지금 이 종목인가 — 섹터 동반 여부가 핵심이다."""

from __future__ import annotations

from signal_desk.signals import why_now


def _rows(spec: dict[str, dict[str, dict]]) -> list[dict]:
    """{date: {ticker: fields}} → PIT 레코드 리스트."""
    out = []
    for d, tickers in spec.items():
        for tk, f in tickers.items():
            out.append({"date": d, "ticker": tk, **f})
    return out


_SEC = {"A": "건자재", "P1": "건자재", "P2": "건자재", "P3": "건자재", "Z": "반도체"}


def _sector_of(tk):
    return _SEC.get(tk)


def test_sector_wide_move_is_called_sector_not_stock_picking():
    """섹터가 통째로 올랐으면 **섹터 전체**라고 말해야 한다 — 팩터가 이 종목을 고른 게 아니다."""
    rows = _rows({
        "2026-08-01": {t: {"score": 0.0, "kind": "HOLD"} for t in ("A", "P1", "P2", "P3")},
        "2026-08-05": {"A": {"score": 1.0, "kind": "BUY"},
                       "P1": {"score": 0.9, "kind": "HOLD"},
                       "P2": {"score": 0.8, "kind": "HOLD"},
                       "P3": {"score": 1.1, "kind": "BUY"}},
    })
    r = why_now.explain(rows, "A", sector_of=_sector_of, name="에이")
    assert r["ready"] and r["score_delta"] == 1.0
    assert r["sector"]["verdict"] == "sector"
    assert r["sector"]["peers_n"] == 3
    assert r["sector"]["share"] >= why_now._SECTOR_SHARE
    assert "섹터 전체" in r["sector"]["text"]


def test_idiosyncratic_move_is_called_stock_specific():
    """섹터는 가만있는데 이 종목만 올랐으면 **종목 고유**다."""
    rows = _rows({
        "2026-08-01": {t: {"score": 0.0, "kind": "HOLD"} for t in ("A", "P1", "P2", "P3")},
        "2026-08-05": {"A": {"score": 1.0, "kind": "BUY"},
                       "P1": {"score": 0.05, "kind": "HOLD"},
                       "P2": {"score": 0.0, "kind": "HOLD"},
                       "P3": {"score": -0.1, "kind": "HOLD"}},
    })
    r = why_now.explain(rows, "A", sector_of=_sector_of)
    assert r["sector"]["verdict"] == "idiosyncratic"
    assert "종목 고유" in r["sector"]["text"]


def test_too_few_peers_is_not_a_verdict():
    """동료가 적으면 중위가 의미 없다 — 판정하지 않고 **이유를 낸다**."""
    rows = _rows({
        "2026-08-01": {"Z": {"score": 0.0, "kind": "HOLD"}},
        "2026-08-05": {"Z": {"score": 1.0, "kind": "BUY"}},
    })
    r = why_now.explain(rows, "Z", sector_of=_sector_of)
    assert r["sector"]["verdict"] == "peers_too_few"
    assert "최소" in r["sector"]["text"]


def test_peers_must_exist_at_both_window_ends():
    """창 한쪽에만 있는 종목을 동료로 세면 **다른 구간**을 비교한 것이 된다."""
    rows = _rows({
        "2026-08-01": {"A": {"score": 0.0, "kind": "HOLD"},
                       "P1": {"score": 0.0, "kind": "HOLD"}},
        "2026-08-05": {"A": {"score": 1.0, "kind": "BUY"},
                       "P1": {"score": 0.9, "kind": "HOLD"},
                       "P2": {"score": 5.0, "kind": "BUY"},     # 시작일에 없음 → 제외
                       "P3": {"score": 5.0, "kind": "BUY"}},    # 시작일에 없음 → 제외
    })
    r = why_now.explain(rows, "A", sector_of=_sector_of)
    assert r["sector"]["peers_n"] == 1, "창 양 끝에 다 있는 동료만 세야 한다"
    assert r["sector"]["verdict"] == "peers_too_few"


def test_factor_moves_are_named_in_plain_korean():
    """어느 관점이 밀었는지 **말로** 낸다 — 초보자에게 `momentum` 은 정보가 아니다."""
    rows = _rows({
        "2026-08-01": {"A": {"score": 0.0, "kind": "HOLD", "momentum": 0.0,
                             "technical": 0.0, "flow": 0.0},
                       "P1": {"score": 0.0, "kind": "HOLD"},
                       "P2": {"score": 0.0, "kind": "HOLD"},
                       "P3": {"score": 0.0, "kind": "HOLD"}},
        "2026-08-05": {"A": {"score": 1.0, "kind": "BUY", "momentum": 0.6,
                             "technical": 0.2, "flow": 0.01},
                       "P1": {"score": 0.1, "kind": "HOLD"},
                       "P2": {"score": 0.0, "kind": "HOLD"},
                       "P3": {"score": 0.0, "kind": "HOLD"}},
    })
    r = why_now.explain(rows, "A", sector_of=_sector_of)
    labels = [f["label"] for f in r["factors"]]
    assert "오르는 추세" in labels and "차트 흐름" in labels
    assert all("momentum" != f["label"] for f in r["factors"])
    assert "누가 사고 있나" not in labels, "0.05 미만 변화는 표기하지 않는다"
    # 표본이 적어 표준화가 불가하면 **크기순을 주장하지 않는다** — 눈금이 다르기 때문이다.
    assert all(f["z"] is None for f in r["factors"])


def test_percentile_does_not_outrank_a_normalized_factor():
    """**이게 실측에서 틀렸던 것이다.** PIT 컬럼은 8개 중 5개가 정규화가 아니다.

    2026-08-08 HD현대: `주가가 싼가 -4.60`(백분위 45.5→40.9)이 `차트 흐름 -0.30`(정규화)을
    크기로 이겨 **1위로 올라왔다.** 백분위는 원래 수십 단위로 움직이므로 크기 비교가 무의미하다.
    전 종목 대비 표준화하면 순위가 뒤집힌다.
    """
    day0, day1 = {}, {}
    # 동료 12종목 — 백분위는 넓게(SD 큼), 차트 흐름은 좁게(SD 작음) 움직인다.
    for i in range(12):
        tk = f"P{i}"
        day0[tk] = {"score": 0.0, "kind": "HOLD", "valuation": 50.0, "technical": 0.0}
        day1[tk] = {"score": 0.1, "kind": "HOLD",
                    "valuation": 50.0 + (i - 6) * 3.0,      # ±18 분위
                    "technical": (i - 6) * 0.01}            # ±0.06
    day0["A"] = {"score": 1.8, "kind": "BUY", "valuation": 45.5, "technical": 0.2}
    day1["A"] = {"score": 1.6, "kind": "HOLD", "valuation": 40.9, "technical": -0.1}
    r = why_now.explain(_rows({"2026-08-01": day0, "2026-08-05": day1}), "A")

    labels = [f["label"] for f in r["factors"]]
    assert labels[0] == "차트 흐름", f"백분위가 여전히 1위다: {labels}"
    val = next(f for f in r["factors"] if f["factor"] == "valuation")
    tech = next(f for f in r["factors"] if f["factor"] == "technical")
    assert abs(val["delta"]) > abs(tech["delta"]), "원값 크기는 백분위가 더 크다(전제 확인)"
    assert abs(val["z"]) < abs(tech["z"]), "표준화하면 차트 흐름이 더 큰 움직임이다"
    # 눈금을 화면에 밝힌다 — 나란히 놓고 단위가 없으면 비교 가능하다고 읽힌다.
    assert val["unit"] == "분위" and tech["unit"] == ""


def test_turn_on_date_and_biggest_jump_are_reported():
    """전환일과 가장 크게 뛴 날 — "요즘 갑자기"의 '언제'에 답한다."""
    rows = _rows({
        "2026-08-01": {"A": {"score": 0.0, "kind": "HOLD"}},
        "2026-08-02": {"A": {"score": 0.1, "kind": "HOLD"}},
        "2026-08-03": {"A": {"score": 1.4, "kind": "BUY"}},     # 급등 + 매수권 전환
        "2026-08-04": {"A": {"score": 1.5, "kind": "BUY"}},
    })
    r = why_now.explain(rows, "A")
    assert r["turned_buy_on"] == "2026-08-03"
    assert r["biggest_move"]["date"] == "2026-08-03"
    assert r["biggest_move"]["delta"] == 1.3


def test_no_move_says_so_instead_of_inventing_a_story():
    """변화가 거의 없으면 **설명을 붙이지 않는다** — 없는 변화에 이야기를 달면 사후 합리화다."""
    rows = _rows({
        "2026-08-01": {"A": {"score": 1.00, "kind": "BUY"}},
        "2026-08-05": {"A": {"score": 1.05, "kind": "BUY"}},
    })
    r = why_now.explain(rows, "A")
    assert r["quiet"] is True and r["quiet_reason"]


def test_single_snapshot_is_blocked_with_a_reason():
    r = why_now.explain([{"date": "2026-08-05", "ticker": "A", "score": 1.0, "kind": "BUY"}], "A")
    assert r["ready"] is False and "하루뿐" in r["blocked_reason"]


def test_ticker_missing_at_a_window_end_is_blocked_with_a_reason():
    """유니버스에 새로 들어온 종목을 조용히 0으로 처리하면 안 된다."""
    rows = _rows({
        "2026-08-01": {"P1": {"score": 0.0, "kind": "HOLD"}},
        "2026-08-05": {"A": {"score": 1.0, "kind": "BUY"}, "P1": {"score": 0.1, "kind": "HOLD"}},
    })
    r = why_now.explain(rows, "A")
    assert r["ready"] is False and "스냅샷에 다 있지 않습니다" in r["blocked_reason"]


def test_nan_never_reaches_the_payload():
    """NaN 은 유효 JSON 이 아니다 — pandas 결손이 그대로 나가면 클라이언트가 깨진다."""
    nan = float("nan")
    rows = _rows({
        "2026-08-01": {"A": {"score": 0.0, "kind": "HOLD", "flow": nan}},
        "2026-08-05": {"A": {"score": 1.0, "kind": "BUY", "flow": 0.5}},
    })
    r = why_now.explain(rows, "A")
    assert all(f["factor"] != "flow" for f in r["factors"])
    assert r["score_delta"] == r["score_delta"]        # NaN 이면 자기와 다르다


def test_payload_never_carries_news_or_macro():
    """**뉴스·거시를 근거에 섞지 않는다.** 섞으면 "이 기사 때문에 올랐다"는 없는 인과가 된다.

    이 리포가 명시적으로 경계하는 것이고(`설명과 결정이 다른 데이터`), 화면이 이 블록에
    `근거` 배지를 달기 때문에 여기 뉴스가 들어오면 라벨이 거짓이 된다.
    """
    import ast
    import inspect

    # **텍스트가 아니라 import 를 본다.** 설명 주석에 "LLM을 쓰지 않는다"라고 쓰면
    # 문자열 검사는 오탐한다 — 이 함정에 이미 여러 번 빠졌다(주석 안의 단어를 세는 실수).
    tree = ast.parse(inspect.getsource(why_now))
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
    for banned in ("llm", "kb", "kb_search", "macro", "db"):
        assert not any(banned == m or m.endswith("." + banned) for m in imported), \
            f"근거 모듈이 {banned} 를 import 한다 — 맥락은 호출자가 따로 라벨해야 한다"

    # 호출자가 뉴스를 주입할 통로도 없어야 한다.
    params = set(inspect.signature(why_now.explain).parameters)
    assert not (params & {"news", "kb", "macro", "digest", "docs"}), \
        "근거 함수가 뉴스·거시를 인자로 받는다"


def test_sector_threshold_is_exposed_with_the_value_not_just_a_verdict():
    """문턱은 값과 함께 낸다 — 판정만 내면 왜 그렇게 갈랐는지 모른다(레포 규칙)."""
    rows = _rows({
        "2026-08-01": {t: {"score": 0.0, "kind": "HOLD"} for t in ("A", "P1", "P2", "P3")},
        "2026-08-05": {"A": {"score": 1.0, "kind": "BUY"},
                       "P1": {"score": 0.6, "kind": "HOLD"},
                       "P2": {"score": 0.6, "kind": "HOLD"},
                       "P3": {"score": 0.6, "kind": "HOLD"}},
    })
    r = why_now.explain(rows, "A", sector_of=_sector_of)
    for k in ("share", "peers_n", "peer_median_delta"):
        assert r["sector"][k] is not None, f"{k} 를 노출하지 않는다"
    assert r["basis"] and "인과가 아니라" in r["basis"]


def test_daily_change_shares_the_same_scale_aware_ranking():
    """**어제 배포한 `daily_change`(#362)도 같은 버그였다** — 같은 컬럼을 크기순으로 정렬했다.

    통계를 두 곳에 두면 갈라진다. 두 화면이 같은 데이터로 다른 1위를 말하면 어느 쪽도
    믿을 수 없다.
    """
    import inspect

    from signal_desk.signals import daily_change

    src = inspect.getsource(daily_change._cause)
    assert "rank_factor_moves" in src, "daily_change 가 자기 정렬을 들고 있다"
    assert "-abs(m[\"delta\"])" not in src, "크기순 정렬이 남아 있다"
    # 전 종목을 넘겨받아야 표준화가 된다 — 안 넘기면 z 가 늘 None 이다.
    diff_src = inspect.getsource(daily_change.diff)
    assert "all_prev=" in diff_src and "all_cur=" in diff_src


def test_scale_metadata_covers_every_snapshot_factor():
    """새 팩터를 추가하면 **단위도 같이 등록**해야 한다 — 빠지면 눈금 없이 나란히 놓인다."""
    for f in why_now._FACTORS:
        assert f in why_now._FACTOR_UNIT, f"{f} 의 단위가 등록되지 않았다"
        assert f in why_now._FACTOR_KO, f"{f} 의 한국어 이름이 없다"
