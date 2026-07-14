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


if __name__ == "__main__":
    app()
