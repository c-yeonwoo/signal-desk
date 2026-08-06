"""CLI — Signal Desk.

사용:
  sigdesk serve    # 웹 대시보드 실행
  sigdesk fetch    # 유니버스+시세(+DART 키 있으면 재무) 수집 -> data/cache/
  sigdesk report   # 수집된 캐시로 터미널 시그널 리포트
"""

from __future__ import annotations

import os

import typer
from rich.console import Console
from rich.table import Table

from signal_desk import config

config.load_env()

app = typer.Typer(add_completion=False, help="Signal Desk — 주식 매매 타이밍 시그널")
console = Console()

_KIND_COLOR = {"BUY": "bold green", "SELL": "bold red", "HOLD": "white"}


@app.command()
def serve(
    host: str = typer.Option(lambda: os.environ.get("HOST", "127.0.0.1")),
    port: int = typer.Option(lambda: int(os.environ.get("PORT", "8765"))),
):
    """대시보드 웹서버 실행."""
    import uvicorn

    console.print(f"[green]Signal Desk:[/green] http://{host}:{port}")
    uvicorn.run("signal_desk.api:app", host=host, port=port, log_level="warning")


@app.command()
def fetch(full: bool = typer.Option(False, "--full", "-f",
          help="시세 전량 재수집(≈5년 백필). 기본은 증분(마지막 저장일부터).")):
    """유니버스+시세 수집(항상) + 재무(DART_API_KEY 있을 때만) → data/cache/."""
    from signal_desk import store

    console.print("[dim]유니버스 조회 중…[/dim]")
    universe = store.fetch_universe()
    console.print(f"[green]유니버스 {len(universe)}종목[/green]")

    console.print(f"[dim]시세 수집 중… ({'전량 백필' if full else '증분'})[/dim]")
    prices = store.fetch_prices(universe, full=full)
    console.print(f"[green]시세 {len(prices)}행[/green] → {store.PRICES_FILE}")

    if not config.dart_key():
        console.print("[yellow]DART_API_KEY 미설정 — 기본적분석 생략(기술점수만 사용)[/yellow]")
    else:
        console.print("[dim]재무데이터 수집 중…[/dim]")
        fundamentals = store.fetch_fundamentals(universe)
        console.print(f"[green]재무데이터 {len(fundamentals)}종목[/green] → {store.FUNDAMENTALS_FILE}")


@app.command("fetch-universe-history")
def fetch_universe_history_cmd(
    months: int = typer.Option(60, "--months", help="거슬러 올라갈 개월 수(월 1회 스냅샷)"),
    force: bool = typer.Option(False, "--force", help="이미 받은 달도 다시 받는다"),
):
    """시점별(PIT) 유니버스 백필 — 생존편향 제거의 원천.

    각 달 첫 거래일의 시총 상위 200을 KRX Open API(`sto/stk_bydd_trd`)로 받아 캐시한다.
    그 응답은 **그 날 상장돼 있던** 종목이라 지금 폐지된 종목도 들어 있다.
    이미 받은 달은 건너뛴다(재실행이 싸다).
    """
    from signal_desk import store

    console.print(f"[dim]PIT 유니버스 백필 — 최근 {months}개월, 월 1회 스냅샷[/dim]")
    r = store.fetch_universe_history(months_back=months, force=force)
    if not r["ok"]:
        console.print(f"[red]{r.get('reason')}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]스냅샷 {r['snapshots']}개[/green] (신규 {r['added']}) · "
                  f"{r['from']} ~ {r['to']} · 고유 종목 [bold]{r['tickers_total']}[/bold]")
    if r["failed"]:
        console.print(f"[yellow]![/yellow] 실패한 달: {', '.join(r['failed'])}")
    if r["changes"]:
        console.print("[dim]최근 편입/편출:[/dim]")
        for c in r["changes"]:
            console.print(f"[dim]  {c['date']}  진입 +{c['in']} · 이탈 −{c['out']}[/dim]")
    # 시세 백필은 **개수를 보고한 뒤** 별도로 돈다 — 몇 종목인지 모르는 채 시작하지 않는다.
    have = set(store.load_all_dated_closes())
    need = [u for u in store.pit_universe_tickers() if u["ticker"] not in have]
    console.print(f"[bold]시세 필요:[/bold] {len(need)}종목 (이미 있음 "
                  f"{r['tickers_total'] - len(need)})")
    if need:
        console.print("[dim]다음: `sigdesk fetch --full` 로 시세를 받으세요 "
                      "(모멘텀이 252거래일을 요구하므로 5년 전량).[/dim]")


@app.command()
def report():
    """수집된 캐시로 종목별 시그널을 Rich 테이블로 출력."""
    from signal_desk import store
    from signal_desk.signals.engine import evaluate

    if not store.is_ready():
        console.print("[red]캐시가 없습니다.[/red] 먼저 `sigdesk fetch`를 실행하세요.")
        raise typer.Exit(1)

    results = evaluate(store.load_universe(), store.load_price_series(), store.load_fundamentals())
    table = Table(title="Signal Desk — 종목 시그널")
    table.add_column("종목")
    table.add_column("코드")
    table.add_column("시그널")
    table.add_column("점수", justify="right")
    table.add_column("신뢰도", justify="right")
    table.add_column("근거")
    for r in results:
        table.add_row(
            r.name, r.ticker, f"[{_KIND_COLOR[r.kind]}]{r.kind}[/{_KIND_COLOR[r.kind]}]",
            f"{r.score:+.2f}", f"{r.confidence:.2f}", " / ".join(r.reasons[:2]) or "-",
        )
    console.print(table)


@app.command()
def accuracy():
    """PIT 시그널 이력 × 종가 → 매수 리프트·팩터 IC (시그널 판별력 실측).

    h20이 안 익었으면 임시로 더 짧은 horizon을 헤드라인으로 쓴다. NaN 팩터값은 IC에서 제외.
    """
    from signal_desk import store
    from signal_desk.signals import accuracy as acc

    df = store.load_signal_history()
    if df.empty:
        console.print("[red]PIT 시그널 이력 없음[/red] — 장 마감 후 스냅샷이 쌓이면 측정됩니다.")
        raise typer.Exit(1)
    console.print("[dim]종가 조인·채점 중…[/dim]")
    out = acc.realized_accuracy(df.to_dict("records"), store.load_all_dated_closes())
    cov = out.get("coverage") or {}
    hh = out.get("headline_horizon")
    console.print(
        f"[green]ready={out.get('ready')}[/green] · headline h{hh}"
        f"{'' if out.get('primary_ready') else ' (임시)'}"
        f" · buy_lift {out.get('buy_lift_pp')}%p"
        f" · buy_prec {out.get('buy_precision_pct')}% (n={out.get('buy_sample')})"
    )
    if cov.get("interim_note"):
        console.print(f"[yellow]{cov['interim_note']}[/yellow]")
    base = out.get("baseline") or {}
    console.print(
        f"[dim]baseline up {base.get('up_pct')}% · matured_primary "
        f"{cov.get('matured_primary')} · price_to {cov.get('price_data_to')}[/dim]"
    )
    # IC는 숫자 하나로 찍지 않는다 — 크기만 보이면 그게 판별력처럼 읽힌다.
    stats = out.get("factor_ic_stats") or {}
    console.print(f"\n[bold]횡단면 IC @h{out.get('factor_ic_horizon')}[/bold] "
                  f"(날짜 단위 · Newey-West · 요건 {out.get('ic_min_dates')}거래일)")
    t = Table(box=None, pad_edge=False)
    for col in ("팩터", "평균IC", "±CI95", "n일", "독립", "폭", "t", "p", "판정"):
        t.add_column(col, justify="right" if col != "팩터" else "left")
    for k, s in stats.items():
        f = lambda v, d=3: (f"{v:+.{d}f}" if isinstance(v, (int, float)) else "–")  # noqa: E731
        if s.get("ic") is not None and s.get("significant"):
            mark = "[green]유의[/green]"
        elif s.get("n_dates"):
            mark = f"[dim]{'무유의' if s.get('ic') is not None else '날짜부족'}[/dim]"
        else:
            mark = "[dim]미측정[/dim]"
        t.add_row(k, f(s.get("ic_mean")), f(s.get("ci95")), str(s.get("n_dates") or 0),
                  str(s.get("independent_dates") or 0), str(s.get("breadth_median") or "–"),
                  f(s.get("t"), 2), (f"{s['p']:.4f}" if s.get("p") is not None else "–"), mark)
    console.print(t)
    # 차단 이유는 **이유별로** 묶는다. "외 9팩터 동일"로 뭉치면 quality·short의
    # `성숙 스냅샷 대기`(고장 후보)가 `날짜 부족`(정상 대기)에 섞여 안 보인다.
    grouped: dict[str, list[str]] = {}
    for k, s in stats.items():
        if s.get("blocked_reason"):
            grouped.setdefault(s["blocked_reason"], []).append(k)
    for why, ks in grouped.items():
        console.print(f"[yellow]{why}[/yellow] [dim]— {', '.join(ks)}[/dim]")


@app.command()
def harness(
    top_pct: float = typer.Option(3.0, "--top-pct", help="매수권 분위(%)"),
    hold: int = typer.Option(5, "--hold", help="보유·리밸런스 주기(거래일)"),
    cost: float = typer.Option(0.25, "--cost", help="왕복 거래비용(%)"),
    trials: int = typer.Option(100, "--trials", help="대조군 시행 수(시행마다 전 위상 재시뮬)"),
    exposure: bool = typer.Option(False, "--exposure", help="국면 익스포저 적용"),
    sweep: bool = typer.Option(False, "--sweep", help="분위·보유기간 조합 일괄 비교"),
    market: str = typer.Option("kr", "--market", help="kr|us — 횡단면 순위는 한 시장 안에서만"),
    shuffle: bool = typer.Option(False, "--shuffle",
                                 help="누수 검사 — 점수와 수익률의 짝을 어긋나게 하고 돌린다"),
    pit: bool = typer.Option(False, "--pit",
                             help="PIT signal_history 점수(fund/flow 포함)로 순위 — 스냅샷 구간만"),
    pit_fund: bool = typer.Option(False, "--pit-fund",
                                  help="시점별 재무로 복원한 6팩터 순위 — 재무·저평가·퀄리티 포함"),
    preregistered: str = typer.Option("", "--preregistered",
                                      help="사전등록 id로 실행 — **보드 정본이 될 수 있는 유일한 경로**"),
    config_json: str = typer.Option("", "--config-json",
                                    help="탐색용 설정 오버라이드 JSON 파일(가중치·선정 룰)"),
    holdout_from: str = typer.Option("", "--holdout-from",
                                     help="이 날짜부터는 **홀드아웃** — 스윕이 보지 않는다(L1)"),
    spa_trials: int = typer.Option(1000, "--spa-trials",
                                   help="SPA 부트스트랩 시행 수(L2 — --sweep일 때만)"),
):
    """포트폴리오 백테스트 — 횡단면 분위 규칙 vs 무작위 대조군 vs 동일가중 벤치마크.

    절대 수익률은 생존편향(유니버스=오늘 기준 상위 200)으로 부풀려져 있으므로 판단 근거로 쓰지
    않는다. 판단은 **무작위 대조군 백분위**로 한다 — 대조군도 같은 편향을 받는다.
    """
    import json as _json
    from pathlib import Path

    from signal_desk import db, prereg, signalcfg, store
    from signal_desk.signals import harness as hz, multiplicity

    if not store.is_ready():
        console.print("[red]캐시가 없습니다.[/red] 먼저 `sigdesk fetch`를 실행하세요.")
        raise typer.Exit(1)

    if preregistered and sweep:
        console.print("[red]--preregistered 와 --sweep 은 같이 못 씁니다.[/red] "
                      "스윕은 탐색 전용이고 정본이 될 수 없습니다.")
        raise typer.Exit(1)
    if preregistered:
        reg = prereg.load()
        if not reg["ok"]:
            console.print(f"[red]{reg['reason']}[/red]")
            raise typer.Exit(1)
        console.print(f"[dim]사전등록 {reg['n_canonical']}개 · 문턱 "
                      f"{reg['threshold_pct']}% (Šidák 보정)[/dim]")
        out = store.run_preregistered(preregistered)
        if not out.get("ready"):
            console.print(f"[red]{out.get('reason')}[/red]")
            raise typer.Exit(1)
        pr_g = out.get("progress") or {}
        console.print(f"[bold]{out.get('verdict')}[/bold] — {out.get('verdict_why')}")
        if pr_g:
            console.print(f"[dim]실효 기간 {pr_g.get('effective_periods')}/"
                          f"{pr_g.get('min_effective_periods')} · PIT "
                          f"{pr_g.get('pit_dates')}/{pr_g.get('min_pit_dates')}일[/dim]")
        console.print("[dim]보드 갱신: "
                      + ("예 — 요건 충족으로 판정 확정" if out.get("board_updated") is not False
                         else "아니오 — 요건 미충족(이력에만 기록)") + "[/dim]")
        return

    overrides: dict = {}
    if config_json:
        overrides = _json.loads(Path(config_json).read_text(encoding="utf-8"))
        console.print(f"[dim]설정 오버라이드 {len(overrides)}개 — 탐색 실행(보드 불변)[/dim]")
    console.print("[dim]패널 구성 중…[/dim]")
    uni = store.load_us_universe() if market == "us" else store.load_universe()
    panel = hz.build_panel(store.load_all_dated_closes(), {u["ticker"] for u in uni})
    console.print(f"[dim]{market.upper()} · {len(panel.dates)}거래일 · {len(panel.closes)}종목 "
                  f"({panel.dates[0]}~{panel.dates[-1]})[/dim]")
    # L1 홀드아웃 — 이 날짜부터는 **스윕이 보지 않는다.** 파라미터를 고른 구간에서 성적을 다시
    # 재면 그건 측정이 아니라 곡선 맞추기다. 홀드아웃 성적은 여기서 **출력하지 않는다** —
    # 보면 본 것이고, 본 구간은 더 이상 홀드아웃이 아니다. 쓰려면 사전등록 `from_date`로 건다.
    holdout_n = 0
    if holdout_from:
        keep = [i for i, d in enumerate(panel.dates) if d < holdout_from]
        holdout_n = len(panel.dates) - len(keep)
        if len(keep) < 60:
            console.print(f"[red]홀드아웃 이전 구간이 {len(keep)}거래일 — 스윕할 표본이 없습니다.[/red]")
            raise typer.Exit(1)
        hi = len(keep)
        panel = hz.Panel(dates=panel.dates[:hi],
                         closes={t: row[:hi] for t, row in panel.closes.items()})
        console.print(f"[yellow]홀드아웃 {holdout_from} 이후 {holdout_n}거래일 제외[/yellow] "
                      f"[dim]— 스윕은 {len(panel.dates)}거래일만 본다. 홀드아웃 성적은 출력하지 "
                      f"않는다(보면 홀드아웃이 아니다). 쓰려면 사전등록 from_date로 걸 것.[/dim]")
    if pit and pit_fund:
        console.print("[red]--pit 와 --pit-fund 는 같이 못 씁니다.[/red] "
                      "전자는 스냅샷 점수, 후자는 시점별 재무 복원입니다.")
        raise typer.Exit(1)
    pit_scores = None
    cov6 = fired6 = covers = None
    if pit_fund:
        # `store.pit_fund_scores` 를 쓴다 — 예전엔 여기서 따로 조립해 **오늘 유니버스**로 돌았고
        # (생존편향 잔존) `store.run_harness` 는 PIT 유니버스로 돌았다. 같은 이름의 실행이 서로
        # 다른 편향을 가졌고 그 차이가 어느 출력에도 안 나타났다.
        pit_scores, cov6, fired6, meta6, covers, uni_note, panel = store.pit_fund_scores(
            panel, store._signal_config_from(overrides) if overrides else signalcfg.get_config(),
            uni)
        if pit_scores is None:
            console.print(f"[red]{meta6.get('error')}[/red]")
            raise typer.Exit(1)
        console.print(f"[dim]시점별 재무 {meta6['fund_dates']}거래일 "
                      f"({meta6['fund_from']}~) · FY {', '.join(meta6['fiscal_years'])} · "
                      f"수급·공매도 제외(6팩터) · {uni_note}[/dim]")
    if pit:
        hdf = store.load_signal_history()
        if hdf.empty:
            console.print("[red]PIT 스냅샷 없음[/red] — `--pit`은 signal_history가 필요합니다.")
            raise typer.Exit(1)
        pit_scores, meta = hz.scores_from_pit(panel, hdf.to_dict("records"))
        console.print(f"[dim]PIT 점수 {meta['pit_cells']}셀 · "
                      f"{meta['pit_from']}~{meta['pit_to']} ({meta['pit_dates']}일)[/dim]")

    combos = ([(tp, h) for tp in (1.0, 3.0, 5.0, 10.0) for h in (5, 20)] if sweep
              else [(top_pct, hold)])
    title = ("포트폴리오 하네스 (시점별 재무 6팩터 — 수급·공매도 제외)" if pit_fund
             else "포트폴리오 하네스 (PIT 8팩터 점수)" if pit
             else "기술·모멘텀 부분집합 탐색 — 가격 재계산은 8팩터가 아니다(기본/저평가 범위 밖)")
    table = Table(title=title)
    for c in ("분위", "보유", "전략 누적", "위상편차", "무작위 중위", "초과", "백분위", "판정"):
        table.add_column(c, justify="left" if c == "판정" else "right")
    seen_warnings: list[str] = []
    # L2(SPA)용 — 조합별 (전략 − 벤치마크) 기간 초과수익. 스윕이 끝난 뒤 한 번에 검정한다.
    spa_diffs: dict[int, dict[str, list[float]]] = {}
    # DSR(L3)이 고르기를 보정하려면 시도 횟수를 넘겨야 한다. 안 넘기면 `n_trials=1`이 되어
    # 기대 최대 Sharpe가 0이 되고 **보정 없는 DSR이 "유의"로 통과한다**(실측 0.9918).
    # 스윕 자체가 조합 수만큼의 시도이므로 이력 + 이번 조합 수를 더한다.
    _tc = db.harness_trial_counts(market=market)
    _sh = db.harness_sharpes(market=market)
    _n_trials = int(_tc.get("distinct_configs") or 0) + len(combos)
    _sr_var = None
    if len(_sh) >= 4:
        _m = sum(_sh) / len(_sh)
        _sr_var = sum((x - _m) ** 2 for x in _sh) / (len(_sh) - 1)
    _kw = {"n_trials": _n_trials, "sr_variance": _sr_var}
    console.print(f"[dim]시도 횟수(L4): 이력 고유 {_tc.get('distinct_configs')}조합 + 이번 "
                  f"{len(combos)}조합 = {_n_trials} · 조정 가능 파라미터 "
                  f"{_tc.get('tunable_params')}개[/dim]")
    for tp, h in combos:
        cfg = hz.HarnessConfig(top_pct=tp, rebalance_days=h, cost_pct=cost,
                               random_trials=trials, use_exposure=exposure,
                               shuffle_returns=shuffle,
                               signal_config=store._signal_config_from(overrides)
                               if overrides else signalcfg.get_config())
        regimes = hz.regimes_at(panel, hz._rebalance_indices(panel, cfg)) if exposure else None
        if pit_fund:
            out = hz.run(panel, cfg, regimes, scores=pit_scores, score_source="price6",
                         coverage=cov6, fired=fired6, covers=covers, **_kw)
        elif pit:
            out = hz.run(panel, cfg, regimes, scores=pit_scores, score_source="pit", **_kw)
        else:
            out = hz.run(panel, cfg, regimes, **_kw)
        if not out["ready"]:
            console.print(f"[red]{out['reason']}[/red]")
            raise typer.Exit(1)
        # 보드 정본은 **사전등록 실행만** 갱신한다(--preregistered). 여기서 저장하면 8조합 스윕의
        # 마지막 칸이 정본이 되는데, 판별력이 전혀 없어도 그중 하나가 95%를 넘을 확률이 33.7%다.
        # 탐색 결과는 이력에만 남긴다 — 나중에 "무엇을 시험해 봤나"를 되짚을 수 있게.
        cfg_dict = store.signal_config_dict(cfg.signal_config)
        db.harness_run_insert({
            "preregistered_id": None, "score_source": out.get("score_source") or "price",
            "market": market,
            "config_json": _json.dumps(cfg_dict, ensure_ascii=False, sort_keys=True),
            "config_hash": prereg.config_hash(cfg_dict),
            "harness_json": _json.dumps({"hold": h, "top_pct": tp, "cost_pct": cost,
                                         "trials": trials, "exposure": exposure,
                                         "shuffle": shuffle, "pit": pit},
                                        ensure_ascii=False, sort_keys=True),
            "percentile": (out.get("vs_random") or {}).get("percentile"),
            "periods": out.get("periods"), "empty_periods": out.get("empty_periods"),
            "effective_periods": out.get("effective_periods"),
            "price_data_to": panel.dates[-1] if panel.dates else None,
            "verdict": out.get("verdict"), "verdict_why": out.get("verdict_why"),
            "is_locked": False,
            "warnings_json": _json.dumps(out.get("warnings") or [], ensure_ascii=False),
            "note": "CLI 탐색 실행 — 보드 정본 아님",
        })
        # 조합별 초과수익 시계열 — 위상 평균이 아니라 **첫 위상**을 쓴다(길이가 같아야 SPA가 성립).
        ph = out.get("phase0_rets") or {}
        a, b = ph.get("strategy") or [], ph.get("benchmark") or []
        if a and len(a) == len(b):
            # **보유기간별로 나눠 담는다.** hold가 다르면 기간 수가 달라(5일 219 vs 20일 54)
            # 같은 날짜축이 아니고 SPA가 성립하지 않는다 — 섞으면 검정이 그냥 거부된다.
            spa_diffs.setdefault(h, {})[f"top{tp:g}%"] = [x - y for x, y in zip(a, b)]
        s, r = out["strategy"], out["vs_random"]
        pct, verdict = r["percentile"], out["verdict"]
        color = {"판별력 있음": "green", "역판별력": "red"}.get(verdict, "yellow")
        table.add_row(f"{tp:g}%", f"{h}일", f"{s['total_ret_pct']:+.1f}%",
                      f"{s['phase_spread_pp']:.1f}pp", f"{r['median_total_pct']:+.1f}%",
                      f"{r['excess_pp']:+.1f}pp",
                      f"{pct:.1f}%" if pct is not None else "–",
                      f"[{color}]{verdict}[/{color}]")
        for w in out["warnings"]:
            if w not in seen_warnings:
                seen_warnings.append(w)
    console.print(table)
    # 커버리지 게이트(X2)가 하네스에 실제로 걸렸는지 — 안 걸렸으면 라이브와 다른 전략을 잰 것이다.
    g = out.get("data_coverage_gate") or {}
    if g.get("min_required"):
        console.print(f"[dim]커버리지 게이트 {g['min_required']:.0%} — "
                      + ("패널 있음 · 차단 " + str(g.get("blocked", 0)) + "회"
                         if g.get("panel_given") else "[yellow]패널 없음 · 미적용[/yellow]")
                      + "[/dim]")
    # DSR(L3) — 시도 횟수를 감안한 초과수익 Sharpe. 판정을 대체하지 않는다.
    d = out.get("dsr") or {}
    if d.get("sharpe") is not None:
        console.print(f"[dim]DSR(초과수익 기준) {d.get('dsr')} · 기간 Sharpe {d['sharpe']:+.3f} · "
                      f"시도 {d.get('n_trials')}회 기대최대 {d.get('expected_max_sharpe'):+.3f} · "
                      f"왜도 {d.get('skew')} 첨도 {d.get('kurtosis')} · "
                      f"T={d.get('n_periods')}[/dim]")
        if d.get("blocked_reason"):
            console.print(f"[dim]  → {d['blocked_reason']}[/dim]")
    # SPA(L2) — 조합을 여러 개 봤을 때 "최고가 벤치마크보다 낫다"의 p-value. 보유기간별로 돈다.
    for h_group in sorted(spa_diffs):
        group = spa_diffs[h_group]
        if len(group) < 2:
            continue
        spa = multiplicity.spa_test(group, trials=spa_trials)
        if spa.get("p_value") is not None:
            col = "green" if spa["significant"] else "yellow"
            console.print(f"[bold]SPA[/bold] 보유 {h_group}일 · 분위 {spa['n_models']}개 · "
                          f"T={spa['n_periods']} · 최고 [bold]{spa['best']}[/bold] · "
                          f"통계량 {spa['statistic']} · [{col}]p={spa['p_value']}[/{col}]")
        elif spa.get("blocked_reason"):
            console.print(f"[yellow]SPA 불가(보유 {h_group}일)[/yellow] "
                          f"[dim]{spa['blocked_reason']}[/dim]")
    if any(len(g) > 1 for g in spa_diffs.values()):
        console.print("[dim]  Hansen(2005) SPA — 정상 부트스트랩으로 조합 간 상관을 반영한다. "
                      "Šidák보다 덜 보수적이지만 **고른 것이라는 사실은 남는다**: p가 작아도 "
                      "사전등록 없이 채택하면 사후선택이다. 보유기간이 다르면 기간 수가 달라 "
                      "같은 날짜축이 아니므로 보유별로 따로 검정한다.[/dim]")
    console.print("[dim]탐색 실행 — 이력에만 남았습니다(GET /api/harness/runs). 보드 정본은 "
                  "`--preregistered <id>` 로 사전등록 조합을 돌릴 때만 갱신됩니다.[/dim]")
    for w in seen_warnings:
        console.print(f"[yellow]![/yellow] {w}")
    if len(combos) > 1:
        # 조합을 여러 개 동시에 보면 그중 하나가 우연히 95%를 넘을 확률이 조합 수만큼 커진다.
        chance = (1 - 0.95 ** len(combos)) * 100
        console.print(f"[yellow]![/yellow] 조합 {len(combos)}개를 한 번에 봤다 — 판별력이 전혀 "
                      f"없어도 그중 하나가 95%를 넘을 확률이 약 {chance:.0f}%다. 한 칸이 초록이란 "
                      f"이유로 그 조합을 고르면 그건 측정이 아니라 고르기다")
    console.print("[dim]백분위 = 대조군(같은 시뮬레이터에 라벨 치환한 점수) 중 전략보다 못한 "
                  "시행의 비율. 95% 이상이라야 순위 판별력의 증거로 볼 수 있다(생존편향·거래비용·"
                  "회전율은 대조군과 공유). 전략·대조군 모두 리밸런스 위상 전부를 돌려 평균낸 "
                  "값이다.[/dim]")


if __name__ == "__main__":
    app()
