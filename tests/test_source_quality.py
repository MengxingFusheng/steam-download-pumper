import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from source_publisher.probe import ProbeResult, probe_candidates
from source_publisher.service import InsufficientSources, PublicationService
from steam_pumper.fleet import FleetAccount
from tests import test_publisher_service as publisher_tests
from tests import test_fleet_daily_store as store_tests


class SourceQualityTests(unittest.TestCase):
    def test_direct_download_cannot_bypass_speed_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            config = publisher_tests.PublisherServiceTests()._config(Path(directory))
            urls = [f'https://slow{i}.example/download?size=67108864' for i in range(3)]
            config.candidates_path.write_text(json.dumps({'schema': 1, 'sources': [{'url': u, 'enabled': True} for u in urls]}))
            def probes(values, **kwargs):
                return [ProbeResult(u, datetime.now(ZoneInfo('Asia/Shanghai')), 5, 4194304, True, '') for u in values]
            with self.assertRaises(InsufficientSources):
                PublicationService(config, probe_fn=probes).run(datetime.now(config.timezone), validate_only=True)

    def test_fastest_host_endpoint_wins_and_one_fast_source_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(publisher_tests.PublisherServiceTests()._config(Path(directory)), min_healthy_sources=1)
            now = datetime.now(config.timezone)
            urls = ['https://one.example/download?size=67108864', 'http://one.example/download?size=67108864',
                    'https://one.example/speedtest/random4000x4000.jpg', 'https://two.example/file']
            def probes(values, **kwargs):
                return [ProbeResult(u, now, speed, 4194304, True, '') for u, speed in zip(urls, [101, 110, 900, 500])]
            result = PublicationService(config, probe_fn=probes).run(now, validate_only=True)
            chosen = [s['url'] for s in json.loads(result.payload)['sources']]
            self.assertEqual(chosen, [urls[2], urls[3], urls[1]])
            only = lambda values, **kwargs: [ProbeResult(urls[2], now, 900, 4194304, True, '')]
            result = PublicationService(config, probe_fn=only).run(now, validate_only=True)
            self.assertEqual(result.source_count, 1)
            self.assertTrue(result.diagnostics['redundancy_degraded'])

    def test_sustained_measurement_is_required_and_uses_worst_round(self):
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        url = 'https://fast.example/download?size=67108864'
        screen = lambda value, **kwargs: ProbeResult(value, now, 5, 4194304, True, '')
        with patch('source_publisher.probe.probe_source', side_effect=screen), patch(
            'source_publisher.benchmark.measure_engine', side_effect=[(10000000, 600), (12000000, 300)]
        ) as measure:
            result = probe_candidates([url], engine_path='/test/discarder')[0]
        self.assertTrue(result.success)
        self.assertEqual(result.probe_mbps, 300)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(measure.call_count, 2)
        self.assertEqual(result.stage, 'sustained')


class OverdeliveryForecastTests(unittest.TestCase):
    setUp = store_tests.FleetDailyStoreTests.setUp
    tearDown = store_tests.FleetDailyStoreTests.tearDown
    _history_bin = store_tests.FleetDailyStoreTests._history_bin
    def test_reliable_645m_history_is_not_clipped_to_500m(self):
        self.store.cfg.upload_ceiling_mbps = 1000
        self.store.import_accounts([FleetAccount('a', '192.168.2.11', 'pppoe-a', True, 10, 'symmetric500')])
        now = datetime(2026, 10, 3, 23, 45, tzinfo=ZoneInfo('Asia/Shanghai'))
        for days in (1, 2, 3):
            self._history_bin(now - timedelta(days=days), 645 / 500, 500)
        view = self.store.daily_forecasts(now)['a']
        self.assertAlmostEqual(view['future_upload_bytes'], 645 * 1.2 * 1000000 / 8 * 900, delta=1)
        self.assertEqual(self.store.accounts()[0].upload_mbps, 500)

    def test_three_evening_bins_calibrate_cold_start_without_changing_ledger(self):
        self.store.cfg.upload_ceiling_mbps = 1000
        self.store.import_accounts([FleetAccount('a', '192.168.2.11', 'pppoe-a', True, 10, 'symmetric500')])
        now = datetime(2026, 10, 3, 19, tzinfo=ZoneInfo('Asia/Shanghai'))
        for minutes in (15, 30, 45):
            self._history_bin(now - timedelta(minutes=minutes), 1.3, 500)
        before = self.store.daily_totals(now.date())['a']['upload']
        view = self.store.daily_forecasts(now)['a']
        self.assertAlmostEqual(view['future_upload_bytes'], 650 * 1.2 * 1000000 / 8 * 5 * 3600, delta=1)
        self.assertEqual(self.store.daily_totals(now.date())['a']['upload'], before)


class SmallManifestTests(unittest.TestCase):
    def test_single_verified_source_is_accepted_only_when_configured(self):
        from steam_pumper.remote_sources import RemoteSourceSettings, parse_manifest_payload
        from tests.test_remote_sources import manifest_payload, NOW, public_resolver
        settings = RemoteSourceSettings.from_env({'SOURCE_LIST_MIN_SOURCES': '1'})
        payload = manifest_payload(source_count=1)
        document = parse_manifest_payload(payload, now=NOW, min_sources=settings.min_sources, resolver=public_resolver)
        self.assertEqual(len(document.urls), 1)
        with self.assertRaises(ValueError):
            parse_manifest_payload(payload, now=NOW, min_sources=3, resolver=public_resolver)


class CompletionDiagnosticsTests(unittest.TestCase):
    def test_evening_protection_and_group_shortfall_are_reported(self):
        from tests import test_fleet_daily_schedule as schedule
        fixture = schedule.DailyControllerTests()
        fixture.setUp()
        try:
            fixture.import_accounts([('a', 10), ('b', 10), ('c', 10), ('d', 10)])
            now = schedule.moment(22)
            fixture.set_budget(4800, now)
            for forecast in fixture.forecasts.values():
                forecast['remaining_download_bytes'] = 350000000000
            rows = fixture.controller._completion_diagnostics(now)
            by_reason = {row['reason']: row for row in rows}
            self.assertIn('upload_only', by_reason)
            shortfall = by_reason['node_capacity_shortfall']
            self.assertEqual(shortfall['maximum_download_bytes'], 900000000000)
            self.assertEqual(shortfall['shortfall_bytes'], 500000000000)
        finally:
            fixture.doCleanups()


class EngineBenchmarkLifecycleTests(unittest.TestCase):
    def test_bounded_measurement_terminates_its_child(self):
        import os
        import threading
        import time
        from source_publisher.benchmark import measure_engine
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / 'engine'
            pidfile = root / 'pid'
            helper.write_text('#!/usr/bin/env python3\nimport os,time,json,pathlib\n'
                              + 'pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()))\n'
                              + 'n=0\nwhile True:\n n+=1048576\n print(json.dumps({"type":"status","bytes":n}),flush=True)\n time.sleep(.05)\n')
            helper.chmod(0o755)
            count, speed = measure_engine('https://fixture.example/file', str(helper), time.monotonic()+5,
                                          threading.Event(), duration=.7, warmup=.1)
            self.assertGreater(count, 2*1024*1024)
            self.assertGreater(speed, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
