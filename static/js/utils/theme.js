const COLORS = {
  bg:      '#0f1117',
  surface: '#1a1d27',
  surface2:'#242734',
  border:  '#2e3143',
  text:    '#e4e4e7',
  muted:   '#9ca3af',
  accent:  '#6366f1',
  green:   '#22c55e',
  red:     '#ef4444',
  yellow:  '#eab308',
  cyan:    '#06b6d4',
  orange:  '#f97316',
  pink:    '#ec4899',
};

const SECTOR_PALETTE = [
  '#6366f1', '#22c55e', '#f97316', '#06b6d4', '#ec4899',
  '#eab308', '#8b5cf6', '#14b8a6', '#f43f5e', '#0ea5e9',
  '#a855f7', '#84cc16', '#ef4444', '#64748b', '#d946ef',
];

function pnlColor(value) {
  if (value > 0) return COLORS.green;
  if (value < 0) return COLORS.red;
  return COLORS.muted;
}

function pnlGradient(value, intensity) {
  const t = Math.min(Math.abs(intensity), 1);
  if (value > 0) {
    const r = Math.round(15 + t * (34 - 15));
    const g = Math.round(20 + t * (197 - 20));
    const b = Math.round(25 + t * (94 - 25));
    return `rgb(${r},${g},${b})`;
  }
  const r = Math.round(60 + t * (239 - 60));
  const g = Math.round(20 + t * (20));
  const b = Math.round(25 + t * (68 - 25));
  return `rgb(${r},${g},${b})`;
}

function formatCurrency(val) {
  if (val == null || isNaN(val)) return 'N/A';
  return '\u20b9' + Number(val).toLocaleString('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function formatPercent(val) {
  if (val == null || isNaN(val)) return 'N/A';
  return val.toFixed(2) + '%';
}

function initChart(container) {
  return echarts.init(container, null, { renderer: 'canvas' });
}

function autoResize(chart, container) {
  const ro = new ResizeObserver(() => chart.resize());
  ro.observe(container);
  return ro;
}

export { COLORS, SECTOR_PALETTE, pnlColor, pnlGradient, formatCurrency, formatPercent, initChart, autoResize };
