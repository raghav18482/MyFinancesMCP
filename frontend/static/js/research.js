const $ = id => document.getElementById(id);

function showLoading(el) { el.style.display = 'flex'; }
function hideLoading(el) { el.style.display = 'none'; }
function showError(el, msg) { el.textContent = msg; el.style.display = 'block'; }
function hideError(el) { el.style.display = 'none'; }

function fmtNum(v, decimals = 2) {
  if (v == null || v !== v) return '—';
  return Number(v).toLocaleString('en-IN', { maximumFractionDigits: decimals, minimumFractionDigits: decimals });
}

function fmtCrore(v) {
  if (v == null) return '—';
  const abs = Math.abs(v);
  if (abs >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (abs >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  return '₹' + fmtNum(v);
}

let holdings = [];
let currentSymbol = null;

let _priceChart = null;
let _rsiChart = null;
let _macdChart = null;
let _financialsChart = null;

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Tab switching ──
document.querySelectorAll('.research-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.research-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.research-tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    $('tab-' + tab.dataset.tab).classList.add('active');

    // Charts rendered in hidden tabs have 0-width; resize them now
    setTimeout(() => {
      if (tab.dataset.tab === 'technical') {
        [_priceChart, _rsiChart, _macdChart].forEach(c => c && c.resize());
      } else if (tab.dataset.tab === 'fundamental') {
        _financialsChart && _financialsChart.resize();
      }
    }, 50);
  });
});

// ── Load holdings and build pills ──
async function init() {
  try {
    const data = await fetchJSON('/api/portfolio/analytics');
    if (data.error) {
      $('stock-pills').innerHTML = `<div class="alert alert-error">${data.error}</div>`;
      return;
    }
    holdings = data.holdings || [];
    if (!holdings.length) {
      $('stock-pills').innerHTML = '<p style="color:var(--muted)">No holdings found.</p>';
      return;
    }
    renderPills();
    selectStock(holdings[0].symbol);
  } catch (err) {
    $('stock-pills').innerHTML = `<div class="alert alert-error">Failed to load: ${err.message}</div>`;
  }
}

function renderPills() {
  const container = $('stock-pills');
  container.innerHTML = '';
  for (const h of holdings) {
    const pill = document.createElement('button');
    pill.className = 'stock-pill';
    pill.dataset.symbol = h.symbol;
    pill.textContent = h.symbol.replace('-EQ', '');
    const pnlPct = h.pnl_pct || 0;
    pill.innerHTML += ` <span class="${pnlPct >= 0 ? 'positive' : 'negative'}">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%</span>`;
    pill.addEventListener('click', () => selectStock(h.symbol));
    container.appendChild(pill);
  }
}

function selectStock(symbol) {
  currentSymbol = symbol;
  document.querySelectorAll('.stock-pill').forEach(p => {
    p.classList.toggle('active', p.dataset.symbol === symbol);
  });

  const h = holdings.find(x => x.symbol === symbol);
  if (h) {
    $('stock-bar').style.display = 'flex';
    $('bar-name').textContent = symbol.replace('-EQ', '');
    $('bar-meta').textContent = `Qty: ${h.qty} · Avg: ₹${fmtNum(h.avg_price)} · Invested: ₹${fmtNum(h.invested)}`;
    $('bar-price').textContent = `₹${fmtNum(h.ltp)}`;
    const pnl = h.pnl || 0;
    const pnlPct = h.pnl_pct || 0;
    $('bar-pnl').className = 'stock-bar-pnl ' + (pnl >= 0 ? 'positive' : 'negative');
    $('bar-pnl').textContent = `${pnl >= 0 ? '+' : ''}₹${fmtNum(pnl)} (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)`;
  }

  $('research-tabs').style.display = 'flex';
  loadFundamental(symbol);
  loadTechnical(symbol);
}

// ── Fundamental Analysis ──
async function loadFundamental(symbol) {
  showLoading($('fundamental-loading'));
  hideError($('fundamental-error'));
  $('fundamental-content').style.display = 'none';

  try {
    const data = await fetchJSON(`/api/research/fundamental?symbol=${encodeURIComponent(symbol)}`);
    hideLoading($('fundamental-loading'));

    if (data.error) {
      showError($('fundamental-error'), data.error);
      return;
    }

    renderValuationMetrics(data.valuation || {});
    renderHealthMetrics(data.health || {});
    renderFinancialsChart(data.revenue_trend || [], data.profit_trend || []);

    $('fundamental-content').style.display = 'block';
  } catch (err) {
    hideLoading($('fundamental-loading'));
    showError($('fundamental-error'), 'Failed to load fundamental data: ' + err.message);
  }
}

function renderValuationMetrics(v) {
  const items = [
    { label: 'P/E Ratio', value: fmtNum(v.pe_ratio), sub: v.forward_pe ? `Forward: ${fmtNum(v.forward_pe)}` : '' },
    { label: 'P/B Ratio', value: fmtNum(v.pb_ratio) },
    { label: 'EV/EBITDA', value: fmtNum(v.ev_ebitda) },
    { label: 'PEG Ratio', value: fmtNum(v.peg_ratio) },
    { label: 'Dividend Yield', value: v.dividend_yield != null ? v.dividend_yield + '%' : '—' },
    { label: 'Trailing EPS', value: fmtNum(v.trailing_eps) },
  ];
  $('valuation-metrics').innerHTML = items.map(i =>
    `<div class="metric-item">
       <span class="metric-label">${i.label}</span>
       <span class="metric-value">${i.value}</span>
       ${i.sub ? `<span class="metric-sub">${i.sub}</span>` : ''}
     </div>`
  ).join('');
}

function renderHealthMetrics(h) {
  const items = [
    { label: 'ROE', value: h.roe != null ? h.roe + '%' : '—' },
    { label: 'ROCE', value: h.roce != null ? h.roce + '%' : '—' },
    { label: 'Debt to Equity', value: fmtNum(h.debt_to_equity) },
    { label: 'Free Cash Flow', value: fmtCrore(h.free_cash_flow) },
    { label: 'Profit Margin', value: h.profit_margin != null ? h.profit_margin + '%' : '—' },
    { label: 'Operating Margin', value: h.operating_margin != null ? h.operating_margin + '%' : '—' },
    { label: 'Revenue Growth', value: h.revenue_growth != null ? h.revenue_growth + '%' : '—' },
    { label: 'Earnings Growth', value: h.earnings_growth != null ? h.earnings_growth + '%' : '—' },
    { label: 'Promoter Holding', value: h.promoter_holding != null ? h.promoter_holding + '%' : '—' },
  ];
  $('health-metrics').innerHTML = items.map(i =>
    `<div class="metric-item">
       <span class="metric-label">${i.label}</span>
       <span class="metric-value">${i.value}</span>
     </div>`
  ).join('');
}

function renderFinancialsChart(revenue, profit) {
  const container = $('chart-financials');
  if (!revenue.length && !profit.length) {
    container.innerHTML = '<div class="chart-empty">No financial trend data available</div>';
    return;
  }

  if (_financialsChart) { _financialsChart.dispose(); }
  const chart = echarts.init(container);
  _financialsChart = chart;
  const years = revenue.map(r => r.year);

  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['Revenue', 'Net Profit'], textStyle: { color: '#9ca3af' }, bottom: 0 },
    grid: { top: 30, right: 30, bottom: 50, left: 30, containLabel: true },
    xAxis: { type: 'category', data: years, axisLabel: { color: '#9ca3af' }, axisLine: { lineStyle: { color: '#2e3143' } } },
    yAxis: {
      type: 'value',
      axisLabel: {
        color: '#9ca3af',
        formatter: v => {
          if (Math.abs(v) >= 1e12) return (v / 1e12).toFixed(1) + 'T';
          if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(1) + 'B';
          if (Math.abs(v) >= 1e7) return (v / 1e7).toFixed(0) + 'Cr';
          return v;
        }
      },
      splitLine: { lineStyle: { color: '#2e3143' } },
    },
    series: [
      {
        name: 'Revenue', type: 'bar', barWidth: '35%',
        data: revenue.map(r => r.value),
        itemStyle: { color: '#6366f1', borderRadius: [4, 4, 0, 0] },
      },
      {
        name: 'Net Profit', type: 'line', smooth: true,
        data: profit.map(p => p.value),
        lineStyle: { color: '#22c55e', width: 2 },
        itemStyle: { color: '#22c55e' },
      },
    ],
  });

  window.addEventListener('resize', () => chart.resize());
}

// ── Technical Analysis ──
async function loadTechnical(symbol) {
  showLoading($('technical-loading'));
  hideError($('technical-error'));
  $('technical-content').style.display = 'none';

  try {
    const data = await fetchJSON(`/api/research/technical?symbol=${encodeURIComponent(symbol)}&days=365`);
    hideLoading($('technical-loading'));

    if (data.error) {
      showError($('technical-error'), data.error);
      return;
    }

    renderTechIndicators(data.indicators || {});
    renderSignals(data.signals || []);
    renderSRLevels(data.support_resistance || {}, data.current_price);
    renderPriceChart(data.chart_data || {}, data.avg_price);
    renderRSIChart(data.chart_data || {});
    renderMACDChart(data.chart_data || {});

    $('technical-content').style.display = 'block';
  } catch (err) {
    hideLoading($('technical-loading'));
    showError($('technical-error'), 'Failed to load technical data: ' + err.message);
  }
}

function renderTechIndicators(ind) {
  const items = [
    { label: '50 DMA', value: ind.sma_50 != null ? '₹' + fmtNum(ind.sma_50) : '—' },
    { label: '200 DMA', value: ind.sma_200 != null ? '₹' + fmtNum(ind.sma_200) : '—' },
    { label: 'RSI (14)', value: fmtNum(ind.rsi, 1) },
    { label: 'MACD', value: fmtNum(ind.macd) },
    { label: 'MACD Signal', value: fmtNum(ind.macd_signal) },
    { label: '52W High', value: ind.week52_high != null ? '₹' + fmtNum(ind.week52_high) : '—' },
    { label: '52W Low', value: ind.week52_low != null ? '₹' + fmtNum(ind.week52_low) : '—' },
    { label: '52W Position', value: ind.week52_position != null ? ind.week52_position + '%' : '—' },
    { label: 'Volume vs Avg', value: ind.volume_ratio != null ? ind.volume_ratio + 'x' : '—' },
  ];
  $('tech-indicators').innerHTML = items.map(i =>
    `<div class="metric-item">
       <span class="metric-label">${i.label}</span>
       <span class="metric-value">${i.value}</span>
     </div>`
  ).join('');
}

function renderSignals(signals) {
  $('signal-list').innerHTML = signals.map(s => {
    const cls = s.status === 'bullish' ? 'signal-bullish' : s.status === 'bearish' ? 'signal-bearish' : 'signal-neutral';
    const icon = s.status === 'bullish' ? '▲' : s.status === 'bearish' ? '▼' : '●';
    return `<div class="signal-item ${cls}">
      <span class="signal-icon">${icon}</span>
      <span class="signal-name">${s.name}</span>
      <span class="signal-val">${s.value}</span>
      <span class="signal-label">${s.label}</span>
    </div>`;
  }).join('');
}

function renderSRLevels(sr, price) {
  if (!sr.pivot) {
    $('sr-levels').innerHTML = '<p style="color:var(--muted)">Not enough data for pivot points.</p>';
    return;
  }

  const levels = [
    { label: 'R2', value: sr.r2, type: 'resist' },
    { label: 'R1', value: sr.r1, type: 'resist' },
    { label: 'Pivot', value: sr.pivot, type: 'pivot' },
    { label: 'S1', value: sr.s1, type: 'support' },
    { label: 'S2', value: sr.s2, type: 'support' },
  ];

  $('sr-levels').innerHTML = `
    <div class="sr-bar">
      ${levels.map(l => `
        <div class="sr-level sr-${l.type}">
          <span class="sr-label">${l.label}</span>
          <span class="sr-value">₹${fmtNum(l.value)}</span>
        </div>
      `).join('')}
    </div>
    ${price ? `<div style="text-align:center;margin-top:.5rem;font-size:.8rem;color:var(--muted)">Current Price: ₹${fmtNum(price)}</div>` : ''}
  `;
}

function renderPriceChart(cd, avgPrice) {
  const container = $('chart-price');
  if (!cd.dates || !cd.dates.length) {
    container.innerHTML = '<div class="chart-empty">No price data available</div>';
    return;
  }

  if (_priceChart) { _priceChart.dispose(); }
  const chart = echarts.init(container);
  _priceChart = chart;
  const n = cd.dates.length;
  const zoomStart = n > 120 ? Math.round((1 - 120 / n) * 100) : 0;

  const series = [
    {
      name: 'Price', type: 'candlestick',
      data: cd.ohlc,
      itemStyle: {
        color: '#22c55e', color0: '#ef4444',
        borderColor: '#22c55e', borderColor0: '#ef4444',
      },
    },
    {
      name: 'Volume', type: 'bar', xAxisIndex: 0, yAxisIndex: 1,
      data: cd.volume,
      itemStyle: { color: 'rgba(99,102,241,.25)' },
      barWidth: '60%',
    },
  ];

  if (cd.sma_50 && cd.sma_50.length) {
    series.push({
      name: '50 DMA', type: 'line', data: cd.sma_50, smooth: true,
      lineStyle: { color: '#3b82f6', width: 1.5 }, symbol: 'none',
    });
  }
  if (cd.sma_200 && cd.sma_200.length) {
    series.push({
      name: '200 DMA', type: 'line', data: cd.sma_200, smooth: true,
      lineStyle: { color: '#f97316', width: 1.5 }, symbol: 'none',
    });
  }
  if (avgPrice) {
    series.push({
      name: 'Avg Buy', type: 'line',
      data: cd.dates.map(() => avgPrice),
      lineStyle: { color: '#eab308', width: 1.5, type: 'dashed' },
      symbol: 'none',
    });
  }

  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, confine: true },
    legend: {
      data: series.filter(s => s.name !== 'Volume').map(s => s.name),
      textStyle: { color: '#9ca3af', fontSize: 11 },
      top: 0, left: 'center',
    },
    grid: { top: 35, right: 50, bottom: 65, left: 50, containLabel: true },
    xAxis: {
      type: 'category', data: cd.dates, boundaryGap: true,
      axisLabel: { color: '#9ca3af', fontSize: 10, rotate: 30, formatter: v => v.slice(5) },
      axisLine: { lineStyle: { color: '#2e3143' } },
    },
    yAxis: [
      {
        type: 'value', scale: true, position: 'left',
        axisLabel: { color: '#9ca3af', fontSize: 10 },
        splitLine: { lineStyle: { color: '#2e3143' } },
      },
      {
        type: 'value', scale: true, position: 'right',
        axisLabel: { show: false }, splitLine: { show: false },
        max: v => v.max * 4,
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: 0, start: zoomStart, end: 100 },
      { type: 'slider', xAxisIndex: 0, start: zoomStart, end: 100, height: 22, bottom: 8, borderColor: '#2e3143', fillerColor: 'rgba(99,102,241,.2)', textStyle: { color: '#9ca3af', fontSize: 10 } },
    ],
    series,
  });

  window.addEventListener('resize', () => chart.resize());
}

function renderRSIChart(cd) {
  const container = $('chart-rsi');
  if (!cd.rsi || !cd.rsi.length) {
    container.innerHTML = '<div class="chart-empty">Not enough data for RSI</div>';
    return;
  }

  if (_rsiChart) { _rsiChart.dispose(); }
  const chart = echarts.init(container);
  _rsiChart = chart;
  const n = cd.dates.length;
  const zoomStart = n > 120 ? Math.round((1 - 120 / n) * 100) : 0;

  chart.setOption({
    tooltip: { trigger: 'axis', confine: true },
    grid: { top: 15, right: 30, bottom: 40, left: 30, containLabel: true },
    xAxis: {
      type: 'category', data: cd.dates, boundaryGap: false,
      axisLabel: { color: '#9ca3af', fontSize: 10, rotate: 30, interval: 'auto', formatter: v => v.slice(5) },
      axisLine: { lineStyle: { color: '#2e3143' } },
    },
    yAxis: {
      type: 'value', min: 0, max: 100,
      axisLabel: { color: '#9ca3af', fontSize: 10 },
      splitLine: { lineStyle: { color: '#2e3143' } },
    },
    visualMap: {
      show: false, pieces: [
        { gt: 0, lte: 30, color: '#22c55e' },
        { gt: 30, lte: 70, color: '#6366f1' },
        { gt: 70, lte: 100, color: '#ef4444' },
      ],
    },
    series: [
      {
        name: 'RSI', type: 'line', data: cd.rsi, symbol: 'none', lineStyle: { width: 1.5 },
        areaStyle: { opacity: 0.05 },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', width: 1 },
          data: [
            { yAxis: 70, lineStyle: { color: 'rgba(239,68,68,.4)' }, label: { formatter: '70', color: '#ef4444', fontSize: 10 } },
            { yAxis: 30, lineStyle: { color: 'rgba(34,197,94,.4)' }, label: { formatter: '30', color: '#22c55e', fontSize: 10 } },
          ],
        },
      },
    ],
    dataZoom: [{ type: 'inside', start: zoomStart, end: 100 }],
  });

  window.addEventListener('resize', () => chart.resize());
}

function renderMACDChart(cd) {
  const container = $('chart-macd');
  if (!cd.macd_line || !cd.macd_line.length) {
    container.innerHTML = '<div class="chart-empty">Not enough data for MACD</div>';
    return;
  }

  if (_macdChart) { _macdChart.dispose(); }
  const chart = echarts.init(container);
  _macdChart = chart;
  const n = cd.dates.length;
  const zoomStart = n > 120 ? Math.round((1 - 120 / n) * 100) : 0;

  chart.setOption({
    tooltip: { trigger: 'axis', confine: true },
    legend: { data: ['MACD', 'Signal', 'Histogram'], textStyle: { color: '#9ca3af', fontSize: 11 }, top: 0, left: 'center' },
    grid: { top: 30, right: 30, bottom: 40, left: 30, containLabel: true },
    xAxis: {
      type: 'category', data: cd.dates, boundaryGap: true,
      axisLabel: { color: '#9ca3af', fontSize: 10, rotate: 30, formatter: v => v.slice(5) },
      axisLine: { lineStyle: { color: '#2e3143' } },
    },
    yAxis: {
      type: 'value', scale: true,
      axisLabel: { color: '#9ca3af', fontSize: 10 },
      splitLine: { lineStyle: { color: '#2e3143' } },
    },
    series: [
      {
        name: 'Histogram', type: 'bar', data: cd.macd_histogram,
        itemStyle: {
          color: p => (p.value >= 0 ? 'rgba(34,197,94,.5)' : 'rgba(239,68,68,.5)'),
        },
      },
      { name: 'MACD', type: 'line', data: cd.macd_line, symbol: 'none', lineStyle: { color: '#3b82f6', width: 1.5 } },
      { name: 'Signal', type: 'line', data: cd.macd_signal, symbol: 'none', lineStyle: { color: '#f97316', width: 1.5 } },
    ],
    dataZoom: [{ type: 'inside', start: zoomStart, end: 100 }],
  });

  window.addEventListener('resize', () => chart.resize());
}

init();
