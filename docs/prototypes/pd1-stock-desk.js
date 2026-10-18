'use strict';
// Deliberate fixture-only UI. No production requests, storage, or orders.
const fixtures = [
  {ticker:'002380',name:'KCC',market:'kr',sector:'건자재',price:428000,change:3.01,kind:'강력매수',rank:0,score:1.94,coverage:88,runup:-5.1,firstPrice:451000,why:'재무와 가격 수준이 후보 평가를 뒷받침합니다.',caution:'다음 장중 가격과 거래량을 아직 반영하지 않았습니다.',factors:[['재무',0.85],['저평가',0.82],['수급',0.16],['퀄리티',0.60],['모멘텀',0.70]],note:'종가 기준'},
  {ticker:'000660',name:'SK하이닉스',market:'kr',sector:'반도체',price:200000,change:-1.20,kind:'매수',rank:1,score:1.81,coverage:88,why:'기존 점수에서 매수 후보로 분류된 예시입니다.',caution:'주가 하락만으로 추가매수를 판단할 수 없습니다.',factors:[['재무',0.7],['저평가',0.4]],note:'종가 기준'},
  {ticker:'267250',name:'HD현대',market:'kr',sector:'지주',price:180000,change:-2.17,kind:'관망',rank:2,score:2.45,coverage:88,why:'점수는 높지만 매수 후보 조건을 통과하지 못한 예시입니다.',caution:'가격 추세 조건 때문에 신규 매수를 보류한 상태입니다.',factors:[['재무',0.8],['저평가',0.7]],note:'추세 확인 필요'},
  {ticker:'322000',name:'HD현대에너지솔루션',market:'kr',sector:'전력·유틸리티',price:40000,change:2.30,kind:'관망',rank:2,score:3.00,coverage:24,why:'점수 계산에 필요한 자료가 부족합니다.',caution:'높은 점수를 매수 근거로 읽으면 안 되는 예시입니다.',factors:[['재무',null],['저평가',null]],note:'계산 자료 부족'},
  {ticker:'005930',name:'삼성전자',market:'kr',sector:'반도체',price:75000,change:0.53,kind:'관망',rank:2,score:0.85,coverage:100,why:'현재 예시 조건에서는 매수 후보가 아닙니다.',caution:'뚜렷한 새 매매 판단 근거가 없는 상태입니다.',factors:[['재무',0.6],['저평가',0.2]],note:'종가 기준'},
  {ticker:'005380',name:'현대차',market:'kr',sector:'자동차',price:240000,change:-0.82,kind:'관망',rank:2,score:0.89,coverage:88,why:'기존 후보 점수와 보유 대응을 구별하는 예시입니다.',caution:'보유 중이라면 내 매입가와 비중을 함께 확인해야 합니다.',factors:[['재무',0.6],['저평가',0.5]],note:'보유 진단과 별개'},
  {ticker:'LYB',name:'LyondellBasell',market:'us',sector:'소재',price:64.01,change:2.14,kind:'강력매수',rank:0,score:2.64,coverage:100,runup:6.7,firstPrice:60.01,why:'재무와 가격 수준이 후보 평가를 뒷받침하는 예시입니다.',caution:'최근 기업 원문은 연결하지 않은 예시입니다.',factors:[['재무',0.85],['저평가',0.79]],note:'종가 기준'},
  {ticker:'DAL',name:'Delta Air Lines',market:'us',sector:'산업재',price:50.00,change:1.10,kind:'관망',rank:2,score:2.34,coverage:100,why:'점수와 최종 후보 판정이 다른 예시입니다.',caution:'가격 추세 조건이 충족되지 않은 상태입니다.',factors:[['재무',0.8],['모멘텀',0.1]],note:'추세 확인 필요'},
  {ticker:'EIX',name:'Edison International',market:'us',sector:'유틸리티',price:60.00,change:-0.65,kind:'관망',rank:2,score:1.87,coverage:100,why:'현재 예시 조건에서는 매수 후보가 아닙니다.',caution:'후보 순위와 보유 여부를 따로 살펴봐야 합니다.',factors:[['재무',0.4],['모멘텀',0.2]],note:'후보 조건 미충족'}
];
const state={market:'kr',query:'',sort:'signal',selected:'002380',data:'closed',favorites:new Set(['000660','267250'])};
const $=id=>document.getElementById(id);
const esc=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=item=>item.market==='us'?'$'+item.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):item.price.toLocaleString('ko-KR');
const delta=n=>(n>0?'+':'')+n.toFixed(2)+'%';
const starSvg='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3l-5.6 2.9 1.1-6.2L3 9.6l6.2-.9Z"/></svg>';
function visibleItems(){
  const q=state.query.toLocaleLowerCase('ko-KR');
  return fixtures.filter(x=>(state.market==='watch'?state.favorites.has(x.ticker):x.market===state.market)&&(!q||x.name.toLocaleLowerCase('ko-KR').includes(q)||x.ticker.toLocaleLowerCase('en-US').includes(q)))
    .sort((a,b)=>state.sort==='name'?a.name.localeCompare(b.name,'ko'):state.sort==='score'?b.score-a.score||a.ticker.localeCompare(b.ticker):a.rank-b.rank||b.score-a.score||a.ticker.localeCompare(b.ticker));
}
function kindClass(item){return state.data!=='closed'?'':item.rank<2?'buy':'';}
function grade(item){return state.data==='closed'?item.kind:state.data==='stale'?'지난 '+item.kind:'현재 판정 보류';}
function row(item){
  const favorite=state.favorites.has(item.ticker), numeric=['closed','stale'].includes(state.data);
  return `<li class="stock-row${state.selected===item.ticker?' selected':''}"><button class="star-button" type="button" data-favorite="${esc(item.ticker)}" aria-label="${esc(item.name)} 관심 ${favorite?'해제':'추가'}" aria-pressed="${favorite}">${starSvg}</button><button class="stock-open" type="button" data-open="${esc(item.ticker)}" aria-pressed="${state.selected===item.ticker}"><span class="row-top"><span class="row-name">${esc(item.name)}</span><span class="row-price">${numeric?money(item)+(item.market==='kr'?'원':''):'—'}</span></span><span class="row-middle"><span>${esc(item.ticker)} · ${esc(item.sector)}</span><span class="row-change ${numeric?(item.change>=0?'up':'down'):''}">${numeric?delta(item.change):'가격 확인 필요'}</span></span><span class="row-bottom"><span class="kind ${kindClass(item)}">${esc(grade(item))}</span><span class="row-reason">${state.data==='stale'?'갱신 지연':esc(item.note)}</span></span></button></li>`;
}
function chart(item){
  const shape=[1.054,1.046,1.062,1.050,1.038,1.039,1.018,1.030,1.010,1.019,1.007,1.014,1.000];
  const values=shape.map(x=>item.price*x),min=Math.min(...values)*.985,max=Math.max(...values)*1.01;
  const coords=values.map((v,i)=>`${46+i*(570/(values.length-1))},${175-(v-min)/(max-min)*135}`).join(' ');
  return `<section class="price-chart" aria-labelledby="price-chart-title"><div class="section-heading"><h3 id="price-chart-title">가격 흐름</h3><span>예시 차트 · 실제 시세 아님</span></div><svg class="chart-surface" viewBox="0 0 640 190" preserveAspectRatio="none" role="img" aria-label="${esc(item.name)}의 가상 가격 흐름. 실제 시세 데이터가 아닌 배치 예시입니다."><path class="grid-line" d="M46 40H620 M46 100H620 M46 175H620"/><polyline class="price-line" points="${coords}"/></svg><div class="chart-axis" aria-hidden="true"><span>기간 시작</span><span>종가 예시</span></div><p class="chart-caption">가격 변화와 후보 등급은 서로 다른 정보입니다.</p></section>`;
}
function unavailable(){
  if(state.data==='loading')return '<div class="status-message" role="status"><h3>가격을 확인하고 있어요</h3><p>새 가격을 받기 전에는 판정을 표시하지 않습니다.</p><div class="loading-placeholder" aria-hidden="true"></div></div>';
  if(state.data==='error')return '<div class="status-message" role="alert"><h3>가격을 불러오지 못했어요</h3><p>판정을 표시할 수 없습니다. 잠시 뒤 다시 확인해 주세요.</p><button type="button" class="small-button" data-retry>다시 불러오기 · 예시</button></div>';
  return '<div class="status-message" role="status"><h3>저장된 가격이 없어요</h3><p>가격이 확인될 때까지 판정을 보류합니다.</p></div>';
}
function renderDetail(){
  const item=fixtures.find(x=>x.ticker===state.selected),panel=$('stock-detail');
  if(!item){panel.innerHTML='<div class="detail-empty"><h2>살펴볼 종목을 선택하세요</h2><p>목록에서 고르면 가격과 판정 근거를 볼 수 있습니다.</p></div>';return;}
  const favorite=state.favorites.has(item.ticker),numeric=['closed','stale'].includes(state.data);
  const identity=`<button type="button" class="mobile-back" data-back><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5-7 7 7 7"/></svg>목록으로</button><div class="identity-line"><div><h2 id="detail-name" tabindex="-1">${esc(item.name)}</h2><p class="identity-meta">${esc(item.ticker)} · ${esc(item.sector)} · ${item.market==='kr'?'국내':'미국'}</p></div><button type="button" class="detail-star" data-favorite="${esc(item.ticker)}" aria-pressed="${favorite}" aria-label="${esc(item.name)} 관심 ${favorite?'해제':'추가'}">${favorite?'관심 해제':'관심 추가'}</button></div>`;
  if(!numeric){panel.innerHTML=identity+unavailable();return;}
  const time=item.market==='kr'?'2026. 10. 16. 국내 종가':'2026. 10. 16. 미국 현지 종가';
  const warning=state.data==='stale'?'<p class="warning-line" role="note">가격 갱신이 지연됐습니다. 아래 금액과 등급은 지난 기록이며 현재 판정으로 쓰지 마세요.</p>':'';
  const explanation=state.data==='stale'?'새 가격으로 판정을 확인할 때까지 기다려 주세요.':item.rank<2?'새 매수 후보로 분류된 예시입니다. 보유 중인 주식의 매매 지시와는 다릅니다.':'매수 후보 조건을 충족하지 않은 예시입니다.';
  panel.innerHTML=identity+warning+`<div class="quote ${state.data==='stale'?'weak-price':''}"><strong>${money(item)}${item.market==='kr'?'<span class="unit">원</span>':''}</strong><span class="change ${item.change>=0?'up':'down'}">${delta(item.change)}</span></div><p class="price-time">${state.data==='stale'?'이전 기록 · ':''}${time} · 화면 예시</p>
    <div class="decision-line"><span class="decision-label kind ${kindClass(item)}">${esc(grade(item))}</span><span class="decision-caption">${state.data==='closed'?'종가를 기준으로 한 후보 평가':'현재 판정 아님'}</span><p>${explanation}</p>${item.runup!==undefined?`<p class="record-change ${item.runup<0?'down':''}">기록상 첫 매수권 뒤 가격 ${item.runup>0?'+':''}${item.runup.toFixed(1)}% · 실제 매입 손익 아님</p>`:''}<a href="#decision-evidence">판정 근거 보기</a></div>
    ${chart(item)}
    <section class="evidence" id="decision-evidence" aria-labelledby="evidence-title"><h3 id="evidence-title">왜 이렇게 봤나요?</h3><p class="evidence-intro">${state.data==='stale'?'아래는 지난 판정의 예시 근거입니다.':'예시 근거를 가격의 움직임과 나란히 살펴보세요.'}</p><div class="reason-grid"><div><h4>${item.rank<2?'좋게 본 점':'현재 상태'}</h4><p>${esc(item.why)}</p></div><div><h4>먼저 확인할 점</h4><p>${esc(item.caution)}</p></div></div><div class="source-summary"><h4>회사 관련 원문</h4><p>연결된 최근 공시·기사 없음 · 예시 상태</p><p>원문이 없다는 것이 악재나 소식이 전혀 없다는 뜻은 아닙니다.</p></div></section>
    <section class="holding-note" aria-labelledby="holding-title"><h3 id="holding-title">이미 보유하고 있다면</h3><p>신규 후보 등급만으로 추가매수나 매도를 정하지 않습니다. 내 매입가와 비중을 함께 확인해야 합니다.</p><a href="#holding-demo">보유 진단 연결은 어떻게 되나요?</a><details id="holding-demo" class="advanced"><summary>내 보유 진단 안내</summary><p>실제 구현에서는 내 투자 화면의 해당 종목 진단으로 이어집니다. 이 시안은 계좌와 보유 여부를 조회하지 않습니다.</p></details></section>
    <details class="advanced"><summary>점수와 계산 자료</summary><p>예시 점수 ${item.score>0?'+':''}${item.score.toFixed(2)} · 계산에 쓴 관점의 비중 ${item.coverage}%. 성공 확률이나 기사 검증률이 아닙니다.</p><table class="factor-table"><thead><tr><th scope="col">관점</th><th scope="col">계산값 · 예시</th></tr></thead><tbody>${item.factors.map(([key,value])=>`<tr><td>${esc(key)}</td><td>${value===null?'자료 없음':(value>0?'+':'')+value.toFixed(2)}</td></tr>`).join('')}</tbody></table></details>
    <details class="advanced"><summary>판정 기록의 의미</summary><p>매수권 첫 기록과 종가의 가격 차이는 실제 체결 손익이 아닙니다. 기록이 없는 날의 판정까지 이어졌다고 가정하지 않습니다. 차트는 이 시안에서 만든 가상 가격 흐름입니다.</p></details>`;
}
function render(){
  const items=visibleItems();
  if(state.selected&&!items.some(x=>x.ticker===state.selected)){state.selected=null;document.body.dataset.detailOpen='false';}
  document.querySelectorAll('[data-market]').forEach(e=>e.setAttribute('aria-pressed',String(e.dataset.market===state.market)));
  $('watch-count').textContent=state.favorites.size;
  $('result-count').textContent=`예시 ${items.length}종목`;
  $('stock-list').innerHTML=['loading','error'].includes(state.data)?'':items.map(row).join('');
  if(state.data==='loading')$('list-status').innerHTML='<div class="list-loading">가격을 확인하고 있어요.</div>';
  else if(state.data==='error')$('list-status').innerHTML='<div class="list-error">목록 가격을 불러오지 못했어요. <button class="small-button" type="button" data-retry>다시 불러오기 · 예시</button></div>';
  else if(!items.length)$('list-status').innerHTML=`<div class="empty-list"><p>${state.market==='watch'&&!state.favorites.size?'관심종목이 없습니다. 국내·해외 목록에서 관심을 추가해 보세요.':'조건에 맞는 종목이 없습니다.'}</p><button type="button" class="small-button" data-reset>목록 다시 보기</button></div>`;
  else $('list-status').textContent='';
  renderDetail();
}
document.addEventListener('click',event=>{
  const target=event.target.closest('button');if(!target)return;
  if(target.dataset.market){state.market=target.dataset.market;state.selected=null;document.body.dataset.detailOpen='false';render();}
  else if(target.dataset.open){state.selected=target.dataset.open;document.body.dataset.detailOpen='true';render();if(matchMedia('(max-width:760px)').matches)$('detail-name')?.focus();else document.querySelector(`[data-open="${state.selected}"]`)?.focus();}
  else if(target.dataset.favorite){const ticker=target.dataset.favorite,adding=!state.favorites.has(ticker),fromDetail=target.classList.contains('detail-star');adding?state.favorites.add(ticker):state.favorites.delete(ticker);render();$('interaction-status').textContent=`시안 관심종목에서 ${adding?'추가':'해제'}했습니다.`;const buttons=[...document.querySelectorAll('[data-favorite]')].filter(e=>e.dataset.favorite===ticker);(buttons.find(e=>e.classList.contains('detail-star')===fromDetail)||buttons[0]||$('stock-search')).focus();}
  else if(target.hasAttribute('data-back')){document.body.dataset.detailOpen='false';(document.querySelector(`[data-open="${state.selected}"]`)||$('stock-search')).focus();}
  else if(target.hasAttribute('data-reset')){state.query='';$('stock-search').value='';if(state.market==='watch'&&!state.favorites.size)state.market='kr';render();$('stock-search').focus();}
  else if(target.hasAttribute('data-retry')){state.data='closed';$('data-state').value='closed';render();$('interaction-status').textContent='예시 종가 확인 상태로 바뀌었습니다. 실제 요청은 보내지 않았습니다.';if(matchMedia('(max-width:760px)').matches&&document.body.dataset.detailOpen==='true')$('detail-name')?.focus();else (document.querySelector(`[data-open="${state.selected}"]`)||$('stock-search')).focus();}
});
$('stock-search').addEventListener('input',e=>{state.query=e.target.value.trim();render();});
$('stock-sort').addEventListener('change',e=>{state.sort=e.target.value;render();});
$('data-state').addEventListener('change',e=>{state.data=e.target.value;render();});
document.addEventListener('click',event=>{const anchor=event.target.closest('a[href="#holding-demo"]');if(anchor){$('holding-demo').open=true;}});
render();
