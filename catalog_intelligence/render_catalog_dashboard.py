"""
Render the catalog intelligence JSON payload as a self-contained HTML dashboard.

Usage:
    .venv\\Scripts\\python.exe catalog_intelligence\\render_catalog_dashboard.py --family EN
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Catalog Intelligence Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --bg: #f5f6f4;
    --panel: #ffffff;
    --panel-2: #f9faf8;
    --text: #111412;
    --muted: #66706a;
    --soft: #8b948e;
    --line: #dfe4df;
    --line-strong: #c5cdc6;
    --blue: #2563eb;
    --teal: #00838f;
    --green: #168a42;
    --amber: #aa6a00;
    --red: #c33b35;
    --ink: #1f2937;
    --chip: #eef2f0;
    --shadow: 0 10px 28px rgba(17, 20, 18, 0.08);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --bg: #101211;
      --panel: #191c1a;
      --panel-2: #141715;
      --text: #f4f7f5;
      --muted: #b0bab3;
      --soft: #818b84;
      --line: #2d332f;
      --line-strong: #455047;
      --chip: #232a25;
      --shadow: none;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  body { min-width: 320px; }
  .shell { width: min(1460px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 60px; }
  .topbar { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: start; margin-bottom: 18px; }
  h1 { margin: 0; font-size: 24px; line-height: 1.15; letter-spacing: 0; }
  h2 { margin: 0; font-size: 16px; line-height: 1.25; letter-spacing: 0; }
  h3 { margin: 0; font-size: 13px; line-height: 1.25; letter-spacing: 0; }
  .subtitle { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
  .note-pill { border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; color: var(--muted); font-size: 12px; background: var(--panel); white-space: nowrap; }
  .kpis { display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; min-height: 74px; }
  .kpi .label { color: var(--muted); font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: .04em; }
  .kpi .value { margin-top: 4px; font-size: 24px; font-weight: 720; font-variant-numeric: tabular-nums; }
  .kpi .sub { margin-top: 2px; color: var(--soft); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; margin-top: 14px; box-shadow: var(--shadow); }
  .card-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
  .desc { color: var(--muted); font-size: 12px; margin: 4px 0 0; max-width: 920px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .tree-layout { display: grid; grid-template-columns: 310px 1fr; gap: 16px; align-items: start; }
  .controls { border-right: 1px solid var(--line); padding-right: 16px; position: sticky; top: 12px; }
  .field { margin-bottom: 12px; }
  label { display: block; color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }
  input, select, button { font: inherit; }
  input[type="text"], select { width: 100%; border: 1px solid var(--line); background: var(--panel-2); color: var(--text); border-radius: 6px; padding: 8px 9px; font-size: 12px; }
  input[type="range"] { width: 100%; accent-color: var(--teal); }
  .level-row { display: grid; grid-template-columns: 54px 1fr; gap: 8px; align-items: center; margin-bottom: 7px; }
  .level-row span { color: var(--soft); font-size: 11px; font-weight: 650; }
  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .btn { border: 1px solid var(--line); border-radius: 6px; background: var(--panel-2); color: var(--text); padding: 7px 10px; font-size: 12px; cursor: pointer; }
  .btn:hover { border-color: var(--line-strong); }
  .summary-grid { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; margin-bottom: 12px; }
  .metric { border: 1px solid var(--line); background: var(--panel-2); border-radius: 8px; padding: 10px; }
  .metric .label { margin: 0; color: var(--muted); font-size: 10px; }
  .metric .value { margin-top: 3px; font-size: 20px; font-weight: 720; font-variant-numeric: tabular-nums; }
  .nodes-list { display: grid; gap: 8px; margin-bottom: 12px; }
  .node-hit { border: 1px solid var(--line); border-left: 4px solid var(--amber); border-radius: 8px; background: var(--panel-2); padding: 10px; display: grid; grid-template-columns: 1fr auto; gap: 10px; }
  .node-hit.critical { border-left-color: var(--red); }
  .node-hit .path { font-size: 12px; font-weight: 700; overflow-wrap: anywhere; }
  .node-hit .pair { color: var(--muted); font-size: 11px; margin-top: 3px; }
  .score { font-variant-numeric: tabular-nums; font-weight: 760; color: var(--amber); white-space: nowrap; }
  .score.critical { color: var(--red); }
  .tree { font-size: 12px; }
  .tree-node { margin-left: 16px; padding-left: 12px; border-left: 1px solid var(--line); }
  .tree-node.root { margin-left: 0; padding-left: 0; border-left: 0; }
  details { margin: 6px 0; }
  summary { list-style: none; cursor: pointer; }
  summary::-webkit-details-marker { display: none; }
  .branch-row { display: grid; grid-template-columns: minmax(190px, 1fr) repeat(4, auto); gap: 10px; align-items: center; min-height: 34px; border: 1px solid var(--line); background: var(--panel-2); border-radius: 8px; padding: 7px 9px; }
  .branch-row.hot { border-color: color-mix(in srgb, var(--amber) 65%, var(--line)); background: color-mix(in srgb, var(--amber) 8%, var(--panel)); }
  .branch-row.critical { border-color: color-mix(in srgb, var(--red) 65%, var(--line)); background: color-mix(in srgb, var(--red) 8%, var(--panel)); }
  .branch-name { min-width: 0; display: flex; align-items: center; gap: 8px; font-weight: 700; overflow-wrap: anywhere; }
  .branch-title { min-width: 0; overflow-wrap: anywhere; }
  .branch-name .swatch { width: 22px; height: 22px; border-radius: 5px; }
  .caret { color: var(--muted); width: 12px; flex: 0 0 auto; }
  .branch-meta { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .tier-wrap { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 8px 0 10px 12px; }
  .tier { border: 1px solid var(--line); border-radius: 8px; background: var(--panel-2); min-width: 0; }
  .tier-head { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--line); padding: 7px 8px; font-size: 11px; font-weight: 750; }
  .tier.good .tier-head { color: var(--green); }
  .tier.better .tier-head { color: var(--blue); }
  .tier.best .tier-head { color: var(--amber); }
  .item-list { display: grid; gap: 5px; padding: 7px; }
  .item-chip { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "identity price" "context price"; gap: 4px 10px; align-items: start; min-width: 0; border: 1px solid var(--line); border-radius: 6px; padding: 7px 8px; background: var(--panel); }
  .item-id { grid-area: identity; display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 8px; min-width: 0; }
  .item-chip b { flex: 0 0 auto; font-size: 11px; overflow-wrap: anywhere; }
  .item-desc, .item-context, .item-context-text { color: var(--muted); min-width: 0; white-space: normal; overflow-wrap: anywhere; line-height: 1.35; }
  .item-context { grid-area: context; display: flex; align-items: flex-start; gap: 6px; }
  .item-chip .branch-meta { grid-area: price; align-self: start; justify-self: end; white-space: nowrap; color: var(--muted); }
  .swatch { display: inline-block; width: 18px; height: 18px; border-radius: 4px; border: 1px solid var(--line-strong); flex: 0 0 auto; background: var(--chip); }
  .badge { display: inline-flex; align-items: center; gap: 5px; border-radius: 999px; padding: 2px 7px; font-size: 11px; font-weight: 700; white-space: nowrap; background: var(--chip); color: var(--muted); }
  .badge.good, .badge.growing, .badge.positive { color: var(--green); background: color-mix(in srgb, var(--green) 12%, transparent); }
  .badge.better { color: var(--blue); background: color-mix(in srgb, var(--blue) 12%, transparent); }
  .badge.best { color: var(--amber); background: color-mix(in srgb, var(--amber) 12%, transparent); }
  .badge.declining, .badge.negative, .badge.drop { color: var(--red); background: color-mix(in srgb, var(--red) 12%, transparent); }
  .badge.stable, .badge.neutral, .badge.insufficient { color: var(--muted); background: var(--chip); }
  .badge.watch { color: var(--amber); background: color-mix(in srgb, var(--amber) 12%, transparent); }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 8px 9px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--muted); font-weight: 750; cursor: pointer; user-select: none; position: sticky; top: 0; background: var(--panel); z-index: 1; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.wrap { white-space: normal; min-width: 220px; }
  #actionTable { table-layout: fixed; min-width: 1580px; font-size: 11.5px; }
  #actionTable th, #actionTable td { vertical-align: top; }
  #actionTable th:nth-child(1), #actionTable td:nth-child(1) { width: 112px; white-space: normal; overflow-wrap: anywhere; }
  #actionTable th:nth-child(2), #actionTable td:nth-child(2) { width: 220px; }
  #actionTable th:nth-child(3), #actionTable td:nth-child(3) { width: 116px; }
  #actionTable th:nth-child(4), #actionTable td:nth-child(4) { width: 56px; }
  #actionTable th:nth-child(5), #actionTable td:nth-child(5) { width: 56px; }
  #actionTable th:nth-child(6), #actionTable td:nth-child(6) { width: 60px; }
  #actionTable th:nth-child(7), #actionTable td:nth-child(7),
  #actionTable th:nth-child(8), #actionTable td:nth-child(8),
  #actionTable th:nth-child(9), #actionTable td:nth-child(9),
  #actionTable th:nth-child(10), #actionTable td:nth-child(10) { width: 84px; }
  #actionTable th:nth-child(11), #actionTable td:nth-child(11),
  #actionTable th:nth-child(12), #actionTable td:nth-child(12) { width: 92px; }
  #actionTable th:nth-child(13), #actionTable td:nth-child(13),
  #actionTable th:nth-child(14), #actionTable td:nth-child(14) { width: 170px; }
  #actionTable th:nth-child(15), #actionTable td:nth-child(15) { width: 126px; }
  #actionTable td.wrap { min-width: 0; text-align: left; }
  #actionTable .partner-cell { justify-content: flex-start; min-width: 0; }
  tbody tr:hover { background: var(--panel-2); }
  .table-scroll { max-height: 460px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
  .filter-line { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
  .filter-line input { max-width: 420px; }
  .bar-list { display: grid; gap: 8px; }
  .bar-row { display: grid; grid-template-columns: minmax(0, 1fr) 64px; gap: 12px; align-items: start; }
  .bar-label { min-width: 0; overflow-wrap: anywhere; font-size: 12px; }
  .bar-pair { display: flex; flex-wrap: wrap; align-items: baseline; gap: 3px 6px; line-height: 1.35; min-width: 0; }
  .pair-item { display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: 3px 6px; min-width: 0; }
  .pair-item b { flex: 0 0 auto; overflow-wrap: anywhere; }
  .pair-desc, .pair-sep { color: var(--muted); }
  .pair-desc { font-weight: 500; overflow-wrap: anywhere; }
  .partner-cell { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: baseline; gap: 3px 6px; line-height: 1.35; min-width: 220px; }
  .momentum-cell { min-width: 0; white-space: normal; line-height: 1.25; font-variant-numeric: tabular-nums; }
  .momentum-delta { font-weight: 760; color: var(--text); }
  .momentum-delta.pos { color: var(--green); }
  .momentum-delta.neg { color: var(--red); }
  .momentum-base { margin-top: 2px; color: var(--muted); font-size: 10.5px; line-height: 1.2; }
  .bar-track { height: 8px; border-radius: 999px; background: var(--line); overflow: hidden; margin-top: 4px; }
  .bar-fill { height: 100%; border-radius: inherit; background: var(--teal); }
  .bar-fill.neg { background: var(--red); }
  .empty { color: var(--muted); font-size: 12px; padding: 12px 0; }
  .foot { color: var(--soft); font-size: 11px; margin: 14px 2px 0; }
  .notes-card { margin-top: 18px; box-shadow: none; }
  .notes-card details { margin: 0; }
  .notes-card summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; cursor: pointer; }
  .notes-card summary h2 { margin: 0; }
  .notes-card summary::after { content: '+'; color: var(--muted); font-weight: 760; font-size: 16px; }
  .notes-card details[open] summary::after { content: '-'; }
  .notes-body { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 14px; }
  .notes-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 18px; margin: 0; }
  .notes-grid div { min-width: 0; }
  .notes-grid dt { font-weight: 760; font-size: 12px; margin-bottom: 3px; }
  .notes-grid dd { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.45; }
  .notes-list { margin: 4px 0 0; padding-left: 18px; color: var(--muted); font-size: 12px; line-height: 1.45; }
  .notes-list li { margin: 2px 0; }
  .notes-grid code { color: var(--text); }
  @media (max-width: 1120px) {
    .kpis { grid-template-columns: repeat(3, 1fr); }
    .tree-layout, .grid-2 { grid-template-columns: 1fr; }
    .controls { border-right: 0; border-bottom: 1px solid var(--line); padding-right: 0; padding-bottom: 12px; position: static; }
    .tier-wrap { grid-template-columns: 1fr; }
  }
  @media (max-width: 680px) {
    .shell { width: min(100% - 20px, 1460px); padding-top: 18px; }
    .topbar { grid-template-columns: 1fr; }
    .kpis, .summary-grid { grid-template-columns: repeat(2, 1fr); }
    .branch-row { grid-template-columns: 1fr; gap: 4px; }
    .item-chip { grid-template-columns: 1fr; grid-template-areas: "identity" "context" "price"; }
    .item-chip .branch-meta { justify-self: start; }
    .notes-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<main class="shell">
  <header class="topbar">
    <div>
      <h1><span id="familyLabel">Catalog</span> Intelligence</h1>
      <p class="subtitle" id="subtitle"></p>
    </div>
    <div class="note-pill" id="priceBasis"></div>
  </header>

  <section class="kpis" id="kpiRow"></section>

  <section class="card">
    <div class="card-head">
      <div>
        <h2>Catalog Family Tree</h2>
        <p class="desc">Custom hierarchy buckets with Good, Better, and Best price-tier leaves plus dynamic similarity and correlation scoring.</p>
      </div>
      <div class="toolbar">
        <button class="btn" id="resetTree" type="button">Reset</button>
      </div>
    </div>
    <div class="tree-layout">
      <aside class="controls">
        <div class="field">
          <label for="treeSearch">Displayed Products</label>
          <input id="treeSearch" type="text" placeholder="Filter item, collection, color, species...">
        </div>
        <div class="field">
          <label>Hierarchy Levels</label>
          <div id="levelControls"></div>
        </div>
        <div class="field">
          <label for="minBucket">Minimum Bucket Size: <span id="minBucketLabel">1</span></label>
          <input id="minBucket" type="range" min="1" max="8" step="1" value="1">
        </div>
      </aside>
      <div>
        <div class="summary-grid" id="treeSummary"></div>
        <div class="nodes-list" id="interferenceNodes"></div>
        <div class="tree" id="familyTree"></div>
      </div>
    </div>
  </section>

  <section class="card" id="actionPlan">
    <div class="card-head">
      <div>
        <h2>Item Action Plan</h2>
        <p class="desc">Momentum is shown as average square feet per month over each trailing window, with the prior-window average underneath. Momentum is the 10-comparison visible trend label; HMM is the seasonality-adjusted model signal. Similar is the strongest color-photo/spec match; Substitute is the most negative sales-correlation partner.</p>
      </div>
    </div>
    <div class="filter-line">
      <input type="text" id="actionFilter" placeholder="Filter item, description, color, partner...">
    </div>
    <div class="table-scroll">
      <table id="actionTable"><thead></thead><tbody></tbody></table>
    </div>
  </section>

  <section class="grid-2">
    <div class="card">
      <div class="card-head"><div><h2>Most Correlated Item Pairs</h2><p class="desc">Positive co-movement, with companion SKUs excluded upstream.</p></div></div>
      <div id="corrBars" class="bar-list"></div>
    </div>
    <div class="card">
      <div class="card-head"><div><h2>Substitute-Effect Pairs</h2><p class="desc">Negative co-movement, where one item tends to rise as the other falls.</p></div></div>
      <div id="subBars" class="bar-list"></div>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><div><h2>Too-Similar Items</h2><p class="desc">Cannibalization candidates: correlation below -0.20, same hue bucket, same species, and current IMP1 price within $0.50/SF.</p></div></div>
    <div class="table-scroll"><table id="similarTable"><thead></thead><tbody></tbody></table></div>
  </section>

  <section class="grid-2">
    <div class="card">
      <div class="card-head"><div><h2>Consolidation Review</h2><p class="desc">Pairs that are both highly correlated and color/spec similar.</p></div></div>
      <div id="consolidationWrap"></div>
    </div>
    <div class="card">
      <div class="card-head"><div><h2>Photo-Analysis Priority</h2><p class="desc">Sales-active items missing color-photo analysis.</p></div></div>
      <div class="table-scroll"><table id="photoTable"><thead></thead><tbody></tbody></table></div>
    </div>
  </section>

  <p class="foot">Built from Gartman sales history, Garrison Color Analysis, master product specs, and catalog-intelligence pair scoring.</p>

  <section class="card notes-card" id="notes">
    <details>
      <summary><h2>Notes</h2></summary>
      <div class="notes-body">
        <dl class="notes-grid">
          <div>
            <dt><code>max</code></dt>
            <dd>The highest pair-level interference score found inside the bucket. It is roughly <code>combined similarity x absolute sales correlation</code>. A value like <code>max 24%</code> means the riskiest pair in that bucket scored 24%, not that the bucket has 24% sales share, 24% margin impact, or a 24% chance of cannibalization.</dd>
          </div>
          <div>
            <dt><code>sim</code></dt>
            <dd>The average combined color/spec similarity across item pairs in that bucket. When color-photo analysis exists, this blends photo color distance with product-spec matches; otherwise it leans on the mapped specs that are available.</dd>
          </div>
          <div>
            <dt><code>corr</code></dt>
            <dd>The average absolute sales correlation across item pairs in that bucket. It measures how strongly sales patterns move together or against each other, without treating positive and negative directions differently in the bucket summary.</dd>
          </div>
          <div>
            <dt>Interference Score</dt>
            <dd>A severity score for possible product overlap. Higher means at least one pair looks more likely to interfere because the items are similar and their sales histories are meaningfully related. The dashboard treats about <code>55%+</code> as notable and <code>70%+</code> as more critical; <code>24%</code> is mild.</dd>
          </div>
          <div>
            <dt>Interference Node</dt>
            <dd>A family-tree bucket that contains at least one high-interference pair. These are the places to inspect for possible assortment overlap, duplicate looks, confusing specs, or items fighting for the same customer.</dd>
          </div>
          <div>
            <dt>Momentum Columns</dt>
            <dd><code>1mo avg SF</code>, <code>3mo avg SF</code>, <code>6mo avg SF</code>, and <code>12mo avg SF</code> show average square feet sold per month for the current trailing window. The smaller line shows the matching prior-window average. Green means current average is higher than prior; red means lower.</dd>
          </div>
          <div>
            <dt>Similar</dt>
            <dd>The strongest flagged similarity partner for the item. This is not only photo-based: it uses combined color-photo similarity and product specs such as species, grade, finish, texture, edge detail, construction, wear layer, thickness, width, lengths, cut, and installation method.</dd>
          </div>
          <div>
            <dt>Substitute</dt>
            <dd>The item with the most negative sales correlation among the flagged substitute-effect pairs. A value such as <code>corr -0.60 | 1 pair</code> means the strongest substitute partner has a -0.60 correlation and this item appears in one substitute-effect pair.</dd>
          </div>
          <div>
            <dt>Momentum Regime</dt>
            <dd>The visible demand-direction label in the Item Action Plan. It is based on 10 positive/negative momentum comparisons using the same 1mo, 3mo, 6mo, and 12mo average SF/month values shown in the table.
              <ul class="notes-list">
                <li><b>Current-window comparisons:</b> <code>1mo > 3mo</code>, <code>1mo > 6mo</code>, <code>1mo > 12mo</code>, <code>3mo > 6mo</code>, <code>3mo > 12mo</code>, and <code>6mo > 12mo</code>.</li>
                <li><b>Prior-window comparisons:</b> <code>1mo > prior 1mo</code>, <code>3mo > prior 3mo</code>, <code>6mo > prior 6mo</code>, and <code>12mo > prior 12mo</code>.</li>
                <li><b>Growing:</b> <code>7</code> or more of the 10 comparisons are positive.</li>
                <li><b>Stable:</b> <code>4</code> to <code>6</code> of the 10 comparisons are positive.</li>
                <li><b>Declining:</b> <code>3</code> or fewer of the 10 comparisons are positive.</li>
                <li><b>Insufficient:</b> fewer than all <code>10</code> comparisons are available, usually because the item is too new or too sparse for a full prior-window read.</li>
              </ul>
            </dd>
          </div>
          <div>
            <dt>HMM Regime</dt>
            <dd>The model-based directional signal. It uses a 3-state Hidden Markov Model on seasonality-adjusted log sales level and month-to-month slope, then reports the current state with a 3-month smoothing vote.
              <ul class="notes-list">
                <li><b>Positive:</b> the former HMM <code>Growing</code> state.</li>
                <li><b>Neutral:</b> the former HMM <code>Stable</code> state.</li>
                <li><b>Negative:</b> the former HMM <code>Declining</code> state.</li>
                <li><b>Insufficient:</b> not enough usable history for the HMM, currently fewer than <code>12</code> months.</li>
              </ul>
            </dd>
          </div>
          <div>
            <dt>Action Labels</dt>
            <dd>Action is rule-based and uses 6-month square-foot change: current 6-month total SF minus prior 6-month total SF. Current thresholds: decline at or below <code id="declineThresholdNote">-</code>; growth at or above <code id="growthThresholdNote">-</code>. Thresholds are calculated from the item distribution, with at least a <code>1,000 SF</code> materiality floor. Drop and phase-out labels are held back when Momentum is <code>Growing</code> and HMM is <code>Positive</code>.
              <ul class="notes-list">
                <li><b>Expand candidate:</b> 6-month SF change is at or above the growth threshold.</li>
                <li><b>Expand - needs photo analysis:</b> same as Expand candidate, but the item is missing color-photo analysis.</li>
                <li><b>Phase-out watch:</b> 6-month SF change is at or below the decline threshold, but the item does not currently have a flagged Similar or Substitute partner.</li>
                <li><b>Drop candidate:</b> 6-month SF change is at or below the decline threshold, the item has at least one flagged Similar or Substitute partner, and the two trend signals are not both positive.</li>
                <li><b>Watch - mixed signals:</b> the 6-month action window is weak, but Momentum is <code>Growing</code> and HMM is <code>Positive</code>; review before treating it as a drop or phase-out candidate.</li>
                <li><b>Watch:</b> the item does not meet the current expand, phase-out, or drop rule; this also covers items without enough recent comparison data.</li>
              </ul>
            </dd>
          </div>
          <div>
            <dt>Good / Better / Best</dt>
            <dd>Price tiers based on the current selling price per square foot from <code>GSFL2K.ITEMMAST.IMP1</code>. These are relative pricing bands for this family, not manufacturer quality ratings.</dd>
          </div>
          <div>
            <dt>Hue Bucket / Color</dt>
            <dd>The catalog color grouping from the Garrison color analysis. The swatch is the average placeholder color for that bucket, meant to help identify whether the bucket is light, medium, dark, warm, cool, etc.</dd>
          </div>
          <div>
            <dt>Too-Similar Items</dt>
            <dd>The focused cannibalization screen. Current rules require negative correlation below <code>-0.20</code>, same hue bucket, same species, and current <code>IMP1</code> price within <code>$0.50/SF</code>.</dd>
          </div>
        </dl>
      </div>
    </details>
  </section>
</main>

<script>
const DATA = /*DATA_START*/{}/*DATA_END*/;

const CRITERIA = [
  {key: 'species', label: 'Species'},
  {key: 'grade', label: 'Grade'},
  {key: 'width', label: 'Width'},
  {key: 'thickness', label: 'Thickness'},
  {key: 'wearLayer', label: 'Wear layer'},
  {key: 'lengths', label: 'Lengths'},
  {key: 'cut', label: 'Cut'},
  {key: 'bevel', label: 'Bevel'},
  {key: 'colorBucket', label: 'Color'}
];
const DEFAULT_LEVELS = ['colorBucket', 'width'];
const PRICE_TIERS = ['Good', 'Better', 'Best', 'Unpriced'];

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function el(tag, attrs={}, ...kids) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => {
    if (v == null || v === false) return;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else node.setAttribute(k, v);
  });
  kids.flat().forEach(k => {
    if (k == null) return;
    node.append(k.nodeType ? k : document.createTextNode(k));
  });
  return node;
}
function fmt(n, d=0) {
  if (n == null || Number.isNaN(Number(n))) return '-';
  return Number(n).toLocaleString(undefined, {maximumFractionDigits:d, minimumFractionDigits:d});
}
function pct(n) {
  if (n == null || Number.isNaN(Number(n))) return '-';
  return Math.round(Number(n) * 100) + '%';
}
function money(n) {
  if (n == null || Number.isNaN(Number(n))) return '-';
  return '$' + Number(n).toLocaleString(undefined, {maximumFractionDigits:2, minimumFractionDigits:2});
}
function sqft(n, d=0) {
  if (n == null || Number.isNaN(Number(n))) return '-';
  return Number(n).toLocaleString(undefined, {maximumFractionDigits:d, minimumFractionDigits:d}) + ' SF';
}
function signedSqft(n) {
  if (n == null || Number.isNaN(Number(n))) return '-';
  const value = Number(n);
  const sign = value > 0 ? '+' : '';
  return sign + sqft(value);
}
function sellPrice(item) {
  return item?.currentSellPrice ?? item?.avgSellPrice ?? null;
}
function momentumCell(r, months) {
  const avg = r[`momentum${months}AvgSqft`];
  const priorAvg = r[`momentum${months}PriorAvgSqft`];
  if (avg == null || Number.isNaN(Number(avg))) return '<span class="branch-meta">-</span>';
  const avgNum = Number(avg);
  const priorAvgNum = Number(priorAvg);
  const cls = priorAvg == null || Number.isNaN(priorAvgNum) ? '' : (avgNum < priorAvgNum ? 'neg' : (avgNum > priorAvgNum ? 'pos' : ''));
  const label = months === 1 ? 'month' : 'months';
  return `<div class="momentum-cell" title="Average monthly SF over the latest ${months} completed ${label}; compared with the prior ${months} ${label}"><div class="momentum-delta ${cls}">${sqft(avg)}/mo</div><div class="momentum-base">prior ${sqft(priorAvg)}/mo</div></div>`;
}
function pairKey(a, b) {
  const aa = String(a || '').trim().toUpperCase();
  const bb = String(b || '').trim().toUpperCase();
  return aa <= bb ? aa + '||' + bb : bb + '||' + aa;
}
function clean(v, fallback='Unknown') {
  const text = String(v ?? '').trim();
  return text ? text : fallback;
}
function swatch(hex) {
  const style = hex ? `background:${esc(hex)}` : '';
  return `<span class="swatch" style="${style}"></span>`;
}
function hexToRgb(hex) {
  const match = String(hex || '').trim().match(/^#?([0-9a-f]{6})$/i);
  if (!match) return null;
  const n = parseInt(match[1], 16);
  return {r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255};
}
function rgbToHex({r, g, b}) {
  return '#' + [r, g, b].map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
}
function averageHex(items) {
  const colors = items.map(item => hexToRgb(item.colorHex)).filter(Boolean);
  if (!colors.length) return '';
  const total = colors.reduce((acc, color) => ({
    r: acc.r + color.r,
    g: acc.g + color.g,
    b: acc.b + color.b
  }), {r: 0, g: 0, b: 0});
  return rgbToHex({r: total.r / colors.length, g: total.g / colors.length, b: total.b / colors.length});
}
function badge(text, cls='', title='') {
  if (!text) return '<span class="badge">-</span>';
  const titleAttr = title ? ` title="${esc(title)}"` : '';
  return `<span class="badge ${cls}"${titleAttr}>${esc(text)}</span>`;
}
function regimeBadge(r, positive=null, total=null) {
  const label = r || 'Insufficient';
  const title = positive == null
    ? (label === 'Insufficient' ? `${label}: fewer than 10 usable momentum comparisons` : label)
    : `${label}: ${positive} of ${total || 10} momentum comparisons are positive`;
  return badge(label, String(label).toLowerCase(), title);
}
function hmmLabel(r) {
  const map = {Growing: 'Positive', Stable: 'Neutral', Declining: 'Negative'};
  const label = r || 'Insufficient';
  return map[label] || label;
}
function hmmBadge(r, months=null, persistence=null, history=null) {
  const label = hmmLabel(r);
  const details = [];
  if (history != null) details.push(`${fmt(history)} months history`);
  if (months != null) details.push(`${fmt(months)} months in state`);
  if (persistence != null) details.push(`${pct(persistence)} persistence`);
  const title = label === 'Insufficient'
    ? 'Insufficient: fewer than 12 usable months for the HMM'
    : `HMM ${label}${details.length ? ': ' + details.join(' | ') : ''}`;
  return badge(label, String(label).toLowerCase(), title);
}
function tierBadge(t) {
  return badge(t || 'Unpriced', String(t || '').toLowerCase());
}
function actionBadge(a) {
  const s = String(a || '');
  const cls = s.includes('Drop') ? 'drop' : (s.includes('Watch') || s.includes('Phase') ? 'watch' : 'growing');
  return badge(s, cls);
}

const actionByItem = new Map((DATA.itemActions || []).map(r => [r.item, r]));
const catalogItems = (DATA.catalogItems && DATA.catalogItems.length ? DATA.catalogItems : DATA.itemActions || [])
  .map(r => Object.assign({}, r, actionByItem.get(r.item) || {}));
const itemById = new Map(catalogItems.map(r => [r.item, r]));
const pairMap = new Map((DATA.interferencePairs || []).map(p => [pairKey(p.a, p.b), p]));
const colorBucketHex = new Map();
catalogItems.forEach(item => {
  const key = clean(item.colorBucket, '');
  if (!key) return;
  if (!colorBucketHex.has(key)) colorBucketHex.set(key, []);
  colorBucketHex.get(key).push(item);
});
colorBucketHex.forEach((items, key) => colorBucketHex.set(key, averageHex(items)));

document.getElementById('familyLabel').textContent = `${DATA.family || ''} Catalog`;
document.getElementById('subtitle').textContent = DATA.generatedNote || '';
const thresholds = DATA.priceTierThresholds || {};
document.getElementById('priceBasis').textContent = thresholds.goodMax == null
  ? 'Price tiers unavailable'
  : `Current IMP1: Good <= ${money(thresholds.goodMax)} | Better <= ${money(thresholds.betterMax)} / SF`;
const momentumBasis = DATA.momentumBasis || {};
const declineThresholdNote = document.getElementById('declineThresholdNote');
const growthThresholdNote = document.getElementById('growthThresholdNote');
if (declineThresholdNote) declineThresholdNote.textContent = signedSqft(momentumBasis.declineThresholdSqft);
if (growthThresholdNote) growthThresholdNote.textContent = signedSqft(momentumBasis.growthThresholdSqft);

function renderKpis() {
  const k = DATA.kpi || {};
  const highPairs = (DATA.interferencePairs || []).filter(p => Number(p.interferenceScore || 0) >= 0.55).length;
  const tiles = [
    ['Items with sales', k.totalItemsWithSales, `${k.totalFamilyItems || 0} active family items`],
    ['Momentum growing', k.growing, '10-comparison regime'],
    ['Momentum declining', k.declining, '10-comparison regime'],
    ['Spec mapped', k.catalogItemsWithSpecs, 'tree-ready products'],
    ['Color analyzed', k.colorAnalyzedInFamily, `${k.needsPhotoReview || 0} photo-review pairs`],
    ['Interference pairs', highPairs, 'score >= 55%']
  ];
  document.getElementById('kpiRow').replaceChildren(...tiles.map(([label, value, sub]) =>
    el('div', {class:'kpi'}, el('div', {class:'label'}, label), el('div', {class:'value'}, fmt(value)), el('div', {class:'sub'}, sub))
  ));
}

function selectedCriteria() {
  const seen = new Set();
  return Array.from(document.querySelectorAll('.level-select'))
    .map(s => s.value)
    .filter(v => v && !seen.has(v) && seen.add(v))
    .map(key => CRITERIA.find(c => c.key === key))
    .filter(Boolean);
}
function setupLevelControls() {
  const wrap = document.getElementById('levelControls');
  wrap.replaceChildren();
  for (let i = 0; i < CRITERIA.length; i++) {
    const select = el('select', {class:'level-select', 'aria-label': `Level ${i + 1}`});
    select.append(el('option', {value:''}, i < DEFAULT_LEVELS.length ? 'None' : 'None'));
    CRITERIA.forEach(c => select.append(el('option', {value:c.key}, c.label)));
    select.value = DEFAULT_LEVELS[i] || '';
    select.addEventListener('change', renderTree);
    wrap.append(el('div', {class:'level-row'}, el('span', {}, `Level ${i + 1}`), select));
  }
}
function visibleItems() {
  const q = document.getElementById('treeSearch').value.trim().toLowerCase();
  if (!q) return catalogItems;
  return catalogItems.filter(r => [
    r.item, r.collection, r.productName, r.description, r.species, r.width,
    r.thickness, r.wearLayer, r.lengths, r.cut, r.bevel, r.colorBucket, r.priceTier
  ].some(v => String(v || '').toLowerCase().includes(q)));
}
function pairsFor(items) {
  const ids = items.map(r => r.item);
  const out = [];
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      const p = pairMap.get(pairKey(ids[i], ids[j]));
      if (p) out.push(p);
    }
  }
  return out;
}
function statsFor(items) {
  const pairs = pairsFor(items);
  const sims = pairs.map(p => p.similarity).filter(v => v != null);
  const corrs = pairs.map(p => p.absCorrelation).filter(v => v != null);
  const scored = pairs.filter(p => p.interferenceScore != null).sort((a,b) => b.interferenceScore - a.interferenceScore);
  const avg = arr => arr.length ? arr.reduce((a,b) => a + Number(b), 0) / arr.length : null;
  return {
    count: items.length,
    pairCount: pairs.length,
    avgSimilarity: avg(sims),
    avgAbsCorrelation: avg(corrs),
    maxScore: scored.length ? Number(scored[0].interferenceScore) : null,
    topPair: scored[0] || null
  };
}
function groupItems(items, criteria, depth=0, path=[], branch=null) {
  if (depth >= criteria.length) {
    return {label: path[path.length - 1] || 'All products', path, branch, criteria: null, items, children: []};
  }
  const criterion = criteria[depth];
  const buckets = new Map();
  items.forEach(item => {
    const value = clean(item[criterion.key]);
    if (!buckets.has(value)) buckets.set(value, []);
    buckets.get(value).push(item);
  });
  const children = Array.from(buckets.entries())
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
    .map(([label, members]) => groupItems(
      members,
      criteria,
      depth + 1,
      [...path, `${criterion.label}: ${label}`],
      {key: criterion.key, label: criterion.label, value: label}
    ));
  return {label: path[path.length - 1] || 'All products', path, branch, criteria: criterion, items, children};
}
function tierGroups(items) {
  const groups = new Map(PRICE_TIERS.map(t => [t, []]));
  items.forEach(item => {
    const tier = PRICE_TIERS.includes(item.priceTier) ? item.priceTier : 'Unpriced';
    groups.get(tier).push(item);
  });
  return Array.from(groups.entries()).filter(([, members]) => members.length);
}
function classForScore(score) {
  if (score == null) return '';
  if (score >= 0.7) return 'critical';
  if (score >= 0.55) return 'hot';
  return '';
}
function renderBranch(node, depth=0, minBucket=1) {
  const stats = statsFor(node.items);
  const scoreCls = classForScore(stats.maxScore);
  const label = node.path.length ? node.path[node.path.length - 1] : 'All displayed products';
  const branchSwatch = node.branch?.key === 'colorBucket'
    ? swatch(colorBucketHex.get(node.branch.value) || averageHex(node.items))
    : '';
  const row = el('div', {class:`branch-row ${scoreCls}`},
    el('div', {class:'branch-name', html:`<span class="caret">${node.children.length ? '+' : ''}</span>${branchSwatch}<span class="branch-title">${esc(label)}</span>`}),
    el('span', {class:'branch-meta'}, `${fmt(stats.count)} items`),
    el('span', {class:'branch-meta'}, `sim ${pct(stats.avgSimilarity)}`),
    el('span', {class:'branch-meta'}, `corr ${pct(stats.avgAbsCorrelation)}`),
    el('span', {class:'branch-meta'}, `max ${pct(stats.maxScore)}`)
  );
  const details = el('details', {open: depth < 2 ? 'open' : null});
  const summary = el('summary');
  summary.append(row);
  details.append(summary);

  if (node.children.length) {
    node.children.filter(c => c.items.length >= minBucket).forEach(child => {
      details.append(renderBranch(child, depth + 1, minBucket));
    });
  } else {
    details.append(renderTiers(node.items));
  }
  return el('div', {class:`tree-node ${depth === 0 ? 'root' : ''}`}, details);
}
function renderTiers(items) {
  const wrap = el('div', {class:'tier-wrap'});
  tierGroups(items).forEach(([tier, members]) => {
    const body = el('div', {class:'item-list'});
    members
      .slice()
      .sort((a,b) => (sellPrice(b) || 0) - (sellPrice(a) || 0))
      .forEach(item => body.append(el('div', {class:'item-chip', title: item.description || ''},
        el('div', {class:'item-id', html:`<b>${esc(item.item)}</b>${item.description ? `<span class="item-desc">${esc(item.description)}</span>` : ''}`}),
        el('span', {class:'item-context', html:`${swatch(item.colorHex)}<span class="item-context-text">${esc(item.productName || item.colorBucket || item.collection || '')}</span>`}),
        el('span', {class:'branch-meta'}, money(sellPrice(item)))
      )));
    wrap.append(el('div', {class:`tier ${tier.toLowerCase()}`},
      el('div', {class:'tier-head'}, el('span', {}, tier), el('span', {}, `${members.length}`)),
      body
    ));
  });
  return wrap;
}
function collectInterferenceNodes(node, out=[], minBucket=1) {
  const stats = statsFor(node.items);
  if (node.path.length && node.items.length >= minBucket && stats.maxScore != null && stats.maxScore >= 0.55) {
    out.push({node, stats});
  }
  node.children.forEach(child => collectInterferenceNodes(child, out, minBucket));
  return out;
}
function renderTreeSummary(items, nodes) {
  const top = nodes[0]?.stats;
  const metrics = [
    ['Displayed', fmt(items.length), 'products'],
    ['Buckets', fmt(nodes.length), 'interference nodes'],
    ['Avg similarity', pct(statsFor(items).avgSimilarity), 'displayed pairs'],
    ['Highest score', pct(top?.maxScore), top?.topPair ? `${top.topPair.a} / ${top.topPair.b}` : 'none']
  ];
  document.getElementById('treeSummary').replaceChildren(...metrics.map(([label, value, sub]) =>
    el('div', {class:'metric'}, el('div', {class:'label'}, label), el('div', {class:'value'}, value), el('div', {class:'sub'}, sub))
  ));
}
function renderInterferenceNodes(nodes) {
  const wrap = document.getElementById('interferenceNodes');
  if (!nodes.length) {
    wrap.replaceChildren(el('div', {class:'empty'}, 'No high-interference nodes at the current hierarchy and bucket-size setting.'));
    return;
  }
  wrap.replaceChildren(...nodes.slice(0, 6).map(({node, stats}) => {
    const p = stats.topPair || {};
    const cls = stats.maxScore >= 0.7 ? 'critical' : '';
    return el('div', {class:`node-hit ${cls}`},
      el('div', {},
        el('div', {class:'path'}, node.path.join(' > ')),
        el('div', {class:'pair'}, `${fmt(node.items.length)} items | top pair ${esc(p.a || '-')} / ${esc(p.b || '-')} | sim ${pct(p.similarity)} | corr ${pct(p.absCorrelation)}`)
      ),
      el('div', {class:`score ${cls}`}, pct(stats.maxScore))
    );
  }));
}
function renderTree() {
  const items = visibleItems();
  const levels = selectedCriteria();
  const minBucket = Number(document.getElementById('minBucket').value || 1);
  const root = groupItems(items, levels);
  const nodes = collectInterferenceNodes(root, [], minBucket).sort((a,b) => b.stats.maxScore - a.stats.maxScore);
  renderTreeSummary(items, nodes);
  renderInterferenceNodes(nodes);
  const tree = document.getElementById('familyTree');
  if (!items.length) {
    tree.replaceChildren(el('div', {class:'empty'}, 'No products match the current filter.'));
    return;
  }
  tree.replaceChildren(renderBranch(root, 0, minBucket));
}

function buildSortableTable(table, columns, rows, initialSortIndex=0, initialSortDir=1) {
  let sortIndex = initialSortIndex;
  let sortDir = initialSortDir;
  let filter = () => true;
  const thead = table.querySelector('thead');
  const tbody = table.querySelector('tbody');
  thead.replaceChildren(el('tr', {}, columns.map((c, i) => {
    const th = el('th', {class:[c.num ? 'num' : '', c.wrap ? 'wrap' : ''].filter(Boolean).join(' '), title:c.title || null}, c.label);
    th.addEventListener('click', () => {
      if (sortIndex === i) sortDir *= -1;
      else { sortIndex = i; sortDir = 1; }
      render();
    });
    return th;
  })));
  function render() {
    const col = columns[sortIndex];
    const sorted = rows.filter(filter).slice().sort((a,b) => {
      const av = col.get(a), bv = col.get(b);
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sortDir;
      return String(av ?? '').localeCompare(String(bv ?? '')) * sortDir;
    });
    tbody.replaceChildren(...sorted.map(r => el('tr', {}, columns.map(c => {
      const td = el('td', {class:[c.num ? 'num' : '', c.wrap ? 'wrap' : ''].filter(Boolean).join(' '), html:c.render ? c.render(r) : esc(c.get(r))});
      return td;
    }))));
  }
  render();
  return {setFilter(fn) { filter = fn; render(); }};
}
function simBar(v) {
  const w = Math.max(0, Math.min(100, Math.round((Number(v) || 0) * 100)));
  return `<div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>${pct(v)}`;
}
function recordForItem(item) {
  const itemText = String(item || '').trim();
  return itemById.get(itemText) || itemById.get(itemText.toUpperCase()) || {};
}
function descriptionForItem(item) {
  const rec = recordForItem(item);
  return rec.description || rec.productName || '';
}
function pluralPairs(count) {
  return `${fmt(count)} pair${Number(count) === 1 ? '' : 's'}`;
}
function partnerCell(partner, value, count, mode='similar') {
  if (!partner) return '<span class="branch-meta">-</span>';
  const detail = mode === 'substitute'
    ? `corr ${fmt(value, 2)} | ${pluralPairs(count)}`
    : `${pct(value)} sim | ${pluralPairs(count)}`;
  return `<div class="partner-cell">${pairItemLabel(partner)}<span class="branch-meta">${detail}</span></div>`;
}
function pairItemLabel(item) {
  const itemText = String(item || '').trim();
  const desc = descriptionForItem(itemText);
  return `<span class="pair-item"><b>${esc(itemText)}</b>${desc ? `<span class="pair-desc">${esc(desc)}</span>` : ''}</span>`;
}
function renderBars(id, rows, negative=false) {
  const wrap = document.getElementById(id);
  if (!rows || !rows.length) {
    wrap.replaceChildren(el('div', {class:'empty'}, 'No pairs to show.'));
    return;
  }
  wrap.replaceChildren(...rows.map(r => {
    const v = Math.abs(Number(r.CORRELATION || 0));
    const pairLabel = `${pairItemLabel(r.A)}<span class="pair-sep">/</span>${pairItemLabel(r.B)}`;
    return el('div', {class:'bar-row'},
      el('div', {class:'bar-label', html:`<div class="bar-pair">${pairLabel}</div><div class="bar-track"><div class="bar-fill ${negative ? 'neg' : ''}" style="width:${Math.round(v * 100)}%"></div></div>`}),
      el('div', {class:'score'}, fmt(r.CORRELATION, 2))
    );
  }));
}
function renderStaticTables() {
  const actionTable = buildSortableTable(document.getElementById('actionTable'), [
    {label:'Item', get:r=>r.item, render:r=>`<b>${esc(r.item)}</b>`},
    {label:'Description', wrap:true, get:r=>r.description || r.productName || '', render:r=>esc(r.description || r.productName || '-')},
    {label:'Color', get:r=>r.colorBucket || '', render:r=>`${swatch(r.colorHex)} ${esc(r.colorBucket || '-')}`},
    {label:'Width', get:r=>r.width || '', render:r=>esc(r.width || '-')},
    {label:'Tier', get:r=>r.priceTier || '', render:r=>tierBadge(r.priceTier)},
    {label:'$/SF', num:true, title:'Current selling price from GSFL2K.ITEMMAST.IMP1', get:r=>sellPrice(r) ?? -Infinity, render:r=>money(sellPrice(r))},
    {label:'1mo avg SF', num:true, title:'Average monthly SF over the latest completed month', get:r=>r.momentum1AvgSqft ?? -Infinity, render:r=>momentumCell(r, 1)},
    {label:'3mo avg SF', num:true, title:'Average monthly SF over the latest 3 completed months', get:r=>r.momentum3AvgSqft ?? -Infinity, render:r=>momentumCell(r, 3)},
    {label:'6mo avg SF', num:true, title:'Average monthly SF over the latest 6 completed months', get:r=>r.momentum6AvgSqft ?? -Infinity, render:r=>momentumCell(r, 6)},
    {label:'12mo avg SF', num:true, title:'Average monthly SF over the latest 12 completed months', get:r=>r.momentum12AvgSqft ?? -Infinity, render:r=>momentumCell(r, 12)},
    {label:'Momentum', title:'10-comparison visible trend label', get:r=>r.regime || '', render:r=>regimeBadge(r.regime, r.regimePositiveComparisons, r.regimeTotalComparisons)},
    {label:'HMM', title:'Seasonality-adjusted model signal', get:r=>hmmLabel(r.hmmRegime), render:r=>hmmBadge(r.hmmRegime, r.hmmMonthsInRegime, r.hmmPersistence, r.hmmMonthsHistory)},
    {label:'Similar', wrap:true, title:'Strongest too-similar partner from combined color-photo and product-spec similarity', get:r=>r.similarValue ?? -Infinity, render:r=>partnerCell(r.similarPartner, r.similarValue, r.similarCount, 'similar')},
    {label:'Substitute', wrap:true, title:'Most negative sales-correlation partner; negative values mean one tends to rise when the other falls', get:r=>r.substituteValue ?? Infinity, render:r=>partnerCell(r.substitutePartner, r.substituteValue, r.substituteCount, 'substitute')},
    {label:'Action', get:r=>r.action || '', render:r=>actionBadge(r.action)}
  ], DATA.itemActions || [], 7, -1);
  document.getElementById('actionFilter').addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    actionTable.setFilter(r => !q || [
      r.item, r.description, r.productName, r.collection, r.colorBucket, r.species, r.width,
      r.regime, r.hmmRegime, r.hmmSourceRegime,
      r.similarPartner, descriptionForItem(r.similarPartner),
      r.substitutePartner, descriptionForItem(r.substitutePartner),
      r.action
    ].some(v => String(v || '').toLowerCase().includes(q)));
  });

  renderBars('corrBars', DATA.topCorrelatedPairs || []);
  renderBars('subBars', DATA.topSubstitutePairs || [], true);

  buildSortableTable(document.getElementById('similarTable'), [
    {label:'Item A', wrap:true, get:r=>r.ITEM_A, render:r=>pairItemLabel(r.ITEM_A)},
    {label:'Item B', wrap:true, get:r=>r.ITEM_B, render:r=>pairItemLabel(r.ITEM_B)},
    {label:'Hue bucket', get:r=>r.COLOR_BUCKET || '', render:r=>`${swatch(colorBucketHex.get(r.COLOR_BUCKET))} ${esc(r.COLOR_BUCKET || '-')}`},
    {label:'Species', get:r=>r.SPECIES || '', render:r=>esc(r.SPECIES || '-')},
    {label:'$/SF A', num:true, get:r=>r.PRICE_A ?? -Infinity, render:r=>money(r.PRICE_A)},
    {label:'$/SF B', num:true, get:r=>r.PRICE_B ?? -Infinity, render:r=>money(r.PRICE_B)},
    {label:'Price gap', num:true, get:r=>r.PRICE_DIFF ?? Infinity, render:r=>money(r.PRICE_DIFF)},
    {label:'Corr.', num:true, get:r=>r.CORRELATION ?? Infinity, render:r=>fmt(r.CORRELATION, 2)},
    {label:'Similarity', num:true, get:r=>r.COMBINED_SIMILARITY ?? -Infinity, render:r=>r.COMBINED_SIMILARITY == null ? '-' : simBar(r.COMBINED_SIMILARITY)}
  ], DATA.tooSimilar || [], 7, 1);

  const cons = document.getElementById('consolidationWrap');
  if (!DATA.consolidationReview || !DATA.consolidationReview.length) {
    cons.replaceChildren(el('div', {class:'empty'}, 'No pairs currently meet both thresholds.'));
  } else {
    const table = el('table', {}, el('thead'), el('tbody'));
    cons.append(el('div', {class:'table-scroll'}, table));
    buildSortableTable(table, [
      {label:'Item A', get:r=>r.A, render:r=>esc(r.A)},
      {label:'Item B', get:r=>r.B, render:r=>esc(r.B)},
      {label:'Corr.', num:true, get:r=>r.CORRELATION, render:r=>fmt(r.CORRELATION,2)},
      {label:'Similarity', num:true, get:r=>r.COMBINED_SIMILARITY, render:r=>pct(r.COMBINED_SIMILARITY)}
    ], DATA.consolidationReview);
  }

  buildSortableTable(document.getElementById('photoTable'), [
    {label:'Item', get:r=>r.ITEM, render:r=>esc(r.ITEM)},
    {label:'36mo SF', num:true, get:r=>r.TOTAL_SQFT_36MO || 0, render:r=>fmt(r.TOTAL_SQFT_36MO)},
    {label:'HMM', get:r=>hmmLabel(r.current_regime), render:r=>hmmBadge(r.current_regime)}
  ], DATA.photoPriority || []);
}

setupLevelControls();
renderKpis();
renderStaticTables();
renderTree();
document.getElementById('treeSearch').addEventListener('input', renderTree);
document.getElementById('minBucket').addEventListener('input', e => {
  document.getElementById('minBucketLabel').textContent = e.target.value;
  renderTree();
});
document.getElementById('resetTree').addEventListener('click', () => {
  document.getElementById('treeSearch').value = '';
  document.getElementById('minBucket').value = '1';
  document.getElementById('minBucketLabel').textContent = '1';
  document.querySelectorAll('.level-select').forEach((s, i) => { s.value = DEFAULT_LEVELS[i] || ''; });
  renderTree();
});
</script>
</body>
</html>
"""


def render_dashboard(data_path: Path, out_path: Path) -> None:
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data_json = json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    html = HTML_TEMPLATE.replace("/*DATA_START*/{}/*DATA_END*/", f"/*DATA_START*/{data_json}/*DATA_END*/")
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path} ({out_path.stat().st_size:,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default="EN")
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    fam = args.family.lower()
    data_path = args.data or DATA_DIR / f"{fam}_dashboard_data.json"
    out_path = args.out or HERE / f"{fam}_catalog_dashboard.html"
    render_dashboard(data_path, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
