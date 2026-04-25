import { COLORS, formatCurrency, initChart, autoResize } from '../utils/theme.js';

/**
 * Render a candlestick chart (same style as analytics) with a live-price
 * overlay line that updates from WS ticks.
 */
export function renderTradingChart(container, candles, stockName, avgBuyPrice) {
  if (!container) return null;
  if (!candles || !candles.length) {
    container.innerHTML = '<div class="chart-empty">No candle data. Select a stock above.</div>';
    return null;
  }

  const chart = initChart(container);

  const dates = candles.map(c => c[0].split('T')[0]);
  const ohlc = candles.map(c => [c[1], c[2], c[3], c[4]]);
  const volumes = candles.map(c => c[5] || 0);
  const lastClose = candles[candles.length - 1][4];

  const markLine = avgBuyPrice ? {
    silent: true,
    symbol: 'none',
    lineStyle: { color: COLORS.yellow, type: 'dashed', width: 2 },
    label: {
      formatter: 'Buy Avg: ' + formatCurrency(avgBuyPrice),
      color: COLORS.yellow,
      fontSize: 11,
      fontWeight: 600,
      backgroundColor: 'rgba(234,179,8,.12)',
      padding: [4, 8],
      borderRadius: 4,
    },
    data: [{ yAxis: avgBuyPrice }],
  } : undefined;

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', crossStyle: { color: COLORS.muted } },
      formatter: function (params) {
        var c = params.find(function (p) { return p.seriesType === 'candlestick'; });
        if (!c) return '';
        var open = c.data[0], close = c.data[1], low = c.data[2], high = c.data[3];
        var change = close - open;
        var changePct = open ? ((change / open) * 100).toFixed(2) : '0.00';
        var color = change >= 0 ? COLORS.green : COLORS.red;
        return '<div style="font-family:Inter,sans-serif;font-size:13px">' +
          '<strong>' + c.axisValue + '</strong><br/>' +
          'O: ' + formatCurrency(open) + ' &nbsp; H: ' + formatCurrency(high) + '<br/>' +
          'L: ' + formatCurrency(low) + ' &nbsp; C: ' + formatCurrency(close) + '<br/>' +
          '<span style="color:' + color + '">' + (change >= 0 ? '+' : '') + change.toFixed(2) + ' (' + changePct + '%)</span>' +
          '</div>';
      },
    },
    grid: [
      { left: 65, right: 20, top: 20, height: '62%' },
      { left: 65, right: 20, top: '76%', height: '17%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        axisLabel: { color: COLORS.muted, fontSize: 10 },
        axisLine: { lineStyle: { color: COLORS.border } },
        splitLine: { show: false },
        gridIndex: 0,
      },
      {
        type: 'category',
        data: dates,
        axisLabel: { show: false },
        axisLine: { lineStyle: { color: COLORS.border } },
        splitLine: { show: false },
        gridIndex: 1,
      },
    ],
    yAxis: [
      {
        scale: true,
        axisLabel: { color: COLORS.muted, fontSize: 11 },
        splitLine: { lineStyle: { color: COLORS.border, type: 'dashed' } },
        axisLine: { show: false },
        gridIndex: 0,
      },
      {
        scale: true,
        axisLabel: { show: false },
        splitLine: { show: false },
        axisLine: { show: false },
        gridIndex: 1,
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 50, end: 100 },
    ],
    series: [
      {
        type: 'candlestick',
        data: ohlc,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: COLORS.green,
          color0: COLORS.red,
          borderColor: COLORS.green,
          borderColor0: COLORS.red,
        },
        markLine: markLine,
      },
      {
        type: 'bar',
        data: volumes.map(function (v, i) {
          return {
            value: v,
            itemStyle: { color: ohlc[i][1] >= ohlc[i][0] ? 'rgba(34,197,94,.3)' : 'rgba(239,68,68,.3)' },
          };
        }),
        xAxisIndex: 1,
        yAxisIndex: 1,
        barMaxWidth: 6,
      },
    ],
  });

  autoResize(chart, container);

  return {
    chart: chart,
    dates: dates,
    ohlc: ohlc,
    volumes: volumes,
    lastClose: lastClose,
  };
}


export function updateLiveTick(state, tick) {
  if (!state || !state.chart) return;
  if (!tick.ltp || tick.ltp <= 0) return;

  var today = new Date().toISOString().split('T')[0];
  var dates = state.dates;
  var ohlc = state.ohlc;
  var volumes = state.volumes;
  var ltp = tick.ltp;

  if (dates.length > 0 && dates[dates.length - 1] === today) {
    var last = ohlc[ohlc.length - 1];
    last[1] = ltp;                          // close
    if (ltp > last[3]) last[3] = ltp;       // high
    if (ltp < last[2]) last[2] = ltp;       // low
  } else {
    dates.push(today);
    ohlc.push([ltp, ltp, ltp, ltp]);
    volumes.push(0);
  }

  state.chart.setOption({
    xAxis: [{ data: dates }, { data: dates }],
    series: [
      { data: ohlc },
      {
        data: volumes.map(function (v, i) {
          return {
            value: v,
            itemStyle: { color: ohlc[i][1] >= ohlc[i][0] ? 'rgba(34,197,94,.3)' : 'rgba(239,68,68,.3)' },
          };
        }),
      },
    ],
  });
}
