import { COLORS, pnlColor, formatCurrency, initChart, autoResize } from '../utils/theme.js';

export function renderGainersLosers(container, holdings) {
  if (!holdings || !holdings.length) {
    container.innerHTML = '<div class="chart-empty">No holdings data available</div>';
    return null;
  }

  const chart = initChart(container);

  const sorted = [...holdings]
    .filter(h => h.invested > 0)
    .sort((a, b) => b.pnl - a.pnl);

  const top5 = sorted.slice(0, 5);
  const bottom5 = sorted.slice(-5).reverse();
  const combined = [...top5, ...bottom5.filter(h => !top5.includes(h))];
  const unique = [...new Map(combined.map(h => [h.symbol, h])).values()];
  unique.sort((a, b) => a.pnl - b.pnl);

  const symbols = unique.map(h => h.symbol.replace('-EQ', ''));
  const pnlValues = unique.map(h => h.pnl);
  const pnlPcts = unique.map(h => h.pnl_pct);

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter(params) {
        const p = params[0];
        const idx = p.dataIndex;
        const h = unique[idx];
        const sign = h.pnl >= 0 ? '+' : '';
        return `
          <div style="font-family:Inter,sans-serif;font-size:13px">
            <strong>${symbols[idx]}</strong><br/>
            P&L: <span style="color:${pnlColor(h.pnl)}">${sign}${formatCurrency(h.pnl)}</span><br/>
            P&L %: <span style="color:${pnlColor(h.pnl)}">${sign}${h.pnl_pct.toFixed(2)}%</span><br/>
            Invested: ${formatCurrency(h.invested)}
          </div>`;
      },
    },
    grid: { left: 100, right: 40, top: 15, bottom: 15 },
    xAxis: {
      type: 'value',
      axisLabel: {
        color: COLORS.muted,
        fontSize: 11,
        formatter: v => {
          if (Math.abs(v) >= 100000) return (v / 100000).toFixed(1) + 'L';
          if (Math.abs(v) >= 1000) return (v / 1000).toFixed(0) + 'K';
          return v;
        },
      },
      splitLine: { lineStyle: { color: COLORS.border, type: 'dashed' } },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'category',
      data: symbols,
      axisLabel: { color: COLORS.text, fontSize: 12, fontWeight: 500 },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      data: pnlValues.map((v, i) => ({
        value: v,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(v >= 0 ? 0 : 1, 0, v >= 0 ? 1 : 0, 0, [
            { offset: 0, color: v >= 0 ? 'rgba(34,197,94,.15)' : 'rgba(239,68,68,.15)' },
            { offset: 1, color: v >= 0 ? COLORS.green : COLORS.red },
          ]),
          borderRadius: v >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
        },
        label: {
          show: true,
          position: v >= 0 ? 'right' : 'left',
          formatter: `${pnlPcts[i] >= 0 ? '+' : ''}${pnlPcts[i].toFixed(1)}%`,
          color: pnlColor(v),
          fontSize: 11,
          fontWeight: 600,
        },
      })),
      barMaxWidth: 28,
    }],
  });

  autoResize(chart, container);
  return chart;
}
