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
    <h2>Controls</h2>
    <div class="toggle-row">
      <span>Log Processing <i class="info-tip">i<span class="tip-text">Enable or disable the BlobLogProcessor. When disabled, logs accumulate in storage but are not forwarded to Site24x7.</span></i></span>
      <button class="toggle" id="processingToggle" onclick="toggleProcessing()"></button>
    </div>
    <div class="toggle-row">
      <span>Auto Scan <i class="info-tip">i<span class="tip-text">When enabled, the resource scan runs automatically every 6 hours. Disabled automatically when "Remove All" is used. Manual "Trigger Scan" always works regardless of this setting.</span></i></span>
      <button class="toggle" id="autoScanToggle" onclick="toggleAutoScan()"></button>
    </div>
    <div class="toggle-row">
      <span>General Log Type <i class="info-tip">i<span class="tip-text">When enabled, log categories without a specific Site24x7 log type config are forwarded using the general fallback log type.</span></i></span>
      <button class="toggle" id="logTypeToggle" onclick="toggleLogType()"></button>
    </div>
    <div class="toggle-row">
      <span>Monitor Pipeline Resources <i class="info-tip">i<span class="tip-text">When enabled, diagnostic settings are created on the pipeline's own resources (function app, storage accounts in the pipeline resource group). Disable to prevent self-referential log loops that can generate oversized blobs.</span></i></span>
      <button class="toggle" id="pipelineToggle" onclick="togglePipelineMonitoring()"></button>
    </div>
    <div class="toggle-row">
      <label for="safeDeleteDays" style="margin:0">Safe-Delete Retention <i class="info-tip">i<span class="tip-text">Storage accounts are only deleted when no blobs are newer than this many days. Prevents data loss during region cleanup. Range: 1–365 days.</span></i></label>
      <div style="display:flex;align-items:center;gap:6px">
        <input type="number" id="safeDeleteDays" min="1" max="365" value="7"
          style="width:60px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);font-size:13px;text-align:center"
          oninput="checkSafeDeleteChanged()" />
        <span style="font-size:11px;color:var(--muted)">days</span>
        <button class="btn btn-primary" id="saveSafeDeleteBtn" onclick="updateSafeDeleteDays()" style="padding:4px 10px;font-size:12px" disabled>Save</button>
      </div>
    </div>
    <div class="btn-group">
      <button class="btn btn-primary" id="scanBtn" onclick="triggerScan()">&#9654; Trigger Scan <i class="info-tip">i<span class="tip-text">Manually trigger a full resource scan. Discovers Azure resources, creates Site24x7 log types, and configures/reconciles diagnostic settings.</span></i></button>
      <button class="btn btn-primary" id="updateBtn" onclick="checkUpdate()">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M8 2v8m0 0l-3-3m3 3l3-3M3 13h10"/></svg>Check Update <i class="info-tip">i<span class="tip-text">Check if a newer version is available in the GitHub repository. If found, shows release notes and asks for confirmation before deploying.</span></i>
      </button>
      <button class="btn btn-danger" id="removeBtn" onclick="removeDiagSettings()">&#10005; Remove All <i class="info-tip">i<span class="tip-text">Remove all s247-diag-logs diagnostic settings from every monitored resource. Does NOT delete storage accounts or log type configs. This action cannot be undone.</span></i></button>
    </div>
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
    <h2>Ignore List <i class="info-tip">i<span class="tip-text">Resources matching any rule below are excluded from diagnostic log collection. Adding a resource here will also remove its existing diagnostic setting on the next scan.</span></i> <span id="ignoreLoading" style="display:none"><span class="spinner"></span></span></h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px">Resources matching any of these rules are excluded from diagnostic log collection.</p>
    <div class="grid" style="margin-bottom:0">
      <div>
        <label class="ignore-label">Subscriptions</label>
        <div id="ignoredSubs" class="ignore-tags"></div>
        <select id="selSub" class="ignore-select" onchange="addFromSelect('subscriptions','selSub')"><option value="">+ Add subscription…</option></select>
      </div>
      <div>
        <label class="ignore-label">Resource Groups</label>
        <div id="ignoredRGs" class="ignore-tags"></div>
        <select id="selRG" class="ignore-select" onchange="addFromSelect('resource_groups','selRG')"><option value="">+ Add resource group…</option></select>
      </div>
      <div>
        <label class="ignore-label">Locations</label>
        <div id="ignoredLocs" class="ignore-tags"></div>
        <select id="selLoc" class="ignore-select" onchange="addFromSelect('locations','selLoc')"><option value="">+ Add location…</option></select>
      </div>
      <div>
        <label class="ignore-label">Resource Types</label>
        <div id="ignoredTypes" class="ignore-tags"></div>
        <select id="selType" class="ignore-select" onchange="addFromSelect('resource_types','selType')"><option value="">+ Add resource type…</option></select>
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
          <select id="selIncludeTag" class="ignore-select" onchange="addTag('include','selIncludeTag')"><option value="">+ Add include tag…</option></select>
          <div style="display:flex;gap:6px;margin-top:6px">
            <input id="addIncludeTagCustom" class="ignore-input" placeholder="Custom: key=value or key" />
            <button class="btn btn-primary btn-sm" onclick="addCustomTagTo('include','addIncludeTagCustom')">Add</button>
          </div>
        </div>
        <div>
          <label class="ignore-label" style="color:var(--red)">&#10005; Exclude Tags <span style="font-weight:normal;color:var(--muted)">(block-list)</span></label>
          <div id="excludeTagsList" class="ignore-tags"></div>
          <select id="selExcludeTag" class="ignore-select" onchange="addTag('exclude','selExcludeTag')"><option value="">+ Add exclude tag…</option></select>
          <div style="display:flex;gap:6px;margin-top:6px">
            <input id="addExcludeTagCustom" class="ignore-input" placeholder="Custom: key=value or key" />
            <button class="btn btn-primary btn-sm" onclick="addCustomTagTo('exclude','addExcludeTagCustom')">Add</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Log Type Filters <i class="info-tip">i<span class="tip-text">Toggle which Azure log categories are collected. Disabled categories are excluded from diagnostic settings on the next scan. Only categories with a Site24x7 log type config or general fallback are forwarded.</span></i> <span id="logtypeLoading" style="display:none"><span class="spinner"></span></span></h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:8px">Toggle which Azure log categories are collected. Disabled categories will not have diagnostic settings created.</p>
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
    <div style="display:flex;gap:6px;margin-bottom:12px">
      <button class="btn btn-primary" style="font-size:11px;padding:4px 10px" onclick="bulkToggleLogTypes(true)">Enable Filtered</button>
      <button class="btn btn-danger" style="font-size:11px;padding:4px 10px" onclick="bulkToggleLogTypes(false)">Disable Filtered</button>
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
      Toggling one on creates that log type in Site24x7 now; the status shows whether it
      succeeded. Categories not yet available in Site24x7 are marked <em>Not supported yet</em>
      and can't be toggled — they become available automatically once added.
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
        <button class="btn btn-danger btn-sm" id="btnClearEvents" onclick="clearDebugEvents()">🗑 Clear Events</button>
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

<script>
const BASE = window.location.origin;
const FUNC_KEY = new URLSearchParams(window.location.search).get('code') || '';
const api = (path, opts = {}) => {
  opts.headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
  const sep = path.includes('?') ? '&' : '?';
  const url = FUNC_KEY ? `${BASE}/api/${path}${sep}code=${FUNC_KEY}` : `${BASE}/api/${path}`;
  return fetch(url, opts).then(r => {
    if (!r.ok) { throw new Error(`API error: ${r.status}`); }
    return r.json();
  });
};

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

function escAttr(s) { return s.replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;'); }
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

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
      return `<span class="badge badge-green" style="font-size:10px">✓ Collecting</span>`;
    if (lt.status === 'failed')
      return `<span class="badge badge-red" style="font-size:10px" title="${escAttr(lt.message||'')}">⚠ Create failed</span>`;
    return `<span class="badge" style="font-size:10px">${esc(lt.status||'')}</span>`;
  }
  // Toggle off. Disabling only stops our collection — it never deletes the
  // log type in Site24x7, so don't say "not created" for one that was created.
  if (lt.status === 'disabled')
    return `<span class="badge" style="font-size:10px" title="Not collecting. The log type still exists in Site24x7 — disabling only stops forwarding.">Off · kept in Site24x7</span>`;
  return `<span class="badge" style="font-size:10px">Off</span>`;
}

function renderEntraLogTypes(logtypes) {
  const el = document.getElementById('entraLogTypeList');
  if (!logtypes.length) { el.innerHTML = '<span class="stat-label">No categories defined.</span>'; return; }
  el.innerHTML = logtypes.map(lt => {
    const unsupported = lt.supported === false;
    const toggle = unsupported
      ? `<button class="toggle" disabled style="opacity:.4;cursor:not-allowed"></button>`
      : `<button class="toggle ${lt.enabled ? 'on' : ''}" onclick="toggleEntraLogType('${escAttr(lt.normalized)}', ${lt.enabled})"></button>`;
    return `
    <div class="toggle-row" style="padding:6px 10px;background:var(--bg);border-radius:6px;border:1px solid var(--border);${unsupported ? 'opacity:.65' : ''}">
      <div style="min-width:0">
        <div style="font-size:13px"><strong>${esc(lt.category)}</strong></div>
        <div style="margin-top:3px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--muted);font-family:monospace">${esc(lt.normalized)}</span>
          ${_entraStatusPill(lt)}
        </div>
        ${(!unsupported && lt.status === 'failed' && lt.message) ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">${esc(lt.message)}</div>` : ''}
      </div>
      ${toggle}
    </div>`;
  }).join('');
}

async function toggleEntraLogType(normalized, currentlyEnabled) {
  const action = currentlyEnabled ? 'disable' : 'enable';
  try {
    const r = await api('entra-logtypes', {
      method: 'POST',
      body: JSON.stringify({ action, category: normalized }),
    });
    if (action === 'enable') {
      showToast(r.status === 'created'
        ? `${normalized}: created in Site24x7`
        : `${normalized}: create failed — ${r.message || 'see status'}`,
        r.status === 'created' ? 'success' : 'warning');
    } else {
      showToast(`${normalized}: removed`);
    }
    await loadStatus();  // refresh rows from server state
  } catch (e) {
    showToast('Failed to update Entra log type: ' + (e.message || e), 'error');
  }
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
    if (inProgress) { _statusPollTimer = setTimeout(loadStatus, 4000); }

    // Reflect scan-in-progress state on the trigger button
    const scanBtn = document.getElementById('scanBtn');
    if (inProgress && !scanBtn._userTriggered) {
      scanBtn.innerHTML = '&#8987; Scanning\u2026';
      scanBtn.disabled = true;
    } else if (!inProgress && !scanBtn._userTriggered) {
      scanBtn.innerHTML = '&#9654; Trigger Scan <i class="info-tip">i<span class="tip-text">Manually trigger a full resource scan. Discovers Azure resources, creates Site24x7 log types, and configures/reconciles diagnostic settings.</span></i>';
      scanBtn.disabled = false;
    }
    document.getElementById('totalRes').textContent = s.resources?.total ?? '—';
    document.getElementById('configRes').textContent = s.resources?.active ?? '—';
    document.getElementById('diagConfigRes').textContent = s.configured_resources_count ?? '—';
    document.getElementById('configResCount').textContent = s.configured_resources_count ?? 0;
    document.getElementById('ignoredRes').textContent = s.resources?.ignored ?? '—';

    // Site24x7 reachability
    const s247b = document.getElementById('s247Badge');
    if (s.s247_reachable === true) {
      s247b.className = 'badge badge-green'; s247b.textContent = 'S247 Connected'; s247b.style.display = 'inline-block';
    } else if (s.s247_reachable === false) {
      s247b.className = 'badge badge-red'; s247b.textContent = 'S247 Unreachable'; s247b.style.display = 'inline-block';
    } else {
      s247b.style.display = 'none';
    }

    const pt = document.getElementById('processingToggle');
    pt.classList.toggle('on', s.processing_enabled);
    const ast = document.getElementById('autoScanToggle');
    ast.classList.toggle('on', s.auto_scan_enabled);
    const lt = document.getElementById('logTypeToggle');
    lt.classList.toggle('on', s.general_logtype_enabled);
    const plt = document.getElementById('pipelineToggle');
    plt.classList.toggle('on', s.monitor_pipeline_resources);

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
        : 'Enable at least one log type in Step 1 first. The dedicated storage account is created on the next scan (and removed when all types are turned off); the subscription and account then appear here.';
      warnEl.style.display = 'block';
    }

    const sdInput = document.getElementById('safeDeleteDays');
    if (s.safe_delete_days != null) {
      sdInput.value = s.safe_delete_days;
      sdInput.dataset.original = s.safe_delete_days;
    }
    document.getElementById('saveSafeDeleteBtn').disabled = true;

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

async function loadHealth() {
  try {
    const h = await api('health');
    _healthData = h;
    const badge = document.getElementById('healthBadge');
    const deps = h.dependencies || {};
    const failed = Object.entries(deps).filter(([,v]) => v !== true && v !== 'ok');
    if (!failed.length) {
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
    if (r.update_available) {
      vb.textContent += '  ➜  v' + r.remote_version + ' available';
      vb.style.background = 'rgba(234,179,8,.15)';
      vb.style.color = 'var(--yellow)';
      vb.style.cursor = 'pointer';
      vb.title = (r.release_notes || 'Click Check Update to upgrade');
      vb.onclick = checkUpdate;
    }
  } catch (e) { /* version check is optional */ }
}

// ─── Controls ────────────────────────────────────────────────────────────

async function toggleProcessing() {
  const toggle = document.getElementById('processingToggle');
  const newState = !toggle.classList.contains('on');
  try {
    await api('processing', { method: 'PUT', body: JSON.stringify({enabled: newState}) });
    toggle.classList.toggle('on', newState);
    showToast(`Processing ${newState ? 'enabled' : 'disabled'}`);
  } catch (e) { showToast('Failed to toggle processing', 'error'); }
}

async function toggleAutoScan() {
  const toggle = document.getElementById('autoScanToggle');
  const newState = !toggle.classList.contains('on');
  try {
    await api('settings', { method: 'PUT', body: JSON.stringify({key: 'AUTO_SCAN_ENABLED', value: newState}) });
    toggle.classList.toggle('on', newState);
    showToast(`Auto scan ${newState ? 'enabled' : 'disabled'}. ${newState ? 'Scans will resume on schedule.' : 'Manual "Trigger Scan" still works.'}`);
  } catch (e) { showToast('Failed to toggle auto scan', 'error'); }
}

async function toggleLogType() {
  const toggle = document.getElementById('logTypeToggle');
  const newState = !toggle.classList.contains('on');
  try {
    await api('general-logtype', { method: 'PUT', body: JSON.stringify({enabled: newState}) });
    toggle.classList.toggle('on', newState);
    showToast(`General log type ${newState ? 'enabled' : 'disabled'}`);
  } catch (e) { showToast('Failed to toggle log type', 'error'); }
}

async function togglePipelineMonitoring() {
  const toggle = document.getElementById('pipelineToggle');
  const newState = !toggle.classList.contains('on');
  try {
    await api('settings', { method: 'PUT', body: JSON.stringify({key: 'MONITOR_PIPELINE_RESOURCES', value: newState}) });
    toggle.classList.toggle('on', newState);
    showToast(`Pipeline resource monitoring ${newState ? 'enabled' : 'disabled'}. Takes effect on next scan.`);
  } catch (e) { showToast('Failed to toggle pipeline monitoring', 'error'); }
}

function checkSafeDeleteChanged() {
  const input = document.getElementById('safeDeleteDays');
  const btn = document.getElementById('saveSafeDeleteBtn');
  const val = parseInt(input.value, 10);
  const orig = parseInt(input.dataset.original || '7', 10);
  btn.disabled = isNaN(val) || val < 1 || val > 365 || val === orig;
}

async function updateSafeDeleteDays() {
  const input = document.getElementById('safeDeleteDays');
  const btn = document.getElementById('saveSafeDeleteBtn');
  const val = parseInt(input.value, 10);
  if (isNaN(val) || val < 1 || val > 365) {
    showToast('Safe-delete days must be between 1 and 365', 'error');
    return;
  }
  try {
    await api('settings', { method: 'PUT', body: JSON.stringify({key: 'SAFE_DELETE_MAX_AGE_DAYS', value: val}) });
    input.dataset.original = val;
    btn.disabled = true;
    showToast('Safe-delete retention updated to ' + val + ' days');
  } catch (e) { showToast('Failed to update safe-delete retention', 'error'); }
}

async function triggerScan() {
  setLoading('scanBtn', true);
  const scanBtn = document.getElementById('scanBtn');
  const origText = scanBtn.innerHTML;
  scanBtn._userTriggered = true;
  try {
    const r = await fetch(`${BASE}/api/scan${FUNC_KEY ? '?code=' + FUNC_KEY : ''}`, {
      method: 'POST'
    });
    const data = await r.json().catch(() => ({}));
    if (data.error) {
      showToast('Scan error: ' + data.error, 'error');
      setLoading('scanBtn', false);
      scanBtn._userTriggered = false;
      return;
    }
    showToast('Scan queued — waiting for results...');
    // Record current last_scan_time to detect when it changes
    const statusBefore = await api('status').catch(() => ({}));
    const prevScanTime = statusBefore.last_scan_time || '';
    let polls = 0;
    scanBtn.innerHTML = '&#8987; Scanning...';
    scanBtn.disabled = true;
    const pollId = setInterval(async () => {
      polls++;
      const s = await api('status').catch(() => ({}));
      await loadStatus();
      const newTime = s.last_scan_time || '';
      const inProgress = s.scan_in_progress;
      if (newTime && newTime !== prevScanTime && !inProgress) {
        clearInterval(pollId);
        scanBtn.innerHTML = origText;
        scanBtn.disabled = false;
        scanBtn._userTriggered = false;
        setLoading('scanBtn', false);
        showToast('Scan complete!');
      } else if (polls >= 40) {
        clearInterval(pollId);
        scanBtn.innerHTML = origText;
        scanBtn.disabled = false;
        scanBtn._userTriggered = false;
        setLoading('scanBtn', false);
        showToast('Scan may still be running — check status later', 'warning');
      }
    }, 15000);
  } catch (e) {
    showToast('Failed to trigger scan: ' + (e.message||e), 'error');
    scanBtn.innerHTML = origText;
    scanBtn.disabled = false;
    scanBtn._userTriggered = false;
    setLoading('scanBtn', false);
  }
}

async function checkUpdate() {
  setLoading('updateBtn', true);
  try {
    const r = await api('check-update');
    if (r.update_available) {
      const notes = r.release_notes ? '\\n\\nRelease notes:\\n' + r.release_notes : '';
      if (confirm('Update available: v' + r.local_version + ' \\u27a1 v' + r.remote_version + notes + '\\n\\nApply update now? The function app will restart.')) {
        setLoading('updateBtn', true);
        showToast('Applying update — this may take a few minutes...');
        try {
          const apply = await api('check-update?apply=1', { method: 'POST' });
          if (apply.action === 'deployed') {
            showToast(`Updated to v${apply.remote_version}! App will restart shortly.`);
            setTimeout(loadVersion, 30000);
          } else {
            const err = apply.deploy_result?.error || apply.error || 'unknown error';
            showToast('Update failed: ' + err, 'error');
          }
        } catch (e) { showToast('Update apply failed', 'error'); }
      }
    } else {
      showToast(`Up to date (v${r.local_version})`);
    }
  } catch (e) { showToast('Update check failed', 'error'); }
  setLoading('updateBtn', false);
}

async function removeDiagSettings() {
  if (!confirm('Remove ALL diagnostic settings from all monitored resources?\\n\\nAuto-scan will be disabled to prevent recreation.\\nYou can re-enable it or use "Trigger Scan" manually.')) return;
  setLoading('removeBtn', true);
  try {
    const r = await api('remove-diagnostic-settings', { method: 'POST' });
    const msg = `Removed ${r.removed || 0} diagnostic settings` +
      (r.errors ? `, ${r.errors} failed` : '') +
      (r.skipped ? `, ${r.skipped} had none` : '') +
      (r.auto_scan_disabled ? '. Auto-scan disabled.' : '');
    showToast(msg, r.errors ? 'error' : 'info');
    setTimeout(loadStatus, 2000);
  } catch (e) { showToast('Removal failed', 'error'); }
  setLoading('removeBtn', false);
}

// ─── Ignore List ─────────────────────────────────────────────────────────

let _ignoreList = { resource_groups: [], locations: [], resource_ids: [], subscriptions: [], tags: {include: [], exclude: []}, resource_types: [] };
let _available = {};

const IGNORE_UI = {
  subscriptions:   { tagsEl: 'ignoredSubs',  selEl: 'selSub' },
  resource_groups: { tagsEl: 'ignoredRGs',   selEl: 'selRG' },
  locations:       { tagsEl: 'ignoredLocs',  selEl: 'selLoc' },
  resource_types:  { tagsEl: 'ignoredTypes', selEl: 'selType' },
};

function renderIgnoreList() {
  for (const [key, ui] of Object.entries(IGNORE_UI)) {
    const el = document.getElementById(ui.tagsEl);
    const items = _ignoreList[key] || [];
    if (!items.length) {
      el.innerHTML = '<span style="font-size:12px;color:var(--muted)">None</span>';
    } else {
      el.innerHTML = items.map(v =>
        `<span class="ignore-tag"><span title="${escAttr(v)}">${escAttr(v)}</span><button onclick="removeIgnoreItem('${key}','${escAttr(v)}')">&times;</button></span>`
      ).join('');
    }
    const sel = document.getElementById(ui.selEl);
    const avail = (_available[key] || []).filter(v => !(items || []).includes(v));
    const firstOpt = sel.querySelector('option');
    sel.innerHTML = '';
    sel.appendChild(firstOpt || Object.assign(document.createElement('option'), {value:'', textContent:'+ Add…'}));
    avail.forEach(v => { const opt = document.createElement('option'); opt.value = v; opt.textContent = v; sel.appendChild(opt); });
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
      `<span class="ignore-tag" style="border-color:${color}40;color:${color};background:${color}18"><span title="${escAttr(v)}">${escAttr(v)}</span><button onclick="removeTag('${mode}','${escAttr(v)}')">&times;</button></span>`
    ).join('');
  }
  const sel = document.getElementById(selElId);
  const allSelected = [...((_ignoreList.tags && _ignoreList.tags.include) || []), ...((_ignoreList.tags && _ignoreList.tags.exclude) || [])];
  const avail = (_available.tags || []).filter(v => !allSelected.includes(v));
  const firstOpt = sel.querySelector('option');
  sel.innerHTML = '';
  sel.appendChild(firstOpt || Object.assign(document.createElement('option'), {value:'', textContent:'+ Add…'}));
  avail.forEach(v => { const opt = document.createElement('option'); opt.value = v; opt.textContent = v; sel.appendChild(opt); });
}

async function loadIgnoreList() {
  const spinner = document.getElementById('ignoreLoading');
  spinner.style.display = 'inline';
  try {
    const r = await api('ignore-list');
    _ignoreList = r.ignore_list || _ignoreList;
    if (Array.isArray(_ignoreList.tags)) { _ignoreList.tags = {include: [], exclude: _ignoreList.tags}; }
    if (!_ignoreList.tags) _ignoreList.tags = {include: [], exclude: []};
    _available = r.available || {};
    renderIgnoreList();
  } catch (e) { console.error('Failed to load ignore list:', e); }
  finally { spinner.style.display = 'none'; }
}

async function saveIgnoreList() {
  try {
    await api('ignore-list', { method: 'PUT', body: JSON.stringify(_ignoreList) });
    showToast('Ignore list updated');
    loadStatus();
  } catch (e) { showToast('Failed to save ignore list', 'error'); }
}

function addFromSelect(key, selId) {
  const sel = document.getElementById(selId);
  const val = sel.value; if (!val) return;
  if (!_ignoreList[key]) _ignoreList[key] = [];
  if (!_ignoreList[key].includes(val)) { _ignoreList[key].push(val); renderIgnoreList(); saveIgnoreList(); }
  sel.value = '';
}

function addTag(mode, selId) {
  const sel = document.getElementById(selId);
  const val = sel.value; if (!val) return;
  if (!_ignoreList.tags) _ignoreList.tags = {include: [], exclude: []};
  if (!_ignoreList.tags[mode]) _ignoreList.tags[mode] = [];
  if (!_ignoreList.tags[mode].includes(val)) { _ignoreList.tags[mode].push(val); renderIgnoreList(); saveIgnoreList(); }
  sel.value = '';
}

function addCustomTagTo(mode, inputId) {
  const input = document.getElementById(inputId);
  const val = input.value.trim(); if (!val) return;
  if (!_ignoreList.tags) _ignoreList.tags = {include: [], exclude: []};
  if (!_ignoreList.tags[mode]) _ignoreList.tags[mode] = [];
  if (_ignoreList.tags[mode].includes(val)) { showToast('Already in list', 'error'); return; }
  _ignoreList.tags[mode].push(val); input.value = '';
  renderIgnoreList(); saveIgnoreList();
}

function removeTag(mode, val) {
  if (_ignoreList.tags && _ignoreList.tags[mode]) {
    _ignoreList.tags[mode] = _ignoreList.tags[mode].filter(v => v !== val);
    renderIgnoreList(); saveIgnoreList();
  }
}

function removeIgnoreItem(key, val) {
  _ignoreList[key] = (_ignoreList[key] || []).filter(v => v !== val);
  renderIgnoreList(); saveIgnoreList();
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
    const toggleCls = isDisabled ? '' : 'on';
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
      <button class="toggle ${toggleCls}" onclick="toggleLogTypeCategory('${escAttr(t.category_key)}', ${isDisabled})"></button>
    </div>`;
  }).join('');
}

let _filteredLogTypes = [];

async function bulkToggleLogTypes(enable) {
  const targets = _filteredLogTypes.filter(t => enable ? t.disabled : !t.disabled);
  if (!targets.length) {
    showToast(`No ${enable ? 'disabled' : 'enabled'} types in current filter`, 'warning');
    return;
  }
  if (!confirm(`${enable ? 'Enable' : 'Disable'} ${targets.length} log type(s)?`)) return;
  try {
    await api('disabled-logtypes', {
      method: 'POST',
      body: JSON.stringify({action: enable ? 'enable' : 'disable', categories: targets.map(t => t.category_key)})
    });
    showToast(`${targets.length} log type(s) ${enable ? 'enabled' : 'disabled'}`);
    await loadDisabledLogTypes();
  } catch (e) { showToast(`Bulk ${enable ? 'enable' : 'disable'} failed`, 'error'); }
}

async function toggleLogTypeCategory(category, currentlyDisabled) {
  const action = currentlyDisabled ? 'enable' : 'disable';
  try {
    await api('disabled-logtypes', { method: 'POST', body: JSON.stringify({action, category}) });
    showToast(`${category} ${action}d`);
    await loadDisabledLogTypes();
  } catch (e) { showToast(`Failed to ${action} ${category}`, 'error'); }
}

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
      const shortType = (r.resource_type || '').replace(/^Microsoft\./i, '');
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
    `<div class="config-issue sev-${esc(i.severity||'warning')}">${esc(i.message)}</div>`
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
    const hasErr = (r.dropped || r.errors || 0) > 0;
    const errBlobs = (r.error_blobs || []);
    const errDetail = errBlobs.length
      ? `<div style="font-size:10px;color:var(--red);margin-top:3px">${errBlobs.map(b =>
          `${esc(b.account)}/${esc(b.container)}/${esc(b.blob)}: ${esc(b.error)}`
        ).join('<br>')}</div>` : '';
    return `<div class="debug-run ${hasErr ? 'run-error' : ''}">
      <div><strong>${esc(t)}</strong></div>
      <div style="color:var(--muted);font-size:11px;margin-top:2px">
        Blobs: ${r.blobs_found||r.blobs_processed||0} | Records: ${r.processed||r.total_records||0} | Uploaded: ${r.uploaded||0} | Errors: ${r.dropped||r.errors||0}
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

async function exportDebugBundle() {
  setLoading('btnExportDebug', true);
  try {
    const sep = '?';
    const url = FUNC_KEY
      ? `${BASE}/api/debug?download=1&test_s247=1&code=${FUNC_KEY}`
      : `${BASE}/api/debug?download=1&test_s247=1`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const blob = await resp.blob();
    const cd = resp.headers.get('content-disposition') || '';
    const fnMatch = cd.match(/filename="?([^"]+)"?/);
    const filename = fnMatch ? fnMatch[1] : 's247-debug-bundle.json';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    showToast('Debug bundle downloaded');
  } catch (e) {
    showToast('Export failed: ' + e.message, 'error');
  } finally {
    setLoading('btnExportDebug', false);
  }
}

async function clearDebugEvents() {
  if (!confirm('Clear all debug events? This cannot be undone.')) return;
  setLoading('btnClearEvents', true);
  try {
    await api('debug?clear=1');
    _debugData = null;
    _debugLoaded = false;
    loadDebugInfo(false);
    showToast('Debug events cleared');
  } catch (e) {
    showToast('Failed to clear: ' + e.message, 'error');
  } finally {
    setLoading('btnClearEvents', false);
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
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp
