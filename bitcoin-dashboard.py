#!/usr/bin/env python3
"""
Bitcoin Knots Sync Dashboard with AssumeUTXO Support

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
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# SSH connection pooling (optional, only if paramiko available)
try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    paramiko = None

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

# Extended history for chart (keep last 30 minutes at 1-minute intervals)
chart_history = deque(maxlen=30)

# Knots peer preference settings (enabled by default)
knots_pref = {
    'enabled': True,
    'min_connections': 8,
    'last_disconnect': 0,
    'cooldown': 300,  # 5 minutes between disconnects
    'disconnected_count': 0,
    'knots_count': 0,
    'core_count': 0,
}

prev_cpu_stats = {'timestamp': 0, 'total': None, 'per_core': None}
last_cpu_result = {'cpu_total_used': 0, 'cpu_user': 0, 'cpu_system': 0, 'cpu_iowait': 0, 'cpu_idle': 100, 'cpu_per_core': []}

# Mode tracking - only switch modes after seeing consistent results
mode_tracker = {'current_mode': None, 'consecutive_count': 0, 'confirmed_mode': None}

# Cache for static info that doesn't change
static_cache = {'version': None, 'pruned': None, 'cpu_model': None, 'cpu_cores': None}

# Time-based cache for slow-changing values
timed_cache = {
    'size_on_disk': {'value': 0, 'pruned': None, 'last_update': 0, 'ttl': 600},  # 10 minutes - disk size grows slowly
    'connections': {'value': 0, 'in': 0, 'out': 0, 'last_update': 0, 'ttl': 60},  # 1 minute
    'peers': {'value': [], 'count': 0, 'knots': 0, 'core': 0, 'last_update': 0, 'ttl': 45},  # 45 seconds
    'blockchain': {'data': None, 'last_update': 0, 'ttl': 30},  # 30 seconds - getblockchaininfo is VERY slow during sync
    'latest_block': {'data': {}, 'last_update': 0, 'ttl': 60},  # 60 seconds
    'mempool': {'data': {}, 'last_update': 0, 'ttl': 30},  # 30 seconds
}

# Disk I/O tracking
prev_disk_stats = {'timestamp': 0, 'read_bytes': 0, 'write_bytes': 0}
last_disk_result = {'disk_read_speed': 0, 'disk_write_speed': 0}

# Network I/O tracking
prev_net_stats = {'timestamp': 0, 'rx_bytes': 0, 'tx_bytes': 0}
last_net_result = {'net_rx_speed': 0, 'net_tx_speed': 0}

# Bitcoin network tracking
prev_btc_net = {'timestamp': 0, 'totalbytesrecv': 0, 'totalbytessent': 0}
last_btc_net_result = {'btc_download_speed': 0, 'btc_upload_speed': 0}

# Thread pool for parallel RPC calls (max 6 concurrent calls)
rpc_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix='rpc')

# Lock for thread-safe cache access
cache_lock = threading.Lock()


# SSH Connection Pool for persistent connections (reduces latency by 100-200ms per call)
class SSHConnectionPool:
    """Manages persistent SSH connections with automatic reconnection"""

    def __init__(self, host, user, max_connections=3):
        self.host = host
        self.user = user
        self.max_connections = max_connections
        self.connections = []
        self.lock = threading.Lock()
        self.enabled = PARAMIKO_AVAILABLE and host not in ['localhost', '127.0.0.1', '0.0.0.0']

    def get_connection(self):
        """Get an available SSH connection, creating if needed"""
        if not self.enabled:
            return None

        with self.lock:
            # Try to reuse existing connection
            for conn in self.connections:
                if conn['in_use'] == False and conn['client'].get_transport() and conn['client'].get_transport().is_active():
                    conn['in_use'] = True
                    return conn

            # Create new connection if under limit
            if len(self.connections) < self.max_connections:
                try:
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(self.host, username=self.user, timeout=5)
                    conn = {'client': client, 'in_use': True}
                    self.connections.append(conn)
                    return conn
                except Exception as e:
                    print(f"SSH connection failed: {e}")
                    return None

            # All connections busy, wait for one (fallback to subprocess)
            return None

    def release_connection(self, conn):
        """Mark connection as available for reuse"""
        if conn and self.enabled:
            with self.lock:
                conn['in_use'] = False

    def execute(self, cmd, timeout=15):
        """Execute command on SSH connection"""
        conn = self.get_connection()
        if not conn:
            return None  # Fallback to subprocess

        try:
            stdin, stdout, stderr = conn['client'].exec_command(cmd, timeout=timeout)
            output = stdout.read().decode('utf-8').strip()
            exit_code = stdout.channel.recv_exit_status()
            self.release_connection(conn)

            if exit_code == 0:
                return output
            return None
        except Exception as e:
            # Connection failed, remove it from pool
            with self.lock:
                if conn in self.connections:
                    self.connections.remove(conn)
                try:
                    conn['client'].close()
                except:
                    pass
            return None

    def close_all(self):
        """Close all SSH connections"""
        with self.lock:
            for conn in self.connections:
                try:
                    conn['client'].close()
                except:
                    pass
            self.connections.clear()


# Global SSH connection pool (initialized after CONFIG is set)
ssh_pool = None


# FIX #5: Cache expiration helper - clear stale data on backend failure
def clear_expired_cache(cache_name, max_age=300):
    """Clear cache if data is older than max_age seconds (default 5 minutes)"""
    if cache_name not in timed_cache:
        return
    cache = timed_cache[cache_name]
    now = time.time()
    age = now - cache['last_update']
    # If data is older than max_age, clear it
    if age > max_age:
        if 'data' in cache:
            cache['data'] = None
        if 'value' in cache:
            cache['value'] = 0
        cache['last_update'] = 0
        print(f"Cleared stale cache '{cache_name}' (age: {age:.0f}s)")


def run_command(cmd, timeout=15):
    global ssh_pool

    try:
        if CONFIG['host'] in ['localhost', '127.0.0.1', '0.0.0.0']:
            # Local execution
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        else:
            # Remote execution - try SSH pool first (100-200ms faster)
            if ssh_pool and ssh_pool.enabled:
                output = ssh_pool.execute(cmd, timeout)
                if output is not None:
                    return output
                # Pool failed, fall through to subprocess

            # Fallback to subprocess SSH (creates new connection each time)
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


def run_bitcoin_cli_parallel(commands):
    """
    Execute multiple bitcoin-cli commands in parallel using thread pool.

    Args:
        commands: dict of {key: rpc_command} pairs

    Returns:
        dict of {key: result} pairs

    Example:
        results = run_bitcoin_cli_parallel({
            'chainstate': 'getchainstates',
            'network': 'getnetworkinfo',
            'mempool': 'getmempoolinfo'
        })
        # Returns: {'chainstate': '...', 'network': '...', 'mempool': '...'}
    """
    results = {}
    futures = {}

    # Submit all commands to thread pool
    for key, cmd in commands.items():
        future = rpc_executor.submit(run_bitcoin_cli, cmd)
        futures[future] = key

    # Collect results as they complete
    for future in as_completed(futures):
        key = futures[future]
        try:
            results[key] = future.result()
        except Exception as e:
            print(f"RPC call '{key}' failed: {e}")
            results[key] = None

    return results


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


def get_disk_io():
    global prev_disk_stats, last_disk_result
    stats = {}
    current_time = time.time()

    # Read disk stats from /proc/diskstats
    diskstats = run_command("cat /proc/diskstats")
    if not diskstats:
        return last_disk_result.copy()

    total_read = 0
    total_write = 0
    for line in diskstats.split('\n'):
        parts = line.split()
        if len(parts) >= 14:
            device = parts[2]
            # Only count main devices (sda, nvme0n1, etc), not partitions
            if device.startswith('sd') and device[-1].isalpha():
                total_read += int(parts[5]) * 512  # sectors read * 512 bytes
                total_write += int(parts[9]) * 512  # sectors written * 512 bytes
            elif device.startswith('nvme') and device.endswith('n1'):
                total_read += int(parts[5]) * 512
                total_write += int(parts[9]) * 512

    if prev_disk_stats['timestamp'] > 0:
        time_diff = current_time - prev_disk_stats['timestamp']
        if time_diff > 0:
            stats['disk_read_speed'] = (total_read - prev_disk_stats['read_bytes']) / time_diff
            stats['disk_write_speed'] = (total_write - prev_disk_stats['write_bytes']) / time_diff
            last_disk_result.update(stats)

    prev_disk_stats = {'timestamp': current_time, 'read_bytes': total_read, 'write_bytes': total_write}

    if not stats:
        stats = last_disk_result.copy()
    return stats


def get_network_io():
    global prev_net_stats, last_net_result
    stats = {}
    current_time = time.time()

    # Read network stats
    netdev = run_command("cat /proc/net/dev")
    if not netdev:
        return last_net_result.copy()

    total_rx = 0
    total_tx = 0
    for line in netdev.split('\n'):
        if ':' in line:
            parts = line.split(':')
            iface = parts[0].strip()
            # Skip loopback
            if iface == 'lo':
                continue
            values = parts[1].split()
            if len(values) >= 9:
                total_rx += int(values[0])  # bytes received
                total_tx += int(values[8])  # bytes transmitted

    if prev_net_stats['timestamp'] > 0:
        time_diff = current_time - prev_net_stats['timestamp']
        if time_diff > 0:
            stats['net_rx_speed'] = (total_rx - prev_net_stats['rx_bytes']) / time_diff
            stats['net_tx_speed'] = (total_tx - prev_net_stats['tx_bytes']) / time_diff
            last_net_result.update(stats)

    prev_net_stats = {'timestamp': current_time, 'rx_bytes': total_rx, 'tx_bytes': total_tx}

    if not stats:
        stats = last_net_result.copy()
    return stats


def get_btc_network_speed():
    global prev_btc_net, last_btc_net_result
    stats = {}
    current_time = time.time()

    nettotals = run_bitcoin_cli("getnettotals")
    if not nettotals:
        return last_btc_net_result.copy()

    try:
        data = json.loads(nettotals)
        total_recv = data.get('totalbytesrecv', 0)
        total_sent = data.get('totalbytessent', 0)

        if prev_btc_net['timestamp'] > 0:
            time_diff = current_time - prev_btc_net['timestamp']
            if time_diff > 0:
                stats['btc_download_speed'] = (total_recv - prev_btc_net['totalbytesrecv']) / time_diff
                stats['btc_upload_speed'] = (total_sent - prev_btc_net['totalbytessent']) / time_diff
                last_btc_net_result.update(stats)

        prev_btc_net = {'timestamp': current_time, 'totalbytesrecv': total_recv, 'totalbytessent': total_sent}
    except json.JSONDecodeError:
        pass

    if not stats:
        stats = last_btc_net_result.copy()
    return stats


def get_mempool_stats():
    # Check cache first
    now = time.time()
    cache = timed_cache['mempool']
    if now - cache['last_update'] < cache['ttl'] and cache['data']:
        return cache['data'].copy()

    stats = {}
    mempool = run_bitcoin_cli("getmempoolinfo")
    if not mempool:
        # FIX #5: Clear expired mempool cache on RPC failure
        clear_expired_cache('mempool', max_age=180)
        return stats

    if mempool:
        try:
            data = json.loads(mempool)
            stats['mempool_size'] = data.get('size', 0)
            stats['mempool_bytes'] = data.get('bytes', 0)
            stats['mempool_usage'] = data.get('usage', 0)
            stats['mempool_maxmempool'] = data.get('maxmempool', 300000000)
            stats['mempool_minfee'] = data.get('mempoolminfee', 0)
            # Knots-specific fields
            stats['mempool_unbroadcast'] = data.get('unbroadcastcount', 0)
            stats['mempool_fullrbf'] = data.get('fullrbf', None)
            stats['mempool_incrementalrelayfee'] = data.get('incrementalrelayfee', 0)
            # Update cache
            cache['data'] = stats.copy()
            cache['last_update'] = now
        except json.JSONDecodeError:
            pass
    return stats


def get_temperature():
    stats = {}
    # Try thermal zones (most Linux systems)
    temp_output = run_command("cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -1")
    if temp_output:
        try:
            # Value is in millidegrees
            stats['cpu_temp'] = int(temp_output) / 1000.0
        except ValueError:
            pass
    # Try sensors command as fallback
    if 'cpu_temp' not in stats:
        sensors_output = run_command("sensors 2>/dev/null | grep -i 'core 0' | head -1")
        if sensors_output and '+' in sensors_output:
            try:
                temp_str = sensors_output.split('+')[1].split('°')[0]
                stats['cpu_temp'] = float(temp_str)
            except (ValueError, IndexError):
                pass
    # Try to get NVMe/SSD temp
    nvme_temp = run_command("cat /sys/class/nvme/nvme*/hwmon*/temp1_input 2>/dev/null | head -1")
    if nvme_temp:
        try:
            stats['nvme_temp'] = int(nvme_temp) / 1000.0
        except ValueError:
            pass
    return stats


def get_peer_info():
    # Check cache first - getpeerinfo is SLOW during sync (2+ seconds)
    now = time.time()
    cache = timed_cache['peers']
    if now - cache['last_update'] < cache['ttl']:
        # Return cached data
        return {
            'peers': cache['value'],
            'peer_count': cache['count'],
            'knots_peers': cache['knots'],
            'core_peers': cache['core']
        }

    stats = {'peers': []}
    peerinfo = run_bitcoin_cli("getpeerinfo")
    if peerinfo:
        try:
            data = json.loads(peerinfo)
            knots_peers = []
            core_peers = []
            other_peers = []

            for peer in data:
                peer_data = {
                    'addr': peer.get('addr', ''),
                    'subver': peer.get('subver', ''),
                    'inbound': peer.get('inbound', False),
                    'synced_headers': peer.get('synced_headers', 0),
                    'synced_blocks': peer.get('synced_blocks', 0),
                }
                # Categorize by client
                subver = peer.get('subver', '').lower()
                if 'knots' in subver:
                    knots_peers.append(peer_data)
                elif 'satoshi' in subver:
                    core_peers.append(peer_data)
                else:
                    other_peers.append(peer_data)

            # Sort: Knots first, then Core, then others
            all_peers = knots_peers + core_peers + other_peers
            stats['peers'] = all_peers[:20]  # Limit to 20 for API
            stats['peer_count'] = len(data)
            stats['knots_peers'] = len(knots_peers)
            stats['core_peers'] = len(core_peers)

            # Update cache
            cache['value'] = stats['peers']
            cache['count'] = stats['peer_count']
            cache['knots'] = stats['knots_peers']
            cache['core'] = stats['core_peers']
            cache['last_update'] = now

            # Update knots_pref stats
            knots_pref['knots_count'] = len(knots_peers)
            knots_pref['core_count'] = len(core_peers)

        except json.JSONDecodeError:
            pass
    return stats


def enforce_knots_preference():
    """If enabled, disconnect Core peers to favor Knots connections. Aggressively drop v30+."""
    global knots_pref

    if not knots_pref['enabled']:
        return {'action': 'disabled'}

    current_time = time.time()

    # Get current peer info
    peerinfo = run_bitcoin_cli("getpeerinfo")
    if not peerinfo:
        return {'action': 'error', 'message': 'Could not get peer info'}

    try:
        data = json.loads(peerinfo)
    except json.JSONDecodeError:
        return {'action': 'error', 'message': 'Invalid peer data'}

    total_peers = len(data)

    # Categorize peers
    v30_peers = []
    other_core_peers = []
    knots_count = 0

    for peer in data:
        subver = peer.get('subver', '')
        subver_lower = subver.lower()
        if 'knots' in subver_lower:
            knots_count += 1
            continue
        # Check for v30+ (Satoshi:30, Satoshi:31, etc.)
        if 'satoshi:30' in subver_lower or 'satoshi:31' in subver_lower or 'satoshi:32' in subver_lower:
            v30_peers.append(peer)
        elif 'satoshi' in subver_lower:
            other_core_peers.append(peer)

    # AGGRESSIVE: Always drop v30+ immediately, no cooldown, even below min connections
    # Only keep v30 if it's literally our only connection
    if v30_peers and total_peers > 1:
        peer = v30_peers[0]
        addr = peer.get('addr', '')
        run_bitcoin_cli(f'disconnectnode "{addr}"')
        knots_pref['disconnected_count'] += 1
        return {
            'action': 'disconnected_v30',
            'addr': addr,
            'subver': peer.get('subver', ''),
            'total_disconnected': knots_pref['disconnected_count'],
            'reason': 'v30+ aggressive drop'
        }

    # For other Core peers, use normal cooldown
    if current_time - knots_pref['last_disconnect'] < knots_pref['cooldown']:
        remaining = int(knots_pref['cooldown'] - (current_time - knots_pref['last_disconnect']))
        return {'action': 'cooldown', 'remaining': remaining, 'v30_count': len(v30_peers)}

    # Don't disconnect other Core if below minimum
    if total_peers <= knots_pref['min_connections']:
        return {'action': 'skipped', 'reason': f'Only {total_peers} connections (min: {knots_pref["min_connections"]})'}

    # Find a non-v30 Core peer to disconnect
    if not other_core_peers:
        return {'action': 'skipped', 'reason': 'No Core peers to disconnect (only Knots left)'}

    # Prefer dropping outbound peers
    core_peer_to_drop = None
    for peer in other_core_peers:
        if not peer.get('inbound', False):
            core_peer_to_drop = peer
            break
    if not core_peer_to_drop:
        core_peer_to_drop = other_core_peers[0]

    # Disconnect the peer
    addr = core_peer_to_drop.get('addr', '')
    run_bitcoin_cli(f'disconnectnode "{addr}"')

    knots_pref['last_disconnect'] = current_time
    knots_pref['disconnected_count'] += 1

    return {
        'action': 'disconnected',
        'addr': addr,
        'subver': core_peer_to_drop.get('subver', ''),
        'total_disconnected': knots_pref['disconnected_count']
    }


def get_disk_estimate():
    stats = {}
    # Full unpruned blockchain is ~600-650 GB as of 2025, growing ~60 GB/year
    # Block sizes: early blocks ~100KB, modern blocks ~2-3 MB

    # First get basic blockchain info
    info = run_bitcoin_cli("getblockchaininfo")
    if not info:
        return stats

    try:
        data = json.loads(info)
        current_size = data.get('size_on_disk', 0)
        current_blocks = data.get('blocks', 0)
        headers = data.get('headers', 0)
        pruned = data.get('pruned', False)

        if pruned:
            stats['disk_estimate_note'] = 'Pruned mode - only recent blocks stored'
            return stats

        # Check if AssumeUTXO mode (dual sync)
        chainstates_raw = run_bitcoin_cli("getchainstates")
        is_assumeutxo = False
        bottom_height = 0
        top_height = current_blocks
        snapshot_height = 840000

        if chainstates_raw:
            try:
                cs_data = json.loads(chainstates_raw)
                states = cs_data.get('chainstates', [])
                if len(states) == 2:
                    is_assumeutxo = True
                    for state in states:
                        if state.get('snapshot_blockhash'):
                            top_height = state.get('blocks', 0)
                        else:
                            bottom_height = state.get('blocks', 0)
            except:
                pass

        # Estimate total blockchain size when complete
        # Use empirical data: ~600-650 GB for ~930k blocks in 2025
        estimated_full_size = 650 * 1024 * 1024 * 1024  # 650 GB in bytes

        if is_assumeutxo and not pruned:
            # AssumeUTXO dual sync calculation
            # Need space for: ALL blocks from 0 to headers
            # Currently have: some early blocks (0→bottom) + recent blocks (snapshot→top)

            # Estimate what percentage of full blockchain we have
            # This is approximate since block sizes vary
            blocks_validated_bottom = bottom_height  # 0 → bottom
            blocks_downloaded_top = top_height - snapshot_height  # snapshot → top
            blocks_have_approx = blocks_validated_bottom + blocks_downloaded_top

            # Percentage complete (rough estimate)
            pct_complete = blocks_have_approx / headers if headers > 0 else 0

            # Estimated total needed
            estimated_total = estimated_full_size
            estimated_remaining = max(0, estimated_total - current_size)

            stats['disk_needed_bytes'] = int(estimated_remaining)
            stats['disk_needed_human'] = format_bytes(estimated_remaining)
            stats['disk_total_estimate'] = estimated_total
            stats['disk_estimate_note'] = f'AssumeUTXO: {pct_complete*100:.0f}% complete'

        else:
            # Normal sync or post-AssumeUTXO
            if headers > current_blocks:
                # Still syncing
                pct_complete = current_blocks / headers if headers > 0 else 0
                estimated_total = estimated_full_size
                estimated_remaining = max(0, estimated_total - current_size)

                stats['disk_needed_bytes'] = int(estimated_remaining)
                stats['disk_needed_human'] = format_bytes(estimated_remaining)
                stats['disk_total_estimate'] = estimated_total
            else:
                # Fully synced
                stats['disk_needed_bytes'] = 0
                stats['disk_needed_human'] = '0 B'
                stats['disk_total_estimate'] = current_size

    except json.JSONDecodeError:
        pass

    return stats


def get_latest_block():
    # Check cache first
    now = time.time()
    cache = timed_cache['latest_block']
    if now - cache['last_update'] < cache['ttl'] and cache['data']:
        return cache['data'].copy()

    stats = {}
    # Get best block hash
    blockhash = run_bitcoin_cli("getbestblockhash")
    if not blockhash:
        # FIX #5: Clear expired latest_block cache on RPC failure
        clear_expired_cache('latest_block', max_age=180)
        return stats

    if blockhash:
        blockinfo = run_bitcoin_cli(f"getblock {blockhash}")
        if blockinfo:
            try:
                data = json.loads(blockinfo)
                stats['latest_block_height'] = data.get('height', 0)
                stats['latest_block_time'] = data.get('time', 0)
                stats['latest_block_txcount'] = data.get('nTx', 0)
                stats['latest_block_size'] = data.get('size', 0)
                # Calculate time since block
                if stats['latest_block_time']:
                    stats['latest_block_age'] = int(time.time()) - stats['latest_block_time']
                # Update cache
                cache['data'] = stats.copy()
                cache['last_update'] = now
            except json.JSONDecodeError:
                pass
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

    # ASYNC OPTIMIZATION: Run independent RPC calls in parallel
    now = time.time()
    blockchain_cache = timed_cache['blockchain']
    need_blockchain = now - blockchain_cache['last_update'] >= blockchain_cache['ttl'] or not blockchain_cache['data']
    need_network = not static_cache['version']

    # Build parallel RPC call list
    parallel_calls = {'chainstates': 'getchainstates'}
    if need_blockchain:
        parallel_calls['blockchain'] = 'getblockchaininfo'
    if need_network:
        parallel_calls['network'] = 'getnetworkinfo'

    # Execute all calls in parallel
    rpc_results = run_bitcoin_cli_parallel(parallel_calls)

    # Process chainstates result
    chainstates = rpc_results.get('chainstates')
    if not chainstates:
        # FIX #5: Clear expired blockchain cache on RPC failure
        clear_expired_cache('blockchain', max_age=300)

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

    # Process getblockchaininfo result (from parallel call or cache)
    blockchain_data = None

    if blockchain_cache['data'] and not need_blockchain:
        # Use cached data
        blockchain_data = blockchain_cache['data']
    elif 'blockchain' in rpc_results and rpc_results['blockchain']:
        # Use data from parallel call
        try:
            blockchain_data = json.loads(rpc_results['blockchain'])
            with cache_lock:
                blockchain_cache['data'] = blockchain_data
                blockchain_cache['last_update'] = now
        except json.JSONDecodeError:
            pass

    # Use blockchain data if available
    if blockchain_data:
        # OPTIMIZATION: Use separate size_on_disk cache with long TTL (10 min)
        # Check if we have cached size_on_disk that's still valid
        size_cache = timed_cache['size_on_disk']
        if now - size_cache['last_update'] < size_cache['ttl']:
            # Use cached size_on_disk (valid for 10 minutes)
            stats['size_on_disk'] = size_cache['value']
            stats['pruned'] = size_cache['pruned'] if size_cache['pruned'] is not None else False
        else:
            # Cache expired, extract from fresh blockchain data and update cache
            stats['size_on_disk'] = blockchain_data.get('size_on_disk', 0)
            stats['pruned'] = blockchain_data.get('pruned', False)
            with cache_lock:
                size_cache['value'] = stats['size_on_disk']
                size_cache['pruned'] = stats['pruned']
                size_cache['last_update'] = now

        # Store pruned in static cache for other code paths
        static_cache['pruned'] = stats['pruned']

        # Only use height/headers/progress in normal mode (assumeutxo gets these from getchainstates)
        if not stats.get('assumeutxo'):
            stats['height'] = blockchain_data.get('blocks', 0)
            stats['headers'] = blockchain_data.get('headers', 0)
            stats['progress'] = blockchain_data.get('verificationprogress', 0) * 100
            stats['ibd'] = blockchain_data.get('initialblockdownload', True)
            if detected_mode == 'unknown':
                detected_mode = 'normal'

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

    # Process network info (from parallel call or cache)
    if static_cache['version']:
        stats['version'] = static_cache['version']
        # Check if connections cache is still valid
        if now - timed_cache['connections']['last_update'] < timed_cache['connections']['ttl']:
            stats['connections'] = timed_cache['connections']['value']
            stats['connections_in'] = timed_cache['connections']['in']
            stats['connections_out'] = timed_cache['connections']['out']
        else:
            peerinfo = run_bitcoin_cli("getconnectioncount")
            if not peerinfo:
                # FIX #5: Clear expired connections cache on RPC failure
                clear_expired_cache('connections', max_age=180)
            elif peerinfo:
                try:
                    with cache_lock:
                        timed_cache['connections']['value'] = int(peerinfo)
                        timed_cache['connections']['last_update'] = now
                    stats['connections'] = timed_cache['connections']['value']
                    stats['connections_in'] = 0
                    stats['connections_out'] = stats['connections']
                except ValueError:
                    stats['connections'] = timed_cache['connections']['value']
    elif 'network' in rpc_results and rpc_results['network']:
        # Use data from parallel call
        try:
            data = json.loads(rpc_results['network'])
            static_cache['version'] = data.get('subversion', 'unknown')
            stats['version'] = static_cache['version']
            with cache_lock:
                timed_cache['connections']['value'] = data.get('connections', 0)
                timed_cache['connections']['in'] = data.get('connections_in', 0)
                timed_cache['connections']['out'] = data.get('connections_out', 0)
                timed_cache['connections']['last_update'] = now
            stats['connections'] = timed_cache['connections']['value']
            stats['connections_in'] = timed_cache['connections']['in']
            stats['connections_out'] = timed_cache['connections']['out']
        except json.JSONDecodeError:
            pass

    # Fallback: If blockchain_data was not available, try separate size_on_disk cache
    if 'size_on_disk' not in stats:
        size_cache = timed_cache['size_on_disk']
        if now - size_cache['last_update'] < size_cache['ttl']:
            # Use cached size_on_disk (valid for 10 minutes)
            stats['size_on_disk'] = size_cache['value']
            stats['pruned'] = size_cache['pruned'] if size_cache['pruned'] is not None else static_cache['pruned']
        # If even the long cache is expired and blockchain_data failed, size_on_disk will be missing
        # Frontend will handle this gracefully

    stats['timestamp'] = time.time()

    # Debug: Add cache age for size_on_disk (helps verify 10-minute caching works)
    if 'size_on_disk' in stats:
        size_cache_age = int(stats['timestamp'] - timed_cache['size_on_disk']['last_update'])
        stats['size_on_disk_cache_age_sec'] = size_cache_age

    return stats


def calculate_speed(stats):
    global chart_history
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

    # Store chart history (every minute)
    if stats.get('assumeutxo'):
        total_speed = max(0, stats.get('speed_top', 0)) + max(0, stats.get('speed_bottom', 0))
    else:
        total_speed = max(0, stats.get('speed', 0))

    # Only add to history if enough time passed (at least 30 seconds)
    if not chart_history or (current_time - chart_history[-1]['time'] >= 30):
        chart_history.append({'time': current_time, 'speed': total_speed})

    stats['chart_history'] = list(chart_history)

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


def format_speed(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    elif bps < 1024 * 1024:
        return f"{bps/1024:.1f} KB/s"
    else:
        return f"{bps/(1024*1024):.1f} MB/s"


DASHBOARD_HTML = '''<!DOCTYPE html>
<html>
<head>
    <title>Bitcoin Knots Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <!-- Version: 2.1.0 - Fixed interpolation and added fetch indicator -->
    <style>
        :root {
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-card: rgba(255,255,255,0.05);
            --bg-card-hover: rgba(255,255,255,0.08);
            --text-primary: #eee;
            --text-secondary: #888;
            --text-muted: #666;
            --border-color: rgba(255,255,255,0.1);
            --accent: #f7931a;
            --accent-glow: rgba(247, 147, 26, 0.3);
        }
        [data-theme="light"] {
            --bg-primary: #f5f5f5;
            --bg-secondary: #e8e8e8;
            --bg-card: rgba(0,0,0,0.05);
            --bg-card-hover: rgba(0,0,0,0.08);
            --text-primary: #222;
            --text-secondary: #555;
            --text-muted: #777;
            --border-color: rgba(0,0,0,0.1);
            --accent: #f7931a;
            --accent-glow: rgba(247, 147, 26, 0.2);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Tahoma, sans-serif;
            background: linear-gradient(135deg, var(--bg-primary) 0%, var(--bg-secondary) 100%);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        .header-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
        }
        .header-controls {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        h1 {
            text-align: center;
            flex: 1;
            color: var(--accent);
            text-shadow: 0 0 20px var(--accent-glow);
        }
        h2 { color: var(--text-secondary); margin-bottom: 20px; font-weight: normal; }
        .icon-btn {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 8px 12px;
            color: var(--text-primary);
            cursor: pointer;
            font-size: 1.1em;
            transition: background 0.2s;
        }
        .icon-btn:hover { background: var(--bg-card-hover); }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: var(--bg-card);
            border-radius: 15px;
            padding: 25px;
            border: 1px solid var(--border-color);
        }
        .card-title {
            font-size: 0.85em;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }
        .card-value { font-size: 1.8em; font-weight: bold; color: var(--text-primary); word-break: break-word; }
        .card-value-small { font-size: 1.1em; font-weight: bold; color: var(--text-primary); word-break: break-word; }
        .card-value-tiny { font-size: 0.9em; font-weight: bold; color: var(--text-primary); word-break: break-word; }
        .card-sub { font-size: 0.9em; color: var(--text-muted); margin-top: 5px; }

        .progress-container {
            background: var(--bg-card);
            border-radius: 15px;
            padding: 30px;
            margin-bottom: 30px;
            border: 1px solid var(--border-color);
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
        .version-tag {
            float: right;
            color: var(--text-secondary);
            font-size: 0.75em;
        }

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
            color: var(--text-secondary);
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
        [data-theme="light"] .progress-stat {
            background: rgba(0,0,0,0.05);
        }
        .progress-stat-label { font-size: 0.8em; color: var(--text-secondary); }
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
        .cpu-bar-label { width: 50px; color: var(--text-secondary); }
        .cpu-bar-bg { flex: 1; height: 10px; background: rgba(0,0,0,0.3); border-radius: 5px; overflow: hidden; display: flex; }
        [data-theme="light"] .cpu-bar-bg { background: rgba(0,0,0,0.15); }
        .cpu-bar-user { background: #4CAF50; height: 100%; }
        .cpu-bar-system { background: #2196F3; height: 100%; }
        .cpu-bar-iowait { background: #FF9800; height: 100%; }
        .cpu-bar-val { width: 40px; text-align: right; color: var(--text-secondary); }
        .cpu-legend { display: flex; gap: 15px; margin-top: 10px; font-size: 0.75em; color: var(--text-secondary); flex-wrap: wrap; }
        .cpu-legend-item { display: flex; align-items: center; gap: 5px; }
        .cpu-legend-color { width: 12px; height: 8px; border-radius: 2px; }
        .cpu-legend-user { background: #4CAF50; }
        .cpu-legend-system { background: #2196F3; }
        .cpu-legend-iowait { background: #FF9800; }

        .system-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 30px; }
        @media (max-width: 800px) { .system-grid { grid-template-columns: 1fr; } }
        .card-wide { grid-column: span 1; }
        .stats-compact { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .stat-item { padding: 12px; background: rgba(0,0,0,0.15); border-radius: 8px; }
        [data-theme="light"] .stat-item { background: rgba(0,0,0,0.05); }
        .stat-label { font-size: 0.7em; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-value { font-size: 1.1em; font-weight: bold; color: var(--text-primary); margin-top: 2px; }
        .stat-sub { font-size: 0.75em; color: var(--text-muted); }

        .footer { text-align: center; color: var(--text-muted); margin-top: 30px; font-size: 0.9em; }

        .fetch-indicator {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #666;
            border: 1px solid rgba(255,255,255,0.2);
            margin-right: 8px;
            opacity: 0.5;
            transition: all 0.3s ease;
        }
        .fetch-indicator.fetch-fresh {
            background: #4caf50;
            border-color: #4caf50;
            opacity: 1;
            animation: pulse-fetch 0.6s ease;
            box-shadow: 0 0 12px rgba(76, 175, 80, 0.8);
        }
        .fetch-indicator.fetch-error {
            background: #f44336;
            opacity: 1;
            box-shadow: 0 0 8px rgba(244, 67, 54, 0.6);
        }
        .fetch-indicator.fetch-aging {
            background: #ff9800;
            opacity: 0.7;
        }
        .fetch-indicator.fetch-stale {
            background: #f44336;
            opacity: 0.5;
        }
        @keyframes pulse-fetch {
            0% { transform: scale(0.8); opacity: 0.5; }
            50% { transform: scale(1.3); opacity: 1; }
            100% { transform: scale(1); opacity: 1; }
        }

        .peer-list {
            background: var(--bg-card);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 30px;
            max-height: 300px;
            overflow-y: auto;
            border: 1px solid var(--border-color);
        }
        .peer-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.85em;
        }
        .peer-row:last-child { border-bottom: none; }
        .peer-addr { color: var(--text-primary); font-family: monospace; font-size: 0.85em; }
        .peer-version { color: var(--text-secondary); font-size: 0.8em; flex: 1; text-align: center; margin: 0 10px; }
        .peer-direction {
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.75em;
            min-width: 50px;
            text-align: center;
        }
        .peer-inbound { background: #2196F3; color: white; }
        .peer-outbound { background: #4CAF50; color: white; }

        /* Chart styles */
        .chart-container {
            background: var(--bg-card);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 30px;
            border: 1px solid var(--border-color);
        }
        .chart-title {
            font-size: 0.85em;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 15px;
        }
        .chart-svg {
            width: 100%;
            height: 150px;
        }
        .chart-line { fill: none; stroke: var(--accent); stroke-width: 2; }
        .chart-area { fill: var(--accent); opacity: 0.15; }
        .chart-grid { stroke: var(--border-color); stroke-width: 1; }
        .chart-label { fill: var(--text-secondary); font-size: 10px; }

        /* Settings modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 100;
            justify-content: center;
            align-items: center;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: var(--bg-primary);
            border-radius: 15px;
            padding: 30px;
            max-width: 500px;
            width: 90%;
            border: 1px solid var(--border-color);
        }
        .modal h2 { margin-bottom: 20px; color: var(--text-primary); }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: var(--text-secondary); font-size: 0.9em; }
        .form-group input, .form-group select {
            width: 100%;
            padding: 10px;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            background: #2a2a3e;
            color: #eee;
            font-size: 1em;
        }
        .form-group select option {
            background: #2a2a3e;
            color: #eee;
        }
        [data-theme="light"] .form-group input,
        [data-theme="light"] .form-group select {
            background: #fff;
            color: #222;
        }
        [data-theme="light"] .form-group select option {
            background: #fff;
            color: #222;
        }
        .form-row { display: flex; gap: 15px; }
        .form-row .form-group { flex: 1; }
        .btn {
            padding: 10px 20px;
            border-radius: 8px;
            border: none;
            cursor: pointer;
            font-size: 1em;
            transition: opacity 0.2s;
        }
        .btn:hover { opacity: 0.8; }
        .btn-primary { background: var(--accent); color: white; }
        .btn-secondary { background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-color); }
        .modal-buttons { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-row">
            <div style="width: 40px;"></div>
            <h1>&#8383; Bitcoin Knots Dashboard</h1>
            <div class="header-controls">
                <button class="icon-btn" onclick="openSettings()" title="Settings">&#9881;</button>
            </div>
        </div>

        <div class="progress-container">
            <span class="mode-indicator mode-normal" id="modeIndicator">Normal Sync</span>
            <span style="margin-left: 15px;"><span class="status-dot status-syncing" id="statusDot"></span><span id="statusText">Syncing...</span></span>
            <span class="version-tag" id="version">-</span>

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
                <div class="card-title">Download Speed</div>
                <div class="card-value" id="btcDownload">-</div>
                <div class="card-sub" id="btcDownloadSub">blockchain sync</div>
            </div>
            <div class="card">
                <div class="card-title">Disk Usage</div>
                <div class="card-value" id="diskUsage">-</div>
                <div class="card-sub" id="diskSub">-</div>
            </div>
            <div class="card">
                <div class="card-title">Disk Needed</div>
                <div class="card-value" id="diskNeeded">-</div>
                <div class="card-sub" id="diskNeededSub">estimated remaining</div>
            </div>
        </div>

        <h2>Mempool</h2>
        <div class="grid">
            <div class="card">
                <div class="card-title">Transactions</div>
                <div class="card-value" id="mempoolTx">-</div>
                <div class="card-sub" id="mempoolTxSub">pending transactions</div>
            </div>
            <div class="card">
                <div class="card-title">Mempool Size</div>
                <div class="card-value" id="mempoolSize">-</div>
                <div class="card-sub" id="mempoolSizeSub">-</div>
            </div>
            <div class="card">
                <div class="card-title">Min Fee</div>
                <div class="card-value" id="mempoolFee">-</div>
                <div class="card-sub" id="mempoolFeeSub">sat/vB to enter</div>
            </div>
            <div class="card">
                <div class="card-title">Policy</div>
                <div class="card-value-small" id="mempoolPolicy">-</div>
                <div class="card-sub" id="mempoolPolicySub">mempool policy</div>
            </div>
        </div>

        <h2>System Resources</h2>
        <div class="system-grid">
            <div class="card card-wide">
                <div class="card-title">CPU <span id="cpuUsage" style="float:right;font-size:1.2em;">-</span></div>
                <div class="card-sub" id="cpuModel" style="margin-bottom:10px;">-</div>
                <div class="cpu-bars" id="cpuBars"></div>
                <div class="cpu-legend">
                    <div class="cpu-legend-item"><div class="cpu-legend-color cpu-legend-user"></div> User</div>
                    <div class="cpu-legend-item"><div class="cpu-legend-color cpu-legend-system"></div> System</div>
                    <div class="cpu-legend-item"><div class="cpu-legend-color cpu-legend-iowait"></div> I/O Wait</div>
                </div>
            </div>
            <div class="card">
                <div class="card-title">System</div>
                <div class="stats-compact">
                    <div class="stat-item">
                        <div class="stat-label">Memory</div>
                        <div class="stat-value" id="memUsage">-</div>
                        <div class="stat-sub" id="memDetails">-</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Temp</div>
                        <div class="stat-value" id="cpuTemp">-</div>
                        <div class="stat-sub" id="tempSub">-</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Disk I/O</div>
                        <div class="stat-value" id="diskIO">-</div>
                        <div class="stat-sub" id="diskIOSub">-</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">Network</div>
                        <div class="stat-value" id="netIO">-</div>
                        <div class="stat-sub" id="netIOSub">-</div>
                    </div>
                    <div class="stat-item" style="grid-column: span 2;">
                        <div class="stat-label">Uptime</div>
                        <div class="stat-value" id="uptime">-</div>
                    </div>
                </div>
            </div>
        </div>

        <h2>Connected Peers</h2>
        <div class="peer-list" id="peerList">
            <div class="card-sub">Loading peers...</div>
        </div>

        <div class="footer">
            <p><span class="fetch-indicator" id="fetchIndicator"></span> Blocks: 60s, CPU: 5s | <span id="lastUpdate">-</span></p>
        </div>
    </div>

    <!-- Settings Modal -->
    <div class="modal-overlay" id="settingsModal">
        <div class="modal">
            <h2>Dashboard Settings</h2>
            <div class="form-group">
                <label>Theme</label>
                <select id="settingTheme">
                    <option value="dark">Dark</option>
                    <option value="light">Light</option>
                </select>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Block Refresh (seconds)</label>
                    <input type="number" id="settingBlockRefresh" min="10" max="300" value="60">
                </div>
                <div class="form-group">
                    <label>CPU Refresh (seconds)</label>
                    <input type="number" id="settingCpuRefresh" min="1" max="60" value="5">
                </div>
            </div>
            <div class="form-group">
                <label>Max Peers Shown</label>
                <input type="number" id="settingMaxPeers" min="5" max="50" value="10">
            </div>
            <div class="form-group" style="margin-top:20px; padding-top:15px; border-top:1px solid var(--border-color);">
                <label style="display:flex; align-items:center; gap:10px; cursor:pointer;">
                    <input type="checkbox" id="settingKnotsPref" style="width:auto;">
                    <span>Prefer Knots peers</span>
                </label>
                <div style="font-size:0.8em; color:var(--text-muted); margin-top:5px;">
                    Slowly disconnect Core peers when above minimum connections to favor Knots nodes.
                </div>
            </div>
            <div class="form-row" id="knotsOptions" style="display:none;">
                <div class="form-group">
                    <label>Min Connections</label>
                    <input type="number" id="settingKnotsMin" min="4" max="20" value="8">
                </div>
                <div class="form-group">
                    <label>Cooldown (seconds)</label>
                    <input type="number" id="settingKnotsCooldown" min="60" max="3600" value="300">
                </div>
            </div>
            <div id="knotsStatus" style="display:none; font-size:0.85em; color:var(--text-secondary); margin-top:10px;"></div>
            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="closeSettings()">Cancel</button>
                <button class="btn btn-primary" onclick="saveSettings()">Save</button>
            </div>
        </div>
    </div>

    <script>
        let lastKnownMode = null;
        let lastData = null;
        let lastFetchTime = 0;
        let blockRefreshInterval = null;
        let cpuRefreshInterval = null;

        // FIX #2: Request-in-flight tracking to prevent stacking
        let updateInFlight = false;
        let updateCpuInFlight = false;

        // FIX #4: Backend health tracking
        let backendHealth = {
            consecutiveFailures: 0,
            lastSuccessTime: Date.now(),
            isHealthy: true
        };

        // Settings
        const defaultSettings = {
            theme: 'dark',
            blockRefresh: 60,
            cpuRefresh: 5,
            maxPeers: 10
        };
        let settings = {...defaultSettings};
        let knotsPref = {enabled: false, min_connections: 8, cooldown: 300};

        // Load settings from localStorage
        function loadSettings() {
            try {
                const saved = localStorage.getItem('dashboardSettings');
                if (saved) {
                    settings = {...defaultSettings, ...JSON.parse(saved)};
                }
            } catch (e) {}
            applySettings();
            loadKnotsPref();
        }

        function loadKnotsPref() {
            fetch('/api/knots-pref').then(r => r.json()).then(d => {
                knotsPref = d;
                updateKnotsUI();
            }).catch(() => {});
        }

        function updateKnotsUI() {
            const cb = document.getElementById('settingKnotsPref');
            const opts = document.getElementById('knotsOptions');
            const status = document.getElementById('knotsStatus');
            cb.checked = knotsPref.enabled;
            opts.style.display = knotsPref.enabled ? 'flex' : 'none';
            document.getElementById('settingKnotsMin').value = knotsPref.min_connections || 8;
            document.getElementById('settingKnotsCooldown').value = knotsPref.cooldown || 300;
            if (knotsPref.enabled) {
                status.style.display = 'block';
                status.innerHTML = `Knots: <b>${knotsPref.knots_count || 0}</b> | Core: <b>${knotsPref.core_count || 0}</b> | Dropped: <b>${knotsPref.disconnected_count || 0}</b>`;
            } else {
                status.style.display = 'none';
            }
        }

        function applySettings() {
            document.documentElement.setAttribute('data-theme', settings.theme);
            document.getElementById('settingTheme').value = settings.theme;
            document.getElementById('settingBlockRefresh').value = settings.blockRefresh;
            document.getElementById('settingCpuRefresh').value = settings.cpuRefresh;
            document.getElementById('settingMaxPeers').value = settings.maxPeers;
        }

        function saveSettings() {
            settings.theme = document.getElementById('settingTheme').value;
            settings.blockRefresh = parseInt(document.getElementById('settingBlockRefresh').value) || 60;
            settings.cpuRefresh = parseInt(document.getElementById('settingCpuRefresh').value) || 5;
            settings.maxPeers = parseInt(document.getElementById('settingMaxPeers').value) || 10;
            localStorage.setItem('dashboardSettings', JSON.stringify(settings));
            applySettings();
            setupIntervals();

            // Save Knots preference to server
            const knotsEnabled = document.getElementById('settingKnotsPref').checked;
            const knotsMin = parseInt(document.getElementById('settingKnotsMin').value) || 8;
            const knotsCooldown = parseInt(document.getElementById('settingKnotsCooldown').value) || 300;
            fetch('/api/knots-pref', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: knotsEnabled, min_connections: knotsMin, cooldown: knotsCooldown})
            }).then(r => r.json()).then(d => {
                knotsPref = {...knotsPref, ...d};
            }).catch(() => {});

            closeSettings();
            update();  // Refresh data immediately to apply new settings
        }

        function toggleTheme() {
            settings.theme = settings.theme === 'dark' ? 'light' : 'dark';
            localStorage.setItem('dashboardSettings', JSON.stringify(settings));
            applySettings();
        }

        function openSettings() {
            loadKnotsPref();  // Refresh Knots status when opening
            document.getElementById('settingsModal').classList.add('active');
        }

        function closeSettings() {
            document.getElementById('settingsModal').classList.remove('active');
        }

        // Knots checkbox toggle
        document.getElementById('settingKnotsPref').addEventListener('change', function() {
            document.getElementById('knotsOptions').style.display = this.checked ? 'flex' : 'none';
        });

        // Close modal on outside click
        document.getElementById('settingsModal').addEventListener('click', function(e) {
            if (e.target === this) closeSettings();
        });

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

            // Update "seconds since last fetch" indicator
            const secAgo = Math.floor(elapsed);
            if (secAgo > 0) {
                const lastUpdateEl = document.getElementById('lastUpdate');
                const time = new Date(lastFetchTime).toLocaleTimeString();
                lastUpdateEl.textContent = `Last updated: ${time} (${secAgo}s ago)`;

                // Update indicator color based on staleness
                const indicator = document.getElementById('fetchIndicator');
                if (secAgo > 120) {  // 2 minutes
                    indicator.className = 'fetch-indicator fetch-stale';
                } else if (secAgo > 60) {  // 1 minute
                    indicator.className = 'fetch-indicator fetch-aging';
                } else if (secAgo > 2) {  // After green pulse fades, reset to gray
                    indicator.className = 'fetch-indicator';
                }
            }
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

        function updateChart() {
            if (chartData.length < 2) return;
            const svg = document.getElementById('speedChart');
            if (!svg) return;  // Chart not in DOM, skip
            const width = 800;
            const height = 150;
            const padding = {top: 10, right: 10, bottom: 20, left: 40};
            const chartWidth = width - padding.left - padding.right;
            const chartHeight = height - padding.top - padding.bottom;

            // Find max value for scaling
            const maxSpeed = Math.max(...chartData.map(d => d.speed), 1);
            const roundedMax = Math.ceil(maxSpeed / 10) * 10 || 10;

            // Build line path
            const points = chartData.map((d, i) => {
                const x = padding.left + (i / (chartData.length - 1)) * chartWidth;
                const y = padding.top + chartHeight - (d.speed / roundedMax) * chartHeight;
                return {x, y};
            });

            const linePath = points.map((p, i) => (i === 0 ? 'M' : 'L') + p.x + ',' + p.y).join(' ');
            const chartLine = document.getElementById('chartLine');
            if (chartLine) chartLine.setAttribute('d', linePath);

            // Area path (fill under line)
            const areaPath = linePath + ' L' + points[points.length-1].x + ',' + (padding.top + chartHeight) +
                ' L' + padding.left + ',' + (padding.top + chartHeight) + ' Z';
            const chartArea = document.getElementById('chartArea');
            if (chartArea) chartArea.setAttribute('d', areaPath);

            // Grid lines
            const gridHtml = [];
            for (let i = 0; i <= 4; i++) {
                const y = padding.top + (i / 4) * chartHeight;
                gridHtml.push('<line class="chart-grid" x1="' + padding.left + '" y1="' + y + '" x2="' + (width - padding.right) + '" y2="' + y + '"/>');
            }
            const chartGrid = document.getElementById('chartGrid');
            if (chartGrid) chartGrid.innerHTML = gridHtml.join('');

            // Y-axis labels
            const labelsHtml = [];
            for (let i = 0; i <= 4; i++) {
                const y = padding.top + (i / 4) * chartHeight + 4;
                const val = Math.round(roundedMax * (1 - i / 4));
                labelsHtml.push('<text class="chart-label" x="' + (padding.left - 5) + '" y="' + y + '" text-anchor="end">' + val + '</text>');
            }
            const chartLabels = document.getElementById('chartLabels');
            if (chartLabels) chartLabels.innerHTML = labelsHtml.join('');

            // Peak display
            const chartPeak = document.getElementById('chartPeak');
            if (chartPeak) chartPeak.textContent = 'Peak: ' + maxSpeed.toFixed(1) + ' blocks/s';
        }

        function updateCpu() {
            // FIX #2: Prevent request stacking
            if (updateCpuInFlight) {
                console.warn('updateCpu: Previous request still in flight, skipping');
                return;
            }
            updateCpuInFlight = true;

            // FIX #1: Add timeout to prevent hanging requests
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 15000); // 15 second timeout

            fetch('/api/cpu', { signal: controller.signal })
                .then(r => {
                    clearTimeout(timeoutId);
                    return r.json();
                })
                .then(d => {
                    // FIX #4: Mark backend as healthy on success
                    backendHealth.consecutiveFailures = 0;
                    backendHealth.lastSuccessTime = Date.now();
                    backendHealth.isHealthy = true;

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
                    // Disk I/O
                    if (d.disk_read_speed_human) {
                        document.getElementById('diskIO').textContent = d.disk_read_speed_human;
                        document.getElementById('diskIOSub').textContent = 'Write: ' + (d.disk_write_speed_human || '-');
                    }
                    // Network I/O
                    if (d.net_rx_speed_human) {
                        document.getElementById('netIO').textContent = d.net_rx_speed_human + ' in';
                        document.getElementById('netIOSub').textContent = (d.net_tx_speed_human || '-') + ' out';
                    }

                    updateCpuInFlight = false;
                })
                .catch(e => {
                    clearTimeout(timeoutId);
                    updateCpuInFlight = false;

                    // FIX #3 & #4: Improve error handling and backend health tracking
                    backendHealth.consecutiveFailures++;
                    if (backendHealth.consecutiveFailures >= 3) {
                        backendHealth.isHealthy = false;
                    }

                    const errMsg = e.name === 'AbortError' ? 'System stats timeout' : 'System stats error';
                    console.error('updateCpu error:', errMsg, e);

                    // FIX #3: Show visual feedback for system stats errors
                    if (backendHealth.consecutiveFailures >= 3) {
                        document.getElementById('statusText').textContent = 'Backend Unhealthy';
                        document.getElementById('statusDot').className = 'status-dot status-error';
                    }
                });
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
            // FIX #2: Prevent request stacking
            if (updateInFlight) {
                console.warn('update: Previous request still in flight, skipping');
                return;
            }
            updateInFlight = true;

            // FIX #1: Add timeout to prevent hanging (already present, kept)
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 15000); // 15 second timeout

            fetch('/api/stats', { signal: controller.signal })
                .then(r => {
                    clearTimeout(timeoutId);
                    return r.json();
                })
                .then(d => {
                if (d.error) {
                    lastData = null;  // Stop interpolation on error
                    updateInFlight = false;

                    // FIX #4: Track backend failures
                    backendHealth.consecutiveFailures++;
                    if (backendHealth.consecutiveFailures >= 3) {
                        backendHealth.isHealthy = false;
                    }

                    document.getElementById('statusText').textContent = 'Error';
                    document.getElementById('statusDot').className = 'status-dot status-error';
                    document.getElementById('fetchIndicator').className = 'fetch-indicator fetch-error';
                    return;
                }

                // FIX #4: Mark backend as healthy on success
                backendHealth.consecutiveFailures = 0;
                backendHealth.lastSuccessTime = Date.now();
                backendHealth.isHealthy = true;

                // Update fetch indicator to show data is fresh
                document.getElementById('fetchIndicator').className = 'fetch-indicator fetch-fresh';
                setTimeout(() => {
                    document.getElementById('fetchIndicator').className = 'fetch-indicator';
                }, 2000);

                // Don't update progress bar until mode is confirmed, but still store data for interpolation
                if (!d.mode_confirmed) {
                    document.getElementById('statusText').textContent = 'Detecting mode...';
                    // Still set lastData so interpolation can work once mode is confirmed
                    lastData = d;
                    lastFetchTime = Date.now();
                    document.getElementById('lastUpdate').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
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

                // Bitcoin download speed
                if (d.btc_download_speed_human) {
                    document.getElementById('btcDownload').textContent = d.btc_download_speed_human;
                    let speedInfo = '';
                    if (d.assumeutxo) {
                        let parts = [];
                        if (d.speed_top) parts.push('tip: ' + d.speed_top.toFixed(1) + ' b/s');
                        if (d.speed_bottom) parts.push('val: ' + d.speed_bottom.toFixed(1) + ' b/s');
                        speedInfo = parts.join(' | ') || 'syncing...';
                    } else if (d.speed) {
                        speedInfo = d.speed.toFixed(1) + ' blocks/s';
                    }
                    document.getElementById('btcDownloadSub').textContent = speedInfo || 'blockchain sync';
                }

                document.getElementById('diskUsage').textContent = d.size_on_disk_human || '-';
                document.getElementById('diskSub').textContent = d.pruned ? 'pruned' : 'full node';

                // Disk needed estimate
                if (d.disk_needed_human) {
                    document.getElementById('diskNeeded').textContent = d.disk_needed_human;
                    document.getElementById('diskNeededSub').textContent = 'to complete sync';
                } else {
                    document.getElementById('diskNeeded').textContent = '-';
                }

                // Version display - highlight Knots
                let ver = (d.version || '-').replace(/\\//g, '');
                if (ver.includes('Knots')) {
                    ver = ver.replace('Bitcoin Knots:', 'Knots ');
                } else if (ver.includes('Satoshi')) {
                    ver = ver.replace('Satoshi:', 'Core ');
                }
                document.getElementById('version').textContent = ver;

                // Mempool stats
                if (d.mempool_size !== undefined) {
                    document.getElementById('mempoolTx').textContent = fmt(d.mempool_size);
                    if (d.mempool_unbroadcast > 0) {
                        document.getElementById('mempoolTxSub').textContent = d.mempool_unbroadcast + ' unbroadcast';
                    } else {
                        document.getElementById('mempoolTxSub').textContent = 'pending transactions';
                    }
                }
                if (d.mempool_bytes !== undefined) {
                    const mb = d.mempool_bytes / (1024 * 1024);
                    const maxMb = (d.mempool_maxmempool || 300000000) / (1024 * 1024);
                    document.getElementById('mempoolSize').textContent = mb.toFixed(1) + ' MB';
                    document.getElementById('mempoolSizeSub').textContent = 'of ' + maxMb.toFixed(0) + ' MB max';
                }
                // Min fee (convert BTC/kB to sat/vB)
                if (d.mempool_minfee !== undefined) {
                    const satPerVb = d.mempool_minfee * 100000;  // BTC/kB to sat/vB
                    document.getElementById('mempoolFee').textContent = satPerVb.toFixed(2);
                    document.getElementById('mempoolFeeSub').textContent = 'sat/vB to enter';
                }
                // Policy (Knots full-RBF)
                if (d.mempool_fullrbf !== undefined && d.mempool_fullrbf !== null) {
                    document.getElementById('mempoolPolicy').textContent = d.mempool_fullrbf ? 'Full RBF' : 'Opt-in RBF';
                    document.getElementById('mempoolPolicySub').textContent = 'replace-by-fee policy';
                } else {
                    document.getElementById('mempoolPolicy').textContent = 'Standard';
                    document.getElementById('mempoolPolicySub').textContent = 'mempool policy';
                }

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

                // Disk I/O
                if (d.disk_read_speed_human) {
                    document.getElementById('diskIO').textContent = d.disk_read_speed_human;
                    document.getElementById('diskIOSub').textContent = 'Write: ' + (d.disk_write_speed_human || '-');
                }

                // Network I/O
                if (d.net_rx_speed_human) {
                    document.getElementById('netIO').textContent = d.net_rx_speed_human + ' in';
                    document.getElementById('netIOSub').textContent = (d.net_tx_speed_human || '-') + ' out';
                }

                document.getElementById('uptime').textContent = d.uptime_human || '-';

                // Temperature
                if (d.cpu_temp !== undefined) {
                    document.getElementById('cpuTemp').textContent = d.cpu_temp.toFixed(0) + '°C';
                    let tempSub = 'CPU';
                    if (d.nvme_temp !== undefined) {
                        tempSub += ' | NVMe: ' + d.nvme_temp.toFixed(0) + '°C';
                    }
                    document.getElementById('tempSub').textContent = tempSub;
                }

                // Peer list
                if (d.peers && d.peers.length > 0) {
                    let html = '';
                    const maxPeers = settings.maxPeers || 10;
                    const peers = d.peers.slice(0, maxPeers);
                    for (let p of peers) {
                        const dir = p.inbound ? 'in' : 'out';
                        const dirClass = p.inbound ? 'peer-inbound' : 'peer-outbound';
                        // Clean up version string - show full client name
                        let ver = (p.subver || '').replace(/\\//g, '');
                        // Remove trailing version numbers and clean up
                        ver = ver.replace(/^Satoshi:/, 'Core ').replace(/^Bitcoin Knots:/, 'Knots ');
                        // Extract just the IP without port for cleaner display
                        const addr = p.addr.split(':')[0] || p.addr;
                        html += `<div class="peer-row">
                            <span class="peer-addr">${addr}</span>
                            <span class="peer-version">${ver}</span>
                            <span class="peer-direction ${dirClass}">${dir}</span>
                        </div>`;
                    }
                    if (d.peer_count > maxPeers) {
                        html += '<div style="text-align:center;color:var(--text-muted);font-size:0.8em;padding-top:8px;">+' + (d.peer_count - maxPeers) + ' more peers</div>';
                    }
                    document.getElementById('peerList').innerHTML = html;
                }

                // Update chart data from server history
                if (d.chart_history && d.chart_history.length > 0) {
                    chartData = d.chart_history.map(h => ({time: h.time * 1000, speed: h.speed}));
                }
                updateChart();

                document.getElementById('lastUpdate').textContent = 'Last updated: ' + new Date().toLocaleTimeString();

                // Store for interpolation
                lastData = d;
                lastFetchTime = Date.now();

                // FIX #2: Clear request-in-flight flag
                updateInFlight = false;

                // FIX #3: Detect and warn about stale data
                const dataAge = (Date.now() - lastFetchTime) / 1000;
                if (dataAge > settings.blockRefresh * 2) {
                    console.warn('Data appears stale, age:', dataAge.toFixed(0), 'seconds');
                }

            }).catch(e => {
                clearTimeout(timeoutId);
                updateInFlight = false;
                lastData = null;  // Stop interpolation on connection error

                // FIX #4: Track backend failures
                backendHealth.consecutiveFailures++;
                if (backendHealth.consecutiveFailures >= 3) {
                    backendHealth.isHealthy = false;
                }

                const errMsg = e.name === 'AbortError' ? 'Request Timeout' : 'Connection Error';
                document.getElementById('statusText').textContent = errMsg;
                document.getElementById('statusDot').className = 'status-dot status-error';
                document.getElementById('fetchIndicator').className = 'fetch-indicator fetch-error';
                console.error('API fetch error:', e, '| Consecutive failures:', backendHealth.consecutiveFailures);
            });
        }

        function setupIntervals() {
            if (blockRefreshInterval) clearInterval(blockRefreshInterval);
            if (cpuRefreshInterval) clearInterval(cpuRefreshInterval);
            blockRefreshInterval = setInterval(update, settings.blockRefresh * 1000);
            cpuRefreshInterval = setInterval(updateCpu, settings.cpuRefresh * 1000);
        }

        // FIX #3 & #4: Periodic backend health check
        function checkBackendHealth() {
            const now = Date.now();
            const timeSinceSuccess = (now - backendHealth.lastSuccessTime) / 1000;

            // If no successful request in 2 minutes, warn user
            if (timeSinceSuccess > 120) {
                console.warn('Backend unhealthy - no successful requests for', timeSinceSuccess.toFixed(0), 'seconds');
                // Update status to show degraded state
                if (backendHealth.consecutiveFailures >= 5) {
                    document.getElementById('statusText').textContent = 'Backend Disconnected';
                    document.getElementById('statusDot').className = 'status-dot status-error';
                } else if (backendHealth.consecutiveFailures >= 3) {
                    document.getElementById('statusText').textContent = 'Backend Degraded';
                    document.getElementById('statusDot').className = 'status-dot status-error';
                }
            }
        }

        // Keyboard shortcuts
        document.addEventListener('keydown', function(e) {
            // Ignore if typing in input field
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            switch(e.key.toLowerCase()) {
                case 'r':
                    // Refresh now
                    e.preventDefault();
                    console.log('Manual refresh triggered');
                    update();
                    updateCpu();
                    break;

                case 's':
                    // Open settings
                    e.preventDefault();
                    document.getElementById('settingsModal').style.display = 'flex';
                    break;

                case 'f':
                    // Toggle fullscreen
                    e.preventDefault();
                    if (!document.fullscreenElement) {
                        document.documentElement.requestFullscreen().catch(err => {
                            console.log('Fullscreen failed:', err);
                        });
                    } else {
                        document.exitFullscreen();
                    }
                    break;

                case '?':
                    // Show keyboard shortcuts help
                    e.preventDefault();
                    alert('Keyboard Shortcuts:\\n\\n' +
                          'R - Refresh now\\n' +
                          'S - Settings\\n' +
                          'F - Fullscreen\\n' +
                          '? - Show this help');
                    break;
            }
        });

        // Initialize
        loadSettings();

        // Fast initial load - CPU/system stats first
        updateCpu();
        document.getElementById('statusText').textContent = 'Loading blockchain...';

        // Then load blockchain data (slow)
        update();

        // Setup refresh intervals
        setupIntervals();
        setInterval(interpolate, 1000);  // Smooth block updates every 1s

        // FIX #3 & #4: Check backend health every 30 seconds
        setInterval(checkBackendHealth, 30000);
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
            start_time = time.time()  # Performance monitoring
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            # ASYNC OPTIMIZATION: Parallelize independent stats collection
            futures = {}
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures['bitcoin'] = executor.submit(get_bitcoin_stats)
                futures['system'] = executor.submit(get_system_stats)
                futures['disk_io'] = executor.submit(get_disk_io)
                futures['net_io'] = executor.submit(get_network_io)
                futures['mempool'] = executor.submit(get_mempool_stats)
                futures['latest'] = executor.submit(get_latest_block)
                futures['btc_net'] = executor.submit(get_btc_network_speed)
                futures['temp'] = executor.submit(get_temperature)
                futures['peers'] = executor.submit(get_peer_info)
                futures['disk_est'] = executor.submit(get_disk_estimate)

                # Wait for all to complete
                stats = futures['bitcoin'].result()

            if stats:
                stats = calculate_speed(stats)
                stats['size_on_disk_human'] = format_bytes(stats.get('size_on_disk', 0))

                # Merge all parallel results
                sys_stats = futures['system'].result()
                stats.update(sys_stats)
                if 'mem_total' in stats:
                    stats['mem_total_human'] = format_bytes(stats['mem_total'])
                    stats['mem_used_human'] = format_bytes(stats['mem_used'])

                disk_io = futures['disk_io'].result()
                stats.update(disk_io)
                stats['disk_read_speed_human'] = format_speed(disk_io.get('disk_read_speed', 0))
                stats['disk_write_speed_human'] = format_speed(disk_io.get('disk_write_speed', 0))

                net_io = futures['net_io'].result()
                stats.update(net_io)
                stats['net_rx_speed_human'] = format_speed(net_io.get('net_rx_speed', 0))
                stats['net_tx_speed_human'] = format_speed(net_io.get('net_tx_speed', 0))

                mempool = futures['mempool'].result()
                stats.update(mempool)

                latest = futures['latest'].result()
                stats.update(latest)

                btc_net = futures['btc_net'].result()
                stats.update(btc_net)
                stats['btc_download_speed_human'] = format_speed(btc_net.get('btc_download_speed', 0))
                stats['btc_upload_speed_human'] = format_speed(btc_net.get('btc_upload_speed', 0))

                temp = futures['temp'].result()
                stats.update(temp)

                peers = futures['peers'].result()
                stats.update(peers)

                disk_est = futures['disk_est'].result()
                stats.update(disk_est)

                # Add Knots preference status
                stats['knots_pref_enabled'] = knots_pref['enabled']
                stats['knots_pref_disconnected'] = knots_pref['disconnected_count']

                # Run Knots preference enforcement if enabled
                if knots_pref['enabled']:
                    enforce_knots_preference()

                # Add performance timing
                stats['api_response_time_ms'] = int((time.time() - start_time) * 1000)
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
            # Add disk I/O
            disk_io = get_disk_io()
            stats.update(disk_io)
            stats['disk_read_speed_human'] = format_speed(disk_io.get('disk_read_speed', 0))
            stats['disk_write_speed_human'] = format_speed(disk_io.get('disk_write_speed', 0))
            # Add network I/O
            net_io = get_network_io()
            stats.update(net_io)
            stats['net_rx_speed_human'] = format_speed(net_io.get('net_rx_speed', 0))
            stats['net_tx_speed_human'] = format_speed(net_io.get('net_tx_speed', 0))
            self.wfile.write(json.dumps(stats).encode())

        elif path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')

        elif path == '/api/knots-pref':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            # Run enforcement check if enabled
            result = enforce_knots_preference()
            response = {
                'enabled': knots_pref['enabled'],
                'min_connections': knots_pref['min_connections'],
                'cooldown': knots_pref['cooldown'],
                'disconnected_count': knots_pref['disconnected_count'],
                'knots_count': knots_pref['knots_count'],
                'core_count': knots_pref['core_count'],
                'last_action': result
            }
            self.wfile.write(json.dumps(response).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else '{}'

        if path == '/api/knots-pref':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            try:
                data = json.loads(body)
                if 'enabled' in data:
                    knots_pref['enabled'] = bool(data['enabled'])
                if 'min_connections' in data:
                    knots_pref['min_connections'] = max(4, min(20, int(data['min_connections'])))
                if 'cooldown' in data:
                    knots_pref['cooldown'] = max(60, min(3600, int(data['cooldown'])))

                response = {
                    'success': True,
                    'enabled': knots_pref['enabled'],
                    'min_connections': knots_pref['min_connections'],
                    'cooldown': knots_pref['cooldown']
                }
            except (json.JSONDecodeError, ValueError) as e:
                response = {'success': False, 'error': str(e)}

            self.wfile.write(json.dumps(response).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


def warmup_cache():
    """Warm up caches in background to make first request fast"""
    import threading
    def warmup():
        time.sleep(2)  # Give server time to start
        print("Warming up caches...")
        try:
            # Warm up bitcoin stats cache
            get_bitcoin_stats()
            # Warm up peer cache
            get_peer_info()
            # Warm up system stats
            get_system_stats()

            # Initialize network speed tracking (needs 2 calls to calculate speed)
            get_btc_network_speed()  # First call establishes baseline
            time.sleep(3)  # Wait a bit for some data transfer
            get_btc_network_speed()  # Second call calculates initial speed

            print("Cache warmup complete")
        except Exception as e:
            print(f"Cache warmup failed: {e}")

    thread = threading.Thread(target=warmup, daemon=True)
    thread.start()


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

    # Initialize SSH connection pool for remote hosts
    global ssh_pool
    ssh_pool = SSHConnectionPool(CONFIG['host'], CONFIG['ssh_user'], max_connections=3)

    print(f"Bitcoin Knots Dashboard")
    print(f"Node: {CONFIG['host']}")
    if CONFIG['docker_container']:
        print(f"Docker: {CONFIG['docker_container']}")
    if ssh_pool.enabled:
        print(f"SSH Pool: Enabled (max 3 connections)")
    print(f"Dashboard: http://0.0.0.0:{CONFIG['port']}")

    # Start cache warmup in background
    warmup_cache()

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    with Server(("0.0.0.0", CONFIG['port']), DashboardHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            if ssh_pool:
                ssh_pool.close_all()


if __name__ == '__main__':
    main()
