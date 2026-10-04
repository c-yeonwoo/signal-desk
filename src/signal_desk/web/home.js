/* Read-only first-screen summary. Every amount is scoped to its own evidence source. */
window.InvestmentHome = (() => {
  let requestId = 0;
  let chart = null;
  let lastChange = null;
  const $ = id => document.getElementById(id);
  const label = {conservative:'안정형', balanced:'균형형', aggressive:'공격형'};
  const fmt = v => Number(v).toLocaleString('ko-KR', {minimumFractionDigits:2, maximumFractionDigits:2});
  const percent = v => v == null || !Number.isFinite(Number(v)) ? '—' : `${Number(v) > 0 ? '+' : ''}${fmt(v)}%`;
  const date = ts => ts ? new Date(Number(ts) * 1000).toLocaleString('ko-KR', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '미확인';
  async function read(url) {
    try { const r = await fetch(url, {cache:'no-store'}); return r.ok ? await r.json() : null; }
    catch { return null; }
  }
  function put(id, value) { const el = $(id); if (el) el.textContent = value; }
  function plot(points, benchmark) {
    const holder = $('home-paper-chart');
    if (!holder || !window.echarts || !Array.isArray(points) || points.length < 2) {
      if (chart) chart.clear();
      return;
    }
    const base = Number(points[0].total_eval);
    if (!Number.isFinite(base) || base <= 0) { if (chart) chart.clear(); return; }
    const aligned = Array.isArray(benchmark) && benchmark.length === points.length
      && points.every((p,i)=>p.date === benchmark[i].date && Number.isFinite(Number(benchmark[i].total_eval)));
    const botSeries = points.map(p=>Number(p.total_eval) / base * 100);
    const benchSeries = aligned ? benchmark.map(p=>Number(p.total_eval) * 100) : null;
    const series = [{type:'line',name:'봇 모의투자',data:botSeries,showSymbol:false,smooth:false,
      lineStyle:{color:'#243CDB',width:2},areaStyle:{color:'#243CDB',opacity:.07}}];
    if (benchSeries) series.push({type:'line',name:'당시 종목 동일비중',data:benchSeries,showSymbol:false,smooth:false,
      lineStyle:{color:'#949aa6',width:1.5,type:'dashed'}});
    chart = window.echarts.getInstanceByDom(holder) || window.echarts.init(holder);
    chart.setOption({animation:false, grid:{left:5,right:5,top:7,bottom:16},
      tooltip:{trigger:'axis',formatter:p=>`${p[0].axisValue}<br>${p.map(x=>`${x.seriesName} ${fmt(x.data)} (기록 첫날=100)`).join('<br>')}`},
      xAxis:{type:'category',data:points.map(p=>p.date),boundaryGap:false,axisLine:{show:false},axisTick:{show:false},axisLabel:{show:false}},
      yAxis:{type:'value',scale:true,show:false},
      series},true);
    chart.resize();
    return aligned;
  }
  function action(d) {
    if (!d || !d.ready) {
      put('home-action-title','내 보유 입력·진단부터');
      put('home-action-why','저장된 진단이 없어 오늘의 개인별 매매 행동을 판단할 수 없습니다.');
      put('home-action-meta','주문 제안 없음 · 먼저 보유와 현금을 확인하세요.');
      put('home-asof','진단 가격 기준시각 없음');
      put('home-next','보유종목과 현금을 입력한 뒤 진단해 주세요.');
      return;
    }
    const g = (d.guidance || [])[0];
    const quality = d.data_quality === 'complete' ? '필요한 자료 확인됨' : '자료 일부 부족';
    const prices = d.price_asof_range;
    const priceStamp = prices ? (prices.first === prices.last ? prices.last : `${prices.first}~${prices.last}`) : '미확인';
    put('home-asof',`보유 가격 기준 ${priceStamp} · 진단 ${d.as_of || '미확인'} · ${quality}`);
    if (!g) {
      put('home-action-title','추가 행동 판단 보류');
      put('home-action-why','저장된 진단에 행동 항목이 없습니다. 새 입력으로 다시 확인하세요.');
    } else {
      const old = d.as_of && d.as_of < new Date(Date.now() - 4 * 86400000).toISOString().slice(0,10);
      put('home-action-title',`${old ? '지난 진단 · ' : ''}${g.action || '검토 필요'}`);
      put('home-action-why',g.reason || '상세 진단의 근거를 확인하세요.');
    }
    const cur = g && g.current_pct != null ? `현재 ${g.current_pct}%` : '현재 비중 미확인';
    const cap = g && g.limit_pct != null ? `한도 ${g.limit_pct}%` : '제안 비중·금액 미산출';
    put('home-action-meta',`${cur} · ${cap} · ${d.as_of || '시점 미확인'} 기준 · 주문 아님`);
    put('home-next','보유종목·가격·설정 한도가 바뀌면 다시 진단해 주세요.');
  }
  function updateChange(d) {
    lastChange = d;
    if (!$('investment-home')) return;
    if (window._homeMarket === 'us') { put('home-change','해외 시그널 일별 변화 요약은 아직 제공하지 않습니다. 국내 변화와 섞지 않습니다.'); return; }
    if (!d || !d.ready) { put('home-change','시장 시그널 변화 · 이전 거래 세션과 비교할 자료가 아직 없습니다.'); return; }
    put('home-change',d.quiet
      ? `시장 시그널 변화 · ${d.prev_date} → ${d.cur_date}: 등급 변경 없음`
      : `시장 시그널 변화 · ${d.prev_date} → ${d.cur_date}: 등급 변경 ${d.changes_total || 0}건. 아래에서 원인 확인`);
  }
  async function load({market='kospi', profile={}}={}) {
    const seq = ++requestId, mkt = market === 'us' ? 'us' : 'kr', currency = mkt === 'us' ? 'USD' : 'KRW';
    window._homeMarket = mkt;
    put('home-market',mkt === 'us' ? '해외 · USD' : '국내 · KRW');
    put('home-account','실계좌 확인 중…'); put('home-copy','추종 설정 확인 중…');
    put('home-paper-return','—'); put('home-real-return','—');
    const realSub = $('home-real-return')?.previousElementSibling?.querySelector('small');
    if (realSub) realSub.textContent = '당일 변동 참고값 · 계좌 전체 수익률 아님';
    put('home-action-title','내 보유 상태를 확인하는 중');
    updateChange(lastChange);
    if (chart) chart.clear();
    const tasks = [];
    tasks.push(read(`/api/portfolio/latest?market=${mkt}`).then(d=>{if(seq===requestId) action(d);}));
    tasks.push(read('/api/my-broker-account').then(d=>{
      if (seq !== requestId) return;
      put('home-account', d && d.ready ? '실계좌 · 토스 조회 확인 (주문 아님)'
        : d && d.account_verified ? '실계좌 · 확인됨, 상세 조회 실패'
        : d ? '실계좌 · 연결 확인 필요' : '실계좌 · 이 계정에서 조회 불가');
    }));
    tasks.push(read(`/api/my-performance?market=${mkt}`).then(d=>{
      if (seq !== requestId) return;
      const p = d && Array.isArray(d.points) ? d.points.at(-1) : null;
      put('home-real-return',p ? percent(p.daily_return_pct) : '관측 없음');
      if (p) {
        const sub = $('home-real-return').previousElementSibling?.querySelector('small');
        if (sub) sub.textContent = `보유주식 당일 참고율 · ${date(p.observed_at)} 관측 · 계좌 수익률 아님`;
      }
    }));
    tasks.push((async()=>{
      const policy = await read('/api/live/copy-policy');
      if (seq !== requestId) return;
      const style = policy && label[policy.source_style] ? policy.source_style
        : label[profile['투자성향']] ? profile['투자성향'] : 'balanced';
      const transmission = policy?.order_transmission_enabled === true
        ? '주문 전송 허용 설정(실제 주문 전 별도 검증 필요)' : '실주문 꺼짐';
      put('home-copy',policy
        ? `실주문 설정(KIS) ${transmission} · ${label[style]} 봇 주문의 ${policy.follow_pct}% 따라가기${policy.configured ? '' : ' (저장 전)'}`
        : `실주문 설정을 확인할 수 없습니다 · ${label[style]} 모의투자만 표시`);
      const paper = await read(`/api/reference-performance?market=${mkt}`);
      if (seq !== requestId) return;
      const bot = paper && (paper.bots || []).find(b=>b.style===style);
      const hold = bot && bot.holding && bot.holding.median_days != null
        ? ` · 보유 중위 ${bot.holding.median_days}달력일` : '';
      put('home-paper-sub',`${label[style]} · ${currency} · 초기자금 대비 · 모의 장부${hold}`);
      put('home-paper-return',bot ? percent(bot.return_pct) : '자료 없음');
      const comparable = plot(bot && (bot.comparison_curve || bot.curve), bot && bot.benchmark_curve);
      put('home-chart-caption',bot && (bot.curve || []).length >= 2
        ? `${comparable ? '비교 시작일' : '기록 첫날'}=100 · 가장 큰 하락폭 ${percent(bot.max_drawdown_pct)} · ${comparable ? '당시 종목 동일비중 비교(점선, 비용 전)' : '비교할 기간의 자료 없음'}`
        : '기록된 자산 경로가 아직 부족합니다.');
    })());
    await Promise.all(tasks);
  }
  return {load, updateChange};
})();
