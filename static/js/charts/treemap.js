import { COLORS, pnlGradient, formatCurrency, formatPercent, initChart, autoResize } from '../utils/theme.js';

export function renderTreemap(container, holdings) {
  if (!holdings || !holdings.length) {
    container.innerHTML = '<div class="chart-empty">No holdings data available</div>';
    return null;
  }

  const chart = initChart(container);

  const maxAbsPct = Math.max(...holdings.map(h => Math.abs(h.pnl_pct)), 1);

  const data = holdings
    .filter(h => h.current > 0)
    .map(h => ({
      name: h.symbol.replace('-EQ', ''),
      value: h.current,
      pnl: h.pnl,
      pnl_pct: h.pnl_pct,
      invested: h.invested,
      itemStyle: {
        color: pnlGradient(h.pnl, Math.abs(h.pnl_pct) / maxAbsPct),
        borderColor: COLORS.surface,
        borderWidth: 2,
      },
    }));

  chart.setOption({
    tooltip: {
      formatter(params) {
        const d = params.data;
        const sign = d.pnl >= 0 ? '+' : '';
        return `
          <div style="font-family:Inter,sans-serif;font-size:13px">
            <strong style="font-size:14px">${d.name}</strong><br/>
            Current: ${formatCurrency(d.value)}<br/>
            Invested: ${formatCurrency(d.invested)}<br/>
            P&L: <span style="color:${d.pnl >= 0 ? COLORS.green : COLORS.red}">${sign}${formatCurrency(d.pnl)} (${sign}${d.pnl_pct.toFixed(2)}%)</span>
          </div>`;
      },
    },
    series: [{
      type: 'treemap',
      width: '100%',
      height: '100%',
      roam: false,
      nodeClick: false,
      breadcrumb: { show: false },
      label: {
        show: true,
        formatter(params) {
          const d = params.data;
          const sign = d.pnl_pct >= 0 ? '+' : '';
          return `{name|${d.name}}\n{pct|${sign}${d.pnl_pct.toFixed(1)}%}`;
        },
        rich: {
          name: { fontSize: 13, fontWeight: 700, color: '#fff', lineHeight: 20 },
          pct:  { fontSize: 11, fontWeight: 500, color: 'rgba(255,255,255,.85)', lineHeight: 16 },
        },
      },
      itemStyle: { borderRadius: 4, gapWidth: 3 },
      data,
    }],
  });

  autoResize(chart, container);
  return chart;
}
