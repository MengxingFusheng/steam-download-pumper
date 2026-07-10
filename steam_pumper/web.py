from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from .controller import PumperController


def _topology_fields(topology_name: str) -> str:
    if topology_name == "ikuai_line":
        return ""
    if topology_name == "multi_ip":
        return """
        <label>线路数量<input name="line_count" type="number" min="2" max="10"></label>
        <label class="wide">线路 IPv4 地址<textarea name="lan_ips"></textarea></label>
        """
    raise ValueError(f"unsupported topology: {topology_name}")


def render_html(topology_name: str) -> str:
    title = "iKuai 单线路下载器" if topology_name == "ikuai_line" else "多 IP 宽带下载器"
    return _HTML_TEMPLATE.replace("{{TITLE}}", title).replace("{{TOPOLOGY_FIELDS}}", _topology_fields(topology_name))


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{TITLE}}</title>
  <style>
    :root { font-family: Arial, "Microsoft YaHei", sans-serif; color: #18212f; background: #eef2f6; }
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
    @media (max-width: 900px) { .metrics { grid-template-columns: repeat(3, 1fr); } form .grid { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 560px) { main { padding: 14px; } header { align-items: flex-start; flex-direction: column; } .metrics { grid-template-columns: repeat(2, 1fr); } form .grid { grid-template-columns: 1fr; } }
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
      <div class="metric">今日完成<b id="dailyPercent">0%</b></div>
      <div class="metric">连接数<b id="workerCount">0</b></div>
    </div>
    <canvas id="trafficChart" width="1120" height="210"></canvas>
    <span id="capacityWarning" class="badge error">源池或线路容量不足</span>
  </section>
  <section>
    <h2>运行参数</h2>
    <form id="configForm">
      <div class="grid">
        <label>目标带宽 Mbps<input name="target_mbps" type="number" min="1"></label>
        <label>每线基础连接数<input name="connections_per_line" type="number" min="1" max="12"></label>
        <label>每线最大连接数<input name="max_connections_per_line" type="number" min="1" max="12"></label>
        <label>目标控制<select name="rate_limit_enabled"><option value="true">启用</option><option value="false">禁用</option></select></label>
        <label>开始时间<input name="start_time" type="time"></label>
        <label>结束时间<input name="end_time" type="time"></label>
        {{TOPOLOGY_FIELDS}}
        <label class="wide">公共 HTTP/HTTPS 源池<textarea name="source_pool"></textarea></label>
      </div>
      <div class="actions" style="margin-top:10px"><button class="primary" type="submit">保存并应用</button></div>
    </form>
  </section>
  <section><h2>线路</h2><div class="table-wrap"><table><thead><tr><th>线路</th><th>绑定 IP</th><th>目标</th><th>当前</th><th>60 秒</th><th>连接</th><th>状态</th><th>错误</th></tr></thead><tbody id="lines"></tbody></table></div></section>
  <section><h2>源健康</h2><div class="table-wrap"><table><thead><tr><th>URL</th><th>IPv4</th><th>健康</th><th>失败</th></tr></thead><tbody id="sources"></tbody></table></div></section>
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
async function refresh() { try { const [status, metrics, sources] = await Promise.all([api('/api/status'),api('/api/metrics'),api('/api/sources')]); showError(); document.getElementById('running').textContent=status.running?'运行中':'已停止';document.getElementById('window').textContent=status.within_window?'时间窗内':'时间窗外';document.getElementById('now').textContent=status.now||'';setForm(status.config||{});document.getElementById('currentMbps').textContent=Number(metrics.current_mbps||0).toFixed(0);document.getElementById('avg10Mbps').textContent=Number(metrics.avg10_mbps||0).toFixed(0);document.getElementById('avg60Mbps').textContent=Number(metrics.avg60_mbps||0).toFixed(0);document.getElementById('todayBytes').textContent=fmtBytes(metrics.today_bytes);document.getElementById('targetPercent').textContent=Number(metrics.target_percent||0).toFixed(0)+'%';document.getElementById('dailyPercent').textContent=Number(metrics.daily_target_percent||0).toFixed(0)+'%';document.getElementById('workerCount').textContent=String(metrics.worker_count||0);document.getElementById('capacityWarning').style.display=metrics.capacity_warning?'inline-flex':'none';chartSamples.push({current:Number(metrics.current_mbps||0),avg60:Number(metrics.avg60_mbps||0)});while(chartSamples.length>120)chartSamples.shift();drawChart(metrics.target_mbps);document.getElementById('lines').innerHTML=(metrics.lines||[]).map(line=>`<tr><td>${escapeHtml(line.line_id)}</td><td>${escapeHtml(line.bind_ip || '-')}</td><td>${Number(line.target_mbps||0).toFixed(0)}</td><td>${line.metrics_available?Number(line.current_mbps||0).toFixed(0):'-'}</td><td>${line.metrics_available?Number(line.avg60_mbps||0).toFixed(0):'-'}</td><td>${Number(line.connections||0)}/${Number(line.max_connections||0)}</td><td>${escapeHtml(line.status)}</td><td>${escapeHtml(line.last_error)}</td></tr>`).join('');document.getElementById('sources').innerHTML=(sources||[]).map(source=>`<tr><td>${escapeHtml(source.url)}</td><td>${escapeHtml(source.ip)}</td><td>${source.healthy?'是':'否'}</td><td>${Number(source.failures||0)}</td></tr>`).join('');document.getElementById('logs').textContent=(status.logs||[]).join('\n'); } catch (error) { showError(error); } }
async function control(path) { try { await api(path,{method:'POST'}); await refresh(); } catch(error) { showError(error); } }
document.getElementById('configForm').addEventListener('submit', async event => { event.preventDefault(); const data={}; for(const [key,value] of new FormData(event.target).entries()){if(['target_mbps','connections_per_line','max_connections_per_line','line_count'].includes(key))data[key]=Number(value);else if(key==='rate_limit_enabled')data[key]=value==='true';else if(['source_pool','lan_ips'].includes(key))data[key]=value.split(/[\n,]+/).map(item=>item.trim()).filter(Boolean);else data[key]=value;} try{await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});await refresh();}catch(error){showError(error);} });
refresh(); setInterval(refresh, 5000);
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
        elif path == "/api/config":
            self._json(200, self.controller.cfg.to_dict())
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
            else:
                self._json(404, {"error": "not found"})
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


def run_web(controller: PumperController, host: str, port: int, topology_name: str) -> None:
    Handler.controller = controller
    Handler.topology_name = topology_name
    server = HTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
