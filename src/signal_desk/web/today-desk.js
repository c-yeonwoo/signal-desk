/* Candidate presentation only. Engine order, grades and order controls are read-only. */
window.TodayDesk = (() => {
  let sequence = 0, state = null, control = null, market = 'kospi', expanded = false, failed = false;
  const lastSnapshots = {};
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const names = {fundamental:'실적·재무', valuation:'가격 대비 기업가치', flow:'외국인·기관 수급',
    quality:'재무 건전성', momentum:'중기 가격 흐름', technical:'가격 지표', reversion:'최근 낙폭', short:'공매도'};
  const stamp = value => {
    if (!value) return '기준 시각 미확인';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '기준 시각 미확인' : date.toLocaleString('ko-KR', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Seoul'}) + ' 한국시간';
  };
  function describe(row) {
    const view = signalPriceView(row);
    const positive = Object.entries(row.score_contributions || {})
      .filter(([key,value]) => names[key] && typeof value === 'number' && Number.isFinite(value) && value > 0)
      .sort((a,b) => b[1]-a[1]).slice(0,2).map(([key]) => names[key]);
    const reason = positive.length ? `${positive.join('·')} 항목이 점수를 높였습니다.` : '자세한 선정 이유는 종목 근거에서 확인하세요.';
    const risks = {event:'위험 공시가 확인됐습니다.', crash:'급락 조건에 해당합니다.', trend:'하락 추세가 확인됐습니다.', earnings:'실적 발표가 가까워졌습니다.'};
    const risk = (row.gates || []).map(key => risks[key]).find(Boolean);
    const negative = Object.entries(row.score_contributions || {})
      .filter(([key,value]) => names[key] && typeof value === 'number' && Number.isFinite(value) && value < 0)
      .sort((a,b) => a[1]-b[1])[0];
    const caution = !hasSignalPrice(row) ? '판단에 쓸 가격을 확인하지 못했습니다.'
      : view.stale || row.current_decision_status === 'deferred' ? view.message
      : row.low_coverage ? '선정에 필요한 자료가 부족합니다.'
      : risk || (row.earnings_soon ? '실적 발표 일정의 영향을 확인하세요.'
        : negative ? `${names[negative[0]]}은 점수를 낮춘 요인입니다.` : '뉴스·공시의 영향은 종목 근거에서 확인하세요.');
    const live = hasSignalPrice(row) && view.status === 'current_quote' && !view.warning && row.current_decision_eligible === true;
    const reference = hasSignalPrice(row) && view.status === 'current_close' && !view.stale && row.current_decision_status === 'close_reference_only';
    return {reason,caution,live,reference,view};
  }
  function model(data, buyControl) {
    const rows = data?.ready === true && Array.isArray(data.items) ? data.items : [];
    const available = rows.length > 0;
    const usable = rows.filter(row => ['BUY','STRONG_BUY'].includes(row.kind)
      && !row.gate_blocked && !row.decision_buy_blocked && !row.low_coverage)
      .filter(row => {const d=describe(row); return d.live || d.reference;});
    // Use the same final-grade/score order as the existing list. Never fill empty slots with HOLDs.
    const candidates = sortSignalRows(usable, 'signal');
    const live = candidates.filter(row => describe(row).live);
    const references = candidates.filter(row => describe(row).reference);
    const invalid = rows.filter(row => !hasSignalPrice(row) || describe(row).view.stale || row.current_decision_status === 'deferred').length;
    const paused = buyControl?.buy_paused === true;
    const known = typeof buyControl?.buy_paused === 'boolean';
    const closeOnly = rows.some(row => row.current_decision_status === 'close_reference_only')
      && !rows.some(row => describe(row).live);
    const title = !available ? '종목 분석 자료를 기다리고 있습니다'
      : paused ? '봇의 새 매수는 보류 중입니다'
      : !known ? '매수 운영 상태를 확인하지 못했습니다'
      : closeOnly ? '지난 장 마감 기준입니다'
      : live.length ? '장중 가격으로 후보를 확인했습니다'
      : invalid === rows.length && rows.length ? '가격 확인을 기다리고 있습니다'
      : '현재 매수 조건을 통과한 후보가 없습니다';
    const message = !available ? '새 후보가 없는지 판단할 자료가 아직 없습니다. 잠시 뒤 다시 불러와 주세요.'
      : paused ? '아래는 엔진의 후보 평가입니다. 봇의 새 매수는 실행되지 않습니다.'
      : !known ? '종목 평가는 볼 수 있지만, 봇이 새 매수를 실행할 수 있는지는 확인이 필요합니다.'
      : closeOnly ? '다음 장의 가격이 들어오기 전까지 현재 매수 판단으로 사용하지 마세요.'
      : invalid ? `${invalid}종목은 가격을 확인하지 못해 현재 후보에서 제외했습니다.`
      : '신규 진입 후보 평가입니다. 보유 대응은 내 투자에서 확인하세요.';
    return {rows,candidates: live.length ? live : references,invalid,paused,known,closeOnly,title,message};
  }
  function paint() {
    if (!$('today-candidates') || !state) return;
    const focusedTicker=document.activeElement?.getAttribute('data-today-stock');
    const result = model(state,control), reference = !result.candidates.some(row=>describe(row).live);
    const hasRecord=failed && result.rows.length>0;
    $('today-status-title').textContent = failed ? '새 자료를 불러오지 못했습니다' : result.title;
    $('today-status-note').textContent = failed ? hasRecord
      ? '아래는 마지막으로 받은 참고 기록입니다. 다시 불러온 뒤 현재 판단을 확인하세요.'
      : '후보 조회에 실패했습니다. 다시 불러오기를 눌러 확인해 주세요.' : result.message;
    $('today-status').dataset.tone = failed || result.paused || !result.known || result.invalid ? 'wait' : 'neutral';
    $('today-candidate-title').textContent = failed ? hasRecord ? '마지막으로 받은 후보 기록' : '후보 조회 실패' : reference && result.candidates.length ? '지난 종가로 살펴볼 후보' : '오늘 살펴볼 후보';
    $('today-candidate-count').textContent = result.candidates.length ? `${result.candidates.length}개` : '';
    const shown = expanded ? result.candidates : result.candidates.slice(0,3);
    $('today-candidates').innerHTML = shown.map(row => {
      const d=describe(row), change=typeof row.change_pct === 'number' && Number.isFinite(row.change_pct) ? row.change_pct : null;
      const grade=failed ? '지난 후보 기록' : d.reference ? '종가 참고 후보' : result.paused ? '엔진 후보 · 새 매수 보류' : !result.known ? '엔진 후보 · 운영 상태 미확인' : '매수 후보';
      return `<li class="today-candidate"><div class="today-candidate-main"><div><h3>${escape(row.name || row.ticker)}</h3><p class="today-stock-meta">${escape(row.ticker)}${row.sector ? ' · '+escape(row.sector) : ''}</p></div>
        <div class="today-candidate-price"><strong>${escape(signalPriceText(row))}</strong><span class="${change < 0 ? 'negative' : change > 0 ? 'positive' : ''}">${change == null ? '등락 미확인' : (change>0?'+':'')+change.toFixed(2)+'%'}</span></div></div>
        <p class="today-candidate-grade">${escape(grade)}</p><dl class="today-reasons"><div><dt>이유</dt><dd>${escape(d.reason)}</dd></div><div><dt>주의</dt><dd>${escape(d.caution)}</dd></div></dl>
        <div class="today-candidate-foot"><span>${escape(d.reference ? (row.signal_close_date || '날짜 미확인')+' 종가' : stamp(row.signal_quote_received_at))}</span>
        <button type="button" class="linkish" data-today-stock="${escape(row.ticker)}">차트와 근거 보기 <span aria-hidden="true">→</span></button></div></li>`;
    }).join('');
    if (!shown.length) $('today-candidates').innerHTML = `<li class="today-empty"><h3>${failed ? '후보를 불러오지 못했습니다' : !result.rows.length ? '종목 자료가 아직 없습니다' : result.invalid === result.rows.length ? '가격을 확인한 뒤 후보를 보여드리겠습니다' : '지금은 새 후보가 없습니다'}</h3><p>${failed ? '후보가 없는지 판단할 수 없습니다. 다시 불러온 뒤 확인하세요.' : !result.rows.length ? '자료가 준비된 뒤 다시 불러와 주세요.' : result.invalid ? '가격 확인이 필요한 종목은 후보로 올리지 않습니다.' : '조건을 통과한 종목이 생기면 여기에 표시합니다.'}</p></li>`;
    const more=$('today-more'); more.hidden=result.candidates.length<=3;
    more.textContent=expanded ? '상위 3개만 보기' : `후보 ${result.candidates.length}개 모두 보기`;
    $('today-retry').hidden = !failed && state.ready === true && result.rows.length>0 && result.invalid===0 && result.known;
    let restored=false;
    $('today-candidates').querySelectorAll('[data-today-stock]').forEach(button => {
      button.addEventListener('click',()=>{
        gotoSignal(button.dataset.todayStock,market === 'us'?'us':'kr');
        document.getElementById('signal-chart-title')?.focus({preventScroll:true});
      });
      if(focusedTicker && button.dataset.todayStock===focusedTicker){button.focus({preventScroll:true});restored=true;}
    });
    if(focusedTicker && !restored)$('today-retry').hidden ? $('today-status-title').focus({preventScroll:true}) : $('today-retry').focus({preventScroll:true});
  }
  async function load(nextMarket) {
    market=nextMarket; const seq=++sequence; expanded=false; state=null; control=null; failed=false;
    $('today-status-title').textContent='가격과 매수 운영 상태를 확인하고 있습니다';
    $('today-status-note').textContent='확인된 후보부터 보여드립니다.';
    $('today-candidate-title').textContent='오늘 살펴볼 후보'; $('today-candidate-count').textContent='';
    $('today-candidates').innerHTML='<li class="today-loading" role="status">후보를 확인하고 있습니다…</li>';
    $('today-more').hidden=true; $('today-retry').hidden=true;
    const previous=lastSnapshots[nextMarket] || _sigCache[nextMarket];
    const current=()=>seq===sequence && _sigMarket===nextMarket;
    const read=async url=>{
      const abort=new AbortController(), timeout=setTimeout(()=>abort.abort(),12000);
      try {const response=await fetch(url,{cache:'no-store',signal:abort.signal});
        if(!response.ok)throw new Error('unavailable'); return await response.json();
      } finally {clearTimeout(timeout);}
    };
    // A slow control request must not hold back the candidate list. Until it resolves, its status stays unknown.
    await Promise.allSettled([
      read('/api/signals?market='+nextMarket).then(data=>{
        if(!current())return;
        if(!Array.isArray(data?.items))throw new Error('invalid list');
        state=data;lastSnapshots[nextMarket]={d:state,ts:Date.now()};paint();
      }).catch(()=>{if(current()){failed=true;state=previous?.d || {ready:false,items:[]};paint();}}),
      read('/api/ledger/state?style=balanced&market='+(nextMarket==='us'?'us':'kr')).then(data=>{
        if(current()){control=data;paint();}
      }).catch(()=>{if(current()){control=null;paint();}}),
    ]);
  }
  function refresh() { if(document.getElementById('view-today')?.style.display!=='none')load(_sigMarket); }
  function showMore() {expanded=!expanded;paint();$('today-more')?.focus({preventScroll:true});}
  function leave() {++sequence;}
  // Re-evaluate quote expiry while this page remains open; no new engine/LLM execution.
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refresh();});
  setInterval(()=>{if(document.visibilityState==='visible' && $('view-today')?.style.display!=='none')paint();},30000);
  const marketSlot=$('today-market-slot'), ribbon=$('market-ribbon');
  if(marketSlot && ribbon)marketSlot.appendChild(ribbon);
  document.addEventListener('DOMContentLoaded',()=>{if(sequence===0 && $('view-today')?.style.display!=='none')load(_sigMarket);});
  return {load,refresh,showMore,leave,describe,model};
})();
