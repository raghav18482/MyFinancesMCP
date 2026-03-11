import { COLORS, SECTOR_PALETTE, formatCurrency, initChart, autoResize } from '../utils/theme.js';

export function renderAllocation(container, holdings, sectorMap) {
  if (!holdings || !holdings.length) {
    container.innerHTML = '<div class="chart-empty">No holdings data available</div>';
    return null;
  }

  const chart = initChart(container);

  const sectorTotals = {};
  let totalValue = 0;

  for (const h of holdings) {
    const sector = sectorMap[h.symbol] || 'Other';
    sectorTotals[sector] = (sectorTotals[sector] || 0) + h.current;
    totalValue += h.current;
  }

  const data = Object.entries(sectorTotals)
    .map(([name, value]) => ({ name, value: Math.round(value) }))
    .sort((a, b) => b.value - a.value);

  chart.setOption({
    tooltip: {
      trigger: 'item',
      formatter(params) {
        return `
          <div style="font-family:Inter,sans-serif;font-size:13px">
            <strong>${params.name}</strong><br/>
            ${formatCurrency(params.value)} &nbsp;(${params.percent.toFixed(1)}%)
          </div>`;
      },
    },
    legend: {
      orient: 'vertical',
      right: 10,
      top: 'center',
      textStyle: { color: COLORS.muted, fontSize: 12 },
      itemWidth: 12,
      itemHeight: 12,
      itemGap: 10,
    },
    series: [{
      type: 'pie',
      radius: ['48%', '75%'],
      center: ['35%', '50%'],
      avoidLabelOverlap: true,
      padAngle: 2,
      itemStyle: { borderRadius: 6, borderColor: COLORS.surface, borderWidth: 2 },
      label: { show: false },
      emphasis: {
        label: {
          show: true,
          fontSize: 14,
          fontWeight: 700,
          color: COLORS.text,
          formatter: '{b}\n{d}%',
        },
        itemStyle: { shadowBlur: 20, shadowColor: 'rgba(99,102,241,.4)' },
      },
      color: SECTOR_PALETTE,
      data,
    }],
    graphic: [{
      type: 'group',
      left: '35%',
      top: 'center',
      children: [
        {
          type: 'text',
          style: {
            text: formatCurrency(totalValue),
            textAlign: 'center',
            fill: COLORS.text,
            fontSize: 18,
            fontWeight: 700,
            fontFamily: 'Inter,sans-serif',
          },
          left: 'center',
          top: -10,
        },
        {
          type: 'text',
          style: {
            text: 'Total Value',
            textAlign: 'center',
            fill: COLORS.muted,
            fontSize: 11,
            fontFamily: 'Inter,sans-serif',
          },
          left: 'center',
          top: 14,
        },
      ],
    }],
  });

  autoResize(chart, container);
  return chart;
}
