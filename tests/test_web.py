import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from steam_pumper.web import Handler, PumperHTTPServer, render_html


class DummyConfig:
    def to_dict(self):
        return {"target_mbps": 800}


class DummyController:
    cfg = DummyConfig()

    def status(self):
        return {"running": False, "config": self.cfg.to_dict(), "logs": []}

    def metrics(self):
        return {"target_mbps": 800, "avg60_mbps": 760, "lines": []}

    def source_snapshot(self):
        return [{"url": "https://example.test/file", "ip": "203.0.113.1", "healthy": True}]

    def source_list_status(self):
        return {
            "enabled": True,
            "revision": 20260720031700,
            "source_count": 3,
            "origin": "remote",
            "last_success_at": "2026-07-20T04:00:00+08:00",
            "next_refresh_at": "2026-07-21T04:10:00+08:00",
            "stale": False,
            "last_error": "",
        }

    def request_source_list_refresh(self):
        return {**self.source_list_status(), "refresh_request_state": "queued"}

    def set_manual_enabled(self, _enabled):
        return None

    def start_downloads(self):
        return None

    def update_config(self, _data):
        return self.cfg


class WebTests(unittest.TestCase):
    def test_http_server_drives_controller_tick(self):
        class TickController:
            def __init__(self):
                self.ticks = 0

            def tick(self):
                self.ticks += 1

        controller = TickController()
        server = PumperHTTPServer(("127.0.0.1", 0), Handler, controller)
        try:
            server.service_actions()
        finally:
            server.server_close()

        self.assertEqual(controller.ticks, 1)

    def test_rendered_console_javascript_is_valid(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript syntax validation")
        for topology in ("multi_ip", "ikuai_line", "fleet"):
            with self.subTest(topology=topology):
                html = render_html(topology)
                script = html.split("<script>", 1)[1].split("</script>", 1)[0]
                result = subprocess.run(
                    ["node", "--check"],
                    input=script,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_fleet_console_has_accessible_rolling_ratio_chart(self):
        html = render_html("fleet")

        for expected in (
            "滚动 15 天上传与下载倍率",
            'id="rollingRatio"',
            'id="rollingUpload"',
            'id="rollingDownload"',
            'id="ratioTargetMarker"',
            'role="img"',
            "function updateFleetRatio",
            "updateFleetRatio(metrics,status.config||{})",
        ):
            self.assertIn(expected, html)
        self.assertNotIn("{{FLEET_RATIO_SECTION}}", html)

    def test_ratio_chart_is_only_rendered_for_fleet(self):
        self.assertNotIn('id="fleetRatioSection"', render_html("multi_ip"))
        self.assertNotIn('id="fleetRatioSection"', render_html("ikuai_line"))

    def test_fleet_source_section_describes_local_or_remote_download_sources(self):
        html = render_html("fleet")

        self.assertIn("下载源清单", html)
        self.assertIn('id="sourceListRefresh"', html)
        self.assertIn("sourceList.enabled===false?'none'", html)

    def test_ikuai_console_has_no_ip_or_line_count_fields(self):
        html = render_html("ikuai_line")

        self.assertIn('name="target_mbps"', html)
        self.assertIn('name="connections_per_line"', html)
        self.assertNotIn('name="line_count"', html)
        self.assertNotIn('name="lan_ips"', html)
        self.assertNotIn("EGRESS_MODE", html)

    def test_multi_ip_console_adds_only_topology_fields(self):
        html = render_html("multi_ip")

        self.assertIn('name="line_count"', html)
        self.assertIn('name="lan_ips"', html)
        self.assertNotIn('name="egress_mode"', html)
        self.assertNotIn("新建连接数", html)

    def test_multi_ip_console_displays_remote_source_list_status_and_refresh(self):
        html = render_html("multi_ip")

        for expected in (
            "远程源清单",
            "sourceListRevision",
            "sourceListActiveRevision",
            "sourceListApplyState",
            "sourceListLineErrors",
            "sourceListCount",
            "sourceListOrigin",
            "sourceListLastSuccess",
            "sourceListNextRefresh",
            "sourceListStale",
            "sourceListError",
            "立即刷新",
            "/api/source-list/refresh",
        ):
            self.assertIn(expected, html)
        self.assertNotIn('name="source_list_url"', html)
        self.assertNotIn('name="source_list_public_key"', html)
        self.assertNotIn("sourceListRefreshState", html)

    def test_ikuai_console_does_not_show_remote_source_list_controls(self):
        html = render_html("ikuai_line")

        self.assertNotIn("远程源清单", html)
        self.assertNotIn("/api/source-list/refresh", html)

    def test_console_escapes_table_values_and_uses_text_content_for_logs(self):
        html = render_html("multi_ip")

        self.assertIn("function escapeHtml", html)
        self.assertIn("escapeHtml(line.bind_ip", html)
        self.assertIn("escapeHtml(source.url", html)
        self.assertIn("logs').textContent", html)

    def test_console_displays_source_circuit_breaker_state(self):
        html = render_html("multi_ip")

        self.assertIn("source.state", html)
        self.assertIn("source.retry_in_seconds", html)
        self.assertIn("隔离/重试", html)

    def test_metrics_sources_and_config_api_endpoints(self):
        Handler.controller = DummyController()
        Handler.topology_name = "ikuai_line"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            metrics = self._get_json(f"{base}/api/metrics")
            sources = self._get_json(f"{base}/api/sources")
            config = self._get_json(f"{base}/api/config")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(metrics["target_mbps"], 800)
        self.assertEqual(sources[0]["ip"], "203.0.113.1")
        self.assertEqual(config["target_mbps"], 800)

    def test_source_list_get_and_manual_refresh_endpoints(self):
        Handler.controller = DummyController()
        Handler.topology_name = "multi_ip"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            current = self._get_json(f"{base}/api/source-list")
            request = urllib.request.Request(f"{base}/api/source-list/refresh", data=b"", method="POST")
            refreshed = json.loads(urllib.request.urlopen(request, timeout=2).read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(current["origin"], "remote")
        self.assertEqual(refreshed["revision"], 20260720031700)
        self.assertEqual(refreshed["refresh_request_state"], "queued")

    def test_manual_refresh_failure_returns_503_json(self):
        from steam_pumper.controller import SourceListRefreshError

        class OfflineController(DummyController):
            def request_source_list_refresh(self):
                raise SourceListRefreshError("OSS offline")

        Handler.controller = OfflineController()
        Handler.topology_name = "multi_ip"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/source-list/refresh",
            data=b"",
            method="POST",
        )
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(payload, {"error": "OSS offline"})

    def test_api_errors_are_json(self):
        class RejectingController(DummyController):
            def update_config(self, _data):
                raise ValueError("bad config")

        Handler.controller = RejectingController()
        Handler.topology_name = "ikuai_line"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/config",
            data=b"{}",
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(payload, {"error": "bad config"})

    def test_fleet_account_and_report_endpoints(self):
        class FleetController(DummyController):
            def accounts_snapshot(self):
                return [{"account_id": "a1", "active": True, "ratio": 2.0}]

            def summary(self):
                return {"active_accounts": 1, "snmp_status": "online"}

            def report_csv(self):
                return "account_id,ratio\na1,2.0\n"

            def import_accounts_csv(self, value):
                self.imported = value
                return {"ok": True, "accounts": 1}

        controller = FleetController()
        Handler.controller = controller
        Handler.topology_name = "fleet"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            accounts = self._get_json(f"{base}/api/fleet/accounts")
            summary = self._get_json(f"{base}/api/fleet/summary")
            report = urllib.request.urlopen(f"{base}/api/fleet/report.csv", timeout=2).read().decode()
            request = urllib.request.Request(
                f"{base}/api/fleet/accounts/import", data=b"account_id,bind_ip,snmp_interface\\na1,192.168.1.10,pppoe-a1\\n",
                headers={"content-type": "text/csv"}, method="POST",
            )
            imported = json.loads(urllib.request.urlopen(request, timeout=2).read().decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(accounts[0]["account_id"], "a1")
        self.assertEqual(summary["snmp_status"], "online")
        self.assertIn("account_id,ratio", report)
        self.assertEqual(imported["accounts"], 1)

    def test_fleet_console_offers_atomic_csv_account_import(self):
        html = render_html("fleet")

        for expected in (
            'id="fleetAccountsForm"', 'id="fleetAccountsFile"',
            "上传账号 CSV", "/api/fleet/accounts/import", "text/csv",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)

    def test_fleet_console_shows_actual_schedule_not_common_time_window(self):
        html = render_html("fleet")

        for expected in ("每日预测分组调度", "00:00–18:00", "18:00–23:00", "23:00–24:00"):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
        self.assertNotIn('name="target_mbps"', html)
        self.assertNotIn('name="start_time"', html)
        self.assertNotIn('name="end_time"', html)
        self.assertNotIn('name="daily_mode_enabled"', html)
        self.assertNotIn('name="per_line_mbps"', html)
        self.assertNotIn('name="line_minimum_mbps"', html)
        self.assertNotIn('Fleet 固定调度', html)
        self.assertNotIn("按 15 天欠量动态补足", html)

    def test_fleet_console_has_daily_forecast_budget_and_group_controls(self):
        html = render_html("fleet")
        for expected in (
            'id="fleetDailySection"', 'id="dailyChart"', 'id="dailyUploadBar"',
            'id="dailyDownloadBar"', 'id="dailyTargetBar"',
            'id="dailyReadinessWarning"', 'id="dailyReportDate"',
            'id="fleetGroupsForm"', 'id="fleetGroupAssignForm"',
            'id="fleetSelectAll"',
            'name="router_snmp_interface"', 'name="router_limit_mbps"',
            "18:00–23:00", "23:00–24:00", "/api/fleet/groups/assign",
            "程序不做下载硬限速", "预算分配 Mbps", "weight 10 线路 500M",
            "昨日欠量不结转", "预测与未知区间保守预算不写入实际账本",
            "prefers-reduced-motion", "focus-visible", 'role="status"',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
        self.assertNotIn("{{FLEET_DAILY_SECTION}}", html)
        self.assertNotIn("{{FLEET_GROUPS_SECTION}}", html)
        self.assertNotIn("{{FLEET_LINE_HEADERS}}", html)
        self.assertNotIn("{{TARGET_HEADING}}", html)

    def test_daily_groups_are_not_rendered_for_legacy_topologies(self):
        for topology in ("ikuai_line", "multi_ip"):
            with self.subTest(topology=topology):
                html = render_html(topology)
                self.assertNotIn('id="fleetDailySection"', html)
                self.assertNotIn('id="fleetGroupsForm"', html)
                self.assertNotIn('name="daily_mode_enabled"', html)

    def test_refresh_preserves_in_progress_configuration_and_group_edits(self):
        html = render_html("fleet")
        self.assertIn("if(configDirty)return", html)
        self.assertIn("if(!fleetGroupsDirty&&signature!==fleetGroupsSignature)", html)
        self.assertIn("if(refreshInFlight)return", html)
        self.assertIn("escapeHtml(group.name", html)
        self.assertIn("escapeHtml(line.group_id", html)

    def test_fleet_daily_report_and_group_endpoints(self):
        class DailyController(DummyController):
            def __init__(self):
                self.report_calls = []
                self.groups = [{"group_id": "standard", "name": "普通组"}]
                self.assignment = None

            def report_csv(self, **kwargs):
                self.report_calls.append(kwargs)
                return "account_id,date\na1,2026-10-03\n"

            def groups_snapshot(self):
                return self.groups

            def update_groups(self, payload):
                self.groups = payload["groups"]
                return self.groups

            def assign_groups(self, payload):
                self.assignment = payload
                return {"ok": True, "accounts": len(payload["account_ids"])}

        controller = DailyController()
        Handler.controller = controller
        Handler.topology_name = "fleet"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            groups = self._get_json(f"{base}/api/fleet/groups")
            report = urllib.request.urlopen(f"{base}/api/fleet/report.csv?period=daily&date=2026-10-03", timeout=2).read().decode()
            urllib.request.urlopen(f"{base}/api/fleet/report.csv", timeout=2).close()
            updated = self._post_json(f"{base}/api/fleet/groups", {"groups": [{"group_id": "standard", "name": "更新组"}]})
            assigned = self._post_json(f"{base}/api/fleet/groups/assign", {"group_id": "standard", "account_ids": ["a1"]})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(groups[0]["name"], "普通组")
        self.assertIn("2026-10-03", report)
        self.assertEqual(controller.report_calls, [{"period": "daily", "date": "2026-10-03"}, {}])
        self.assertEqual(updated[0]["name"], "更新组")
        self.assertEqual(assigned, {"ok": True, "accounts": 1})
        self.assertEqual(controller.assignment, {"group_id": "standard", "account_ids": ["a1"]})

    def test_fleet_report_rejects_invalid_period_dates_and_repeated_parameters(self):
        class DailyController(DummyController):
            def report_csv(self, **_kwargs):
                raise AssertionError("invalid query must not reach controller")

        Handler.controller = DailyController()
        Handler.topology_name = "fleet"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/fleet/report.csv"
        try:
            for query in (
                "period=other", "period=daily&date=2026-02-30",
                "period=daily&date=20261003", "period=daily&date=",
                "date=2026-10-03", "period=daily&period=rolling",
            ):
                with self.subTest(query=query):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(f"{base}?{query}", timeout=2)
                    self.assertEqual(raised.exception.code, 400)
                    payload = json.loads(raised.exception.read().decode())
                    raised.exception.close()
                    self.assertIn("error", payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fleet_group_payload_errors_are_400_and_legacy_routes_are_unavailable(self):
        class RejectingGroupsController(DummyController):
            def update_groups(self, _payload):
                raise ValueError("duplicate group_id")

            def assign_groups(self, _payload):
                raise ValueError("unknown account_id")

        Handler.controller = RejectingGroupsController()
        Handler.topology_name = "fleet"
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for route, payload in (("/api/fleet/groups", {}), ("/api/fleet/groups/assign", {}), ("/api/fleet/groups", [])):
                with self.subTest(route=route, payload=payload):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        self._post_json(base + route, payload)
                    self.assertEqual(raised.exception.code, 400)
                    self.assertIn("error", json.loads(raised.exception.read().decode()))
                    raised.exception.close()
            Handler.topology_name = "multi_ip"
            for route in ("/api/fleet/groups", "/api/fleet/groups/assign"):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self._post_json(base + route, {})
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._get_json(base + "/api/fleet/groups")
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_daily_web_api_uses_real_profile_forecast_and_budget_fields(self):
        from steam_pumper.config import FleetConfig
        from steam_pumper.fleet import FleetController, SnmpInterface, parse_accounts_csv

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        before = now - timedelta(seconds=60)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "APPLY_LAN_IPS": "0", "SNMP_HOST": "", "FLEET_AUTOSTART": "0", "FLEET_ACCOUNTS_CSV": "",
        }):
            controller = FleetController(Path(directory) / "config.json", FleetConfig(daily_mode_enabled=True))
            controller.store.import_accounts(parse_accounts_csv(
                "account_id,bind_ip,snmp_interface,weight\n"
                "a1,192.168.2.11,pppoe-a1,1\n"
                "a2,192.168.2.12,pppoe-a2,10\n"
            ))
            controller.store.record_snmp([
                SnmpInterface(1, "pppoe-a1", "", 1000, 2000, oper_status=1),
                SnmpInterface(2, "pppoe-a2", "", 1000, 2000, oper_status=1),
            ], before)
            controller.snmp_quality = controller.store.record_snmp([
                SnmpInterface(1, "pppoe-a1", "", 1_500_001_000, 375_002_000, oper_status=1),
                SnmpInterface(2, "pppoe-a2", "", 3_750_001_000, 3_750_002_000, oper_status=1),
            ], now)
            controller.wan_budget.observe([SnmpInterface(24, "wan1", "", 1000, 0, oper_status=1)], before, None, swap=False, stale_seconds=120)
            controller.wan_budget.observe([SnmpInterface(24, "wan1", "", 15_000_001_000, 0, oper_status=1)], now, None, swap=False, stale_seconds=120)
            Handler.controller = controller
            Handler.topology_name = "fleet"
            server = HTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                metrics = self._get_json(base + "/api/metrics")
                summary = self._get_json(base + "/api/fleet/summary")
                accounts = self._get_json(base + "/api/fleet/accounts")
                groups = self._get_json(base + "/api/fleet/groups")
                report = urllib.request.urlopen(base + "/api/fleet/report.csv?period=daily&date=" + now.date().isoformat(), timeout=2).read().decode()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                controller.shutdown()
            self.assertTrue(metrics["daily_mode_enabled"])
            self.assertEqual(metrics["daily"]["profile_unconfirmed_accounts"], 0)
            self.assertGreater(metrics["daily"]["today_download_bytes"], 0)
            self.assertEqual(summary["router_budget"]["interface"], "wan1")
            self.assertEqual(summary["router_budget"]["status"], "ok")
            self.assertEqual(metrics["router_budget"]["available_mbps"], 2800)
            self.assertEqual([account["weight"] for account in accounts], [1, 10])
            self.assertEqual([account["group_id"] for account in accounts], ["standard", "symmetric500"])
            self.assertEqual(accounts[1]["upload_mbps"], 500)
            self.assertIn("forecast_quality", accounts[0]["daily"])
            self.assertEqual(len(groups), 2)
            self.assertIn("pre18_", report)

    @staticmethod
    def _get_json(url):
        return json.loads(urllib.request.urlopen(url, timeout=2).read().decode("utf-8"))

    @staticmethod
    def _post_json(url, payload):
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode())


if __name__ == "__main__":
    unittest.main()
