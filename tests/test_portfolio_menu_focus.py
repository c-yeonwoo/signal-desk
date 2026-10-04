"""The portfolio's first view should keep holdings and diagnosis ahead of optional tools."""

from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html").read_text(encoding="utf-8")


def test_portfolio_primary_path_precedes_optional_sections():
    portfolio = HTML.split('id="trading-rebal"', 1)[1].split('id="trading-dividend"', 1)[0]
    primary = [
        'id="pf-market-seg"',
        'id="real-holdings-card"',
        'id="manual-holdings-card"',
        'id="portfolio-intelligence-card"',
        'id="pf-evidence"',
        'id="pf-tools"',
    ]
    assert [portfolio.index(marker) for marker in primary] == sorted(portfolio.index(marker) for marker in primary)
    assert 'id="pf-evidence" open' not in portfolio
    assert 'id="pf-tools" open' not in portfolio
    assert '보유 진단하기' in portfolio
    assert '실계좌 추종 설정 →' not in portfolio
    assert 'id="home-next"' not in portfolio
    assert portfolio.index('id="home-asof"') < portfolio.index('class="home-more"')
    assert "InvestmentGuide.detail('상세 분석과 근거 보기'" in HTML
    assert "InvestmentGuide.detail('내 종목별 금액과 비중 보기'" not in HTML
    performance = portfolio.split('id="account-performance-card"', 1)[1].split('</section>', 1)[0]
    assert 'id="account-performance-summary"' in performance.split('<details', 1)[0]
    assert 'id="account-value-chart"' in performance.split('<details', 1)[1]


def test_optional_controls_and_research_remain_available_but_folded():
    portfolio = HTML.split('id="trading-rebal"', 1)[1].split('id="trading-dividend"', 1)[0]
    settings = portfolio.split('<details class="pf-settings">', 1)[1].split('</details>', 1)[0]
    assert all(f'id="{field}"' in settings for field in ('pi-single', 'pi-sector', 'pi-cluster', 'pi-mincash', 'pi-profile-note'))
    assert 'id="pi-cash"' in portfolio.split('<details class="pf-settings">', 1)[0]
    research = portfolio.split('id="pf-tools"', 1)[1]
    assert all(f'id="{field}"' in research for field in ('gp-goal', 'holdings-div-card', 'heatmap-body', 'rebal-result'))
    assert 'if (document.getElementById(\'pf-tools\').open)' in HTML
    assert 'if (document.getElementById(\'pf-evidence\').open)' in HTML
