"""Real-browser smoke test using a temporary cloud server and a fake network adapter.

Run: python -m dashboard.verify_browser [--chromium /path/to/chrome]
No production node or persistent dashboard is contacted.
"""
import argparse
import copy
import hashlib
import json
import re
import socket
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect

from dashboard.app import create_app
from dashboard.security import hash_password
from steam_pumper.config import FleetConfig
from steam_pumper.dashboard_agent import DashboardAgent
from steam_pumper.dashboard_protocol import config_version
from steam_pumper.fleet import FleetController, parse_accounts_csv


def completion_fixture(snapshot):
    """Known cumulative targets differ from interval traffic by design."""
    snapshot = copy.deepcopy(snapshot)
    gib = 1024**3
    for line, actual, upload, current_rate, qualities in zip(
            snapshot['lines'], [250, 25], [8, 22], [5.5, 2.0], [(150, 50, 50), (10, 5, 5)]):
        line.update(actual_bytes=actual*gib, target_bytes=100*gib,
                    upload_bytes=upload*gib, upload_mbps=300, current_upload_mbps=current_rate,
                    measured_bytes=qualities[0]*gib, estimated_bytes=qualities[1]*gib, legacy_bytes=qualities[2]*gib)
    snapshot['summary'].update(actual_bytes=275*gib, target_bytes=200*gib,
                               remaining_bytes=75*gib, upload_bytes=30*gib, upload_mbps=7.5,
                               measured_bytes=160*gib, estimated_bytes=55*gib, legacy_bytes=55*gib)
    for period in snapshot['periods']:
        period['state'] = 'closed'
        for line, interval, cumulative in zip(period['lines'], [10, 5], [125, 25]):
            line.update(actual_bytes=interval*gib, cumulative_actual_bytes=cumulative*gib)
            if period['phase'] == 'daytime':
                line.update(target_bytes=100*gib,
                            completion='achieved' if cumulative >= 100 else 'unmet')
            elif period['phase'] == 'upload_only':
                line.update(target_bytes=None, completion='not_applicable')
            else:
                line.update(target_bytes=None, completion='unknown')
    return snapshot


def expect_meter(meter, percentage, capped):
    expect(meter).to_contain_text(percentage)
    bar = meter.locator('[role="progressbar"]')
    expect(bar).to_have_count(1)
    expect(bar).to_have_attribute('aria-valuenow', str(capped))
    expect(bar).to_have_attribute('aria-valuemax', '100')
    assert bar.evaluate('(node) => node.value') == capped
    assert bar.evaluate('(node) => node.getBoundingClientRect().width <= node.parentElement.getBoundingClientRect().width + 1')


def expect_segments(meter, expected):
    drawing = meter.locator('svg.progress-segments')
    expect(drawing).to_have_count(1)
    expect(drawing).to_be_visible()
    bounds = drawing.bounding_box()
    assert bounds and bounds['width'] > 30 and bounds['height'] >= 8, bounds
    segments = drawing.locator('rect[data-quality]').evaluate_all(
        '''nodes => nodes.map(node => {
            const gradientId=node.getAttribute('fill').match(/url\\(#([^)]*)\\)/)[1];
            const stops=node.ownerSVGElement.querySelector('#'+gradientId).querySelectorAll('stop');
            return {quality:node.dataset.quality,start:Number(node.getAttribute('x')),
                width:Number(node.getAttribute('width')),color:getComputedStyle(stops[0]).stopColor,
                opacity:Number(stops[stops.length-1].getAttribute('stop-opacity'))};
        })''')
    actual = {segment['quality']:segment['width'] for segment in segments}
    for quality in ['measured', 'estimated', 'legacy', 'unclassified', 'remaining']:
        assert abs(actual.get(quality, 0)-expected.get(quality, 0)) < .05, (quality, actual, expected)
    assert all(0 <= segment['start'] <= 100 and 0 <= segment['width'] <= 100 and
               segment['start'] + segment['width'] <= 100.05 for segment in segments), segments
    colored = [segment['color'] for segment in segments
               if segment['quality'] in {'measured', 'estimated', 'legacy'} and segment['width'] > 0]
    assert len(colored) == len(set(colored)), 'different traffic sources need distinct colors'
    assert all(segment['opacity'] <= .55 for segment in segments if segment['quality'] == 'remaining')


def verify_translucent_curves(page):
    for kind in ['actual', 'program']:
        curves = page.locator('#chart polyline.'+kind)
        assert curves.count() >= 2, 'missing telemetry intervals must split the curve'
        for style in curves.evaluate_all(
                'nodes => nodes.map(node => ({opacity:Number(getComputedStyle(node).strokeOpacity),fill:getComputedStyle(node).fill}))'):
            assert .45 <= style['opacity'] <= .7, style
            assert style['fill'] == 'none', style
    assert page.locator('#chartRows tr').filter(has_text='缺失').count() >= 5


def verify_completion_and_upload(page, agent, snapshot):
    expect(page.locator('#overview .kpis')).to_have_count(0)
    expect_meter(page.locator('#nodes .completion-meter'), '137.5%', 100)
    expect(page.locator('#nodes')).to_contain_text('30.00 GiB')
    expect(page.locator('#nodes')).to_contain_text(re.compile(r'7\.5(?:0)? Mbps'))
    expect(page.locator('#nodes')).to_contain_text(re.compile(r'2\.4\s*倍'))
    expect(page.locator('#nodes')).to_contain_text('预计上传')
    expect_segments(page.locator('#nodes .completion-meter'),
                    {'measured':160/275*100,'estimated':20,'legacy':20,'unclassified':5/275*100})
    for width, height in [(375, 812), (768, 1024), (1440, 1000), (812, 375)]:
        page.set_viewport_size({'width':width,'height':height})
        assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'),f'overview overflow at {width}x{height}'
        page.screenshot(path=f'/tmp/fleet-dashboard-overview-completion-{width}x{height}.png',full_page=True)
    page.get_by_role('button',name='天津 · 分时拉流验证节点',exact=True).click()
    page.locator('#chart svg').wait_for()
    verify_translucent_curves(page)
    expect_meter(page.locator('#nodeCompletion .completion-meter'), '137.5%', 100)
    expect_meter(page.locator('#lineRows tr').nth(0).locator('.completion-meter'), '250.0%', 100)
    expect_meter(page.locator('#lineRows tr').nth(1).locator('.completion-meter'), '25.0%', 25)
    expect_segments(page.locator('#nodeCompletion .completion-meter'),
                    {'measured':160/275*100,'estimated':20,'legacy':20,'unclassified':5/275*100})
    expect_segments(page.locator('#lineRows tr').nth(0).locator('.completion-meter'),
                    {'measured':60,'estimated':20,'legacy':20})
    expect_segments(page.locator('#lineRows tr').nth(1).locator('.completion-meter'),
                    {'measured':10,'estimated':5,'legacy':5,'unclassified':5,'remaining':75})
    page.locator('#lineRows tr').nth(1).locator('.completion-meter').screenshot(path='/tmp/fleet-dashboard-line-segments.png')
    expect(page.locator('#nodeCompletion')).to_contain_text(re.compile(r'2\.4\s*倍'))
    expect(page.locator('#lineRows')).to_contain_text(re.compile(r'2\.4\s*倍'))
    expect(page.locator('#detailKpis')).to_contain_text('30.00 GiB')
    expect(page.locator('#detailKpis')).to_contain_text(re.compile(r'7\.5(?:0)? Mbps'))
    upload_cell = page.locator('#lineRows tr').nth(0).locator('td').nth(4)
    expect(upload_cell).to_contain_text('8.00 GiB')
    expect(upload_cell).to_contain_text(re.compile(r'5\.5(?:0)? Mbps'))
    expect(upload_cell).not_to_contain_text('300')
    daytime = page.locator('#periods .period').filter(has=page.get_by_role('heading',name='00–18 点 · 日间补量',exact=True))
    # 150 / 200 cumulative = 75%; interval traffic is only 15 / 200.
    expect_meter(daytime.locator('.completion-meter'), '75.0%', 75)
    # Period records have cumulative actuals but no source attribution.
    expect_segments(daytime.locator('.completion-meter'), {'unclassified':75,'remaining':25})
    daytime.locator('.completion-meter').screenshot(path='/tmp/fleet-dashboard-period-segments.png')
    upload_period = page.locator('#periods .period').filter(has=page.get_by_role('heading',name='上传专用时段',exact=True))
    expect(upload_period).to_contain_text('不适用')
    expect(upload_period.locator('[role="progressbar"]')).to_have_count(0)
    unknown_period = page.locator('#periods .period').filter(has=page.get_by_role('heading',name='23–24 点 · 晚间补量',exact=True))
    expect(unknown_period.locator('.completion-meter')).to_contain_text('目标未知')
    expect(unknown_period.locator('[role="progressbar"]')).to_have_count(0)
    page.locator('#chartLine').select_option('line-A')
    expect_meter(daytime.locator('.completion-meter'), '125.0%', 100)
    expect_segments(daytime.locator('.completion-meter'), {'unclassified':100})
    page.locator('#chartLine').select_option('')
    expect_meter(daytime.locator('.completion-meter'), '75.0%', 75)
    for width, height in [(375, 812), (768, 1024), (1440, 1000), (812, 375)]:
        page.set_viewport_size({'width':width,'height':height})
        assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'),f'completion overflow at {width}x{height}'
        page.screenshot(path=f'/tmp/fleet-dashboard-completion-{width}x{height}.png',full_page=True)
    # Inconsistent historical attribution must not manufacture measured traffic.
    inconsistent = copy.deepcopy(snapshot)
    inconsistent['summary']['measured_bytes'] = 999*1024**3
    agent.request('/heartbeat', {'snapshot':inconsistent})
    page.reload();page.locator('#chart svg').wait_for()
    expect_meter(page.locator('#nodeCompletion .completion-meter'), '137.5%', 100)
    expect_segments(page.locator('#nodeCompletion .completion-meter'), {'unclassified':100})
    # Older agents omit these fields. Nonzero package capacity must stay unknown.
    legacy = copy.deepcopy(snapshot)
    legacy['mode'] = 'legacy'
    legacy['config'].update(target_ratio=3.6, daily_mode_enabled=False)
    legacy['config_version'] = config_version(legacy['config'])
    for field in ['upload_bytes', 'upload_mbps']:
        legacy['summary'].pop(field)
    for line in legacy['lines']:
        line.pop('upload_bytes')
        line.pop('current_upload_mbps')
        line['target_bytes'] = 0
    legacy['summary']['target_bytes'] = legacy['summary']['remaining_bytes'] = 0
    for period in legacy['periods']:
        if period['phase'] == 'daytime':
            for line in period['lines']:
                line.update(target_bytes=0, completion='no_target')
    agent.request('/heartbeat', {'snapshot':legacy})
    page.reload();page.locator('#chart svg').wait_for()
    expect(page.locator('#nodeCompletion .completion-meter')).to_contain_text('无目标')
    expect(page.locator('#nodeCompletion')).to_contain_text(re.compile(r'3\.6\s*倍'))
    expect(page.locator('#nodeCompletion')).to_contain_text(re.compile(r'当日(?:累计)?上传'))
    expect(page.locator('#nodeCompletion [role="progressbar"]')).to_have_count(0)
    expect(daytime.locator('.completion-meter')).to_contain_text('无目标')
    expect(daytime.locator('[role="progressbar"]')).to_have_count(0)
    for row in page.locator('#lineRows tr').all():
        expect(row.locator('.completion-meter')).to_contain_text('无目标')
        expect(row.locator('[role="progressbar"]')).to_have_count(0)
        expect(row.locator('td').nth(4)).to_contain_text('未知')
        expect(row.locator('td').nth(4)).not_to_contain_text('300')
    expect(page.locator('#detailKpis article').filter(has_text='当前上传')).to_contain_text('未知')
    page.locator('#back').click();page.get_by_role('button',name='天津 · 分时拉流验证节点',exact=True).wait_for()
    expect(page.locator('#nodes')).to_contain_text('未知')
    expect(page.locator('#nodes')).not_to_contain_text(re.compile(r'7\.5(?:0)? Mbps'))
    expect(page.locator('#nodes .completion-meter')).to_contain_text('无目标')
    expect(page.locator('#nodes')).to_contain_text(re.compile(r'3\.6\s*倍'))
    expect(page.locator('#nodes')).to_contain_text(re.compile(r'当日(?:累计)?上传'))
    expect(page.locator('#nodes [role="progressbar"]')).to_have_count(0)
    agent.request('/heartbeat', {'snapshot':snapshot})
    page.reload();page.get_by_role('button',name='天津 · 分时拉流验证节点',exact=True).click()
    page.locator('#chart svg').wait_for()


def verify_rename(page, agent):
    old_name = '天津 · 分时拉流验证节点'
    new_name = '天津 · 已编辑浏览器验证节点'
    dialog = page.locator('#renameDialog')
    input_name = dialog.locator('[name=name]')
    page.locator('#detail .rename-node').click()
    expect(dialog).to_be_visible()
    expect(input_name).to_have_value(old_name)
    expect(input_name).to_have_attribute('required', '')
    original_viewport = page.viewport_size
    for width, height in [(375, 812), (812, 375)]:
        page.set_viewport_size({'width':width,'height':height})
        assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'),f'rename overflow at {width}x{height}'
        page.screenshot(path=f'/tmp/fleet-dashboard-rename-{width}x{height}.png',full_page=True)
    page.set_viewport_size(original_viewport)
    input_name.fill('取消的名称');dialog.get_by_role('button',name='取消',exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator('#nodeName')).to_have_text(old_name)
    page.locator('#detail .rename-node').click()
    input_name.fill('   ');dialog.locator('#renameSave').click()
    expect(dialog.locator('[role=alert]')).to_contain_text('请输入节点名称')
    input_name.fill('名'*101);dialog.locator('#renameSave').click()
    expect(dialog.locator('[role=alert]')).to_contain_text('100')
    input_name.fill(new_name)
    history = page.evaluate('''async id => {
        const prefix='/api/v1/nodes/'+id;
        return Promise.all(['/series','/periods'].map(async suffix => (await fetch(prefix+suffix)).json()));
    }''', agent.node_id)
    def save_failure(route):
        if route.request.method == 'PATCH':
            route.fulfill(status=500, content_type='application/json', body=json.dumps({'detail':'模拟保存失败'}))
        else:
            route.continue_()
    page.route('**/api/v1/nodes/**', save_failure)
    with page.expect_response(lambda response: response.request.method == 'PATCH') as rejected:
        dialog.locator('#renameSave').click()
    assert rejected.value.status == 500
    expect(dialog).to_be_visible();expect(input_name).to_have_value(new_name)
    expect(dialog.locator('[role=alert]')).to_be_visible()
    expect(dialog.locator('[role=alert]')).to_contain_text('保存未完成')
    expect(dialog.locator('#renameSave')).to_be_enabled()
    page.unroute('**/api/v1/nodes/**', save_failure)
    with page.expect_response(lambda response: response.request.method == 'PATCH') as saved:
        dialog.locator('#renameSave').click()
    assert saved.value.status == 200
    expect(dialog).not_to_be_visible();expect(page.locator('#nodeName')).to_have_text(new_name)
    assert page.url.split('#')[-1] == agent.node_id
    # A restarted agent sends its original registration name; the edited name wins.
    agent.request('/register', {'node_id':agent.node_id, 'node_token':agent.token,
                               'name':old_name, 'external_id':agent.external_id}, enrollment=True)
    page.reload();expect(page.locator('#nodeName')).to_have_text(new_name)
    retained = page.evaluate('''async id => {
        const prefix='/api/v1/nodes/'+id;
        return Promise.all(['/series','/periods'].map(async suffix => (await fetch(prefix+suffix)).json()));
    }''', agent.node_id)
    assert history[1] == retained[1], 'renaming must retain period history'
    before = {point['start']:point for point in history[0]['points']}
    after = {point['start']:point for point in retained[0]['points']}
    overlap = before.keys() & after.keys()
    assert len(overlap) >= 143 and all(before[start] == after[start] for start in overlap), 'renaming must retain traffic history'
    page.locator('#back').click();page.get_by_role('button',name=new_name,exact=True).wait_for()
    page.locator('#nodes .node').filter(has=page.get_by_role('button',name=new_name,exact=True)).locator('.rename-node').click()
    expect(input_name).to_have_value(new_name)
    dialog.get_by_role('button',name='取消',exact=True).click()
    page.get_by_role('button',name=new_name,exact=True).click();page.locator('#chart svg').wait_for()


def verify_route_during_refresh(page, agent, classic_id, classic_name):
    # Keep an actual old-node response pending while the user changes routes twice.
    page.wait_for_function('() => !refreshing')
    page.evaluate('''oldId => {
        const originalFetch=window.fetch.bind(window);
        window.__routeOriginalFetch=originalFetch;
        window.__routeDelayPending=false;
        let delayed=false;
        window.fetch=async (...args) => {
            const response=await originalFetch(...args);
            const url=String(args[0]?.url||args[0]);
            if(!delayed&&url.endsWith('/api/v1/nodes/'+oldId+'/commands')) {
                delayed=true;window.__routeDelayPending=true;
                await new Promise(resolve=>setTimeout(resolve,1500));
                window.__routeDelayPending=false;
            }
            return response;
        };
        window.dispatchEvent(new HashChangeEvent('hashchange'));
    }''', agent.node_id)
    try:
        page.wait_for_function('() => window.__routeDelayPending')
        started=time.monotonic()
        page.locator('#back').click()
        page.get_by_role('button',name=classic_name,exact=True).click()
        expect(page.locator('#nodeName')).to_have_text(classic_name,timeout=5000)
        assert time.monotonic()-started < 5, 'new-node rendering must not wait for the 15-second refresh'
        assert page.url.split('#')[-1] == classic_id
        expect(page.locator('#lineRows tr')).to_have_count(2)
        expect(page.locator('#lineRows')).to_contain_text('classic-1')
        expect(page.locator('#lineRows')).to_contain_text('classic-2')
        expect(page.locator('#lineRows')).not_to_contain_text('line-A')
        expect(page.locator('#lineRows')).not_to_contain_text('line-B')
        expect(page.locator('#quality')).to_contain_text('接口统计')
        expect(page.locator('#nodeControls')).not_to_be_visible()
        for row in page.locator('#lineRows tr').all():
            expect(row.locator('td').nth(2)).to_have_text('未知')
            expect(row.locator('td').nth(5)).to_have_text('未知')
    finally:
        page.evaluate('''() => {
            window.fetch=window.__routeOriginalFetch;
            delete window.__routeOriginalFetch;delete window.__routeDelayPending;
        }''')
    page.locator('#back').click()
    page.get_by_role('button',name='天津 · 分时拉流验证节点',exact=True).click()
    expect(page.locator('#nodeName')).to_have_text('天津 · 分时拉流验证节点')
    expect(page.locator('#lineRows')).to_contain_text('line-A')
    expect(page.locator('#lineRows')).to_contain_text('line-B')
    expect(page.locator('#nodeControls')).to_be_visible()


def verify_partial_upload_and_classic(page, agent, snapshot, transport):
    old_name = '天津 · 分时拉流验证节点'
    partial = copy.deepcopy(snapshot)
    partial['summary']['upload_mbps'] = None
    partial['lines'][1]['current_upload_mbps'] = None
    disabled = copy.deepcopy(partial['lines'][0])
    disabled.update(line_id='disabled-line', enabled=False, current_upload_mbps=999)
    partial['lines'].append(disabled)
    agent.request('/heartbeat', {'snapshot':partial})
    page.reload();page.locator('#chart svg').wait_for()
    sampled = page.locator('#detailKpis article').filter(has_text='已测上传速度')
    expect(sampled).to_contain_text('1/2 线路')
    expect(sampled).to_contain_text(re.compile(r'5\.5(?:0)? Mbps'))
    expect(sampled).not_to_contain_text('999')
    expect(page.locator('#detailKpis article').filter(has_text='当前上传速度')).to_have_count(0)
    expect(page.locator('#lineRows tr').nth(1).locator('td').nth(4)).to_contain_text('未知')
    page.locator('#back').click();page.get_by_role('button',name=old_name,exact=True).wait_for()
    fleet_card = page.locator('#nodes .node').filter(has=page.get_by_role('button',name=old_name,exact=True))
    expect(fleet_card).to_contain_text('已测上传速度 · 1/2 线路')
    expect(fleet_card).to_contain_text(re.compile(r'5\.5(?:0)? Mbps'))
    expect(fleet_card).not_to_contain_text('999')
    original_viewport = page.viewport_size
    page.set_viewport_size({'width':375,'height':812})
    page.screenshot(path='/tmp/fleet-dashboard-partial-upload-375x812.png',full_page=True)
    page.set_viewport_size(original_viewport)
    # An observed zero is a valid sample, while the other enabled line is unknown.
    partial['lines'][0]['current_upload_mbps'] = 0
    agent.request('/heartbeat', {'snapshot':partial})
    page.reload();page.get_by_role('button',name=old_name,exact=True).wait_for()
    expect(fleet_card).to_contain_text('已测上传速度 · 1/2 线路')
    expect(fleet_card.locator('.uploads')).to_contain_text('0 Mbps')
    agent.request('/heartbeat', {'snapshot':snapshot})

    classic_id, classic_token = str(uuid.uuid4()), 'c'*43
    classic_name = '经典 · 多 IP 程序验证节点'
    classic = copy.deepcopy(snapshot)
    target = 800*1_000_000//8*18*3600
    actual = target//4
    classic.update(mode='multi_ip', measurement_basis='program', target_basis='configured_speed',
                   stats_source='program', capabilities=[], config={},
                   config_version=hashlib.sha256(b'{}').hexdigest(), observed_at=int(time.time()),
                   snmp_status='unavailable', periods=[], rolling=None, groups=[], router_budget={})
    classic['summary'].update(actual_bytes=actual, program_bytes=actual, target_bytes=target,
                              remaining_bytes=target-actual, measured_bytes=0, estimated_bytes=0,
                              legacy_bytes=0, unknown_seconds=0, upload_bytes=None, upload_mbps=None)
    for index, line in enumerate(classic['lines']):
        line.update(line_id='classic-'+str(index+1), snmp_interface='', enabled=True,
                    actual_bytes=actual//2, program_bytes=actual//2, target_bytes=target//2,
                    measured_bytes=0, estimated_bytes=0, legacy_bytes=0, unknown_seconds=0,
                    upload_bytes=None, upload_mbps=None, current_upload_mbps=None,
                    assigned_mbps=400, metrics_available=True)
    def classic_request(path, payload, token):
        response = transport.post('/api/v1/agent'+path, json=payload,
                                  headers={'Authorization':'Bearer '+token})
        response.raise_for_status()
        return response.json()
    classic_request('/register', {'node_id':classic_id, 'node_token':classic_token,
                                 'name':classic_name}, 'e'*40)
    classic_request('/heartbeat', {'snapshot':classic}, classic_token)
    page.reload();page.get_by_role('button',name=classic_name,exact=True).wait_for()
    classic_card = page.locator('#nodes .node').filter(has=page.get_by_role('button',name=classic_name,exact=True))
    expect(classic_card).to_contain_text('当日下载 · 程序统计')
    expect(classic_card).to_contain_text('配置下载速度 × 时段时长')
    expect(classic_card).not_to_contain_text('倍率未知')
    expect(classic_card.locator('.uploads')).to_contain_text('未知')
    expect_meter(classic_card.locator('.completion-meter'), '25.0%', 25)
    expect_segments(classic_card.locator('.completion-meter'), {'unclassified':25,'remaining':75})
    page.get_by_role('button',name=classic_name,exact=True).click();page.locator('#chart svg').wait_for()
    expect(page.locator('#detailKpis')).to_contain_text('当日下载 · 程序统计')
    expect(page.locator('#detailKpis')).to_contain_text('配置下载速度 × 时段时长')
    expect(page.locator('#detailKpis')).to_contain_text(f'{target/1024**4:.2f} TiB')
    expect(page.locator('#nodeCompletion')).not_to_contain_text('倍率未知')
    expect(page.locator('#quality')).to_contain_text('未接入 SNMP')
    expect(page.locator('#quality')).to_contain_text('没有持久流量历史')
    expect(page.locator('#quality')).not_to_contain_text('滚动')
    expect(page.locator('#nodeControls')).not_to_be_visible()
    expect(page.locator('#start')).to_be_disabled();expect(page.locator('#stop')).to_be_disabled()
    expect(page.locator('#periods .period')).to_have_count(0)
    expect(page.locator('#chart polyline.actual')).to_have_count(0)
    expect(page.locator('#chart polyline.program')).to_have_count(0)
    expect(page.locator('#detail .legend .blue')).to_contain_text('未接入')
    expect(page.locator('#detail .legend .amber')).to_have_text('● 下载统计')
    expect(page.locator('#chart svg')).to_have_attribute('aria-label',re.compile('未接入'))
    assert page.locator('#chartRows tr').filter(has_text='缺失').count() == 144
    for row in page.locator('#lineRows tr').all():
        expect(row.locator('td').nth(4)).to_contain_text('未知')

    # Only closed samples collected after enrollment may appear in program history.
    upper = int(time.time())//600*600
    buckets = []
    for start, program_bytes in [(upper-1200, 300_000_000), (upper-600, 600_000_000)]:
        buckets.append({'line_id':'__classic_node__', 'start':start, 'actual_bytes':0,
                        'program_bytes':program_bytes, 'measured_bytes':0, 'estimated_bytes':0,
                        'legacy_bytes':0, 'unknown_seconds':0, 'sample_seconds':0,
                        'observed_minutes':10, 'quality':'program_only'})
    classic_request('/telemetry', {'schema_version':1, 'buckets':buckets}, classic_token)
    page.reload();page.locator('#chart polyline.program').wait_for()
    expect(page.locator('#chart polyline.actual')).to_have_count(0)
    expect(page.locator('#chartRows tr').nth(142).locator('td').nth(1)).to_have_text('未接入')
    expect(page.locator('#chartRows tr').nth(142).locator('td').nth(2)).to_have_text('4.00')
    expect(page.locator('#chartRows tr').nth(143).locator('td').nth(2)).to_have_text('8.00')
    assert page.locator('#chartRows tr').filter(has_text='缺失').count() == 142
    assert page.locator('#chartRows tr').evaluate_all(
        "rows => rows.filter(row => row.cells[1].textContent === '未接入' && row.cells[2].textContent === '缺失').length") == 142
    for width, height in [(375, 812), (1440, 1000)]:
        page.set_viewport_size({'width':width,'height':height})
        assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'),f'classic overflow at {width}x{height}'
        page.screenshot(path=f'/tmp/fleet-dashboard-classic-{width}x{height}.png',full_page=True)
    fallback = copy.deepcopy(classic)
    fallback['stats_source'] = 'interface_fallback'
    for line in fallback['lines']:
        line.update(metrics_available=False, actual_bytes=0, program_bytes=0)
    classic_request('/heartbeat', {'snapshot':fallback}, classic_token)
    page.reload();page.locator('#chart svg').wait_for()
    expect(page.locator('#detailKpis')).to_contain_text('接口统计')
    expect(page.locator('#detailKpis')).not_to_contain_text('当日下载 · 程序统计')
    expect(page.locator('#detailKpis')).not_to_contain_text('当日拉流进程下载')
    expect(page.locator('#detail .legend .amber')).to_contain_text('下载统计')
    expect(page.locator('#detail .legend .amber')).not_to_contain_text('拉流进程')
    expect(page.locator('#quality')).to_contain_text('接口')
    expect(page.locator('#quality')).to_contain_text('未接入 SNMP')
    expect(page.locator('#nodeControls')).not_to_be_visible()
    for row in page.locator('#lineRows tr').all():
        expect(row.locator('td').nth(2)).to_have_text('未知')
        expect(row.locator('td').nth(5)).to_have_text('未知')
        expect(row.locator('.completion-meter')).to_contain_text('未知')
        expect(row.locator('[role="progressbar"]')).to_have_count(0)
    page.set_viewport_size({'width':375,'height':812})
    assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path='/tmp/fleet-dashboard-classic-fallback-375x812.png',full_page=True)
    page.locator('#back').click();page.get_by_role('button',name=old_name,exact=True).click()
    page.locator('#chart svg').wait_for()
    expect(page.locator('#nodeControls')).to_be_visible()
    verify_route_during_refresh(page,agent,classic_id,classic_name)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--chromium');args=parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);node_dir=root/'node';node_dir.mkdir()
        app=create_app({'DASHBOARD_DB':str(root/'cloud.db'),'DASHBOARD_ENROLLMENT_TOKEN':'e'*40,
                        'DASHBOARD_READ_TOKEN':'r'*40,'DASHBOARD_ADMIN_PASSWORD_HASH':hash_password('testpassword123')})
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        base=f'http://127.0.0.1:{port}'
        deadline=time.monotonic()+10
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('test server did not start')
            time.sleep(.05)
        with patch.dict('os.environ',{'APPLY_LAN_IPS':'0','FLEET_AUTOSTART':'false','DASHBOARD_URL':''}):
            controller=FleetController(node_dir/'config.json',FleetConfig(daily_mode_enabled=True,target_ratio=2.4))
        controller.store.import_accounts(parse_accounts_csv('account_id,bind_ip,snmp_interface,weight\nline-A,192.168.2.11,pppoe-a,1\nline-B,192.168.2.12,pppoe-b,10\n'))
        with patch('threading.Thread.start'):
            agent=DashboardAgent(node_dir,{'DASHBOARD_URL':'https://browser.test','DASHBOARD_NODE_NAME':'天津 · 分时拉流验证节点'})
        transport=httpx.Client(base_url=base,timeout=10)
        def request(path,payload=None,enrollment=False):
            response=transport.request('POST' if payload is not None else 'GET','/api/v1/agent'+path,json=payload,
                                       headers={'Authorization':'Bearer '+('e'*40 if enrollment else agent.token)})

            if response.status_code>=400:raise RuntimeError(response.text)
            return response.json()
        agent.request=request
        now=datetime.now(ZoneInfo('Asia/Shanghai'));upper=int(now.timestamp())//600*600
        with controller.store.db:
            for i in range(144):
                if 30<=i<=34:continue
                for account in ['line-A','line-B']:
                    controller.store._add_traffic(account,(upper-86400+i*600)//60,
                         measured_download=int((100+i%30)*1_000_000/8*600),
                         measured_upload=10000000,download=500000000,sample_seconds=600)
        agent.tick(controller,now,100)
        visual_snapshot=completion_fixture(json.loads(agent.get('snapshot')))
        agent.set('snapshot',json.dumps(visual_snapshot));agent.cycle()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,**({'executable_path':args.chromium} if args.chromium else {}))
                context=browser.new_context(http_credentials={'username':'admin','password':'testpassword123'},viewport={'width':1440,'height':1000})
                page=context.new_page();errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base);page.get_by_role('button',name='天津 · 分时拉流验证节点',exact=True).wait_for()
                verify_completion_and_upload(page,agent,visual_snapshot)
                verify_partial_upload_and_classic(page,agent,visual_snapshot,transport)
                page.locator('#lineRows tr').first.wait_for()
                assert page.locator('#lineRows tr').count()==2
                assert page.locator('#chartRows tr').count()==144
                page.locator('#chartLine').select_option('line-A')
                expect(page.locator('#chartRows tr:last-child td:last-child')).to_have_text('1')
                page.locator('#chartLine').select_option('')
                expect(page.locator('#chartRows tr:last-child td:last-child')).to_have_text('2')
                verify_rename(page,agent)
                page.on('dialog',lambda dialog:dialog.accept())
                page.locator('#start').click()
                page.get_by_text('启动下载 · 等待节点领取',exact=True).wait_for()
                agent.cycle();agent.tick(controller,datetime.now(ZoneInfo('Asia/Shanghai')),140);agent.cycle()
                page.reload();page.get_by_text('启动下载 · 执行成功',exact=True).wait_for()
                assert controller.manual_enabled
                page.locator('#stop').click()
                page.get_by_text('停止下载 · 等待节点领取',exact=True).wait_for()
                agent.cycle();agent.tick(controller,datetime.now(ZoneInfo('Asia/Shanghai')),180);agent.cycle()
                page.reload();page.get_by_text('停止下载 · 执行成功',exact=True).wait_for()
                assert not controller.manual_enabled
                # A dirty form keeps the configuration revision it was based on.
                page.get_by_text('编辑调度参数',exact=True).click()
                page.locator('[name=target_ratio]').fill('2.1')
                controller.update_config({'target_ratio':2.2})
                agent.tick(controller,datetime.now(ZoneInfo('Asia/Shanghai')),220);agent.cycle()
                page.locator('#configForm button').click()
                page.locator('#error').wait_for()
                assert '节点配置已变化' in page.locator('#error').inner_text()
                assert controller.cfg.target_ratio==2.2
                for width in [375,768,1440]:
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'),f'overflow at {width}'
                    page.screenshot(path=f'/tmp/fleet-dashboard-{width}.png',full_page=True)
                assert not errors,errors
                print('Browser PASS: route switching during pending refresh without stale node data, partial upload coverage/unknown/valid zero/disabled-line exclusion, classic program accounting/configured-speed targets/no invented SNMP or history/no remote controls, colored glass completion segments, source proportions and unknown attribution, target multipliers, translucent curves with gaps, rename/cancel/validation/500 retry/re-registration/history, >100% clamping, cumulative period targets, unknown/no-target/upload-only states, real upload rate vs package capacity, old-agent fields, 144-point chart, line filtering, start/stop receipts, configuration conflict; mobile/tablet/desktop/landscape without page overflow or JS errors')
                browser.close()
        finally:
            transport.close();agent.db.close();controller.shutdown();server.should_exit=True;thread.join(timeout=5);sock.close()


if __name__=='__main__':main()
