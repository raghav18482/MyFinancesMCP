import { COLORS, initChart, autoResize } from '../utils/theme.js';

const TF_LABELS = {
  '10min': '10 Min',
  '1hr':   '1 Hour',
  '4hr':   '4 Hours',
  '1day':  '1 Day',
  '1week': '1 Week',
  '1month':'1 Month',
  '1year': '1 Year',
};

const TF_ORDER = ['10min', '1hr', '4hr', '1day', '1week', '1month', '1year'];

export function renderPrediction(container, data) {
  if (!data || data.error) {
    container.innerHTML = `<div class="chart-empty">${data?.error || 'No prediction data'}</div>`;
    return null;
  }

  const { predictions, overall_outlook, overall_score, model_type,
          top_bullish_signals, top_bearish_signals, symbol } = data;

  container.innerHTML = '';

  const wrapper = document.createElement('div');
  wrapper.className = 'pred-wrapper';

  const outlookColor = overall_outlook === 'bullish' ? COLORS.green
    : overall_outlook === 'bearish' ? COLORS.red : COLORS.yellow;
  const outlookIcon = overall_outlook === 'bullish' ? '▲'
    : overall_outlook === 'bearish' ? '▼' : '●';

  const header = document.createElement('div');
  header.className = 'pred-overall';
  header.innerHTML = `
    <div class="pred-overall-main">
      <span class="pred-outlook-icon" style="color:${outlookColor}">${outlookIcon}</span>
      <span class="pred-outlook-label" style="color:${outlookColor}">
        ${overall_outlook.toUpperCase()}
      </span>
      <span class="pred-outlook-score">${(overall_score * 100).toFixed(1)}%</span>
    </div>
    <div class="pred-model-tag">
      ${model_type === 'lightgbm' ? 'ML Model' : 'Technical Analysis'} &bull; ${symbol.replace('-EQ', '')}
    </div>
  `;
  wrapper.appendChild(header);

  const grid = document.createElement('div');
  grid.className = 'pred-grid';

  for (const tf of TF_ORDER) {
    const p = predictions[tf];
    if (!p) continue;

    const card = document.createElement('div');
    card.className = 'pred-card';

    const dirColor = p.direction === 'up' ? COLORS.green
      : p.direction === 'down' ? COLORS.red : COLORS.yellow;
    const dirIcon = p.direction === 'up' ? '▲' : p.direction === 'down' ? '▼' : '—';
    const conf = (p.confidence * 100).toFixed(0);

    const confBarW = Math.max(20, p.confidence * 100);

    card.innerHTML = `
      <div class="pred-card-tf">${TF_LABELS[tf]}</div>
      <div class="pred-card-dir" style="color:${dirColor}">
        <span class="pred-dir-icon">${dirIcon}</span>
        <span class="pred-dir-text">${p.direction.toUpperCase()}</span>
      </div>
      <div class="pred-card-conf">
        <div class="pred-conf-bar-bg">
          <div class="pred-conf-bar" style="width:${confBarW}%;background:${dirColor}"></div>
        </div>
        <span class="pred-conf-pct">${conf}%</span>
      </div>
    `;
    grid.appendChild(card);
  }
  wrapper.appendChild(grid);

  const chartRow = document.createElement('div');
  chartRow.className = 'pred-chart-row';

  const gaugeContainer = document.createElement('div');
  gaugeContainer.className = 'pred-gauge-box';
  chartRow.appendChild(gaugeContainer);

  const signalsPanel = document.createElement('div');
  signalsPanel.className = 'pred-signals';

  let bullishHTML = '<div class="pred-signal-group"><div class="pred-signal-title" style="color:' + COLORS.green + '">Bullish Signals</div>';
  if (top_bullish_signals && top_bullish_signals.length > 0) {
    for (const s of top_bullish_signals) {
      bullishHTML += `<div class="pred-signal-row"><span class="pred-signal-name">${s.feature}</span><span class="pred-signal-val" style="color:${COLORS.green}">${s.value > 0 ? '+' : ''}${s.value}</span></div>`;
    }
  } else {
    bullishHTML += '<div class="pred-signal-row"><span class="pred-signal-name" style="color:' + COLORS.muted + '">No strong signals</span></div>';
  }
  bullishHTML += '</div>';

  let bearishHTML = '<div class="pred-signal-group"><div class="pred-signal-title" style="color:' + COLORS.red + '">Bearish Signals</div>';
  if (top_bearish_signals && top_bearish_signals.length > 0) {
    for (const s of top_bearish_signals) {
      bearishHTML += `<div class="pred-signal-row"><span class="pred-signal-name">${s.feature}</span><span class="pred-signal-val" style="color:${COLORS.red}">${s.value}</span></div>`;
    }
  } else {
    bearishHTML += '<div class="pred-signal-row"><span class="pred-signal-name" style="color:' + COLORS.muted + '">No strong signals</span></div>';
  }
  bearishHTML += '</div>';

  signalsPanel.innerHTML = bullishHTML + bearishHTML;
  chartRow.appendChild(signalsPanel);
  wrapper.appendChild(chartRow);

  container.appendChild(wrapper);

  renderGaugeChart(gaugeContainer, predictions);
}

function renderGaugeChart(container, predictions) {
  const chart = initChart(container);

  const seriesData = TF_ORDER.map(tf => {
    const p = predictions[tf];
    if (!p) return null;
    const val = p.direction === 'up' ? p.confidence * 100 : (p.direction === 'down' ? (1 - p.confidence) * 100 : 50);
    return {
      name: TF_LABELS[tf],
      value: parseFloat(val.toFixed(1)),
    };
  }).filter(Boolean);

  const barColors = seriesData.map(d => {
    if (d.value > 55) return COLORS.green;
    if (d.value < 45) return COLORS.red;
    return COLORS.yellow;
  });

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter(params) {
        const p = params[0];
        const dir = p.value > 55 ? 'Bullish' : p.value < 45 ? 'Bearish' : 'Neutral';
        const color = p.value > 55 ? COLORS.green : p.value < 45 ? COLORS.red : COLORS.yellow;
        return `<strong>${p.name}</strong><br/>
          <span style="color:${color}">${dir}</span>: ${p.value}% confidence`;
      },
    },
    grid: {
      left: 12, right: 12, top: 16, bottom: 4,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: seriesData.map(d => d.name),
      axisLabel: { color: COLORS.muted, fontSize: 11, fontWeight: 600 },
      axisLine: { lineStyle: { color: COLORS.border } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      splitNumber: 4,
      axisLabel: {
        color: COLORS.muted,
        fontSize: 10,
        formatter: '{value}%',
      },
      splitLine: { lineStyle: { color: COLORS.border, type: 'dashed' } },
      axisLine: { show: false },
    },
    series: [{
      type: 'bar',
      data: seriesData.map((d, i) => ({
        value: d.value,
        itemStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: barColors[i] },
              { offset: 1, color: barColors[i] + '44' },
            ],
          },
          borderRadius: [4, 4, 0, 0],
        },
      })),
      barWidth: '50%',
      label: {
        show: true,
        position: 'top',
        color: COLORS.text,
        fontSize: 12,
        fontWeight: 700,
        formatter: p => {
          const dir = p.value > 55 ? '▲' : p.value < 45 ? '▼' : '—';
          return `${dir} ${p.value}%`;
        },
      },
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { color: COLORS.muted, type: 'dashed', width: 1 },
        label: { show: false },
        data: [{ yAxis: 50 }],
      },
    }],
  });

  autoResize(chart, container);
  return chart;
}
