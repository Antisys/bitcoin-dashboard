#!/usr/bin/env python3
"""
Bitcoin Sync Dashboard with AssumeUTXO Support

Shows a unified progress bar with:
- Left side: Background validation (0 → snapshot height)
- Middle: The loaded snapshot
- Right side: Catching up to tip (snapshot → current tip)
"""

import subprocess
import json
import time
import http.server
import socketserver
import argparse
import os
from urllib.parse import urlparse
from collections import deque

CONFIG = {
    'host': 'localhost',
    'ssh_user': os.environ.get('USER', 'root'),
    'port': 8890,
    'rpc_host': '127.0.0.1',
    'rpc_port': 8332,
    'docker_container': None,
}

history_top = deque(maxlen=60)
history_bottom = deque(maxlen=60)

prev_cpu_stats = {'timestamp': 0, 'total': None, 'per_core': None}
last_cpu_result = {'cpu_total_used': 0, 'cpu_user': 0, 'cpu_system': 0, 'cpu_iowait': 0, 'cpu_idle': 100, 'cpu_per_core': []}

# Mode tracking - only switch modes after seeing consistent results
mode_tracker = {'current_mode': None, 'consecutive_count': 0, 'confirmed_mode': None}

# Cache for static info that doesn't change
static_cache = {'version': None, 'pruned': None, 'cpu_model': None, 'cpu_cores': None}

# Time-based cache for slow-changing values
timed_cache = {
    'size_on_disk': {'value': 0, 'last_update': 0, 'ttl': 120},  # 2 minutes
    'connections': {'value': 0, 'in': 0, 'out': 0, 'last_update': 0, 'ttl': 60},  # 1 minute
}


def run_command(cmd, timeout=15):
    try:
        if CONFIG['host'] == 'localhost' or CONFIG['host'] == '127.0.0.1':
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        else:
            ssh_cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                       f"{CONFIG['ssh_user']}@{CONFIG['host']}", cmd]
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def run_bitcoin_cli(rpc_cmd):
    if CONFIG['docker_container']:
        cmd = f"docker exec {CONFIG['docker_container']} bitcoin-cli -rpcuser=bitcoin -rpcpassword=bitcoinrpc {rpc_cmd}"
    else:
        cmd = f"bitcoin-cli -rpcconnect={CONFIG['rpc_host']} -rpcport={CONFIG['rpc_port']} {rpc_cmd}"
    return run_command(cmd)


def parse_proc_stat(stat_output):
    result = {'total': None, 'per_core': []}
    if not stat_output:
        return result
    for line in stat_output.split('\n'):
        parts = line.split()
        if not parts:
            continue
        if parts[0] == 'cpu':
            try:
                times = [int(x) for x in parts[1:8]]
                result['total'] = {
                    'user': times[0], 'nice': times[1], 'system': times[2],
                    'idle': times[3], 'iowait': times[4], 'irq': times[5],
                    'softirq': times[6], 'total': sum(times)
                }
            except (ValueError, IndexError):
                pass
        elif parts[0].startswith('cpu') and parts[0][3:].isdigit():
            try:
                core_num = int(parts[0][3:])
                times = [int(x) for x in parts[1:8]]
                result['per_core'].append({
                    'core': core_num, 'user': times[0], 'nice': times[1],
                    'system': times[2], 'idle': times[3], 'iowait': times[4],
                    'irq': times[5], 'softirq': times[6], 'total': sum(times)
                })
            except (ValueError, IndexError):
                pass
    result['per_core'].sort(key=lambda x: x['core'])
    return result


def calculate_cpu_usage(prev, curr):
    if not prev or not curr:
        return None
    total_delta = curr['total'] - prev['total']
    if total_delta <= 0:
        return None
    return {
        'user': ((curr['user'] - prev['user']) / total_delta) * 100,
        'system': ((curr['system'] - prev['system']) / total_delta) * 100,
        'idle': ((curr['idle'] - prev['idle']) / total_delta) * 100,
        'iowait': ((curr['iowait'] - prev['iowait']) / total_delta) * 100,
        'total_used': ((total_delta - (curr['idle'] - prev['idle']) - (curr['iowait'] - prev['iowait'])) / total_delta) * 100
    }


def calculate_per_core_usage(prev_cores, curr_cores):
    if not prev_cores or not curr_cores or len(prev_cores) != len(curr_cores):
        return None
    result = []
    for prev, curr in zip(prev_cores, curr_cores):
        total_delta = curr['total'] - prev['total']
        if total_delta <= 0:
            result.append({'core': curr['core'], 'user': 0, 'system': 0, 'iowait': 0, 'idle': 100})
            continue
        result.append({
            'core': curr['core'],
            'user': ((curr['user'] - prev['user'] + curr['nice'] - prev['nice']) / total_delta) * 100,
            'system': ((curr['system'] - prev['system'] + curr['irq'] - prev['irq'] + curr['softirq'] - prev['softirq']) / total_delta) * 100,
            'iowait': ((curr['iowait'] - prev['iowait']) / total_delta) * 100,
            'idle': ((curr['idle'] - prev['idle']) / total_delta) * 100
        })
    return result


def get_cpu_stats():
    global prev_cpu_stats, last_cpu_result
    stats = {}
    stat_output = run_command("cat /proc/stat")
    current_time = time.time()
    current_stats = parse_proc_stat(stat_output)

    if prev_cpu_stats['total'] and (current_time - prev_cpu_stats['timestamp']) >= 0.3:
        cpu_usage = calculate_cpu_usage(prev_cpu_stats['total'], current_stats['total'])
        if cpu_usage:
            stats['cpu_total_used'] = cpu_usage['total_used']
            stats['cpu_user'] = cpu_usage['user']
            stats['cpu_system'] = cpu_usage['system']
            stats['cpu_iowait'] = cpu_usage['iowait']
            stats['cpu_idle'] = cpu_usage['idle']
            last_cpu_result.update(stats)
        per_core = calculate_per_core_usage(prev_cpu_stats['per_core'], current_stats['per_core'])
        if per_core:
            stats['cpu_per_core'] = per_core
            last_cpu_result['cpu_per_core'] = per_core
        prev_cpu_stats = {'timestamp': current_time, 'total': current_stats['total'], 'per_core': current_stats['per_core']}
    elif not prev_cpu_stats['total']:
        # First call - just store baseline
        prev_cpu_stats = {'timestamp': current_time, 'total': current_stats['total'], 'per_core': current_stats['per_core']}

    # Return last known values if current calculation failed
    if not stats:
        stats = last_cpu_result.copy()
    return stats


def get_system_stats():
    stats = {}
    # Cache static CPU info
    if static_cache['cpu_model']:
        stats['cpu_model'] = static_cache['cpu_model']
        stats['cpu_cores'] = static_cache['cpu_cores']
    else:
        cpu_info = run_command("cat /proc/cpuinfo | grep 'model name' | head -1 | cut -d: -f2")
        if cpu_info:
            static_cache['cpu_model'] = cpu_info.strip()
            stats['cpu_model'] = static_cache['cpu_model']
        nproc = run_command("nproc")
        if nproc:
            try:
                static_cache['cpu_cores'] = int(nproc)
                stats['cpu_cores'] = static_cache['cpu_cores']
            except ValueError:
                pass
    cpu_stats = get_cpu_stats()
    stats.update(cpu_stats)

    mem_info = run_command("free -b | grep Mem")
    if mem_info:
        parts = mem_info.split()
        if len(parts) >= 7:
            try:
                stats['mem_total'] = int(parts[1])
                stats['mem_used'] = int(parts[2])
                stats['mem_percent'] = (stats['mem_used'] / stats['mem_total']) * 100
            except (ValueError, ZeroDivisionError):
                pass

    uptime_info = run_command("cat /proc/uptime")
    if uptime_info:
        try:
            stats['uptime_seconds'] = float(uptime_info.split()[0])
            stats['uptime_human'] = format_duration(stats['uptime_seconds'])
        except (ValueError, IndexError):
            pass
    return stats


def get_bitcoin_stats():
    global mode_tracker
    stats = {'sync_mode': 'normal', 'assumeutxo': False, 'mode_confirmed': False}
    detected_mode = 'unknown'

    # Try getchainstates first (for assumeutxo detection)
    chainstates = run_bitcoin_cli("getchainstates")
    if chainstates:
        try:
            data = json.loads(chainstates)
            states = data.get('chainstates', [])
            stats['headers'] = data.get('headers', 0)

            if len(states) == 2:
                # AssumeUTXO mode - two chainstates
                detected_mode = 'assumeutxo'
                stats['assumeutxo'] = True
                stats['sync_mode'] = 'assumeutxo'

                # Find which is background (validated=true) and which is snapshot
                for state in states:
                    if state.get('snapshot_blockhash'):
                        # This is the snapshot chainstate (syncing to tip)
                        stats['snapshot_height'] = 840000  # Known snapshot height
                        stats['top_height'] = state.get('blocks', 0)
                        stats['top_progress'] = state.get('verificationprogress', 0) * 100
                    else:
                        # This is background validation (syncing from genesis)
                        stats['bottom_height'] = state.get('blocks', 0)
                        stats['bottom_progress'] = state.get('verificationprogress', 0) * 100

                # Calculate combined progress
                snapshot = stats.get('snapshot_height', 840000)
                headers = stats['headers']
                bottom = stats.get('bottom_height', 0)
                top = stats.get('top_height', snapshot)

                stats['bottom_pct'] = (bottom / headers) * 100 if headers > 0 else 0
                stats['top_pct'] = (top / headers) * 100 if headers > 0 else 0
                stats['snapshot_pct'] = (snapshot / headers) * 100 if headers > 0 else 0

                stats['height'] = top
                stats['ibd'] = top < headers

            elif len(states) == 1:
                # Single chainstate - normal mode confirmed
                detected_mode = 'normal'

        except json.JSONDecodeError:
            detected_mode = 'unknown'

    # Fallback to regular getblockchaininfo for normal mode data
    if not stats.get('assumeutxo'):
        info = run_bitcoin_cli("getblockchaininfo")
        if info:
            try:
                data = json.loads(info)
                stats['height'] = data.get('blocks', 0)
                stats['headers'] = data.get('headers', 0)
                stats['progress'] = data.get('verificationprogress', 0) * 100
                stats['ibd'] = data.get('initialblockdownload', True)
                stats['size_on_disk'] = data.get('size_on_disk', 0)
                stats['pruned'] = data.get('pruned', False)
                if detected_mode == 'unknown':
                    detected_mode = 'normal'
            except json.JSONDecodeError:
                pass

    # Mode confirmation logic - sticky once confirmed
    if detected_mode != 'unknown':
        if detected_mode == mode_tracker['current_mode']:
            mode_tracker['consecutive_count'] += 1
        else:
            mode_tracker['current_mode'] = detected_mode
            mode_tracker['consecutive_count'] = 1

        # First time: confirm after 3 readings
        # After confirmed: require 5 consecutive different readings to switch
        if mode_tracker['confirmed_mode'] is None:
            if mode_tracker['consecutive_count'] >= 3:
                mode_tracker['confirmed_mode'] = detected_mode
        elif detected_mode != mode_tracker['confirmed_mode'] and mode_tracker['consecutive_count'] >= 5:
            mode_tracker['confirmed_mode'] = detected_mode

    # Use confirmed mode for output
    if mode_tracker['confirmed_mode']:
        stats['sync_mode'] = mode_tracker['confirmed_mode']
        stats['assumeutxo'] = (mode_tracker['confirmed_mode'] == 'assumeutxo')
        stats['mode_confirmed'] = True
    else:
        stats['mode_confirmed'] = False

    # Get network info - cache version, timed cache for connections
    now = time.time()
    if static_cache['version']:
        stats['version'] = static_cache['version']
        # Check if connections cache is still valid
        if now - timed_cache['connections']['last_update'] < timed_cache['connections']['ttl']:
            stats['connections'] = timed_cache['connections']['value']
            stats['connections_in'] = timed_cache['connections']['in']
            stats['connections_out'] = timed_cache['connections']['out']
        else:
            peerinfo = run_bitcoin_cli("getconnectioncount")
            if peerinfo:
                try:
                    timed_cache['connections']['value'] = int(peerinfo)
                    timed_cache['connections']['last_update'] = now
                    stats['connections'] = timed_cache['connections']['value']
                    stats['connections_in'] = 0
                    stats['connections_out'] = stats['connections']
                except ValueError:
                    stats['connections'] = timed_cache['connections']['value']
    else:
        netinfo = run_bitcoin_cli("getnetworkinfo")
        if netinfo:
            try:
                data = json.loads(netinfo)
                static_cache['version'] = data.get('subversion', 'unknown')
                stats['version'] = static_cache['version']
                timed_cache['connections']['value'] = data.get('connections', 0)
                timed_cache['connections']['in'] = data.get('connections_in', 0)
                timed_cache['connections']['out'] = data.get('connections_out', 0)
                timed_cache['connections']['last_update'] = now
                stats['connections'] = timed_cache['connections']['value']
                stats['connections_in'] = timed_cache['connections']['in']
                stats['connections_out'] = timed_cache['connections']['out']
            except json.JSONDecodeError:
                pass

    # Get size on disk - use timed cache (2 min TTL)
    now = time.time()
    if now - timed_cache['size_on_disk']['last_update'] < timed_cache['size_on_disk']['ttl']:
        stats['size_on_disk'] = timed_cache['size_on_disk']['value']
        if static_cache['pruned'] is not None:
            stats['pruned'] = static_cache['pruned']
    else:
        info = run_bitcoin_cli("getblockchaininfo")
        if info:
            try:
                data = json.loads(info)
                timed_cache['size_on_disk']['value'] = data.get('size_on_disk', 0)
                timed_cache['size_on_disk']['last_update'] = now
                stats['size_on_disk'] = timed_cache['size_on_disk']['value']
                static_cache['pruned'] = data.get('pruned', False)
                stats['pruned'] = static_cache['pruned']
            except json.JSONDecodeError:
                stats['size_on_disk'] = timed_cache['size_on_disk']['value']
                stats['pruned'] = static_cache['pruned'] or False

    stats['timestamp'] = time.time()
    return stats


def calculate_speed(stats):
    current_time = stats['timestamp']

    if stats.get('assumeutxo'):
        # Track both sync directions
        top_height = stats.get('top_height', 0)
        bottom_height = stats.get('bottom_height', 0)

        history_top.append((current_time, top_height))
        history_bottom.append((current_time, bottom_height))

        # Calculate top speed (toward tip)
        if len(history_top) >= 2:
            t_diff = history_top[-1][0] - history_top[0][0]
            b_diff = history_top[-1][1] - history_top[0][1]
            stats['speed_top'] = max(0, b_diff / t_diff) if t_diff > 0 else 0

        # Calculate bottom speed (background validation)
        if len(history_bottom) >= 2:
            t_diff = history_bottom[-1][0] - history_bottom[0][0]
            b_diff = history_bottom[-1][1] - history_bottom[0][1]
            stats['speed_bottom'] = max(0, b_diff / t_diff) if t_diff > 0 else 0

        # ETA for top sync (to tip)
        remaining_top = stats.get('headers', 0) - top_height
        if stats.get('speed_top', 0) > 0:
            stats['eta_top'] = remaining_top / stats['speed_top']
            stats['eta_top_human'] = format_duration(stats['eta_top'])

        # ETA for bottom sync (to snapshot)
        remaining_bottom = stats.get('snapshot_height', 840000) - bottom_height
        if stats.get('speed_bottom', 0) > 0:
            stats['eta_bottom'] = remaining_bottom / stats['speed_bottom']
            stats['eta_bottom_human'] = format_duration(stats['eta_bottom'])
    else:
        # Normal sync
        history_top.append((current_time, stats.get('height', 0)))
        if len(history_top) >= 2:
            t_diff = history_top[-1][0] - history_top[0][0]
            b_diff = history_top[-1][1] - history_top[0][1]
            stats['speed'] = max(0, b_diff / t_diff) if t_diff > 0 else 0
            remaining = stats.get('headers', 0) - stats.get('height', 0)
            if stats['speed'] > 0:
                stats['eta'] = remaining / stats['speed']
                stats['eta_human'] = format_duration(stats['eta'])

    return stats


def format_duration(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    elif seconds < 86400:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    else:
        return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"


def format_bytes(b):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


DASHBOARD_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Bitcoin Sync Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Tahoma, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #eee;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 {
            text-align: center;
            margin-bottom: 30px;
            color: #f7931a;
            text-shadow: 0 0 20px rgba(247, 147, 26, 0.3);
        }
        h2 { color: #888; margin-bottom: 20px; font-weight: normal; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: rgba(255,255,255,0.05);
            border-radius: 15px;
            padding: 25px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .card-title {
            font-size: 0.85em;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }
        .card-value { font-size: 1.8em; font-weight: bold; color: #fff; word-break: break-word; }
        .card-value-small { font-size: 1.1em; font-weight: bold; color: #fff; word-break: break-word; }
        .card-sub { font-size: 0.9em; color: #666; margin-top: 5px; }

        .progress-container {
            background: rgba(255,255,255,0.1);
            border-radius: 15px;
            padding: 30px;
            margin-bottom: 30px;
        }
        .mode-indicator {
            display: inline-block;
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.8em;
            margin-bottom: 15px;
        }
        .mode-assumeutxo { background: #4CAF50; color: white; }
        .mode-normal { background: #2196F3; color: white; }

        .unified-bar-container {
            position: relative;
            height: 50px;
            background: rgba(0,0,0,0.4);
            border-radius: 10px;
            margin: 20px 0;
            overflow: hidden;
        }
        .bar-bottom {
            position: absolute;
            left: 0;
            top: 0;
            height: 100%;
            background: linear-gradient(90deg, #2196F3 0%, #64B5F6 100%);
            z-index: 1;
        }
        .bar-top {
            position: absolute;
            top: 0;
            height: 100%;
            background: linear-gradient(90deg, #FF9800 0%, #f7931a 100%);
            z-index: 2;
        }
        .bar-snapshot-marker {
            position: absolute;
            top: 0;
            height: 100%;
            width: 4px;
            background: #fff;
            z-index: 3;
            box-shadow: 0 0 10px rgba(255,255,255,0.5);
        }
        .bar-label {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            font-size: 0.85em;
            font-weight: bold;
            z-index: 4;
            text-shadow: 0 1px 3px rgba(0,0,0,0.8);
        }
        .bar-label-bottom { left: 10px; color: #fff; }
        .bar-label-top { right: 10px; color: #fff; }
        .bar-label-snapshot {
            color: #fff;
            font-size: 0.75em;
            white-space: nowrap;
        }

        .progress-labels {
            display: flex;
            justify-content: space-between;
            font-size: 0.9em;
            color: #888;
            margin-top: 10px;
        }
        .progress-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }
        .progress-stat {
            background: rgba(0,0,0,0.2);
            padding: 15px;
            border-radius: 10px;
        }
        .progress-stat-label { font-size: 0.8em; color: #888; }
        .progress-stat-value { font-size: 1.3em; font-weight: bold; margin-top: 5px; }
        .color-bottom { color: #64B5F6; }
        .color-top { color: #f7931a; }

        .status-dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 2s infinite;
        }
        .status-syncing { background: #f7931a; }
        .status-synced { background: #00ff00; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

        .legend {
            display: flex;
            gap: 20px;
            justify-content: center;
            margin-top: 15px;
            flex-wrap: wrap;
        }
        .legend-item {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.9em;
        }
        .legend-color {
            width: 20px;
            height: 12px;
            border-radius: 3px;
        }
        .legend-bottom { background: linear-gradient(90deg, #2196F3, #64B5F6); }
        .legend-top { background: linear-gradient(90deg, #FF9800, #f7931a); }
        .legend-snapshot { background: #fff; width: 4px; }

        .cpu-bars { margin-top: 15px; }
        .cpu-bar-row { display: flex; align-items: center; margin-bottom: 6px; font-size: 0.8em; }
        .cpu-bar-label { width: 50px; color: #888; }
        .cpu-bar-bg { flex: 1; height: 10px; background: rgba(0,0,0,0.3); border-radius: 5px; overflow: hidden; display: flex; }
        .cpu-bar-user { background: #4CAF50; height: 100%; }
        .cpu-bar-system { background: #2196F3; height: 100%; }
        .cpu-bar-iowait { background: #FF9800; height: 100%; }
        .cpu-bar-val { width: 40px; text-align: right; color: #aaa; }
        .cpu-legend { display: flex; gap: 15px; margin-top: 10px; font-size: 0.75em; color: #888; flex-wrap: wrap; }
        .cpu-legend-item { display: flex; align-items: center; gap: 5px; }
        .cpu-legend-color { width: 12px; height: 8px; border-radius: 2px; }
        .cpu-legend-user { background: #4CAF50; }
        .cpu-legend-system { background: #2196F3; }
        .cpu-legend-iowait { background: #FF9800; }

        .footer { text-align: center; color: #555; margin-top: 30px; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="container">
        <h1>&#8383; Bitcoin Sync Dashboard</h1>

        <div class="progress-container">
            <span class="mode-indicator mode-normal" id="modeIndicator">Normal Sync</span>
            <span style="margin-left: 15px;"><span class="status-dot status-syncing" id="statusDot"></span><span id="statusText">Syncing...</span></span>

            <div class="unified-bar-container" id="unifiedBar">
                <div class="bar-bottom" id="barBottom" style="width: 0%"></div>
                <div class="bar-top" id="barTop" style="left: 0%; width: 0%"></div>
                <div class="bar-snapshot-marker" id="snapshotMarker" style="left: 50%; display: none;"></div>
                <span class="bar-label bar-label-bottom" id="labelBottom"></span>
                <span class="bar-label bar-label-top" id="labelTop"></span>
            </div>

            <div class="progress-labels">
                <span>Block 0</span>
                <span id="snapshotLabel" style="display: none;">Snapshot: 840,000</span>
                <span id="tipLabel">Tip: 0</span>
            </div>

            <div class="legend" id="legend">
                <div class="legend-item"><div class="legend-color legend-bottom"></div> Background Validation</div>
                <div class="legend-item"><div class="legend-color legend-snapshot"></div> Snapshot</div>
                <div class="legend-item"><div class="legend-color legend-top"></div> Syncing to Tip</div>
            </div>

            <div class="progress-stats" id="progressStats">
                <div class="progress-stat">
                    <div class="progress-stat-label">Background Validation (0 → 840,000)</div>
                    <div class="progress-stat-value color-bottom" id="bottomStats">-</div>
                </div>
                <div class="progress-stat">
                    <div class="progress-stat-label">Catching Up (840,000 → Tip)</div>
                    <div class="progress-stat-value color-top" id="topStats">-</div>
                </div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <div class="card-title">Connections</div>
                <div class="card-value" id="connections">-</div>
                <div class="card-sub" id="connSub">-</div>
            </div>
            <div class="card">
                <div class="card-title">Disk Usage</div>
                <div class="card-value" id="diskUsage">-</div>
                <div class="card-sub" id="diskSub">-</div>
            </div>
            <div class="card">
                <div class="card-title">Version</div>
                <div class="card-value-small" id="version">-</div>
                <div class="card-sub" id="versionSub">-</div>
            </div>
        </div>

        <h2>System Resources</h2>
        <div class="grid">
            <div class="card">
                <div class="card-title">CPU</div>
                <div class="card-value" id="cpuUsage">-</div>
                <div class="card-sub" id="cpuModel">-</div>
                <div class="cpu-bars" id="cpuBars"></div>
                <div class="cpu-legend">
                    <div class="cpu-legend-item"><div class="cpu-legend-color cpu-legend-user"></div> User</div>
                    <div class="cpu-legend-item"><div class="cpu-legend-color cpu-legend-system"></div> System</div>
                    <div class="cpu-legend-item"><div class="cpu-legend-color cpu-legend-iowait"></div> I/O Wait</div>
                </div>
            </div>
            <div class="card">
                <div class="card-title">Memory</div>
                <div class="card-value" id="memUsage">-</div>
                <div class="card-sub" id="memDetails">-</div>
            </div>
            <div class="card">
                <div class="card-title">Uptime</div>
                <div class="card-value" id="uptime">-</div>
                <div class="card-sub">system uptime</div>
            </div>
        </div>

        <div class="footer">
            <p>Blocks: 60s, CPU: 5s | <span id="lastUpdate">-</span></p>
        </div>
    </div>

    <script>
        let lastKnownMode = null;
        let lastData = null;
        let lastFetchTime = 0;

        function fmt(n) { return n.toLocaleString(); }

        function resetBars() {
            document.getElementById('barBottom').style.width = '0%';
            document.getElementById('barTop').style.left = '0%';
            document.getElementById('barTop').style.width = '0%';
            document.getElementById('snapshotMarker').style.display = 'none';
        }

        function interpolate() {
            if (!lastData || !lastData.mode_confirmed) return;

            const elapsed = (Date.now() - lastFetchTime) / 1000;
            const d = lastData;
            const headers = d.headers || 0;

            if (d.assumeutxo) {
                // Interpolate both syncs - use 80% of speed to be conservative
                const snapshot = d.snapshot_height || 840000;
                const topSpeed = Math.max(0, (d.speed_top || 0)) * 0.8;
                const bottomSpeed = Math.max(0, (d.speed_bottom || 0)) * 0.8;
                const estTop = Math.max(0, Math.min((d.top_height || 0) + (topSpeed * elapsed), headers));
                const estBottom = Math.max(0, Math.min((d.bottom_height || 0) + (bottomSpeed * elapsed), snapshot));

                const snapshotPct = (snapshot / headers) * 100;
                const topPct = (estTop / headers) * 100;
                const bottomPct = (estBottom / headers) * 100;

                // Update progress bars
                document.getElementById('barBottom').style.width = bottomPct + '%';
                document.getElementById('barTop').style.left = snapshotPct + '%';
                document.getElementById('barTop').style.width = (topPct - snapshotPct) + '%';
                document.getElementById('labelBottom').textContent = fmt(Math.floor(estBottom));
                document.getElementById('labelTop').textContent = fmt(Math.floor(estTop));

                // Update stats boxes
                const bottomRemain = snapshot - Math.floor(estBottom);
                const topRemain = headers - Math.floor(estTop);
                document.getElementById('bottomStats').innerHTML =
                    `Block ${fmt(Math.floor(estBottom))} / ${fmt(snapshot)}<br>` +
                    `<span style="font-size:0.7em">${fmt(bottomRemain)} remaining` +
                    (d.speed_bottom ? ` @ ${Math.max(0, d.speed_bottom).toFixed(1)} b/s` : '') +
                    (d.eta_bottom_human ? ` (ETA: ${d.eta_bottom_human})` : '') + `</span>`;
                document.getElementById('topStats').innerHTML =
                    `Block ${fmt(Math.floor(estTop))} / ${fmt(headers)}<br>` +
                    `<span style="font-size:0.7em">${fmt(topRemain)} remaining` +
                    (d.speed_top ? ` @ ${Math.max(0, d.speed_top).toFixed(1)} b/s` : '') +
                    (d.eta_top_human ? ` (ETA: ${d.eta_top_human})` : '') + `</span>`;
            } else {
                // Normal sync interpolation - use 80% of speed to be conservative
                const speed = (d.speed || 0) * 0.8;
                const estHeight = Math.min((d.height || 0) + (speed * elapsed), headers);
                const pct = headers > 0 ? (estHeight / headers) * 100 : 0;

                document.getElementById('barTop').style.width = pct + '%';
                document.getElementById('labelTop').textContent = fmt(Math.floor(estHeight)) + ' (' + pct.toFixed(2) + '%)';
            }
        }

        function updateCpu() {
            fetch('/api/cpu').then(r => r.json()).then(d => {
                if (d.cpu_total_used !== undefined) {
                    document.getElementById('cpuUsage').textContent = d.cpu_total_used.toFixed(1) + '%';
                }
                if (d.cpu_model) {
                    document.getElementById('cpuModel').textContent = d.cpu_model + ' (' + (d.cpu_cores || '?') + ' cores)';
                }
                renderCpuBars(d.cpu_per_core);
                if (d.mem_percent !== undefined) {
                    document.getElementById('memUsage').textContent = d.mem_percent.toFixed(0) + '%';
                    document.getElementById('memDetails').textContent = (d.mem_used_human||'-') + ' / ' + (d.mem_total_human||'-');
                }
                if (d.uptime_human) {
                    document.getElementById('uptime').textContent = d.uptime_human;
                }
            }).catch(() => {});
        }

        function renderCpuBars(cores) {
            if (!cores) return;
            let html = '';
            for (let c of cores) {
                let total = (c.user||0) + (c.system||0) + (c.iowait||0);
                html += `<div class="cpu-bar-row">
                    <span class="cpu-bar-label">Core ${c.core}</span>
                    <div class="cpu-bar-bg">
                        <div class="cpu-bar-user" style="width:${c.user||0}%"></div>
                        <div class="cpu-bar-system" style="width:${c.system||0}%"></div>
                        <div class="cpu-bar-iowait" style="width:${c.iowait||0}%"></div>
                    </div>
                    <span class="cpu-bar-val">${total.toFixed(0)}%</span>
                </div>`;
            }
            document.getElementById('cpuBars').innerHTML = html;
        }

        function update() {
            fetch('/api/stats').then(r => r.json()).then(d => {
                if (d.error) {
                    document.getElementById('statusText').textContent = 'Error';
                    document.getElementById('statusDot').className = 'status-dot status-error';
                    return;
                }

                // Don't update progress bar until mode is confirmed
                if (!d.mode_confirmed) {
                    document.getElementById('statusText').textContent = 'Detecting mode...';
                    return;
                }

                const isAssume = d.assumeutxo;
                const headers = d.headers || 0;
                const currentMode = isAssume ? 'assumeutxo' : 'normal';

                // Reset bars if mode changed
                if (lastKnownMode !== null && lastKnownMode !== currentMode) {
                    resetBars();
                }
                lastKnownMode = currentMode;

                // Mode indicator
                document.getElementById('modeIndicator').textContent = isAssume ? 'AssumeUTXO' : 'Normal Sync';
                document.getElementById('modeIndicator').className = 'mode-indicator ' + (isAssume ? 'mode-assumeutxo' : 'mode-normal');

                // Show/hide assumeutxo elements
                document.getElementById('snapshotLabel').style.display = isAssume ? 'inline' : 'none';
                document.getElementById('snapshotMarker').style.display = isAssume ? 'block' : 'none';
                document.getElementById('legend').style.display = isAssume ? 'flex' : 'none';

                if (isAssume) {
                    // AssumeUTXO mode - dual progress
                    const snapshot = d.snapshot_height || 840000;
                    const bottom = d.bottom_height || 0;
                    const top = d.top_height || snapshot;

                    const snapshotPct = (snapshot / headers) * 100;
                    const bottomPct = (bottom / headers) * 100;
                    const topPct = (top / headers) * 100;

                    // Bottom bar (validation from 0)
                    document.getElementById('barBottom').style.width = bottomPct + '%';

                    // Top bar (from snapshot to current)
                    document.getElementById('barTop').style.left = snapshotPct + '%';
                    document.getElementById('barTop').style.width = (topPct - snapshotPct) + '%';

                    // Snapshot marker
                    document.getElementById('snapshotMarker').style.left = snapshotPct + '%';

                    // Labels
                    document.getElementById('labelBottom').textContent = fmt(bottom);
                    document.getElementById('labelTop').textContent = fmt(top);
                    document.getElementById('tipLabel').textContent = 'Tip: ' + fmt(headers);

                    // Stats
                    const bottomRemain = snapshot - bottom;
                    const topRemain = headers - top;
                    document.getElementById('bottomStats').innerHTML =
                        `Block ${fmt(bottom)} / ${fmt(snapshot)}<br>` +
                        `<span style="font-size:0.7em">${fmt(bottomRemain)} remaining` +
                        (d.speed_bottom ? ` @ ${Math.max(0, d.speed_bottom).toFixed(1)} b/s` : '') +
                        (d.eta_bottom_human ? ` (ETA: ${d.eta_bottom_human})` : '') + `</span>`;

                    document.getElementById('topStats').innerHTML =
                        `Block ${fmt(top)} / ${fmt(headers)}<br>` +
                        `<span style="font-size:0.7em">${fmt(topRemain)} remaining` +
                        (d.speed_top ? ` @ ${Math.max(0, d.speed_top).toFixed(1)} b/s` : '') +
                        (d.eta_top_human ? ` (ETA: ${d.eta_top_human})` : '') + `</span>`;

                    document.getElementById('progressStats').style.display = 'grid';

                    // Status
                    const synced = (top >= headers) && (bottom >= snapshot);
                    document.getElementById('statusText').textContent = synced ? 'Synced' : 'Syncing...';
                    document.getElementById('statusDot').className = 'status-dot ' + (synced ? 'status-synced' : 'status-syncing');

                } else {
                    // Normal sync mode
                    const height = d.height || 0;
                    const pct = headers > 0 ? (height / headers) * 100 : 0;

                    document.getElementById('barBottom').style.width = '0%';
                    document.getElementById('barTop').style.left = '0%';
                    document.getElementById('barTop').style.width = pct + '%';

                    document.getElementById('labelBottom').textContent = '';
                    document.getElementById('labelTop').textContent = fmt(height) + ' (' + pct.toFixed(2) + '%)';
                    document.getElementById('tipLabel').textContent = 'Tip: ' + fmt(headers);

                    document.getElementById('progressStats').style.display = 'none';

                    document.getElementById('statusText').textContent = d.ibd ? 'Syncing...' : 'Synced';
                    document.getElementById('statusDot').className = 'status-dot ' + (d.ibd ? 'status-syncing' : 'status-synced');
                }

                // Common stats

                document.getElementById('connections').textContent = d.connections || 0;
                document.getElementById('connSub').textContent = (d.connections_in||0) + ' in / ' + (d.connections_out||0) + ' out';

                document.getElementById('diskUsage').textContent = d.size_on_disk_human || '-';
                document.getElementById('diskSub').textContent = d.pruned ? 'pruned' : 'full node';

                document.getElementById('version').textContent = (d.version || '-').replace(/\\//g, '');

                // System stats
                if (d.cpu_total_used !== undefined) {
                    document.getElementById('cpuUsage').textContent = d.cpu_total_used.toFixed(1) + '%';
                }
                document.getElementById('cpuModel').textContent = (d.cpu_model || '-') + ' (' + (d.cpu_cores || '?') + ' cores)';
                renderCpuBars(d.cpu_per_core);

                if (d.mem_percent !== undefined) {
                    document.getElementById('memUsage').textContent = d.mem_percent.toFixed(0) + '%';
                    document.getElementById('memDetails').textContent = (d.mem_used_human||'-') + ' / ' + (d.mem_total_human||'-');
                }

                document.getElementById('uptime').textContent = d.uptime_human || '-';
                document.getElementById('lastUpdate').textContent = 'Last updated: ' + new Date().toLocaleTimeString();

                // Store for interpolation
                lastData = d;
                lastFetchTime = Date.now();

            }).catch(e => {
                document.getElementById('statusText').textContent = 'Connection Error';
            });
        }

        // Fast initial load - CPU/system stats first
        updateCpu();
        document.getElementById('statusText').textContent = 'Loading blockchain...';

        // Then load blockchain data (slow)
        update();

        setInterval(update, 60000);  // Fetch real data every 60s
        setInterval(interpolate, 1000);  // Smooth block updates every 1s
        setInterval(updateCpu, 5000);  // CPU/memory updates every 5s
    </script>
</body>
</html>'''


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/' or path == '/index.html':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

        elif path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            stats = get_bitcoin_stats()
            if stats:
                stats = calculate_speed(stats)
                stats['size_on_disk_human'] = format_bytes(stats.get('size_on_disk', 0))
                sys_stats = get_system_stats()
                stats.update(sys_stats)
                if 'mem_total' in stats:
                    stats['mem_total_human'] = format_bytes(stats['mem_total'])
                    stats['mem_used_human'] = format_bytes(stats['mem_used'])
            else:
                stats = {'error': 'Could not fetch stats'}

            self.wfile.write(json.dumps(stats).encode())

        elif path == '/api/cpu':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            stats = get_system_stats()
            if 'mem_total' in stats:
                stats['mem_total_human'] = format_bytes(stats['mem_total'])
                stats['mem_used_human'] = format_bytes(stats['mem_used'])
            self.wfile.write(json.dumps(stats).encode())

        elif path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')

        else:
            self.send_response(404)
            self.end_headers()


def main():
    parser = argparse.ArgumentParser(description='Bitcoin Sync Dashboard')
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--ssh-user', default=os.environ.get('USER', 'root'))
    parser.add_argument('--port', type=int, default=8890)
    parser.add_argument('--docker', metavar='CONTAINER')
    args = parser.parse_args()

    CONFIG['host'] = args.host
    CONFIG['ssh_user'] = args.ssh_user
    CONFIG['port'] = args.port
    CONFIG['docker_container'] = args.docker

    print(f"Bitcoin Sync Dashboard")
    print(f"Node: {CONFIG['host']}")
    if CONFIG['docker_container']:
        print(f"Docker: {CONFIG['docker_container']}")
    print(f"Dashboard: http://0.0.0.0:{CONFIG['port']}")

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    with Server(("0.0.0.0", CONFIG['port']), DashboardHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")


if __name__ == '__main__':
    main()
