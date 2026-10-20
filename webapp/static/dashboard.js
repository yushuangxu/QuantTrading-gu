/* Presentation only: existing API values, no indicator or trend calculations. */
(function () {
  'use strict';
  const QUALITY = {
    fresh: {label: '获取有效', tone: 'green'},
    stale: {label: '历史 / 过期', tone: 'orange'},
    unknown: {label: '待核验', tone: 'orange'},
    unavailable: {label: '获取失败', tone: 'red'}
  };
  const STATES = {closed: '已完成', forming: '临时 · 未完成', unknown: '完成状态未知'};
  const DIRECTIONS = {up: '↑', down: '↓', flat: '→', unknown: ''};
  function escapeText(value) {
    return String(value == null ? '—' : value).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  }
  function numeric(value) { return typeof value === 'number' && Number.isFinite(value); }
  function number(value) { return numeric(value) ? value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '—'; }
  function percent(value) { return numeric(value) ? (value > 0 ? '+' : '') + value.toFixed(2) + '%' : '—'; }
  function priceTone(value) { return numeric(value) ? value > 0 ? 'up' : value < 0 ? 'down' : '' : ''; }
  function quality(value) { return QUALITY[value] || {label:'待获取', tone:'gray'}; }
  function trendTone(value) { return ({'偏强':'red', '震荡':'blue', '偏弱':'green'})[value] || 'gray'; }
  function tag(text, tone) { return '<span class="tag ' + tone + '">' + escapeText(text) + '</span>'; }
  function timestamp(value, timeOnly) {
    if (!value || !Number.isFinite(Date.parse(value))) return '—';
    return new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', ...(timeOnly ? {} : {year:'numeric',month:'2-digit',day:'2-digit'}), hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value));
  }
  function ordered(items, filter, order, specs) {
    const rows = specs.map(spec => ({...spec, ...(items.get(spec.code) || {})}));
    const filtered = rows.filter(d => filter === 'all' || (d.trend || '数据不足') === filter);
    if (order !== 'default') filtered.sort((a,b) => {
      if (!numeric(a.change_pct)) return numeric(b.change_pct) ? 1 : 0;
      if (!numeric(b.change_pct)) return -1;
      return order === 'asc' ? a.change_pct-b.change_pct : b.change_pct-a.change_pct;
    });
    return filtered;
  }
  // Pure presentation helpers can be checked offline without a browser/build tool.
  function errorPresentation(value) {
    if (!value) return {summary:'', detail:''};
    const raw = String(value);
    const detail = raw.replace(/((?:https?|socks5h?):\/\/)[^\s/]+@/gi, '$1[已隐藏凭证]@');
    let summary = '指数行情获取异常，请展开技术诊断。';
    if (/ProxyError|RemoteDisconnected/i.test(raw)) summary = '行情连接中断，请检查安全代理或数据源访问状态。';
    else if (/timeout|超时/i.test(raw)) summary = '指数行情请求超时，请稍后重试。';
    else if (/缓存/.test(raw)) summary = '指数缓存读写异常，请留意数据日期。';
    else if (/IndexDataError|校验|字段|冲突/.test(raw)) summary = '指数数据未通过校验，暂不提供正式分析。';
    return {summary, detail};
  }
  function sourcePresentation(source) {
    if (source === 'tencent/index-daily') return {label:'腾讯财经 · 指数日线',url:'https://gu.qq.com/sh000001/zs'};
    return source === 'sohu/index-daily' ? {label:'搜狐证券 · 指数日线',url:'https://q.stock.sohu.com/zs/000001/lshq.shtml'} :
      {label:'东方财富 · 指数日线',url:'https://akshare.akfamily.xyz/data/index/index.html'};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {escapeText, number, percent, priceTone, quality, trendTone, timestamp, ordered, errorPresentation, sourcePresentation};
  if (typeof document === 'undefined') return;

  const config = JSON.parse(document.getElementById('dashboardConfig').textContent);
  const specs = config.indices;
  const byId = id => document.getElementById(id);
  const items = new Map();
  let selected = specs[0].code, range = 120, busy = false, chart = null;
  const pending = new Set();
  const legacy = new URL(window.location.href); legacy.port = '8090'; legacy.pathname = '/'; legacy.search = ''; legacy.hash = '';
  byId('legacyLink').href = legacy.href;

  async function request(url) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(url, {signal:controller.signal, headers:{Accept:'application/json'}});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return await response.json();
    } finally { clearTimeout(timeout); }
  }

  function renderCards() {
    document.querySelectorAll('[data-index]').forEach(card => {
      const code = card.dataset.index, spec = specs.find(s => s.code === code), d = items.get(code);
      card.classList.toggle('selected', selected === code);
      card.setAttribute('aria-pressed', String(selected === code));
      card.setAttribute('aria-label', spec.name + (d ? '，' + (d.trend || '数据不足') : '，正在加载'));
      if (!d) return;
      const q = quality(d.availability);
      card.innerHTML = '<div class="stat-heading"><span>' + escapeText(spec.name) + '</span><span class="code">' + escapeText(code) + '</span></div>' +
        '<div class="stat-value-row"><span class="stat-value">' + number(d.close) + '</span><span class="stat-change ' + priceTone(d.change_pct) + '">' + percent(d.change_pct) + '</span></div>' +
        '<div class="stat-footer"><span><i class="quality-dot ' + escapeText(d.availability || '') + '"></i>' + escapeText(d.analysis_date ? '正式 ' + d.analysis_date.slice(5) : q.label) + '</span>' + tag(d.trend || '数据不足', trendTone(d.trend)) + '</div>';
    });
    byId('indexCards').setAttribute('aria-busy', String(pending.size > 0));
    const times = [...items.values()].map(d=>d.fetched_at).filter(t=>t && Number.isFinite(Date.parse(t))).sort((a,b)=>Date.parse(b)-Date.parse(a));
    byId('updatedAt').textContent = '数据更新 ' + timestamp(times[0], true);
    byId('updatedAt').title = times.length ? '最近成功获取：' + timestamp(times[0], false) + '（北京时间，逐指数时间见下方）' : '尚无成功获取时间';
  }

  function renderTable() {
    const rows = ordered(items, byId('trendFilter').value, byId('sortOrder').value, specs);
    byId('analysisRows').innerHTML = rows.length ? rows.map(d => {
      const q = quality(d.availability);
      const maCell = n => '<td class="numeric">' + number(d.ma?.['ma'+n]) +
        (n===20 || n===60 ? '<span class="ma-arrow" title="均线方向">' + (DIRECTIONS[d.directions?.['ma'+n]?.direction] || '') + '</span>' : '') + '</td>';
      return '<tr class="' + (d.code === selected ? 'is-selected' : '') + '"><td><button class="index-table-button" data-select="' + escapeText(d.code) + '">' + escapeText(d.name) + '<span class="table-code">' + escapeText(d.code) + '</span></button></td>' +
        '<td class="numeric">' + number(d.close) + '</td><td class="numeric ' + priceTone(d.change_pct) + '">' + percent(d.change_pct) + '</td>' + [5,10,20,60].map(maCell).join('') +
        '<td>' + tag(d.trend || '数据不足', trendTone(d.trend)) + '</td><td class="date-cell">' + escapeText(d.analysis_date) + '<small>' + escapeText(STATES[d.analysis_bar_state] || '无有效日线') + '</small></td><td>' + tag(q.label, q.tone) + '</td></tr>';
    }).join('') : '<tr><td colspan="10" class="table-empty">暂无符合此趋势的指数 · 可切换至“全部趋势”</td></tr>';
    byId('tableCount').textContent = '共 ' + rows.length + ' 个指数';
    byId('changeSort').closest('th').setAttribute('aria-sort', byId('sortOrder').value === 'asc' ? 'ascending' : byId('sortOrder').value === 'desc' ? 'descending' : 'none');
  }

  function emptyChart(title, note, retry) {
    if (chart) { chart.dispose(); chart = null; }
    byId('chartStage').innerHTML = '<div class="chart-placeholder"><div class="empty-icon" aria-hidden="true"></div><div class="empty-title">' + escapeText(title) + '</div><div class="empty-note">' + escapeText(note) + '</div>' + (retry ? '<button class="button" data-retry>重新获取</button>' : '') + '</div>';
  }

  function renderChart() {
    const d = items.get(selected), spec = specs.find(s=>s.code===selected);
    byId('chartTitle').textContent = spec.name + ' · 指数趋势';
    byId('indexSelect').value = selected;
    if (!d) {
      byId('chartSubtitle').textContent = '正在获取日线';
      byId('chartCount').textContent = '等待有效日线';
      if (chart) { chart.dispose(); chart = null; }
      byId('chartStage').innerHTML = '<div class="chart-placeholder"><div class="skeleton skeleton-chart"></div><span>正在加载所选指数…</span></div>';
      return;
    }
    byId('chartSubtitle').textContent = (d.analysis_date ? '正式分析日 ' + d.analysis_date : '尚无正式分析日') + (d.availability === 'stale' ? ' · 历史参考' : '');
    const rows = Array.isArray(d.chart) ? d.chart.slice(-range) : [];
    byId('chartCount').textContent = '分析日成交额 ' + (numeric(d.amount) ? number(d.amount/1e8) : '—') + ' 亿元 · 展示 ' + rows.length + ' / ' + (d.closed_count ?? 0) + ' 根';
    if (!rows.length) {
      emptyChart(d.availability === 'unavailable' ? '暂时无法获取指数数据' : '暂无已完成日线', errorPresentation(d.error).summary || (d.warnings || []).join('；') || '等待有效数据，系统不会使用合成行情替代。', true);
      return;
    }
    if (!window.echarts) { emptyChart('图表组件未加载', '请检查 ECharts CDN 连接。指数数值及均线表格仍可正常查看。', false); return; }
    if (!chart) { byId('chartStage').replaceChildren(); chart = window.echarts.init(byId('chartStage')); }
    const colors = {'close':'#1677ff','ma5':'#73d13d','ma10':'#13c2c2','ma20':'#faad14','ma60':'#722ed1'};
    chart.setOption({
      animation:false,backgroundColor:'#ffffff',
      tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#f0f0f0',textStyle:{color:'#262626',fontSize:12},axisPointer:{type:'line',lineStyle:{color:'#bfbfbf',type:'dashed'}},valueFormatter:v=>number(v)},
      legend:{data:['收盘','MA5','MA10','MA20','MA60'],selected:{'MA5':false,'MA10':false},top:8,right:12,itemWidth:16,itemHeight:3,textStyle:{color:'#8c8c8c',fontSize:11},icon:'roundRect'},
      grid:{left:62,right:24,top:54,bottom:36},
      xAxis:{type:'category',boundaryGap:false,data:rows.map(r=>r.date),axisLine:{lineStyle:{color:'#f0f0f0'}},axisTick:{show:false},axisLabel:{color:'#8c8c8c',fontSize:10,margin:14,formatter:v=>v.slice(5)}},
      yAxis:{type:'value',scale:true,splitNumber:4,axisLine:{show:false},axisTick:{show:false},axisLabel:{color:'#8c8c8c',fontSize:10},splitLine:{lineStyle:{color:'#f0f0f0',type:'dashed'}}},
      series:[['收盘','close'],['MA5','ma5'],['MA10','ma10'],['MA20','ma20'],['MA60','ma60']].map(([name,key])=>({name,type:'line',data:rows.map(r=>r[key] ?? null),connectNulls:false,symbol:'none',lineStyle:{color:colors[key],width:key==='close'?2.3:1.4},itemStyle:{color:colors[key]},...(key==='close'?{areaStyle:{color:'rgba(22,119,255,0.035)'}}:{})}))
    }, true);
  }

  function renderQuality() {
    const d = items.get(selected);
    if (!d) {
      byId('qualityTag').outerHTML = '<span id="qualityTag" class="tag gray">正在加载</span>';
      byId('qualityFacts').innerHTML = '<span class="muted">正在读取所选指数的数据质量信息…</span>';
      byId('qualityAlert').className = 'alert info';
      byId('qualityAlert').innerHTML = '<span class="alert-icon">i</span><div><strong>等待所选指数返回</strong><p>不会沿用其他指数的日期、趋势或数据状态。</p></div>';
      return;
    }
    const q = quality(d.availability), latest = d.latest, source = sourcePresentation(d.source);
    byId('qualityTag').outerHTML = '<span id="qualityTag" class="tag ' + q.tone + '">' + escapeText(q.label) + '</span>';
    const fact = (label,value,sub) => '<div><div class="fact-label">'+label+'</div><div class="fact-value">'+value+(sub?'<small>'+sub+'</small>':'')+'</div></div>';
    byId('qualityFacts').innerHTML =
      fact('正式分析日期',escapeText(d.analysis_date),escapeText(STATES[d.analysis_bar_state] || '无已完成数据')) +
      fact('最新观察 · 不用于正式趋势',escapeText(latest?.date)+' · '+number(latest?.close)+' ('+percent(d.latest_change_pct)+')',escapeText(STATES[latest?.state] || '无有效数据')) +
      fact('数据来源','<a href="'+source.url+'" target="_blank" rel="noopener">'+source.label+' ↗</a>','原始指数点位 · 成交额单位：元') +
      fact('最近成功获取 · 北京时间',escapeText(timestamp(d.fetched_at, false)), '有效已完成 '+escapeText(d.closed_count ?? 0)+' / 要求 '+escapeText(d.required_history ?? 120)+' 根');
    const diagnostic = errorPresentation(d.error);
    const messages = [...new Set([diagnostic.summary,...(d.warnings || []),latest?.state_reason].filter(Boolean))];
    let title = '日线口径已分离，临时值不影响正式分析', tone = 'info';
    if (d.availability === 'unavailable') { title = '数据获取失败 · 不提供替代行情'; tone = 'error'; }
    else if (d.availability === 'stale') { title = '当前展示为历史 / 降级数据 · 请注意分析日期'; tone = 'warning'; }
    else if (d.trend === '数据不足') { title = '有效历史不足或数据未通过正式分析要求'; tone = 'warning'; }
    else if (d.availability === 'unknown') { title = '行情新鲜度待核验 · 不作为当前市场结论'; tone = 'warning'; }
    const alert = byId('qualityAlert'); alert.className = 'alert '+tone;
    alert.innerHTML = '<span class="alert-icon">'+(tone==='info'?'i':'!')+'</span><div><strong>'+escapeText(title)+'</strong><p>'+escapeText(messages.join('\n') || '正式趋势基于对应分析日的已完成记录；不生成买卖或仓位建议。')+'</p>'+(diagnostic.detail?'<details class="index-diagnostics"><summary>查看技术诊断</summary><pre>'+escapeText(diagnostic.detail)+'</pre></details>':'')+'</div>';
  }

  function selectIndex(code) {
    if (!specs.some(s=>s.code===code)) return;
    selected = code;
    renderCards(); renderChart(); renderTable(); renderQuality();
  }

  async function loadIndex(spec) {
    pending.add(spec.code);
    try {
      const d = await request(config.indexApi.replace(/indices$/, 'index/') + encodeURIComponent(spec.code));
      if (d.code !== spec.code) throw new Error('指数响应身份不匹配');
      items.set(spec.code,d);
    } catch (error) {
      const old = items.get(spec.code);
      items.set(spec.code, {...(old || spec), availability:old?.chart?.length ? 'stale' : 'unavailable',
        error:'本次页面请求失败：'+(error.name==='AbortError'?'请求超时':error.message)+(old?'。保留上次页面结果，不代表当前行情。':''),
        ...(old ? {} : {trend:'数据不足',chart:[],warnings:[]})});
    } finally {
      pending.delete(spec.code);
      renderCards(); renderTable();
      if (selected === spec.code) { renderChart(); renderQuality(); }
    }
  }

  async function loadStatus() {
    const el = byId('systemStatus');
    try {
      const d = await request(config.statusApi);
      el.innerHTML = '<i class="dot online"></i>Web 正常';
      el.title = '原行情扫描进程：'+(d.daemon?'运行中':'未启动')+'；此处仅显示 Web 服务可用性，不代表行情新鲜。';
    } catch (_) { el.innerHTML = '<i class="dot warning"></i>服务连接异常'; }
  }

  async function loadAll() {
    if (busy) return;
    busy = true;
    const button = byId('refreshButton'); button.disabled = true;
    button.innerHTML = '<span class="spinning" aria-hidden="true">↻</span> 获取中';
    byId('indexCards').setAttribute('aria-busy','true');
    try { await Promise.all([loadStatus(), ...specs.map(loadIndex)]); }
    finally { busy = false; button.disabled = false; button.innerHTML = '<span aria-hidden="true">↻</span> 刷新数据'; }
  }

  document.querySelectorAll('[data-index]').forEach(card=>card.addEventListener('click',()=>selectIndex(card.dataset.index)));
  byId('indexSelect').addEventListener('change',e=>selectIndex(e.target.value));
  document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{
    range = Number(button.dataset.range);
    document.querySelectorAll('[data-range]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});
    renderChart();
  }));
  byId('analysisRows').addEventListener('click',e=>{const button=e.target.closest('[data-select]');if(button) selectIndex(button.dataset.select);});
  byId('trendFilter').addEventListener('change',renderTable);
  byId('sortOrder').addEventListener('change',renderTable);
  byId('changeSort').addEventListener('click',()=>{byId('sortOrder').value=byId('sortOrder').value==='desc'?'asc':'desc';renderTable();});
  byId('chartStage').addEventListener('click',e=>{if(e.target.closest('[data-retry]')) loadAll();});
  byId('refreshButton').addEventListener('click',loadAll);
  document.querySelectorAll('nav .nav-item').forEach(link=>link.addEventListener('click',()=>{
    document.querySelectorAll('nav .nav-item').forEach(el=>el.classList.toggle('active',el===link));
  }));
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(()=>{if(chart)chart.resize();}).observe(byId('chartStage'));
  window.addEventListener('resize',()=>{if(chart)chart.resize();});
  // The server retains the existing cache TTL; refreshing never forces a fetch.
  setInterval(()=>{if(!document.hidden)loadAll();},60000);
  loadAll();
})();
