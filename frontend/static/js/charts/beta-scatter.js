import { COLORS, initChart, autoResize } from '../utils/theme.js';

export function renderBetaScatter(container, betaData) {
  if (!betaData || !betaData.dates || !betaData.dates.length) {
    container.innerHTML = '<div class="chart-empty">Not enough historical data to compute beta</div>';
    return null;
  }

  const chart = initChart(container);

  const { dates, nifty_cumulative, portfolio_cumulative, beta, alpha, r_squared, stock_betas = [] } = betaData;

  const labels = dates.map(d => {
    const parts = d.split('-');
    return `${parts[2]}/${parts[1]}`;
  });

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(15,17,23,.95)',
      borderColor: COLORS.border,
      textStyle: { color: COLORS.text, fontSize: 12, fontFamily: 'Inter, sans-serif' },
      formatter(params) {
        let html = `<div style="margin-bottom:4px;font-weight:600">${params[0].axisValue}</div>`;
        for (const p of params) {
          const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${p.color};margin-right:6px"></span>`;
          html += `<div>${dot}${p.seriesName}: <b>${p.value >= 0 ? '+' : ''}${p.value.toFixed(2)}%</b></div>`;
        }
        return html;
      },
    },
    legend: {
      data: ['NIFTY 50', 'My Portfolio'],
      top: 6,
      textStyle: { color: COLORS.text, fontSize: 12 },
      itemWidth: 20,
      itemHeight: 3,
    },
    grid: { left: 55, right: 20, top: 45, bottom: 50 },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: {
        color: COLORS.muted,
        fontSize: 10,
        rotate: 45,
        interval: Math.max(0, Math.floor(labels.length / 12) - 1),
      },
      axisLine: { lineStyle: { color: COLORS.border } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      name: 'Cumulative Return (%)',
      nameTextStyle: { color: COLORS.muted, fontSize: 11 },
      axisLabel: {
        color: COLORS.muted,
        fontSize: 11,
        formatter: v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%',
      },
      splitLine: { lineStyle: { color: COLORS.border, type: 'dashed' } },
      axisLine: { show: false },
    },
    series: [
      {
        name: 'NIFTY 50',
        type: 'line',
        data: nifty_cumulative,
        smooth: 0.3,
        symbol: 'none',
        lineStyle: { color: COLORS.yellow, width: 2.5 },
        itemStyle: { color: COLORS.yellow },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(234,179,8,0.18)' },
            { offset: 1, color: 'rgba(234,179,8,0)' },
          ]),
        },
      },
      {
        name: 'My Portfolio',
        type: 'line',
        data: portfolio_cumulative,
        smooth: 0.3,
        symbol: 'none',
        lineStyle: { color: COLORS.green, width: 2.5 },
        itemStyle: { color: COLORS.green },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(34,197,94,0.18)' },
            { offset: 1, color: 'rgba(34,197,94,0)' },
          ]),
        },
      },
    ],
  });

  autoResize(chart, container);

  _renderStatsBar(container, betaData);

  return chart;
}

const INFO = {
  beta: {
    title: 'Portfolio Beta (\u03B2)',
    desc: 'Measures how sensitive your portfolio is to market (NIFTY 50) movements. A beta of 1.0 means your portfolio moves exactly with the market.',
    ideal: '\u03B2 < 0.8 = Defensive (less volatile)\n\u03B2 \u2248 1.0 = Moves with market\n\u03B2 > 1.2 = Aggressive (more volatile)',
    tip: 'For long-term wealth building, \u03B2 between 0.8\u20131.2 is considered balanced. Lower beta during uncertain markets helps protect capital.',
  },
  alpha: {
    title: 'Alpha (\u03B1)',
    desc: 'The excess daily return your portfolio generates beyond what the market movement explains. Positive alpha = you\'re beating the market after adjusting for risk.',
    ideal: '\u03B1 > 0 = Outperforming the market\n\u03B1 = 0 = Matching the market\n\u03B1 < 0 = Underperforming',
    tip: 'Even a small positive daily alpha compounds significantly over time. Consistent \u03B1 > 0.01% daily is excellent.',
  },
  r2: {
    title: 'R-Squared (R\u00B2)',
    desc: 'Shows what percentage of your portfolio\'s movement is explained by the market. R\u00B2 of 1.0 means your portfolio perfectly tracks NIFTY 50.',
    ideal: 'R\u00B2 > 0.7 = Closely tracks the market\nR\u00B2 0.4\u20130.7 = Moderate correlation\nR\u00B2 < 0.4 = Largely independent of market',
    tip: 'High R\u00B2 with low beta = diversified market tracker. Low R\u00B2 = your returns come from stock-specific factors, not the broad market.',
  },
};

function _infoIcon(key) {
  const info = INFO[key];
  return `<span class="beta-info-wrap">
    <span class="beta-info-icon" tabindex="0">i</span>
    <span class="beta-info-tip">
      <strong>${info.title}</strong>
      <span class="beta-info-desc">${info.desc}</span>
      <span class="beta-info-section">
        <span class="beta-info-heading">Ideal Values</span>
        <span class="beta-info-values">${info.ideal}</span>
      </span>
      <span class="beta-info-section">
        <span class="beta-info-heading">Pro Tip</span>
        <span class="beta-info-values">${info.tip}</span>
      </span>
    </span>
  </span>`;
}

function _renderStatsBar(container, data) {
  const { beta, alpha, r_squared, stock_betas = [], days_used } = data;
  const topBetas = stock_betas.slice(0, 6);

  const betaColor = beta > 1.2 ? COLORS.red : beta > 0.8 ? COLORS.yellow : COLORS.green;
  const betaLabel = beta > 1.2 ? 'High volatility'
                   : beta > 0.8 ? 'Moves with market'
                   : 'Defensive';

  const stockChips = topBetas.map(s => {
    const sym = s.symbol.replace('-EQ', '');
    const c = s.beta > 1.2 ? COLORS.red : s.beta > 0.8 ? COLORS.yellow : COLORS.green;
    return `<span class="beta-chip" style="border-color:${c}">
      <span class="beta-chip-sym">${sym}</span>
      <span class="beta-chip-val" style="color:${c}">${s.beta.toFixed(2)}</span>
    </span>`;
  }).join('');

  const bar = document.createElement('div');
  bar.className = 'beta-stats-bar';
  bar.innerHTML = `
    <div class="beta-stat beta-stat-main">
      <span class="beta-stat-label">Portfolio Beta ${_infoIcon('beta')}</span>
      <span class="beta-stat-value" style="color:${betaColor}">${beta.toFixed(3)}</span>
      <span class="beta-stat-tag" style="color:${betaColor}">${betaLabel}</span>
    </div>
    <div class="beta-stat">
      <span class="beta-stat-label">Alpha (daily) ${_infoIcon('alpha')}</span>
      <span class="beta-stat-value">${(alpha * 100).toFixed(4)}%</span>
    </div>
    <div class="beta-stat">
      <span class="beta-stat-label">R-squared ${_infoIcon('r2')}</span>
      <span class="beta-stat-value">${r_squared.toFixed(3)}</span>
    </div>
    <div class="beta-stat">
      <span class="beta-stat-label">Days</span>
      <span class="beta-stat-value">${days_used || '\u2014'}</span>
    </div>
    ${topBetas.length ? `<div class="beta-stock-chips">${stockChips}</div>` : ''}
  `;

  container.parentElement.appendChild(bar);
}
