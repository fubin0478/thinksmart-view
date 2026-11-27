#!/opt/thinksmart-addons/venv/bin/python
"""Local read-only display adapter for the owner's public Nezha stream."""
import argparse
import asyncio
import calendar
from collections import deque
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from thinksmart_display import DisplayControl

LOG = logging.getLogger('thinksmart-monitor')
_ORIG_CLIENT_SESSION = aiohttp.ClientSession
_SHARED_SSL_CTX = None


def _get_ssl_ctx():
    global _SHARED_SSL_CTX
    if _SHARED_SSL_CTX is None:
        import ssl
        _SHARED_SSL_CTX = ssl.create_default_context()
    return _SHARED_SSL_CTX


def _conn_kw(limit: int = 8) -> dict:
    if aiohttp.ClientSession is not _ORIG_CLIENT_SESSION:
        return {}
    return {'connector': aiohttp.TCPConnector(ssl=_get_ssl_ctx(), limit=limit, ttl_dns_cache=300)}

_LAST_TRIM_MONO = 0.0


def _trim_idle_heap() -> bool:
    global _LAST_TRIM_MONO
    now_m = time.monotonic()
    if now_m - _LAST_TRIM_MONO < 30.0:
        return False
    _LAST_TRIM_MONO = now_m
    try:
        import ctypes
        import gc
        gc.collect()
        ctypes.CDLL('libc.so.6').malloc_trim(0)
        return True
    except Exception:
        return False

NEZHA_HOST = os.environ.get('NEZHA_HOST', 'nezha.example.com').strip().removeprefix('https://').removeprefix('http://').strip('/') or 'nezha.example.com'
UPSTREAM = os.environ.get('NEZHA_WS_URL', f'wss://{NEZHA_HOST}/api/v1/ws/server')
TRAFFIC_UPSTREAM = os.environ.get('NEZHA_SERVICE_URL', f'https://{NEZHA_HOST}/api/v1/service')
SERVER_SERVICE_UPSTREAM = os.environ.get('NEZHA_SERVER_SERVICE_URL', f'https://{NEZHA_HOST}/api/v1/server/{{sid}}/service')
_DEFAULT_UPSTREAM = UPSTREAM
_DEFAULT_TRAFFIC_UPSTREAM = TRAFFIC_UPSTREAM
_DEFAULT_SERVER_SERVICE_UPSTREAM = SERVER_SERVICE_UPSTREAM
# The upstream stream (~1 update/s, ~230 MB/day through the proxy) is only kept open while the
# status page is actually polling /api/state; it closes after this many seconds without a viewer.
VIEW_IDLE = float(os.environ.get('MONITOR_VIEW_IDLE', '90'))
FIRST_DATA_WAIT = 3.0  # a page opened while idle waits this long for fresh data instead of flashing "disconnected"
VOICE_DATA_WAIT = 6.0  # cold voice queries wait slightly longer so first ask gets live numbers
VOICE_VIEW_LEASE = 15.0  # short WS lease when woken solely by voice query
IDLE_PROBE_INITIAL = 45.0
IDLE_PROBE_INTERVAL = 600.0  # 10-minute single-frame background health probe (<4 MB/day)
IDLE_PROBE_OFFLINE_INTERVAL = 60.0  # 1-minute fast re-probe while any node is offline
IDLE_PROBE_NIGHT_INTERVAL = 900.0  # 15-minute gentler probe during 00:00-06:30 sleep window
IDLE_PROBE_RECHECK_DELAY = 6.5  # debounce window before recording an idle offline transition
MAX_EVENTS_HISTORY = 50
CGROUP_SYSTEM_SLICE = Path('/sys/fs/cgroup/system.slice')
PROC_ROOT = Path('/proc')
USD_TO_CNY_REF = 7.25
CARRIER_ORDER = {'联通': 0, '电信': 1, '移动': 2}
NUMBERS = (
    'cpu', 'mem_used', 'swap_used', 'disk_used',
    'net_in_transfer', 'net_out_transfer', 'net_in_speed', 'net_out_speed',
    'uptime', 'load_1', 'load_5', 'load_15',
    'tcp_conn_count', 'udp_conn_count', 'process_count',
)
SH_TZ = ZoneInfo('Asia/Shanghai')
_CPU_CORE_RE = re.compile(r'(\d+)\s*(?:Virtual|Physical)?\s*Core', re.IGNORECASE)
_CPU_CLEAN_RE = re.compile(
    r'\((?:R|TM|tm)\)|\b(?:CPU|Processor)\b|@\s*[\d.]+\s*GHz|\b\d+\s*(?:Virtual|Physical)?\s*Core\b.*$',
    re.IGNORECASE,
)
_PRICE_NUM_RE = re.compile(r'(\d+(?:\.\d+)?)')


def number(value):
    if isinstance(value, bool):
        return 0
    try:
        value = float(value)
        return max(0, value) if math.isfinite(value) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


_CPU_CACHE = {}


def _parse_cpu_spec(cpu_list):
    if not isinstance(cpu_list, list) or not cpu_list:
        return None, 0
    first = cpu_list[0]
    if not isinstance(first, str) or not first.strip():
        return None, 0
    key = tuple(str(x)[:120] for x in cpu_list[:4] if isinstance(x, str))
    if key in _CPU_CACHE:
        return _CPU_CACHE[key]
    cores = 0
    for item in key:
        m = _CPU_CORE_RE.search(item)
        cores += int(m.group(1)) if m else 1
    cleaned = _CPU_CLEAN_RE.sub(' ', key[0])
    model = ' '.join(cleaned.split())[:32] or None
    res = (model, max(0, min(512, cores)))
    if len(_CPU_CACHE) >= 64:
        _CPU_CACHE.pop(next(iter(_CPU_CACHE)))
    _CPU_CACHE[key] = res
    return res


def _carrier_order_key(name: str) -> int:
    for k, idx in CARRIER_ORDER.items():
        if k in name:
            return idx
    return 99


def _window_stats_and_buckets(vals, bucket_size: int, spike_thr: float):
    if not vals:
        return {'avg': None, 'min': None, 'max': None, 'p96': None, 'loss': 0.0, 'pts': []}
    pos = [v for v in vals if v > 0]
    drops = sum(1 for v in vals if v <= 0)
    loss_pct = round(100.0 * drops / len(vals), 1)
    if pos:
        s_pos = sorted(pos)
        avg_ms = round(sum(pos) / len(pos), 1)
        min_ms = round(s_pos[0], 1)
        max_ms = round(s_pos[-1], 1)
        p96_ms = round(s_pos[min(len(s_pos) - 1, int(len(s_pos) * 0.96))], 1)
    else:
        avg_ms = min_ms = max_ms = p96_ms = None
    pts = []
    if bucket_size <= 1:
        for v in vals:
            if v <= 0:
                pts.append([0.0, 2, 0])
            else:
                rv = round(v, 1)
                pts.append([rv, 1 if v >= spike_thr else 0, round(v)])
    else:
        n = len(vals)
        # Align buckets from the right so the last bucket always ends at the newest minute
        rem = n % bucket_size
        idx = 0
        while idx < n:
            step = rem if (idx == 0 and rem > 0) else bucket_size
            chunk = vals[idx:idx + step]
            idx += step
            c_pos = [v for v in chunk if v > 0]
            has_drop = any(v <= 0 for v in chunk)
            has_spike = any(v >= spike_thr for v in c_pos)
            if not c_pos:
                pts.append([0.0, 2, 0])
            else:
                if has_spike and len(c_pos) >= 3:
                    sc = sorted(c_pos)
                    rep = sc[len(sc) // 2]
                else:
                    rep = sum(c_pos) / len(c_pos)
                pts.append([round(rep, 1), 2 if has_drop else (1 if has_spike else 0), round(max(c_pos))])
    return {
        'avg': avg_ms,
        'min': min_ms,
        'max': max_ms,
        'p96': p96_ms,
        'loss': loss_pct,
        'pts': pts,
    }


def _align_minute_series(raw_ts, raw_delays, now_ts: float | None = None):
    """Align Nezha minute probe arrays onto a continuous 1-minute grid, zero-filling agent outages."""
    dl_seq = [number(x) for x in raw_delays[-1440:]]
    if not dl_seq:
        return [], 0.0, False, 0
    stale = False
    stale_sec = 0
    if isinstance(raw_ts, list) and len(raw_ts) == len(raw_delays):
        ts_seq = [number(x) for x in raw_ts[-1440:]]
        aligned = []
        prev_t = None
        for t_ms, d_val in zip(ts_seq, dl_seq):
            if prev_t is not None and t_ms > prev_t:
                dt_ms = t_ms - prev_t
                if dt_ms >= 105000.0:
                    missing = min(1440, max(0, int(round(dt_ms / 60000.0)) - 1))
                    if missing > 0:
                        aligned.extend([0.0] * missing)
            aligned.append(d_val)
            if t_ms > 0:
                prev_t = t_ms
        end_ts = round(prev_t / 1000.0, 1) if (prev_t and prev_t > 0) else round(time.time(), 1)
        if now_ts is not None and prev_t and prev_t > 0:
            last_s = prev_t / 1000.0
            lag_s = float(now_ts) - last_s
            if lag_s >= 180.0:
                stale = True
                stale_sec = int(round(lag_s))
                trail_missing = min(1440, max(1, int((lag_s - 65.0) // 60.0)))
                aligned.extend([0.0] * trail_missing)
                end_ts = round(last_s + trail_missing * 60.0, 1)
        return aligned[-1440:], end_ts, stale, stale_sec
    end_ms = number(raw_ts[-1]) if raw_ts else 0
    end_ts = round(end_ms / 1000.0, 1) if end_ms > 0 else round(time.time(), 1)
    if now_ts is not None and end_ms > 0:
        lag_s = float(now_ts) - (end_ms / 1000.0)
        if lag_s >= 180.0:
            stale = True
            stale_sec = int(round(lag_s))
            trail_missing = min(1440, max(1, int((lag_s - 65.0) // 60.0)))
            dl_seq = (dl_seq + [0.0] * trail_missing)[-1440:]
            end_ts = round((end_ms / 1000.0) + trail_missing * 60.0, 1)
    return dl_seq, end_ts, stale, stale_sec


def normalize_server_service(payload, now_ts: float | None = None, server_alive: bool | None = None):
    """Parse /api/v1/server/{sid}/service per-server minute latency into 联通 -> 电信 -> 移动."""
    if not isinstance(payload, dict) or payload.get('success') is not True:
        return []
    data = payload.get('data')
    if not isinstance(data, list):
        return []
    items = []
    for raw_idx, obj in enumerate(data):
        if not isinstance(obj, dict):
            continue
        name = str(obj.get('monitor_name') or '').strip()[:16]
        if not name:
            continue
        raw_delays = obj.get('avg_delay') if isinstance(obj.get('avg_delay'), list) else []
        raw_ts = obj.get('created_at') if isinstance(obj.get('created_at'), list) else []
        vals, end_ts, stale, stale_sec = _align_minute_series(raw_ts, raw_delays, now_ts=now_ts)
        if not vals:
            continue
        if server_alive is False:
            stale = True
        pos_all = sorted(v for v in vals if v > 0)
        med_24h = pos_all[len(pos_all) // 2] if pos_all else 200.0
        spike_thr = max(420.0, med_24h * 1.85)

        vals_1h = vals[-60:]
        vals_6h = vals[-360:]
        w1 = _window_stats_and_buckets(vals_1h, 1, spike_thr)
        w6 = _window_stats_and_buckets(vals_6h, 5, spike_thr)
        w24 = _window_stats_and_buckets(vals, 15, spike_thr)

        latest_ms = None
        if not stale:
            for v in reversed(vals[-5:]):
                if v > 0:
                    latest_ms = round(v, 1)
                    break

        recent_15 = vals[-15:]
        recent_loss = (100.0 * sum(1 for v in recent_15 if v <= 0) / len(recent_15)) if recent_15 else 0.0
        high_15 = sum(1 for v in recent_15 if v >= 600.0)
        recent_pos = sorted(v for v in vals[-3:] if v > 0)
        smooth_ms = recent_pos[len(recent_pos) // 2] if recent_pos else None
        consec_drop = len(vals) >= 2 and vals[-1] <= 0 and vals[-2] <= 0
        loss_1h = w1['loss']
        avail_24h = round(max(0.0, 100.0 - w24['loss']), 2)
        if (
            stale
            or latest_ms is None
            or consec_drop
            or recent_loss >= 20.0
            or (smooth_ms is not None and smooth_ms >= 450.0)
            or high_15 >= 5
        ):
            status = 'bad'
        elif (
            vals[-1] <= 0
            or recent_loss >= 6.5
            or loss_1h >= 3.0
            or (smooth_ms is not None and smooth_ms >= 280.0)
            or latest_ms >= 450.0
            or high_15 >= 3
        ):
            status = 'warn'
        else:
            status = 'ok'

        mid = str(obj.get('monitor_id') if obj.get('monitor_id') is not None else raw_idx)[:8]
        sid = obj.get('server_id') if type(obj.get('server_id')) is int else None
        rec = {
            'id': mid,
            'server_id': sid,
            'name': name,
            'latest_ms': latest_ms,
            'min_ms': w1['min'],
            'max_ms': w1['max'],
            'avg_ms': w1['avg'],
            'p96_ms': w1['p96'],
            'loss_pct': loss_1h,
            'avail_pct': avail_24h,
            'end_ts': end_ts,
            'status': status,
            'stale': bool(stale),
            'delays': [round(v, 1) for v in vals_1h],
            'win': {
                '1h': w1,
                '6h': w6,
                '24h': w24,
            },
        }
        if stale and stale_sec > 0:
            rec['stale_sec'] = stale_sec
        items.append((_carrier_order_key(name), mid, rec))
    items.sort(key=lambda t: (t[0], t[1]))
    return [x[2] for x in items[:8]]


def normalize_carriers(payload):
    """Fallback three-carrier latency parser from /api/v1/service, ordered 联通 -> 电信 -> 移动."""
    if not isinstance(payload, dict) or payload.get('success') is not True:
        return []
    data = payload.get('data')
    services = data.get('services') if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return []
    items = []
    for raw_id, obj in services.items():
        if not isinstance(obj, dict):
            continue
        name = str(obj.get('service_name') or '').strip()[:16]
        if not name:
            continue
        raw_delays = obj.get('delay') if isinstance(obj.get('delay'), list) else []
        raw_ups = obj.get('up') if isinstance(obj.get('up'), list) else []
        raw_downs = obj.get('down') if isinstance(obj.get('down'), list) else []
        delays = [round(number(x), 1) for x in raw_delays[-30:]]
        ups = [int(number(x)) for x in raw_ups[-30:]]
        downs = [int(number(x)) for x in raw_downs[-30:]]
        valid_ms = sorted(v for v in delays if v > 0)
        latest_ms = None
        for v in reversed(delays):
            if v > 0:
                latest_ms = v
                break
        min_ms = valid_ms[0] if valid_ms else None
        max_ms = valid_ms[-1] if valid_ms else None
        avg_ms = round(sum(valid_ms) / len(valid_ms), 1) if valid_ms else None
        p96_ms = valid_ms[min(len(valid_ms) - 1, int(len(valid_ms) * 0.96))] if valid_ms else None
        cur_up = int(number(obj.get('current_up')))
        cur_down = int(number(obj.get('current_down')))
        tot_up = int(number(obj.get('total_up')))
        tot_down = int(number(obj.get('total_down')))
        cur_tot = cur_up + cur_down
        tot_all = tot_up + tot_down
        loss_pct = round(100.0 * cur_down / cur_tot, 1) if cur_tot > 0 else 0.0
        avail_pct = round(100.0 * tot_up / tot_all, 2) if tot_all > 0 else None
        recent_pos = sorted(v for v in delays[-3:] if v > 0)
        smooth_ms = recent_pos[len(recent_pos) // 2] if recent_pos else None
        if latest_ms is None or loss_pct >= 15.0 or (smooth_ms is not None and smooth_ms >= 450.0):
            status = 'bad'
        elif loss_pct >= 2.0 or (smooth_ms is not None and smooth_ms >= 300.0) or latest_ms >= 450.0:
            status = 'warn'
        else:
            status = 'ok'
        med = valid_ms[len(valid_ms) // 2] if valid_ms else 200.0
        spike_thr = max(420.0, med * 1.85)
        w1 = _window_stats_and_buckets(delays, 1, spike_thr)
        w1['loss'] = loss_pct
        items.append((_carrier_order_key(name), str(raw_id)[:8], {
            'id': str(raw_id)[:8],
            'name': name,
            'latest_ms': latest_ms,
            'min_ms': min_ms,
            'max_ms': max_ms,
            'avg_ms': avg_ms,
            'p96_ms': p96_ms,
            'loss_pct': loss_pct,
            'avail_pct': avail_pct,
            'current_up': cur_up,
            'current_down': cur_down,
            'end_ts': round(time.time(), 1),
            'status': status,
            'delays': delays,
            'ups': ups,
            'downs': downs,
            'win': {'1h': w1, '6h': w1, '24h': w1},
        }))
    items.sort(key=lambda t: (t[0], t[1]))
    return [x[2] for x in items[:8]]


def normalize_traffic(payload, config, now=None):
    """Only publish the configured billing counter, never reboot totals or a false remainder."""
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise ValueError('Invalid traffic response')
    data = payload.get('data')
    stats = data.get('cycle_transfer_stats') if isinstance(data, dict) else None
    entry = stats.get(str(config['rule_id'])) if isinstance(stats, dict) else None
    if not isinstance(entry, dict):
        raise ValueError('Missing configured traffic counter')
    try:
        start, end, complete_from = [dt.datetime.fromisoformat(v.replace('Z', '+00:00'))
                                     for v in (entry['from'], entry['to'], config['complete_from'])]
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('Invalid traffic period') from exc
    now = dt.datetime.now(dt.timezone.utc) if now is None else now
    if not isinstance(now, dt.datetime) or any(v.tzinfo is None for v in (start, end, complete_from, now)) or not start <= now < end:
        raise ValueError('Traffic period is stale or invalid')
    transfer = entry.get('transfer')
    srv_key = str(config['server_id'])
    used = transfer.get(srv_key) if isinstance(transfer, dict) else None
    # At exact cycle rollover (e.g. 11-16 00:00), Nezha may emit an empty `transfer: {}` for ~15 min
    # until the first `next_update` tick settles. Treat an empty dict in the first 20 min as 0 B.
    if used is None and isinstance(transfer, dict) and len(transfer) == 0 and 0 <= (now - start).total_seconds() < 1200:
        used = 0
    expected_max = config.get('limit_bytes')
    raw_max = entry.get('max')
    if config.get('allow_upstream_max') is True and type(raw_max) is int and raw_max > 0:
        base_limit = raw_max
    else:
        if raw_max != expected_max:
            raise ValueError('Missing configured traffic counter')
        base_limit = expected_max
    if type(used) is not int or used < 0:
        raise ValueError('Missing configured traffic counter')
    complete = start >= complete_from
    raw_used, calibrated, limit, applied_extra = used, False, base_limit, 0
    calibration = config.get('calibration') or {}
    if isinstance(calibration, dict) and calibration.get('from') and calibration.get('to'):
        try:
            cal_start, cal_end = [dt.datetime.fromisoformat(v.replace('Z', '+00:00'))
                                  for v in (calibration['from'], calibration['to'])]
        except (KeyError, TypeError, AttributeError, ValueError):
            cal_start = cal_end = None
        if (cal_start is not None and cal_end is not None
                and cal_start.tzinfo is not None and cal_end.tzinfo is not None
                and cal_start == start and cal_end == end):
            baseline, observed = calibration.get('used_bytes'), calibration.get('observed_bytes')
            extra = calibration.get('extra_bytes', 0)
            if (any(type(v) is not int or v < 0 for v in (baseline, observed, extra))
                    or used < observed):
                raise ValueError('Calibration baseline is invalid or upstream counter regressed')
            used, calibrated = baseline + used - observed, True
            applied_extra = extra
            limit = base_limit + extra
    out = {
        'available': True,
        'server_id': config['server_id'],
        'used_bytes': used,
        'raw_used_bytes': raw_used,
        'calibrated': calibrated,
        'limit_bytes': limit,
        'from': entry['from'],
        'to': entry['to'],
        'complete': complete,
        'remaining_bytes': max(0, limit - used) if complete or calibrated else None,
    }
    if applied_extra > 0:
        out['extra_bytes'] = applied_extra
    if type(config.get('reset_day')) is int and 1 <= config['reset_day'] <= 31:
        out['reset_day'] = config['reset_day']
    rname = str(entry.get('name') or '').strip()[:32]
    if rname:
        out['rule_name'] = rname
    return out


def normalize_extra_traffic_rules(payload, skip_rule_id=None, valid_sids=None, now=None):
    """Auto-discover any additional per-server billing cycle rules configured in Nezha."""
    if not isinstance(payload, dict) or payload.get('success') is not True:
        return {}
    data = payload.get('data')
    stats = data.get('cycle_transfer_stats') if isinstance(data, dict) else None
    if not isinstance(stats, dict):
        return {}
    now = dt.datetime.now(dt.timezone.utc) if now is None else now
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        return {}
    skip_str = str(skip_rule_id) if skip_rule_id is not None else None
    sid_allow = set(valid_sids) if valid_sids is not None else None
    out = {}
    for raw_rid, entry in stats.items():
        rid_str = str(raw_rid)
        if rid_str == skip_str or not isinstance(entry, dict):
            continue
        raw_max = entry.get('max')
        if type(raw_max) is not int or raw_max <= 0:
            continue
        try:
            start = dt.datetime.fromisoformat(str(entry['from']).replace('Z', '+00:00'))
            end = dt.datetime.fromisoformat(str(entry['to']).replace('Z', '+00:00'))
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None or end.tzinfo is None or not (start <= now < end):
            continue
        transfer = entry.get('transfer')
        srv_names = entry.get('server_name')
        if not isinstance(transfer, dict):
            continue
        cand_map = dict(transfer)
        if not cand_map and isinstance(srv_names, dict) and len(srv_names) == 1 and 0 <= (now - start).total_seconds() < 1200:
            only_k = next(iter(srv_names))
            cand_map = {only_k: 0}
        if len(cand_map) != 1:
            continue
        sid_key, used = next(iter(cand_map.items()))
        try:
            sid = int(sid_key)
        except (TypeError, ValueError):
            continue
        if sid_allow is not None and sid not in sid_allow:
            continue
        if type(used) is not int or used < 0:
            continue
        rec = {
            'available': True,
            'rule_id': rid_str[:12],
            'server_id': sid,
            'used_bytes': used,
            'raw_used_bytes': used,
            'calibrated': False,
            'limit_bytes': raw_max,
            'from': entry['from'],
            'to': entry['to'],
            'complete': True,
            'remaining_bytes': max(0, raw_max - used),
        }
        rname = str(entry.get('name') or '').strip()[:32]
        if rname:
            rec['rule_name'] = rname
        out[str(sid)] = rec
    return out


def compute_billing_cycle(reset_day: int, now_dt: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """Compute [start, end) monthly billing window in Asia/Shanghai anchored at 00:00 on reset_day."""
    rday = max(1, min(31, int(reset_day)))
    cur = dt.datetime.now(SH_TZ) if now_dt is None else now_dt.astimezone(SH_TZ)
    y, m = cur.year, cur.month
    cand_day = min(rday, calendar.monthrange(y, m)[1])
    cand = dt.datetime(y, m, cand_day, 0, 0, 0, tzinfo=SH_TZ)
    if cur >= cand:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end_day = min(rday, calendar.monthrange(ny, nm)[1])
        return cand, dt.datetime(ny, nm, end_day, 0, 0, 0, tzinfo=SH_TZ)
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    start_day = min(rday, calendar.monthrange(py, pm)[1])
    return dt.datetime(py, pm, start_day, 0, 0, 0, tzinfo=SH_TZ), cand


def _read_small_text(path: Path, limit: int = 8192) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        chunks, rem = [], limit
        while rem > 0:
            b = os.read(fd, min(4096, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        return b''.join(chunks).decode('utf-8', errors='ignore')
    except OSError:
        return None
    finally:
        os.close(fd)


def probe_local_links(
    prefer_direct: bool = False,
    cgroup_dir: Path | None = None,
    proc_dir: Path | None = None,
    run_dir: Path | None = None,
) -> list[dict]:
    """Zero-fork local health check for dual reverse SSH tunnels and local Clash (:7890) proxy."""
    cg_base = CGROUP_SYSTEM_SLICE if cgroup_dir is None else Path(cgroup_dir)
    pr_base = PROC_ROOT if proc_dir is None else Path(proc_dir)
    rn_base = Path('/run') if (run_dir is None and cgroup_dir is None and proc_dir is None) else (Path(run_dir) if run_dir is not None else None)
    units = (
        ('tun_us', '美优隧道', 'thinksmart-intercom-tunnel.service', 'thinksmart-intercom-tunnel.up'),
        ('tun_jp', '绿云隧道', 'thinksmart-intercom-tunnel-jp.service', 'thinksmart-intercom-tunnel-jp.up'),
    )
    links = []
    any_found = False
    for lid, label, unit, up_name in units:
        if cgroup_dir is None and unit == 'thinksmart-intercom-tunnel-jp.service' and not Path('/etc/systemd/system/multi-user.target.wants/thinksmart-intercom-tunnel-jp.service').exists():
            continue
        raw_procs = _read_small_text(cg_base / unit / 'cgroup.procs', 2048)
        if raw_procs is None:
            continue
        any_found = True
        pids = []
        for line in raw_procs.splitlines():
            s = line.strip()
            if s.isdigit():
                pids.append(int(s))
        if not pids:
            links.append({'id': lid, 'name': label, 'status': 'bad', 'detail': '未运行'})
            continue
        has_ssh = False
        for pid in pids[:8]:
            comm = (_read_small_text(pr_base / str(pid) / 'comm', 64) or '').strip().lower()
            if comm == 'ssh':
                has_ssh = True
                break
        if has_ssh:
            if rn_base is None or (rn_base / up_name).exists():
                links.append({'id': lid, 'name': label, 'status': 'ok', 'detail': '已连通'})
            else:
                links.append({'id': lid, 'name': label, 'status': 'warn', 'detail': '握手中'})
        else:
            links.append({'id': lid, 'name': label, 'status': 'warn', 'detail': '重连中'})

    clash_procs = _read_small_text(cg_base / 'mihomo.service' / 'cgroup.procs', 2048)
    if clash_procs is not None:
        any_found = True
        pids = [int(x.strip()) for x in clash_procs.splitlines() if x.strip().isdigit()]
        if not pids:
            links.append({'id': 'clash', 'name': 'Clash代理', 'status': 'bad', 'detail': '未运行'})
        else:
            listening = None
            net_order = ('tcp6', 'tcp') if pr_base == Path('/proc') else ('tcp', 'tcp6')
            for net_name in net_order:
                raw_tcp = _read_small_text(pr_base / 'net' / net_name, 65536)
                if raw_tcp is not None:
                    if listening is None:
                        listening = False
                    for ln in raw_tcp.splitlines()[1:]:
                        parts = ln.split()
                        if len(parts) >= 4 and parts[1].upper().endswith(':1ED2') and parts[3].upper() == '0A':
                            listening = True
                            break
                if listening:
                    break
            if listening is False:
                links.append({'id': 'clash', 'name': 'Clash代理', 'status': 'bad', 'detail': '端口异常'})
            elif prefer_direct:
                links.append({'id': 'clash', 'name': 'Clash代理', 'status': 'warn', 'detail': '直连兜底'})
            else:
                links.append({'id': 'clash', 'name': 'Clash代理', 'status': 'ok', 'detail': '正常 :7890'})
    return links if any_found else []


_NOTE_CACHE = {}


def _extract_note(note):
    raw_str = note if isinstance(note, str) and len(note) <= 4096 else None
    if raw_str is not None and raw_str in _NOTE_CACHE:
        return _NOTE_CACHE[raw_str]
    if isinstance(note, str):
        try:
            note = json.loads(note or '{}')
        except ValueError:
            note = None
    out = None
    if isinstance(note, dict):
        out = {}
        plan, bill = note.get('planDataMod'), note.get('billingDataMod')
        if isinstance(plan, dict):
            for k, src, lim in (('bandwidth', 'bandwidth', 20), ('traffic_limit', 'trafficVol', 20), ('route', 'networkRoute', 20)):
                if plan.get(src):
                    out[k] = str(plan[src])[:lim]
            tt = str(plan.get('trafficType') or '')
            if tt == '2':
                out['traffic_type'] = '双向'
            elif tt == '1':
                out['traffic_type'] = '单向'
            v6 = str(plan.get('IPv6') or '')
            if v6 in ('0', '1'):
                out['ipv6'] = (v6 == '1')
        if isinstance(bill, dict):
            for k, src, lim in (('price', 'amount', 20), ('cycle', 'cycle', 15), ('expires', 'endDate', 35)):
                if bill.get(src):
                    out[k] = str(bill[src])[:lim]
            ar = str(bill.get('autoRenewal') or '')
            if ar in ('0', '1'):
                out['auto_renewal'] = (ar == '1')
    if raw_str is not None:
        if len(_NOTE_CACHE) >= 64:
            _NOTE_CACHE.pop(next(iter(_NOTE_CACHE)))
        _NOTE_CACHE[raw_str] = out
    return out


_STAMP_CACHE = {}
_FRAC_RE = re.compile(r'\.(\d+)')


def _parse_stamp(value):
    if not isinstance(value, str):
        return None
    if len(value) <= 64 and value in _STAMP_CACHE:
        return _STAMP_CACHE[value]
    stamp = None
    try:
        norm = value.replace('Z', '+00:00')
        parsed = dt.datetime.fromisoformat(norm)
        if parsed.tzinfo is not None:
            stamp = parsed.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            norm = _FRAC_RE.sub(lambda m: '.' + (m.group(1)[:6]).ljust(6, '0'), value.replace('Z', '+00:00'), count=1)
            parsed = dt.datetime.fromisoformat(norm)
            if parsed.tzinfo is not None:
                stamp = parsed.timestamp()
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    if len(value) <= 64:
        if len(_STAMP_CACHE) >= 128:
            _STAMP_CACHE.pop(next(iter(_STAMP_CACHE)))
        _STAMP_CACHE[value] = stamp
    return stamp


def normalize(payload, ids):
    """Viewer count (`online`) is NOT server health; derive health from last_active."""
    if not isinstance(payload, dict):
        raise ValueError('Invalid dashboard object')
    now = number(payload.get('now')) / 1000
    if not now:
        raise ValueError('Missing dashboard timestamp')
    by_id = {}
    raw = payload.get('servers')
    if not isinstance(raw, list):
        raise ValueError('Missing server list')
    id_set = set(ids)
    for item in raw:
        if not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] not in id_set:
            continue
        ident = item['id']
        host, state = item.get('host'), item.get('state')
        stamp = _parse_stamp(item.get('last_active'))
        entry = {'id': ident, 'alive': None if stamp is None else -5 <= now - stamp <= 20,
                 'last_active': stamp or 0}
        if 'name' in item and isinstance(item['name'], str) and item['name'].strip():
            entry['name'] = item['name'].strip()[:80]
        if 'country_code' in item and isinstance(item['country_code'], str) and item['country_code'].strip():
            entry['country'] = item['country_code'].strip()[:2].lower()
        if isinstance(host, dict):
            for key in ('mem_total', 'disk_total'):
                if key in host:
                    val = number(host[key])
                    if val > 0:
                        entry[key] = val
            if 'swap_total' in host:
                entry['swap_total'] = number(host['swap_total'])
            cpu_model, cpu_cores = _parse_cpu_spec(host.get('cpu'))
            if cpu_model:
                entry['cpu_model'] = cpu_model
            if cpu_cores > 0:
                entry['cpu_cores'] = cpu_cores
            for hk, out_k, lim in (('platform', 'platform', 24), ('arch', 'arch', 16), ('virtualization', 'virt', 16)):
                hv = host.get(hk)
                if isinstance(hv, str) and hv.strip():
                    entry[out_k] = hv.strip()[:lim]
        # Missing whole metadata objects mean unchanged; malformed notes must not
        # discard valid expiry information or stop the seven other node updates.
        if 'public_note' in item:
            note_meta = _extract_note(item['public_note'])
            if note_meta:
                entry.update(note_meta)
        # Go omits zero-valued counters inside a present state object.
        if 'state' in item and not isinstance(state, dict):
            entry['alive'] = None
        if isinstance(state, dict):
            entry.update({key: number(state.get(key)) for key in NUMBERS})
        by_id[ident] = entry
    return [by_id[i] for i in ids if i in by_id]


def _fmt_bytes_short(n, rate=False):
    n = max(0.0, float(n or 0))
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    i = 0
    while n >= 1024.0 and i < 4:
        n /= 1024.0
        i += 1
    txt = f'{n:.0f}' if i == 0 or n >= 100 else (f'{n:.1f}' if n >= 10 else f'{n:.2f}')
    return f'{txt} {units[i]}{"/s" if rate else ""}'


def summarize_fleet_billing(servers):
    cyc_mult = {'year': 1.0, 'halfyear': 2.0, 'quarter': 4.0, 'month': 12.0,
                '年': 1.0, '半年': 2.0, '季': 4.0, '月': 12.0}
    usd_year = 0.0
    cny_year = 0.0
    nearest = None
    now_s = time.time()
    for s in servers or ():
        raw_p = str(s.get('price') or '').strip()
        if raw_p:
            m = _PRICE_NUM_RE.search(raw_p)
            if m:
                amt = float(m.group(1))
                cyc = str(s.get('cycle') or 'Year').strip().lower()
                mult = cyc_mult.get(cyc, 1.0)
                low_p = raw_p.lower()
                if '¥' in raw_p or '￥' in raw_p or 'cny' in low_p or 'rmb' in low_p:
                    cny_year += amt * mult
                else:
                    usd_year += amt * mult
        ts = _parse_stamp(s.get('expires'))
        if ts:
            days = math.ceil((ts - now_s) / 86400.0)
            if nearest is None or days < nearest[0]:
                nearest = (days, str(s.get('name') or ''))
    equiv_cny_year = round(usd_year * USD_TO_CNY_REF + cny_year)
    equiv_cny_month = round(equiv_cny_year / 12.0)
    return {
        'usd_year': round(usd_year, 2),
        'cny_year': round(cny_year, 2),
        'equiv_cny_year': equiv_cny_year,
        'equiv_cny_month': equiv_cny_month,
        'nearest': nearest,
    }


_SERVER_VOICE_ALIASES = (
    (('美国小鸡', '美小'), 6),
    (('日本小鸡', '日小'), 15),
    (('美国优化', '美优', '主力', '美国'), 3),
    (('绿云', '日本绿云', '日本'), 12),
    (('法国',), 4),
    (('香港', '港小'), 19),
    (('德国', '德小'), 21),
    (('越南',), 10),
)


def _match_server_by_query(servers, q_low: str):
    for s in servers or ():
        sname = str(s.get('name') or '').lower()
        if sname and sname in q_low:
            return s
    for keys, sid in _SERVER_VOICE_ALIASES:
        if any(k in q_low for k in keys):
            found = next((s for s in (servers or ()) if s.get('id') == sid), None)
            if found:
                return found
    return None


def _fmt_dur_zh(seconds: float) -> str:
    sec = max(1, int(round(float(seconds or 0))))
    if sec < 60:
        return f'{sec} 秒'
    mins = sec // 60
    if mins < 60:
        return f'{mins} 分钟'
    hrs, rem_m = divmod(mins, 60)
    if hrs < 24:
        return f'{hrs} 小时 {rem_m} 分钟' if rem_m else f'{hrs} 小时'
    days, rem_h = divmod(hrs, 24)
    return f'{days} 天 {rem_h} 小时' if rem_h else f'{days} 天'


def _fmt_hm_zh(wall_ts: float) -> str:
    try:
        d_ev = dt.datetime.fromtimestamp(float(wall_ts), tz=SH_TZ)
        d_now = dt.datetime.fromtimestamp(time.time(), tz=SH_TZ)
        delta_days = (d_now.date() - d_ev.date()).days
        prefix = '' if delta_days == 0 else ('昨日' if delta_days == 1 else f'{d_ev.month}月{d_ev.day}日')
        return f'{prefix}{d_ev.hour:02d}点{d_ev.minute:02d}分'
    except Exception:
        return '刚才'


def _detect_isp_in_query(q_low: str) -> str | None:
    if '电信' in q_low:
        return '电信'
    if '移动' in q_low:
        return '移动'
    if '联通' in q_low:
        return '联通'
    return None


def _detect_compare_mode(q_low: str, focus: str = '') -> str | None:
    has_who = any(p in q_low for p in ('哪台', '哪个', '哪一', '谁', '排行', '对比', '比较'))
    if (
        any(k in q_low for k in ('哪台最快', '哪个最快', '延迟最低', '延时最低', 'ping最低', '哪台延迟低', '最快的是', '最流畅', '推荐哪台'))
        or (has_who and any(s in q_low for s in ('最快', '延迟低', '延时低', '延迟最低', '延时最低', '更低', '更快')))
    ):
        return 'lat_low'
    if (
        any(k in q_low for k in ('哪台最慢', '哪个最慢', '延迟最高', '延时最高', 'ping最高', '哪台延迟高', '最卡', '哪台最差'))
        or (has_who and any(s in q_low for s in ('最慢', '最差', '延迟高', '延时高', '延迟最高', '延时最高')))
    ):
        return 'lat_high'
    if any(k in q_low for k in ('磁盘最满', '存储最高', '存储最满', '硬盘最满', '空间最少', '哪台快满了')) or (
        has_who and any(s in q_low for s in ('磁盘满', '硬盘满', '空间少', '最满'))
    ):
        return 'disk_high'
    if any(k in q_low for k in ('内存最高', '内存最满', '内存最大', '内存占用最高')) or (
        has_who and '内存' in q_low and any(s in q_low for s in ('最高', '最大', '最满', '最多'))
    ):
        return 'mem_high'
    if any(k in q_low for k in ('负载最低', '最闲', '最空', '占用最低', 'cpu最低')) or (
        has_who and any(s in q_low for s in ('最闲', '最轻', '负载低'))
    ):
        return 'load_low'
    if focus == 'compare' or any(k in q_low for k in ('负载最高', '最忙', '占用最高', 'cpu最高', '压力最大', '哪台负载高')) or (
        has_who and any(s in q_low for s in ('最忙', '负载高', '压力大'))
    ):
        return 'load_high'
    return None


def resolve_voice_ui_cmd(snap: dict, query: str = '', focus: str = '') -> str:
    q = (str(query or '') + ' ' + str(focus or '')).strip().lower()
    servers = snap.get('servers') or []
    matched = _match_server_by_query(servers, q)
    if matched is not None and isinstance(matched.get('id'), int):
        sid = matched['id']
        if sid == 3 and (focus == 'carriers' or '三网' in q):
            return 'isp'
        ask_isp = _detect_isp_in_query(q)
        if ask_isp == '电信':
            return f'detail_{sid}_ct'
        if ask_isp == '移动':
            return f'detail_{sid}_cm'
        if ask_isp == '联通':
            return f'detail_{sid}_cu'
        return f'detail_{sid}'
    cmp_mode = _detect_compare_mode(q, focus)
    if cmp_mode:
        return 'fleet_lat' if cmp_mode.startswith('lat_') else 'fleet_load'
    if focus == 'expiry' or any(k in q for k in ('到期', '续费', '过期')):
        return 'fleet_exp'
    if focus == 'carriers' or any(k in q for k in ('三网', '延迟', '延时', '电信', '联通', '移动', 'ping')):
        return 'isp'
    if focus in ('billing', 'events') or any(
        k in q for k in ('账单', '年费', '多少钱', '总览', '掉线', '离线记录', '隧道', '代理', 'clash')
    ):
        return 'fleet'
    return 'overview'


def build_voice_summary(snap: dict, query: str = '', focus: str = '') -> str:
    """Format a concise natural-Chinese spoken report from a Monitor.snapshot()."""
    q = (str(query or '') + ' ' + str(focus or '')).strip().lower()
    servers = snap.get('servers') or []
    raw_traffic = snap.get('traffic') or {}
    traffic = raw_traffic if raw_traffic.get('available') else (snap.get('traffic_cached') or raw_traffic)
    extra_traffic = snap.get('extra_traffic') or {}
    carriers = snap.get('carriers') or []
    unicom_map = snap.get('unicom_map') or {}
    isp_map = snap.get('isp_map') or {}
    local_links = snap.get('local_links') or []
    offline_since = snap.get('offline_since') or {}
    events = snap.get('events') or []
    connected = bool(snap.get('connected'))

    def _fmt_quota_zh(num_bytes: int | float, prefer_tb: bool = False) -> str:
        b = int(round(max(0.0, float(num_bytes or 0))))
        if prefer_tb and b >= 1_000_000_000_000 and b % 1_000_000_000_000 == 0:
            return f'{b // 1_000_000_000_000} TB'
        return f'{b / 1e9:.0f} GB'

    def _fmt_rule_sentence(srv_name: str, tr_obj: dict) -> str:
        if not tr_obj or not tr_obj.get('available') or not (tr_obj.get('limit_bytes', 0) > 0):
            return f'{srv_name}的周期流量暂时还在同步中。'
        lim_b = tr_obj['limit_bytes']
        used_gb = tr_obj['used_bytes'] / 1e9
        lim_gb = lim_b / 1e9
        is_local = tr_obj.get('source') == 'local'
        lim_txt = _fmt_quota_zh(lim_b, prefer_tb=is_local)
        pct = min(100.0, max(0.0, 100.0 * used_gb / lim_gb))
        rem_b = tr_obj.get('remaining_bytes')
        approx = '约 ' if tr_obj.get('calibrated') else ''
        rday = tr_obj.get('reset_day')
        reset_txt = f'每月{int(rday)}日零点' if isinstance(rday, int) and 1 <= rday <= 31 else '每月16日'
        daily_txt = ''
        try:
            to_dt = dt.datetime.fromisoformat(tr_obj['to'].replace('Z', '+00:00')).astimezone(SH_TZ)
            if isinstance(rday, int) and 1 <= rday <= 31:
                reset_txt = f'{to_dt.month}月{to_dt.day}日零点（每月{rday}日）'
            else:
                reset_txt = f'{to_dt.month}月{to_dt.day}日零点'
            days_left = (to_dt.timestamp() - time.time()) / 86400.0
            if rem_b is not None and days_left > 0.2:
                per_day = (rem_b / 1e9) / max(0.5, days_left)
                daily_txt = f'，重置前日均可用约 {per_day:.0f} GB'
        except Exception:
            pass
        if rem_b is not None:
            rem_gb = rem_b / 1e9
            rem_txt = f'{rem_b / 1e12:.2f} TB' if (is_local and rem_b >= 1_000_000_000_000) else f'{rem_gb:.0f} GB'
            return (f'{srv_name}本期已用{approx}{used_gb:.0f} GB，总额度 {lim_txt}（已用 {pct:.0f}%），'
                    f'剩余{approx}{rem_txt}，{reset_txt}重置{daily_txt}。')
        return f'{srv_name}本期已记录 {used_gb:.1f} GB（总额度 {lim_txt}），{reset_txt}重置。'

    def _fleet_traffic_sentence() -> str:
        bits = []
        q_sid = traffic.get('server_id', 3)
        for s in servers:
            sid = s.get('id')
            nm = str(s.get('name') or f'#{sid}')
            if sid == q_sid and traffic.get('available') and (traffic.get('limit_bytes') or 0) > 0:
                rem_b = traffic.get('remaining_bytes')
                rem_part = f'，剩约 {rem_b / 1e9:.0f} GB' if rem_b is not None else ''
                r_day = int(s.get('traffic_reset_day') or 16)
                bits.append(f'{nm} {_fmt_quota_zh(traffic["limit_bytes"])}（每月{r_day}日重置{rem_part}）')
            elif str(sid) in extra_traffic and extra_traffic[str(sid)].get('available') is not False and (extra_traffic[str(sid)].get('limit_bytes') or 0) > 0:
                er = extra_traffic[str(sid)]
                r_day = er.get('reset_day') or s.get('traffic_reset_day')
                r_lbl = f'每月{int(r_day)}日重置' if r_day else '按月重置'
                if er.get('remaining_bytes') is not None:
                    rem_part = f'，剩 {er["remaining_bytes"] / 1e9:.0f} GB'
                else:
                    rem_part = f'，已计 {(er.get("used_bytes") or 0) / 1e9:.1f} GB'
                bits.append(f'{nm} {_fmt_quota_zh(er["limit_bytes"], prefer_tb=True)}（{r_lbl}{rem_part}）')
        return ('各服务器月流量与重置时间：' + '；'.join(bits) + '。') if bits else _traffic_sentence()

    def _traffic_sentence():
        return _fmt_rule_sentence('美国优化线路', traffic)

    def _carrier_sentence():
        if not carriers:
            return '美国优化线路的三网延迟数据暂时还未就绪。'
        ordered = sorted(carriers[:3], key=lambda c: _carrier_order_key(str(c.get('name') or '')))
        parts = []
        for c in ordered:
            ms = c.get('latest_ms')
            loss = c.get('loss_pct') or 0.0
            if c.get('stale'):
                ms_s = '数据陈旧'
            else:
                ms_s = f'{ms:.0f} 毫秒' if ms is not None else '超时'
            loss_s = f'（丢包 {loss:.1f}%）' if loss >= 1.0 else ''
            parts.append(f"{c['name']} {ms_s}{loss_s}")
        return '美国优化线路当前三网延迟：' + '，'.join(parts) + '。'

    def _billing_sentence():
        bill = summarize_fleet_billing(servers)
        cost_parts = []
        if bill['usd_year'] > 0:
            u_txt = f"{bill['usd_year']:.0f}" if bill['usd_year'].is_integer() else f"{bill['usd_year']:.2f}"
            cost_parts.append(f'{u_txt} 美元')
        if bill['cny_year'] > 0:
            c_txt = f"{bill['cny_year']:.0f}" if bill['cny_year'].is_integer() else f"{bill['cny_year']:.2f}"
            cost_parts.append(f'{c_txt} 元人民币')
        joined = '加 '.join(cost_parts) if cost_parts else '暂无标价'
        near = bill['nearest']
        near_txt = f'；最近到期的是{near[1]}，还剩 {near[0]} 天' if near else ''
        return (f"当前共 {len(servers)} 台服务器，年度续费账单合计 {joined}，"
                f"折合人民币约每年 {bill['equiv_cny_year']} 元（月均约 {bill['equiv_cny_month']} 元）{near_txt}。")

    def _events_sentence():
        now_w = time.time()
        cur_off = []
        for s in servers:
            if s.get('alive') is False:
                since_ts = offline_since.get(str(s.get('id')))
                if since_ts:
                    cur_off.append(f"{s.get('name')}（自{_fmt_hm_zh(since_ts)}起已离线 {_fmt_dur_zh(now_w - since_ts)}）")
                else:
                    cur_off.append(str(s.get('name')))
        rec_items = [e for e in events if isinstance(e, dict) and e.get('type') == 'recovered'][:2]
        segments = []
        if cur_off:
            segments.append('当前正在离线：' + '、'.join(cur_off) + '。')
        if rec_items:
            r_bits = [
                f"{_fmt_hm_zh(e.get('at', now_w))}{e.get('name', '节点')}恢复在线（曾离线 {_fmt_dur_zh(e.get('duration_s', 60))}）"
                for e in rec_items
            ]
            segments.append('最近恢复记录：' + '；'.join(r_bits) + '。')
        if not segments:
            return f'近期监控期间全部 {len(servers)} 台服务器持续稳定在线，没有掉线记录。'
        return ''.join(segments)

    def _compare_sentence(mode: str, ask_isp: str | None) -> str:
        online = [s for s in servers if s.get('alive') is True]
        if not online:
            return '当前暂无在线服务器可供横向对比。'
        if mode in ('lat_low', 'lat_high'):
            target_isp = ask_isp or '联通'
            scored = []
            for s in online:
                sid_s = str(s.get('id'))
                ms_val = None
                srv_isps = isp_map.get(sid_s)
                if isinstance(srv_isps, dict) and isinstance(srv_isps.get(target_isp), dict):
                    ms_val = srv_isps[target_isp].get('ms')
                if ms_val is None and target_isp == '联通':
                    u_obj = unicom_map.get(sid_s)
                    if isinstance(u_obj, dict):
                        ms_val = u_obj.get('ms')
                if ms_val is None and s.get('id') == 3 and carriers:
                    c_match = next((c for c in carriers if target_isp in str(c.get('name') or '')), None)
                    if c_match:
                        ms_val = c_match.get('latest_ms')
                if isinstance(ms_val, (int, float)) and ms_val > 0:
                    scored.append((float(ms_val), str(s.get('name') or f"#{s.get('id')}")))
            if not scored:
                return f'各节点的{target_isp}探测延迟正在同步中，请几秒后再试。'
            scored.sort(key=lambda x: x[0], reverse=(mode == 'lat_high'))
            top_bits = [f'{nm} {ms:.0f} 毫秒' for ms, nm in scored[:3]]
            if mode == 'lat_low':
                tail = f'；最高的是{scored[-1][1]} {scored[-1][0]:.0f} 毫秒' if len(scored) > 3 else ''
                return f'当前{target_isp}延迟最低的是：' + '、'.join(top_bits) + f'{tail}。'
            tail = f'；最低的是{scored[-1][1]} {scored[-1][0]:.0f} 毫秒' if len(scored) > 3 else ''
            return f'当前{target_isp}延迟最高的是：' + '、'.join(top_bits) + f'{tail}。'
        if mode == 'disk_high':
            scored = []
            for s in online:
                dtot = number(s.get('disk_total'))
                dpct = (100.0 * number(s.get('disk_used')) / dtot) if dtot > 0 else 0.0
                scored.append((dpct, str(s.get('name') or '')))
            scored.sort(key=lambda x: x[0], reverse=True)
            bits = [f'{nm} {pct:.0f}%' for pct, nm in scored[:3]]
            return '当前磁盘占用最高的是：' + '、'.join(bits) + '。'
        if mode == 'mem_high':
            scored = []
            for s in online:
                mtot = number(s.get('mem_total'))
                mpct = (100.0 * number(s.get('mem_used')) / mtot) if mtot > 0 else 0.0
                scored.append((mpct, str(s.get('name') or '')))
            scored.sort(key=lambda x: x[0], reverse=True)
            bits = [f'{nm} {pct:.0f}%' for pct, nm in scored[:3]]
            return '当前内存占用最高的是：' + '、'.join(bits) + '。'
        scored = []
        for s in online:
            cpu_p = number(s.get('cpu'))
            mtot = number(s.get('mem_total'))
            mem_p = (100.0 * number(s.get('mem_used')) / mtot) if mtot > 0 else 0.0
            key_v = cpu_p * 0.7 + mem_p * 0.3
            scored.append((key_v, cpu_p, mem_p, str(s.get('name') or '')))
        scored.sort(key=lambda x: x[0], reverse=(mode != 'load_low'))
        bits = [f'{nm}（CPU {c:.1f}%、内存 {m:.0f}%）' for _, c, m, nm in scored[:3]]
        prefix = '当前最空闲的服务器是：' if mode == 'load_low' else '当前负载最高的服务器是：'
        return prefix + '、'.join(bits) + '。'

    matched_srv = _match_server_by_query(servers, q)
    ask_isp = _detect_isp_in_query(q)
    cmp_mode = _detect_compare_mode(q, focus)

    want_links = matched_srv is None and any(k in q for k in ('隧道', '反向代理', 'clash', '科学上网', '7890'))
    want_events = focus == 'events' or any(k in q for k in ('掉线', '断线', '离线记录', '挂过', '故障记录', '掉过线'))
    want_traffic = focus == 'traffic' or any(k in q for k in ('流量', '额度', '剩多少', '用了多少', '重置', '清零', '套餐包'))
    want_carrier = focus == 'carriers' or any(k in q for k in ('延迟', '延时', '三网', '电信', '联通', '移动', '丢包', 'ping'))
    want_billing = focus == 'billing' or any(k in q for k in ('多少钱', '年费', '账单', '花销', '费用', '成本', '总价'))
    want_expiry = focus == 'expiry' or any(k in q for k in ('到期', '续费', '过期'))

    if want_links and local_links:
        l_bits = [f"{lk.get('name')} {lk.get('detail')}" for lk in local_links if isinstance(lk, dict)]
        if l_bits:
            return '本机链路状态：' + '，'.join(l_bits) + '。'
    if cmp_mode and matched_srv is None:
        return _compare_sentence(cmp_mode, ask_isp)
    if want_events and matched_srv is None:
        return _events_sentence()
    if want_traffic and matched_srv is None and extra_traffic and any(k in q for k in ('所有', '全部', '各', '每台', '哪些', '汇总', '一览', '分别')):
        return _fleet_traffic_sentence()
    if want_traffic and (matched_srv is None or matched_srv.get('id') == traffic.get('server_id', 3)):
        return _traffic_sentence()
    if want_traffic and matched_srv is not None and str(matched_srv.get('id')) in extra_traffic:
        return _fmt_rule_sentence(str(matched_srv.get('name') or '该服务器'), extra_traffic[str(matched_srv['id'])])
    if want_billing and matched_srv is None:
        return _billing_sentence()
    if want_carrier and matched_srv is None:
        return _carrier_sentence()

    if matched_srv is not None:
        s = matched_srv
        alive = s.get('alive')
        if not connected and alive is None:
            return f"{s.get('name', '该服务器')}当前数据正在同步，请稍后再试。"
        if alive is False:
            since_ts = offline_since.get(str(s.get('id')))
            if since_ts:
                dur_txt = _fmt_dur_zh(time.time() - float(since_ts))
                return f"注意：{s.get('name', '该服务器')}自{_fmt_hm_zh(since_ts)}起已离线 {dur_txt}，请检查机器或网络连接。"
            return f"注意：{s.get('name', '该服务器')}目前处于离线状态，请检查机器或网络连接。"
        cpu_pct = number(s.get('cpu'))
        mem_t = number(s.get('mem_total'))
        mem_u = number(s.get('mem_used'))
        mem_pct = (100.0 * mem_u / mem_t) if mem_t > 0 else 0.0
        disk_t = number(s.get('disk_total'))
        disk_u = number(s.get('disk_used'))
        disk_pct = (100.0 * disk_u / disk_t) if disk_t > 0 else 0.0
        sid_str = str(s.get('id'))
        srv_isps = dict(isp_map.get(sid_str) or {})
        if not srv_isps and s.get('id') == 3 and carriers:
            for c in carriers[:3]:
                ckey = _detect_isp_in_query(str(c.get('name') or '')) or str(c.get('name') or '')
                srv_isps[ckey] = {'ms': c.get('latest_ms'), 'loss': c.get('loss_pct') or 0.0, 'status': c.get('status', 'ok'), 'stale': bool(c.get('stale'))}
        if (ask_isp or want_carrier) and srv_isps:
            if ask_isp and ask_isp in srv_isps:
                cur_i = srv_isps[ask_isp]
                ms_v = cur_i.get('ms')
                loss_v = float(cur_i.get('loss') or 0.0)
                if cur_i.get('stale'):
                    lead = f"{s.get('name')}当前{ask_isp}探针数据陈旧"
                elif ms_v is None:
                    lead = f"{s.get('name')}当前{ask_isp}探测超时（丢包 {loss_v:.1f}%）"
                else:
                    loss_note = f'（丢包 {loss_v:.1f}%）' if loss_v >= 1.0 else ''
                    lead = f"{s.get('name')}当前{ask_isp}延迟 {ms_v:.0f} 毫秒{loss_note}"
                other_bits = [
                    f"{k} {srv_isps[k]['ms']:.0f} 毫秒"
                    for k in ('联通', '电信', '移动')
                    if k != ask_isp and k in srv_isps and srv_isps[k].get('ms') is not None and not srv_isps[k].get('stale')
                ]
                peer_txt = ('，同机' + '、'.join(other_bits)) if other_bits else ''
                return f"{lead}{peer_txt}；CPU {cpu_pct:.1f}%，内存 {mem_pct:.0f}%。"
            all_bits = []
            for k in ('联通', '电信', '移动'):
                if k in srv_isps:
                    ci = srv_isps[k]
                    ms_v = ci.get('ms')
                    loss_v = float(ci.get('loss') or 0.0)
                    ms_txt = '数据陈旧' if ci.get('stale') else (f'{ms_v:.0f} 毫秒' if ms_v is not None else '超时')
                    loss_txt = f'（丢包 {loss_v:.1f}%）' if loss_v >= 1.0 else ''
                    all_bits.append(f'{k} {ms_txt}{loss_txt}')
            if all_bits:
                return f"{s.get('name')}当前三网延迟：" + '，'.join(all_bits) + f"；CPU {cpu_pct:.1f}%，内存 {mem_pct:.0f}%。"
        swap_u = number(s.get('swap_used'))
        swap_note = f'，Swap 已用 {_fmt_bytes_short(swap_u)}' if swap_u >= 512 * 1048576 else ''
        uni = unicom_map.get(sid_str)
        uni_note = f"，联通延迟 {uni['ms']:.0f} 毫秒" if isinstance(uni, dict) and uni.get('ms') is not None else ''
        tcp = int(number(s.get('tcp_conn_count')))
        tcp_note = f'，TCP 连接 {tcp} 个' if tcp >= 50 else ''
        exp_note = ''
        if s.get('expires'):
            ts = _parse_stamp(s['expires'])
            if ts:
                rem_s = ts - time.time()
                days = math.ceil(rem_s / 86400.0) if rem_s >= 0 else -1
                exp_note = f'，距离到期还有 {days} 天' if days >= 0 else '，已到期'
        ev_note = ''
        if want_events:
            srv_ev = next((e for e in events if isinstance(e, dict) and e.get('id') == s.get('id') and e.get('type') == 'recovered'), None)
            if srv_ev:
                ev_note = f"，最近曾于{_fmt_hm_zh(srv_ev.get('at', time.time()))}恢复在线（曾离线 {_fmt_dur_zh(srv_ev.get('duration_s', 60))}）"
            else:
                ev_note = '，近期持续稳定未掉线'
        if s.get('id') == traffic.get('server_id', 3):
            extra = ' ' + _traffic_sentence()
        elif sid_str in extra_traffic:
            extra = ' ' + _fmt_rule_sentence(str(s.get('name') or ''), extra_traffic[sid_str])
        else:
            extra = ''
        return (f"{s.get('name')}当前在线，CPU {cpu_pct:.1f}%，内存 {mem_pct:.0f}%{swap_note}，"
                f"存储 {disk_pct:.0f}%{uni_note}，实时上传 {_fmt_bytes_short(s.get('net_out_speed'), True)}、"
                f"下载 {_fmt_bytes_short(s.get('net_in_speed'), True)}{tcp_note}{exp_note}{ev_note}。{extra}").strip()

    if want_expiry:
        soon = []
        nearest = None
        now_s = time.time()
        for s in servers:
            ts = _parse_stamp(s.get('expires'))
            if ts:
                days = math.ceil((ts - now_s) / 86400.0)
                if nearest is None or days < nearest[0]:
                    nearest = (days, s.get('name', ''))
                if days <= 45:
                    soon.append(f"{s.get('name')}（剩 {days} 天）")
        if soon:
            return '近期即将到期的服务器有：' + '、'.join(soon) + '。'
        if nearest:
            return f'所有 {len(servers)} 台服务器到期时间都很充裕，最近到期的是{nearest[1]}，还剩 {nearest[0]} 天。'

    if not connected and not any(s.get('alive') is not None for s in servers):
        return '哪吒监控正在连接中，暂时还没拿到实时状态，请几秒后再问我。'

    total = len(servers)
    online = [s for s in servers if s.get('alive') is True]
    offline = [s for s in servers if s.get('alive') is False]
    parts = []
    if offline:
        off_names = '、'.join(str(s.get('name')) for s in offline)
        parts.append(f'注意：共有 {len(offline)} 台服务器离线（{off_names}），其余 {len(online)} 台在线。')
    elif len(online) < total:
        parts.append(f'当前已同步 {len(online)}/{total} 台服务器在线，其余节点正在同步中。')
    else:
        parts.append(f'{len(online)}/{total} 台服务器全部在线运行正常。')

    hot = []
    for s in online:
        mem_t = number(s.get('mem_total'))
        mem_pct = (100.0 * number(s.get('mem_used')) / mem_t) if mem_t > 0 else 0.0
        disk_t = number(s.get('disk_total'))
        disk_pct = (100.0 * number(s.get('disk_used')) / disk_t) if disk_t > 0 else 0.0
        cpu_pct = number(s.get('cpu'))
        if cpu_pct >= 85:
            hot.append(f"{s.get('name')} CPU {cpu_pct:.0f}%")
        elif mem_pct >= 85:
            hot.append(f"{s.get('name')}内存 {mem_pct:.0f}%")
        elif disk_pct >= 85:
            hot.append(f"{s.get('name')}存储 {disk_pct:.0f}%")
    if hot:
        parts.append('负载较高：' + '、'.join(hot[:2]) + '。')

    if traffic and traffic.get('available') and traffic.get('remaining_bytes') is not None:
        rem_gb = traffic['remaining_bytes'] / 1e9
        parts.append(f'美国优化剩余流量约 {rem_gb:.0f} GB。')
    if carriers:
        ordered = sorted(carriers[:3], key=lambda c: _carrier_order_key(str(c.get('name') or '')))
        c_bits = [f"{c['name']} {c['latest_ms']:.0f}" for c in ordered if c.get('latest_ms') is not None]
        if c_bits:
            parts.append('美优延迟 ' + '、'.join(c_bits) + ' 毫秒。')
    return ''.join(parts)


async def _bounded_json(response, limit: int = 1048576):
    content = getattr(response, 'content', None)
    if content is None or not hasattr(content, 'iter_chunked'):
        return await response.json()
    buf = bytearray()
    async for chunk in content.iter_chunked(65536):
        buf.extend(chunk)
        if len(buf) > limit:
            raise ValueError('Upstream JSON payload exceeds limit')
    return json.loads(buf.decode('utf-8'))


def _read_regular_json(path: Path, max_bytes: int = 262144):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError('Not a regular bounded JSON file')
        chunks, rem = [], max_bytes + 1
        while rem > 0:
            b = os.read(fd, min(65536, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise OSError('JSON file exceeds max_bytes')
        return (st.st_ino, st.st_mtime_ns, st.st_size), json.loads(raw.decode('utf-8'))
    finally:
        os.close(fd)


class Monitor:
    def __init__(self, assets, state_dir=None):
        self.assets = Path(assets)
        self._servers_cfg_path = self.assets / 'servers.json'
        try:
            sig, seed = _read_regular_json(self._servers_cfg_path)
        except (OSError, ValueError):
            sig, seed = None, []
        seed = [s for s in seed if isinstance(s, dict) and 'id' in s] if isinstance(seed, list) else []
        self._servers_cfg_sig = sig
        self._servers_cfg_checked = time.monotonic()
        self.seed = seed
        self.seed_by_id = {s['id']: s for s in self.seed}
        self._seed_fallback = [dict(s, alive=None) for s in self.seed]
        self.ids = [s['id'] for s in self.seed]
        self.servers = []
        self.received_at = None
        self.received_wall = None
        self.connected = False
        self.display = DisplayControl()
        self.last_view = None
        self.wanted = asyncio.Event()   # set by a page poll; the consumer sleeps on it while idle
        self.fresh = asyncio.Event()    # set by each upstream update, cleared on disconnect
        self.wake_retry = asyncio.Event()
        self._traffic_cfg_path = self.assets / 'traffic.json'
        self._traffic_cfg_sig = None
        self._traffic_cfg_checked = 0.0
        self.traffic_config = {}
        self._reload_traffic_config(force=True)
        self.traffic = None
        self._last_valid_traffic = None
        self._last_valid_traffic_mono = 0.0
        self.extra_traffic = {}
        self._last_valid_extra_traffic = {}
        self._last_valid_extra_mono = 0.0
        self.carriers = []
        self._carriers_updated_mono = 0.0
        self.traffic_checked = None
        self._traffic_failures = 0
        self.traffic_lock = asyncio.Lock()
        self.traffic_session = None
        self._traffic_task = None
        self._traffic_to_str = None
        self._traffic_to_ts = 0.0
        self.speed_hist = {}
        self._last_speed_mono = 0.0
        self._srv_latency_cache = {}
        self._srv_latency_fail_at = {}
        self._srv_latency_locks = {}
        self._warm_task = None
        self._idle_probe_task = None
        self._prefer_direct_until = 0.0
        self._local_links_memo = (0.0, [])
        self.open_ui_seq = 0
        self._open_ui_mono = 0.0
        self._open_ui_cmd = ''
        self._alive_state = {}
        self._events = []
        self._events_path = self._resolve_state_file(state_dir, 'events.json')
        self._load_events()
        self._traffic_ledger = {}
        self._ledger_dirty = False
        self._ledger_saved_mono = 0.0
        self._ledger_path = self._resolve_state_file(state_dir, 'traffic_ledger.json')
        self._load_traffic_ledger()
        self._override_cfg_path = self._resolve_state_file(state_dir, 'config.json')
        self._override_sig = None
        self._override_checked = 0.0
        self._override_cfg = {}
        self._eff_override_tuple = None
        self._upstream_gen = 0
        self._host_gen = 0
        self._seq = 0
        self._traffic_seq = 0
        self._state_etag_memo = (None, '')
        self._traffic_cfg_dirty = False

    def _resolve_state_file(self, state_dir, filename: str) -> Path | None:
        if state_dir is not None:
            return Path(state_dir) / filename
        if str(self.assets) == '/usr/local/share/thinksmart-monitor':
            env_sd = (os.environ.get('STATE_DIRECTORY') or '').split(':')[0].strip()
            base = Path(env_sd) if env_sd else Path('/var/lib/thinksmart-monitor')
            if base.is_dir():
                return base / filename
        return None

    def _resolve_events_path(self, state_dir) -> Path | None:
        return self._resolve_state_file(state_dir, 'events.json')

    def _load_traffic_ledger(self):
        if self._ledger_path is None:
            return
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            st = self._ledger_path.lstat()
            self._ledger_stat = (st.st_ino, st.st_mtime_ns)
            _, raw = _read_regular_json(self._ledger_path, max_bytes=65536)
            if isinstance(raw, dict):
                entries = raw.get('servers') if isinstance(raw.get('servers'), dict) else raw
                for k, v in entries.items():
                    try:
                        sid = int(k)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(v, dict) and isinstance(v.get('from'), str) and isinstance(v.get('to'), str):
                        self._traffic_ledger[str(sid)] = {
                            'from': v['from'][:40],
                            'to': v['to'][:40],
                            'acc_in': max(0, int(v.get('acc_in') or 0)),
                            'acc_out': max(0, int(v.get('acc_out') or 0)),
                            'last_in': max(0, int(v.get('last_in') or 0)),
                            'last_out': max(0, int(v.get('last_out') or 0)),
                            'complete': bool(v.get('complete')),
                            'updated_at': float(v.get('updated_at') or 0.0),
                        }

    def _save_traffic_ledger(self):
        if self._ledger_path is None:
            self._ledger_dirty = False
            return
        with contextlib.suppress(OSError, ValueError, TypeError):
            parent = self._ledger_path.parent
            if not parent.is_dir() or parent.is_symlink() or self._ledger_path.is_symlink():
                return
            try:
                cur_st = self._ledger_path.lstat()
                cur_sig = (cur_st.st_ino, cur_st.st_mtime_ns)
            except FileNotFoundError:
                cur_sig = None
            if cur_sig != getattr(self, '_ledger_stat', None):
                self._traffic_ledger.clear()
                self._ledger_stat = None
                if cur_sig is not None:
                    self._load_traffic_ledger()
                self._ledger_dirty = False
                return
            tmp = self._ledger_path.with_name(f'.traffic_ledger.{os.getpid()}.{threading.get_ident()}.json.tmp')
            payload = json.dumps(
                {'servers': self._traffic_ledger},
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8')
            created = False
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                created = True
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if self._ledger_path.is_symlink():
                    raise OSError('Refusing symlink ledger path')
                os.replace(tmp, self._ledger_path)
                created = False
                st_new = self._ledger_path.lstat()
                self._ledger_stat = (st_new.st_ino, st_new.st_mtime_ns)
            finally:
                if created:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
            self._ledger_dirty = False
            self._ledger_saved_mono = time.monotonic()

    def _load_events(self):
        if self._events_path is None:
            return
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            _, raw = _read_regular_json(self._events_path, max_bytes=65536)
            if isinstance(raw, dict):
                st_map = raw.get('alive_state')
                if isinstance(st_map, dict):
                    for k, v in st_map.items():
                        sid = int(k)
                        if isinstance(v, dict) and isinstance(v.get('alive'), bool):
                            self._alive_state[sid] = {
                                'alive': v['alive'],
                                'since': float(v.get('since') or 0.0),
                            }
                evs = raw.get('events')
                if isinstance(evs, list):
                    self._events = [e for e in evs if isinstance(e, dict)][-MAX_EVENTS_HISTORY:]

    def _save_events(self):
        if self._events_path is None:
            return
        with contextlib.suppress(OSError, ValueError, TypeError):
            parent = self._events_path.parent
            if not parent.is_dir() or parent.is_symlink() or self._events_path.is_symlink():
                return
            tmp = self._events_path.with_name(f'.events.{os.getpid()}.{threading.get_ident()}.json.tmp')
            payload = json.dumps(
                {
                    'alive_state': {str(k): v for k, v in self._alive_state.items()},
                    'events': self._events[-MAX_EVENTS_HISTORY:],
                },
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8')
            created = False
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                created = True
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if self._events_path.is_symlink():
                    raise OSError('Refusing symlink events path')
                os.replace(tmp, self._events_path)
                created = False
            finally:
                if created:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)

    def _record_alive_transitions(self, now_wall: float):
        dirty = False
        for s in self.servers:
            sid = s['id']
            alive = s.get('alive')
            if alive not in (True, False):
                continue
            sname = str(s.get('name') or f'#{sid}')[:32]
            last_act = float(s.get('last_active') or 0.0)
            eff_off_at = round(last_act, 1) if (0 < now_wall - last_act < 86400 * 7) else round(now_wall, 1)
            prev = self._alive_state.get(sid)
            if prev is None:
                since_val = round(now_wall, 1) if alive else eff_off_at
                self._alive_state[sid] = {'alive': alive, 'since': since_val}
                dirty = True
                if alive is False:
                    self._events.append({'type': 'offline', 'id': sid, 'name': sname, 'at': since_val})
            elif prev.get('alive') is True and alive is False:
                self._alive_state[sid] = {'alive': False, 'since': eff_off_at}
                self._events.append({'type': 'offline', 'id': sid, 'name': sname, 'at': eff_off_at})
                dirty = True
            elif prev.get('alive') is False and alive is True:
                off_at = float(prev.get('since') or now_wall)
                dur_s = max(1, int(round(max(0.0, now_wall - off_at))))
                self._alive_state[sid] = {'alive': True, 'since': round(now_wall, 1)}
                self._events.append({
                    'type': 'recovered',
                    'id': sid,
                    'name': sname,
                    'at': round(now_wall, 1),
                    'offline_at': round(off_at, 1),
                    'duration_s': dur_s,
                })
                dirty = True
        if len(self._events) > MAX_EVENTS_HISTORY:
            self._events = self._events[-MAX_EVENTS_HISTORY:]
            dirty = True
        if dirty:
            self._save_events()

    def _runtime_override(self) -> dict:
        p = getattr(self, '_override_cfg_path', None)
        if p is None:
            return {}
        now = time.monotonic()
        if now - self._override_checked < 2.0:
            return self._override_cfg
        self._override_checked = now
        try:
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise OSError()
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            if sig != self._override_sig:
                sig, loaded = _read_regular_json(p, max_bytes=16384)
                self._override_sig = sig
                self._override_cfg = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self._override_sig = None
            self._override_cfg = {}
        raw_prx = self._override_cfg.get('nezha_proxy')
        eff = (
            self._clean_nezha_origin(self._override_cfg.get('nezha_host', '')),
            raw_prx.strip() if isinstance(raw_prx, str) else None,
        )
        prev_eff = getattr(self, '_eff_override_tuple', None)
        if prev_eff is not None and eff != prev_eff:
            self._prefer_direct_until = 0.0
            self._upstream_gen = getattr(self, '_upstream_gen', 0) + 1
            if hasattr(self, 'wake_retry'):
                self.wake_retry.set()
            if eff[0] != prev_eff[0]:
                self._host_gen = getattr(self, '_host_gen', 0) + 1
                self._traffic_failures = 0
                self._carriers_updated_mono = 0.0
                self.servers = []
                self.carriers = []
                self._alive_state.clear()
                self._srv_latency_cache.clear()
                if hasattr(self, '_srv_latency_fail_at'):
                    self._srv_latency_fail_at.clear()
                self.speed_hist.clear()
                self.traffic = None
                self._last_valid_traffic = None
                self.extra_traffic = {}
                self._last_valid_extra_traffic = {}
                self.traffic_checked = 0.0
                self.received_at = None
                self.received_wall = None
                self._seq += 1
        self._eff_override_tuple = eff
        return self._override_cfg

    @staticmethod
    def _clean_nezha_origin(raw: str) -> str:
        s = str(raw or '').strip().rstrip('/')
        if not s:
            return ''
        if not s.startswith(('https://', 'http://')):
            s = 'https://' + s
        try:
            sp = urlsplit(s)
            _ = sp.port
        except ValueError:
            return ''
        if sp.scheme not in ('https', 'http') or not sp.netloc or sp.username or sp.password:
            return ''
        return f'{sp.scheme}://{sp.netloc}'

    def _ws_upstream_and_origin(self) -> tuple[str, str]:
        if UPSTREAM == _DEFAULT_UPSTREAM:
            orig = self._clean_nezha_origin(self._runtime_override().get('nezha_host', ''))
            if orig:
                ws_scheme = 'wss' if orig.startswith('https://') else 'ws'
                return f"{ws_scheme}://{orig.split('://', 1)[1]}/api/v1/ws/server", orig
        return UPSTREAM, f'https://{NEZHA_HOST}'

    def _traffic_upstream_url(self) -> str:
        if TRAFFIC_UPSTREAM == _DEFAULT_TRAFFIC_UPSTREAM:
            orig = self._clean_nezha_origin(self._runtime_override().get('nezha_host', ''))
            if orig:
                return f'{orig}/api/v1/service'
        return TRAFFIC_UPSTREAM

    def _server_service_upstream_tpl(self) -> str:
        if SERVER_SERVICE_UPSTREAM == _DEFAULT_SERVER_SERVICE_UPSTREAM:
            orig = self._clean_nezha_origin(self._runtime_override().get('nezha_host', ''))
            if orig:
                return orig + '/api/v1/server/{sid}/service'
        return SERVER_SERVICE_UPSTREAM

    def _proxy_order(self):
        ov = self._runtime_override()
        if 'nezha_proxy' in ov and isinstance(ov.get('nezha_proxy'), str):
            prx = ov['nezha_proxy'].strip() or None
        else:
            prx = os.environ.get('NEZHA_PROXY', 'http://127.0.0.1:7890') or None
        if not prx:
            return (None,)
        if time.monotonic() < self._prefer_direct_until:
            return (None, prx)
        return (prx, None)

    async def _http_get_json(self, url: str, limit: int = 1048576):
        sess = self.traffic_session if self.traffic_session and not self.traffic_session.closed else None
        cm = contextlib.nullcontext(sess) if sess else aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=6.5, sock_connect=3.5), trust_env=False)
        order = self._proxy_order()
        last_exc = None
        async with cm as session:
            for idx, cand in enumerate(order):
                try:
                    async with session.get(url, proxy=cand, allow_redirects=False) as response:
                        response.raise_for_status()
                        data = await _bounded_json(response, limit)
                    if cand is None and len(order) > 1 and order[0] is not None:
                        self._prefer_direct_until = time.monotonic() + 180.0
                        LOG.info('Proxy unreachable; switched upstream HTTP to direct fallback for 180s')
                    elif cand is not None:
                        self._prefer_direct_until = 0.0
                    return data
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                    if isinstance(exc, ValueError) and cand is None:
                        raise
                    last_exc = exc
                    if idx + 1 < len(order):
                        continue
                    raise
        raise last_exc  # pragma: no cover

    async def _ws_connect_with_fallback(self, session, receive_timeout: float):
        order = self._proxy_order()
        ws_url, ws_origin = self._ws_upstream_and_origin()
        last_exc = None
        for idx, cand in enumerate(order):
            try:
                ws = await session.ws_connect(
                    ws_url,
                    origin=ws_origin,
                    proxy=cand,
                    timeout=aiohttp.ClientWSTimeout(ws_receive=receive_timeout, ws_close=1),
                    max_msg_size=1024 * 1024,
                )
                if cand is None and len(order) > 1 and order[0] is not None:
                    self._prefer_direct_until = time.monotonic() + 180.0
                    LOG.info('Proxy unreachable; switched upstream WS to direct fallback for 180s')
                elif cand is not None:
                    self._prefer_direct_until = 0.0
                return ws
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                if isinstance(exc, ValueError) and cand is None:
                    raise
                last_exc = exc
                if idx + 1 < len(order):
                    continue
                raise
        raise last_exc  # pragma: no cover

    def _can_fetch_server_service(self) -> bool:
        if SERVER_SERVICE_UPSTREAM == _DEFAULT_SERVER_SERVICE_UPSTREAM and (
            TRAFFIC_UPSTREAM != _DEFAULT_TRAFFIC_UPSTREAM or UPSTREAM != _DEFAULT_UPSTREAM
        ):
            return False
        return True

    @staticmethod
    def _merge_server_entry(seed_entry: dict, prev_entry: dict | None, live_entry: dict | None) -> dict:
        if live_entry is not None:
            merged = {**seed_entry, **(prev_entry or {}), **live_entry}
        else:
            merged = {**seed_entry, **(prev_entry or {}), 'alive': None}
        if seed_entry.get('traffic_limit_override') and seed_entry.get('traffic_limit'):
            merged['traffic_limit'] = seed_entry['traffic_limit']
        if 'traffic_reset_day' in seed_entry:
            merged['traffic_reset_day'] = seed_entry['traffic_reset_day']
        return merged

    def _reload_servers_config(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._servers_cfg_checked < 2.0:
            return False
        self._servers_cfg_checked = now
        try:
            st = self._servers_cfg_path.lstat()
            if not stat.S_ISREG(st.st_mode):
                return False
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            if sig == self._servers_cfg_sig:
                return False
            sig, loaded = _read_regular_json(self._servers_cfg_path)
            if not isinstance(loaded, list) or not all(
                isinstance(x, dict) and type(x.get('id')) is int for x in loaded
            ):
                return False
            old_seed_by_id = dict(getattr(self, 'seed_by_id', None) or {})
            self._servers_cfg_sig = sig
            self.seed = loaded
            self.seed_by_id = {s['id']: s for s in loaded}
            self._seed_fallback = [dict(s, alive=None) for s in loaded]
            self.ids = [s['id'] for s in loaded]
            if self.servers:
                prev_by_id = {s['id']: s for s in self.servers}
                new_servers = []
                for ident in self.ids:
                    new_s = self.seed_by_id[ident]
                    if ident in prev_by_id:
                        old_s = old_seed_by_id.get(ident)
                        changed = {k: v for k, v in new_s.items() if isinstance(old_s, dict) and old_s and old_s.get(k) != v}
                        new_servers.append(self._merge_server_entry(new_s, None, {**prev_by_id[ident], **changed}))
                    else:
                        new_servers.append(dict(new_s, alive=None))
                self.servers = new_servers
            valid_ids = set(self.ids)
            for attr in ('_srv_latency_cache', '_srv_latency_fail_at', 'speed_hist', '_alive_state'):
                dmap = getattr(self, attr, None)
                if isinstance(dmap, dict):
                    for k in [k for k in dmap if k not in valid_ids]:
                        dmap.pop(k, None)
            ext_tr = getattr(self, 'extra_traffic', None)
            if isinstance(ext_tr, dict):
                valid_id_strs = {str(i) for i in valid_ids}
                for k in [k for k in ext_tr if str(k) not in valid_id_strs]:
                    ext_tr.pop(k, None)
            self._seq += 1
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _reload_traffic_config(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._traffic_cfg_checked < 2.0:
            return False
        self._traffic_cfg_checked = now
        try:
            st = self._traffic_cfg_path.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise OSError()
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            if sig == self._traffic_cfg_sig:
                return False
            sig, loaded = _read_regular_json(self._traffic_cfg_path)
            if isinstance(loaded, dict):
                self._traffic_cfg_sig = sig
                self.traffic_config = loaded
                self._traffic_cfg_dirty = True
                return True
        except (OSError, ValueError):
            if self._traffic_cfg_sig is not None:
                self._traffic_cfg_sig = None
                self.traffic_config = {}
                self._traffic_cfg_dirty = True
                return True
        return False

    def _update_traffic_ledger(self, now_wall: float):
        local_rules = self.traffic_config.get('local_rules') if isinstance(self.traffic_config, dict) else None
        if not isinstance(local_rules, dict) or not local_rules:
            return
        now_dt = dt.datetime.fromtimestamp(now_wall, tz=SH_TZ)
        srv_by_id = {s['id']: s for s in self.servers}
        save_now = False
        for raw_sid, rule in local_rules.items():
            if not isinstance(rule, dict):
                continue
            sid = rule.get('server_id')
            if type(sid) is not int:
                try:
                    sid = int(raw_sid)
                except (TypeError, ValueError):
                    continue
            srv = srv_by_id.get(sid)
            if srv is None or srv.get('alive') is not True:
                continue
            rday = rule.get('reset_day')
            if type(rday) is not int or not (1 <= rday <= 31):
                continue
            cur_in = int(round(max(0.0, number(srv.get('net_in_transfer')))))
            cur_out = int(round(max(0.0, number(srv.get('net_out_transfer')))))
            uptime_s = max(0.0, number(srv.get('uptime')))
            if cur_in == 0 and cur_out == 0 and uptime_s == 0 and number(srv.get('mem_used')) == 0:
                continue
            start_dt, end_dt = compute_billing_cycle(rday, now_dt)
            start_iso, end_iso = start_dt.isoformat(), end_dt.isoformat()
            start_ts = start_dt.timestamp()
            cf_raw = rule.get('complete_from')
            try:
                cf_dt = dt.datetime.fromisoformat(str(cf_raw).replace('Z', '+00:00')) if cf_raw else end_dt
                if cf_dt.tzinfo is None:
                    cf_dt = end_dt
            except ValueError:
                cf_dt = end_dt

            sid_key = str(sid)
            rec = self._traffic_ledger.get(sid_key)
            booted_in_cycle = uptime_s > 0 and (now_wall - uptime_s) >= start_ts
            if rec is None:
                acc_in = cur_in if booted_in_cycle else 0
                acc_out = cur_out if booted_in_cycle else 0
                is_comp = bool(start_dt >= cf_dt and booted_in_cycle and (now_wall - start_ts) < 1800)
                self._traffic_ledger[sid_key] = {
                    'from': start_iso, 'to': end_iso,
                    'acc_in': acc_in, 'acc_out': acc_out,
                    'last_in': cur_in, 'last_out': cur_out,
                    'complete': is_comp, 'updated_at': round(now_wall, 1),
                }
                self._ledger_dirty = True
                save_now = True
            elif rec.get('from') != start_iso:
                prev_in = int(rec.get('last_in') or 0)
                prev_out = int(rec.get('last_out') or 0)
                prev_at = float(rec.get('updated_at') or 0.0)
                near_boundary = prev_at > 0 and 0 <= (start_ts - prev_at) <= 7200 and 0 <= (now_wall - start_ts) <= 7200
                if booted_in_cycle:
                    acc_in, acc_out = cur_in, cur_out
                    is_comp = bool(start_dt >= cf_dt and (near_boundary or (now_wall - start_ts) <= 1800))
                elif cur_in >= prev_in and cur_out >= prev_out and prev_at > 0 and now_wall > prev_at:
                    ratio = 1.0 if prev_at >= start_ts else max(0.0, min(1.0, (now_wall - start_ts) / (now_wall - prev_at)))
                    acc_in = int(round((cur_in - prev_in) * ratio))
                    acc_out = int(round((cur_out - prev_out) * ratio))
                    is_comp = bool(start_dt >= cf_dt and near_boundary)
                else:
                    acc_in, acc_out = 0, 0
                    is_comp = False
                self._traffic_ledger[sid_key] = {
                    'from': start_iso, 'to': end_iso,
                    'acc_in': acc_in, 'acc_out': acc_out,
                    'last_in': cur_in, 'last_out': cur_out,
                    'complete': is_comp, 'updated_at': round(now_wall, 1),
                }
                self._ledger_dirty = True
                save_now = True
            else:
                prev_in = int(rec.get('last_in') or 0)
                prev_out = int(rec.get('last_out') or 0)
                prev_at = float(rec.get('updated_at') or 0.0)
                actually_rebooted = (uptime_s <= 300) or (prev_at > 0 and now_wall > prev_at and uptime_s <= (now_wall - prev_at) + 180)
                rebooted = False
                if cur_in >= prev_in:
                    d_in, new_last_in = cur_in - prev_in, cur_in
                elif not actually_rebooted:
                    d_in, new_last_in = 0, (prev_in if (prev_in - cur_in) < 65536 else cur_in)
                else:
                    d_in, new_last_in = cur_in, cur_in
                    rebooted = True
                if cur_out >= prev_out:
                    d_out, new_last_out = cur_out - prev_out, cur_out
                elif not actually_rebooted:
                    d_out, new_last_out = 0, (prev_out if (prev_out - cur_out) < 65536 else cur_out)
                else:
                    d_out, new_last_out = cur_out, cur_out
                    rebooted = True
                rec['acc_in'] = int(rec.get('acc_in') or 0) + d_in
                rec['acc_out'] = int(rec.get('acc_out') or 0) + d_out
                rec['last_in'] = new_last_in
                rec['last_out'] = new_last_out
                rec['updated_at'] = round(now_wall, 1)
                if rebooted:
                    self._ledger_dirty = True
                    save_now = True
                elif d_in > 0 or d_out > 0:
                    self._ledger_dirty = True

        now_m = time.monotonic()
        flush_interval = 60.0 if self.viewed_recently() else 3600.0
        if save_now or (self._ledger_dirty and now_m - self._ledger_saved_mono >= flush_interval):
            self._save_traffic_ledger()

    def _build_local_extra_traffic(self, now_s: float) -> dict[str, dict]:
        local_rules = self.traffic_config.get('local_rules') if isinstance(self.traffic_config, dict) else None
        if not isinstance(local_rules, dict) or not local_rules:
            return {}
        quota_sid = self.traffic_config.get('server_id', 3)
        now_dt = dt.datetime.fromtimestamp(now_s, tz=SH_TZ)
        out = {}
        for raw_sid, rule in local_rules.items():
            if not isinstance(rule, dict):
                continue
            sid = rule.get('server_id')
            if type(sid) is not int:
                try:
                    sid = int(raw_sid)
                except (TypeError, ValueError):
                    continue
            if sid == quota_sid or sid not in self.seed_by_id:
                continue
            rday = rule.get('reset_day')
            base_limit = rule.get('limit_bytes')
            if type(rday) is not int or not (1 <= rday <= 31) or type(base_limit) is not int or base_limit <= 0:
                continue
            start_dt, end_dt = compute_billing_cycle(rday, now_dt)
            start_iso, end_iso = start_dt.isoformat(), end_dt.isoformat()
            cf_raw = rule.get('complete_from')
            try:
                cf_dt = dt.datetime.fromisoformat(str(cf_raw).replace('Z', '+00:00')) if cf_raw else end_dt
                if cf_dt.tzinfo is None:
                    cf_dt = end_dt
            except ValueError:
                cf_dt = end_dt
            sid_key = str(sid)
            rec = self._traffic_ledger.get(sid_key)
            if isinstance(rec, dict) and rec.get('from') == start_iso:
                acc_in = max(0, int(rec.get('acc_in') or 0))
                acc_out = max(0, int(rec.get('acc_out') or 0))
                complete = bool(rec.get('complete')) and (start_dt >= cf_dt)
            else:
                acc_in = 0
                acc_out = 0
                prev_at = float(rec.get('updated_at') or 0.0) if isinstance(rec, dict) else 0.0
                start_ts = start_dt.timestamp()
                complete = bool(start_dt >= cf_dt and prev_at > 0 and 0 <= (start_ts - prev_at) <= 7200 and 0 <= (now_s - start_ts) <= 7200)
            mode = str(rule.get('mode') or 'both').strip().lower()
            if mode == 'max':
                raw_used = max(acc_in, acc_out)
            elif mode == 'out':
                raw_used = acc_out
            else:
                mode = 'both'
                raw_used = acc_in + acc_out
            used, calibrated, limit, applied_extra = raw_used, False, base_limit, 0
            cal = rule.get('calibration')
            if isinstance(cal, dict) and cal.get('from') == start_iso and cal.get('to') == end_iso:
                baseline, observed = cal.get('used_bytes'), cal.get('observed_bytes', 0)
                extra = cal.get('extra_bytes', 0)
                if all(type(v) is int and v >= 0 for v in (baseline, observed, extra)) and raw_used >= observed:
                    used = baseline + raw_used - observed
                    calibrated = True
                    applied_extra = extra
                    limit = base_limit + extra
            item = {
                'available': True,
                'rule_id': f'local_{sid}',
                'source': 'local',
                'server_id': sid,
                'used_bytes': used,
                'raw_used_bytes': raw_used,
                'used_in_bytes': acc_in,
                'used_out_bytes': acc_out,
                'calibrated': calibrated,
                'limit_bytes': limit,
                'from': start_iso,
                'to': end_iso,
                'reset_day': rday,
                'mode': mode,
                'complete': complete,
                'remaining_bytes': max(0, limit - used) if (complete or calibrated) else None,
            }
            if applied_extra > 0:
                item['extra_bytes'] = applied_extra
            rname = str(rule.get('name') or '').strip()[:32]
            if rname:
                item['rule_name'] = rname
            out[sid_key] = item
        return out

    def _traffic_to_epoch(self, to_str):
        if to_str != self._traffic_to_str:
            ts = dt.datetime.fromisoformat(to_str.replace('Z', '+00:00')).timestamp()
            self._traffic_to_str = to_str
            self._traffic_to_ts = ts
        return self._traffic_to_ts

    async def fetch_server_latency(self, sid: int, force: bool = False):
        self._reload_servers_config()
        if sid not in self.seed_by_id:
            return None
        now = time.monotonic()
        srv = next((s for s in self.servers if s.get('id') == sid), None)
        srv_alive = srv.get('alive') if srv else None
        cached = self._srv_latency_cache.get(sid)
        if cached and not force and now - cached['at'] < 55.0 and cached.get('srv_alive') == srv_alive:
            return cached
        fail_info = getattr(self, '_srv_latency_fail_at', {}).get(sid)
        if not force and fail_info and now - fail_info[0] < 20.0 and fail_info[1] == srv_alive:
            return cached
        if not self._can_fetch_server_service():
            return cached
        lock = self._srv_latency_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            srv = next((s for s in self.servers if s.get('id') == sid), None)
            srv_alive = srv.get('alive') if srv else None
            cached = self._srv_latency_cache.get(sid)
            if cached and not force and now - cached['at'] < 55.0 and cached.get('srv_alive') == srv_alive:
                return cached
            fail_info = getattr(self, '_srv_latency_fail_at', {}).get(sid)
            if not force and fail_info and now - fail_info[0] < 20.0 and fail_info[1] == srv_alive:
                return cached
            fail_map = getattr(self, '_srv_latency_fail_at', None)
            if fail_map is None:
                fail_map = {}
                self._srv_latency_fail_at = fail_map
            my_host_gen = getattr(self, '_host_gen', 0)
            try:
                ss_tpl = self._server_service_upstream_tpl()
                raw_json = await self._http_get_json(ss_tpl.format(sid=sid), 1048576)
                if getattr(self, '_host_gen', 0) != my_host_gen:
                    return self._srv_latency_cache.get(sid)
                now_wall = time.time()
                ref_now = None
                if SERVER_SERVICE_UPSTREAM == _DEFAULT_SERVER_SERVICE_UPSTREAM:
                    ref_now = now_wall
                elif isinstance(raw_json, dict) and isinstance(raw_json.get('data'), list):
                    max_end_ms = 0.0
                    for item in raw_json['data']:
                        if isinstance(item, dict) and isinstance(item.get('created_at'), list) and item['created_at']:
                            max_end_ms = max(max_end_ms, number(item['created_at'][-1]))
                    if max_end_ms > 0 and 0 <= now_wall - (max_end_ms / 1000.0) <= 14400.0:
                        ref_now = now_wall
                carriers = normalize_server_service(raw_json, now_ts=ref_now, server_alive=srv_alive)
                if not carriers:
                    fail_map[sid] = (time.monotonic(), srv_alive)
                    return cached
                unicom = next((c for c in carriers if '联通' in c['name']), carriers[0])
                others = {c['name']: c.get('latest_ms') for c in carriers if c is not unicom}
                entry = {
                    'ok': True,
                    'server_id': sid,
                    'srv_alive': srv_alive,
                    'updated_at': round(now_wall, 1),
                    'at': time.monotonic(),
                    'carriers': carriers,
                    'unicom': unicom,
                    'others': others,
                }
                prev_cs = cached.get('carriers') if cached else None
                prev_sig = tuple(
                    (c.get('name'), c.get('latest_ms'), c.get('loss_pct'), c.get('status'), c.get('stale'))
                    for c in (prev_cs or ()) if isinstance(c, dict)
                ) if prev_cs else None
                cur_sig = tuple(
                    (c.get('name'), c.get('latest_ms'), c.get('loss_pct'), c.get('status'), c.get('stale'))
                    for c in carriers if isinstance(c, dict)
                )
                fail_map.pop(sid, None)
                self._srv_latency_cache[sid] = entry
                if sid == self.traffic_config.get('server_id', 3):
                    self.carriers = carriers
                    self._carriers_updated_mono = entry['at']
                if prev_sig != cur_sig:
                    self._seq += 1
                return entry
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                if getattr(self, '_host_gen', 0) != my_host_gen:
                    return self._srv_latency_cache.get(sid)
                fail_map[sid] = (time.monotonic(), srv_alive)
                return cached

    def _schedule_warm_latencies(self):
        if self.traffic_session is None or self.traffic_session.closed:
            return
        if not self._can_fetch_server_service() or not self.viewed_recently():
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _job():
            await asyncio.sleep(0.35)
            now = time.monotonic()
            for sid in list(self.ids):
                if not self.viewed_recently():
                    break
                cached = self._srv_latency_cache.get(sid)
                if cached and now - cached['at'] < 120.0:
                    continue
                await self.fetch_server_latency(sid)
                await asyncio.sleep(0.6)

        t = loop.create_task(_job())
        t.add_done_callback(lambda fut: not fut.cancelled() and fut.exception())
        self._warm_task = t

    async def refresh_traffic(self):
        async with self.traffic_lock:
            cfg_changed = bool(self._reload_traffic_config(force=True) or getattr(self, '_traffic_cfg_dirty', False))
            self._traffic_cfg_dirty = False
            if not self.traffic_config:
                if cfg_changed and (self.traffic is not None or self.carriers or self.extra_traffic):
                    self.traffic = None
                    self._last_valid_traffic = None
                    self._last_valid_traffic_mono = 0.0
                    self.extra_traffic = {}
                    self._last_valid_extra_traffic = {}
                    self._last_valid_extra_mono = 0.0
                    self.carriers = []
                    self._carriers_updated_mono = 0.0
                    self._traffic_seq += 1
                return
            if cfg_changed:
                self._last_valid_traffic = None
                self._last_valid_traffic_mono = 0.0
                self._last_valid_extra_traffic = {}
                self._last_valid_extra_mono = 0.0
            expired = (self.traffic and self.traffic.get('available') and
                       self._traffic_to_epoch(self.traffic['to']) <= time.time())
            fail_ttls = (3.5, 3.5, 7.0, 15.0, 30.0)
            ttl = 30.0 if (self.traffic and self.traffic.get('available')) else fail_ttls[min(self._traffic_failures, len(fail_ttls) - 1)]
            if not cfg_changed and not expired and self.traffic_checked is not None and time.monotonic() - self.traffic_checked < ttl:
                return
            quota_sid = self.traffic_config.get('server_id', 3)
            raw_payload = None
            my_host_gen = getattr(self, '_host_gen', 0)
            try:
                raw_payload = await self._http_get_json(self._traffic_upstream_url(), 1048576)
                if getattr(self, '_host_gen', 0) != my_host_gen:
                    return
                with contextlib.suppress(Exception):
                    ex_rules = normalize_extra_traffic_rules(
                        raw_payload,
                        skip_rule_id=self.traffic_config.get('rule_id'),
                        valid_sids=self.ids,
                    )
                    self.extra_traffic = ex_rules
                    if ex_rules:
                        self._last_valid_extra_traffic = ex_rules
                        self._last_valid_extra_mono = time.monotonic()
                us_lat = await self.fetch_server_latency(quota_sid) if self._can_fetch_server_service() else None
                if getattr(self, '_host_gen', 0) != my_host_gen:
                    return
                if us_lat and us_lat.get('carriers'):
                    self.carriers = us_lat['carriers']
                    self._carriers_updated_mono = us_lat['at']
                else:
                    self.carriers = normalize_carriers(raw_payload)
                    if self.carriers:
                        self._carriers_updated_mono = time.monotonic()
                self.traffic = normalize_traffic(raw_payload, self.traffic_config)
                if self.traffic and self.traffic.get('available'):
                    self._last_valid_traffic = self.traffic
                    self._last_valid_traffic_mono = time.monotonic()
                self._traffic_failures = 0
                self.traffic_checked = time.monotonic()
                self._traffic_seq += 1
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                if getattr(self, '_host_gen', 0) != my_host_gen:
                    return
                self._traffic_failures += 1
                us_lat = self._srv_latency_cache.get(quota_sid)
                if us_lat and us_lat.get('carriers') and time.monotonic() - us_lat['at'] < 300.0:
                    self.carriers = us_lat['carriers']
                    self._carriers_updated_mono = us_lat['at']
                elif raw_payload is not None:
                    with contextlib.suppress(Exception):
                        self.carriers = normalize_carriers(raw_payload)
                        if self.carriers:
                            self._carriers_updated_mono = time.monotonic()
                self.traffic = {'available': False, 'server_id': quota_sid}
                self.extra_traffic = {}
                self.traffic_checked = time.monotonic()
                self._traffic_seq += 1

    async def _ensure_traffic_wait(self, timeout_s: float):
        if self._traffic_task is None or self._traffic_task.done():
            t = asyncio.create_task(self.refresh_traffic())
            t.add_done_callback(lambda fut: not fut.cancelled() and fut.exception())
            self._traffic_task = t
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._traffic_task), timeout=timeout_s)

    def touch(self, lease: float | None = None):
        now = time.monotonic()
        gap = (now - self.last_view) if self.last_view is not None else VIEW_IDLE
        was_viewed = self.last_view is not None and gap < VIEW_IDLE
        if lease is None or lease >= VIEW_IDLE:
            self.last_view = now
        else:
            short_anchor = now - max(0.0, VIEW_IDLE - float(lease))
            self.last_view = max(self.last_view, short_anchor) if self.last_view is not None else short_anchor
        self.wanted.set()
        if not was_viewed or (not self.connected and gap > 12.0):
            self.wake_retry.set()
        if lease is None:
            self._schedule_warm_latencies()

    def viewed_recently(self):
        last_v = getattr(self, 'last_view', None)
        return last_v is not None and time.monotonic() - last_v < VIEW_IDLE

    def update(self, payload):
        self._reload_servers_config()
        self._reload_traffic_config()
        data = normalize(payload, self.ids)
        prev_by_id = {s['id']: s for s in self.servers}
        by_id = {s['id']: s for s in data}
        self.servers = [
            self._merge_server_entry(self.seed_by_id[ident], prev_by_id.get(ident), by_id.get(ident))
            for ident in self.ids
        ]
        now_m = time.monotonic()
        if self._last_speed_mono and now_m - self._last_speed_mono > 12.0:
            self.speed_hist.clear()
        self._last_speed_mono = now_m
        active_ids = set()
        for s in self.servers:
            sid = s['id']
            active_ids.add(sid)
            if s.get('alive') is True:
                dq = self.speed_hist.get(sid)
                if dq is None:
                    dq = deque(maxlen=60)
                    self.speed_hist[sid] = dq
                dq.append([int(round(number(s.get('net_out_speed')))), int(round(number(s.get('net_in_speed'))))])
            elif s.get('alive') is False:
                self.speed_hist.pop(sid, None)
        for sid in list(self.speed_hist):
            if sid not in active_ids:
                self.speed_hist.pop(sid, None)
        self.received_at = now_m
        self.received_wall = time.time()
        self._record_alive_transitions(self.received_wall)
        self._update_traffic_ledger(self.received_wall)
        self._seq += 1

    def _get_local_links(self, now_m: float) -> list[dict]:
        prev_at, prev_links = self._local_links_memo
        prefer_direct = now_m < self._prefer_direct_until
        if prev_at > 0 and now_m - prev_at < 3.0:
            return prev_links
        links = probe_local_links(prefer_direct=prefer_direct)
        self._local_links_memo = (now_m, links)
        return links

    def snapshot(self):
        self._reload_servers_config()
        self._reload_traffic_config()
        now_m = time.monotonic()
        now_s = time.time()
        age = now_m - self.received_at if self.received_at is not None else None
        fresh = bool(self.connected and age is not None and age < 20)
        if len(self.servers) == len(self.seed):
            servers = self.servers
        elif not self.servers:
            servers = self._seed_fallback
        else:
            by_id = {s['id']: s for s in self.servers}
            servers = [by_id.get(s['id'], fb) for s, fb in zip(self.seed, self._seed_fallback)]
        alive_by_id = {s['id']: s.get('alive') for s in servers}
        traffic = self.traffic
        if traffic and traffic.get('available') and self._traffic_to_epoch(traffic['to']) <= now_s:
            traffic = {'available': False, 'server_id': traffic['server_id']}
        traffic_cached = None
        if (
            self._last_valid_traffic
            and self._last_valid_traffic.get('available')
            and now_m - self._last_valid_traffic_mono < 600.0
            and self._traffic_to_epoch(self._last_valid_traffic['to']) > now_s
            and not (traffic and traffic.get('server_id') and self._last_valid_traffic.get('server_id') != traffic.get('server_id'))
        ):
            traffic_cached = self._last_valid_traffic
        extra_traffic = self._build_local_extra_traffic(now_s)
        src_extra = self.extra_traffic
        if not src_extra and self._last_valid_extra_traffic and now_m - self._last_valid_extra_mono < 600.0:
            src_extra = self._last_valid_extra_traffic
        for sid_k, er in (src_extra or {}).items():
            try:
                if dt.datetime.fromisoformat(str(er['to']).replace('Z', '+00:00')).timestamp() > now_s:
                    extra_traffic[sid_k] = er
            except Exception:
                pass
        unicom_map = {}
        isp_map = {}
        for sid, entry in self._srv_latency_cache.items():
            if sid not in self.seed_by_id or now_m - float(entry.get('at') or 0.0) >= 900.0:
                continue
            if alive_by_id.get(sid) is False:
                continue
            uni = entry.get('unicom')
            if isinstance(uni, dict) and uni.get('latest_ms') is not None and not uni.get('stale'):
                unicom_map[str(sid)] = {
                    'ms': uni['latest_ms'],
                    'status': uni.get('status', 'ok'),
                }
            c_dict = {}
            for c in entry.get('carriers') or ():
                if not isinstance(c, dict):
                    continue
                ckey = _detect_isp_in_query(str(c.get('name') or '')) or str(c.get('name') or '')[:8]
                if not ckey:
                    continue
                c_dict[ckey] = {
                    'ms': None if c.get('stale') else c.get('latest_ms'),
                    'loss': c.get('loss_pct', 0.0),
                    'status': 'bad' if c.get('stale') else c.get('status', 'ok'),
                    'stale': bool(c.get('stale')),
                }
            if c_dict:
                isp_map[str(sid)] = c_dict
        quota_sid = self.traffic_config.get('server_id', 3)
        if quota_sid in self.seed_by_id and str(quota_sid) not in isp_map and self.carriers and alive_by_id.get(quota_sid) is not False:
            c_dict = {}
            for c in self.carriers[:3]:
                if isinstance(c, dict):
                    ckey = _detect_isp_in_query(str(c.get('name') or '')) or str(c.get('name') or '')[:8]
                    if ckey:
                        c_dict[ckey] = {
                            'ms': None if c.get('stale') else c.get('latest_ms'),
                            'loss': c.get('loss_pct', 0.0),
                            'status': 'bad' if c.get('stale') else c.get('status', 'ok'),
                            'stale': bool(c.get('stale')),
                        }
            if c_dict:
                isp_map[str(quota_sid)] = c_dict
        offline_since = {
            str(sid): round(float(v['since']), 1)
            for sid, v in self._alive_state.items()
            if sid in self.seed_by_id and v.get('alive') is False and v.get('since')
        }
        return {
            'connected': fresh,
            'age': round(age, 1) if age is not None else None,
            'received_at': self.received_wall,
            'servers': servers,
            'traffic': traffic,
            'traffic_cached': traffic_cached,
            'extra_traffic': extra_traffic,
            'carriers': self.carriers,
            'speed_hist': {str(k): list(v) for k, v in self.speed_hist.items() if v},
            'unicom_map': unicom_map,
            'isp_map': isp_map,
            'local_links': self._get_local_links(now_m),
            'offline_since': offline_since,
            'events': list(reversed(self._events[-MAX_EVENTS_HISTORY:])),
        }

    def compute_badge(self):
        """Compact alert summary for Home dock '服务器' tile (empty string when healthy)."""
        self._runtime_override()
        self._reload_servers_config()
        self._reload_traffic_config()
        now_m = time.monotonic()
        now_s = time.time()
        ui_fresh = bool(self.open_ui_seq > 0 and now_m - self._open_ui_mono < 15.0)

        def _out(text: str, amber: bool, bad: bool):
            return {
                'ok': True,
                'text': text,
                'amber': amber,
                'bad': bad,
                'open_ui_seq': self.open_ui_seq,
                'open_ui_fresh': ui_fresh,
                'open_ui_cmd': self._open_ui_cmd if ui_fresh else '',
            }

        servers = self.servers if len(self.servers) == len(self.seed) else self._seed_fallback
        age = (now_m - self.received_at) if self.received_at is not None else None
        recent_probe = age is not None and age < 1500.0
        if recent_probe:
            offline = [s for s in servers if s.get('alive') is False]
            if offline:
                return _out(f'{len(offline)}离线', False, True)

        if recent_probe:
            max_disk = 0.0
            max_mem = 0.0
            for s in servers:
                if s.get('alive') is not True:
                    continue
                dtot = number(s.get('disk_total'))
                if dtot > 0:
                    max_disk = max(max_disk, 100.0 * number(s.get('disk_used')) / dtot)
                mtot = number(s.get('mem_total'))
                if mtot > 0:
                    max_mem = max(max_mem, 100.0 * number(s.get('mem_used')) / mtot)
            if max_disk >= 92.0:
                return _out(f'磁盘{min(99, round(max_disk))}%', False, True)
            if max_mem >= 92.0:
                return _out(f'内存{min(99, round(max_mem))}%', False, True)

        min_days = None
        for s in servers:
            ts = _parse_stamp(s.get('expires'))
            if ts:
                rem_s = ts - now_s
                days = math.ceil(rem_s / 86400.0) if rem_s >= 0 else -1
                if min_days is None or days < min_days:
                    min_days = days
        if min_days is not None and min_days <= 3:
            txt = '已到期' if min_days < 0 else f'{min_days}天'
            return _out(txt, False, True)
        if min_days is not None and min_days <= 14:
            return _out(f'{min_days}天', True, False)

        quota_sid = self.traffic_config.get('server_id', 3)
        us_srv = next((s for s in servers if s.get('id') == quota_sid), None)
        carriers_fresh = self.carriers and (
            self._carriers_updated_mono == 0.0 or now_m - self._carriers_updated_mono < 1500.0
        )
        if carriers_fresh and (us_srv is None or us_srv.get('alive') is not False):
            top3 = self.carriers[:3]
            bad_cnt = sum(1 for c in top3 if isinstance(c, dict) and c.get('status') == 'bad')
            uni_bad = any(isinstance(c, dict) and '联通' in str(c.get('name') or '') and c.get('status') == 'bad' for c in top3)
            if len(top3) >= 3 and bad_cnt >= 3:
                return _out('三网故障', False, True)
            if bad_cnt >= 2 or uni_bad:
                return _out('线路波动', True, False)

        return _out('', False, False)

    def _has_offline_server(self) -> bool:
        if any(s.get('alive') is False for s in self.servers):
            return True
        return any(
            sid in self.seed_by_id and v.get('alive') is False
            for sid, v in self._alive_state.items()
        )

    def _next_idle_probe_interval(self, now_wall: float | None = None) -> float:
        if self._has_offline_server():
            return IDLE_PROBE_OFFLINE_INTERVAL
        try:
            d = dt.datetime.fromtimestamp(time.time() if now_wall is None else now_wall, tz=SH_TZ)
            if 0 <= d.hour * 60 + d.minute < 390:
                return IDLE_PROBE_NIGHT_INTERVAL
        except Exception:
            pass
        return IDLE_PROBE_INTERVAL

    async def _probe_once_while_idle(self, recheck_delay: float | None = None):
        if self.viewed_recently() or self.connected:
            return
        my_host_gen = getattr(self, '_host_gen', 0)
        eff_recheck = IDLE_PROBE_RECHECK_DELAY if recheck_delay is None else max(0.0, float(recheck_delay))
        prev_off = {
            sid for sid, v in self._alive_state.items()
            if sid in self.seed_by_id and v.get('alive') is False
        } | {s['id'] for s in self.servers if s.get('alive') is False}
        sess = self.traffic_session if self.traffic_session and not self.traffic_session.closed else None
        cm = contextlib.nullcontext(sess) if sess else aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=max(10.0, 10.0 + eff_recheck), sock_connect=3.5), trust_env=False, **_conn_kw(8))
        chosen_raw = None
        try:
            async with cm as session:
                async with asyncio.timeout(max(10.0, 10.0 + eff_recheck)):
                    ws = await self._ws_connect_with_fallback(session, receive_timeout=8)
                    early_closed = False
                    newly_dead = set()
                    deadline = 0.0
                    async with ws:
                        msg = await ws.receive()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            raw1 = json.loads(msg.data)
                            chosen_raw = raw1
                            if eff_recheck > 0 and not self.viewed_recently():
                                norm1 = normalize(raw1, self.ids)
                                newly_dead = {s['id'] for s in norm1 if s.get('alive') is False and s['id'] not in prev_off}
                                if newly_dead:
                                    deadline = time.monotonic() + eff_recheck
                                    while time.monotonic() < deadline and not self.viewed_recently():
                                        rem = deadline - time.monotonic()
                                        if rem <= 0:
                                            break
                                        try:
                                            m2 = await asyncio.wait_for(ws.receive(), timeout=min(rem + 0.25, 8.0))
                                        except asyncio.TimeoutError:
                                            break
                                        if m2.type == aiohttp.WSMsgType.TEXT:
                                            raw2 = json.loads(m2.data)
                                            chosen_raw = raw2
                                            norm2 = normalize(raw2, self.ids)
                                            newly_dead = {s['id'] for s in norm2 if s.get('alive') is False and s['id'] in newly_dead}
                                            if not newly_dead:
                                                break
                                        elif m2.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                            early_closed = True
                                            break
                    if early_closed and newly_dead and not self.viewed_recently():
                        rem = deadline - time.monotonic()
                        if rem > 0:
                            await asyncio.sleep(rem)
                        if getattr(self, '_host_gen', 0) != my_host_gen:
                            return
                        if not self.viewed_recently() and not self.connected:
                            ws2 = await self._ws_connect_with_fallback(session, receive_timeout=8)
                            async with ws2:
                                m_conf = await ws2.receive()
                                if m_conf.type == aiohttp.WSMsgType.TEXT:
                                    chosen_raw = json.loads(m_conf.data)
        except Exception:
            pass
        if getattr(self, '_host_gen', 0) != my_host_gen:
            return
        if chosen_raw is not None and not self.viewed_recently() and not self.connected:
            with contextlib.suppress(Exception):
                self.update(chosen_raw)
        if not self.viewed_recently() and not self.connected:
            await self.refresh_traffic()

    async def _idle_probe_loop(self):
        await asyncio.sleep(IDLE_PROBE_INITIAL)
        while True:
            if UPSTREAM == _DEFAULT_UPSTREAM and not self.viewed_recently() and not self.connected:
                with contextlib.suppress(Exception):
                    await self._probe_once_while_idle()
            await asyncio.sleep(self._next_idle_probe_interval())

    async def consume(self):
        delay = 1
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=3.5)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False, **_conn_kw(8)) as session:
            while True:
                if not self.viewed_recently():
                    self.wanted.clear()
                    if not self.viewed_recently():   # a poll may have landed in between
                        LOG.info('No viewer; upstream idle until the status page is opened')
                        await self.wanted.wait()
                        LOG.info('Status page opened; connecting upstream')
                        delay = 1
                idle_close = False
                ws = None
                try:
                    self._runtime_override()
                    my_gen = getattr(self, '_upstream_gen', 0)
                    async with asyncio.timeout(12):
                        ws = await self._ws_connect_with_fallback(session, receive_timeout=15)
                    async with ws:
                        async for msg in ws:
                            self._runtime_override()
                            if getattr(self, '_upstream_gen', 0) != my_gen:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.update(json.loads(msg.data))
                                if not self.connected:
                                    LOG.info('Nezha connected; %d configured servers', len(self.servers))
                                self.connected = True
                                self.fresh.set()
                                delay = 1
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                break
                            if not self.viewed_recently():
                                LOG.info('No viewer for %ds; closing upstream', VIEW_IDLE)
                                idle_close = True
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Log categories only: do not copy remote payloads or private URLs.
                    LOG.warning('Upstream unavailable: %s; retry in %ss', type(exc).__name__, delay)
                finally:
                    if ws is not None and not ws.closed:
                        with contextlib.suppress(Exception):
                            await ws.close()
                    self.connected = False
                    self.fresh.clear()
                    if idle_close or not self.viewed_recently():
                        if self._ledger_dirty:
                            self._save_traffic_ledger()
                        _trim_idle_heap()
                        if self.speed_hist:
                            self.speed_hist.clear()
                            self._seq += 1
                if idle_close or not self.viewed_recently():
                    continue
                self.wake_retry.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake_retry.wait(), timeout=delay)
                if self.wake_retry.is_set():
                    delay = 1
                    continue
                delay = min(delay * 2, 30)

    async def _wake_and_snapshot(self, wait_s: float | None = None, lease: float | None = None):
        idle = not self.connected
        self.touch(lease=lease)
        eff_wait = FIRST_DATA_WAIT if wait_s is None else float(wait_s)
        t_wait = max(0.5, eff_wait + 1.0)
        if idle:
            async def _wait_fresh():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.fresh.wait(), eff_wait)
            await asyncio.gather(_wait_fresh(), self._ensure_traffic_wait(t_wait))
        else:
            await self._ensure_traffic_wait(t_wait)
        return self.snapshot()

    def app(self):
        def _is_trusted_host(host_hdr) -> bool:
            if not host_hdr or not isinstance(host_hdr, str):
                return False
            raw = host_hdr.strip()
            if not raw or any(c in raw for c in ('/', '\\', '?', '#', '@', ' ', '\t', '\r', '\n')):
                return False
            try:
                sp = urlsplit(f'//{raw}')
                _ = sp.port
                hn = sp.hostname
            except ValueError:
                return False
            if not hn or sp.username is not None or sp.password is not None or sp.path or sp.query or sp.fragment:
                return False
            if '%' in hn:
                return False
            hn_clean = hn[:-1].lower() if hn.endswith('.') else hn.lower()
            return hn_clean in ('127.0.0.1', 'localhost', '::1')

        _LOOPBACK_ORIGINS = frozenset({
            'http://127.0.0.1:8090', 'http://localhost:8090',
            'http://127.0.0.1:8092', 'http://localhost:8092',
            'http://127.0.0.1:8095', 'http://localhost:8095',
        })

        def _origin_ok(request) -> bool:
            origin = request.headers.get('Origin')
            if origin is None:
                return True
            try:
                po = urlsplit(origin)
                _ = po.port
            except ValueError:
                return False
            if po.scheme not in ('http', 'https') or not po.hostname or po.path or po.query or po.fragment or po.username is not None:
                return False
            if request.host and origin == f'{request.scheme}://{request.host}':
                return True
            if request.remote in ('127.0.0.1', '::1') and origin in _LOOPBACK_ORIGINS:
                return True
            return False

        @web.middleware
        async def host_guard(request, handler):
            if not _is_trusted_host(request.headers.get('Host')):
                raise web.HTTPForbidden()
            origin = request.headers.get('Origin')
            if origin is not None and not _origin_ok(request):
                raise web.HTTPForbidden()
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                if not (
                    request.remote in ('127.0.0.1', '::1')
                    and (
                        (origin is not None and origin in _LOOPBACK_ORIGINS)
                        or (origin is None and request.method in ('GET', 'HEAD')
                            and request.headers.get('Sec-Fetch-Dest') == 'iframe'
                            and request.path == '/')
                    )
                ):
                    raise web.HTTPForbidden()
            return await handler(request)

        app = web.Application(client_max_size=1024, middlewares=[host_guard])
        static_cache = {}

        def _static_file(rel_name, ctype):
            async def handler(request):
                path = self.assets / rel_name
                try:
                    st = path.lstat()
                    if not stat.S_ISREG(st.st_mode) or st.st_size > 1048576:
                        raise OSError()
                    key = (st.st_ino, st.st_mtime_ns, st.st_size)
                    cached = static_cache.get(rel_name)
                    if cached and cached[0] == key:
                        data, etag = cached[1], cached[2]
                    else:
                        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                        try:
                            fst = os.fstat(fd)
                            if not stat.S_ISREG(fst.st_mode) or fst.st_size > 1048576:
                                raise OSError()
                            chunks, rem = [], 1048577
                            while rem > 0:
                                b = os.read(fd, min(65536, rem))
                                if not b:
                                    break
                                chunks.append(b)
                                rem -= len(b)
                            data = b''.join(chunks)
                            if len(data) > 1048576:
                                raise OSError()
                            key = (fst.st_ino, fst.st_mtime_ns, fst.st_size)
                            etag = f'"{fst.st_ino:x}-{fst.st_mtime_ns:x}-{fst.st_size:x}"'
                        finally:
                            os.close(fd)
                        if len(static_cache) >= 16 and rel_name not in static_cache:
                            static_cache.pop(next(iter(static_cache)))
                        static_cache[rel_name] = (key, data, etag)
                except OSError:
                    raise web.HTTPNotFound()
                headers = {'Cache-Control': 'no-cache', 'ETag': etag, 'X-Content-Type-Options': 'nosniff'}
                if ctype == 'text/html':
                    headers['Content-Security-Policy'] = (
                        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                        "connect-src 'self' http://127.0.0.1:8095 http://localhost:8095; "
                        "frame-src 'self' http://127.0.0.1:8092 http://localhost:8092; "
                        "frame-ancestors 'self' http://127.0.0.1:8090 http://localhost:8090 "
                        "http://127.0.0.1:8095 http://localhost:8095"
                    )
                if request.headers.get('If-None-Match') == etag:
                    return web.Response(status=304, headers=headers)
                return web.Response(body=data, content_type=ctype, charset='utf-8', headers=headers)
            return handler

        index = _static_file('index.html', 'text/html')

        async def state(request):
            snap = await self._wake_and_snapshot()
            tr = snap['traffic']
            links_sig = tuple((l['id'], l['status'], l['detail']) for l in snap['local_links'])
            umap_sig = tuple(sorted((k, v.get('ms'), v.get('status')) for k, v in snap['unicom_map'].items()))
            isp_sig = tuple(sorted(
                (sk, tuple(sorted((ik, iv.get('ms'), iv.get('loss'), iv.get('status'), iv.get('stale')) for ik, iv in sv.items())))
                for sk, sv in snap['isp_map'].items() if isinstance(sv, dict)
            ))
            extra_sig = tuple(sorted(
                (k, v.get('from'), v.get('complete'), v.get('used_bytes'), v.get('limit_bytes'), v.get('calibrated'), v.get('remaining_bytes'))
                for k, v in snap['extra_traffic'].items()
            ))
            carriers_sig = tuple(
                (c.get('name'), c.get('latest_ms'), c.get('loss_pct'), c.get('status'), c.get('stale'), c.get('end_ts'))
                for c in snap['carriers'] if isinstance(c, dict)
            )
            mkey = (self._seq, len(self.servers), snap['received_at'], snap['connected'],
                    snap['age'] is not None, self._traffic_seq, bool(tr and tr.get('available')),
                    bool(snap['traffic_cached']), extra_sig, carriers_sig,
                    len(self._events), len(snap['offline_since']), links_sig, umap_sig, isp_sig)
            if self._state_etag_memo[0] == mkey:
                etag = self._state_etag_memo[1]
            else:
                sem = {
                    'connected': snap['connected'],
                    'has_age': snap['age'] is not None,
                    'received_at': snap['received_at'],
                    'servers': snap['servers'],
                    'traffic': snap['traffic'],
                    'traffic_cached': snap['traffic_cached'],
                    'extra_traffic': snap['extra_traffic'],
                    'carriers': snap['carriers'],
                    'speed_hist': snap['speed_hist'],
                    'unicom_map': snap['unicom_map'],
                    'isp_map': snap['isp_map'],
                    'local_links': snap['local_links'],
                    'offline_since': snap['offline_since'],
                    'events': snap['events'],
                }
                sem_bytes = json.dumps(sem, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                etag = f'"{hashlib.blake2b(sem_bytes, digest_size=8).hexdigest()}"'
                self._state_etag_memo = (mkey, etag)
            headers = {'Cache-Control': 'no-store', 'ETag': etag}
            if request.headers.get('If-None-Match') == etag:
                return web.Response(status=304, headers=headers)
            body = json.dumps(snap, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            return web.Response(body=body, content_type='application/json', charset='utf-8', headers=headers)

        async def server_latency(request):
            raw_id = request.query.get('id') or ''
            try:
                sid = int(raw_id)
            except (TypeError, ValueError):
                return web.json_response({'ok': False, 'error': 'invalid id'}, status=400, headers={'Cache-Control': 'no-store'})
            self._reload_servers_config()
            if sid not in self.seed_by_id:
                return web.json_response({'ok': False, 'error': 'unknown server'}, status=404, headers={'Cache-Control': 'no-store'})
            self.touch()
            res = await self.fetch_server_latency(sid)
            if not res:
                return web.json_response({'ok': False, 'server_id': sid}, headers={'Cache-Control': 'no-store'})
            payload = {
                'ok': True,
                'server_id': sid,
                'updated_at': res['updated_at'],
                'unicom': res['unicom'],
                'others': res['others'],
                'carriers': res['carriers'],
            }
            return web.json_response(payload, headers={'Cache-Control': 'no-store'})

        async def badge(request):
            headers = {'Cache-Control': 'no-store'}
            origin = request.headers.get('Origin')
            if origin in _LOOPBACK_ORIGINS:
                headers['Access-Control-Allow-Origin'] = origin
                headers['Vary'] = 'Origin'
            return web.json_response(self.compute_badge(), headers=headers)

        async def voice_summary(request):
            if request.headers.get('Origin') is not None or request.headers.get('Sec-Fetch-Mode') is not None:
                raise web.HTTPForbidden()
            q = str(request.query.get('query') or '')[:80]
            focus = str(request.query.get('focus') or '')[:24]
            want_open = str(request.query.get('open_ui') or '').strip().lower() in ('1', 'true', 'yes')
            lease_arg = None if want_open else VOICE_VIEW_LEASE
            snap = await self._wake_and_snapshot(wait_s=max(FIRST_DATA_WAIT, VOICE_DATA_WAIT), lease=lease_arg)
            q_low = (q + ' ' + focus).lower()
            cmp_mode = _detect_compare_mode(q_low, focus)
            if cmp_mode in ('lat_low', 'lat_high') and self._can_fetch_server_service():
                missing_sids = [
                    s['id'] for s in (snap.get('servers') or [])
                    if isinstance(s.get('id'), int) and s.get('alive') is True and str(s['id']) not in snap.get('isp_map', {})
                ]
                if missing_sids:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            asyncio.gather(*(self.fetch_server_latency(sid) for sid in missing_sids), return_exceptions=True),
                            timeout=2.4,
                        )
                        snap = self.snapshot()
            else:
                matched = _match_server_by_query(snap.get('servers') or [], q_low)
                if matched and isinstance(matched.get('id'), int) and str(matched['id']) not in snap.get('isp_map', {}):
                    if self._can_fetch_server_service():
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(self.fetch_server_latency(matched['id']), timeout=1.5)
                            snap = self.snapshot()
            ui_cmd = ''
            if want_open:
                ui_cmd = resolve_voice_ui_cmd(snap, query=q, focus=focus)
                self._open_ui_cmd = ui_cmd
                self._open_ui_mono = time.monotonic()
                self.open_ui_seq += 1
            txt = build_voice_summary(snap, query=q, focus=focus)
            return web.json_response(
                {'ok': True, 'text': txt, 'connected': snap['connected'], 'open_ui': want_open, 'open_ui_cmd': ui_cmd},
                headers={'Cache-Control': 'no-store'},
            )

        async def health(_):
            age = time.monotonic() - self.received_at if self.received_at is not None else None
            fresh = bool(self.connected and age is not None and age < 20)
            # connected=false with viewer=false is the normal idle state, not a fault
            return web.json_response({'ok': True, 'connected': fresh, 'servers': len(self.servers),
                                      'viewer': self.viewed_recently()})

        async def display_state(_):
            return web.json_response(self.display.snapshot(), headers={'Cache-Control': 'no-store'})

        async def display_change(request):
            # Only the local screen/forwarded preview can write display preferences.
            # No CORS: a different site cannot submit JSON to this loopback API.
            try:
                req_url_host = request.url.host
                req_host = request.host
            except ValueError:
                raise web.HTTPForbidden()
            if req_url_host not in ('127.0.0.1', 'localhost') or request.headers.get('Origin') != f'{request.scheme}://{req_host}':
                raise web.HTTPForbidden()
            if request.content_type != 'application/json':
                raise web.HTTPUnsupportedMediaType()
            try:
                body = await request.json()
                value = (await self.display.preview(body) if request.path.endswith('/preview')
                         else await self.display.change(body))
                return web.json_response(dict(ok=True, **value), headers={'Cache-Control': 'no-store'})
            except (ValueError, TypeError):
                return web.json_response({'ok': False, 'error': '设置无效或传感器暂不可用，请重试'}, status=400, headers={'Cache-Control': 'no-store'})
            except (RuntimeError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
                return web.json_response({'ok': False, 'error': '亮度没有保存，请重试'}, status=503, headers={'Cache-Control': 'no-store'})

        async def lifecycle(_app):
            self.traffic_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6.5, sock_connect=3.5), trust_env=False, **_conn_kw(8))
            task = asyncio.create_task(self.consume())
            probe_task = asyncio.create_task(self._idle_probe_loop())
            self._idle_probe_task = probe_task
            try:
                yield
            finally:
                for bg in (task, probe_task, self._warm_task, self._traffic_task):
                    if bg is not None and not bg.done():
                        bg.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await bg
                self._idle_probe_task = None
                self._warm_task = None
                self._traffic_task = None
                if self._ledger_dirty:
                    self._save_traffic_ledger()
                if self.traffic_session and not self.traffic_session.closed:
                    await self.traffic_session.close()
                self.traffic_session = None

        app.cleanup_ctx.append(lifecycle)
        app.router.add_get('/', index)
        app.router.add_get('/api/state', state)
        app.router.add_get('/api/server_latency', server_latency)
        app.router.add_get('/api/badge', badge)
        app.router.add_get('/api/voice_summary', voice_summary)
        app.router.add_get('/api/health', health)
        app.router.add_get('/api/display', display_state)
        app.router.add_post('/api/display', display_change)
        app.router.add_post('/api/display/preview', display_change)
        app.router.add_get('/assets/home_overlay.js', _static_file('home_overlay.js', 'application/javascript'))
        app.router.add_get('/assets/servers.json', _static_file('servers.json', 'application/json'))
        app.router.add_get('/assets/traffic.json', _static_file('traffic.json', 'application/json'))

        async def static_no_cache(request, response):
            response.headers.setdefault('X-Content-Type-Options', 'nosniff')
            if response.status == 304:
                response.headers['Content-Length'] = '0'
            if request.path.startswith('/assets/'):
                response.headers.setdefault('Cache-Control', 'no-cache')

        app.on_response_prepare.append(static_no_cache)
        app.router.add_static('/assets/', self.assets, follow_symlinks=False)
        return app


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument('--assets', default='/usr/local/share/thinksmart-monitor')
    p.add_argument('--port', type=int, default=8092)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s monitor: %(message)s')
    mon = Monitor(args.assets)
    with contextlib.suppress(OSError):
        md = mon.display.mode
        if md.parent.exists() and not md.parent.is_symlink() and md.parent.stat().st_uid == os.getuid() \
                and not md.parent.stat().st_mode & 0o1000:   # 公用目录（粘滞位）不收紧
            os.chmod(md.parent, 0o700)
        if md.exists() and not md.is_symlink() and md.stat().st_uid == os.getuid():
            os.chmod(md, 0o600)
    web.run_app(mon.app(), host='127.0.0.1', port=args.port, access_log=None)
