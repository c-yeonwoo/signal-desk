"""PD4 uses the shipped renderer, including error and cross-market races."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_market_summary_related_stocks_and_freshness():
    if not shutil.which("node"):
        pytest.skip("Node required for actual UI contracts")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8'),els={},pending=[],nav=[];
const el=id=>els[id] ||= {textContent:'',innerHTML:'',hidden:false,classList:{add(){},toggle(){}},
  replaceChildren(){this.innerHTML='';this.textContent=''},removeAttribute(k){delete this[k]}};
const ctx=vm.createContext({document:{getElementById:el},URL,Date,Number,
  esc:v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  fetch:url=>new Promise(resolve=>pending.push({url,resolve})),gotoSignal:(...x)=>nav.push(x),setMarketBriefImage(){}});
vm.runInContext("let _sigMarket='kr',_marketBriefSeq=0,_effBuyThreshold,_adaptBump,_adaptReasons;",ctx);
vm.runInContext(html.slice(html.indexOf('function openMarketBriefStock('),html.indexOf('let _marketBriefSeq =')),ctx);
vm.runInContext(html.slice(html.indexOf('async function loadRegime(){'),html.indexOf('// ③ 신뢰 스트립')),ctx);
const data={market:'kr',status:'ready',price_as_of:'2026-10-16',expected_as_of:'2026-10-16',
  universe_count:200,price_count:200,daily_coverage:{available:195,analyzed:200},today_headline:'상승이 많았어요',
  summary:'오늘 오른 종목이 많지만 최근 평균 아래인 종목도 많아요.',
  movers:[{ticker:'005930',name:'삼성<전자>',change_pct:3.1,sector:'반도체'}],
  selection:{buy_count:3,strong_buy_count:1,computed_at:'2026-10-16T08:00:00Z'},
  facts:[],unknown:[],brief_news:[{name:'삼성전자',title:'실적 발표',source:'DART',published:'2026-10-16',url:'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=123'}]};
const answer=(i,d,ok=true)=>pending[i].resolve({ok,json:async()=>d});
(async()=>{
 const first=ctx.loadRegime();answer(0,data);await first;
 assert.equal(el('mb-basis').textContent,data.summary);
 assert.match(el('mb-coverage').textContent,/200종목.*195종목/);
 assert.match(el('mb-movers').innerHTML,/삼성&lt;전자&gt;/);
 assert.match(el('mb-movers').innerHTML,/data-market="kr"/);
 assert.match(el('mb-news-note').textContent,/2026-10-16.*원인/);
 assert.equal(el('mb-retry').hidden,true);
 ctx.openMarketBriefStock({dataset:{ticker:'005930',market:'kr'}});
 ctx.openMarketBriefStock({dataset:{ticker:"');alert(1)",market:'kr'}});assert.equal(nav.length,1);
 // Old KR response may not paint US data. Starting a new read clears source/news.
 const old=ctx.loadRegime();vm.runInContext("_sigMarket='us'",ctx);const current=ctx.loadRegime();
 assert.equal(el('mb-news-link').hidden,true);
 answer(2,{...data,market:'us',universe_count:509,price_count:509,movers:[{ticker:'BRK.B',name:'Berkshire',change_pct:-2.5}],brief_news:[]});await current;
 answer(1,data);await old;assert.match(el('mb-movers').innerHTML,/BRK.B/);
 assert.match(el('mb-movers').innerHTML,/data-market="us"/);
 assert.match(el('mb-news-note').textContent,/없다는 뜻은 아닙니다/);
 // Stale response cannot retain a past actionable related list or old source link.
 const stale=ctx.loadRegime();answer(3,{...data,status:'stale'});await stale;
 assert.doesNotMatch(el('mb-movers').innerHTML,/data-ticker/);assert.equal(el('mb-news-link').hidden,true);
 assert.equal(el('mb-retry').hidden,false);
 const failed=ctx.loadRegime();answer(4,{},false);await failed;
 assert.match(el('mb-state').textContent,/불러오지 못/);assert.equal(el('mb-retry').hidden,false);
 // Raw URLs with credentials must not become source links.
 ctx.renderMarketBriefRelated({...data,brief_news:[{url:'https://user:secret@example.com/a'}]});
 assert.equal(el('mb-news-link').hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1], check=True)
