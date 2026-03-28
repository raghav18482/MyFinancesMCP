import { COLORS, formatCurrency, initChart, autoResize } from '../utils/theme.js';

export function renderCashGauge(container, funds, summary) {
  const cash = parseFloat(funds.available_cash) || 0;
  const invested = summary.total_invested || 0;
  const total = cash + invested;

  if (total <= 0) {
    container.innerHTML = '<div class="chart-empty">No funds data available</div>';
    return null;
  }

  const chart = initChart(container);
  const investedPct = (invested / total) * 100;
  const cashPct = (cash / total) * 100;

  chart.setOption({
    tooltip: {
      trigger: 'item',
      formatter(params) {
        return `
          <div style="font-family:Inter,sans-serif;font-size:13px">
            <strong>${params.name}</strong><br/>
            ${formatCurrency(params.value)} &nbsp;(${((params.value / total) * 100).toFixed(1)}%)
          </div>`;
      },
    },
    series: [
      {
        type: 'pie',
        radius: ['62%', '80%'],
        startAngle: 210,
        endAngle: -30,
        padAngle: 3,
        itemStyle: { borderRadius: 8, borderColor: COLORS.surface, borderWidth: 3 },
        label: { show: false },
        emphasis: {
          itemStyle: { shadowBlur: 20, shadowColor: 'rgba(99,102,241,.3)' },
        },
        data: [
          { value: Math.round(invested), name: 'Invested', itemStyle: { color: COLORS.accent } },
          { value: Math.round(cash), name: 'Cash', itemStyle: { color: COLORS.green } },
        ],
      },
    ],
    graphic: [
      {
        type: 'group',
        left: 'center',
        top: '35%',
        children: [
          {
            type: 'text',
            style: {
              text: formatCurrency(total),
              textAlign: 'center',
              fill: COLORS.text,
              fontSize: 20,
              fontWeight: 700,
              fontFamily: 'Inter,sans-serif',
            },
            left: 'center',
          },
          {
            type: 'text',
            style: {
              text: 'Total Capital',
              textAlign: 'center',
              fill: COLORS.muted,
              fontSize: 11,
              fontFamily: 'Inter,sans-serif',
            },
            left: 'center',
            top: 26,
          },
        ],
      },
      {
        type: 'group',
        left: 'center',
        bottom: '5%',
        children: [
          {
            type: 'text',
            style: {
              text: `Invested: ${investedPct.toFixed(1)}%   |   Cash: ${cashPct.toFixed(1)}%`,
              textAlign: 'center',
              fill: COLORS.muted,
              fontSize: 12,
              fontFamily: 'Inter,sans-serif',
            },
            left: 'center',
          },
        ],
      },
    ],
  });

  autoResize(chart, container);
  return chart;
}
