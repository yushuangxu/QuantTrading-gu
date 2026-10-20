/* Render server-computed breadth only. Never sum quotes or recalculate bins. */
(function () {
  'use strict';
  const root = document.getElementById('breadth');
  if (!root) return;
  const el = id => document.getElementById(id);
  const esc = value => String(value == null ? '—' : value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const count = value => numeric(value) ? value.toLocaleString('en-US',{maximumFractionDigits:0}) : '—';
  const pct = value => numeric(value) ? value.toFixed(2)+'%' : '—';
  const at = value => value && Number.isFinite(Date.parse(value)) ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value)) : '—';
  let pending = false, previous = null;
  const charts = new Map();
  const COLORS = {up:'#cf1322',down:'#389e0d',flat:'#bfbfbf'};

  function clearCharts(message) {
    charts.forEach(chart=>chart.dispose());charts.clear();
    ['breadthDirections','breadthDistribution'].forEach(id=>{
      el(id).innerHTML='<div class="chart-placeholder"><div class="empty-icon" aria-hidden="true"></div><span>'+esc(message)+'</span></div>';
    });
  }
  function renderCharts(data) {
    if (!data.stats || !data.stats.valid_count) { clearCharts('暂无可核验的有效涨跌样本'); return; }
    if (!window.echarts) { clearCharts('图表组件未加载，以上统计仍可查看'); return; }
    ['breadthDirections','breadthDistribution'].forEach(id=>{
      if (!charts.has(id)) { el(id).replaceChildren();charts.set(id,window.echarts.init(el(id))); }
    });
    const directions=data.direction_distribution || [];
    charts.get('breadthDirections').setOption({animation:false,
      tooltip:{trigger:'item',backgroundColor:'#fff',borderColor:'#f0f0f0',textStyle:{color:'#262626'},formatter:p=>esc(p.data.name)+'：'+count(p.data.value)+'只（'+pct(p.data.serverPct)+'）'},
      legend:{bottom:0,textStyle:{color:'#8c8c8c',fontSize:11}},
      graphic:[{type:'text',left:'center',top:'34%',style:{text:count(data.stats.valid_count),fontSize:25,fontWeight:600,fill:'#262626'}},{type:'text',left:'center',top:'50%',style:{text:'有效样本',fontSize:10,fill:'#8c8c8c'}}],
      series:[{type:'pie',radius:['49%','68%'],center:['50%','44%'],label:{show:false},itemStyle:{borderColor:'#fff',borderWidth:3},data:directions.map(d=>({name:d.label,value:d.count,serverPct:d.pct,itemStyle:{color:COLORS[d.key] || '#bfbfbf'}}))}]
    },true);
    const bins=data.change_distribution || [];
    charts.get('breadthDistribution').setOption({animation:false,
      grid:{left:42,right:8,top:28,bottom:42},
      tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#f0f0f0',textStyle:{color:'#262626'},formatter:params=>params.map(p=>esc(p.name)+'：'+count(p.value)+'只').join('<br>')},
      xAxis:{type:'category',data:bins.map(d=>d.label),axisLine:{lineStyle:{color:'#f0f0f0'}},axisTick:{show:false},axisLabel:{color:'#8c8c8c',fontSize:9,interval:0}},
      yAxis:{type:'value',minInterval:1,axisLabel:{color:'#8c8c8c',fontSize:10},splitLine:{lineStyle:{color:'#f0f0f0',type:'dashed'}}},
      series:[{type:'bar',barMaxWidth:42,label:{show:true,position:'top',fontSize:10,color:'#595959'},data:bins.map(d=>({value:d.count,itemStyle:{color:['lt_neg5','neg5_neg3','neg3_zero'].includes(d.key)?COLORS.down:d.key==='flat'?COLORS.flat:COLORS.up,borderRadius:[3,3,0,0]}}))}]
    },true);
  }
  function render(data) {
    const s=data.stats, q=data.quality || {}, coverage=data.coverage || {};
    const verified=coverage.verified_complete===true && numeric(s?.all_a_amount_cny);
    const status=el('breadthStatus');
    const states={unavailable:['获取失败','red'],stale:['历史 / 过期','orange'],unknown:['新鲜度未知','orange'],loading:['获取中','gray'],degraded:['样本 · 覆盖未核实','orange'],fresh:['有效快照','green']};
    const [label,tone]=states[data.availability] || ['未核验','gray'];
    status.className='tag '+tone;status.textContent=label;
    el('breadthUp').textContent=count(s?.up);el('breadthDown').textContent=count(s?.down);el('breadthFlat').textContent=count(s?.flat);
    el('breadthAdvance').textContent=pct(s?.advance_pct);
    el('breadthDenominator').textContent='有效涨跌幅样本 '+count(s?.valid_count)+' 只';
    el('breadthAmountLabel').textContent=verified?'全A成交额':'样本成交额（未核实全覆盖）';
    const amount=verified?s.all_a_amount_cny:s?.observed_amount_cny;
    el('breadthAmount').textContent=numeric(amount)?(amount/1e8).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
    el('breadthRatio').textContent='上涨 / 下跌 '+(s?.up_down_text || '—');
    el('breadthThresholds').textContent='涨幅 > 3%：'+count(s?.up_gt3)+'　 > 5%：'+count(s?.up_gt5)+'　│　 跌幅 < -3%：'+count(s?.down_lt_neg3)+'　 < -5%：'+count(s?.down_lt_neg5);
    const field=(name,value)=>'<div><div class="fact-label">'+esc(name)+'</div><p>'+value+'</p></div>';
    const exchange=q.by_exchange || {};
    el('breadthQuality').innerHTML=
      field('行情日期 / 完成状态','<strong>'+esc(data.trade_date)+'</strong> · '+(data.snapshot_kind==='intraday'?'盘中批量快照':'完成状态未知')+'<br>非认证日终，非原子采集')+
      field('最近成功采集 · 北京时间',esc(at(data.fetched_at))+'<br>最近请求 '+esc(at(data.last_request_at)))+
      field('A股基准与股票覆盖率',numeric(coverage.quote_coverage_pct)?pct(coverage.quote_coverage_pct)+' · 基准 '+count(coverage.reference_count)+' 只':'<strong>覆盖率未知</strong> · 缺少独立基准名单')+
      field('实际样本 / 交易所分布','A股记录 '+count(q.a_share_count)+' · 有效涨跌幅 '+count(q.valid_change_count)+'<br>沪 '+count(exchange.SH)+' / 深 '+count(exchange.SZ)+' / 北 '+count(exchange.BJ))+
      field('来源时间范围（北京时间）',esc(at(data.quote_time_min))+'<br>'+esc(at(data.quote_time_max)))+
      field('数据源 / 成交额口径','<a href="https://quote.eastmoney.com/center/gridlist.html#hs_a_board" target="_blank" rel="noopener">东方财富 · 沪深京A股批量行情 ↗</a><br>成交额原单位：元；已去重；缺额 '+count(q.amount_missing_count)+' 只');
    const alert=el('breadthAlert');
    let title='按返回A股样本统计，尚未核实完整市场覆盖', body=(data.warnings || []).join('\n'), alertTone='warning';
    if (data.availability==='unavailable') { title='暂未取得可核验的全A快照';body='不展示部分页统计，不以指数、自选股或合成行情替代。原始错误和已采集范围见下方诊断。';alertTone='error'; }
    else if (data.availability==='loading') { title='已有一次批量获取在进行';body='请稍后刷新；不会重复发起第二次全市场请求。';alertTone='info'; }
    else if (data.availability==='stale') { title='当前为历史 / 降级样本，不代表此刻全A市场'; }
    else if (verified && data.availability==='fresh') { title='同日期完整基准名单已对比，统计仅对应所示快照时段';alertTone='info'; }
    alert.className='alert '+alertTone;
    alert.innerHTML='<span class="alert-icon">'+(alertTone==='info'?'i':'!')+'</span><div><strong>'+esc(title)+'</strong><p>'+esc(body || '请核对采集时段、基准名单及有效分母，不据此生成买卖或仓位建议。')+'</p></div>';
    const diagnostic={source:data.source,source_url:data.source_url,date_basis:data.date_basis,availability:data.availability,freshness:data.freshness,
      error:data.error,failure_details:data.failure_details,quality:data.quality,coverage:data.coverage,
      amount_change:data.stats?.amount_change_reason || '未获得可比两日数据，不估算变化'};
    el('breadthDiagnostics').innerHTML='<pre>'+esc(JSON.stringify(diagnostic,null,2))+'</pre>';
    el('breadthStats').setAttribute('aria-busy',String(data.availability==='loading'));
    renderCharts(data);
  }
  async function load() {
    if (pending) return;
    pending=true;el('breadthRefresh').disabled=true;el('breadthRefresh').textContent='获取中';
    el('breadthStats').setAttribute('aria-busy','true');
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),35000);
    try {
      const response=await fetch(root.dataset.api,{signal:controller.signal,headers:{Accept:'application/json'}});
      if (!response.ok) throw new Error('HTTP '+response.status);
      const data=await response.json();previous=data;render(data);
    } catch(error) {
      render({...previous,stats:previous?.stats || null,availability:previous?.stats?'stale':'unavailable',
        error:'页面请求失败：'+(error.name==='AbortError'?'超时':error.message),
        warnings:['本次页面请求失败；如保留旧值，仅代表上次获取时点，不更新成功采集时间。']});
    } finally {clearTimeout(timer);pending=false;el('breadthRefresh').disabled=false;el('breadthRefresh').textContent='更新广度';}
  }
  el('breadthRefresh').addEventListener('click',load);
  el('refreshButton').addEventListener('click',load);
  if (typeof ResizeObserver!=='undefined') new ResizeObserver(()=>charts.forEach(chart=>chart.resize())).observe(root);
  setInterval(()=>{if(!document.hidden)load();},60000);
  load();
})();
