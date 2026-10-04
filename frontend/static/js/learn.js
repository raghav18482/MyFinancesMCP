// Renders the metrics guide straight from data/metric_glossary.json — the same
// file the Python scorer grades against, so the "ideal range" shown here is
// always the threshold actually used. Nothing on this page is LLM-generated.

const $ = id => document.getElementById(id);

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const PILLAR_ORDER = ['valuation', 'profitability', 'balance_sheet', 'growth'];

function renderIntro(intro) {
  $('learn-intro').innerHTML = (intro || []).map((item, n) => `
    <div class="learn-rule">
      <span class="learn-rule-num">${n + 1}</span>
      <div>
        <h3>${esc(item.title)}</h3>
        <p>${esc(item.body)}</p>
      </div>
    </div>`).join('');
}

function metricsFor(metrics, pillarKey) {
  return Object.entries(metrics)
    .filter(([, m]) => m.pillar === pillarKey)
    .map(([key, m]) => ({ key, ...m }));
}

function renderToc(glossary) {
  const groups = PILLAR_ORDER.map(key => {
    const pillar = glossary.pillars[key];
    const links = metricsFor(glossary.metrics, key)
      .map(m => `<a href="#${esc(m.key)}">${esc(m.label)}</a>`)
      .join('');
    return `
      <div class="learn-toc-group">
        <span class="learn-toc-label">${esc(pillar.label)}</span>
        <div class="learn-toc-links">${links}</div>
      </div>`;
  }).join('');
  $('learn-toc').innerHTML = groups;
}

/** Turn a metric's bands into a readable threshold table. */
function bandTable(bands) {
  if (!bands || !bands.length) return '';

  const rows = bands.map((band, i) => {
    const prev = i > 0 ? bands[i - 1].max : null;
    let range;
    if (band.max === null) {
      range = prev === null ? 'any value' : `above ${prev}`;
    } else if (prev === null) {
      range = `up to ${band.max}`;
    } else {
      range = `${prev} to ${band.max}`;
    }
    return `
      <tr class="band-${esc(band.verdict)}">
        <td class="band-range">${esc(range)}</td>
        <td class="band-verdict"><span class="band-pill band-${esc(band.verdict)}">${esc(band.verdict)}</span></td>
        <td class="band-note">${esc(band.note)}</td>
      </tr>`;
  }).join('');

  return `
    <details class="learn-bands">
      <summary>How this metric is graded</summary>
      <table class="learn-band-table">
        <thead><tr><th>Value</th><th>Verdict</th><th>Reading</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </details>`;
}

function renderMetric(m) {
  const scored = (m.weight || 0) > 0;
  return `
    <article class="learn-metric card" id="${esc(m.key)}">
      <div class="learn-metric-head">
        <div>
          <h3 class="learn-metric-title">${esc(m.label)}</h3>
          <span class="learn-metric-full">${esc(m.full_name)}</span>
        </div>
        ${scored
          ? '<span class="learn-metric-tag is-scored">Counts toward the score</span>'
          : '<span class="learn-metric-tag">Shown for context only</span>'}
      </div>

      ${m.formula ? `<p class="learn-metric-formula">${esc(m.formula)}</p>` : ''}
      <p class="learn-metric-plain">${esc(m.plain)}</p>

      <div class="learn-metric-facts">
        ${m.ideal ? `
          <div class="learn-fact is-ideal">
            <span class="learn-fact-label">Ideal</span>
            <span class="learn-fact-value">${esc(m.ideal)}</span>
          </div>` : ''}
        ${m.caveat ? `
          <div class="learn-fact">
            <span class="learn-fact-label">Context</span>
            <span class="learn-fact-value">${esc(m.caveat)}</span>
          </div>` : ''}
        ${m.watch_out ? `
          <div class="learn-fact is-warn">
            <span class="learn-fact-label">Watch out</span>
            <span class="learn-fact-value">${esc(m.watch_out)}</span>
          </div>` : ''}
      </div>

      ${scored ? bandTable(m.bands) : ''}
    </article>`;
}

function renderPillars(glossary) {
  $('learn-pillars').innerHTML = PILLAR_ORDER.map(key => {
    const pillar = glossary.pillars[key];
    const metrics = metricsFor(glossary.metrics, key);
    return `
      <section class="learn-section" id="pillar-${esc(key)}">
        <div class="learn-section-head">
          <h2 class="learn-section-title">${esc(pillar.label)}</h2>
          <span class="learn-section-tagline">${esc(pillar.tagline)}</span>
        </div>
        <p class="learn-section-body">${esc(pillar.body)}</p>
        <div class="learn-metrics">${metrics.map(renderMetric).join('')}</div>
      </section>`;
  }).join('');
}

function renderScoring(scoring) {
  $('learn-steps').innerHTML = (scoring.how_it_works || [])
    .map(step => `<li>${esc(step)}</li>`).join('');

  $('learn-grades').innerHTML = `
    <span class="learn-grades-label">Grade bands</span>
    <div class="learn-grade-chips">
      ${(scoring.grades || []).map(g => `
        <span class="learn-grade-chip grade-${esc(String(g.grade).toLowerCase())}">
          <strong>${esc(g.grade)}</strong>
          <span>${esc(g.label)}</span>
          <em>${esc(g.min)}+</em>
        </span>`).join('')}
    </div>`;

  $('learn-limits').innerHTML = `
    <span class="learn-limits-label">What the score cannot see</span>
    <p>${esc(scoring.limits)}</p>`;
}

/** Jumping to #metric from a research-page popover should highlight the entry. */
function highlightHash() {
  const id = decodeURIComponent(window.location.hash.slice(1));
  if (!id) return;
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('is-targeted');
  setTimeout(() => el.classList.remove('is-targeted'), 2400);
}

async function init() {
  try {
    const res = await fetch('/static/data/metric_glossary.json');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const glossary = await res.json();

    renderIntro(glossary.intro);
    renderToc(glossary);
    renderPillars(glossary);
    renderScoring(glossary.scoring || {});

    $('learn-loading').style.display = 'none';
    $('learn-content').style.display = 'block';

    highlightHash();
    window.addEventListener('hashchange', highlightHash);
  } catch (err) {
    $('learn-loading').style.display = 'none';
    const el = $('learn-error');
    el.textContent = 'Could not load the metrics guide: ' + err.message;
    el.style.display = 'block';
  }
}

init();
