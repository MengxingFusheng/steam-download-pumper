from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .controller import PumperController


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Steam Download Pumper</title>
  <style>
    :root { color-scheme: light dark; font-family: Arial, sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #172033; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 18px; font-size: 26px; }
    section { background: white; border: 1px solid #d9e0ea; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
    label { display: grid; gap: 6px; font-size: 13px; color: #42526b; }
    input, select, textarea { min-height: 38px; border: 1px solid #b8c2d2; border-radius: 6px; padding: 0 10px; font: inherit; }
    textarea { min-height: 92px; padding: 10px; resize: vertical; }
    button { min-height: 38px; border: 0; border-radius: 6px; padding: 0 14px; font-weight: 700; cursor: pointer; }
    .primary { background: #2563eb; color: white; }
    .danger { background: #dc2626; color: white; }
    .muted { background: #e5e7eb; color: #172033; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
    .pill { display: inline-flex; align-items: center; min-height: 28px; padding: 0 10px; border-radius: 999px; background: #e8eef9; color: #1d4ed8; font-weight: 700; }
    .metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 12px; }
    .metric { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; }
    .metric b { display: block; font-size: 20px; margin-top: 4px; }
    canvas { width: 100%; height: 220px; border: 1px solid #e5e7eb; border-radius: 8px; background: #fbfdff; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; border-bottom: 1px solid #e5e7eb; padding: 8px; }
    pre { white-space: pre-wrap; max-height: 280px; overflow: auto; background: #111827; color: #d1d5db; padding: 12px; border-radius: 6px; }
    @media (prefers-color-scheme: dark) {
      body { background: #111827; color: #e5e7eb; }
      section { background: #1f2937; border-color: #374151; }
      label { color: #cbd5e1; }
      input, select, textarea { background: #111827; color: #e5e7eb; border-color: #4b5563; }
      .muted { background: #374151; color: #e5e7eb; }
      .pill { background: #1e3a8a; color: #dbeafe; }
      .metric { border-color: #374151; }
      canvas { background: #111827; border-color: #374151; }
    }
  </style>
</head>
<body>
<main>
  <h1>Steam Download Pumper</h1>
  <section>
    <span id="running" class="pill">loading</span>
    <span id="window" class="pill">loading</span>
    <span id="now" class="pill">loading</span>
    <div class="actions">
      <button class="primary" onclick="control('/api/start')">启动</button>
      <button class="danger" onclick="control('/api/stop')">停止</button>
      <button class="muted" onclick="refresh()">刷新</button>
    </div>
  </section>
  <section>
    <h2>实时流量</h2>
    <div class="metric-grid">
      <div class="metric">当前 Mbps <b id="currentMbps">0</b></div>
      <div class="metric">10 秒均值 <b id="avg10Mbps">0</b></div>
      <div class="metric">60 秒均值 <b id="avg60Mbps">0</b></div>
      <div class="metric">今日累计 <b id="todayBytes">0</b></div>
      <div class="metric">目标达成 <b id="targetPercent">0%</b></div>
      <div class="metric">最低日目标 <b id="dailyPercent">0%</b></div>
    </div>
    <canvas id="trafficChart" width="1100" height="220"></canvas>
    <p id="capacityWarning" class="pill" style="display:none">源池或线路容量不足</p>
  </section>
  <section>
    <h2>参数</h2>
    <form id="configForm">
      <div class="grid">
        <label>目标带宽 Mbps <input name="target_mbps" type="number" min="1"></label>
        <label>外部线路条数 <input name="line_count" type="number" min="2" max="10"></label>
        <label>每条线路连接数 <input name="connections_per_line" type="number" min="1"></label>
        <label>最大每线连接数 <input name="max_connections_per_line" type="number" min="1" max="12"></label>
        <label>出口 IP 模式
          <select name="egress_mode"><option value="single_ip">单 IP：新建连接数分流</option><option value="multi_ip">多 IP：一线一 IP</option></select>
        </label>
        <label>容器主 IP <input name="lan_ip" placeholder="192.168.1.233"></label>
        <label>是否限速
          <select name="rate_limit_enabled"><option value="true">是</option><option value="false">否</option></select>
        </label>
        <label>开始时间 <input name="start_time" type="time"></label>
        <label>结束时间 <input name="end_time" type="time"></label>
        <label>下载模式
          <select name="download_mode"><option value="public_http">公共 HTTP 丢弃</option><option value="steam_tmpfs">Steam tmpfs</option></select>
        </label>
        <label>Steam AppID 列表 <input name="app_ids" placeholder="90"></label>
        <label>循环后删除文件
          <select name="delete_after_cycle"><option value="true">是</option><option value="false">否</option></select>
        </label>
      </div>
      <label style="margin-top:12px">多 IP 列表 <textarea name="lan_ips" placeholder="multi_ip 模式下每行或逗号分隔一个 IP"></textarea></label>
      <label style="margin-top:12px">公共源 URL 池 <textarea name="source_pool" placeholder="每行或逗号分隔一个 http/https URL"></textarea></label>
      <div class="actions"><button class="primary" type="submit">保存并应用</button></div>
    </form>
  </section>
  <section>
    <h2>源健康</h2>
    <table><thead><tr><th>URL</th><th>IPv4</th><th>健康</th><th>失败</th></tr></thead><tbody id="sources"></tbody></table>
  </section>
  <section>
    <h2>爱快 WAN</h2>
    <p id="ikuaiStatus" class="pill">未启用</p>
    <table><thead><tr><th>线路</th><th>下载 Mbps</th><th>占比</th><th>连接数</th></tr></thead><tbody id="wanStats"></tbody></table>
  </section>
  <section>
    <h2>Worker</h2>
    <table><thead><tr><th>ID</th><th>线路</th><th>源 IP</th><th>目标</th><th>状态</th><th>次数</th><th>PID</th><th>错误</th></tr></thead><tbody id="workers"></tbody></table>
  </section>
  <section>
    <h2>日志</h2>
    <pre id="logs"></pre>
  </section>
</main>
<script>
const chartSamples = [];
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
function setForm(cfg) {
  for (const [key, value] of Object.entries(cfg)) {
    const input = document.querySelector(`[name="${key}"]`);
    if (!input) continue;
    input.value = Array.isArray(value) ? value.join('\\n') : String(value);
  }
}
function fmtBytes(bytes) {
  if (bytes >= 1e12) return (bytes / 1e12).toFixed(2) + ' TB';
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + ' GB';
  if (bytes >= 1e6) return (bytes / 1e6).toFixed(2) + ' MB';
  return String(bytes || 0) + ' B';
}
function drawChart(target) {
  const canvas = document.getElementById('trafficChart');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = '#d1d5db';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = 10 + i * ((h - 20) / 4);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  const maxValue = Math.max(target || 1, ...chartSamples.map(s => s.avg60), ...chartSamples.map(s => s.current)) * 1.15;
  function line(values, color) {
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    values.forEach((value, index) => {
      const x = values.length <= 1 ? 0 : index * (w / (values.length - 1));
      const y = h - 10 - (value / maxValue) * (h - 20);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  line(chartSamples.map(s => s.current), '#2563eb');
  line(chartSamples.map(s => s.avg60), '#16a34a');
}
async function refresh() {
  const status = await api('/api/status');
  const metrics = await api('/api/metrics');
  const sources = await api('/api/sources');
  document.getElementById('running').textContent = status.running ? '运行中' : '已停止';
  document.getElementById('window').textContent = status.within_window ? '时间窗内' : '时间窗外';
  document.getElementById('now').textContent = status.now;
  setForm(status.config);
  document.getElementById('currentMbps').textContent = metrics.current_mbps.toFixed(0);
  document.getElementById('avg10Mbps').textContent = metrics.avg10_mbps.toFixed(0);
  document.getElementById('avg60Mbps').textContent = metrics.avg60_mbps.toFixed(0);
  document.getElementById('todayBytes').textContent = fmtBytes(metrics.today_bytes);
  document.getElementById('targetPercent').textContent = metrics.target_percent.toFixed(0) + '%';
  document.getElementById('dailyPercent').textContent = metrics.daily_percent.toFixed(0) + '%';
  document.getElementById('capacityWarning').style.display = metrics.capacity_warning ? 'inline-flex' : 'none';
  const ikuai = metrics.ikuai || { enabled: false, interfaces: [], error: '' };
  document.getElementById('ikuaiStatus').textContent = ikuai.enabled ? (ikuai.error ? `错误: ${ikuai.error}` : '已启用') : '未启用';
  document.getElementById('wanStats').innerHTML = (ikuai.interfaces || []).map(w =>
    `<tr><td>${w.interface}</td><td>${w.download_mbps.toFixed(0)}</td><td>${w.share_percent.toFixed(0)}%</td><td>${w.connect_num}</td></tr>`
  ).join('');
  chartSamples.push({ current: metrics.current_mbps, avg60: metrics.avg60_mbps });
  while (chartSamples.length > 120) chartSamples.shift();
  drawChart(metrics.target_mbps);
  document.getElementById('workers').innerHTML = status.workers.map(w =>
    `<tr><td>${w.worker_id}</td><td>${w.line_index}</td><td>${w.source_ip || ''}</td><td>${w.target || w.app_id || ''}</td><td>${w.status}</td><td>${w.cycles}</td><td>${w.current_pid || ''}</td><td>${w.last_error || ''}</td></tr>`
  ).join('');
  document.getElementById('sources').innerHTML = sources.map(s =>
    `<tr><td>${s.url}</td><td>${s.ip || ''}</td><td>${s.healthy ? '是' : '否'}</td><td>${s.failures || 0}</td></tr>`
  ).join('');
  document.getElementById('logs').textContent = status.logs.join('\\n');
}
async function control(path) {
  await api(path, { method: 'POST' });
  await refresh();
}
document.getElementById('configForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const data = {};
  for (const [key, value] of form.entries()) {
    if (['target_mbps','line_count','connections_per_line','max_connections_per_line'].includes(key)) data[key] = Number(value);
    else if (['rate_limit_enabled','delete_after_cycle'].includes(key)) data[key] = value === 'true';
    else if (key === 'app_ids') data[key] = value.split(',').map(v => v.trim()).filter(Boolean);
    else if (key === 'source_pool' || key === 'lan_ips') data[key] = value.split(/[\\n,]+/).map(v => v.trim()).filter(Boolean);
    else data[key] = value;
  }
  await api('/api/config', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(data) });
  await refresh();
});
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    controller: PumperController

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
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
                data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                self._json(200, self.controller.update_config(data).to_dict())
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _send(self, status: int, body: str, content_type: str) -> None:
        body_bytes = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def run_web(controller: PumperController, host: str, port: int) -> None:
    Handler.controller = controller
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
