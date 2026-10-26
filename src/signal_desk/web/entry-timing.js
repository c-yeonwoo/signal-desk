/* Read-only archived research. No broker, collection, or order requests. */
(() => {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = (v, suffix = '') => typeof v === 'number' && Number.isFinite(v)
    ? `${v > 0 ? '+' : ''}${v.toFixed(2)}${suffix}` : '계산 보류';
  const usd = v => typeof v === 'number' && Number.isFinite(v)
    ? v.toLocaleString('en-US', {style:'currency', currency:'USD'}) : '미확인';
  const color = v => v > 0 ? 'et-positive' : v < 0 ? 'et-negative' : '';
  const verdict = {blocked_sources:'자료 부족 · 판정 보류', not_adopted_performance:'채택하지 않음',
    needs_independent_evaluation:'별도 표본 검증 필요'};
  const status = {closed:'청산 완료', no_order:'주문 없음', no_affordable_whole_share:'수량 부족',
    limit_not_marketable:'지정가 초과 · 미진입', reference_missing:'기준 호가 없음', entry_missing:'진입 호가 없음',
    exit_missing_position_unresolved:'청산 호가 없음 · 보유 미해결', blocked_by_unresolved_prior_execution:'앞선 거래 미해결'};
  let chart = null, busy = false;
  const timestamp = ns => typeof ns === 'number' ? escape(new Date(ns / 1e6).toLocaleString('ko-KR',
    {timeZone:'America/New_York', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false})) : '없음';
  function curve(path) {
    let pnl = 0, unresolved = false;
    return [0, ...path.rows.map(row => {
      if (row.net_pnl === null || unresolved) { unresolved = true; return null; }
      pnl += row.net_pnl;
      return pnl / 100; // $10,000 initial cash, account percentage points.
    })];
  }
  function render(data, holder) {
    const expanded = [...holder.querySelectorAll('details')].map(d => d.open);
    if (chart) { chart.dispose(); chart = null; }
    if (data.status !== 'available') {
      holder.innerHTML = `<p>${data.status === 'not_available' ? '아직 배포된 연구 결과가 없습니다.' : '연구 결과 파일을 확인하지 못했습니다. 새로고침 후에도 같으면 배포 상태를 확인해 주세요.'}</p>`;
      return;
    }
    const early = data.paths['immediate:base'], opening = data.paths['opening:base'];
    const methods = ['immediate','opening'];
    const pair = (label, fn) => `<tr><th scope="row">${label}</th>${methods.map(m => '<td>' + fn(data.paths[m + ':base'], m) + '</td>').join('')}</tr>`;
    const cases = early.rows.map(row => {
      const other = opening.rows.find(r => r.id === row.id);
      const cell = r => r ? `<td><span class="${color(r.trade_net_return_pct)}">${num(r.trade_net_return_pct, '%')}</span><small>${escape(status[r.status] || '상태 확인 필요')}</small><span class="et-time">판단 ${timestamp(r.decision_ns)}<br>진입 ${timestamp(r.entry_ns)}<br>청산 ${timestamp(r.exit_ns)}</span><small>손익 ${usd(r.net_pnl)} · 비용 ${usd(r.fees)}</small></td>` : '<td>사건 없음</td>';
      return `<tr><th scope="row">${escape(row.issuer)}<small>${escape(row.id.split(':')[1])}</small></th>${cell(row)}${cell(other)}</tr>`;
    }).join('');
    const conclusion = methods.every(m => data.verdicts[m] === 'not_adopted_performance')
      ? '두 방식 모두 성과 기준을 넘지 못해 채택하지 않았습니다.' : '아직 실전 전략으로 채택하지 않았습니다.';
    holder.innerHTML = `<p class="et-summary">${data.eligible_events}건 비교. ${conclusion}</p>
      <p><small>연구 실행 ${escape(new Date(data.generated_at).toLocaleString('ko-KR'))} · 2025년 상반기 미국 실적 발표 · 전체 ${data.cohort_events}건 중 기존 조건에 맞는 ${data.eligible_events}건</small></p>
      <table><caption>같은 초기 자금 $10,000 · 비용 후 결과</caption><thead><tr><th scope="col">비교 항목</th><th scope="col">발표 직후<small>기사 확인 10분 뒤</small></th><th scope="col">첫 정규장<small>개장 5분 뒤</small></th></tr></thead><tbody>
      ${pair('계좌 수익률', p => `<span class="et-return ${color(p.conditional_account_return_pct)}">${num(p.conditional_account_return_pct, '%')}</span>`)}
      ${pair('판정', (_, m) => escape(verdict[data.verdicts[m]] || '판정 확인 필요'))}
      ${pair('완료 거래', p => `${p.closed_trades} / ${p.selected_events}건`)}
      ${pair('SPY 비교', (_, m) => num(data.paths[m + ':market'].conditional_account_return_pct, '%'))}
      ${pair('비용 2배', (_, m) => num(data.paths[m + ':double_cost'].conditional_account_return_pct, '%'))}
      ${pair('총 가정 비용', p => usd(p.closed_subset_assumed_fees) + (!p.complete ? '<small>완료 거래만</small>' : ''))}
      </tbody></table>
      <div class="et-chart" id="entry-timing-chart" role="img" aria-label="사건 순서별 비용 후 누적 계좌 수익률. 같은 사건 순서로 비교하며 현금 기준은 0%입니다."></div>
      <p><small>가로축은 날짜가 아닌 같은 사건의 순서입니다. 미해결 거래 뒤의 선은 그리지 않습니다. 현금 보유 기준 0%.</small></p>
      <details><summary>사건별 판단·진입·청산 보기</summary><p>시각은 미국 동부시간입니다. 사건별 %는 해당 매수금액 대비, 위 계좌 %는 초기 $10,000 대비입니다.</p><table class="et-cases"><caption>동일 사건의 비용 후 거래 수익률</caption><thead><tr><th scope="col">종목·발표일</th><th scope="col">발표 직후</th><th scope="col">첫 정규장</th></tr></thead><tbody>${cases}</tbody></table></details>
      <details><summary>비교 조건·자료 부족·주의점</summary><p>매수·매도마다 0.12% 가정 비용을 뺐습니다. 당시 계좌의 확정 정산비용은 아닙니다. ask로 사고 bid로 파는 가격 차이도 반영했습니다. 사건마다 현금의 1/6 이내, 정수 주식, 실제 진입 후 60분 보유입니다.</p>
      <p>이미 본 소수 표본입니다. 기사의 최초 버전·당시 API 도착 시각·실제 체결 보장은 확인하지 못했습니다. 과거 호가 수량은 raw size ×100 해석을 사용했습니다. 가격이 많이 오른다는 보장이나 운영 봇 성적이 아닙니다.</p>
      <p>보존 호가 구간 ${data.source_windows}개 · 원문 ${(data.source_raw_bytes / 1e6).toFixed(2)}MB · 조건 미확인 행 ${data.unknown_condition_rows}개. 청산 시점 사이의 평가손실은 누적선에 포함되지 않습니다.</p>
      <table><caption>추가 스트레스 · 계좌 수익률</caption><tbody><tr><th scope="row">1분 더 늦게 진입</th>${methods.map(m => '<td>' + num(data.paths[m + ':delay60'].conditional_account_return_pct, '%') + '</td>').join('')}</tr><tr><th scope="row">절반 수량</th>${methods.map(m => '<td>' + num(data.paths[m + ':half_size'].conditional_account_return_pct, '%') + '</td>').join('')}</tr></tbody></table>
      ${methods.map(m => `<p>${m === 'immediate' ? '발표 직후' : '첫 정규장'} 미완료: ${escape(data.paths[m + ':base'].unresolved_executions.join(', ') || '없음')}</p>`).join('')}
      <p>새 연구 결과를 배포할 때 바뀌는 요약입니다. 이 화면의 새로고침은 수집·백테스트·주문을 실행하지 않습니다.</p></details>`;
    holder.querySelectorAll('details').forEach((d, i) => { d.open = expanded[i] || false; });
    if (window.echarts) {
      chart = window.echarts.init(document.getElementById('entry-timing-chart'));
      const css = getComputedStyle(holder);
      chart.setOption({animation:false, color:[css.getPropertyValue('--accent').trim(), css.getPropertyValue('--dim').trim()],
        legend:{data:['발표 직후','첫 정규장']}, tooltip:{trigger:'axis', valueFormatter:v => num(v,'%')},
        grid:{left:52, right:16, top:48, bottom:40},
        xAxis:{type:'category', boundaryGap:false, data:['시작', ...early.rows.map((r,i) => `${i+1}. ${r.issuer}`)]},
        yAxis:{type:'value', axisLabel:{formatter:'{value}%'}, splitLine:{lineStyle:{color:css.getPropertyValue('--line').trim()}}},
        series:methods.map((m,i) => ({name:i ? '첫 정규장':'발표 직후', type:'line', step:'end', connectNulls:false,
          data:curve(data.paths[m+':base']), lineStyle:{type:i ? 'dashed':'solid'}, markLine:i ? undefined : {silent:true, symbol:'none', label:{show:false}, data:[{yAxis:0}]}}))});
    } else {
      document.getElementById('entry-timing-chart').textContent = '그래프를 불러오지 못했습니다. 위 표에서 수익률을 확인할 수 있습니다.';
    }
  }
  async function load() {
    const holder = document.getElementById('entry-timing-body');
    if (!holder || busy) return;
    busy = true;
    const button = document.getElementById('entry-timing-refresh');
    button.disabled = true; holder.setAttribute('aria-busy','true');
    try {
      const response = await fetch('/api/admin/research/entry-timing', {cache:'no-store', signal:AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error('request_failed');
      render(await response.json(), holder);
    } catch (_) {
      if (chart) { chart.dispose(); chart = null; }
      holder.innerHTML = '<p class="et-error" role="alert">연구 결과를 불러오지 못했습니다. 로그인 상태를 확인하고 결과 새로고침을 눌러 주세요.</p>';
    } finally {
      busy = false; button.disabled = false; holder.removeAttribute('aria-busy');
    }
  }
  window.loadEntryTiming = load;
  window.addEventListener('resize', () => chart?.resize());
  setInterval(() => {
    const panel = document.getElementById('admin-verify');
    if (!document.hidden && location.hash === '#admin/verify' && panel && panel.getClientRects().length
        && !document.querySelector('#entry-timing-body details[open]')) load();
  }, 60000);
})();
