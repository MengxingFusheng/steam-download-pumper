from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from .controller import PumperController, SourceListRefreshError


def _topology_fields(topology_name: str) -> str:
    if topology_name == "ikuai_line":
        return ""
    if topology_name == "multi_ip":
        return """
        <label>线路数量<input name="line_count" type="number" min="2" max="10"></label>
        <label class="wide">线路 IPv4 地址<textarea name="lan_ips"></textarea></label>
        """
    if topology_name == "fleet":
        return ""
    raise ValueError(f"unsupported topology: {topology_name}")


def _runtime_fields(topology_name: str) -> str:
    if topology_name == "fleet":
        return """
        <label>每线目标 Mbps<input name="per_line_mbps" type="number" min="1" max="1000"></label>
        <label>最短轮转分钟<input name="rotation_minutes" type="number" min="1" max="240"></label>
        <label>滚动目标倍率<input name="target_ratio" type="number" min="1" max="10" step="0.1"></label>
        <label>SNMP 采样秒数<input name="snmp_poll_seconds" type="number" min="15" max="900"></label>
        <label>线路下载上限 Mbps<input name="download_ceiling_mbps" type="number" min="1" max="10000"></label>
        <label>无进度换线秒数<input name="line_stall_seconds" type="number" min="30" max="900"></label>
        <label>低速判断窗口秒数<input name="line_slow_window_seconds" type="number" min="30" max="1800"></label>
        <label>每线最低 Mbps<input name="line_minimum_mbps" type="number" min="1" max="1000"></label>
        <label>故障冷却分钟<input name="line_failure_cooldown_minutes" type="number" min="1" max="1440"></label>
        <label>每线基础连接数<input name="connections_per_line" type="number" min="1" max="12"></label>
        <label>每线最大连接数<input name="max_connections_per_line" type="number" min="1" max="12"></label>
        <label>目标控制<select name="rate_limit_enabled"><option value="true">启用</option><option value="false">禁用</option></select></label>
        """
    return """
        <label>目标带宽 Mbps<input name="target_mbps" type="number" min="1"></label>
        <label>每线基础连接数<input name="connections_per_line" type="number" min="1" max="12"></label>
        <label>每线最大连接数<input name="max_connections_per_line" type="number" min="1" max="12"></label>
        <label>目标控制<select name="rate_limit_enabled"><option value="true">启用</option><option value="false">禁用</option></select></label>
        <label>开始时间<input name="start_time" type="time"></label>
        <label>结束时间<input name="end_time" type="time"></label>
    """


def _fleet_schedule_section(topology_name: str) -> str:
    if topology_name != "fleet":
        return ""
    return """
  <section>
    <h2>Fleet 固定调度</h2>
    <div class="table-wrap"><table><thead><tr><th>时间（Asia/Shanghai）</th><th>聚合下载计划</th><th>最多活跃账号</th></tr></thead><tbody>
      <tr><td>00:00–01:00</td><td>2 Gbps</td><td>10</td></tr>
      <tr><td>01:00–07:00</td><td>4 Gbps</td><td>20</td></tr>
      <tr><td>07:00–18:00</td><td>2 Gbps</td><td>10</td></tr>
      <tr><td>18:00–24:00</td><td>停止下载，仅保留 SNMP 采集与账本</td><td>0</td></tr>
    </tbody></table></div>
    <p>iKuai 的 4 Gbps 是总下载硬上限；此表才是 Fleet 实际调度依据。18:00 到达时会停止全部 Fleet 下载线路。</p>
  </section>
    """


def _source_list_section(topology_name: str) -> str:
    if topology_name == "ikuai_line":
        return ""
    if topology_name not in {"multi_ip", "fleet"}:
        raise ValueError(f"unsupported topology: {topology_name}")
    title = "下载源清单" if topology_name == "fleet" else "远程源清单"
    return f"""
  <section>
    <div class="toolbar">
      <h2 style="margin-right:auto">{title}</h2>
      <button id="sourceListRefresh" class="neutral" onclick="refreshSourceList()">立即刷新</button>
    </div>
    <div class="metrics">
      <div class="metric">Revision<b id="sourceListRevision">-</b></div>
      <div class="metric">源数量<b id="sourceListCount">0</b></div>
      <div class="metric">Origin<b id="sourceListOrigin">local-fallback</b></div>
      <div class="metric">最后成功<b id="sourceListLastSuccess">-</b></div>
      <div class="metric">下次刷新<b id="sourceListNextRefresh">-</b></div>
      <div class="metric">过期状态<b id="sourceListStale">否</b></div>
      <div class="metric">错误<b id="sourceListError">-</b></div>
    </div>
  </section>
    """


def _fleet_ratio_section(topology_name: str) -> str:
    if topology_name != "fleet":
        return ""
    return """
  <section id="fleetRatioSection" aria-labelledby="fleetRatioHeading">
    <div class="ratio-heading">
      <div>
        <h2 id="fleetRatioHeading">滚动 15 天上传与下载倍率</h2>
        <p class="section-note">iKuai SNMP 下载量对比 SNMP 上传量；目标是下载达到上传的 2 倍，程序下载量仅用于运行校验。</p>
      </div>
      <div class="ratio-kpi" aria-live="polite">
        <span>当前倍率</span><b id="rollingRatio">—</b>
        <span id="ratioState" class="ratio-state">等待账本数据</span>
      </div>
    </div>
    <div class="ratio-values">
      <div><span>SNMP 上传</span><b id="rollingUpload">0 B</b></div>
      <div><span>SNMP 下载</span><b id="rollingDownload">0 B</b></div>
      <div><span>2× 所需下载</span><b id="rollingRequired">0 B</b></div>
      <div><span>距离目标</span><b id="rollingDebt">0 B</b></div>
    </div>
    <div class="ratio-values quality-values">
      <div><span>新口径实测 上 / 下</span><b id="rollingMeasured">0 B / 0 B</b></div>
      <div><span>重拨估算 上 / 下</span><b id="rollingEstimated">0 B / 0 B</b></div>
      <div><span>历史口径 上 / 下</span><b id="rollingLegacy">0 B / 0 B</b></div>
      <div><span>程序下载校验</span><b id="rollingProgram">0 B</b></div>
      <div><span>未知时长 / 计数中断</span><b id="rollingUnknown">0 秒 / 0</b></div>
    </div>
    <div id="ratioChart" class="ratio-chart" role="img" aria-label="滚动十五天上传下载倍率数据尚未加载">
      <div class="ratio-row"><span>上传 1×</span><div class="ratio-track"><i id="uploadBar" class="ratio-bar upload"></i><em id="ratioTargetMarker" title="2 倍目标"></em></div></div>
      <div class="ratio-row"><span>下载</span><div class="ratio-track"><i id="downloadBar" class="ratio-bar download"></i></div></div>
      <div class="ratio-axis"><span>0</span><span id="ratioScaleEnd">2×目标</span></div>
    </div>
    <div class="ratio-progress-text"><span>目标完成度</span><b id="ratioProgress">0%</b></div>
  </section>
    """


def _source_list_refresh_function(topology_name: str) -> str:
    if topology_name == "ikuai_line":
        return ""
    return "async function refreshSourceList() { try { setSourceList(await api('/api/source-list/refresh',{method:'POST'}));showError(); } catch(error) { showError(error); } }"


def _fleet_accounts_section(topology_name: str) -> str:
    if topology_name != "fleet":
        return ""
    return """
  <section>
    <h2>账号映射导入</h2>
    <p>上传 CSV（必填列：account_id、bind_ip、snmp_interface；可选 enabled）。整批校验，任一行错误不会变更当前映射。</p>
    <form id="fleetAccountsForm" class="toolbar">
      <input id="fleetAccountsFile" type="file" accept=".csv,text/csv" required>
      <button class="primary" type="submit">上传账号 CSV</button>
    </form>
  </section>
    """


def render_html(topology_name: str) -> str:
    title = {
        "ikuai_line": "iKuai 单线路下载器",
        "multi_ip": "多 IP 宽带下载器",
        "fleet": "PPPoE Fleet 下载调度器",
    }.get(topology_name)
    if title is None:
        raise ValueError(f"unsupported topology: {topology_name}")
    return (
        _HTML_TEMPLATE.replace("{{TITLE}}", title)
        .replace("{{RUNTIME_FIELDS}}", _runtime_fields(topology_name))
        .replace("{{TOPOLOGY_FIELDS}}", _topology_fields(topology_name))
        .replace("{{SOURCE_LIST_SECTION}}", _source_list_section(topology_name))
        .replace("{{FLEET_ACCOUNTS_SECTION}}", _fleet_accounts_section(topology_name))
        .replace("{{FLEET_SCHEDULE_SECTION}}", _fleet_schedule_section(topology_name))
        .replace("{{FLEET_RATIO_SECTION}}", _fleet_ratio_section(topology_name))
        .replace("{{SOURCE_LIST_REFRESH_FUNCTION}}", _source_list_refresh_function(topology_name))
        .replace("{{REFRESH_MS}}", "10000" if topology_name == "fleet" else "5000")
    )


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{TITLE}}</title>
  <style>
    :root { font-family: Arial, "Microsoft YaHei", sans-serif; color: #18212f; background: #eef2f6; --blue: #1769aa; --green: #16804b; --amber: #9a6700; --line: #d4dce7; }
    * { box-sizing: border-box; }
    body { margin: 0; }
    main { width: min(1180px, 100%); margin: 0 auto; padding: 20px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
    h1 { margin: 0; font-size: 25px; }
    h2 { margin: 0 0 12px; font-size: 17px; }
    section { padding: 16px 0; border-top: 1px solid #cbd5e1; }
    .toolbar, .badges, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .badge { display: inline-flex; min-height: 28px; align-items: center; padding: 0 9px; border: 1px solid #b7c4d4; border-radius: 6px; background: #fff; font-size: 13px; }
    .error { display: none; color: #991b1b; border-color: #f1a8a8; background: #fff1f1; }
    button { min-height: 38px; border: 1px solid transparent; border-radius: 6px; padding: 0 14px; font: inherit; font-weight: 700; cursor: pointer; }
    .primary { color: #fff; background: #1769aa; }
    .stop { color: #fff; background: #b42318; }
    .neutral { color: #18212f; border-color: #b7c4d4; background: #fff; }
    .metrics { display: grid; grid-template-columns: repeat(7, minmax(110px, 1fr)); gap: 8px; }
    .metric { min-width: 0; padding: 10px; border: 1px solid #d4dce7; border-radius: 6px; background: #fff; font-size: 12px; color: #526175; }
    .metric b { display: block; margin-top: 5px; overflow-wrap: anywhere; color: #18212f; font-size: 19px; }
    canvas { display: block; width: 100%; height: 210px; margin-top: 10px; border: 1px solid #d4dce7; border-radius: 6px; background: #fff; }
    form .grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 10px; }
    label { display: grid; gap: 5px; color: #475569; font-size: 13px; }
    label.wide { grid-column: 1 / -1; }
    input, select, textarea { width: 100%; min-height: 38px; border: 1px solid #aebaca; border-radius: 6px; padding: 7px 9px; background: #fff; color: #18212f; font: inherit; }
    textarea { min-height: 82px; resize: vertical; }
    .table-wrap { overflow-x: auto; border: 1px solid #d4dce7; border-radius: 6px; background: #fff; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px; border-bottom: 1px solid #e4e9f0; text-align: left; white-space: nowrap; }
    th { color: #526175; background: #f6f8fa; }
    tr:last-child td { border-bottom: 0; }
    pre { max-height: 240px; margin: 0; overflow: auto; padding: 12px; border-radius: 6px; background: #202a37; color: #e5edf7; white-space: pre-wrap; }
    .ratio-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
    .section-note { margin: -5px 0 0; color: #526175; font-size: 13px; }
    .ratio-kpi { min-width: 150px; text-align: right; color: #526175; font-size: 12px; }
    .ratio-kpi b { display: block; margin: 2px 0; color: #18212f; font-size: 32px; line-height: 1.1; }
    .ratio-state { display: inline-block; color: var(--amber); font-weight: 700; }
    .ratio-state.achieved { color: var(--green); }
    .ratio-values { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 14px; }
    .ratio-values div { padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; background: #fff; color: #526175; font-size: 12px; }
    .ratio-values b { display: block; margin-top: 4px; color: #18212f; font-size: 17px; }
    .quality-values { grid-template-columns: repeat(5, 1fr); margin-top: 8px; }
    .quality-values b { font-size: 14px; }
    .ratio-chart { position: relative; margin-top: 12px; padding: 16px 14px 10px; border: 1px solid var(--line); border-radius: 6px; background: #fff; }
    .ratio-row { display: grid; grid-template-columns: 78px 1fr; align-items: center; gap: 10px; margin-bottom: 12px; color: #475569; font-size: 13px; font-weight: 700; }
    .ratio-track { position: relative; height: 22px; overflow: visible; border-radius: 4px; background: #e9eef4; }
    .ratio-bar { display: block; width: 0; height: 100%; border-radius: 4px; transition: width .25s ease; }
    .ratio-bar.upload { background: var(--blue); }
    .ratio-bar.download { background: var(--green); }
    #ratioTargetMarker { position: absolute; top: -5px; bottom: -39px; left: 100%; width: 2px; background: var(--amber); transform: translateX(-1px); }
    #ratioTargetMarker::before { content: "2×目标"; position: absolute; right: 4px; top: -13px; color: var(--amber); font-size: 11px; font-style: normal; white-space: nowrap; }
    .ratio-axis { display: flex; justify-content: space-between; margin-left: 88px; color: #64748b; font-size: 11px; }
    .ratio-progress-text { display: flex; justify-content: space-between; margin-top: 8px; color: #475569; font-size: 13px; }
    @media (max-width: 900px) { .metrics, .quality-values { grid-template-columns: repeat(3, 1fr); } form .grid { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 560px) { main { padding: 14px; } header, .ratio-heading { align-items: flex-start; flex-direction: column; } .metrics, .ratio-values { grid-template-columns: repeat(2, 1fr); } .ratio-kpi { text-align: left; } form .grid { grid-template-columns: 1fr; } }
    @media (prefers-reduced-motion: reduce) { .ratio-bar { transition: none; } }
  </style>
</head>
<body>
<main>
  <header>
    <h1>{{TITLE}}</h1>
    <div class="badges"><span id="running" class="badge">加载中</span><span id="window" class="badge">加载中</span><span id="now" class="badge">加载中</span></div>
  </header>
  <div id="apiError" class="badge error"></div>
  <section>
    <div class="toolbar"><button class="primary" onclick="control('/api/start')">启动</button><button class="stop" onclick="control('/api/stop')">停止</button><button class="neutral" onclick="refresh()">刷新</button></div>
  </section>
  <section>
    <h2>实时流量</h2>
    <div class="metrics">
      <div class="metric">当前 Mbps<b id="currentMbps">0</b></div>
      <div class="metric">10 秒均值<b id="avg10Mbps">0</b></div>
      <div class="metric">60 秒均值<b id="avg60Mbps">0</b></div>
      <div class="metric">今日累计<b id="todayBytes">0</b></div>
      <div class="metric">速度达成<b id="targetPercent">0%</b></div>
      <div class="metric">今日倍率进度<b id="dailyPercent">0%</b></div>
      <div class="metric">连接数<b id="workerCount">0</b></div>
    </div>
    <canvas id="trafficChart" width="1120" height="210"></canvas>
    <span id="capacityWarning" class="badge error">源池或线路容量不足</span>
  </section>
  {{FLEET_RATIO_SECTION}}
  <section>
    <h2>运行参数</h2>
    <form id="configForm">
      <div class="grid">
        {{RUNTIME_FIELDS}}
        {{TOPOLOGY_FIELDS}}
        <label class="wide">公共 HTTP/HTTPS 源池<textarea name="source_pool"></textarea></label>
      </div>
      <div class="actions" style="margin-top:10px"><button class="primary" type="submit">保存并应用</button></div>
    </form>
  </section>
  {{SOURCE_LIST_SECTION}}
  {{FLEET_ACCOUNTS_SECTION}}
  {{FLEET_SCHEDULE_SECTION}}
  <section><h2>线路</h2><div class="table-wrap"><table><thead><tr><th>线路</th><th>绑定 IP</th><th>目标</th><th>当前</th><th>60 秒</th><th>15天上传</th><th>15天下载</th><th>倍率</th><th>欠量</th><th>SNMP质量</th><th>连接</th><th>状态</th><th>错误</th></tr></thead><tbody id="lines"></tbody></table></div></section>
  <section><h2>源健康</h2><div class="table-wrap"><table><thead><tr><th>URL</th><th>IPv4</th><th>状态</th><th>连续失败</th><th>隔离/重试</th><th>错误</th></tr></thead><tbody id="sources"></tbody></table></div></section>
  <section><h2>日志</h2><pre id="logs"></pre></section>
</main>
<script>
const chartSamples = [];
function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function api(path, options) { const response = await fetch(path, options); const body = await response.json().catch(() => ({error: `HTTP ${response.status}`})); if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`); return body; }
function showError(error) { const node = document.getElementById('apiError'); node.textContent = error ? String(error.message || error) : ''; node.style.display = error ? 'inline-flex' : 'none'; }
function setForm(config) { for (const [key, value] of Object.entries(config)) { const input = document.querySelector(`[name="${key}"]`); if (input) input.value = Array.isArray(value) ? value.join('\n') : String(value); } }
function fmtBytes(bytes) { if (bytes >= 1e12) return (bytes / 1e12).toFixed(2) + ' TB'; if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + ' GB'; if (bytes >= 1e6) return (bytes / 1e6).toFixed(2) + ' MB'; return String(bytes || 0) + ' B'; }
function drawChart(target) { const canvas = document.getElementById('trafficChart'); const ctx = canvas.getContext('2d'); const width = canvas.width, height = canvas.height; ctx.clearRect(0, 0, width, height); ctx.strokeStyle = '#d6dee8'; for (let i=0;i<=4;i++){const y=8+i*((height-16)/4);ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();} const max=Math.max(target||1,...chartSamples.flatMap(s=>[s.current,s.avg60]))*1.15; const line=(key,color)=>{ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();chartSamples.forEach((sample,index)=>{const x=chartSamples.length<2?0:index*(width/(chartSamples.length-1));const y=height-8-(sample[key]/max)*(height-16);if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();};line('current','#1769aa');line('avg60','#16804b'); }
function setSourceList(sourceList) { if (!document.getElementById('sourceListRevision')) return; document.getElementById('sourceListRevision').textContent=sourceList.revision||'-';document.getElementById('sourceListCount').textContent=String(sourceList.source_count||0);document.getElementById('sourceListOrigin').textContent=sourceList.origin||'local-fallback';document.getElementById('sourceListLastSuccess').textContent=sourceList.last_success_at||'-';document.getElementById('sourceListNextRefresh').textContent=sourceList.next_refresh_at||'-';document.getElementById('sourceListStale').textContent=sourceList.stale?'是':'否';document.getElementById('sourceListError').textContent=sourceList.apply_error||sourceList.last_error||'-';const refreshButton=document.getElementById('sourceListRefresh');if(refreshButton)refreshButton.style.display=sourceList.enabled===false?'none':''; }
function updateFleetRatio(metrics, config) { const chart=document.getElementById('ratioChart');if(!chart)return;const upload=Number(metrics.rolling_upload_bytes||0);const download=Number(metrics.rolling_download_bytes||0);const targetRatio=Number(config.target_ratio||2);const required=Number(metrics.rolling_target_download_bytes??upload*targetRatio);const debt=Math.max(0,Number(metrics.rolling_debt_bytes??required-download));const ratio=upload>0?download/upload:null;const scale=Math.max(required,download,1);const progress=required>0?download/required*100:0;document.getElementById('rollingRatio').textContent=ratio===null?'—':ratio.toFixed(2)+'×';document.getElementById('rollingUpload').textContent=fmtBytes(upload);document.getElementById('rollingDownload').textContent=fmtBytes(download);document.getElementById('rollingRequired').textContent=fmtBytes(required);document.getElementById('rollingDebt').textContent=fmtBytes(debt);document.getElementById('uploadBar').style.width=(upload/scale*100).toFixed(2)+'%';document.getElementById('downloadBar').style.width=(download/scale*100).toFixed(2)+'%';document.getElementById('ratioTargetMarker').style.left=(required/scale*100).toFixed(2)+'%';document.getElementById('ratioScaleEnd').textContent=fmtBytes(scale);document.getElementById('ratioProgress').textContent=progress.toFixed(1)+'%';const state=document.getElementById('ratioState');const achieved=ratio!==null&&ratio>=targetRatio;state.textContent=upload===0?'等待上传账本':achieved?'已达到 '+targetRatio.toFixed(1)+'× 目标':'还差 '+fmtBytes(debt);state.className='ratio-state'+(achieved?' achieved':'');chart.setAttribute('aria-label',`滚动十五天 SNMP 上传 ${fmtBytes(upload)}，SNMP 下载 ${fmtBytes(download)}，当前倍率 ${ratio===null?'暂无':ratio.toFixed(2)+'倍'}，目标 ${targetRatio.toFixed(1)}倍`); }
function updateFleetQuality(metrics) { const measured=document.getElementById('rollingMeasured');if(!measured)return;measured.textContent=fmtBytes(metrics.rolling_measured_upload_bytes)+' / '+fmtBytes(metrics.rolling_measured_download_bytes);document.getElementById('rollingEstimated').textContent=fmtBytes(metrics.rolling_estimated_upload_bytes)+' / '+fmtBytes(metrics.rolling_estimated_download_bytes);document.getElementById('rollingLegacy').textContent=fmtBytes(metrics.rolling_legacy_upload_bytes)+' / '+fmtBytes(metrics.rolling_legacy_download_bytes);document.getElementById('rollingProgram').textContent=fmtBytes(metrics.rolling_program_download_bytes);document.getElementById('rollingUnknown').textContent=Number(metrics.rolling_unknown_seconds||0)+' 秒 / '+Number(metrics.rolling_interruptions||0); }
async function refresh() {
  try {
    const [status, metrics, sources, sourceList] = await Promise.all([api('/api/status'),api('/api/metrics'),api('/api/sources'),api('/api/source-list')]);
    showError();
    document.getElementById('running').textContent=status.running?'运行中':'已停止';
    document.getElementById('window').textContent=status.within_window?'时间窗内':'时间窗外';
    document.getElementById('now').textContent=status.now||'';
    setForm(status.config||{});setSourceList(sourceList||{});updateFleetRatio(metrics,status.config||{});updateFleetQuality(metrics);
    document.getElementById('currentMbps').textContent=Number(metrics.current_mbps||0).toFixed(0);
    document.getElementById('avg10Mbps').textContent=Number(metrics.avg10_mbps||0).toFixed(0);
    document.getElementById('avg60Mbps').textContent=Number(metrics.avg60_mbps||0).toFixed(0);
    document.getElementById('todayBytes').textContent=fmtBytes(metrics.today_bytes);
    document.getElementById('targetPercent').textContent=Number(metrics.target_percent||0).toFixed(0)+'%';
    document.getElementById('dailyPercent').textContent=Number(metrics.daily_target_percent||0).toFixed(0)+'%';
    document.getElementById('workerCount').textContent=String(metrics.worker_count||0);
    document.getElementById('capacityWarning').style.display=metrics.capacity_warning?'inline-flex':'none';
    chartSamples.push({current:Number(metrics.current_mbps||0),avg60:Number(metrics.avg60_mbps||0)});while(chartSamples.length>120)chartSamples.shift();drawChart(metrics.target_mbps);
    document.getElementById('lines').innerHTML=(metrics.lines||[]).map(line=>`<tr><td>${escapeHtml(line.line_id)}</td><td>${escapeHtml(line.bind_ip || '-')}</td><td>${Number(line.target_mbps||0).toFixed(0)}</td><td>${line.metrics_available?Number(line.current_mbps||0).toFixed(0):'-'}</td><td>${line.metrics_available?Number(line.avg60_mbps||0).toFixed(0):'-'}</td><td>${fmtBytes(line.rolling_upload_bytes)}</td><td>${fmtBytes(line.rolling_download_bytes)}</td><td>${line.ratio==null?'-':Number(line.ratio).toFixed(2)+'×'}</td><td>${fmtBytes(line.debt_bytes)}</td><td>${escapeHtml(line.snmp_quality||'pending')}</td><td>${Number(line.connections||0)}/${Number(line.max_connections||0)}</td><td>${escapeHtml(line.status)}</td><td>${escapeHtml(line.last_error)}</td></tr>`).join('');
    document.getElementById('sources').innerHTML=(sources||[]).map(source=>`<tr><td>${escapeHtml(source.url)}</td><td>${escapeHtml(source.ip)}</td><td>${escapeHtml(source.state || (source.healthy?'healthy':'unhealthy'))}</td><td>${Number(source.failures||0)}</td><td>${source.retry_in_seconds?Number(source.retry_in_seconds)+' 秒':'-'}</td><td>${escapeHtml(source.last_error||'')}</td></tr>`).join('');
    document.getElementById('logs').textContent=(status.logs||[]).join('\n');
  } catch (error) { showError(error); }
}
async function control(path) { try { await api(path,{method:'POST'}); await refresh(); } catch(error) { showError(error); } }
async function importFleetAccounts(event) { event.preventDefault(); const file=document.getElementById('fleetAccountsFile').files[0]; if (!file) { showError(new Error('请选择 CSV 文件')); return; } try { const result=await api('/api/fleet/accounts/import',{method:'POST',headers:{'content-type':'text/csv'},body:await file.text()}); showError(); alert(`已导入 ${result.accounts} 个账号`); await refresh(); } catch(error) { showError(error); } }
{{SOURCE_LIST_REFRESH_FUNCTION}}
document.getElementById('configForm').addEventListener('submit', async event => { event.preventDefault(); const data={}; for(const [key,value] of new FormData(event.target).entries()){if(['target_mbps','connections_per_line','max_connections_per_line','line_count','per_line_mbps','rotation_minutes','snmp_poll_seconds','download_ceiling_mbps','line_stall_seconds','line_slow_window_seconds','line_minimum_mbps','line_failure_cooldown_minutes'].includes(key))data[key]=Number(value);else if(key==='target_ratio')data[key]=Number(value);else if(key==='rate_limit_enabled')data[key]=value==='true';else if(['source_pool','lan_ips'].includes(key))data[key]=value.split(/[\n,]+/).map(item=>item.trim()).filter(Boolean);else data[key]=value;} try{await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});await refresh();}catch(error){showError(error);} });
const fleetAccountsForm=document.getElementById('fleetAccountsForm'); if (fleetAccountsForm) fleetAccountsForm.addEventListener('submit', importFleetAccounts);
refresh(); setInterval(refresh, {{REFRESH_MS}});
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    controller: PumperController
    topology_name: str = "ikuai_line"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, render_html(self.topology_name), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, self.controller.status())
        elif path == "/api/metrics":
            self._json(200, self.controller.metrics())
        elif path == "/api/sources":
            self._json(200, self.controller.source_snapshot())
        elif path == "/api/source-list":
            self._json(200, self.controller.source_list_status())
        elif path == "/api/config":
            self._json(200, self.controller.cfg.to_dict())
        elif path == "/api/fleet/accounts" and self.topology_name == "fleet":
            self._json(200, self.controller.accounts_snapshot())
        elif path == "/api/fleet/summary" and self.topology_name == "fleet":
            self._json(200, self.controller.summary())
        elif path == "/api/fleet/report.csv" and self.topology_name == "fleet":
            self._send(200, self.controller.report_csv(), "text/csv; charset=utf-8")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/start":
                self.controller.set_manual_enabled(True)
                self.controller.start_downloads()
                self._json(200, {"ok": True})
            elif path == "/api/stop":
                self.controller.set_manual_enabled(False)
                self._json(200, {"ok": True})
            elif path == "/api/config":
                length = int(self.headers.get("content-length", "0"))
                if length > 1_048_576:
                    raise ValueError("request body is too large")
                data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("configuration payload must be a JSON object")
                self._json(200, self.controller.update_config(data).to_dict())
            elif path == "/api/source-list/refresh":
                status = self.controller.request_source_list_refresh()
                self._json(202, status)
            elif path == "/api/fleet/accounts/import" and self.topology_name == "fleet":
                length = int(self.headers.get("content-length", "0"))
                if length > 1_048_576:
                    raise ValueError("request body is too large")
                content_type = self.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"text/csv", "application/csv"}:
                    raise ValueError("account import requires text/csv")
                self._json(200, self.controller.import_accounts_csv(self.rfile.read(length).decode("utf-8")))
            else:
                self._json(404, {"error": "not found"})
        except SourceListRefreshError as exc:
            self._json(503, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"internal error: {exc}"})

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _send(self, status: int, body: str, content_type: str) -> None:
        body_bytes = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body_bytes)))
        self.send_header("x-content-type-options", "nosniff")
        self.end_headers()
        self.wfile.write(body_bytes)


class PumperHTTPServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int], handler: type[Handler], controller: PumperController) -> None:
        self.controller = controller
        super().__init__(server_address, handler)

    def service_actions(self) -> None:
        self.controller.tick()


def run_web(controller: PumperController, host: str, port: int, topology_name: str) -> None:
    Handler.controller = controller
    Handler.topology_name = topology_name
    server = PumperHTTPServer((host, port), Handler, controller)
    try:
        server.serve_forever()
    finally:
        server.server_close()
