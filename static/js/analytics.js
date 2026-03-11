import { renderTreemap } from './charts/treemap.js';
import { renderAllocation } from './charts/allocation.js';
import { renderGainersLosers } from './charts/gainers-losers.js';
import { renderCashGauge } from './charts/cash-gauge.js';
import { renderCandlestick } from './charts/candlestick.js';
import { renderBetaScatter } from './charts/beta-scatter.js';
import { renderRebalancer } from './charts/rebalancer.js';

const $ = id => document.getElementById(id);

function showLoading(el) {
  el.innerHTML = '<div class="chart-loading"><div class="spinner"></div></div>';
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function main() {
  const containers = {
    treemap:   $('chart-treemap'),
    allocation:$('chart-allocation'),
    gainers:   $('chart-gainers'),
    gauge:     $('chart-gauge'),
    candle:    $('chart-candle'),
    beta:      $('chart-beta'),
    rebalancer:$('chart-rebalancer'),
  };

  Object.values(containers).forEach(showLoading);

  let portfolio, sectorMap;

  try {
    [portfolio, sectorMap] = await Promise.all([
      fetchJSON('/api/portfolio/analytics'),
      fetchJSON('/static/data/sector_map.json'),
    ]);
  } catch (err) {
    Object.values(containers).forEach(el => {
      el.innerHTML = `<div class="chart-empty">Failed to load data: ${err.message}</div>`;
    });
    return;
  }

  if (portfolio.error) {
    Object.values(containers).forEach(el => {
      el.innerHTML = `<div class="chart-empty">${portfolio.error}</div>`;
    });
    return;
  }

  const { holdings, summary, funds } = portfolio;

  renderTreemap(containers.treemap, holdings);
  renderAllocation(containers.allocation, holdings, sectorMap);
  renderGainersLosers(containers.gainers, holdings);
  renderCashGauge(containers.gauge, funds, summary);
  renderRebalancer(containers.rebalancer, holdings, funds);

  setupCandlestickPicker(containers.candle, holdings);
  loadBetaChart(containers.beta);
}

function setupCandlestickPicker(container, holdings) {
  const picker = $('stock-picker');
  if (!picker || !holdings.length) {
    container.innerHTML = '<div class="chart-empty">No holdings to display</div>';
    return;
  }

  for (const h of holdings) {
    const opt = document.createElement('option');
    opt.value = h.symbol;
    opt.textContent = h.symbol.replace('-EQ', '');
    opt.dataset.avg = h.avg_price;
    opt.dataset.token = h.symboltoken || '';
    picker.appendChild(opt);
  }

  picker.addEventListener('change', () => loadCandle(container, picker, holdings));

  if (holdings.length > 0) {
    picker.value = holdings[0].symbol;
    loadCandle(container, picker, holdings);
  }
}

async function loadCandle(container, picker, holdings) {
  const symbol = picker.value;
  if (!symbol) return;

  const h = holdings.find(x => x.symbol === symbol);
  const avgPrice = h ? h.avg_price : null;

  showLoading(container);

  try {
    const res = await fetch('/api/portfolio/candles?' + new URLSearchParams({
      symbol,
      exchange: 'NSE',
      interval: 'ONE_DAY',
      days: '90',
    }));
    const data = await res.json();

    if (data.error) {
      container.innerHTML = `<div class="chart-empty">${data.error}</div>`;
      return;
    }

    renderCandlestick(container, data.candles, symbol.replace('-EQ', ''), avgPrice);
  } catch (err) {
    container.innerHTML = `<div class="chart-empty">Failed to load candle data: ${err.message}</div>`;
  }
}

async function loadBetaChart(container) {
  showLoading(container);
  try {
    const data = await fetchJSON('/api/portfolio/beta?days=90');
    if (data.error) {
      container.innerHTML = `<div class="chart-empty">${data.error}</div>`;
      return;
    }
    renderBetaScatter(container, data);
  } catch (err) {
    container.innerHTML = `<div class="chart-empty">Beta computation takes a moment — ${err.message}</div>`;
  }
}

main();
