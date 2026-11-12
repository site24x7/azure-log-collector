import azure.functions as func

DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Site24x7 Diagnostic Logs — Dashboard</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --green: #22c55e; --red: #ef4444; --yellow: #eab308; --blue: #3b82f6; }
  [data-theme="light"] { --bg: #f1f5f9; --card: #ffffff; --border: #e2e8f0; --text: #1e293b; --muted: #64748b; --accent: #0284c7; --green: #16a34a; --red: #dc2626; --yellow: #ca8a04; --blue: #2563eb; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; padding: 20px 24px; transition: background .3s, color .3s; }
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; flex-wrap: wrap; gap: 10px; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 span { color: var(--accent); }
  .header-actions { display: flex; align-items: center; gap: 10px; }
  .icon-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); width: 36px; height: 36px; border-radius: 8px; cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; transition: .2s; }
  .icon-btn:hover { border-color: var(--accent); color: var(--accent); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-bottom: 14px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; position: relative; transition: background .3s, border-color .3s; }
  .card h2 { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 10px; }
  .stat-label { font-size: 13px; color: var(--muted); }
  .stat-row { display: flex; gap: 20px; margin-top: 8px; }
  .stat-item { text-align: center; }
  .stat-item .num { font-size: 18px; font-weight: 600; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .badge-green { background: rgba(34,197,94,.15); color: var(--green); }
  .badge-red { background: rgba(239,68,68,.15); color: var(--red); }
  .badge-yellow { background: rgba(234,179,8,.15); color: var(--yellow); }
  .toggle { position: relative; width: 44px; height: 24px; background: var(--border); border-radius: 12px; cursor: pointer; transition: .3s; border: none; }
  .toggle.on { background: var(--green); }
  .toggle::after { content: ''; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px; background: white; border-radius: 50%; transition: .3s; }
  .toggle.on::after { left: 23px; }
  .toggle-row { display: flex; align-items: center; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border); }
  .toggle-row:last-child { border-bottom: none; }
  .btn { padding: 10px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; transition: .2s; }
  .btn-primary { background: var(--accent); color: white; }
  [data-theme="light"] .btn-primary { color: #fff; }
  .btn-primary:hover { opacity: .85; }
  .btn-danger { background: var(--red); color: white; }
  .btn-danger:hover { opacity: .85; }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn-group { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
  .info-tip { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 15px; height: 15px; border-radius: 50%; border: 1px solid var(--muted); background: transparent; color: var(--muted); font-size: 10px; font-weight: 700; cursor: help; margin-left: 6px; flex-shrink: 0; font-style: normal; font-family: Georgia, 'Times New Roman', serif; line-height: 1; text-transform: none; letter-spacing: 0; vertical-align: middle; transition: background .15s, color .15s, border-color .15s; }
  .info-tip:hover { background: var(--accent); border-color: var(--accent); color: white; }
  .info-tip .tip-text { visibility: hidden; opacity: 0; position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%); background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 8px; font-size: 12px; font-weight: 400; white-space: normal; width: 240px; z-index: 100; box-shadow: 0 4px 12px rgba(0,0,0,.3); transition: opacity .2s; pointer-events: none; text-align: left; line-height: 1.4; }
  .info-tip:hover .tip-text { visibility: visible; opacity: 1; }
  .info-tip .tip-text::after { content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%); border: 6px solid transparent; border-top-color: var(--border); }
  .region-list { margin-top: 10px; }
  .region-item { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .region-item:last-child { border-bottom: none; }
  .region-name { color: var(--accent); }
  .region-sa { color: var(--muted); font-family: monospace; font-size: 12px; }
  .sub-list { margin-top: 8px; }
  .sub-item { font-family: monospace; font-size: 12px; color: var(--muted); padding: 3px 0; }
  .error-list { margin-top: 8px; max-height: 150px; overflow-y: auto; }
  .error-item { background: rgba(239,68,68,.1); border-left: 3px solid var(--red); padding: 8px 12px; margin-bottom: 6px; border-radius: 0 4px 4px 0; font-size: 12px; font-family: monospace; color: var(--red); word-break: break-all; }
  .toast { position: fixed; bottom: 24px; right: 24px; padding: 12px 20px; border-radius: 8px; font-size: 13px; font-weight: 500; z-index: 100; transform: translateY(80px); opacity: 0; transition: .3s; }
  .toast.show { transform: translateY(0); opacity: 1; }
  .toast-success { background: var(--green); color: white; }
  .toast-error { background: var(--red); color: white; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3); border-top-color: white; border-radius: 50%; animation: spin .6s linear infinite; margin-right: 6px; vertical-align: middle; }
  [data-theme="light"] .spinner { border-color: rgba(0,0,0,.15); border-top-color: var(--accent); }
  @keyframes spin { to { transform: rotate(360deg); } }
  #lastRefresh { font-size: 12px; color: var(--muted); }
  .ignore-tags { display: flex; flex-wrap: wrap; gap: 6px; min-height: 28px; margin-bottom: 6px; }
  .ignore-tag { display: inline-flex; align-items: center; gap: 4px; background: rgba(56,189,248,.12); color: var(--accent); border: 1px solid rgba(56,189,248,.25); padding: 3px 8px; border-radius: 14px; font-size: 12px; font-family: monospace; max-width: 100%; }
  .ignore-tag span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 250px; }
  .ignore-tag button { background: none; border: none; color: var(--red); cursor: pointer; font-size: 14px; line-height: 1; padding: 0 2px; }
  .ignore-tag button:hover { color: white; }
  .ignore-label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 4px; font-weight: 600; }
  .ignore-select { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 7px 10px; border-radius: 6px; font-size: 12px; cursor: pointer; appearance: auto; transition: background .3s, border-color .3s; }
  .ignore-select:focus { outline: none; border-color: var(--accent); }
  .ignore-input { flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; font-size: 12px; transition: background .3s, border-color .3s; }
  .ignore-input:focus { outline: none; border-color: var(--accent); }
  .btn-sm { padding: 6px 12px; font-size: 12px; }
  /* Copy box — value with an inline copy icon, sized to its content */
  .copybox { display: inline-flex; align-items: center; gap: 8px; max-width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px 5px 10px; }
  .copybox code { background: transparent; border: none; padding: 0; font-size: 12px; word-break: break-all; }
  .copybtn { position: relative; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; width: 22px; height: 22px; border: none; border-radius: 4px; background: transparent; color: var(--muted); cursor: pointer; transition: background .15s, color .15s; }
  .copybtn:hover { background: var(--border); color: var(--accent); }
  .copybtn svg { width: 14px; height: 14px; }
  .copybtn::after { content: 'Copy'; visibility: hidden; opacity: 0; position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%); background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 3px 8px; border-radius: 6px; font-size: 11px; white-space: nowrap; z-index: 100; box-shadow: 0 4px 12px rgba(0,0,0,.3); transition: opacity .15s; pointer-events: none; }
  .copybtn:hover::after { visibility: visible; opacity: 1; }
  /* Tabs */
  .tab-bar { display: flex; gap: 0; margin-bottom: 16px; border-bottom: 2px solid var(--border); position: sticky; top: 0; z-index: 50; background: var(--bg); padding-top: 8px; }
  .tab-btn { padding: 10px 20px; font-size: 14px; font-weight: 600; background: none; border: none; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: .2s; }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  /* Debug tab */
  .debug-event { padding: 8px 12px; margin-bottom: 4px; border-radius: 4px; font-size: 12px; font-family: monospace; border-left: 3px solid var(--border); background: rgba(255,255,255,.03); }
  .debug-event.level-error { border-left-color: var(--red); background: rgba(239,68,68,.06); }
  .debug-event.level-warning { border-left-color: var(--yellow); background: rgba(234,179,8,.06); }
  .debug-event.level-info { border-left-color: var(--blue); background: rgba(59,130,246,.06); }
  .debug-event .de-time { color: var(--muted); font-size: 11px; }
  .debug-event .de-src { color: var(--accent); font-size: 11px; margin-left: 8px; }
  .debug-event .de-msg { margin-top: 2px; color: var(--text); word-break: break-word; }
  .debug-event .de-detail { margin-top: 2px; color: var(--muted); font-size: 11px; max-height: 60px; overflow-y: auto; word-break: break-all; }
  .debug-run { padding: 8px 12px; margin-bottom: 4px; border-radius: 4px; font-size: 12px; border-left: 3px solid var(--green); background: rgba(34,197,94,.06); }
  .debug-run.run-error { border-left-color: var(--red); background: rgba(239,68,68,.06); }
  .config-issue { padding: 6px 10px; margin-bottom: 4px; border-radius: 4px; font-size: 12px; }
  .config-issue.sev-error { background: rgba(239,68,68,.1); color: var(--red); }
  .config-issue.sev-warning { background: rgba(234,179,8,.1); color: var(--yellow); }
  .config-issue.sev-info { background: rgba(59,130,246,.1); color: var(--blue); }
</style>
</head>
<body>

<div class="header">
  <h1><span>Site24x7</span> Diagnostic Logs</h1>
  <div class="header-actions">
    <span id="lastRefresh"></span>
    <button class="icon-btn" onclick="loadAll()" title="Refresh">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 1.5v4h4"/><path d="M1.8 5.5A6.5 6.5 0 1 1 1.5 8"/></svg>
    </button>
    <button class="icon-btn" id="themeBtn" onclick="toggleTheme()" title="Toggle theme">
      <svg id="themeIcon" width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 12.5V2.5a5.5 5.5 0 0 1 0 11z"/></svg>
    </button>
  </div>
</div>

<!-- Status Bar (always visible) -->
<div class="grid">
  <div class="card" id="statusCard">
    <h2>System Status</h2>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <span id="healthBadge" class="badge badge-yellow">Checking...</span>
      <span id="s247Badge" class="badge" style="display:none"></span>
      <span id="versionBadge" class="badge" style="background:rgba(56,189,248,.15);color:var(--accent);display:none"></span>
      <span id="lastScan" class="stat-label"></span>
    </div>
    <div id="scanPhase" style="display:none;padding:4px 0 8px"></div>
    <div class="stat-row">
      <div class="stat-item"><div class="num" id="totalRes">—</div><div class="stat-label">Total Resources</div></div>
      <div class="stat-item"><div class="num" style="color:var(--green)" id="configRes">—</div><div class="stat-label">Active Resources</div></div>
      <div class="stat-item"><div class="num" style="color:var(--accent)" id="diagConfigRes">—</div><div class="stat-label">Diag Settings Created</div></div>
      <div class="stat-item"><div class="num" style="color:var(--muted)" id="ignoredRes">—</div><div class="stat-label">Ignored / Excluded</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Settings</h2>
    <p class="stat-label" style="margin-bottom:6px">This page is read-only. Change the collector from its page in Site24x7 (AppLogs &gt; Azure Diagnostic Logs).</p>
    <div class="toggle-row"><span>Log Collection</span><span class="badge" id="collectionValue">—</span></div>
    <div class="toggle-row"><span>Log Forwarding</span><span class="badge" id="processingValue">—</span></div>
    <div class="toggle-row"><span>Auto Scan</span><span class="badge" id="autoScanValue">—</span></div>
    <div class="toggle-row"><span>General Log Type</span><span class="badge" id="logTypeValue">—</span></div>
    <div class="toggle-row"><span>Collector's Own Resources</span><span class="badge" id="pipelineValue">—</span></div>
  </div>
</div>

<!-- Tab Navigation -->
<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('overview')">Overview</button>
  <button class="tab-btn" onclick="switchTab('filters')">Filters</button>
  <button class="tab-btn" onclick="switchTab('resources')">Resources</button>
  <button class="tab-btn" onclick="switchTab('entra')" id="entraTabBtn">Platform Logs</button>
  <button class="tab-btn" onclick="switchTab('debug')">🔍 Debug</button>
</div>

<!-- Tab: Overview -->
<div id="tab-overview" class="tab-panel active">
  <!-- Error Card -->
  <div class="card" id="errorCard" style="display:none;margin-bottom:14px">
    <h2>Errors</h2>
    <div id="errorList" class="error-list"></div>
  </div>
  <div class="grid">
    <div class="card">
      <h2>Monitored Subscriptions</h2>
      <div id="subList" class="sub-list"><span class="stat-label">Loading...</span></div>
    </div>
    <div class="card">
      <h2>Provisioned Regions</h2>
      <div id="regionList" class="region-list"><span class="stat-label">Loading...</span></div>
    </div>
  </div>
  <div class="card" id="scanDetailsCard" style="display:none">
    <h2>Last Scan Details</h2>
    <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Diagnostic Settings (per resource)</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px;margin-bottom:10px">
      <div class="stat-item"><div class="num" id="sdNewlyConfigured">0</div><div class="stat-label">Newly Created</div></div>
      <div class="stat-item"><div class="num" id="sdUpdated">0</div><div class="stat-label">Updated</div></div>
      <div class="stat-item"><div class="num" id="sdAlreadyConfigured">0</div><div class="stat-label">Already Existed</div></div>
      <div class="stat-item"><div class="num" id="sdRemoved">0</div><div class="stat-label">Cleaned Up</div></div>
      <div class="stat-item"><div class="num" id="sdSkipped">0</div><div class="stat-label">Skipped (No LogType)</div></div>
    </div>
    <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">S247 Log Types (account-level)</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px;margin-bottom:10px">
      <div class="stat-item"><div class="num" id="sdLogtypesCreated">0</div><div class="stat-label">Newly Created</div></div>
      <div class="stat-item"><div class="num" id="sdConfigsRefreshed">0</div><div class="stat-label">Configs Refreshed</div></div>
      <div class="stat-item"><div class="num" id="sdResourceTypes">0</div><div class="stat-label">Unique Azure Types</div></div>
    </div>
    <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Infrastructure</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px;margin-bottom:10px">
      <div class="stat-item"><div class="num" id="sdRegions">0</div><div class="stat-label">Storage Regions</div></div>
      <div class="stat-item"><div class="num" id="sdErrors">0</div><div class="stat-label">Errors</div></div>
    </div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:4px"><strong>Phase Timings</strong> <span id="sdTotalDuration" style="margin-left:8px"></span></div>
    <div id="sdPhaseTimings" style="display:flex;flex-wrap:wrap;gap:4px"></div>
  </div>
</div>

<!-- Tab: Filters -->
<div id="tab-filters" class="tab-panel">
  <div class="card" style="margin-bottom:16px">
    <h2>Resource Filters <i class="info-tip">i<span class="tip-text">Logs are collected from resources in the listed subscriptions, regions and resource types. A resource left out has its diagnostic setting removed on the next scan.</span></i> <span id="ignoreLoading" style="display:none"><span class="spinner"></span></span></h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px">Logs are collected only from what is listed. An empty list means all, including subscriptions, regions and types added later. The subscription that hosts the collector is always included.</p>
    <div class="grid" style="margin-bottom:0">
      <div>
        <label class="ignore-label">Subscriptions</label>
        <div id="ignoredSubs" class="ignore-tags"></div>
      </div>
      <div>
        <label class="ignore-label">Regions</label>
        <div id="ignoredLocs" class="ignore-tags"></div>
      </div>
      <div>
        <label class="ignore-label">Resource Types</label>
        <div id="ignoredTypes" class="ignore-tags"></div>
      </div>
    </div>

    <!-- Tags: Include / Exclude -->
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:16px">
      <h2 style="font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Tag Filters <i class="info-tip">i<span class="tip-text">Include tags act as an allow-list — only matching resources are collected. Exclude tags always skip matching resources, even if they match an include tag. Format: key=value (exact match) or key (any value).</span></i></h2>
      <p style="font-size:12px;color:var(--muted);margin-bottom:12px">
        <strong>Include tags</strong>: Only collect from resources matching at least one. Leave empty to collect from all.<br>
        <strong>Exclude tags</strong>: Always skip matching resources. Exclude takes priority.
      </p>
      <div class="grid" style="grid-template-columns:1fr 1fr;margin-bottom:0">
        <div>
          <label class="ignore-label" style="color:var(--green)">&#10003; Include Tags <span style="font-weight:normal;color:var(--muted)">(allow-list)</span></label>
          <div id="includeTagsList" class="ignore-tags"></div>
        </div>
        <div>
          <label class="ignore-label" style="color:var(--red)">&#10005; Exclude Tags <span style="font-weight:normal;color:var(--muted)">(block-list)</span></label>
          <div id="excludeTagsList" class="ignore-tags"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Log Type Filters <i class="info-tip">i<span class="tip-text">Which Azure log categories are collected; change them in Site24x7. Categories that are off are left out of the diagnostic settings on the next scan.</span></i> <span id="logtypeLoading" style="display:none"><span class="spinner"></span></span></h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:8px">Which Azure log categories are collected. Change them in Site24x7, on the collector's Log Types tab.</p>
    <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center;flex-wrap:wrap">
      <input type="text" id="logtypeSearch" placeholder="Search by category, S247 type, resource…" oninput="renderLogTypes()" style="flex:1;min-width:180px;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);font-size:13px">
      <select id="logtypeStatusFilter" onchange="renderLogTypes()" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);font-size:13px">
        <option value="all">All</option>
        <option value="enabled">Enabled</option>
        <option value="disabled">Disabled</option>
      </select>
      <select id="resourceTypeFilter" onchange="renderLogTypes()" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);font-size:13px;max-width:280px">
        <option value="">All Resource Types</option>
      </select>
      <span id="logtypeMatchCount" style="font-size:12px;color:var(--muted)"></span>
    </div>
    <div id="logtypeList" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px">
      <span class="stat-label">Loading…</span>
    </div>
  </div>
</div>

<!-- Tab: Resources -->
<div id="tab-resources" class="tab-panel">
  <div class="card">
    <h2>
      Configured Resources <span id="configResCount" class="badge" style="background:rgba(56,189,248,.15);color:var(--accent);font-size:11px;vertical-align:middle">0</span>
      <span id="configResLoading" style="display:none"><span class="spinner"></span></span>
    </h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:10px">Resources that have Azure diagnostic settings configured by this pipeline.</p>
    <input type="text" id="configResSearch" placeholder="Search by name, type, or category…" oninput="filterConfiguredResources()"
      style="width:100%;padding:8px 12px;margin-bottom:10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
    <div id="configResList" style="max-height:500px;overflow-y:auto">
      <span class="stat-label">Loading…</span>
    </div>
  </div>
</div>

<!-- Tab: Platform Logs — non-resource (tenant/subscription) log sources -->
<div id="tab-entra" class="tab-panel">
  <div class="card" style="margin-bottom:16px">
    <h2>Platform Logs</h2>
    <div class="alert alert-info" style="font-size:13px">
      Logs that live <strong>above individual resources</strong> — at the tenant or
      subscription level — and so aren't picked up by the resource scan. Configure them here.
      Resource-level diagnostic logs are handled automatically and don't appear on this tab.
      <div style="margin-top:8px">
        <strong>Available:</strong> Microsoft Entra ID (tenant). &nbsp;
        <strong>Planned:</strong> Azure Activity logs (subscription), and other non-resource sources.
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:8px;background:transparent;border:none;padding:0 2px">
    <h2 style="font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Microsoft Entra ID (tenant)</h2>
    <div style="font-size:12px;color:var(--muted)">
      Sign-ins, audit, provisioning, risk. Setup is two parts: <strong>(1)</strong> enable the
      log types below to provision them in Site24x7, and <strong>(2)</strong> a tenant admin
      enables the matching Entra diagnostic setting in Azure. We confirm part 1 (created/failed);
      we <strong>cannot</strong> verify part 2.
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <h2>Step 1 — Choose log types to collect</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:10px">
      Turn log types on or off in Site24x7, on the collector's Log Types tab (Microsoft Entra ID).
      The status shows whether each was created. Categories not yet available in Site24x7 are
      marked <em>Not supported yet</em>.
    </p>
    <div id="entraLogTypeList" style="display:flex;flex-direction:column;gap:6px">
      <span class="stat-label">Loading…</span>
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <h2>Step 2 — Target storage account</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:8px">In Step 3's "Archive to a storage account", select exactly these — matching the dropdowns on the Azure diagnostic settings page:</p>
    <div id="entraTargetFields" style="display:none;flex-direction:column;gap:8px">
      <div style="display:flex;gap:8px;align-items:flex-start">
        <span style="min-width:110px;font-size:12px;color:var(--muted);padding-top:6px">Subscription</span>
        <div style="display:inline-flex;flex-direction:column;align-items:flex-start;gap:2px;min-width:0">
          <span class="copybox">
            <code id="entraTargetSub"></code>
            <button class="copybtn" aria-label="Copy" onclick="copyField('entraTargetSub','Subscription')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></button>
          </span>
          <div id="entraTargetSubId" style="font-size:11px;color:var(--muted);padding-left:2px;word-break:break-all"></div>
        </div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="min-width:110px;font-size:12px;color:var(--muted)">Storage account</span>
        <span class="copybox">
          <code id="entraTargetName"></code>
          <button class="copybtn" aria-label="Copy" onclick="copyField('entraTargetName','Storage account')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></button>
        </span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="min-width:110px;font-size:12px;color:var(--muted)">Resource group</span>
        <code id="entraTargetRg" style="font-size:12px;color:var(--muted);word-break:break-all"></code>
      </div>
    </div>
    <div id="entraTargetWarn" style="display:none;font-size:12px;color:var(--yellow);margin-top:8px"></div>
  </div>

  <div class="card">
    <h2>Step 3 — Enable on the Azure side <span style="font-size:11px;color:var(--muted)">(tenant admin, one-time)</span></h2>
    <ol style="font-size:13px;line-height:1.9;padding-left:20px">
      <li>Sign in as a user with the <strong>Security Administrator</strong> or <strong>Global Administrator</strong> role (a service principal / managed identity will be rejected).</li>
      <li>Go to <strong>Entra ID → Monitoring &amp; health → Diagnostic settings → Add diagnostic setting</strong>.</li>
      <li>Under <strong>Logs</strong>, tick the same categories you enabled in Step 1.</li>
      <li>Under <strong>Destination details</strong>, choose <strong>Archive to a storage account</strong>, pick the subscription, and select the storage account from Step 2.</li>
      <li><strong>Save.</strong> Logs land in <code>insights-logs-*</code> containers within ~5–15 min and are forwarded automatically. You can stop forwarding any type later from <strong>Filters → Log Type Filters</strong>.</li>
    </ol>
    <p style="font-size:12px;color:var(--muted)">CLI / PowerShell commands and troubleshooting: see <code>docs/entra-id-logs.md</code>.</p>
  </div>
</div>

<!-- Tab: Debug -->
<div id="tab-debug" class="tab-panel">
  <div class="grid">
    <div class="card">
      <h2>Debug Actions</h2>
      <div class="btn-group" style="margin-top:0">
        <button class="btn btn-primary btn-sm" id="btnLoadDebug" onclick="loadDebugInfo(false)">🔄 Refresh Debug Info</button>
        <button class="btn btn-primary btn-sm" id="btnTestS247" onclick="loadDebugInfo(true)">🔌 Test S247 Connectivity</button>
        <button class="btn btn-primary btn-sm" id="btnExportDebug" onclick="exportDebugBundle()">📥 Export Debug Bundle</button>
        <button class="btn btn-primary btn-sm" id="btnExportLog" onclick="exportDebugBundle('log')" title="Readable log with keys and tokens removed — safe to send to Site24x7 support">📄 Download Log File</button>
      </div>
      <div id="debugS247Status" style="margin-top:10px"><span class="stat-label" style="font-size:11px">Run "Test S247 Connectivity" to check</span></div>
      <span id="debugS247Badge" style="display:none"></span>
    </div>
    <div class="card">
      <h2>Config Validation</h2>
      <div id="debugConfigIssues"><span class="stat-label">Click Refresh to load...</span></div>
    </div>
  </div>
  <div class="card" style="margin-bottom:16px">
    <h2>System Health</h2>
    <div id="debugHealthStatus"><span class="stat-label">Click Refresh to load...</span></div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Recent Events <span id="debugEventCount" class="badge" style="background:rgba(56,189,248,.15);color:var(--accent);font-size:11px;vertical-align:middle">-</span></h2>
      <div style="margin-bottom:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <label style="font-size:12px;color:var(--muted)">Level:</label>
        <select id="debugEventFilter" onchange="renderDebugEvents()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-size:12px">
          <option value="all">All</option>
          <option value="error">Errors</option>
          <option value="warning">Warnings</option>
          <option value="info">Info</option>
        </select>
        <label style="font-size:12px;color:var(--muted)">Source:</label>
        <select id="debugSourceFilter" onchange="renderDebugEvents()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-size:12px">
          <option value="all">All</option>
        </select>
      </div>
      <div id="debugEventList" style="max-height:400px;overflow-y:auto"><span class="stat-label">Loading…</span></div>
    </div>
    <div class="card">
      <h2>Processing Runs <span id="debugRunCount" class="badge" style="background:rgba(56,189,248,.15);color:var(--accent);font-size:11px;vertical-align:middle">-</span></h2>
      <div id="debugRunList" style="max-height:400px;overflow-y:auto"><span class="stat-label">Loading…</span></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>
<div id="authBanner" class="badge-red" role="alert" style="display:none;position:fixed;top:0;left:0;right:0;z-index:101;padding:12px 20px;font-size:13px;line-height:1.5"></div>


<script>
const BASE = window.location.origin;
// The key arrives in the address (?code=) because the page is opened with it.
// Keep it only for this tab, take it out of the address bar and history, and
// send it in a header: never in a request URL, where it would reach logs and
// the Referer header.
const FUNC_KEY = (() => {
  const params = new URLSearchParams(window.location.search);
  let key = params.get('code') || '';
  try {
    if (key) { sessionStorage.setItem('s247-dashboard-key', key); }
    else { key = sessionStorage.getItem('s247-dashboard-key') || ''; }
  } catch (e) { /* storage blocked: the key lives in memory only */ }
  if (params.has('code')) {
    params.delete('code');
    const rest = params.toString();
    history.replaceState(null, '', window.location.pathname + (rest ? '?' + rest : '') + window.location.hash);
  }
  return key;
})();
const KEY_HEADERS = FUNC_KEY ? { 'x-functions-key': FUNC_KEY } : {};
const api = (path, opts = {}) => {
  opts.headers = Object.assign({'Content-Type': 'application/json'}, KEY_HEADERS, opts.headers || {});
  const url = `${BASE}/api/${path}`;
  return fetch(url, opts).then(r => {
    if (r.status === 401) { showAuthHelp(); }
    if (!r.ok) { throw new Error(`API error: ${r.status}`); }
    return r.json();
  });
};

// The page opens with any key valid for the Dashboard function, but its API
// calls need a key valid for every function. The portal's "Get function URL"
// on Dashboard gives a Dashboard-only key, so explain instead of showing an
// empty, "Unreachable" dashboard.
function showAuthHelp() {
  const b = document.getElementById('authBanner');
  if (!b || b.style.display === 'block') return;
  b.textContent = 'This link uses a key that only opens the Dashboard, so its data requests are refused (401). '
    + 'Open it with the host key named default (not the Dashboard function key, also named default): use the Open Dashboard link in '
    + 'Site24x7, or Azure portal > Function App > App keys > Host keys > default. '
    + 'Avoid the _master key: it also grants key management.';
  b.style.display = 'block';
}

function showToast(msg, type = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast toast-${type} show`;
  setTimeout(() => t.classList.remove('show'), 3000);
}

function setLoading(btnId, loading) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.disabled = loading;
  if (loading) btn.dataset.orig = btn.innerHTML;
  btn.innerHTML = loading ? '<span class="spinner"></span>Working...' : (btn.dataset.orig || btn.innerHTML);
}

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelector(`.tab-btn[onclick="switchTab('${name}')"]`).classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  // Lazy-load resources tab
  if (name === 'resources' && !_configuredResources) loadConfiguredResources();
  // Lazy-load debug tab
  if (name === 'debug' && !_debugLoaded) loadDebugInfo(false);
}

// HTML-escape for text and attribute values alike.
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function escAttr(s) { return esc(s); }
// A value passed to an inline handler: a JS string literal first, then HTML-escaped.
// (The browser decodes the attribute before running it, so HTML-escaping alone
// lets a quote end the string.)
function jsArg(s) { return esc(JSON.stringify(String(s ?? ''))); }

function copyField(elId, label) {
  const val = (document.getElementById(elId).textContent || '').trim();
  if (!val) { showToast('Nothing to copy yet', 'warning'); return; }
  navigator.clipboard.writeText(val).then(
    () => showToast(`${label} copied`),
    () => showToast('Copy failed — select and copy manually', 'error')
  );
}

function _entraStatusPill(lt) {
  if (lt.supported === false)
    return `<span class="badge" style="font-size:10px" title="This log type isn't available in Site24x7 yet — it will appear here once added.">Not supported yet</span>`;
  if (lt.enabled) {
    if (lt.status === 'created')
      return `<span class="badge badge-green" style="font-size:10px" title="Ready to forward logs after the tenant admin configures Azure. Check AppLogs to verify ingestion.">✓ Ready to receive</span>`;
    if (lt.status === 'failed')
      return `<span class="badge badge-red" style="font-size:10px" title="${escAttr(lt.message||'')}">⚠ Create failed</span>`;
    return `<span class="badge" style="font-size:10px">${esc(lt.status||'')}</span>`;
  }
  // Toggle off (whether never created or created-then-disabled). Disabling
  // only stops our collection; it never deletes the Site24x7 log type.
  return `<span class="badge" style="font-size:10px">Off</span>`;
}

function renderEntraLogTypes(logtypes) {
  const el = document.getElementById('entraLogTypeList');
  if (!logtypes.length) { el.innerHTML = '<span class="stat-label">No categories defined.</span>'; return; }
  el.innerHTML = logtypes.map(lt => {
    const unsupported = lt.supported === false;
    const toggle = '';
    return `
    <div class="toggle-row" style="padding:6px 10px;background:var(--bg);border-radius:6px;border:1px solid var(--border);${unsupported ? 'opacity:.65' : ''}">
      <div style="min-width:0">
        <div style="font-size:13px"><strong>${esc(lt.category)}</strong></div>
        <div style="margin-top:3px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--muted);font-family:monospace">${esc(lt.normalized)}</span>
          ${_entraStatusPill(lt)}
        </div>
        ${(!unsupported && lt.message) ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">${esc(lt.message)}</div>` : ''}
      </div>
      ${toggle}
    </div>`;
  }).join('');
}

function fmtTime(iso) {
  if (!iso) return '?';
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    timeZoneName: 'short'
  });
}

// ─── Status & Health ─────────────────────────────────────────────────────

let _statusPollTimer = null;
async function loadStatus() {
  try {
    const s = await api('status');
    const lst = s.last_scan_time;
    let inProgress = s.scan_in_progress;
    // Detect stale in_progress: if scan started >15min ago, treat as timed out
    if (inProgress && lst && lst !== 'never') {
      const age = (Date.now() - new Date(lst).getTime()) / 60000;
      if (age > 15) inProgress = false;
    }
    let scanText = 'Last scan: never';
    if (lst && lst !== 'never') {
      scanText = `Last scan: ${fmtTime(lst)}`;
      if (inProgress) scanText += ' (scanning\u2026)';
    } else if (inProgress) {
      scanText = 'Scan in progress\u2026';
    }
    document.getElementById('lastScan').textContent = scanText;

    // Show the full phase stepper while a scan is running
    let phaseEl = document.getElementById('scanPhase');
    if (inProgress && s.current_phase) {
      const phases = (s.scan_phases && s.scan_phases.length)
        ? s.scan_phases
        : [{num: s.current_phase, name: s.current_phase_name}];
      const total = phases.length;
      const cur = s.current_phase;
      const pct = Math.round((cur / total) * 100);
      const spinner = '<span style="display:inline-block;width:11px;height:11px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle"></span>';
      const rows = phases.map(p => {
        let icon, color, weight;
        if (p.num < cur)      { icon = '\u2713'; color = 'var(--green)'; weight = '400'; }        // done
        else if (p.num === cur) { icon = spinner; color = 'var(--accent)'; weight = '600'; } // active (spinning)
        else                  { icon = '\u00b7'; color = 'var(--muted)'; weight = '400'; }        // pending
        const prog = (p.num === cur && s.phase_progress) ? ` \u2014 ${esc(s.phase_progress)}` : '';
        return `<div style="display:flex;gap:8px;font-size:12px;color:${color};font-weight:${weight};line-height:1.7">
          <span style="width:12px;text-align:center;flex-shrink:0">${icon}</span>
          <span>${p.num}. ${esc(p.name)}${prog}</span>
        </div>`;
      }).join('');
      phaseEl.innerHTML = `
        <div style="background:var(--border);border-radius:4px;height:4px;overflow:hidden;margin:6px 0 8px">
          <div style="background:var(--accent);width:${pct}%;height:100%;transition:width .4s"></div>
        </div>
        <div style="padding-left:2px">${rows}</div>`;
      phaseEl.style.display = 'block';
    } else {
      phaseEl.style.display = 'none';
    }

    // Real-time refresh: while a scan runs, re-poll status every few seconds.
    // Single guarded timer (cleared+reset each call) so it never multiplies,
    // and it stops on its own once the scan finishes.
    if (_statusPollTimer) { clearTimeout(_statusPollTimer); _statusPollTimer = null; }
    // Otherwise keep checking every 15 s while the tab is visible, so a scan started
    // elsewhere (Site24x7, the timer) shows up here too.
    if (inProgress) { _statusPollTimer = setTimeout(loadStatus, 4000); }
    else if (!document.hidden) { _statusPollTimer = setTimeout(loadStatus, 15000); }

    document.getElementById('totalRes').textContent = s.resources?.total ?? '—';
    document.getElementById('configRes').textContent = s.resources?.active ?? '—';
    document.getElementById('diagConfigRes').textContent = s.configured_resources_count ?? '—';
    document.getElementById('configResCount').textContent = s.configured_resources_count ?? 0;
    document.getElementById('ignoredRes').textContent = s.resources?.ignored ?? '—';

    // "Healthy" from /api/health only means the code and libraries loaded; a
    // collector that Site24x7 rejects (e.g. wrong Device Key or data centre)
    // collects nothing, so report it as Degraded with the reason.
    _s247Problem = s.s247_reachable === false
      ? 'Site24x7: ' + ((s.s247_errors || [])[0]?.message || 'unreachable during the last scan')
      : null;
    if (_s247Problem) { showDegraded(_s247Problem); }

    // Site24x7 reachability
    const s247b = document.getElementById('s247Badge');
    if (s.s247_reachable === true) {
      s247b.className = 'badge badge-green'; s247b.textContent = 'S247 Connected'; s247b.style.display = 'inline-block';
    } else if (s.s247_reachable === false) {
      s247b.className = 'badge badge-red'; s247b.textContent = 'S247 Unreachable'; s247b.style.display = 'inline-block';
    } else {
      s247b.style.display = 'none';
    }

    showOnOff('collectionValue', s.collection ? s.collection.enabled : undefined);
    showOnOff('processingValue', s.processing_enabled);
    showOnOff('autoScanValue', s.auto_scan_enabled);
    showOnOff('logTypeValue', s.general_logtype_enabled);
    showOnOff('pipelineValue', s.monitor_pipeline_resources);

    // Entra ID tab — always visible.
    const entra = s.entra || {};
    renderEntraLogTypes(entra.logtypes || []);
    const fieldsEl = document.getElementById('entraTargetFields');
    const warnEl = document.getElementById('entraTargetWarn');
    const tid = entra.target_storage_account_id || '';
    if (tid) {
      // Split the resource ID into the pieces the Azure portal asks for.
      const parts = tid.split('/');
      const sub = parts[2] || '';
      const rg = parts[4] || '';
      const name = entra.target_storage_account_name || parts[parts.length - 1] || '';
      const subName = entra.target_subscription_name || '';
      // Portal lists subscriptions by name — show name primary, GUID beneath.
      document.getElementById('entraTargetSub').textContent = subName || sub;
      document.getElementById('entraTargetSubId').textContent = subName ? sub : '';
      document.getElementById('entraTargetName').textContent = name;
      document.getElementById('entraTargetRg').textContent = rg;
      fieldsEl.style.display = 'flex';
      warnEl.style.display = 'none';
    } else {
      fieldsEl.style.display = 'none';
      warnEl.textContent = entra.any_enabled
        ? 'A log type is enabled but the dedicated storage account has not been created yet. Run a scan (or wait for one) — it is provisioned automatically and the fields then appear here.'
        : 'Enable at least one log type in Step 1 first. The dedicated storage account is created on the next scan; the subscription and account then appear here.';
      warnEl.style.display = 'block';
    }

    // Subscriptions
    const subEl = document.getElementById('subList');
    if (s.subscription_ids?.length) {
      subEl.innerHTML = s.subscription_ids.map(id => `<div class="sub-item">${esc(id)}</div>`).join('');
    } else {
      subEl.innerHTML = '<span class="stat-label">No subscriptions configured</span>';
    }

    // Regions
    const regEl = document.getElementById('regionList');
    if (s.provisioned_regions?.length) {
      regEl.innerHTML = s.provisioned_regions.map(r =>
        `<div class="region-item"><span class="region-name">${esc(r.region)}</span><span class="region-sa">${esc(r.storage_account)}</span></div>`
      ).join('');
    } else {
      regEl.innerHTML = '<span class="stat-label">No regions provisioned yet</span>';
    }

    // Errors — combine system errors + S247 connectivity errors
    const errCard = document.getElementById('errorCard');
    const errEl = document.getElementById('errorList');
    const allErrors = [...(s.errors || [])];
    // Surface S247 errors prominently when reachable=false
    if (s.s247_reachable === false && s.s247_errors?.length) {
      s.s247_errors.forEach(e => {
        const phase = e.phase ? `[${e.phase}] ` : '';
        const cat = e.category ? ` (category: ${e.category})` : '';
        allErrors.unshift(`⚠ S247: ${phase}${e.message || 'Unknown error'}${cat}`);
      });
    } else if (s.s247_reachable === false) {
      allErrors.unshift('⚠ S247: Site24x7 was unreachable during last scan — new log types could not be created');
    }
    if (allErrors.length) {
      errCard.style.display = 'block';
      errEl.innerHTML = allErrors.map(e => `<div class="error-item">${esc(e)}</div>`).join('');
    } else {
      errCard.style.display = 'none';
    }

    // Scan details widget
    const sd = s.scan_details;
    const sdCard = document.getElementById('scanDetailsCard');
    if (sd && s.last_scan_time && s.last_scan_time !== 'never') {
      sdCard.style.display = 'block';
      document.getElementById('sdNewlyConfigured').textContent = sd.newly_configured || 0;
      document.getElementById('sdUpdated').textContent = sd.updated || 0;
      document.getElementById('sdAlreadyConfigured').textContent = sd.already_configured || 0;
      document.getElementById('sdRemoved').textContent = sd.removed || 0;
      document.getElementById('sdSkipped').textContent = sd.skipped || 0;
      document.getElementById('sdLogtypesCreated').textContent = sd.logtypes_created || 0;
      document.getElementById('sdConfigsRefreshed').textContent = sd.configs_refreshed || 0;
      const errEl2 = document.getElementById('sdErrors');
      errEl2.textContent = sd.errors || 0;
      errEl2.style.color = (sd.errors || 0) > 0 ? 'var(--red)' : '';
      document.getElementById('sdRegions').textContent = sd.regions_count || 0;
      document.getElementById('sdResourceTypes').textContent = sd.unique_resource_types || 0;
      document.getElementById('sdTotalDuration').textContent = sd.total_duration ? `(Total: ${sd.total_duration}s)` : '';
      const ptEl = document.getElementById('sdPhaseTimings');
      const pt = sd.phase_timings || {};
      const phaseLabels = {
        phase1_supported_types: 'P1: Supported Types',
        phase2_discovery: 'P2: Discovery',
        phase3_regions: 'P3: Regions',
        phase4_categories: 'P4: Categories',
        phase5_logtype_creation: 'P5: LogType Creation',
        phase5b_config_refresh: 'P5b: Config Refresh',
        phase6_diag_settings: 'P6: Diag Settings',
      };
      const phaseSum = Object.keys(phaseLabels).reduce((s, k) => s + (pt[k] || 0), 0);
      const overhead = sd.total_duration ? Math.max(0, +(sd.total_duration - phaseSum).toFixed(1)) : 0;
      let badges = Object.entries(phaseLabels).map(([k, label]) => {
        const v = pt[k];
        if (v == null) return '';
        return `<span style="display:inline-block;padding:3px 8px;border-radius:4px;background:var(--bg);border:1px solid var(--border);font-size:11px">${label}: <strong>${v}s</strong></span>`;
      }).join('');
      if (overhead > 0.1) {
        badges += `<span style="display:inline-block;padding:3px 8px;border-radius:4px;background:var(--bg);border:1px solid var(--border);font-size:11px;opacity:0.7">Overhead: <strong>${overhead}s</strong></span>`;
      }
      ptEl.innerHTML = badges;
    }
  } catch (e) {
    console.error('Status load failed:', e);
  }
}

function showOnOff(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value === true ? 'On' : value === false ? 'Off' : '—';
  el.className = 'badge ' + (value === true ? 'badge-green' : value === false ? 'badge-yellow' : '');
}

// Set by loadStatus; loadHealth runs in parallel and must not overwrite it.
let _s247Problem = null;

function showDegraded(reason) {
  const hb = document.getElementById('healthBadge');
  hb.className = 'badge badge-yellow'; hb.textContent = 'Degraded'; hb.title = reason;
}

async function loadHealth() {
  try {
    const h = await api('health');
    _healthData = h;
    const badge = document.getElementById('healthBadge');
    const deps = h.dependencies || {};
    const failed = Object.entries(deps).filter(([,v]) => v !== true && v !== 'ok');
    if (!failed.length && _s247Problem) {
      showDegraded(_s247Problem);
    } else if (!failed.length) {
      badge.className = 'badge badge-green'; badge.textContent = 'Healthy';
      badge.title = 'All ' + Object.keys(deps).length + ' dependencies OK';
    } else {
      badge.className = 'badge badge-yellow'; badge.textContent = 'Degraded';
      badge.title = 'Failed: ' + failed.map(([k]) => k).join(', ');
    }
  } catch (e) {
    const badge = document.getElementById('healthBadge');
    badge.className = 'badge badge-red'; badge.textContent = 'Unreachable';
    badge.title = 'Health endpoint unreachable';
  }
}

async function loadVersion() {
  try {
    const r = await api('check-update');
    const vb = document.getElementById('versionBadge');
    vb.textContent = 'v' + (r.local_version || '?');
    vb.style.display = 'inline-block';
    // Reset first, so an installed update clears the "available" badge.
    vb.style.background = ''; vb.style.color = ''; vb.style.cursor = '';
    vb.title = ''; vb.onclick = null;
    if (r.update_available) {
      vb.textContent += '  ➜  v' + r.remote_version + ' available';
      vb.style.background = 'rgba(234,179,8,.15)';
      vb.style.color = 'var(--yellow)';
      vb.title = 'Install it from the collector page in Site24x7';
    }
    return r;
  } catch (e) { return null; /* version check is optional */ }
}

// ─── Ignore List ─────────────────────────────────────────────────────────

let _ignoreList = { subscriptions: [], locations: [], resource_types: [], tags: {include: [], exclude: []} };
let _available = {};

// Include lists (empty = all) and where their choices come from in the collector's answer.
const IGNORE_UI = {
  subscriptions:  { tagsEl: 'ignoredSubs',  selEl: 'selSub',  source: 'subscriptions' },
  locations:      { tagsEl: 'ignoredLocs',  selEl: 'selLoc',  source: 'locations' },
  resource_types: { tagsEl: 'ignoredTypes', selEl: 'selType', source: 'resource_types' },
};

// A subscription by its name when the collector knows it, else its ID.
function filterLabel(key, v) {
  const names = (key === 'subscriptions' && _available.subscription_names) || {};
  return names[v] || v;
}

function renderIgnoreList() {
  for (const [key, ui] of Object.entries(IGNORE_UI)) {
    const el = document.getElementById(ui.tagsEl);
    const items = _ignoreList[key] || [];
    if (!items.length) {
      el.innerHTML = '<span style="font-size:12px;color:var(--muted)">All</span>';
    } else {
      el.innerHTML = items.map(v =>
        `<span class="ignore-tag"><span title="${escAttr(v)}">${escAttr(filterLabel(key, v))}</span></span>`
      ).join('');
    }
  }
  renderTagSection('include', 'includeTagsList', 'selIncludeTag');
  renderTagSection('exclude', 'excludeTagsList', 'selExcludeTag');
}

function renderTagSection(mode, tagsElId, selElId) {
  const tags = (_ignoreList.tags && _ignoreList.tags[mode]) || [];
  const el = document.getElementById(tagsElId);
  if (!tags.length) {
    el.innerHTML = '<span style="font-size:12px;color:var(--muted)">None</span>';
  } else {
    const color = mode === 'include' ? 'var(--green)' : 'var(--red)';
    el.innerHTML = tags.map(v =>
      `<span class="ignore-tag" style="border-color:${color}40;color:${color};background:${color}18"><span title="${escAttr(v)}">${escAttr(v)}</span></span>`
    ).join('');
  }
}

async function loadIgnoreList() {
  const spinner = document.getElementById('ignoreLoading');
  spinner.style.display = 'inline';
  try {
    const r = await api('ignore-list');
    _ignoreList = r.ignore_list || _ignoreList;
    if (!_ignoreList.tags) _ignoreList.tags = {include: [], exclude: []};
    _available = r.available || {};
    renderIgnoreList();
  } catch (e) { console.error('Failed to load ignore list:', e); }
  finally { spinner.style.display = 'none'; }
}

// ─── Disabled Log Types ──────────────────────────────────────────────────

let _disabledLogTypes = [];
let _supportedTypes = [];
let _allResourceTypes = [];

async function loadDisabledLogTypes() {
  const spinner = document.getElementById('logtypeLoading');
  spinner.style.display = 'inline';
  try {
    const r = await api('disabled-logtypes');
    _disabledLogTypes = r.disabled_logtypes || [];
    _supportedTypes = r.supported_types || [];
    _allResourceTypes = r.all_resource_types || [];
    // Populate resource type filter
    const sel = document.getElementById('resourceTypeFilter');
    sel.innerHTML = '<option value="">All Resource Types</option>' +
      '<option value="__none__">No Resources</option>' +
      _allResourceTypes.map(rt => `<option value="${escAttr(rt)}">${esc(rt)}</option>`).join('');
    renderLogTypes();
  } catch (e) {
    console.error('Failed to load disabled log types:', e);
    document.getElementById('logtypeList').innerHTML = '<span class="stat-label">Failed to load</span>';
  } finally { spinner.style.display = 'none'; }
}

function renderLogTypes() {
  const el = document.getElementById('logtypeList');
  if (!_supportedTypes.length) {
    el.innerHTML = '<span class="stat-label">No supported log types found. Run a scan first.</span>';
    return;
  }

  const search = (document.getElementById('logtypeSearch').value || '').toLowerCase();
  const statusFilter = document.getElementById('logtypeStatusFilter').value;
  const rtFilter = document.getElementById('resourceTypeFilter').value;

  _filteredLogTypes = _supportedTypes.filter(t => {
    if (statusFilter === 'enabled' && t.disabled) return false;
    if (statusFilter === 'disabled' && !t.disabled) return false;
    if (rtFilter === '__none__' && (t.resource_types || []).length) return false;
    if (rtFilter && rtFilter !== '__none__' && !(t.resource_types || []).includes(rtFilter)) return false;
    if (search) {
      const hay = `${t.category} ${t.category_key} ${t.s247_logtype} ${t.s247_display_name} ${(t.resource_types||[]).join(' ')}`.toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  const en = _filteredLogTypes.filter(t => !t.disabled).length;
  const dis = _filteredLogTypes.filter(t => t.disabled).length;
  document.getElementById('logtypeMatchCount').textContent =
    `${_filteredLogTypes.length} of ${_supportedTypes.length} (${en} enabled, ${dis} disabled)`;

  if (!_filteredLogTypes.length) {
    el.innerHTML = '<span class="stat-label">No matching log types</span>';
    return;
  }

  el.innerHTML = _filteredLogTypes.map(t => {
    const isDisabled = t.disabled;
    const hasResources = (t.resource_types || []).length > 0;
    const s247Tag = t.s247_logtype !== t.category_key
      ? `<span style="display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(56,189,248,.12);color:var(--accent);vertical-align:middle" title="${escAttr('S247 Log Type: ' + t.s247_display_name)}">S247: ${esc(t.s247_display_name)}</span>`
      : '';
    const noResBadge = `<span style="display:inline-block;font-size:9px;padding:1px 5px;border-radius:4px;background:rgba(255,255,255,.08);color:var(--muted);vertical-align:middle">No resources</span>`;
    const rtypes = hasResources
      ? `<div style="font-size:10px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escAttr((t.resource_types||[]).join(', '))}">${esc((t.resource_types||[]).join(', '))}</div>` : '';
    return `<div class="toggle-row" style="padding:6px 10px;background:var(--bg);border-radius:6px;border:1px solid var(--border)">
      <div style="min-width:0">
        <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escAttr(t.category)}"><strong>${esc(t.category)}</strong></div>
        <div style="margin-top:2px;display:flex;align-items:center;gap:4px;flex-wrap:wrap">${s247Tag}${hasResources ? '' : noResBadge}</div>
        ${t.category.toLowerCase() !== t.category_key ? `<div style="font-size:11px;color:var(--muted);font-family:monospace">${esc(t.category_key)}</div>` : ''}
        ${rtypes}
      </div>
      <span class="badge ${isDisabled ? 'badge-yellow' : 'badge-green'}">${isDisabled ? 'Off' : 'On'}</span>
    </div>`;
  }).join('');
}

let _filteredLogTypes = [];

// ─── Configured Resources ────────────────────────────────────────────────

let _configuredResources = null;

async function loadConfiguredResources() {
  const loading = document.getElementById('configResLoading');
  loading.style.display = '';
  try {
    const r = await api('configured-resources');
    _configuredResources = r.resources || [];
    document.getElementById('configResCount').textContent = r.count ?? 0;
    renderConfiguredResources(_configuredResources);
  } catch (e) {
    document.getElementById('configResList').innerHTML = '<span class="stat-label" style="color:var(--red)">Failed to load</span>';
  } finally { loading.style.display = 'none'; }
}

function renderConfiguredResources(resources) {
  const el = document.getElementById('configResList');
  if (!resources.length) {
    el.innerHTML = '<span class="stat-label">No resources configured yet. Run a scan first.</span>';
    return;
  }
  const truncCell = 'padding:6px 8px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
  el.innerHTML = '<table style="width:100%;font-size:12px;border-collapse:collapse;table-layout:fixed">' +
    '<colgroup><col style="width:18%"><col style="width:18%"><col style="width:6%"><col style="width:26%"><col style="width:16%"><col style="width:16%"></colgroup>' +
    '<tr style="color:var(--muted);text-align:left"><th style="padding:6px 8px">Name</th><th style="padding:6px 8px">Type</th><th style="padding:6px 8px;text-align:center">#</th><th style="padding:6px 8px">Categories</th><th style="padding:6px 8px">Storage</th><th style="padding:6px 8px">Configured At</th></tr>' +
    resources.map(r => {
      const t = r.configured_at ? fmtTime(r.configured_at) : '—';
      const cats = (r.categories || []).join(', ') || '—';
      const catCount = (r.categories || []).length;
      const shortType = (r.resource_type || '').replace(/^Microsoft\\./i, '');
      return `<tr style="border-top:1px solid var(--border)">` +
        `<td style="${truncCell}" title="${escAttr(r.name + '\\n' + r.id)}">${esc(r.name)}</td>` +
        `<td style="${truncCell};color:var(--muted)" title="${escAttr(r.resource_type)}">${esc(shortType)}</td>` +
        `<td style="padding:6px 8px;text-align:center;color:var(--accent);font-weight:600" title="${escAttr(catCount + ' log categories')}">${catCount}</td>` +
        `<td style="${truncCell};color:var(--muted)" title="${escAttr(cats)}">${esc(cats)}</td>` +
        `<td style="${truncCell};color:var(--muted)" title="${escAttr(r.storage_account || '')}">${esc(r.storage_account)}</td>` +
        `<td style="padding:6px 8px;color:var(--muted);white-space:nowrap">${t}</td></tr>`;
    }).join('') + '</table>';
}

function filterConfiguredResources() {
  if (!_configuredResources) return;
  const q = document.getElementById('configResSearch').value.toLowerCase();
  const filtered = _configuredResources.filter(r =>
    r.name.toLowerCase().includes(q) || r.id.toLowerCase().includes(q) ||
    r.resource_type.toLowerCase().includes(q) || (r.categories || []).join(' ').toLowerCase().includes(q)
  );
  renderConfiguredResources(filtered);
}

// ─── Theme ───────────────────────────────────────────────────────────────

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'light' ? 'dark' : 'light';
  if (next === 'dark') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', 'light');
  localStorage.setItem('s247-theme', next);
}

(function initTheme() {
  const saved = localStorage.getItem('s247-theme');
  if (saved === 'light') document.documentElement.setAttribute('data-theme', 'light');
})();

// ─── Debug Tab ───────────────────────────────────────────────────────────

let _debugLoaded = false;
let _debugData = null;
let _healthData = null;

async function loadDebugInfo(testS247 = false) {
  setLoading('btnLoadDebug', true);
  if (testS247) setLoading('btnTestS247', true);
  try {
    const params = testS247 ? 'test_s247=1' : '';
    _debugData = await api('debug' + (params ? '?' + params : ''));
    _debugLoaded = true;
    renderDebugConfigIssues();
    renderDebugEvents();
    renderDebugRuns();
    renderDebugHealth();
    if (_debugData.s247_connectivity) renderDebugS247Status();
    showToast('Debug info loaded');
  } catch (e) {
    showToast('Failed to load debug info: ' + e.message, 'error');
  } finally {
    setLoading('btnLoadDebug', false);
    setLoading('btnTestS247', false);
  }
}

function renderDebugConfigIssues() {
  const el = document.getElementById('debugConfigIssues');
  const issues = (_debugData && _debugData.config_issues) || [];
  if (!issues.length) {
    el.innerHTML = '<span class="badge badge-green">✓ No config issues found</span>';
    return;
  }
  el.innerHTML = issues.map(i =>
    `<div class="config-issue sev-${esc(i.level||i.severity||'warning')}">${esc(i.message)}</div>`
  ).join('');
}

function renderDebugHealth() {
  const el = document.getElementById('debugHealthStatus');
  if (!_healthData) {
    el.innerHTML = '<span class="stat-label">Health data not loaded yet</span>';
    return;
  }
  const deps = _healthData.dependencies || {};
  const entries = Object.entries(deps);
  const failed = entries.filter(([,v]) => v !== true && v !== 'ok');
  let html = `<div style="font-size:12px;margin-bottom:8px;color:var(--muted)">Python: ${esc(_healthData.python_version || '?')} · ${entries.length} dependencies</div>`;
  html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:4px">';
  entries.forEach(([name, status]) => {
    const ok = status === true || status === 'ok';
    const icon = ok ? '✓' : '✗';
    const color = ok ? 'var(--green)' : 'var(--red)';
    const detail = ok ? '' : ` — ${esc(String(status))}`;
    html += `<div style="font-size:12px;padding:3px 6px;border-radius:4px;background:var(--bg);border:1px solid var(--border)"><span style="color:${color};font-weight:700">${icon}</span> ${esc(name)}${detail}</div>`;
  });
  html += '</div>';
  if (failed.length) {
    html += `<div style="margin-top:8px;font-size:12px;color:var(--red);font-weight:600">${failed.length} module(s) failed to load</div>`;
  }
  el.innerHTML = html;
}

function renderDebugEvents() {
  const el = document.getElementById('debugEventList');
  const filter = document.getElementById('debugEventFilter').value;
  const srcFilter = document.getElementById('debugSourceFilter').value;
  let events = (_debugData && _debugData.recent_events) || [];

  // Populate source filter options (once)
  const srcSel = document.getElementById('debugSourceFilter');
  if (srcSel.options.length <= 1 && events.length) {
    const sources = [...new Set(events.map(e => e.component || e.source || 'Unknown'))].sort();
    sources.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s; opt.textContent = s;
      srcSel.appendChild(opt);
    });
  }

  if (filter !== 'all') events = events.filter(e => e.level === filter);
  if (srcFilter !== 'all') events = events.filter(e => (e.component || e.source || 'Unknown') === srcFilter);
  document.getElementById('debugEventCount').textContent = events.length;
  if (!events.length) {
    el.innerHTML = '<span class="stat-label">No events recorded</span>';
    return;
  }
  el.innerHTML = events.map(e => {
    const t = e.timestamp ? fmtTime(e.timestamp) : '?';
    const detail = e.details ? `<div class="de-detail">${esc(JSON.stringify(e.details))}</div>` : '';
    return `<div class="debug-event level-${esc(e.level||'info')}">
      <span class="de-time">${esc(t)}</span><span class="de-src">${esc(e.component||e.source||'')}</span>
      <div class="de-msg">${esc(e.message||'')}</div>${detail}
    </div>`;
  }).join('');
}

function renderDebugRuns() {
  const el = document.getElementById('debugRunList');
  const runs = (_debugData && _debugData.processing_runs) || [];
  document.getElementById('debugRunCount').textContent = runs.length;
  if (!runs.length) {
    el.innerHTML = '<span class="stat-label">No processing runs recorded</span>';
    return;
  }
  el.innerHTML = runs.map(r => {
    const t = r.timestamp ? fmtTime(r.timestamp) : '?';
    const hasErr = (r.dropped || r.retrying || r.errors || 0) > 0;
    const errBlobs = (r.error_blobs || []);
    const errDetail = errBlobs.length
      ? `<div style="font-size:10px;color:var(--red);margin-top:3px">${errBlobs.map(b =>
          `${esc(b.account)}/${esc(b.container)}/${esc(b.blob)}: ${esc(b.error)}`
        ).join('<br>')}</div>` : '';
    return `<div class="debug-run ${hasErr ? 'run-error' : ''}">
      <div><strong>${esc(t)}</strong></div>
      <div style="color:var(--muted);font-size:11px;margin-top:2px">
        Blobs: ${r.blobs_seen||r.blobs_found||0} | Records: ${r.processed||r.total_records||0} | Uploaded: ${r.uploaded||0} | Retrying: ${r.retrying||0} | Dropped: ${r.dropped||r.errors||0}
        ${r.duration_s ? ' | ' + r.duration_s.toFixed(1) + 's' : ''}
      </div>
      ${errDetail}
    </div>`;
  }).join('');
}

function renderDebugS247Status() {
  const el = document.getElementById('debugS247Status');
  const badge = document.getElementById('debugS247Badge');
  const c = _debugData.s247_connectivity;
  if (!c) return;
  const ok = c.logtype_supported_ok && c.upload_domain_ok;
  badge.innerHTML = ok
    ? '<span class="badge badge-green">Connected</span>'
    : '<span class="badge badge-red">Issues Detected</span>';
  el.innerHTML = `<div style="font-size:12px;font-family:monospace">
    <div>Base URL: ${esc(c.base_url||'?')} — ${c.logtype_supported_ok ? '✅' : '❌'} logtype_supported</div>
    <div>Upload: ${esc(c.upload_domain||'?')} — ${c.upload_domain_ok ? '✅' : '❌'} reachable</div>
    ${c.logtype_ok !== undefined ? `<div>Logtype API: ${c.logtype_ok ? '✅' : '❌'}</div>` : ''}
    ${c.error ? `<div style="color:var(--red);margin-top:4px">${esc(c.error)}</div>` : ''}
  </div>`;
}

async function exportDebugBundle(format) {
  const btn = format === 'log' ? 'btnExportLog' : 'btnExportDebug';
  const download = format === 'log' ? 'log' : '1';
  setLoading(btn, true);
  try {
    const resp = await fetch(`${BASE}/api/debug?download=${download}&test_s247=1`, { headers: KEY_HEADERS });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const blob = await resp.blob();
    const cd = resp.headers.get('content-disposition') || '';
    const fnMatch = cd.match(/filename="?([^"]+)"?/);
    const filename = fnMatch ? fnMatch[1] : (format === 'log' ? 's247-collector.log' : 's247-debug-bundle.json');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    showToast(format === 'log' ? 'Log file downloaded' : 'Debug bundle downloaded');
  } catch (e) {
    showToast('Export failed: ' + e.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

// ─── Init ────────────────────────────────────────────────────────────────

function loadAll() {
  document.getElementById('lastRefresh').textContent = 'Refreshed: ' + new Date().toLocaleTimeString();
  loadHealth();
  loadStatus();
  loadIgnoreList();
  loadDisabledLogTypes();
  loadVersion();
}

loadAll();
setInterval(loadAll, 60000);
// Catch up at once when the tab becomes visible again (status checks pause while hidden).
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadStatus(); });
</script>
</body>
</html>"""


def main(req: func.HttpRequest) -> func.HttpResponse:
    resp = func.HttpResponse(DASHBOARD_HTML_TEMPLATE, mimetype="text/html", status_code=200)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    # Security headers
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    # The page is opened with ?code=<key>; never send that URL as a Referer.
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp
