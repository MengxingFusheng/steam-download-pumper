from __future__ import annotations

import json
from datetime import date as calendar_date
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

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
        <label>路由 SNMP 接口<input name="router_snmp_interface" type="text" maxlength="128"></label>
        <label>全路由下载预算 Mbps<input name="router_limit_mbps" type="number" min="1" max="5000"></label>
        <label>安全预留 Mbps<input name="router_reserve_mbps" type="number" min="0" max="4999"></label>
        <label>23–24 点补量上限 Mbps<input name="late_download_limit_mbps" type="number" min="1" max="2000"></label>
        <label>WAN 有效数据期限秒<input name="wan_stale_seconds" type="number" min="60" max="120"></label>
        <label>最短轮转分钟<input name="rotation_minutes" type="number" min="1" max="240"></label>
        <label>每日目标倍率<input name="target_ratio" type="number" min="1" max="10" step="0.1"></label>
        <label>SNMP 采样秒数<input name="snmp_poll_seconds" type="number" min="15" max="900"></label>
        <label>SNMP 下行物理校验上限 Mbps<input name="download_ceiling_mbps" type="number" min="1" max="10000"></label>
        <label>无进度换线秒数<input name="line_stall_seconds" type="number" min="30" max="900"></label>
        <label>低速判断窗口秒数<input name="line_slow_window_seconds" type="number" min="30" max="1800"></label>
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
    <h2>每日预测分组调度</h2>
    <p>每日 Asia/Shanghai 00:00–24:00 独立对账，昨日欠量不结转。所有 Fleet 节点统一使用以下规则；爱快负责全路由 5G 硬限速。</p>
    <div class="table-wrap"><table><thead><tr><th>时间（Asia/Shanghai）</th><th>下载规则</th></tr></thead><tbody>
      <tr><td>00:00–18:00</td><td>按线路、分组及路由预算下载，争取达到预计全天上传的 2 倍</td></tr>
      <tr><td>18:00–23:00</td><td>停止下载，继续采集 SNMP</td></tr>
      <tr><td>23:00–24:00</td><td>仅补当天缺口，Fleet 合计最多 2 Gbps</td></tr>
    </tbody></table></div>
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
      <div class="metric">已接收版本<b id="sourceListRevision">-</b></div>
      <div class="metric">已应用版本<b id="sourceListActiveRevision">-</b></div>
      <div class="metric">应用状态<b id="sourceListApplyState">-</b></div>
      <div class="metric">源数量<b id="sourceListCount">0</b></div>
      <div class="metric">Origin<b id="sourceListOrigin">local-fallback</b></div>
      <div class="metric">最后成功<b id="sourceListLastSuccess">-</b></div>
      <div class="metric">下次刷新<b id="sourceListNextRefresh">-</b></div>
      <div class="metric">过期状态<b id="sourceListStale">否</b></div>
      <div class="metric">错误<b id="sourceListError">-</b></div>
    </div>
    <p id="sourceListLineErrors" class="section-note" role="status"></p>
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
    <p>上传 CSV（必填列：account_id、bind_ip、snmp_interface、weight；可选 enabled、group_id）。weight 仅接受 1（50M 上传 / 200M 下载）或 10（500M 对等）；全部启用账号须确认带宽档案后才能下载。未指定 group_id 时进入对应默认组。整批校验，任一行错误不会变更当前映射。</p>
    <form id="fleetAccountsForm" class="toolbar">
      <label class="file-label" for="fleetAccountsFile">账号 CSV<input id="fleetAccountsFile" type="file" accept=".csv,text/csv" required></label>
      <button class="primary" type="submit">上传账号 CSV</button>
    </form>
    <p id="fleetImportFeedback" class="form-feedback" role="status" aria-live="polite"></p>
  </section>
    """


def _fleet_daily_section(topology_name: str) -> str:
    if topology_name != "fleet":
        return ""
    return """
  <section id="fleetDailySection" aria-labelledby="fleetDailyHeading">
    <div class="ratio-heading">
      <div><h2 id="fleetDailyHeading">当天上下行与预测目标</h2><p class="section-note">SNMP 实际流量独立记账；预测与未知区间保守预算不写入实际账本。历史不足 3 个有效日的区间，按 18–24 点额定上传上限保守预测。</p></div>
      <span id="dailyModeState" class="badge">每日预测调度</span>
    </div>
    <p id="dailyReadinessWarning" class="notice" role="status">请先导入带 weight 的账号 CSV，确认普通线路与 500M 线路档案。</p>
    <p id="dailyDiagnostics" class="section-note" role="status"></p>
    <div class="ratio-values">
      <div><span>当日 SNMP 上传</span><b id="dailyUpload">0 B</b></div>
      <div><span>当日 SNMP 下载</span><b id="dailyDownload">0 B</b></div>
      <div><span>预计全天上传</span><b id="dailyPredictedUpload">0 B</b></div>
      <div><span>当天目标下载 / 剩余</span><b id="dailyTargetDebt">0 B / 0 B</b></div>
    </div>
    <div id="dailyChart" class="ratio-chart" role="img" aria-label="当天上下行与预测目标数据尚未加载">
      <div class="ratio-row daily-chart-row"><span>实际上传</span><div class="ratio-track"><i id="dailyUploadBar" class="ratio-bar upload"></i></div><b id="dailyUploadLabel">0 B</b></div>
      <div class="ratio-row daily-chart-row"><span>实际下载</span><div class="ratio-track"><i id="dailyDownloadBar" class="ratio-bar download"></i></div><b id="dailyDownloadLabel">0 B</b></div>
      <div class="ratio-row daily-chart-row"><span>预测目标</span><div class="ratio-track"><i id="dailyTargetBar" class="ratio-bar forecast"></i></div><b id="dailyTargetLabel">0 B</b></div>
      <p class="chart-legend">蓝色：SNMP 上传 · 绿色：SNMP 下载 · 斜纹：预计全天上传 × 2</p>
    </div>
    <div class="ratio-progress-text"><span id="dailyDatePhase">等待当日预测</span><b id="dailyProgress">0%</b></div>
    <div class="ratio-values quality-values">
      <div><span>实测 上 / 下</span><b id="dailyMeasured">0 B / 0 B</b></div>
      <div><span>估算 上 / 下</span><b id="dailyEstimated">0 B / 0 B</b></div>
      <div><span>未知时长 / 保守上传预算</span><b id="dailyUnknown">0 秒 / 0 B</b></div>
      <div><span>剩余上传预测</span><b id="dailyFutureUpload">0 B</b></div>
      <div><span>达标账号 / 启用账号</span><b id="dailyAchieved">0 / 0</b></div>
    </div>
    <p id="dailyResults" class="section-note result-note">18 点前结果与晚间补量后全天结果将在对应时段结束后显示。</p>
    <div class="toolbar report-toolbar"><label for="dailyReportDate">每日对账日期<input id="dailyReportDate" type="date"></label><a id="dailyReportExport" class="button-link neutral" href="/api/fleet/report.csv?period=daily" download>导出每日 CSV</a><a class="button-link neutral" href="/api/fleet/report.csv" download>导出滚动 15 天 CSV</a></div>
  </section>
  <section aria-labelledby="routerBudgetHeading">
    <h2 id="routerBudgetHeading">路由下行预算（分钟级近似）</h2>
    <div class="metrics budget-metrics">
      <div class="metric">接口 / 数据状态<b id="budgetStatus">wan1 / 等待基线</b></div>
      <div class="metric">WAN 下行 Mbps<b id="wanDownMbps">—</b></div>
      <div class="metric">Fleet 60 秒均值 Mbps<b id="budgetFleetMbps">0</b></div>
      <div class="metric">其它业务约 Mbps<b id="otherDownMbps">0</b></div>
      <div class="metric">可用 / 时段额度 Mbps<b id="availableBudgetMbps">0 / 0</b></div>
      <div class="metric">最后有效采样<b id="wanLastSample">—</b></div>
    </div>
    <p class="section-note result-note">其它业务 = max(0, WAN 下行 − Fleet 近 60 秒均值)。两个采样窗口无需严格对齐；WAN 基线、重置或数据过期时暂停每日模式下载，取得两次有效采样后恢复。</p>
  </section>
    """


def _fleet_groups_section(topology_name: str) -> str:
    if topology_name != "fleet":
        return ""
    return """
  <section id="fleetGroupsSection" aria-labelledby="fleetGroupsHeading">
    <h2 id="fleetGroupsHeading">分组配置与批量调整</h2>
    <p class="section-note">权重决定单线档案，分组控制调度预算与并发；所有分组合计最多 20 个下载进程。程序不做下载硬限速，请在爱快配置 weight 1 线路 200M、weight 10 线路 500M 下载限速及全路由 5G 硬限速。</p>
    <form id="fleetGroupsForm">
      <div class="table-wrap"><table><thead><tr><th>分组 ID</th><th>名称</th><th>下载预算上限 Mbps</th><th>最大活跃账号</th><th>启用</th><th>当前运行 / 预算分配 Mbps</th></tr></thead><tbody id="fleetGroupsRows"></tbody></table></div>
      <div class="actions" style="margin-top:10px"><button class="primary" type="submit">保存分组配置</button><button id="addFleetGroup" class="neutral" type="button">新增分组</button></div>
    </form>
    <p id="fleetGroupsFeedback" class="form-feedback" role="status" aria-live="polite"></p>
    <form id="fleetGroupAssignForm" class="toolbar"><label for="fleetAssignGroup">目标分组<select id="fleetAssignGroup" required></select></label><span id="fleetSelectedCount">已选 0 条线路</span><button class="neutral" type="submit">调整已选线路分组</button><button class="neutral" type="button" id="clearFleetSelection">清空选择</button></form>
    <p class="section-note result-note">在下方线路表中勾选账号。调整分组不会修改爱快配置；当前额度可能立即重新分配。</p>
    <p id="fleetAssignFeedback" class="form-feedback" role="status" aria-live="polite"></p>
  </section>
    """


def _fleet_line_headers(topology_name: str) -> str:
    if topology_name != "fleet":
        return ""
    return '<th><label class="select-label"><input id="fleetSelectAll" type="checkbox">全选</label></th><th>档案（上/下 Mbps）</th><th>分组</th><th>当天上传</th><th>当天下载</th><th>当日目标 / 进度</th>'


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
        .replace("{{FLEET_DAILY_SECTION}}", _fleet_daily_section(topology_name))
        .replace("{{FLEET_GROUPS_SECTION}}", _fleet_groups_section(topology_name))
        .replace("{{FLEET_LINE_HEADERS}}", _fleet_line_headers(topology_name))
        .replace("{{TARGET_HEADING}}", "预算分配 Mbps" if topology_name == "fleet" else "目标 Mbps")
        .replace("{{DAILY_PROGRESS_LABEL}}", "当日实测倍率进度" if topology_name == "fleet" else "今日倍率进度")
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
    button:disabled { opacity: .55; cursor: wait; }
    button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 3px solid var(--blue); outline-offset: 3px; }
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
    .ratio-bar { display: block; width: 0; height: 100%; border-radius: 4px; }
    .ratio-bar.upload { background: var(--blue); }
    .ratio-bar.download { background: var(--green); }
    .ratio-bar.forecast { background: repeating-linear-gradient(135deg, #b6790d, #b6790d 6px, #e7bc6c 6px, #e7bc6c 10px); }
    .daily-chart-row { grid-template-columns: 78px 1fr 100px; }
    .daily-chart-row b { text-align: right; color: #18212f; font-size: 13px; }
    .chart-legend { margin: 4px 0 0; color: #475569; font-size: 12px; }
    .notice { padding: 10px 12px; border: 1px solid #d9b566; border-radius: 6px; background: #fff8e6; color: #795300; font-size: 13px; }
    .form-feedback { min-height: 20px; margin: 8px 0; color: #475569; font-size: 13px; }
    .form-feedback.failure { color: #991b1b; }
    .result-note { margin-top: 12px; }
    .button-link { display: inline-flex; align-items: center; min-height: 38px; padding: 0 14px; border: 1px solid #b7c4d4; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 700; }
    .report-toolbar { margin-top: 12px; align-items: end; }
    .budget-metrics { grid-template-columns: repeat(6, minmax(120px, 1fr)); }
    .budget-metrics b { font-size: 16px; }
    .file-label { flex: 1; min-width: 180px; }
    #fleetGroupsRows input[type="text"] { min-width: 135px; }
    #fleetGroupsRows input[type="number"] { min-width: 110px; }
    #fleetGroupAssignForm { margin-top: 14px; align-items: end; }
    #fleetGroupAssignForm label { min-width: 170px; }
    .select-label { display: inline-flex; align-items: center; gap: 6px; min-height: 36px; }
    input[type="checkbox"] { width: 20px; height: 20px; min-height: 20px; accent-color: var(--blue); }
    .line-progress { width: 120px; height: 6px; margin-top: 5px; border-radius: 3px; background: #e9eef4; overflow: hidden; }
    .line-progress i { display: block; height: 100%; background: var(--green); }
    .line-progress-label { font-size: 12px; color: #475569; }
    #ratioTargetMarker { position: absolute; top: -5px; bottom: -39px; left: 100%; width: 2px; background: var(--amber); transform: translateX(-1px); }
    #ratioTargetMarker::before { content: "2×目标"; position: absolute; right: 4px; top: -13px; color: var(--amber); font-size: 11px; font-style: normal; white-space: nowrap; }
    .ratio-axis { display: flex; justify-content: space-between; margin-left: 88px; color: #64748b; font-size: 11px; }
    .ratio-progress-text { display: flex; justify-content: space-between; margin-top: 8px; color: #475569; font-size: 13px; }
    @media (max-width: 900px) { .metrics, .quality-values { grid-template-columns: repeat(3, 1fr); } form .grid { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 560px) { main { padding: 14px; } header, .ratio-heading { align-items: flex-start; flex-direction: column; } .metrics, .ratio-values { grid-template-columns: repeat(2, minmax(0, 1fr)); } .ratio-kpi { text-align: left; } form .grid { grid-template-columns: 1fr; } button, .button-link, input:not([type="checkbox"]), select { min-height: 44px; } .daily-chart-row { grid-template-columns: 70px 1fr; gap: 8px; } .daily-chart-row b { grid-column: 2; text-align: left; } .ratio-values b { font-size: 15px; overflow-wrap: anywhere; } .report-toolbar label, .report-toolbar a { width: 100%; } }
    @media (prefers-reduced-motion: reduce) { .ratio-bar { transition: none; } }
  </style>
</head>
<body>
<main>
  <header>
    <h1>{{TITLE}}</h1>
    <div class="badges"><span id="running" class="badge">加载中</span><span id="window" class="badge">加载中</span><span id="now" class="badge">加载中</span></div>
  </header>
  <div id="apiError" class="badge error" role="alert"></div>
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
      <div class="metric">{{DAILY_PROGRESS_LABEL}}<b id="dailyPercent">0%</b></div>
      <div class="metric">连接数<b id="workerCount">0</b></div>
    </div>
    <canvas id="trafficChart" width="1120" height="210" role="img" aria-label="最近 20 分钟下载速率：蓝线为当前速率，绿线为 60 秒均值"></canvas>
    <span id="capacityWarning" class="badge error">源池或线路容量不足</span>
  </section>
  {{FLEET_DAILY_SECTION}}
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
  {{FLEET_GROUPS_SECTION}}
  {{FLEET_SCHEDULE_SECTION}}
  <section><h2>线路</h2><div class="table-wrap"><table><thead><tr>{{FLEET_LINE_HEADERS}}<th>线路</th><th>绑定 IP</th><th>{{TARGET_HEADING}}</th><th>当前 Mbps</th><th>60 秒 Mbps</th><th>15天上传</th><th>15天下载</th><th>倍率</th><th>欠量</th><th>SNMP质量</th><th>连接</th><th>状态</th><th>错误</th></tr></thead><tbody id="lines"></tbody></table></div></section>
  <section><h2>源健康</h2><div class="table-wrap"><table><thead><tr><th>URL</th><th>IPv4</th><th>状态</th><th>连续失败</th><th>隔离/重试</th><th>错误</th></tr></thead><tbody id="sources"></tbody></table></div></section>
  <section><h2>日志</h2><pre id="logs"></pre></section>
</main>
<script>
const chartSamples = [];
const fleetSelectedAccounts = new Set();
let configDirty = false;
let fleetGroupsDirty = false;
let fleetGroupsSignature = '';
let refreshInFlight = false;
let latestFleetLines = [];
function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function api(path, options) { const response = await fetch(path, options); const body = await response.json().catch(() => ({error: `HTTP ${response.status}`})); if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`); return body; }
function showError(error) { const node = document.getElementById('apiError'); node.textContent = error ? String(error.message || error) : ''; node.style.display = error ? 'inline-flex' : 'none'; }
function setForm(config) { if(configDirty)return;for (const [key, value] of Object.entries(config)) { const input = document.querySelector(`[name="${key}"]`); if (input) input.value = Array.isArray(value) ? value.join('\n') : String(value); } }
function fmtBytes(bytes) { if (bytes >= 1e12) return (bytes / 1e12).toFixed(2) + ' TB'; if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + ' GB'; if (bytes >= 1e6) return (bytes / 1e6).toFixed(2) + ' MB'; return String(bytes || 0) + ' B'; }
function drawChart(target) { const canvas = document.getElementById('trafficChart'); const ctx = canvas.getContext('2d'); const width = canvas.width, height = canvas.height; ctx.clearRect(0, 0, width, height); ctx.strokeStyle = '#d6dee8'; for (let i=0;i<=4;i++){const y=8+i*((height-16)/4);ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();} const max=Math.max(target||1,...chartSamples.flatMap(s=>[s.current,s.avg60]))*1.15; const line=(key,color)=>{ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();chartSamples.forEach((sample,index)=>{const x=chartSamples.length<2?0:index*(width/(chartSamples.length-1));const y=height-8-(sample[key]/max)*(height-16);if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.stroke();};line('current','#1769aa');line('avg60','#16804b'); }
function setSourceList(sourceList) { if (!document.getElementById('sourceListRevision')) return; document.getElementById('sourceListRevision').textContent=sourceList.revision||'-';document.getElementById('sourceListActiveRevision').textContent=sourceList.active_revision||'-';document.getElementById('sourceListApplyState').textContent=({applied:'已应用',pending:'等待确认',partial:'部分未应用',failed:'应用失败'}[sourceList.apply_state]||'未知');document.getElementById('sourceListLineErrors').textContent=(sourceList.lines||[]).filter(line=>line.apply_state!=='applied').map(line=>line.line_id+' · '+(line.error||'等待应用确认')).join('；');document.getElementById('sourceListCount').textContent=String(sourceList.source_count||0);document.getElementById('sourceListOrigin').textContent=sourceList.origin||'local-fallback';document.getElementById('sourceListLastSuccess').textContent=sourceList.last_success_at||'-';document.getElementById('sourceListNextRefresh').textContent=sourceList.next_refresh_at||'-';document.getElementById('sourceListStale').textContent=sourceList.stale?'是':'否';document.getElementById('sourceListError').textContent=sourceList.apply_error||sourceList.last_error||'-';const refreshButton=document.getElementById('sourceListRefresh');if(refreshButton)refreshButton.style.display=sourceList.enabled===false?'none':''; }
function updateFleetRatio(metrics, config) { const chart=document.getElementById('ratioChart');if(!chart)return;const upload=Number(metrics.rolling_upload_bytes||0);const download=Number(metrics.rolling_download_bytes||0);const targetRatio=Number(config.target_ratio||2);const required=Number(metrics.rolling_target_download_bytes??upload*targetRatio);const debt=Math.max(0,Number(metrics.rolling_debt_bytes??required-download));const ratio=upload>0?download/upload:null;const scale=Math.max(required,download,1);const progress=required>0?download/required*100:0;document.getElementById('rollingRatio').textContent=ratio===null?'—':ratio.toFixed(2)+'×';document.getElementById('rollingUpload').textContent=fmtBytes(upload);document.getElementById('rollingDownload').textContent=fmtBytes(download);document.getElementById('rollingRequired').textContent=fmtBytes(required);document.getElementById('rollingDebt').textContent=fmtBytes(debt);document.getElementById('uploadBar').style.width=(upload/scale*100).toFixed(2)+'%';document.getElementById('downloadBar').style.width=(download/scale*100).toFixed(2)+'%';document.getElementById('ratioTargetMarker').style.left=(required/scale*100).toFixed(2)+'%';document.getElementById('ratioScaleEnd').textContent=fmtBytes(scale);document.getElementById('ratioProgress').textContent=progress.toFixed(1)+'%';const state=document.getElementById('ratioState');const achieved=ratio!==null&&ratio>=targetRatio;state.textContent=upload===0?'等待上传账本':achieved?'已达到 '+targetRatio.toFixed(1)+'× 目标':'还差 '+fmtBytes(debt);state.className='ratio-state'+(achieved?' achieved':'');chart.setAttribute('aria-label',`滚动十五天 SNMP 上传 ${fmtBytes(upload)}，SNMP 下载 ${fmtBytes(download)}，当前倍率 ${ratio===null?'暂无':ratio.toFixed(2)+'倍'}，目标 ${targetRatio.toFixed(1)}倍`); }
function updateFleetQuality(metrics) { const measured=document.getElementById('rollingMeasured');if(!measured)return;measured.textContent=fmtBytes(metrics.rolling_measured_upload_bytes)+' / '+fmtBytes(metrics.rolling_measured_download_bytes);document.getElementById('rollingEstimated').textContent=fmtBytes(metrics.rolling_estimated_upload_bytes)+' / '+fmtBytes(metrics.rolling_estimated_download_bytes);document.getElementById('rollingLegacy').textContent=fmtBytes(metrics.rolling_legacy_upload_bytes)+' / '+fmtBytes(metrics.rolling_legacy_download_bytes);document.getElementById('rollingProgram').textContent=fmtBytes(metrics.rolling_program_download_bytes);document.getElementById('rollingUnknown').textContent=Number(metrics.rolling_unknown_seconds||0)+' 秒 / '+Number(metrics.rolling_interruptions||0); }
function updateFleetDaily(metrics, config) {
  const chart=document.getElementById('dailyChart');if(!chart)return;
  const daily=metrics.daily||{};const budget=metrics.router_budget||{};
  const upload=Number(daily.today_upload_bytes||0), download=Number(daily.today_download_bytes||0);
  const target=Number(daily.target_download_bytes||0), predicted=Number(daily.predicted_upload_bytes||0);
  const debt=Math.max(0,Number(daily.remaining_download_bytes??target-download));
  const scale=Math.max(upload,download,target,1);const progress=target>0?download/target*100:0;
  document.getElementById('dailyModeState').textContent='每日预测调度';
  document.getElementById('dailyDiagnostics').textContent=(metrics.diagnostics||[]).map(item=>item.line_id+' · '+item.message).join('；');
  document.getElementById('dailyUpload').textContent=fmtBytes(upload);
  document.getElementById('dailyDownload').textContent=fmtBytes(download);
  document.getElementById('dailyPredictedUpload').textContent=fmtBytes(predicted);
  document.getElementById('dailyTargetDebt').textContent=fmtBytes(target)+' / '+fmtBytes(debt);
  for(const [key,value] of [['Upload',upload],['Download',download],['Target',target]]) {
    document.getElementById('daily'+key+'Bar').style.width=(value/scale*100).toFixed(2)+'%';
    document.getElementById('daily'+key+'Label').textContent=fmtBytes(value);
  }
  chart.setAttribute('aria-label',`当日 SNMP 上传 ${fmtBytes(upload)}，下载 ${fmtBytes(download)}，预计目标 ${fmtBytes(target)}，剩余 ${fmtBytes(debt)}`);
  document.getElementById('dailyProgress').textContent=progress.toFixed(1)+'%';
  const phaseLabels={daytime:'00–18 点预测下载',day:'00–18 点预测下载',upload_only:'18–23 点停止下载',evening_override:'今晚指定线路临时下载',quiet:'18–23 点停止下载',paused:'18–23 点停止下载',late_makeup:'23–24 点当天补量',late:'23–24 点当天补量',night:'23–24 点当天补量'};
  document.getElementById('dailyDatePhase').textContent=(daily.date||'等待日期')+' · '+(phaseLabels[daily.phase]||daily.phase||'等待预测');
  document.getElementById('dailyMeasured').textContent=fmtBytes(daily.measured_upload_bytes)+' / '+fmtBytes(daily.measured_download_bytes);
  document.getElementById('dailyEstimated').textContent=fmtBytes(daily.estimated_upload_bytes)+' / '+fmtBytes(daily.estimated_download_bytes);
  document.getElementById('dailyUnknown').textContent=Number(daily.unknown_seconds||0)+' 秒 / '+fmtBytes(daily.unknown_upload_budget_bytes);
  document.getElementById('dailyFutureUpload').textContent=fmtBytes(daily.future_upload_bytes);
  document.getElementById('dailyAchieved').textContent=Number(daily.achieved_accounts||0)+' / '+Number(daily.total_accounts||0);
  const warning=document.getElementById('dailyReadinessWarning');const unconfirmed=Number(daily.profile_unconfirmed_accounts||0);
  warning.hidden=unconfirmed===0;warning.textContent=`有 ${unconfirmed} 个启用账号尚未确认 weight 档案，请上传完整带 weight 的 CSV 后启动下载。`;
  const pre18=daily.pre18_result,final=daily.final_result;
  const resultText=result=>typeof result==='string'?result:`下载 ${fmtBytes(result.download_bytes)} / 目标 ${fmtBytes(result.target_download_bytes)}${result.achieved_accounts!=null?'，达标 '+result.achieved_accounts+' 条':''}`;
  document.getElementById('dailyResults').textContent=(pre18?'18 点前结果：'+resultText(pre18):'18 点前结果：尚未结算')+'；'+(final?'全天结果：'+resultText(final):'全天结果：晚间补量结束后结算');
  const budgetStatusLabels={pending:'等待采样',baseline:'建立基线',reset:'计数器重置',stale:'有效数据过期',ok:'数据有效',missing:'接口缺失',unavailable:'接口不可用',anomaly:'异常增量'};
  document.getElementById('budgetStatus').textContent=(budget.interface||config.router_snmp_interface||'wan1')+' / '+(budgetStatusLabels[budget.status]||budget.status||'等待基线');
  document.getElementById('wanDownMbps').textContent=budget.wan_down_mbps==null?'—':Number(budget.wan_down_mbps).toFixed(0);
  document.getElementById('budgetFleetMbps').textContent=Number(budget.fleet_avg60_mbps||0).toFixed(0);
  document.getElementById('otherDownMbps').textContent=Number(budget.other_down_mbps||0).toFixed(0);
  document.getElementById('availableBudgetMbps').textContent=Number(budget.available_mbps||0).toFixed(0)+' / '+Number(budget.phase_limit_mbps||0).toFixed(0);
  document.getElementById('wanLastSample').textContent=budget.last_sample_at||'—';
  const reportDate=document.getElementById('dailyReportDate');if(!reportDate.value&&daily.date){reportDate.value=daily.date;updateDailyReportLink();}
}
function updateDailyReportLink() { const value=document.getElementById('dailyReportDate').value;document.getElementById('dailyReportExport').href='/api/fleet/report.csv?period=daily'+(value?'&date='+encodeURIComponent(value):''); }
function fleetGroupRow(group, isNew=false) {
  const id=escapeHtml(group.group_id||'');
  return `<tr data-group="${id}"><td><input data-field="group_id" type="text" value="${id}" maxlength="64" required ${isNew?'':'readonly'} aria-label="分组 ID"></td><td><input data-field="name" type="text" value="${escapeHtml(group.name||'')}" maxlength="128" required aria-label="${id} 名称"></td><td><input data-field="download_limit_mbps" type="number" min="1" max="10000" value="${Number(group.download_limit_mbps||1)}" required aria-label="${id} 下载上限 Mbps"></td><td><input data-field="max_active" type="number" min="1" max="20" value="${Number(group.max_active||1)}" required aria-label="${id} 最大活跃账号"></td><td><label class="select-label"><input data-field="enabled" type="checkbox" ${group.enabled!==false?'checked':''}>启用</label></td><td data-runtime>${Number(group.active_accounts||0)} / ${Number(group.assigned_mbps||0).toFixed(0)}</td></tr>`;
}
function updateFleetGroups(groups) {
  const rows=document.getElementById('fleetGroupsRows');if(!rows)return;
  const signature=JSON.stringify(groups.map(group=>[group.group_id,group.name,group.download_limit_mbps,group.max_active,group.enabled]));
  if(!fleetGroupsDirty&&signature!==fleetGroupsSignature){rows.innerHTML=groups.map(group=>fleetGroupRow(group)).join('');fleetGroupsSignature=signature;}
  for(const row of rows.querySelectorAll('tr')){const group=groups.find(value=>value.group_id===row.dataset.group);if(group)row.querySelector('[data-runtime]').textContent=Number(group.active_accounts||0)+' / '+Number(group.assigned_mbps||0).toFixed(0);}
  const select=document.getElementById('fleetAssignGroup');const previous=select.value;
  select.innerHTML=groups.map(group=>`<option value="${escapeHtml(group.group_id)}">${escapeHtml(group.name)} (${escapeHtml(group.group_id)})${group.enabled===false?' · 已禁用':''}</option>`).join('');
  if(groups.some(group=>group.group_id===previous))select.value=previous;
}
function updateFleetSelection() {
  const count=document.getElementById('fleetSelectedCount');if(!count)return;count.textContent='已选 '+fleetSelectedAccounts.size+' 条线路';
  const all=document.getElementById('fleetSelectAll');all.checked=latestFleetLines.length>0&&latestFleetLines.every(line=>fleetSelectedAccounts.has(String(line.account_id||line.line_id)));all.indeterminate=fleetSelectedAccounts.size>0&&!all.checked;
}
function renderLines(lines) {
  const fleet=Boolean(document.getElementById('fleetGroupsSection'));latestFleetLines=lines;
  if(fleet){const ids=new Set(lines.map(line=>String(line.account_id||line.line_id)));for(const id of fleetSelectedAccounts)if(!ids.has(id))fleetSelectedAccounts.delete(id);}
  document.getElementById('lines').innerHTML=lines.map(line=>{
    const id=String(line.account_id||line.line_id);const daily=line.daily||{};
    const target=Number(daily.target_download_bytes||0),download=Number(daily.today_download_bytes||0);const progress=target>0?download/target*100:0;
    const forecastQuality={'cold-start':'冷启动保守预测',historical:'历史预测',mixed:'混合预测'}[daily.forecast_quality]||daily.forecast_quality||'等待预测';
    const fleetCells=fleet?`<td><label class="select-label"><input class="fleet-account-select" data-account="${escapeHtml(id)}" type="checkbox" ${fleetSelectedAccounts.has(id)?'checked':''} aria-label="选择 ${escapeHtml(id)}"></label></td><td>${line.profile_confirmed===false?'未确认':Number(line.upload_mbps||50)+' / '+Number(line.download_mbps||200)}${line.weight!=null?'（w'+Number(line.weight)+'）':''}</td><td>${escapeHtml(line.group_id||'-')}</td><td>${fmtBytes(daily.today_upload_bytes)}</td><td>${fmtBytes(download)}</td><td>${fmtBytes(target)}<div class="line-progress" role="img" aria-label="当日下载目标完成 ${progress.toFixed(1)}%"><i style="width:${Math.min(100,Math.max(0,progress)).toFixed(1)}%"></i></div><span class="line-progress-label">${progress.toFixed(1)}% · 欠 ${fmtBytes(daily.remaining_download_bytes)}<br>${escapeHtml(forecastQuality)}</span></td>`:'';
    return `<tr>${fleetCells}<td>${escapeHtml(line.line_id)}</td><td>${escapeHtml(line.bind_ip || '-')}</td><td>${Number(line.assigned_mbps??line.target_mbps??0).toFixed(0)}</td><td>${line.metrics_available?Number(line.current_mbps||0).toFixed(0):'-'}</td><td>${line.metrics_available?Number(line.avg60_mbps||0).toFixed(0):'-'}</td><td>${fmtBytes(line.rolling_upload_bytes)}</td><td>${fmtBytes(line.rolling_download_bytes)}</td><td>${line.ratio==null?'-':Number(line.ratio).toFixed(2)+'×'}</td><td>${fmtBytes(line.debt_bytes)}</td><td>${escapeHtml(line.snmp_quality||'pending')}</td><td>${Number(line.connections||0)}/${Number(line.max_connections||0)}</td><td>${escapeHtml(line.status)}</td><td>${escapeHtml(line.last_error)}</td></tr>`;
  }).join('');if(fleet)updateFleetSelection();
}
async function submitFleetForm(form, feedbackId, action, successMessage) {
  const feedback=document.getElementById(feedbackId);const button=form.querySelector('button[type="submit"]');
  button.disabled=true;form.setAttribute('aria-busy','true');feedback.className='form-feedback';feedback.textContent='正在保存…';
  try{await action();feedback.textContent=successMessage;showError();await refresh();}
  catch(error){feedback.textContent=String(error.message||error);feedback.className='form-feedback failure';showError(error);}
  finally{button.disabled=false;form.removeAttribute('aria-busy');}
}
async function refresh() {
  if(refreshInFlight)return;refreshInFlight=true;
  try {
    const [status, metrics, sources, sourceList] = await Promise.all([api('/api/status'),api('/api/metrics'),api('/api/sources'),api('/api/source-list')]);
    showError();
    document.getElementById('running').textContent=status.running?'运行中':'已停止';
    document.getElementById('window').textContent=status.within_window?'时间窗内':'时间窗外';
    document.getElementById('now').textContent=status.now||'';
    setForm(status.config||{});setSourceList(sourceList||{});updateFleetRatio(metrics,status.config||{});updateFleetQuality(metrics);updateFleetDaily(metrics,status.config||{});
    if(document.getElementById('fleetGroupsSection'))updateFleetGroups(Array.isArray(metrics.groups)?metrics.groups:await api('/api/fleet/groups'));
    document.getElementById('currentMbps').textContent=Number(metrics.current_mbps||0).toFixed(0);
    document.getElementById('avg10Mbps').textContent=Number(metrics.avg10_mbps||0).toFixed(0);
    document.getElementById('avg60Mbps').textContent=Number(metrics.avg60_mbps||0).toFixed(0);
    document.getElementById('todayBytes').textContent=fmtBytes(metrics.today_bytes);
    document.getElementById('targetPercent').textContent=Number(metrics.target_percent||0).toFixed(0)+'%';
    document.getElementById('dailyPercent').textContent=Number(metrics.daily_target_percent||0).toFixed(0)+'%';
    document.getElementById('workerCount').textContent=String(metrics.worker_count||0);
    document.getElementById('capacityWarning').style.display=metrics.capacity_warning?'inline-flex':'none';
    chartSamples.push({current:Number(metrics.current_mbps||0),avg60:Number(metrics.avg60_mbps||0)});while(chartSamples.length>120)chartSamples.shift();drawChart(metrics.target_mbps);
    renderLines(metrics.lines||[]);
    document.getElementById('sources').innerHTML=(sources||[]).map(source=>`<tr><td>${escapeHtml(source.url)}</td><td>${escapeHtml(source.ip)}</td><td>${escapeHtml(source.state || (source.healthy?'healthy':'unhealthy'))}</td><td>${Number(source.failures||0)}</td><td>${source.retry_in_seconds?Number(source.retry_in_seconds)+' 秒':'-'}</td><td>${escapeHtml(source.last_error||'')}</td></tr>`).join('');
    document.getElementById('logs').textContent=(status.logs||[]).join('\n');
  } catch (error) { showError(error); } finally { refreshInFlight=false; }
}
async function control(path) { try { await api(path,{method:'POST'}); await refresh(); } catch(error) { showError(error); } }
async function importFleetAccounts(event) { event.preventDefault(); const file=document.getElementById('fleetAccountsFile').files[0]; if (!file) { showError(new Error('请选择 CSV 文件')); return; } await submitFleetForm(event.target,'fleetImportFeedback',async()=>{const result=await api('/api/fleet/accounts/import',{method:'POST',headers:{'content-type':'text/csv'},body:await file.text()});document.getElementById('fleetImportFeedback').dataset.accounts=String(result.accounts);},'账号映射已整批导入'); }
{{SOURCE_LIST_REFRESH_FUNCTION}}
document.getElementById('configForm').addEventListener('input',()=>{configDirty=true;});
document.getElementById('configForm').addEventListener('submit', async event => { event.preventDefault(); const data={};const button=event.target.querySelector('button[type="submit"]');button.disabled=true; for(const [key,value] of new FormData(event.target).entries()){if(['target_mbps','connections_per_line','max_connections_per_line','line_count','rotation_minutes','snmp_poll_seconds','download_ceiling_mbps','line_stall_seconds','line_slow_window_seconds','line_failure_cooldown_minutes','router_limit_mbps','router_reserve_mbps','late_download_limit_mbps','wan_stale_seconds'].includes(key))data[key]=Number(value);else if(key==='target_ratio')data[key]=Number(value);else if(['rate_limit_enabled'].includes(key))data[key]=value==='true';else if(['source_pool','lan_ips'].includes(key))data[key]=value.split(/[\n,]+/).map(item=>item.trim()).filter(Boolean);else data[key]=value;} try{await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});configDirty=false;await refresh();}catch(error){showError(error);}finally{button.disabled=false;} });
const fleetAccountsForm=document.getElementById('fleetAccountsForm'); if (fleetAccountsForm) fleetAccountsForm.addEventListener('submit', importFleetAccounts);
const groupsForm=document.getElementById('fleetGroupsForm');if(groupsForm){
  groupsForm.addEventListener('input',()=>{fleetGroupsDirty=true;});
  groupsForm.addEventListener('submit',async event=>{event.preventDefault();const groups=Array.from(document.querySelectorAll('#fleetGroupsRows tr')).map(row=>{const group={};for(const input of row.querySelectorAll('[data-field]')){group[input.dataset.field]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value.trim();}return group;});await submitFleetForm(event.target,'fleetGroupsFeedback',async()=>{await api('/api/fleet/groups',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({groups})});fleetGroupsDirty=false;},'分组配置已保存');});
  document.getElementById('addFleetGroup').addEventListener('click',()=>{document.getElementById('fleetGroupsRows').insertAdjacentHTML('beforeend',fleetGroupRow({name:'新分组',download_limit_mbps:1000,max_active:5,enabled:true},true));fleetGroupsDirty=true;document.querySelector('#fleetGroupsRows tr:last-child [data-field="group_id"]').focus();});
  document.getElementById('fleetGroupAssignForm').addEventListener('submit',async event=>{event.preventDefault();if(!fleetSelectedAccounts.size){const feedback=document.getElementById('fleetAssignFeedback');feedback.textContent='请先在下方线路表勾选账号';feedback.className='form-feedback failure';return;}const group_id=document.getElementById('fleetAssignGroup').value;const account_ids=Array.from(fleetSelectedAccounts);await submitFleetForm(event.target,'fleetAssignFeedback',()=>api('/api/fleet/groups/assign',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({group_id,account_ids})}),`已将 ${account_ids.length} 条线路调整分组`);});
  document.getElementById('lines').addEventListener('change',event=>{const input=event.target;if(!input.classList.contains('fleet-account-select'))return;input.checked?fleetSelectedAccounts.add(input.dataset.account):fleetSelectedAccounts.delete(input.dataset.account);updateFleetSelection();});
  document.getElementById('fleetSelectAll').addEventListener('change',event=>{for(const line of latestFleetLines){const id=String(line.account_id||line.line_id);event.target.checked?fleetSelectedAccounts.add(id):fleetSelectedAccounts.delete(id);}renderLines(latestFleetLines);});
  document.getElementById('clearFleetSelection').addEventListener('click',()=>{fleetSelectedAccounts.clear();renderLines(latestFleetLines);});
  document.getElementById('dailyReportDate').addEventListener('change',updateDailyReportLink);
}
refresh(); setInterval(refresh, {{REFRESH_MS}});
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    controller: PumperController
    topology_name: str = "ikuai_line"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
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
        elif path == "/api/fleet/groups" and self.topology_name == "fleet":
            self._json(200, self.controller.groups_snapshot())
        elif path == "/api/fleet/report.csv" and self.topology_name == "fleet":
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError("report query parameters must not be repeated")
                period = query.get("period", ["rolling"])[0]
                report_date = query.get("date", [None])[0]
                if period not in {"rolling", "daily"}:
                    raise ValueError("report period must be rolling or daily")
                if report_date is not None:
                    try:
                        parsed_date = calendar_date.fromisoformat(report_date)
                    except ValueError as exc:
                        raise ValueError("report date must be a valid YYYY-MM-DD date") from exc
                    if parsed_date.isoformat() != report_date:
                        raise ValueError("report date must use YYYY-MM-DD format")
                    if period != "daily":
                        raise ValueError("report date is only supported for daily reports")
                report = (
                    self.controller.report_csv(period=period, date=report_date)
                    if query else self.controller.report_csv()
                )
                self._send(200, report, "text/csv; charset=utf-8")
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
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
            elif path == "/api/fleet/groups" and self.topology_name == "fleet":
                self._json(200, self.controller.update_groups(self._read_json_object()))
            elif path == "/api/fleet/groups/assign" and self.topology_name == "fleet":
                self._json(200, self.controller.assign_groups(self._read_json_object()))
            elif path == "/api/fleet/evening-override" and self.topology_name == "fleet":
                self._json(200, self.controller.set_evening_override(self._read_json_object()))
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

    def _read_json_object(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length < 0 or length > 1_048_576:
            raise ValueError("request body is too large or has an invalid length")
        data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("request payload must be a JSON object")
        return data

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
