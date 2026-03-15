const $ = id => document.getElementById(id);

function fmtCurrency(v) {
  if (v == null) return '—';
  const abs = Math.abs(v);
  if (abs >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (abs >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  return '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}

function fmtNum(v, d = 2) {
  if (v == null || v !== v) return '—';
  return Number(v).toLocaleString('en-IN', { maximumFractionDigits: d, minimumFractionDigits: d });
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Market Breadth ──
async function loadBreadth() {
  try {
    const data = await fetchJSON('/api/sectors/breadth');
    $('breadth-loading').style.display = 'none';

    if (data.error) {
      $('breadth-error').textContent = data.error;
      $('breadth-error').style.display = 'block';
      return;
    }

    const stats = [];

    if (data.india_vix) {
      const vix = data.india_vix;
      const cls = vix.current > 20 ? 'negative' : vix.current < 14 ? 'positive' : '';
      stats.push(`
        <div class="stat">
          <div class="label">India VIX</div>
          <div class="value ${cls}">${fmtNum(vix.current, 2)}</div>
        </div>`);
    }

    if (data.week52_highs != null) {
      stats.push(`
        <div class="stat">
          <div class="label">52W Highs</div>
          <div class="value positive">${data.week52_highs}</div>
        </div>`);
    }

    if (data.week52_lows != null) {
      stats.push(`
        <div class="stat">
          <div class="label">52W Lows</div>
          <div class="value negative">${data.week52_lows}</div>
        </div>`);
    }

    if (data.sector_heatmap && data.sector_heatmap.length) {
      const gainers = data.sector_heatmap.filter(s => s.change > 0).length;
      const losers = data.sector_heatmap.filter(s => s.change < 0).length;
      const ratio = losers > 0 ? (gainers / losers).toFixed(2) : gainers;
      stats.push(`
        <div class="stat">
          <div class="label">Adv / Dec (Indices)</div>
          <div class="value">${gainers} / ${losers} (${ratio})</div>
        </div>`);
    }

    $('breadth-stats').innerHTML = stats.join('');

    // FII/DII table
    if (data.fii_dii && data.fii_dii.length) {
      const fiiSection = $('fii-dii-section');
      fiiSection.style.display = 'block';
      const table = $('fii-dii-table');
      const cols = Object.keys(data.fii_dii[0]);
      table.querySelector('thead tr').innerHTML = cols.map(c => `<th>${c}</th>`).join('');
      table.querySelector('tbody').innerHTML = data.fii_dii.map(row =>
        '<tr>' + cols.map(c => `<td>${row[c] ?? '—'}</td>`).join('') + '</tr>'
      ).join('');
    }

    $('breadth-content').style.display = 'block';

    // Render heatmap chart or show empty state
    if (data.sector_heatmap && data.sector_heatmap.length) {
      renderHeatmap(data.sector_heatmap);
    } else {
      $('chart-heatmap').innerHTML = '<div class="chart-empty">No live index data available (market may be closed)</div>';
    }
  } catch (err) {
    $('breadth-loading').style.display = 'none';
    $('breadth-error').textContent = 'Failed to load market breadth: ' + err.message;
    $('breadth-error').style.display = 'block';
    $('chart-heatmap').innerHTML = `<div class="chart-empty">${err.message}</div>`;
  }
}

function renderHeatmap(heatmap) {
  const container = $('chart-heatmap');
  const chart = echarts.init(container);

  const filtered = heatmap.filter(h =>
    !h.name.includes('Broad') && !h.name.includes('Strategy') && !h.name.includes('Fixed')
  ).slice(0, 25);

  const names = filtered.map(h => h.name);
  const values = filtered.map(h => h.change);
  const maxAbs = Math.max(...values.map(Math.abs), 1);

  chart.setOption({
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: p => `${p[0].name}<br/>Change: <b>${p[0].value >= 0 ? '+' : ''}${p[0].value.toFixed(2)}%</b>`,
    },
    grid: { top: 10, right: 20, bottom: 10, left: 160, containLabel: false },
    xAxis: {
      type: 'value',
      axisLabel: { color: '#9ca3af', fontSize: 10, formatter: v => v + '%' },
      splitLine: { lineStyle: { color: '#2e3143' } },
      axisLine: { lineStyle: { color: '#2e3143' } },
    },
    yAxis: {
      type: 'category', data: names.reverse(), inverse: false,
      axisLabel: { color: '#e4e4e7', fontSize: 11, width: 150, overflow: 'truncate' },
      axisLine: { lineStyle: { color: '#2e3143' } },
    },
    series: [{
      type: 'bar',
      data: values.reverse().map(v => ({
        value: v,
        itemStyle: {
          color: v >= 0
            ? `rgba(34,197,94,${0.3 + 0.7 * Math.abs(v) / maxAbs})`
            : `rgba(239,68,68,${0.3 + 0.7 * Math.abs(v) / maxAbs})`,
          borderRadius: v >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
        },
      })),
      barWidth: '65%',
      label: {
        show: true, position: 'insideRight',
        formatter: p => `${p.value >= 0 ? '+' : ''}${p.value.toFixed(2)}%`,
        color: '#e4e4e7', fontSize: 10,
      },
    }],
  });

  window.addEventListener('resize', () => chart.resize());
}

// ── Sector Cards ──
async function loadSectors() {
  try {
    const data = await fetchJSON('/api/sectors/overview');
    $('sectors-loading').style.display = 'none';

    if (data.error) {
      $('sectors-error').textContent = data.error;
      $('sectors-error').style.display = 'block';
      return;
    }

    const sectors = data.sectors || [];
    if (!sectors.length) {
      $('sectors-container').innerHTML = '<p style="color:var(--muted);text-align:center;padding:2rem">No sector data available.</p>';
      return;
    }

    $('sectors-container').innerHTML = sectors.map((s, i) => renderSectorCard(s, i >= 3)).join('');
  } catch (err) {
    $('sectors-loading').style.display = 'none';
    $('sectors-error').textContent = 'Failed to load sectors: ' + err.message;
    $('sectors-error').style.display = 'block';
  }
}

function renderSectorCard(sector, collapsed) {
  const perf = sector.index_performance || {};
  const perfPills = ['1w', '1m', '3m', '6m', '1y'].map(p => {
    const v = perf[p];
    if (v == null) return '';
    const cls = v >= 0 ? 'positive' : 'negative';
    return `<span class="perf-pill ${cls}">${p.toUpperCase()}: ${v >= 0 ? '+' : ''}${v}%</span>`;
  }).join('');

  const holdingsHtml = (sector.holdings || []).map(h => {
    const pnlCls = (h.pnl_pct || 0) >= 0 ? 'positive' : 'negative';
    return `<div class="sector-holding">
      <span class="sh-sym">${(h.symbol || '').replace('-EQ', '')}</span>
      <span class="sh-price">₹${fmtNum(h.ltp)}</span>
      <span class="sh-pnl ${pnlCls}">${(h.pnl_pct || 0) >= 0 ? '+' : ''}${fmtNum(h.pnl_pct, 1)}%</span>
    </div>`;
  }).join('');

  const driver = sector.driver
    ? `<div class="sector-driver"><strong>Key Driver</strong><p>${sector.driver}</p></div>`
    : '';

  return `
    <div class="sector-card${collapsed ? ' collapsed' : ''}">
      <div class="sector-card-head" onclick="this.parentElement.classList.toggle('collapsed')">
        <div class="sector-card-left">
          <h2>${sector.name}</h2>
          <span class="news-badge">${sector.weight}% of portfolio</span>
          <span class="news-badge" style="background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.25);color:#22c55e">${fmtCurrency(sector.invested)}</span>
          <span style="font-size:.75rem;color:var(--muted)">${sector.holdings_count} stock${sector.holdings_count !== 1 ? 's' : ''}</span>
        </div>
        <span class="news-chevron"></span>
      </div>
      <div class="sector-card-body">
        <div class="sector-grid">
          <div>
            <h3 class="sector-subhead">${sector.index_name}</h3>
            <div class="perf-pills">${perfPills || '<span style="color:var(--muted);font-size:.8rem">No index data</span>'}</div>
          </div>
          <div>
            <h3 class="sector-subhead">Your Holdings</h3>
            <div class="sector-holdings-list">${holdingsHtml || '<span style="color:var(--muted);font-size:.8rem">—</span>'}</div>
          </div>
        </div>
        ${driver}
      </div>
    </div>`;
}

// ── Init ──
loadBreadth();
loadSectors();
