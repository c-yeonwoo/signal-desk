/* Read-only explanations. The ledger/analysis APIs remain the source of truth. */
(function () {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const valid = v => typeof v === 'number' && Number.isFinite(v);
  const num = (v, n = 1) => valid(v) ? v.toLocaleString('ko-KR', {maximumFractionDigits:n, minimumFractionDigits:n}) : '확인 필요';
  const money = (v, currency) => valid(v) ? (currency === 'USD' ? '$' : '') + num(v, currency === 'USD' ? 2 : 0) + (currency === 'USD' ? '' : '원') : '확인 필요';
  const amount = (label, v, currency, signed = true) => `<div><span>${label}</span><strong class="${signed && v < 0 ? 'neg' : signed && v > 0 ? 'pos' : ''}">${money(v, currency)}</strong></div>`;
  const detail = (title, body) => `<details class="invest-detail"><summary>${title}</summary><div>${body}</div></details>`;

  function review(d) {
    const total = d.total_eval, seed = d.seed_cash;
    const ready = valid(total) && valid(seed) && seed > 0;
    const pnl = ready ? total - seed : null;
    const open = valid(d.unrealized_pnl) ? d.unrealized_pnl : valid(d.pnl) ? d.pnl : null;
    // Never fill unknown components with zero, or subtract fees a second time.
    const realized = ready && valid(open) ? pnl - open : null;
    const recovery = ready && total > 0 ? Math.max(0, (seed / total - 1) * 100) : null;
    const driver = !ready || !valid(open) ? 'unknown' : pnl > 0 ? 'positive' : pnl === 0 ? 'flat'
      : realized === open ? 'mixed' : Math.max(0, -realized) > Math.max(0, -open) ? 'realized' : 'open';
    return {ready, pnl, open, realized, recovery, driver};
  }

  function renderReview(d) {
    const r = review(d), currency = d.currency;
    const titles = {unknown:'손익을 설명할 자료가 부족해요', positive:'지금까지의 성과를 지키는 점검',
      flat:'현재 계좌 전체 손익은 본전이에요', mixed:'손실이 과거 거래와 현재 보유에 나뉘어 있어요',
      realized:'손실은 주로 이미 끝난 거래에서 발생했어요', open:'손실은 주로 지금 보유한 종목에 남아 있어요'};
    const first = {
      unknown:['장부부터 확인하기', '초기자금·현재 자산·보유 손익을 확인한 뒤 계획을 다시 계산합니다. 빠진 값을 0원으로 보지 않습니다.'],
      flat:['서로 상쇄된 손익이 있는지 확인하기', '전체 손익이 0이어도 판 종목과 보유 종목의 손익은 다를 수 있습니다. 아직 거래가 없는지도 아래 기록에서 확인합니다.'],
      mixed:['과거 거래와 현재 보유를 함께 점검하기', '확정된 손실과 보유 중인 손실의 크기가 같습니다. 반복 매매 내역과 현재 보유 비중을 함께 확인합니다.'],
      positive:['좋은 결과도 같은 기준으로 확인하기', '이익이 일부 종목이나 짧은 기간에 몰렸는지 확인합니다. 수익이 났다는 이유만으로 매수 규모를 늘리지는 않습니다.'],
      realized:['반복된 진입·매도부터 검토하기', '아래 거래내역에서 손실 제한 매도 후 재매수, 잦은 종목 교체가 있었는지 살펴봅니다. 현재 보유분의 반등만으로 과거 손실이 사라지지는 않습니다.'],
      open:['현재 보유의 하락 위험부터 확인하기', '보유종목의 손익과 비중을 함께 봅니다. 비슷하게 움직이는 종목에 몰렸는지, 처음 산 이유가 여전히 유효한지 확인합니다.'],
    }[r.driver];
    const hasPositions = (d.positions || []).length > 0;
    const steps = [first,
      ['개선안은 모의투자에서 먼저 비교하기', r.driver === 'realized'
        ? '재진입을 늦추거나 거래 횟수를 줄이는 안을 기존 규칙과 같은 기간·비용 조건으로 비교합니다. 아직 더 낫다고 검증된 변경안은 아닙니다.'
        : '종목당 비중을 낮추는 안을 기존 규칙과 같은 기간·비용 조건으로 비교합니다. 손실 감소뿐 아니라 놓친 수익도 함께 확인합니다.'],
      ['다음 거래일 마감 후 다시 확인하기', hasPositions
        ? '전체 손익·보유 손익·현금 비중이 어떻게 바뀌었는지 확인합니다. 개선안의 비용 후 성과와 최대 손실이 함께 좋아지는지 검증한 뒤 채택 여부를 판단합니다.'
        : '현재는 보유종목이 없습니다. 손실을 만회하려고 진입하지 않고, 새 매수 조건이 확인되는지 다음 거래일 마감 후 살펴봅니다.']];
    if (r.driver === 'unknown') steps.splice(1);
    return `<div class="invest-kicker">${esc(d.label || '선택한 봇')} · ${currency === 'USD' ? '해외' : '국내'} 모의투자</div>
      <h2>${titles[r.driver]}</h2>
      <p class="invest-muted">처음 넣은 가상자금 ${money(d.seed_cash, currency)} → 현재 ${money(d.total_eval, currency)}</p>
      <div class="invest-breakdown">${amount('계좌 전체 손익' + (r.ready ? ' · ' + num(r.pnl / d.seed_cash * 100, 2) + '%' : ''), r.pnl, currency)}${amount('팔아서 확정된 손익', r.realized, currency)}${amount('아직 보유 중인 손익', r.open, currency)}</div>
      <p class="invest-muted">전체 손익 = 확정된 손익 + 보유 중인 손익. 장부에 반영된 거래비용을 다시 빼지 않습니다. 평가액은 장부 기준이며 실시간 증권사 잔고가 아닙니다.</p>
      <div id="bot-cost-review" class="invest-muted">거래비용 기록을 확인하는 중…</div>
      ${r.ready && r.pnl < 0 ? `<div class="invest-recovery"><b>초기자금까지 남은 거리</b><strong>${valid(r.recovery) ? '+' + num(r.recovery, 2) + '%' : '현재 자산으로 계산 불가'}</strong><span>입출금 없이 현재 총자산이 늘어나야 하는 비율입니다. 예상 수익률이나 회복 기한이 아닙니다.</span></div>` : ''}
      <h3>${r.ready && r.pnl < 0 ? '손실 회복을 위한 재점검 계획' : '다음 점검 계획'}</h3>
      <ol class="invest-plan">${steps.map(([title, text]) => `<li><b>${title}</b><p>${text}</p></li>`).join('')}</ol>
      ${detail('왜 공격형의 손실이 더 클 수 있나요?', '<p>같은 종목이 하락해도 더 큰 비중으로 샀다면 계좌 손실이 커집니다. 하지만 성향별 매수 시점·종목 교체·보유기간도 달라서 비중만으로 원인을 단정할 수 없습니다.</p><p>이 화면은 손익이 어디에 남아 있는지 설명합니다. 시장 하락, 진입 타이밍, 거래비용 중 무엇이 얼마나 기여했는지는 동일 기간의 시장 비교와 전체 체결 분석이 추가로 필요합니다.</p>')}
      <p class="invest-muted">장부에 맞춰 자동으로 만든 검토 계획입니다. 봇 설정·실계좌 주문은 변경하지 않습니다.</p>`;
  }

  function renderCosts(d) {
    const c = d?.costs;
    if (!c || !valid(c.trades) || !valid(c.cost_recorded_trades) || !valid(c.total_execution_cost))
      return '거래비용 자료를 불러오지 못해 비용의 영향을 판단할 수 없습니다.';
    const full = c.trades === c.cost_recorded_trades;
    return `<b>거래하며 든 비용</b> ${money(c.total_execution_cost, d.currency)} · 전체 ${num(c.trades,0)}건 중 ${num(c.cost_recorded_trades,0)}건 기록
      <br>${full ? '기록된 수수료와 예상 가격보다 불리하게 체결된 차이의 합계입니다.' : '비용이 기록되지 않은 과거 거래가 있어 전체 비용은 알 수 없습니다. 미기록 비용을 0원으로 계산하지 않습니다.'}
      전체 손익에서 이 금액을 또 빼지는 마세요.`;
  }

  function explain(g, holdings = []) {
    const names = (g.tickers || []).map(t => holdings.find(h => h.ticker === t)?.name || t).join(' · ');
    const current = num(g.current_pct), limit = num(g.limit_pct);
    switch (g.kind) {
      case 'input': return ['내가 가진 종목부터 알려주세요', '종목·수량·평균 매입가격을 입력하면 내 자산의 구성을 설명할 수 있어요.', '입력한 뒤 다시 분석하세요.'];
      case 'data_quality': return ['자료를 보완해야 정확히 말할 수 있어요', g.reason, '가격·과거시세가 확보된 뒤 재분석하세요. 지금 결과만으로 매매량을 정하지 마세요.'];
      case 'concentration': return [`${g.name || g.ticker}에 돈이 많이 몰려 있어요`, `전체 자산의 ${current}%를 차지해요. 내가 정한 한도는 ${limit}%예요. 이 종목이 하락하면 계좌 전체에 미치는 영향도 커져요.`, '추가 매수 전에 이 비중을 유지할 이유부터 확인하세요. 아래 비중 조정 예시와 비교해 보세요.'];
      case 'sector': return [`${g.sector || '한 업종'}에 투자가 몰려 있어요`, `같은 업종이 전체 자산의 ${current}%예요. 설정 한도 ${limit}%를 넘어요. 종목이 달라도 같은 뉴스에 함께 하락할 수 있어요.`, '종목 수보다 업종이 얼마나 나뉘어 있는지 먼저 확인하세요.'];
      case 'correlation': return ['여러 종목이지만 함께 움직이는 편이에요', `${names}의 합계 비중은 ${current}%로 설정 한도 ${limit}%를 넘어요. 과거 가격 움직임이 비슷해 분산 효과가 작을 수 있어요.`, '이 묶음을 하나의 투자처럼 보고, 새 종목도 같은 방향으로 움직이는지 확인하세요.'];
      case 'liquidity': return ['남겨둔 현금이 내 기준보다 적어요', `현재 현금 ${current}%, 내가 정한 최소 현금 ${limit}%예요. 추가 매수나 예상 밖 지출에 쓸 여유가 줄어들어요.`, '추가 매수 전에 필요한 현금을 먼저 확보할 수 있는지 확인하세요.'];
      case 'monitor': return ['설정한 비중·현금 한도 안에 있어요', '확인된 자료에서 한도 초과는 발견되지 않았어요. 손실 위험이 없거나 지금 매수하기 좋다는 뜻은 아니에요.', '보유가 바뀌거나 다음 거래일 가격이 갱신되면 다시 확인하세요.'];
      default: return [g.action || '추가 확인이 필요해요', g.reason || '설명 자료가 없습니다.', '아래 상세 근거를 확인하세요.'];
    }
  }

  function renderPortfolio(d) {
    const q = d.data_quality || {}, s = d.summary || {}, holdings = d.holdings || [];
    const incomplete = q.status !== 'complete';
    const guidance = [...(d.guidance || [])].sort((a,b) =>
      ({blocker:0,high:1,medium:2,normal:3}[a.priority] ?? 4) - ({blocker:0,high:1,medium:2,normal:3}[b.priority] ?? 4));
    const cards = guidance.map((g, i) => {
      const [title, why, next] = explain(g, holdings);
      return `<article class="invest-guidance"><span class="invest-kicker">${i === 0 ? '먼저 확인' : '함께 확인'} ${i+1}</span><h3>${esc(title)}</h3><p>${esc(why)}</p><p class="invest-next"><b>그래서 지금은</b> ${esc(next)}</p></article>`;
    });
    return `<section class="invest-portfolio"><div class="invest-kicker">내 포트폴리오 · 쉬운 해설</div><h2>${incomplete ? '확인된 내용부터 차근차근 볼게요' : '내 돈이 어디에 모여 있는지 볼게요'}</h2>
      <p class="invest-muted">분석 기준 ${esc(d.as_of || '날짜 확인 필요')} · ${incomplete ? '자료가 일부 부족해요. 금액·비중은 확인된 자료 기준이며 결과가 달라질 수 있어요.' : '필요한 자료가 확보됐어요. 미래 수익을 예측하는 분석은 아니에요.'}</p>
      <div class="invest-breakdown">${amount(incomplete ? '확인된 자산' : '분석한 자산', s.total_value, d.currency, false)}${amount('그중 주식', s.invested_value, d.currency, false)}${amount('남겨둔 현금', s.cash, d.currency, false)}</div>
      ${cards.slice(0, 3).join('') || '<p>보유종목과 현금을 입력한 뒤 분석해 주세요.</p>'}
      ${cards.length > 3 ? detail(`함께 확인할 내용 ${cards.length - 3}개 더 보기`, cards.slice(3).join('')) : ''}
      ${d.allocation?.ready ? '<p class="invest-muted">아래 비중 조정은 입력한 한도에 맞춘 계산 예시이며, 주문으로 이어지지 않아요.</p>' : ''}</section>`;
  }
  window.InvestmentGuide = {review, renderReview, renderCosts, explain, renderPortfolio, detail};
})();
