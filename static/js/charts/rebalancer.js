import { COLORS, SECTOR_PALETTE, formatCurrency, initChart, autoResize } from '../utils/theme.js';

export function renderRebalancer(container, holdings, funds) {
  if (!holdings || !holdings.length) {
    container.innerHTML = '<div class="chart-empty">No holdings data to simulate</div>';
    return null;
  }

  const cash = parseFloat(funds.available_cash) || 0;

  const state = holdings
    .filter(h => h.current > 0)
    .sort((a, b) => b.current - a.current)
    .slice(0, 12)
    .map(h => ({
      symbol: h.symbol,
      label: h.symbol.replace('-EQ', ''),
      original: h.current,
      current: h.current,
      sellPct: 0,
    }));

  let extraCash = 0;
  let buySymbol = '';
  let buyAmount = 0;

  const chartEl = document.createElement('div');
  chartEl.className = 'rebal-chart';
  chartEl.style.height = '320px';

  const controlsEl = document.createElement('div');
  controlsEl.className = 'rebal-controls';

  container.innerHTML = '';
  container.appendChild(controlsEl);
  container.appendChild(chartEl);

  const chart = initChart(chartEl);

  function buildControls() {
    let html = '<div class="rebal-header"><span class="rebal-title">Drag sliders to simulate selling</span>';
    html += `<span class="rebal-cash">Freed Cash: <strong id="freedCash">${formatCurrency(0)}</strong></span></div>`;
    html += '<div class="rebal-sliders">';

    for (let i = 0; i < state.length; i++) {
      const s = state[i];
      html += `
        <div class="rebal-row">
          <span class="rebal-label">${s.label}</span>
          <input type="range" min="0" max="100" value="0" class="rebal-slider" data-idx="${i}">
          <span class="rebal-pct" id="pct-${i}">0%</span>
          <span class="rebal-val" id="val-${i}">${formatCurrency(s.original)}</span>
        </div>`;
    }

    html += '</div>';
    html += '<div class="rebal-buy-row">';
    html += '<label class="rebal-buy-label">Reinvest in:</label>';
    html += '<select id="buyTarget" class="rebal-select"><option value="">-- Select stock --</option>';
    for (const s of state) {
      html += `<option value="${s.symbol}">${s.label}</option>`;
    }
    html += '<option value="__CASH__">Keep as Cash</option></select>';
    html += `<label class="rebal-buy-label" style="margin-left:1rem">Amount:</label>`;
    html += `<input type="number" id="buyAmount" class="rebal-input" placeholder="0" min="0" value="0">`;
    html += '<button class="btn btn-sm btn-accent" id="applyBuy">Apply</button>';
    html += '</div>';

    controlsEl.innerHTML = html;
  }

  function updateChart() {
    const data = state.map((s, i) => ({
      name: s.label,
      value: Math.round(s.current),
      itemStyle: {
        color: SECTOR_PALETTE[i % SECTOR_PALETTE.length],
        opacity: s.current < s.original ? 0.5 : 1,
      },
    }));

    chart.setOption({
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter(params) {
          const p = params[0];
          const s = state[p.dataIndex];
          const diff = s.current - s.original;
          const diffSign = diff >= 0 ? '+' : '';
          return `
            <div style="font-family:Inter,sans-serif;font-size:13px">
              <strong>${s.label}</strong><br/>
              Original: ${formatCurrency(s.original)}<br/>
              After: ${formatCurrency(s.current)}<br/>
              Change: <span style="color:${diff >= 0 ? COLORS.green : COLORS.red}">${diffSign}${formatCurrency(diff)}</span>
            </div>`;
        },
      },
      grid: { left: 100, right: 30, top: 10, bottom: 10 },
      xAxis: {
        type: 'value',
        axisLabel: {
          color: COLORS.muted,
          fontSize: 11,
          formatter: v => {
            if (v >= 100000) return (v / 100000).toFixed(1) + 'L';
            if (v >= 1000) return (v / 1000).toFixed(0) + 'K';
            return v;
          },
        },
        splitLine: { lineStyle: { color: COLORS.border, type: 'dashed' } },
        axisLine: { show: false },
      },
      yAxis: {
        type: 'category',
        data: data.map(d => d.name),
        axisLabel: { color: COLORS.text, fontSize: 12, fontWeight: 500 },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: [{
        type: 'bar',
        data,
        barMaxWidth: 24,
        itemStyle: { borderRadius: [0, 4, 4, 0] },
      }],
    });
  }

  buildControls();
  updateChart();

  controlsEl.addEventListener('input', (e) => {
    if (!e.target.classList.contains('rebal-slider')) return;
    const idx = parseInt(e.target.dataset.idx);
    const pct = parseInt(e.target.value);
    state[idx].sellPct = pct;
    state[idx].current = state[idx].original * (1 - pct / 100);
    document.getElementById(`pct-${idx}`).textContent = pct + '%';
    document.getElementById(`val-${idx}`).textContent = formatCurrency(state[idx].current);

    extraCash = state.reduce((sum, s) => sum + (s.original - s.current), 0);
    document.getElementById('freedCash').textContent = formatCurrency(extraCash);
    document.getElementById('buyAmount').max = Math.round(extraCash + cash);

    updateChart();
  });

  container.addEventListener('click', (e) => {
    if (e.target.id !== 'applyBuy') return;
    const target = document.getElementById('buyTarget').value;
    const amount = parseFloat(document.getElementById('buyAmount').value) || 0;
    if (!target || amount <= 0 || amount > extraCash + cash) return;

    if (target !== '__CASH__') {
      const s = state.find(s => s.symbol === target);
      if (s) s.current += amount;
    }

    updateChart();
  });

  autoResize(chart, chartEl);
  return chart;
}
