/* Stored-close thumbnails. No signal calculation, extrapolation or quote substitution. */
(function () {
  const cache=new Map(), pending=new Map(), queue=[], watched=new Set();
  let active=0, observer=null, show=true;
  try {show=localStorage.getItem('sd_mini_charts')!=='off';} catch {}
  const TTL=300000, CAP=64;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const validDate=value=>typeof value==='string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && !Number.isNaN(Date.parse(value+'T00:00:00Z'))
    && new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;
  function geometry(data,expectedClose) {
    if(data?.ready!==true || data.reference_only!==true || data.basis!=='stored_close'
      || !Array.isArray(data.dates) || !Array.isArray(data.close) || data.dates.length!==data.close.length)return null;
    const dates=data.dates.slice(-20),values=data.close.slice(-20);
    if(dates.length<2 || dates.some((date,i)=>!validDate(date) || (i>0 && date<=dates[i-1]))
      || data.as_of!==dates.at(-1))return null;
    const prices=values.map(value=>typeof value==='number' && Number.isFinite(value) && value>0?value:null);
    const available=prices.filter(value=>value!==null);
    if(available.length<2)return null;
    const min=Math.min(...available),max=Math.max(...available),range=max-min;
    let path='',gap=true;
    prices.forEach((price,i)=>{
      if(price===null){gap=true;return;}
      const x=3+i/(prices.length-1)*134,y=range===0?23:43-(price-min)/range*40;
      path+=(gap?'M':'L')+x.toFixed(2)+' '+y.toFixed(2)+' ';gap=false;
    });
    const asof=dates.at(-1),expected=data.expected_as_of;
    if(validDate(expected) && asof>expected)return null;
    const consistent=validDate(expected) && asof===expected && (!expectedClose || asof===expectedClose);
    const missing=available.length!==prices.length;
    const label=(consistent?'':'기준 확인 필요 · ')+asof.slice(5)+'까지 '+prices.length+'개 종가'+(missing?' · 결측 있음':'');
    return {path:path.trim(),label,asof,consistent,missing};
  }
  function markup(ticker,market,expectedClose) {
    return `<span class="mini-chart" data-mini-ticker="${escape(ticker)}" data-mini-market="${market==='us'?'us':'kospi'}" data-mini-close="${escape(expectedClose||'')}"><span class="mini-chart-note">종가 흐름 확인 중</span></span>`;
  }
  function draw(element,data) {
    const chart=geometry(data,element.dataset.miniClose);
    if(!chart){element.innerHTML='<span class="mini-chart-note">종가 차트 확인 불가</span>';return;}
    element.classList.toggle('mini-chart-unverified',!chart.consistent);
    element.innerHTML=`<svg viewBox="0 0 140 46" role="img" aria-label="${escape(chart.label+' · 확정 종가 흐름, 실시간 가격 아님')}"><path d="${chart.path}" fill="none" stroke="currentColor" stroke-width="1.7" vector-effect="non-scaling-stroke"/></svg><span class="mini-chart-note">${escape(chart.label)}</span>`;
  }
  async function read(market,ticker) {
    const key=market+':'+ticker,old=cache.get(key);
    if(old && Date.now()-old.time<(old.data?TTL:30000))return old.data;
    if(pending.has(key))return pending.get(key);
    const request=(async()=>{
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
      let data=null;
      try {
        const response=await fetch('/api/signals/'+encodeURIComponent(ticker)+'/chart?market='+market+'&preview=true',{signal:controller.signal});
        if(response.ok)data=await response.json();
      } catch {} finally {clearTimeout(timer);}
      cache.delete(key);cache.set(key,{time:Date.now(),data});
      while(cache.size>CAP)cache.delete(cache.keys().next().value);
      return data;
    })();
    pending.set(key,request);
    try{return await request;}finally{pending.delete(key);}
  }
  function pump() {
    while(active<2 && queue.length){
      const element=queue.shift();
      if(!element.isConnected)continue;
      if(observer){
        const rect=element.getBoundingClientRect();
        if(rect.bottom<0 || rect.top>innerHeight){watched.add(element);observer.observe(element);continue;}
      }
      active++;
      read(element.dataset.miniMarket,element.dataset.miniTicker).then(data=>{
        if(element.isConnected)draw(element,data);
      }).finally(()=>{active--;pump();});
    }
  }
  function scan(container) {
    if(!container)return;
    watched.forEach(element=>{if(!element.isConnected){observer?.unobserve(element);watched.delete(element);}});
    const elements=[...container.querySelectorAll('[data-mini-ticker]')];
    if(!observer && typeof IntersectionObserver!=='undefined')observer=new IntersectionObserver(entries=>{
      entries.forEach(entry=>{if(entry.isIntersecting){observer.unobserve(entry.target);watched.delete(entry.target);queue.push(entry.target);}});pump();
    });
    elements.forEach((element,index)=>{
      if(element.dataset.miniObserved)return;
      element.dataset.miniObserved='true';
      if(observer){watched.add(element);observer.observe(element);}
      else if(index<3)queue.push(element);
      else element.innerHTML='<span class="mini-chart-note">상세에서 종가 확인</span>';
    });
    pump();
  }
  function enabled(){return show;}
  function toggle(){
    const next=show=!show;try{localStorage.setItem('sd_mini_charts',next?'on':'off');}catch{}
    const button=document.getElementById('sig-mini-toggle');if(button)button.setAttribute('aria-pressed',String(next));
    renderSignalList();button?.focus({preventScroll:true});
  }
  window.MiniCharts = {geometry,markup,scan,enabled,toggle};
})();
