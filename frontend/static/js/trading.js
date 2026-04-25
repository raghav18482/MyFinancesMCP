/*
 * Trading page – holdings dropdown, candlestick chart, live ticks, proposals.
 * Self-contained: no static imports that can break from stale browser caches.
 */

(async function () {
  // ── Dynamic imports with cache-busting ──────────────────
  var _v = Date.now();
  var theme, chartMod;
  try {
    theme = await import('./utils/theme.js?_=' + _v);
  } catch (e) {
    console.error('theme import failed', e);
    return;
  }

  var COLORS        = theme.COLORS;
  var formatCurrency = theme.formatCurrency;
  var initChart      = theme.initChart;
  var autoResize     = theme.autoResize;

  // ── DOM refs ────────────────────────────────────────────
  var $chartContainer = document.getElementById('rt-chart');
  var $holdingsSelect = document.getElementById('rt-holdings-select');
  var $symbolInput    = document.getElementById('rt-symbol');
  var $symbolBtn      = document.getElementById('rt-symbol-btn');
  var $ltpDisplay     = document.getElementById('rt-ltp');
  var $changeDisplay  = document.getElementById('rt-change');
  var $symbolLabel    = document.getElementById('rt-symbol-label');

  var wsSid = (window.__WS_SID__ || '').trim();

  var chartState = null;
  var ws = null;
  var baseLtp = null;
  var holdingsCache = [];
  var loadGeneration = 0;

  function disposeRtChart(dom) {
    if (!dom || !window.echarts) return;
    try {
      var inst = window.echarts.getInstanceByDom(dom);
      if (inst) inst.dispose();
    } catch (e) { /* ignore */ }
  }

  // ── Candlestick chart (inline, same as analytics) ──────

  function renderCandlestickChart(container, candles, stockName, avgBuyPrice) {
    if (!candles || !candles.length) {
      container.innerHTML = '<div class="chart-empty">No candle data. Select a stock above.</div>';
      return null;
    }

    disposeRtChart(container);
    container.innerHTML = '';
    var chart = initChart(container);
    var dates   = candles.map(function (c) { return c[0].split('T')[0]; });
    var ohlc    = candles.map(function (c) { return [c[1], c[2], c[3], c[4]]; });
    var volumes = candles.map(function (c) { return c[5] || 0; });
    var lastClose = candles[candles.length - 1][4];

    var markLine = avgBuyPrice ? {
      silent: true, symbol: 'none',
      lineStyle: { color: COLORS.yellow, type: 'dashed', width: 2 },
      label: {
        formatter: 'Buy Avg: ' + formatCurrency(avgBuyPrice),
        color: COLORS.yellow, fontSize: 11, fontWeight: 600,
        backgroundColor: 'rgba(234,179,8,.12)', padding: [4, 8], borderRadius: 4,
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
          var chg = close - open;
          var pct = open ? ((chg / open) * 100).toFixed(2) : '0.00';
          var col = chg >= 0 ? COLORS.green : COLORS.red;
          return '<div style="font-family:Inter,sans-serif;font-size:13px">' +
            '<strong>' + c.axisValue + '</strong><br/>' +
            'O: ' + formatCurrency(open) + ' &nbsp; H: ' + formatCurrency(high) + '<br/>' +
            'L: ' + formatCurrency(low) + ' &nbsp; C: ' + formatCurrency(close) + '<br/>' +
            '<span style="color:' + col + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + ' (' + pct + '%)</span></div>';
        },
      },
      grid: [
        { left: 65, right: 20, top: 20, height: '62%' },
        { left: 65, right: 20, top: '76%', height: '17%' },
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0,
          axisLabel: { color: COLORS.muted, fontSize: 10 },
          axisLine: { lineStyle: { color: COLORS.border } }, splitLine: { show: false } },
        { type: 'category', data: dates, gridIndex: 1,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: COLORS.border } }, splitLine: { show: false } },
      ],
      yAxis: [
        { scale: true, gridIndex: 0,
          axisLabel: { color: COLORS.muted, fontSize: 11 },
          splitLine: { lineStyle: { color: COLORS.border, type: 'dashed' } }, axisLine: { show: false } },
        { scale: true, gridIndex: 1,
          axisLabel: { show: false }, splitLine: { show: false }, axisLine: { show: false } },
      ],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 50, end: 100 }],
      series: [
        {
          type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: COLORS.green, color0: COLORS.red, borderColor: COLORS.green, borderColor0: COLORS.red },
          markLine: markLine,
        },
        {
          type: 'bar', xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 6,
          data: volumes.map(function (v, i) {
            return { value: v, itemStyle: { color: ohlc[i][1] >= ohlc[i][0] ? 'rgba(34,197,94,.3)' : 'rgba(239,68,68,.3)' } };
          }),
        },
      ],
    });

    autoResize(chart, container);
    requestAnimationFrame(function () {
      try { chart.resize(); } catch (e) { /* ignore */ }
    });
    return { chart: chart, dates: dates, ohlc: ohlc, volumes: volumes, lastClose: lastClose };
  }

  function pushLiveTick(state, tick) {
    if (!state || !state.chart || !tick.ltp || tick.ltp <= 0) return;
    var today = new Date().toISOString().split('T')[0];
    var dates = state.dates, ohlc = state.ohlc, volumes = state.volumes;

    if (dates.length > 0 && dates[dates.length - 1] === today) {
      var last = ohlc[ohlc.length - 1];
      last[1] = tick.ltp;
      if (tick.ltp > last[3]) last[3] = tick.ltp;
      if (tick.ltp < last[2]) last[2] = tick.ltp;
    } else {
      dates.push(today);
      ohlc.push([tick.ltp, tick.ltp, tick.ltp, tick.ltp]);
      volumes.push(0);
    }

    state.chart.setOption({
      xAxis: [{ data: dates }, { data: dates }],
      series: [
        { data: ohlc },
        { data: volumes.map(function (v, i) {
            return { value: v, itemStyle: { color: ohlc[i][1] >= ohlc[i][0] ? 'rgba(34,197,94,.3)' : 'rgba(239,68,68,.3)' } };
          }),
        },
      ],
    });
  }

  // ── Load holdings into dropdown ─────────────────────────

  async function loadHoldings() {
    var holdings = window.__HOLDINGS__ || [];

    if (!holdings.length) {
      try {
        var res = await fetch('/api/portfolio/analytics');
        if (res.ok) {
          var data = await res.json();
          holdings = data.holdings || [];
        }
      } catch (e) { /* fallback failed, stay empty */ }
    }

    holdingsCache = holdings.slice().sort(function (a, b) { return b.current - a.current; });
    $holdingsSelect.innerHTML = '';

    if (!holdingsCache.length) {
      $holdingsSelect.innerHTML = '<option value="">No holdings found</option>';
      return;
    }

    var defOpt = document.createElement('option');
    defOpt.value = '';
    defOpt.textContent = 'Select a stock\u2026';
    $holdingsSelect.appendChild(defOpt);

    holdingsCache.forEach(function (h) {
      var opt = document.createElement('option');
      opt.value = h.symbol;
      var sign = h.pnl >= 0 ? '+' : '';
      opt.textContent = h.symbol.replace('-EQ', '') + '  ' + formatCurrency(h.ltp) + '  (' + sign + h.pnl_pct.toFixed(1) + '%)';
      $holdingsSelect.appendChild(opt);
    });

    $holdingsSelect.value = holdingsCache[0].symbol;
    loadSymbol(holdingsCache[0].symbol);
  }

  // ── Load chart + live feed for a symbol ─────────────────

  async function loadSymbol(symbol) {
    if (!symbol) return;
    var gen = ++loadGeneration;

    if (ws) {
      try { ws.close(); } catch (e) { /* ignore */ }
      ws = null;
    }
    if (chartState && chartState.chart) {
      try { chartState.chart.dispose(); } catch (e) { /* ignore */ }
    }
    chartState = null;
    baseLtp = null;

    $ltpDisplay.textContent = '--';
    $changeDisplay.textContent = '';
    $changeDisplay.className = 'rt-change';
    $symbolLabel.textContent = symbol.replace('-EQ', '');
    disposeRtChart($chartContainer);
    $chartContainer.innerHTML = '<div class="chart-loading"><div class="spinner"></div></div>';

    var h = holdingsCache.find(function (x) { return x.symbol === symbol; });
    var avgPrice = h ? h.avg_price : null;

    try {
      var res = await fetch('/api/portfolio/candles?symbol=' + encodeURIComponent(symbol) + '&exchange=NSE&days=90&interval=ONE_DAY');
      var data = await res.json();
      if (gen !== loadGeneration) return;

      if (data.error) {
        $chartContainer.innerHTML = '<div class="chart-empty">' + escapeHtml(data.error) + '</div>';
        return;
      }
      if (data.candles && data.candles.length) {
        try {
          chartState = renderCandlestickChart($chartContainer, data.candles, symbol.replace('-EQ', ''), avgPrice);
        } catch (err) {
          console.error('Chart render failed', err);
          $chartContainer.innerHTML = '<div class="chart-empty">Chart error: ' + escapeHtml(err.message) + '</div>';
          return;
        }
        if (gen !== loadGeneration) return;
        baseLtp = data.candles[data.candles.length - 1][4];
        $ltpDisplay.textContent = formatCurrency(baseLtp);
      } else {
        $chartContainer.innerHTML = '<div class="chart-empty">No candle data for ' + escapeHtml(symbol) + '</div>';
        return;
      }
    } catch (e) {
      if (gen !== loadGeneration) return;
      $chartContainer.innerHTML = '<div class="chart-empty">Failed to load chart: ' + escapeHtml(e.message) + '</div>';
      return;
    }

    if (gen !== loadGeneration) return;
    startLiveFeed(symbol, gen);
  }

  function startLiveFeed(symbol, gen) {
    if (!wsSid) return;
    var h = holdingsCache.find(function (x) { return x.symbol === symbol; });
    var tok = h && h.symboltoken ? String(h.symboltoken).trim() : '';
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/market/' + encodeURIComponent(symbol) + '?sid=' + encodeURIComponent(wsSid);
    if (tok && /^\d+$/.test(tok)) {
      url += '&symboltoken=' + encodeURIComponent(tok);
    }
    var socket = new WebSocket(url);
    ws = socket;

    socket.onmessage = function (e) {
      if (gen !== loadGeneration || socket !== ws) return;
      try {
        var msg = JSON.parse(e.data);
        if (msg.error) {
          console.warn('Live feed:', msg.error);
          return;
        }
        if (msg.status === 'subscribed') return;
        if (msg.ltp && msg.ltp > 0) {
          $ltpDisplay.textContent = formatCurrency(msg.ltp);
          if (baseLtp && baseLtp > 0) {
            var change = msg.ltp - baseLtp;
            var pct = (change / baseLtp * 100).toFixed(2);
            var sign = change >= 0 ? '+' : '';
            $changeDisplay.textContent = sign + change.toFixed(2) + ' (' + sign + pct + '%)';
            $changeDisplay.className = 'rt-change ' + (change >= 0 ? 'rt-change--up' : 'rt-change--down');
          }
          pushLiveTick(chartState, msg);
        }
      } catch (err) { /* ignore parse errors */ }
    };
    socket.onerror = function () { console.warn('WS error for', symbol); };
    socket.onclose = function () { console.debug('WS closed for', symbol); };
  }

  // ── Event listeners ─────────────────────────────────────

  $holdingsSelect.addEventListener('change', function () {
    var sym = $holdingsSelect.value;
    if (sym) { $symbolInput.value = ''; loadSymbol(sym); }
  });

  $symbolBtn.addEventListener('click', function () {
    var sym = $symbolInput.value.trim().toUpperCase();
    if (sym) { $holdingsSelect.value = ''; loadSymbol(sym); }
  });

  $symbolInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      var sym = $symbolInput.value.trim().toUpperCase();
      if (sym) { $holdingsSelect.value = ''; loadSymbol(sym); }
    }
  });

  loadHoldings();

  // ── Proposals panel ─────────────────────────────────────

  var $proposals = document.getElementById('proposals-list');

  async function refreshProposals() {
    try {
      var res = await fetch('/api/trading/proposals');
      var data = await res.json();
      if (!data.proposals || !data.proposals.length) {
        $proposals.innerHTML = '<p class="proposals-empty">No proposals yet. Chat with the trading agent to get trade ideas.</p>';
        return;
      }
      $proposals.innerHTML = data.proposals.map(function (p) {
        var statusClass = 'proposal-status--' + p.status;
        var canAct = p.status === 'pending';
        return '<div class="proposal-card">' +
          '<div class="proposal-summary">' + escapeHtml(p.summary) + '</div>' +
          '<div class="proposal-meta">' +
            '<span class="proposal-id">' + p.proposal_id + '</span>' +
            '<span class="proposal-status ' + statusClass + '">' + p.status.toUpperCase() + '</span>' +
          '</div>' +
          (canAct ? '<div class="proposal-actions">' +
            '<button class="btn btn-sm btn-green proposal-approve-btn" data-id="' + p.proposal_id + '">Approve</button>' +
            '<button class="btn btn-sm btn-danger proposal-reject-btn" data-id="' + p.proposal_id + '">Reject</button>' +
          '</div>' : '') +
          (p.order_id ? '<div class="proposal-result">Order ID: ' + p.order_id + '</div>' : '') +
          (p.error ? '<div class="proposal-error">' + escapeHtml(p.error) + '</div>' : '') +
        '</div>';
      }).join('');

      $proposals.querySelectorAll('.proposal-approve-btn').forEach(function (btn) {
        btn.addEventListener('click', function () { handleApprove(btn.dataset.id); });
      });
      $proposals.querySelectorAll('.proposal-reject-btn').forEach(function (btn) {
        btn.addEventListener('click', function () { handleReject(btn.dataset.id); });
      });
    } catch (e) { /* ignore */ }
  }

  async function handleApprove(id) {
    if (!confirm('Execute this trade? This will place a REAL order on Angel One.')) return;
    try {
      var res = await fetch('/api/trading/proposals/' + id + '/approve', { method: 'POST' });
      var data = await res.json();
      if (data.ok) alert('Order executed! Order ID: ' + (data.order_id || 'N/A'));
      else alert('Error: ' + (data.error || 'Unknown'));
    } catch (e) { alert('Network error: ' + e.message); }
    refreshProposals();
  }

  async function handleReject(id) {
    try { await fetch('/api/trading/proposals/' + id + '/reject', { method: 'POST' }); }
    catch (e) { /* ignore */ }
    refreshProposals();
  }

  refreshProposals();
  setInterval(refreshProposals, 8000);

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
})();
