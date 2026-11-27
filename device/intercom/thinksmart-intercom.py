#!/opt/thinksmart-addons/venv/bin/python
import asyncio
import glob
import hashlib
import io
import math
import os
import re
import sys
import json
import stat
import struct
import signal
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
import contextlib
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        import audioop as _audioop
    except ImportError:  # pragma: no cover
        _audioop = None
import aiohttp
from aiohttp import web, WSMsgType


def _scale_pcm_s16le(pcm_bytes, factor: float) -> bytes:
    raw = bytes(pcm_bytes) if isinstance(pcm_bytes, memoryview) else pcm_bytes
    if not raw:
        return b""
    if _audioop is not None:
        return _audioop.mul(raw, 2, factor)
    from array import array
    arr = array("h")
    arr.frombytes(raw)
    for idx, sample in enumerate(arr):
        val = int(sample * factor)
        arr[idx] = 32767 if val > 32767 else (-32768 if val < -32768 else val)
    return arr.tobytes()


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

INTERCOM_CALL_FLAG = Path("/run/user/1000/intercom-call-active")
MIRROR_FLAG_PATH = Path("/run/user/1000/uxplay-mirroring-active")
STUDY_SESSION_KEY = web.AppKey("study_session", aiohttp.ClientSession)
INTERCOM_SRV_KEY = web.AppKey("intercom_srv", object)


def _app_item(req_or_app, app_key, legacy_key: str):
    app_obj = req_or_app if isinstance(req_or_app, dict) or hasattr(req_or_app, "router") else getattr(req_or_app, "app", None)
    if app_obj is None:
        return None
    if app_key in app_obj:
        return app_obj[app_key]
    return app_obj.get(legacy_key)


def _read_regular_file(path: Path, max_bytes: int = 8192) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError("Not a regular small file")
        chunks = []
        rem = max_bytes + 1
        while rem > 0:
            b = os.read(fd, min(8192, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise OSError("File exceeds max_bytes")
        return raw.decode("utf-8")
    finally:
        os.close(fd)


def _read_regular_bytes(path: Path, max_bytes: int = 262144):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError("Not a regular small file")
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        chunks = []
        rem = max_bytes + 1
        while rem > 0:
            b = os.read(fd, min(65536, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise OSError("File exceeds max_bytes")
        return sig, raw
    finally:
        os.close(fd)
BACKLIGHT_PATH = Path("/sys/class/backlight/backlight/brightness")
ACCEL_X_PATH = Path("/sys/bus/iio/devices/iio:device0/in_accel_x_raw")
ACCEL_Y_PATH = Path("/sys/bus/iio/devices/iio:device0/in_accel_y_raw")

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#e3dbce">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0a101c">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>联想 View 摄像头</title>
<script>try { const t = localStorage.getItem('ts-theme'); if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch (e) {}</script>
<script>/* 2026-11-20 Claude：管理后台（/admin/）嵌入本页时只显示一块：call 对讲、msg 留言、cd 倒数日 */
try { const em = new URLSearchParams(location.search).get('embed') || ''; if (window.parent !== window && /^(call|msg|cd)$/.test(em)) document.documentElement.dataset.embed = em; } catch (e) {}</script>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/apple-touch-icon.png" sizes="180x180" type="image/png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
  :root {
    --bg: #07090f;
    --surface: #10141d;
    --surface-2: #171c28;
    --line: rgba(255,255,255,0.07);
    --text: #eef2f8;
    --muted: #8a93a6;
    --dim: #5b6477;
    --live: #ff4d5e;
    --ok: #2fd59b;
    --warn: #ffb547;
    --danger: #ff5a5f;
    --accent: #5b8cff;
    --radius: 22px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html { height: 100%; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    overflow-x: hidden;
    -webkit-font-smoothing: antialiased;
    -webkit-user-select: none;
    user-select: none;
  }
  svg.i { width: 24px; height: 24px; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; }

  /* ---------- Header ---------- */
  header {
    padding: calc(10px + env(safe-area-inset-top)) 18px 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .dev { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .dev-icon {
    width: 42px; height: 42px; border-radius: 14px;
    background: linear-gradient(145deg, #222a3b, #131824);
    border: 1px solid var(--line);
    display: flex; align-items: center; justify-content: center;
    color: #c8d3ea;
    flex-shrink: 0;
  }
  .dev-name { font-size: 17px; font-weight: 700; letter-spacing: .2px; }
  .dev-sub { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--dim); flex-shrink: 0; }
  .dot.online { background: var(--ok); box-shadow: 0 0 0 3px rgba(47,213,155,.18); }
  .dot.call { background: var(--warn); box-shadow: 0 0 0 3px rgba(255,181,71,.2); animation: blink 1.2s infinite; }
  @keyframes blink { 50% { opacity: .35; } }
  .hdr-btn {
    width: 40px; height: 40px; border-radius: 13px;
    border: 1px solid var(--line); background: var(--surface);
    color: var(--text); display: flex; align-items: center; justify-content: center; cursor: pointer;
    touch-action: manipulation; transition: transform .14s cubic-bezier(.22,1,.36,1), background-color .14s ease;
  }
  .hdr-btn:active { transform: scale(.94); transition: transform .05s ease-out, background-color .05s ease; }
  .hdr-btn svg.i { width: 20px; height: 20px; }
  .hdr-right { display: flex; gap: 8px; align-items: center; }
  .saver {
    height: 40px; padding: 0 12px; border-radius: 13px; cursor: pointer; white-space: nowrap;
    border: 1px solid var(--line); background: var(--surface); color: var(--muted);
    font-size: 13px; font-weight: 700; font-family: inherit; display: flex; align-items: center; gap: 6px;
    touch-action: manipulation; transition: transform .14s cubic-bezier(.22,1,.36,1), background-color .14s ease;
  }
  .saver:active { transform: scale(.96); transition: transform .05s ease-out, background-color .05s ease; }
  .saver svg.i { width: 17px; height: 17px; }
  .saver.on { background: rgba(47,213,155,.14); border-color: rgba(47,213,155,.45); color: var(--ok); }

  /* ---------- Video stage ---------- */
  .stage-wrap { padding: 0 12px; display: flex; justify-content: center; flex-shrink: 0; }
  .stage {
    position: relative;
    width: 100%;
    aspect-ratio: var(--ar, 4 / 3);
    max-height: 62dvh;
    min-height: 220px;
    border-radius: var(--radius);
    overflow: hidden;
    background: radial-gradient(120% 80% at 50% 40%, #121826 0%, #05070b 70%);
    border: 1px solid var(--line);
    display: flex; align-items: center; justify-content: center;
  }
  #remoteCanvas { width: 100%; height: 100%; object-fit: contain; display: block; }
  .stage::after {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background:
      linear-gradient(to bottom, rgba(0,0,0,.55) 0%, rgba(0,0,0,0) 22%),
      linear-gradient(to top, rgba(0,0,0,.55) 0%, rgba(0,0,0,0) 26%);
  }
  .waiting {
    position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px;
    color: var(--muted); font-size: 14px; z-index: 1;
  }
  .waiting.hide { display: none; }
  .spinner { width: 34px; height: 34px; border-radius: 50%; border: 3px solid rgba(255,255,255,.1); border-top-color: var(--accent); animation: spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .ov { position: absolute; z-index: 3; display: flex; align-items: center; gap: 8px; }
  .ov-tl { top: 12px; left: 12px; }
  .ov-tr { top: 12px; right: 12px; }
  .ov-bl { bottom: 12px; left: 12px; right: 120px; flex-wrap: wrap; }
  .ov-br { bottom: 12px; right: 12px; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px;
    height: 28px; padding: 0 10px; border-radius: 999px;
    background: rgba(8,10,16,.62); border: 1px solid rgba(255,255,255,.12);
    font-size: 12px; font-weight: 600; color: #e7ecf5; white-space: nowrap;
    font-variant-numeric: tabular-nums;
    -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
  }
  .chip svg.i { width: 14px; height: 14px; }
  .chip.live { background: rgba(255,77,94,.92); border-color: transparent; color: #fff; letter-spacing: .6px; }
  .chip.live .rec { width: 7px; height: 7px; border-radius: 50%; background: #fff; animation: blink 1.4s infinite; }
  .chip.off { background: rgba(60,66,82,.8); }
  .chip.warn { background: rgba(255,90,95,.92); border-color: transparent; color: #fff; }
  .chip.info { background: rgba(47,213,155,.92); border-color: transparent; color: #04261a; }
  .chip.hide { display: none; }
  .round {
    width: 40px; height: 40px; border-radius: 50%;
    background: rgba(8,10,16,.62); border: 1px solid rgba(255,255,255,.14);
    color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer;
    touch-action: manipulation; transition: transform .14s cubic-bezier(.22,1,.36,1), background-color .14s ease;
    -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
  }
  .round svg.i { width: 19px; height: 19px; }
  .round:active { transform: scale(.93); transition: transform .05s ease-out, background-color .05s ease; }

  .flash { position: absolute; inset: 0; background: #fff; opacity: 0; pointer-events: none; z-index: 6; }
  .flash.go { animation: flash .45s ease-out; }
  @keyframes flash { 0% { opacity: .85; } 100% { opacity: 0; } }

  .pip-box {
    position: absolute; right: 12px; top: 50px;
    width: 96px; height: 128px; border-radius: 16px; overflow: hidden;
    border: 2px solid rgba(255,255,255,.85); background: #111;
    box-shadow: 0 10px 28px rgba(0,0,0,.55);
    display: none; z-index: 4;
  }
  .pip-box.active { display: block; }
  #localVideo { width: 100%; height: 100%; object-fit: cover; transform: scaleX(-1); }

  /* ---------- Status tiles ---------- */
  .tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 12px 12px 16px; }
  .tile {
    display: flex; align-items: center; gap: 9px;
    background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
    padding: 10px 10px; min-width: 0;
  }
  .tile .ti {
    width: 32px; height: 32px; border-radius: 10px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    background: rgba(47,213,155,.12); color: var(--ok);
  }
  .tile .ti svg.i { width: 17px; height: 17px; }
  .tile b { display: block; font-size: 11.5px; color: var(--muted); font-weight: 600; }
  .tile span { display: block; font-size: 13.5px; font-weight: 700; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile.warn .ti { background: rgba(255,90,95,.14); color: var(--danger); }
  .tile.warn span { color: #ff8a8e; }
  .tile.busy .ti { background: rgba(255,181,71,.14); color: var(--warn); }
  .tile.busy span { color: var(--warn); }

  /* ---------- Snapshot strip ---------- */
  .shots { padding: 0 12px 14px; flex-shrink: 0; display: flex; flex-direction: column; }
  .shots-hd { display: flex; align-items: baseline; justify-content: space-between; padding: 0 4px 8px; }
  .shots-hd b { font-size: 14px; }
  .shots-hd span { font-size: 12px; color: var(--muted); }
  .shots-row { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 2px; }
  .shots-row::-webkit-scrollbar { height: 0; }
  .shot { flex: 0 0 auto; width: 104px; aspect-ratio: 4 / 3; border-radius: 12px; overflow: hidden; border: 1px solid var(--line); background: #000; cursor: pointer; position: relative; touch-action: manipulation; }
  .shot img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .shot i { position: absolute; left: 6px; bottom: 5px; font-style: normal; font-size: 10.5px; font-weight: 700; color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,.8); font-variant-numeric: tabular-nums; }
  .shots-empty {
    border: 1.5px dashed rgba(255,255,255,.1); border-radius: 14px; padding: 16px; text-align: center;
    color: var(--dim); font-size: 12.5px; display: flex; align-items: center; justify-content: center; gap: 8px;
  }
  .shots-empty svg.i { width: 18px; height: 18px; }
  body.theater .shots { display: none; }

  /* ---------- Controls ---------- */
  .dock {
    margin-top: auto;
    padding: 16px 14px calc(16px + env(safe-area-inset-bottom));
    background: var(--surface);
    border-top: 1px solid var(--line);
    border-radius: 26px 26px 0 0;
  }
  .ctl-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
  .ctl {
    background: none; border: none; color: var(--text); cursor: pointer;
    display: flex; flex-direction: column; align-items: center; gap: 7px;
    font-size: 12px; font-weight: 600; font-family: inherit; touch-action: manipulation;
  }
  .ctl .orb {
    position: relative; width: 58px; height: 58px; border-radius: 50%;
    background: var(--surface-2); border: 1px solid var(--line);
    display: flex; align-items: center; justify-content: center;
    transition: background .18s ease, color .18s ease, transform .14s cubic-bezier(.22,1,.36,1);
    overflow: hidden;
  }
  .ctl:active .orb { transform: scale(.92); transition: transform .05s ease-out, background .05s ease; }
  .ctl.on .orb { background: var(--accent); border-color: transparent; color: #fff; box-shadow: 0 6px 18px rgba(91,140,255,.35); }
  .ctl.muted .orb { background: rgba(255,90,95,.16); border-color: rgba(255,90,95,.35); color: var(--danger); }
  .ctl.muted .lbl { color: #ff8a8e; }
  .ctl:disabled { opacity: .35; cursor: default; }
  .ctl:disabled .orb { transform: none; }
  .ctl .lbl { color: var(--muted); }
  .ctl.on .lbl { color: var(--text); }
  .vu-bar {
    position: absolute; left: 0; bottom: 0; height: 100%; width: 100%;
    background: rgba(255,255,255,.18); transform-origin: left center; transform: scaleX(0);
    transition: transform .08s linear; pointer-events: none;
  }
  .ctl .orb svg.i { position: relative; z-index: 1; }

  .call-btn {
    margin-top: 16px; width: 100%; height: 58px; border-radius: 20px; border: none; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 10px;
    font-size: 16.5px; font-weight: 700; font-family: inherit; color: #fff;
    background: linear-gradient(135deg, #2fd59b 0%, #12b07c 100%);
    box-shadow: 0 10px 26px rgba(47,213,155,.28);
    touch-action: manipulation; transition: transform .14s cubic-bezier(.22,1,.36,1);
  }
  .call-btn:active { transform: scale(.97); transition: transform .05s ease-out; }
  .call-btn.stop { background: linear-gradient(135deg, #ff6a6f 0%, #e5484d 100%); box-shadow: 0 10px 26px rgba(255,90,95,.3); }
  .call-btn:disabled { background: var(--surface-2); color: var(--muted); box-shadow: none; }
  .call-btn .sub { font-weight: 500; opacity: .85; font-size: 13px; font-variant-numeric: tabular-nums; }
  .hint { text-align: center; font-size: 11.5px; color: var(--dim); margin-top: 10px; }

  .toast {
    position: fixed; left: 50%; top: calc(16px + env(safe-area-inset-top)); transform: translate(-50%, -140%);
    background: rgba(20,24,34,.96); border: 1px solid var(--line); color: var(--text);
    padding: 11px 18px; border-radius: 14px; font-size: 14px; font-weight: 600; z-index: 50;
    box-shadow: 0 12px 30px rgba(0,0,0,.5); transition: transform .3s ease; max-width: 88vw; text-align: center;
  }
  .toast.show { transform: translate(-50%, 0); }

  /* ---------- Theater / landscape ---------- */
  body.theater header, body.theater .tiles { display: none; }
  body.theater .stage-wrap { position: fixed; inset: 0; padding: 0; z-index: 20; background: #000; }
  body.theater .stage { border-radius: 0; border: none; aspect-ratio: auto; max-height: none; height: 100%; }
  body.theater .dock {
    position: fixed; left: 50%; bottom: calc(14px + env(safe-area-inset-bottom)); transform: translateX(-50%);
    z-index: 30; margin: 0; padding: 10px 14px; border-radius: 22px;
    background: rgba(12,15,22,.78); border: 1px solid rgba(255,255,255,.1);
    -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
    display: flex; align-items: center; gap: 12px; width: auto;
  }
  body.theater .ctl-row { display: flex; gap: 10px; }
  body.theater .ctl .lbl, body.theater .hint { display: none; }
  body.theater .ctl .orb { width: 50px; height: 50px; }
  body.theater .call-btn { margin: 0; width: auto; height: 50px; padding: 0 20px; font-size: 14.5px; border-radius: 999px; }
  body.theater .call-btn .sub { display: none; }
  body.theater .ov-tl { top: calc(12px + env(safe-area-inset-top)); left: calc(12px + env(safe-area-inset-left)); }
  body.theater .ov-br { bottom: auto; top: calc(12px + env(safe-area-inset-top)); right: calc(12px + env(safe-area-inset-right)); }
  body.theater .ov-tr { top: calc(12px + env(safe-area-inset-top)); right: calc(112px + env(safe-area-inset-right)); }
  body.theater .ov-bl { bottom: calc(92px + env(safe-area-inset-bottom)); left: calc(12px + env(safe-area-inset-left)); }
  body.theater .pip-box { top: auto; bottom: calc(92px + env(safe-area-inset-bottom)); }
  @media (max-width: 500px) {
    body.theater .ov-tr { top: calc(56px + env(safe-area-inset-top)); right: calc(12px + env(safe-area-inset-right)); }
  }

  @media (min-width: 720px) {
    body:not(.theater) { max-width: 560px; margin: 0 auto; }
  }
  /* ---------- Message board (round 109) ---------- */
  .msgs { padding: 0 12px 14px; }
  body.theater .msgs { display: none; }
  .msg-compose { display: flex; gap: 8px; align-items: stretch; }
  #msgInput {
    flex: 1; resize: none; min-height: 54px; max-height: 120px; padding: 10px 12px; border-radius: 14px;
    border: 1px solid var(--line); background: var(--surface); color: var(--text); font-family: inherit;
    font-size: 15px; line-height: 1.4; -webkit-user-select: text; user-select: text; outline: none;
  }
  #msgInput:focus { border-color: rgba(91,140,255,.55); }
  .msg-send { width: 70px; border: none; border-radius: 14px; background: var(--accent); color: #fff; font-size: 15px; font-weight: 700; font-family: inherit; cursor: pointer; touch-action: manipulation; transition: transform .14s cubic-bezier(.22,1,.36,1); }
  .msg-send:active:not(:disabled), .msg-mic:active:not(:disabled) { transform: scale(.95); transition: transform .05s ease-out; }
  .msg-send:disabled, .msg-mic:disabled { background: var(--surface-2); color: var(--muted); }
  .msg-mic { width: 52px; border: 1px solid rgba(47,213,155,.35); border-radius: 14px; background: rgba(47,213,155,.14); color: #5ef2be; display: flex; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; touch-action: manipulation; transition: transform .14s cubic-bezier(.22,1,.36,1); }
  .msg-mic svg.i { width: 22px; height: 22px; }
  .vrec-bar { display: none; align-items: center; gap: 8px; margin-top: 8px; padding: 9px 12px; border-radius: 14px; background: rgba(255,90,95,.13); border: 1px solid rgba(255,90,95,.35); font-size: 13.5px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .vrec-bar.on { display: flex; }
  .vrec-dot { width: 9px; height: 9px; border-radius: 50%; background: #ff5a5f; flex-shrink: 0; }
  #vrecTime { flex: 1; color: #ffd6d8; }
  .vrec-cancel, .vrec-go { height: 34px; padding: 0 12px; border-radius: 10px; border: none; font-family: inherit; font-size: 13px; font-weight: 700; cursor: pointer; }
  .vrec-cancel { background: rgba(255,255,255,.1); color: var(--text); }
  .vrec-go { background: #2fd59b; color: #05261a; }
  .msg-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
  .msg-item { display: flex; gap: 10px; align-items: flex-start; padding: 9px 12px; border-radius: 14px; background: var(--surface); border: 1px solid var(--line); contain: content; }
  .msg-item.from-screen { border-color: rgba(47,213,155,.32); background: rgba(47,213,155,.07); }
  .msg-item .mt { flex: 1; min-width: 0; font-size: 14px; line-height: 1.45; word-break: break-word; white-space: pre-line; -webkit-user-select: text; user-select: text; }
  .msg-src { display: inline-block; padding: 1px 6px; margin-right: 6px; border-radius: 6px; font-size: 11px; font-weight: 700; background: rgba(47,213,155,.2); color: #7bf5ca; vertical-align: 1px; }
  .v-pill { display: inline-flex; align-items: center; gap: 7px; height: 34px; padding: 0 13px; border-radius: 17px; border: 1px solid rgba(91,140,255,.4); background: rgba(91,140,255,.16); color: #dce8ff; font-family: inherit; font-size: 13.5px; font-weight: 700; cursor: pointer; }
  .msg-item.from-screen .v-pill { border-color: rgba(47,213,155,.45); background: rgba(47,213,155,.18); color: #d6fff0; }
  .v-pill.playing { background: #2fd59b; color: #042419; border-color: transparent; }
  .msg-item .mtime { display: block; font-size: 11.5px; color: var(--dim); margin-top: 3px; }
  .msg-item .ms { flex-shrink: 0; font-size: 12px; font-weight: 700; color: var(--warn); white-space: nowrap; margin-top: 2px; }
  .msg-item.read .ms { color: var(--ok); }
  /* 2026-11-12: who, transcripts, reminders, quick replies */
  .msg-who { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
  .msg-who button { height: 32px; padding: 0 12px; border-radius: 16px; border: 1px solid var(--line); background: var(--surface); color: var(--text); font-family: inherit; font-size: 13.5px; cursor: pointer; }
  .msg-who button.on { background: var(--accent); border-color: transparent; color: #fff; font-weight: 700; }
  .msg-who.need button { border-color: rgba(255,181,71,.6); }
  .msg-time { width: 52px; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); color: var(--muted); font-size: 20px; cursor: pointer; flex-shrink: 0; }
  .msg-time.on { background: rgba(255,181,71,.18); border-color: rgba(255,181,71,.5); color: #ffd28a; }
  .msg-when { display: none; align-items: center; gap: 8px; margin-top: 8px; font-size: 13px; color: #ffd28a; flex-wrap: wrap; }
  .msg-when.on { display: flex; }
  .msg-when select, .msg-when input { height: 34px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface); color: var(--text); font-family: inherit; font-size: 14px; padding: 0 8px; }
  .msg-item .who { font-weight: 700; margin-right: 6px; color: #b9cbff; }
  .msg-item.from-screen .who { color: #7bf5ca; }
  .msg-item .tr { display: block; margin-top: 5px; font-size: 13.5px; color: var(--text); opacity: .86; }
  .msg-item.remind { border-color: rgba(255,181,71,.35); background: rgba(255,181,71,.06); }
  .msg-item .rrecall, .msg-item .rdel, .msg-manage { margin-left: 8px; height: 26px; padding: 0 10px; border-radius: 9px; border: 1px solid var(--line); background: transparent; color: var(--muted); font-family: inherit; font-size: 12px; cursor: pointer; }
  .msg-item .rdel, .msg-manage.on { border-color: rgba(255,90,95,.4); color: #ff9a9d; }
  .msg-item .rdel.sure { background: rgba(255,90,95,.18); }
  .msg-item .tr.dim { opacity: .55; }
  .msg-hd-r { display: flex; align-items: center; }
  .msg-item .rcancel { margin-left: 8px; height: 26px; padding: 0 10px; border-radius: 9px; border: 1px solid rgba(255,90,95,.4); background: transparent; color: #ff9a9d; font-family: inherit; font-size: 12px; cursor: pointer; }
  .msg-quick { margin-top: 10px; font-size: 13px; color: var(--muted); }
  .msg-quick summary { cursor: pointer; padding: 4px 0; }
  .msg-quick textarea { width: 100%; box-sizing: border-box; margin-top: 6px; min-height: 120px; padding: 8px 10px; border-radius: 12px; border: 1px solid var(--line); background: var(--surface); color: var(--text); font-family: inherit; font-size: 14px; line-height: 1.5; }
  .msg-quick button { margin-top: 6px; height: 34px; padding: 0 16px; border-radius: 10px; border: none; background: var(--accent); color: #fff; font-family: inherit; font-weight: 700; cursor: pointer; }
  /* ===== 2026-11-13 (Claude): the home screen's look on the phone page. Same colours and solid cards, no gradients or glows;
     light or dark follows the phone's own setting. A layer at the end: the rules above stay as they were. ===== */
  :root {
    --bg: #0a101c; --surface: #121a2b; --surface-2: #1d2840; --line: #232e45;
    --text: #f2f5fb; --muted: #b4bfd4; --dim: #8a96ad;
    --ok: #3ddc97; --warn: #f5c04a; --danger: #ff6b6b; --accent: #5ab8ff; --radius: 24px;
    --on-fill: #0a101c; --edge: transparent;
    --ok-soft: #14291f; --ok-ink: #8ff0c4; --acc-soft: #17243a; --acc-ink: #a9dcff;
    --warn-soft: #2f2814; --warn-ink: #f5d27a; --bad-soft: #33201f; --bad-ink: #ffb3b3; --bad-line: #6a3036;
    --vu: rgba(255,255,255,.18); --toast-bg: #1b2538; --toast-fg: #f2f5fb;
  }
  /* light: the phone's setting (自动) unless the page switch says 浅色 or 深色 */
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg: #e3dbce; --surface: #f6f2ea; --surface-2: #e8e1d5; --line: #d6cdbf;
      --text: #18191b; --muted: #46484d; --dim: #5a5c61;
      --ok: #216e37; --warn: #926400; --danger: #bf2a2a; --accent: #0b7470;
      --on-fill: #faf8f4; --edge: #d6cdbf;
      --ok-soft: #d2e6d6; --ok-ink: #216e37; --acc-soft: #c7dfda; --acc-ink: #0b6864;
      --warn-soft: #eed9b6; --warn-ink: #84510a; --bad-soft: #efc9c9; --bad-ink: #a82626; --bad-line: #dfa6a6;
      --vu: rgba(24,25,27,.14); --toast-bg: #18191b; --toast-fg: #f8f6f2;
    }
    /* full-screen video keeps its dark floating bar */
    :root:not([data-theme="dark"]) body.theater .dock {
      --text: #f2f5fb; --muted: #b4bfd4; --surface-2: #1d2840; --accent: #5ab8ff; --on-fill: #0a101c; --ok: #3ddc97; --danger: #ff6b6b;
      --bad-soft: #33201f; --bad-ink: #ffb3b3; --bad-line: #6a3036; --vu: rgba(255,255,255,.18);
    }
  }
  :root[data-theme="light"] {
      --bg: #e3dbce; --surface: #f6f2ea; --surface-2: #e8e1d5; --line: #d6cdbf;
      --text: #18191b; --muted: #46484d; --dim: #5a5c61;
      --ok: #216e37; --warn: #926400; --danger: #bf2a2a; --accent: #0b7470;
      --on-fill: #faf8f4; --edge: #d6cdbf;
      --ok-soft: #d2e6d6; --ok-ink: #216e37; --acc-soft: #c7dfda; --acc-ink: #0b6864;
      --warn-soft: #eed9b6; --warn-ink: #84510a; --bad-soft: #efc9c9; --bad-ink: #a82626; --bad-line: #dfa6a6;
      --vu: rgba(24,25,27,.14); --toast-bg: #18191b; --toast-fg: #f8f6f2;
  }
  :root[data-theme="light"] body.theater .dock {
      --text: #f2f5fb; --muted: #b4bfd4; --surface-2: #1d2840; --accent: #5ab8ff; --on-fill: #0a101c; --ok: #3ddc97; --danger: #ff6b6b;
      --bad-soft: #33201f; --bad-ink: #ffb3b3; --bad-line: #6a3036; --vu: rgba(255,255,255,.18);
  }
  :root[data-theme="dark"] { color-scheme: dark; }
  :root[data-theme="light"] { color-scheme: light; }
  .hdr-btn.theme-btn { position: relative; }
  .hdr-btn.theme-btn .i { width: 21px; height: 21px; }
  .dev-name { white-space: nowrap; }
  @media (max-width: 400px) { header .dev-icon { display: none; } }
  .toast { transition: transform .3s ease, opacity .3s ease; }
  .toast:not(.show) { opacity: 0; }
  body.theater .call-btn { white-space: nowrap; }
  @media (max-width: 440px) { body.theater .dock { gap: 8px; padding: 8px 10px; } body.theater .ctl-row { gap: 8px; } body.theater .ctl .orb { width: 46px; height: 46px; } body.theater .call-btn { width: 54px; padding: 0; justify-content: center; } body.theater .call-btn #txtCall { display: none; } }
  .dev-icon { background: var(--surface-2); border: none; color: var(--muted); }
  .dot.online, .dot.call { box-shadow: none; }
  .hdr-btn, .saver { height: 44px; border-radius: 14px; border: 1px solid var(--edge); background: var(--surface); }
  .hdr-btn { width: 44px; }
  .saver.on { background: var(--ok-soft); border-color: transparent; box-shadow: inset 0 0 0 1.5px var(--ok); color: var(--ok-ink); }
  .stage { background: #05070b; border: none; }
  .waiting { color: #b4bfd4; }
  .tile { border: 1px solid var(--edge); border-radius: 18px; }
  .tile .ti { border-radius: 12px; background: var(--ok-soft); color: var(--ok); }
  .tile.warn .ti { background: var(--bad-soft); color: var(--danger); }
  .tile.warn span { color: var(--bad-ink); }
  .tile.busy .ti { background: var(--warn-soft); color: var(--warn); }
  .tile.busy span { color: var(--warn-ink); }
  .shots-hd b { font-size: 15px; }
  .shot { border-color: var(--line); }
  .shots-empty { border-color: var(--line); color: var(--dim); border-radius: 16px; }
  .dock { border-top: 1px solid var(--edge); border-radius: 28px 28px 0 0; }
  .ctl .orb { border: none; }
  .ctl.on .orb { background: var(--accent); color: var(--on-fill); box-shadow: none; }
  .ctl.muted .orb { background: var(--bad-soft); border: none; box-shadow: inset 0 0 0 1.5px var(--bad-line); color: var(--danger); }
  .ctl.muted .lbl { color: var(--bad-ink); }
  .vu-bar { background: var(--vu); }
  .call-btn { height: 60px; border-radius: 22px; background: var(--ok); color: var(--on-fill); box-shadow: none; }
  .call-btn.stop { background: var(--danger); box-shadow: none; }
  .call-btn:disabled { background: var(--surface-2); color: var(--muted); }
  .toast { background: var(--toast-bg); color: var(--toast-fg); border: none; box-shadow: 0 8px 20px rgba(0,0,0,.28); }
  /* 留言到屏幕 */
  #msgInput { border: 1px solid var(--edge); border-radius: 16px; }
  #msgInput:focus { border-color: var(--accent); }
  .msg-send { border-radius: 16px; color: var(--on-fill); }
  .msg-mic { border: none; border-radius: 16px; background: var(--ok-soft); color: var(--ok-ink); box-shadow: inset 0 0 0 1.5px var(--ok); }
  .msg-send:disabled, .msg-mic:disabled { background: var(--surface-2); color: var(--dim); box-shadow: none; }
  .msg-time { border: 1px solid var(--edge); border-radius: 16px; display: flex; align-items: center; justify-content: center; }
  .msg-time svg.i { width: 23px; height: 23px; }
  .msg-time.on { background: var(--warn-soft); border-color: transparent; box-shadow: inset 0 0 0 1.5px var(--warn); color: var(--warn-ink); }
  .msg-when { color: var(--warn-ink); }
  .msg-when select, .msg-when input { height: 40px; border-radius: 12px; }
  .vrec-bar { border: none; border-radius: 16px; background: var(--bad-soft); box-shadow: inset 0 0 0 1.5px var(--bad-line); }
  .vrec-dot { background: var(--danger); }
  #vrecTime { color: var(--bad-ink); }
  .vrec-cancel, .vrec-go { height: 40px; border-radius: 12px; }
  .vrec-cancel { background: var(--surface-2); }
  .vrec-go { background: var(--ok); color: var(--on-fill); }
  .msg-who { gap: 8px; font-size: 14px; }
  .msg-who button { height: 40px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--edge); font-size: 15px; }
  .msg-who button.on { background: var(--acc-soft); border-color: transparent; box-shadow: inset 0 0 0 1.5px var(--accent); color: var(--acc-ink); }
  .msg-who.need button { border-color: var(--warn); }
  /* message cards: no tinted frames; what the phones sent is set in, what the screen sent back sits at the edge */
  .msg-list { gap: 8px; }
  .msg-item, .msg-item.from-screen, .msg-item.remind { border: 1px solid var(--edge); border-radius: 16px; padding: 11px 14px; background: var(--surface); }
  .msg-item:not(.from-screen) { margin-left: 28px; }
  .msg-src { padding: 2px 8px; border-radius: 7px; background: var(--surface-2); color: var(--muted); }
  .msg-item .who, .msg-item.from-screen .who { color: var(--text); }
  .msg-item.remind .who { color: var(--warn-ink); }
  .msg-item .who svg.i, .msg-item .mt > svg.i { width: 16px; height: 16px; vertical-align: -2.5px; margin-right: 3px; }
  .msg-item .mt > svg.i { color: var(--warn); }
  .v-pill, .msg-item.from-screen .v-pill { height: 40px; border-radius: 12px; border: none; background: var(--surface-2); color: var(--text); }
  .v-pill[data-unr="1"], .msg-item.from-screen .v-pill[data-unr="1"] { background: var(--warn); color: var(--on-fill); }
  .v-pill.playing, .msg-item.from-screen .v-pill.playing { background: var(--acc-soft); color: var(--acc-ink); box-shadow: inset 0 0 0 1.5px var(--accent); }
  .msg-item .rrecall, .msg-item .rdel, .msg-manage, .msg-item .rcancel { height: 34px; padding: 0 12px; border-radius: 10px; border: none; background: var(--surface-2); color: var(--muted); font-size: 13px; }
  .msg-item .rdel, .msg-manage.on, .msg-item .rcancel { background: transparent; box-shadow: inset 0 0 0 1.5px var(--bad-line); color: var(--bad-ink); }
  .msg-item .rdel.sure { background: var(--bad-soft); }
  .msg-quick summary { padding: 8px 0; }
  .msg-quick textarea { border-radius: 14px; }
  .msg-quick button { height: 40px; border-radius: 12px; color: var(--on-fill); }
  /* ===== 2026-11-13: 倒数日 / 生日 / 纪念日 (phone management card) ===== */
  .cds { margin-top: 0; }
  .cd-add-btn { height: 34px; padding: 0 13px; border-radius: 10px; border: none; background: var(--accent); color: var(--on-fill); font-size: 13px; font-weight: 700; cursor: pointer; }
  .cd-add-btn.open { background: var(--surface-2); color: var(--muted); }
  .cd-form { display: none; margin-top: 12px; padding: 14px; border-radius: 16px; background: var(--surface-2); flex-direction: column; gap: 10px; }
  .cd-form.open { display: flex; }
  .cd-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .cd-inp { flex: 1; min-width: 0; height: 42px; padding: 0 12px; border-radius: 12px; border: 1px solid var(--line); background: var(--surface); color: var(--text); font-size: 15px; outline: none; }
  .cd-inp:focus { border-color: var(--accent); }
  .cd-qchips { display: flex; gap: 6px; flex-wrap: wrap; }
  .cd-qchip { height: 30px; padding: 0 10px; border-radius: 999px; border: none; background: var(--surface); color: var(--muted); font-size: 12.5px; font-weight: 600; }
  .cd-seg { display: inline-flex; padding: 3px; border-radius: 11px; background: var(--surface); gap: 3px; }
  .cd-seg button { height: 32px; padding: 0 10px; border-radius: 8px; border: none; background: transparent; color: var(--muted); font-size: 13px; font-weight: 600; }
  .cd-seg button.on { background: var(--acc-soft); color: var(--acc-ink); }
  .cd-sel { height: 38px; padding: 0 10px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface); color: var(--text); font-size: 14px; }
  .cd-chk { display: inline-flex; align-items: center; gap: 6px; font-size: 13.5px; color: var(--text); cursor: pointer; }
  .cd-chk input { width: 17px; height: 17px; accent-color: var(--accent); }
  .cd-f-acts { display: flex; gap: 8px; justify-content: flex-end; margin-top: 2px; }
  .cd-f-acts button { height: 38px; padding: 0 16px; border-radius: 11px; border: none; font-size: 14px; font-weight: 700; }
  .cd-btn-cancel { background: var(--surface); color: var(--muted); }
  .cd-btn-save { background: var(--accent); color: var(--on-fill); }
  .cd-list { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
  .cd-list:empty { display: none; }
  .cd-empty { font-size: 13.5px; color: var(--dim); padding: 8px 2px 2px; }
  .cd-it { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 13px; border-radius: 14px; background: var(--surface-2); }
  .cd-it.past { opacity: .65; }
  .cd-it-l { flex: 1; min-width: 0; }
  .cd-it-t { font-size: 15px; font-weight: 700; color: var(--text); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .cd-tag { font-size: 11.5px; font-weight: 700; padding: 1px 7px; border-radius: 6px; background: var(--acc-soft); color: var(--acc-ink); }
  .cd-tag.pin { background: var(--warn-soft); color: var(--warn-ink); }
  .cd-it-sub { font-size: 12.5px; color: var(--dim); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cd-it-r { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
  .cd-days { text-align: right; margin-right: 2px; }
  .cd-days b { font-size: 20px; font-weight: 800; color: var(--accent); font-variant-numeric: tabular-nums; }
  .cd-days small { display: block; font-size: 11px; color: var(--dim); }
  .cd-ibtn { height: 32px; padding: 0 9px; border-radius: 9px; border: none; background: var(--surface); color: var(--muted); font-size: 12.5px; font-weight: 600; }
  .cd-ibtn.on { background: var(--warn-soft); color: var(--warn-ink); }
  .cd-ibtn.del { background: transparent; box-shadow: inset 0 0 0 1.5px var(--bad-line); color: var(--bad-ink); }
  .cd-ibtn.del.sure { background: var(--bad-soft); }
  /* ----- 倒数日 card polish (Claude, 2026-11-13 evening): text gets the full width, the three actions open under the row,
     every control is finger-sized ----- */
  #cdHint { font-size: 12.5px; color: var(--dim); margin-right: 10px; white-space: nowrap; }
  .cd-add-btn { height: 40px; padding: 0 16px; border-radius: 12px; font-size: 14px; }
  .cd-form { padding: 14px; gap: 12px; border: 1px solid var(--edge); }
  .cd-form > .cd-inp { flex: none; width: 100%; height: 46px; font-size: 16px; }
  .cd-row .cd-inp { height: 46px; font-size: 16px; }
  .cd-qchips { gap: 8px; }
  .cd-qchip { height: 36px; padding: 0 13px; font-size: 13.5px; font-weight: 700; color: var(--text); }
  .cd-qchip.pre { background: var(--acc-soft); color: var(--acc-ink); }
  .cd-seg { display: flex; width: 100%; padding: 4px; border-radius: 13px; gap: 4px; }
  .cd-seg button { flex: 1; height: 38px; padding: 0 6px; border-radius: 10px; font-size: 14px; font-weight: 700; white-space: nowrap; }
  .cd-seg button.on { background: var(--accent); color: var(--on-fill); }
  .cd-sel { height: 46px; padding: 0 12px; border-radius: 12px; font-size: 15px; }
  #cdLunarRow .cd-sel { flex: 1; min-width: 0; }
  .cd-chk { min-height: 44px; gap: 8px; font-size: 14.5px; white-space: nowrap; }
  .cd-chk input { width: 22px; height: 22px; }
  .cd-prev { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; border-radius: 12px; background: var(--surface); font-size: 13.5px; line-height: 1.4; color: var(--muted); }
  .cd-prev b { flex: none; color: var(--accent); font-size: 14.5px; white-space: nowrap; }
  .cd-f-acts { gap: 10px; margin-top: 0; }
  .cd-f-acts button { height: 46px; border-radius: 13px; font-size: 15px; }
  .cd-btn-cancel { flex: none; padding: 0 22px; }
  .cd-btn-save { flex: 1; }
  .cd-empty { padding: 14px 4px 4px; font-size: 14px; line-height: 1.5; }
  .cd-it { display: block; padding: 0; overflow: hidden; border: 1px solid var(--edge); }
  .cd-it.past { opacity: 1; }
  .cd-it-main { width: 100%; min-height: 64px; padding: 11px 10px 11px 14px; border: none; background: transparent; color: inherit; font: inherit; text-align: left; display: flex; align-items: center; gap: 10px; cursor: pointer; }
  .cd-it-t { font-size: 15.5px; gap: 6px; flex-wrap: nowrap; }
  .cd-it-t .cd-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cd-dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
  .cd-it[data-cat="birthday"] .cd-dot { background: var(--danger); }
  .cd-it[data-cat="anniv"] .cd-dot { background: var(--warn); }
  .cd-it[data-cat="life"] .cd-dot { background: var(--ok); }
  .cd-tag { flex: none; white-space: nowrap; }
  .cd-it-sub { font-size: 13px; margin-top: 4px; padding-left: 14px; color: var(--muted); }
  .cd-days { flex: none; margin: 0; display: flex; align-items: baseline; gap: 4px; white-space: nowrap; }
  .cd-days small { display: inline; font-size: 12.5px; color: var(--muted); }
  .cd-days b { font-size: 22px; font-weight: 700; }
  .cd-days b i { font-style: normal; font-size: 13px; margin-left: 2px; color: var(--muted); }
  .cd-it.past .cd-name, .cd-it.past .cd-days b { color: var(--dim); }
  .cd-it.today { background: var(--acc-soft); }
  .cd-chev { flex: none; width: 18px; height: 18px; color: var(--dim); fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; transition: transform .15s ease; }
  .cd-it.open .cd-chev { transform: rotate(180deg); }
  .cd-it-acts { display: none; gap: 8px; padding: 0 12px 12px; }
  .cd-it.open .cd-it-acts { display: flex; }
  .cd-ibtn { flex: 1; height: 42px; padding: 0 8px; border-radius: 12px; font-size: 14px; font-weight: 700; color: var(--text); }
  .cd-ibtn.del { flex: none; padding: 0 18px; color: var(--bad-ink); }
  .cd-ibtn.del.sure { background: var(--danger); box-shadow: none; color: var(--on-fill); }
  .cd-more { width: 100%; height: 44px; margin-top: 8px; border-radius: 14px; border: 1px solid var(--edge); background: var(--surface); color: var(--muted); font-family: inherit; font-size: 14px; font-weight: 700; cursor: pointer; }

  /* ===== 2026-11-18 Claude：小字不小于 13px，按钮不小于 44px（拍题已移到单独的手机页面 /tutor/，这里不再放讲题卡片） ===== */
  .dev-sub, #statusText { font-size: 13px; }
  .chip { font-size: 13px; }
  .tile b { font-size: 13px; }
  .tile span { font-size: 15px; }
  .shots-hd span, #msgHint, #cdHint, #shotsCount { font-size: 13px; }
  .msg-item .mtime, .mtime { font-size: 13px; }
  .msg-item .ms, .ms { font-size: 13px; }
  .msg-item .tr, .tr { font-size: 15px; }
  .v-pill, .msg-item .v-pill { font-size: 14px; height: 44px; }
  .msg-item .rrecall, .msg-item .rdel, .msg-manage, .msg-item .rcancel { height: 40px; padding: 0 14px; font-size: 14px; }
  .msg-who button { height: 44px; padding: 0 16px; }
  .msg-quick summary { font-size: 14px; }
  .cd-add-btn { height: 44px; }
  .cd-tag { font-size: 13px; }
  .cd-it-sub { font-size: 14px; }
  .cd-list small { font-size: 13px; }
  .shots-empty { font-size: 13px; }
  .round { width: 44px; height: 44px; }
  .ctl .lbl, .lbl { font-size: 13px; }
  #callSub { font-size: 14px; }
  .dock .hint, .hint { font-size: 13px; }
  #saverTxt { font-size: 14px; }
  /* ===== 2026-11-18 Claude 第三轮：倒数日表单的选中项和其他页面一样是「浅底 + 描边 + 深色字」（原来是实心色块）；
     「生日 / 中考」这类词条改成描边，不再像已选中；表单按钮 40–44px，字不出现半像素 ===== */
  .cd-seg button { height: 44px; }
  .cd-seg button.on { background: var(--acc-soft); color: var(--acc-ink); box-shadow: inset 0 0 0 2px var(--accent); }
  .cd-qchip { height: 40px; font-size: 14px; }
  .cd-qchip.pre { background: transparent; color: var(--text); box-shadow: inset 0 0 0 2px var(--surface); }
  .cd-prev { font-size: 14px; }
  .cd-prev > span { word-break: keep-all; overflow-wrap: anywhere; }
  .cd-prev b { font-size: 15px; }
  .msg-quick button { height: 44px; font-size: 15px; }
  /* ===== 2026-11-18 Claude 第四轮：适配——窄屏（≤380 宽，常见安卓是 360）状态块图标放到文字上方，「已打开」不再被截；
     写留言的输入框单独占一行，三个按钮在下一行；≤340 宽收起标题旁的说明；平板和电脑上内容列从 560 放宽到 680，视频更大 ===== */
  .msg-manage, .cd-add-btn, .msg-send { white-space: nowrap; }
  @media (max-width: 380px) {
    .tiles { gap: 6px; padding: 10px 10px 14px; }
    .tile { flex-direction: column; align-items: flex-start; gap: 6px; padding: 10px; }
    .tile .ti { width: 28px; height: 28px; border-radius: 9px; }
    .msg-compose { flex-wrap: wrap; }
    #msgInput { order: -1; flex: 1 1 100%; min-height: 48px; }
    .msg-compose .msg-mic, .msg-compose .msg-time { height: 48px; }
    .msg-compose .msg-send { flex: 1; height: 48px; }
  }
  @media (max-width: 340px) {
    #msgHint, #cdHint { display: none; }
  }
  @media (min-width: 720px) {
    body:not(.theater) { max-width: 680px; }
  }
  /* ===== 2026-11-18 Claude 第五轮：连不上设备时状态块显示「—」并变灰（原来照样显示绿色的「已打开 / 正常 / 空闲」），留言读不到时有一行说明 ===== */
  .tile.off .ti { background: var(--surface-2); color: var(--muted); }
  .msg-err { padding: 14px 12px; border-radius: 14px; background: var(--surface); color: var(--muted); font-size: 14px; text-align: center; }

  /* ===== 2026-11-25 闹钟留言（闹钟在前 · 留言板在后） ===== */
  .al-bar { display: none; margin-top: 10px; padding: 11px 12px; border-radius: 14px; background: var(--bad-soft); border: 1px solid var(--bad-line); align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .al-bar.on { display: flex; }
  .al-bar.miss { background: var(--warn-soft); border-color: transparent; color: var(--warn-ink); }
  .al-bar-txt { font-size: 14px; font-weight: 700; color: var(--text); flex: 1; min-width: 140px; }
  .al-bar.miss .al-bar-txt { color: var(--warn-ink); }
  .al-bar-acts { display: flex; gap: 6px; flex-wrap: wrap; }
  .al-bar-acts button { height: 38px; padding: 0 12px; border-radius: 11px; border: none; font-size: 13px; font-weight: 700; cursor: pointer; background: var(--surface); color: var(--text); }
  .al-bar-acts button.stop { background: var(--danger); color: var(--on-fill); }
  .al-item { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 13px; border-radius: 14px; background: var(--surface-2); border: 1px solid var(--edge); }
  .al-item-l { flex: 1; min-width: 0; }
  .al-item-t { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
  .al-time { font-size: 19px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text); }
  .al-tag { font-size: 12px; font-weight: 700; padding: 2px 8px; border-radius: 7px; background: var(--acc-soft); color: var(--acc-ink); }
  .al-item-sub { font-size: 13px; color: var(--muted); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .al-sound-row { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; }
  .al-vol-row { display: flex; align-items: center; gap: 10px; font-size: 14px; color: var(--text); }
  .al-vol-row input[type="range"] { flex: 1; accent-color: var(--accent); }

  /* ===== 2026-11-20 Claude：嵌在管理后台里（深浅色由后台统一切换） ===== */
  html[data-embed] #themeBtn { display: none; }
  html[data-embed="msg"] header, html[data-embed="msg"] .stage-wrap, html[data-embed="msg"] .tiles,
  html[data-embed="msg"] #cdBox, html[data-embed="msg"] .shots, html[data-embed="msg"] .dock,
  html[data-embed="cd"] header, html[data-embed="cd"] .stage-wrap, html[data-embed="cd"] .tiles,
  html[data-embed="cd"] #alarmBox, html[data-embed="cd"] #msgBox, html[data-embed="cd"] .shots, html[data-embed="cd"] .dock,
  html[data-embed="call"] #alarmBox, html[data-embed="call"] #msgBox, html[data-embed="call"] #cdBox { display: none !important; }
  html[data-embed="msg"] body, html[data-embed="cd"] body { padding-top: 6px; padding-bottom: calc(20px + env(safe-area-inset-bottom)); }
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute">
  <defs>
    <symbol id="i-cam" viewBox="0 0 24 24"><rect x="3" y="6" width="13" height="12" rx="3"/><path d="M16 10.5l4.2-2.6c.4-.2.8 0 .8.5v7.2c0 .5-.4.7-.8.5L16 13.5"/></symbol>
    <symbol id="i-cam-off" viewBox="0 0 24 24"><path d="M16 10.5l4.2-2.6c.4-.2.8 0 .8.5v7.2c0 .5-.4.7-.8.5L16 13.5"/><path d="M8 6h5a3 3 0 0 1 3 3v5M16 16.5A3 3 0 0 1 13 18H6a3 3 0 0 1-3-3V9a3 3 0 0 1 2-2.8"/><path d="M3 3l18 18"/></symbol>
    <symbol id="i-ear" viewBox="0 0 24 24"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3" y="14" width="5" height="6" rx="2"/><rect x="16" y="14" width="5" height="6" rx="2"/></symbol>
    <symbol id="i-mic" viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></symbol>
    <symbol id="i-mic-off" viewBox="0 0 24 24"><path d="M15 9.5V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 4.6 2.5"/><path d="M5.5 11a6.5 6.5 0 0 0 10.4 5.2M18.5 11a6.5 6.5 0 0 1-.6 2.7M12 17.5V21M3 3l18 18"/></symbol>
    <symbol id="i-flip" viewBox="0 0 24 24"><path d="M4 9a8 8 0 0 1 14-3l2 2M20 4v4h-4M20 15a8 8 0 0 1-14 3l-2-2M4 20v-4h4"/></symbol>
    <symbol id="i-phone" viewBox="0 0 24 24"><path d="M5 4h3.5l1.8 4.4-2.2 1.4a11 11 0 0 0 6.1 6.1l1.4-2.2L20 15.5V19a1 1 0 0 1-1 1A16 16 0 0 1 4 5a1 1 0 0 1 1-1z"/></symbol>
    <symbol id="i-hang" viewBox="0 0 24 24"><path d="M3.5 13.8c4.7-4.4 12.3-4.4 17 0l-1.9 2.6-3.3-1.3v-2.6a10 10 0 0 0-6.6 0v2.6l-3.3 1.3z"/></symbol>
    <symbol id="i-shot" viewBox="0 0 24 24"><path d="M4 8.5A2.5 2.5 0 0 1 6.5 6H8l1.5-2h5L16 6h1.5A2.5 2.5 0 0 1 20 8.5v8a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 16.5z"/><circle cx="12" cy="12.5" r="3.5"/></symbol>
    <symbol id="i-full" viewBox="0 0 24 24"><path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/></symbol>
    <symbol id="i-exit" viewBox="0 0 24 24"><path d="M9 4v4a1 1 0 0 1-1 1H4M15 4v4a1 1 0 0 0 1 1h4M15 20v-4a1 1 0 0 1 1-1h4M9 20v-4a1 1 0 0 0-1-1H4"/></symbol>
    <symbol id="i-lock" viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/></symbol>
    <symbol id="i-vol" viewBox="0 0 24 24"><path d="M4 10v4h3.5L12 18V6L7.5 10z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/></symbol>
    <symbol id="i-vol-off" viewBox="0 0 24 24"><path d="M4 10v4h3.5L12 18V6L7.5 10z"/><path d="M16 10l4 4M20 10l-4 4"/></symbol>
    <symbol id="i-leaf" viewBox="0 0 24 24"><path d="M5 19c0-8 5-13 15-14-1 10-6 15-14 15"/><path d="M5 19l7-7"/></symbol>
    <symbol id="i-screen" viewBox="0 0 24 24"><rect x="3" y="4.5" width="18" height="12.5" rx="2.5"/><path d="M9 20.5h6M12 17v3.5"/></symbol>
    <symbol id="i-alarm" viewBox="0 0 24 24"><circle cx="12" cy="13" r="7"/><path d="M12 10v3l2 1.5M5 4L3 6M19 4l2 2"/></symbol>
    <symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/></symbol>
    <symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></symbol>
    <symbol id="i-auto" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17A8.5 8.5 0 0 0 12 3.5z" fill="currentColor"/></symbol>
  <symbol id="i-house" viewBox="0 0 24 24"><path d="M4 11l8-7 8 7v9H4z"/><path d="M10 20v-6h4v6"/></symbol>
  </defs>
</svg>

<header>
  <div class="dev">
    <div class="dev-icon"><svg class="i"><use href="#i-screen"/></svg></div>
    <div style="min-width:0">
      <div class="dev-name">联想 View</div>
      <div class="dev-sub"><span id="statusDot" class="dot"></span><span id="statusText">正在连接…</span></div>
    </div>
  </div>
  <div class="hdr-right">
    <button class="saver" id="saverBtn" onclick="toggleSaver()" aria-label="省流模式"><svg class="i"><use href="#i-leaf"/></svg><span id="saverTxt">省流</span></button>
    <button class="hdr-btn theme-btn" id="themeBtn" onclick="cycleTheme()" aria-label="深浅色：自动" title="深浅色"><svg class="i"><use id="themeIco" href="#i-auto"/></svg></button>
    <button class="hdr-btn" onclick="toggleTheater()" aria-label="全屏"><svg class="i"><use href="#i-full"/></svg></button>
  </div>
</header>

<div class="stage-wrap">
  <div class="stage" id="stage">
    <canvas id="remoteCanvas" width="648" height="486"></canvas>
    <div class="waiting" id="waiting"><div class="spinner"></div><span id="waitingText">正在连接摄像头…</span></div>

    <div class="ov ov-tl">
      <span class="chip live" id="liveChip"><span class="rec"></span>LIVE</span>
      <span class="chip" id="clockChip">--:--:--</span>
    </div>
    <div class="ov ov-tr">
      <span class="chip off" id="audioChip"><svg class="i"><use id="audioIcon" href="#i-vol-off"/></svg><span id="audioStat">静音</span></span>
      <span class="chip" id="fpsText">-- FPS</span>
    </div>
    <div class="ov ov-bl">
      <span id="shutterBadge" class="chip hide"><svg class="i"><use href="#i-lock"/></svg>镜头盖已合上</span>
      <span id="hwMicBadge" class="chip hide"><svg class="i"><use href="#i-mic-off"/></svg>设备麦克风已静音</span>
      <span id="callBadge" class="chip hide"><svg class="i"><use href="#i-phone"/></svg><span id="callBadgeText">通话中</span></span>
    </div>
    <div class="ov ov-br">
      <button class="round" onclick="takeSnapshot()" aria-label="抓拍"><svg class="i"><use href="#i-shot"/></svg></button>
      <button class="round" id="theaterBtn" onclick="toggleTheater()" aria-label="全屏"><svg class="i"><use href="#i-full"/></svg></button>
    </div>

    <div id="pipBox" class="pip-box"><video id="localVideo" autoplay playsinline muted></video></div>
    <div class="flash" id="flash"></div>
  </div>
</div>

<div class="tiles">
  <div class="tile" id="tileShutter">
    <div class="ti"><svg class="i"><use href="#i-cam"/></svg></div>
    <div style="min-width:0"><b>镜头</b><span id="tileShutterTxt">已打开</span></div>
  </div>
  <div class="tile" id="tileMic">
    <div class="ti"><svg class="i"><use href="#i-mic"/></svg></div>
    <div style="min-width:0"><b>麦克风</b><span id="tileMicTxt">正常</span></div>
  </div>
  <div class="tile" id="tileCall">
    <div class="ti"><svg class="i"><use href="#i-phone"/></svg></div>
    <div style="min-width:0"><b>通话</b><span id="tileCallTxt">空闲</span></div>
  </div>
</div>

<div class="msgs alarms" id="alarmBox">
  <div class="shots-hd"><b>闹钟与提醒</b><div class="msg-hd-r"><span id="alarmHint">到点响铃唤醒屏幕</span><button type="button" class="cd-add-btn" id="alarmAddBtn" onclick="toggleAlarmForm()">＋ 新建</button></div></div>
  <div class="al-bar" id="alarmRingBar">
    <span class="al-bar-txt" id="alarmRingTxt">闹钟正在响铃</span>
    <div class="al-bar-acts">
      <button type="button" onclick="alarmAct('snooze')">贪睡 10 分钟</button>
      <button type="button" class="stop" onclick="alarmAct('stop')">停掉闹钟</button>
    </div>
  </div>
  <div class="al-bar miss" id="alarmMissedBar">
    <span class="al-bar-txt" id="alarmMissedTxt"></span>
    <div class="al-bar-acts">
      <button type="button" onclick="alarmAct('clear_missed')">知道了</button>
    </div>
  </div>
  <div class="cd-form" id="alarmForm">
    <div class="cd-qchips">
      <button type="button" class="cd-qchip pre" onclick="pickAlarmPreset('07:00','起床')">起床 07:00</button>
      <button type="button" class="cd-qchip pre" onclick="pickAlarmPreset('13:30','午休')">午休 13:30</button>
      <button type="button" class="cd-qchip pre" onclick="pickAlarmPreset('16:30','接孩子')">接孩子 16:30</button>
      <button type="button" class="cd-qchip pre" onclick="pickAlarmPreset('22:00','睡觉')">睡觉 22:00</button>
    </div>
    <div class="cd-row">
      <input type="time" class="cd-inp" id="alarmClockInp" value="07:00">
      <select class="cd-sel" id="alarmRepeatSel" aria-label="重复">
        <option value="">只响一次</option>
        <option value="daily">每天</option>
        <option value="workdays">工作日</option>
        <option value="weekends">周末</option>
        <option value="w1">每周一</option>
        <option value="w2">每周二</option>
        <option value="w3">每周三</option>
        <option value="w4">每周四</option>
        <option value="w5">每周五</option>
        <option value="w6">每周六</option>
        <option value="w7">每周日</option>
      </select>
    </div>
    <input type="text" class="cd-inp" id="alarmLabelInp" maxlength="40" placeholder="备注，如：起床、吃药、接孩子">
    <div class="cd-f-acts">
      <button type="button" class="cd-btn-cancel" onclick="toggleAlarmForm(false)">取消</button>
      <button type="button" class="cd-btn-save" id="alarmSaveBtn" onclick="savePhoneAlarm()">保存闹钟</button>
    </div>
  </div>
  <div class="cd-empty" id="alarmEmpty">正在读取闹钟…</div>
  <div class="cd-list" id="alarmList"></div>
  <details class="msg-quick" id="alarmSoundBox">
    <summary>闹钟音量与铃声（大屏独立设定）</summary>
    <div class="al-sound-row">
      <div class="al-vol-row">
        <span>闹钟音量</span>
        <input type="range" id="alarmVolRange" min="0" max="100" value="70" oninput="if($('alarmVolVal'))$('alarmVolVal').textContent=this.value+'%'" onchange="alarmAct('volume',{volume:Number(this.value)})">
        <b id="alarmVolVal">70%</b>
      </div>
      <div class="al-vol-row">
        <span>铃声</span>
        <select class="cd-sel" id="alarmToneSel" style="flex:1" onchange="alarmAct('tone',{tone:this.value})"></select>
      </div>
    </div>
  </details>
</div>

<div class="msgs" id="msgBox">
  <div class="shots-hd"><b>留言到屏幕</b><div class="msg-hd-r"><span id="msgHint">文字或 1 分钟语音 · 可定时提醒</span><button type="button" class="msg-manage" id="msgManage" onclick="toggleMsgManage()">管理</button></div></div>
  <div class="msg-who" id="msgWho"></div>
  <div class="msg-compose">
    <button type="button" class="msg-mic" id="msgVoiceBtn" onclick="togglePhoneVoiceRec()" aria-label="录语音留言" title="录语音留言"><svg class="i"><use href="#i-mic"/></svg></button>
    <textarea id="msgInput" maxlength="200" rows="2" placeholder="写一句话，或点左边麦克风发语音"></textarea>
    <button type="button" class="msg-time" id="msgTimeBtn" onclick="toggleMsgWhen()" aria-label="定时提醒" title="定时提醒"><svg class="i"><use href="#i-alarm"/></svg></button>
    <button class="msg-send" id="msgSend" onclick="sendMsg()">发送</button>
  </div>
  <div class="msg-when" id="msgWhen"><span>到点在屏幕上弹出并朗读（睡眠中只弹出）：</span><select id="msgDay"><option value="0">今天</option><option value="1">明天</option><option value="2">后天</option></select><input type="time" id="msgClock" value="07:00"><select id="msgRepeat" onchange="syncMsgRepeat()" aria-label="重复"><option value="">只一次</option><option value="daily">每天</option><option value="workdays">工作日</option><option value="w1">每周一</option><option value="w2">每周二</option><option value="w3">每周三</option><option value="w4">每周四</option><option value="w5">每周五</option><option value="w6">每周六</option><option value="w7">每周日</option></select></div>
  <div class="vrec-bar" id="vrecBar">
    <span class="vrec-dot"></span>
    <span id="vrecTime">正在录音 0:00 / 1:00</span>
    <button type="button" class="vrec-cancel" onclick="stopPhoneVoiceRec(false)">取消</button>
    <button type="button" class="vrec-go" onclick="stopPhoneVoiceRec(true)">发送语音</button>
  </div>
  <div class="msg-list" id="msgList"></div>
  <details class="msg-quick" id="msgQuickBox"><summary>屏幕上的快捷回复（孩子点一下就能回）</summary>
    <textarea id="quickEdit" placeholder="一行一条，最多 8 条，每条 14 个字以内"></textarea>
    <button type="button" onclick="saveQuick()">保存</button>
  </details>
</div>


<div class="msgs cds" id="cdBox">
  <div class="shots-hd"><b>倒数日 · 纪念日</b><div class="msg-hd-r"><span id="cdHint">生日 · 考试 · 正数日</span><button type="button" class="cd-add-btn" id="cdAddBtn" onclick="toggleCdForm()">＋ 添加</button></div></div>
  <div class="cd-form" id="cdForm">
    <input type="text" class="cd-inp" id="cdTitle" maxlength="24" placeholder="名称，如：萌萌中考、妈妈生日" oninput="previewPhoneCd()">
    <div class="cd-qchips" id="cdQuickChips">
      <button type="button" class="cd-qchip" onclick="cdPickWord('爸爸')">爸爸</button>
      <button type="button" class="cd-qchip" onclick="cdPickWord('妈妈')">妈妈</button>
      <button type="button" class="cd-qchip" onclick="cdPickWord('萌萌')">萌萌</button>
      <button type="button" class="cd-qchip" onclick="cdPickWord('朵朵')">朵朵</button>
      <button type="button" class="cd-qchip pre" onclick="cdPickPreset('生日','yearly','lunar','birthday')">生日</button>
      <button type="button" class="cd-qchip pre" onclick="cdPickPreset('中考','once','solar','exam')">中考</button>
      <button type="button" class="cd-qchip pre" onclick="cdPickPreset('纪念日','yearly','solar','anniv')">纪念日</button>
      <button type="button" class="cd-qchip pre" onclick="cdPickPreset('坚持打卡','countup','solar','life')">坚持打卡</button>
    </div>
    <div class="cd-row">
      <div class="cd-seg" id="cdModeSeg">
        <button type="button" data-m="once" class="on" onclick="setCdMode('once')">单次倒数</button>
        <button type="button" data-m="yearly" onclick="setCdMode('yearly')">每年重复</button>
        <button type="button" data-m="countup" onclick="setCdMode('countup')">正数日</button>
      </div>
      <div class="cd-seg" id="cdCalSeg">
        <button type="button" data-c="solar" class="on" onclick="setCdCal('solar')">公历</button>
        <button type="button" data-c="lunar" onclick="setCdCal('lunar')">农历</button>
      </div>
    </div>
    <div class="cd-row" id="cdSolarRow">
      <input type="date" class="cd-inp" id="cdSolarInp" onchange="previewPhoneCd()">
    </div>
    <div class="cd-row" id="cdLunarRow" style="display:none">
      <select class="cd-sel" id="cdLunarY" style="display:none" onchange="previewPhoneCd()"></select>
      <select class="cd-sel" id="cdLunarM" onchange="previewPhoneCd()"></select>
      <select class="cd-sel" id="cdLunarD" onchange="previewPhoneCd()"></select>
      <label class="cd-chk"><input type="checkbox" id="cdLunarLeap" onchange="previewPhoneCd()">闰月</label>
    </div>
    <div class="cd-row" id="cdYearlyExtra" style="display:none">
      <input type="number" class="cd-inp" id="cdStartYear" min="1920" max="2099" inputmode="numeric" placeholder="哪年出生或开始（选填，用来算周岁、周年）" oninput="previewPhoneCd()">
    </div>
    <div class="cd-prev" id="cdPrevBar"><span>选好日期后，这里显示对应的公历、农历</span><b></b></div>
    <div class="cd-row" style="justify-content:space-between">
      <select class="cd-sel" id="cdCatSel" onchange="previewPhoneCd()">
        <option value="">分类：自动</option>
        <option value="exam">考试日程</option>
        <option value="birthday">生日</option>
        <option value="anniv">纪念日</option>
        <option value="life">生活纪念</option>
      </select>
      <label class="cd-chk"><input type="checkbox" id="cdPinChk">置顶到屏幕主页</label>
    </div>
    <div class="cd-f-acts">
      <button type="button" class="cd-btn-cancel" onclick="toggleCdForm(false)">取消</button>
      <button type="button" class="cd-btn-save" id="cdSaveBtn" onclick="savePhoneCd()">保存</button>
    </div>
  </div>
  <div class="cd-empty" id="cdEmpty">正在读取倒数日…</div>
  <div class="cd-list" id="cdList"></div>
</div>

<div class="shots">
  <div class="shots-hd"><b>抓拍</b><span id="shotsCount">本次打开页面期间</span></div>
  <div class="shots-row" id="shotsRow">
    <div class="shots-empty" style="flex:1"><svg class="i"><use href="#i-shot"/></svg>点画面右下角的相机按钮抓拍，照片会出现在这里</div>
  </div>
</div>

<div class="dock">
  <div class="ctl-row">
    <button id="btnListen" class="ctl" onclick="toggleListen()">
      <span class="orb"><div id="vuRx" class="vu-bar"></div><svg class="i"><use id="iconListen" href="#i-ear"/></svg></span>
      <span class="lbl" id="txtListen">监听现场</span>
    </button>
    <button id="btnMic" class="ctl" onclick="toggleMic()" disabled>
      <span class="orb"><div id="vuTx" class="vu-bar"></div><svg class="i"><use id="iconMic" href="#i-mic"/></svg></span>
      <span class="lbl" id="txtMic">我的麦克风</span>
    </button>
    <button id="btnCam" class="ctl" onclick="togglePhoneCam()" disabled>
      <span class="orb"><svg class="i"><use id="iconCam" href="#i-cam"/></svg></span>
      <span class="lbl" id="txtCam">我的画面</span>
    </button>
    <button id="btnSwitchCam" class="ctl" onclick="switchPhoneCam()" disabled>
      <span class="orb"><svg class="i"><use href="#i-flip"/></svg></span>
      <span class="lbl">翻转镜头</span>
    </button>
  </div>
  <button id="btnCall" class="call-btn" onclick="toggleCall()">
    <svg class="i"><use id="iconCall" href="#i-phone"/></svg>
    <span id="txtCall">视频通话</span>
    <span class="sub" id="callSub">· 会唤醒设备屏幕</span>
  </button>
  <div class="hint">画面方向会跟着设备摆放自动扶正</div>
</div>

<div class="toast" id="toast"></div>
<canvas id="sendCanvas" width="480" height="360" style="display:none"></canvas>

<script>
let ws = null;
let inCall = false;
let listenOnly = false;
let micEnabled = true;
let phoneCamEnabled = true;
let facingMode = 'user';
let connected = false;
let otherInCall = false;
let shutterClosed = false;
let hwMicMuted = false;
let callStartTs = 0;
let lastFrameTs = 0;

// Orientation state (automatically driven by BMA253 gravity sensor in background)
let rotDeg = 0;
let flipH = true;
let lastBmp = null;

let audioCtx = null;
let nextPlayTime = 0;
let lastAudioRxTs = 0;
let localStream = null;
let micSrcNode = null;
let scriptNode = null;
let muteGainNode = null;
let sendVideoTimer = null;
let callStarting = false;
let callSeq = 0;
let decodingFrame = false;
let pendingJpeg = null;
let hiddenJpeg = null;

let frameCount = 0;
let lastFpsTime = performance.now();
const $ = (id) => document.getElementById(id);
const canvas = $('remoteCanvas');
const ctx = canvas.getContext('2d');
const sendCanvas = $('sendCanvas');
const sendCtx = sendCanvas.getContext('2d');
const localVideo = $('localVideo');
var embedActive = true;
function _embedHidden() { return window.parent !== window && (!embedActive || window.innerWidth === 0); }

/* ---------------- Data saver (every 2nd frame, about half the traffic) ---------------- */
let saverOn = false;
try { saverOn = localStorage.getItem('ts-saver') === '1'; } catch (e) {}
function renderSaver() {
  $('saverBtn').classList.toggle('on', saverOn);
  $('saverTxt').textContent = saverOn ? '省流中' : '省流';
}
function sendSaver() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'saver', enable: saverOn }));
}
function toggleSaver() {
  saverOn = !saverOn;
  try { localStorage.setItem('ts-saver', saverOn ? '1' : '0'); } catch (e) {}
  renderSaver();
  sendSaver();
  toast(saverOn ? '已开启省流：画面每秒约 7 帧，流量减半' : '已关闭省流：恢复每秒 15 帧');
}

/* ---------------- UI helpers ---------------- */
let toastTimer = null;
/* 2026-11-13: 深浅色 switch (自动 follows the phone) */
const THEME_NAMES = { auto: '自动（跟随手机）', light: '浅色', dark: '深色' };
const themeMetas = document.querySelectorAll('meta[name="theme-color"]');
function themeChoice() { return document.documentElement.dataset.theme || 'auto'; }
function renderTheme() {
  const t = themeChoice();
  const ico = document.getElementById('themeIco'), btn = document.getElementById('themeBtn');
  if (ico) ico.setAttribute('href', t === 'light' ? '#i-sun' : t === 'dark' ? '#i-moon' : '#i-auto');
  if (btn) btn.setAttribute('aria-label', '深浅色：' + THEME_NAMES[t]);
  themeMetas.forEach(m => {
    if (!m.dataset.media) m.dataset.media = m.getAttribute('media') || '';
    if (t === 'auto') m.setAttribute('media', m.dataset.media);
    else if (m.dataset.media.includes(t)) m.removeAttribute('media');
    else m.setAttribute('media', 'not all');
  });
}
function cycleTheme() {
  const order = ['auto', 'light', 'dark'];
  const t = order[(order.indexOf(themeChoice()) + 1) % 3];
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  try { t === 'auto' ? localStorage.removeItem('ts-theme') : localStorage.setItem('ts-theme', t); } catch (e) {}
  renderTheme();
  toast('深浅色：' + THEME_NAMES[t]);
}
renderTheme();
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}
function setIcon(useId, name) { $(useId).setAttribute('href', '#' + name); }
function pad(n) { return String(n).padStart(2, '0'); }
function setVu(id, pct) {
  const tf = 'scaleX(' + Math.max(0, Math.min(1, (pct || 0) / 100)).toFixed(3) + ')';
  const el = $(id);
  if (el && el.style.transform !== tf) el.style.transform = tf;
}

function renderUi() {
  // header status
  $('statusDot').className = 'dot' + (!connected ? '' : (inCall ? ' call' : ' online'));
  $('statusText').textContent = !connected ? '连接断开，正在重连…'
    : inCall ? '视频通话中'
    : (location.protocol === 'https:' ? '在线 · 加密连接' : '在线');

  // hardware tiles + overlay badges
  $('tileShutter').className = 'tile' + (!connected ? ' off' : (shutterClosed ? ' warn' : ''));
  $('tileShutterTxt').textContent = !connected ? '—' : (shutterClosed ? '已合上' : '已打开');
  $('shutterBadge').className = shutterClosed ? 'chip warn' : 'chip hide';
  const staleNow = !lastFrameTs || (performance.now() - lastFrameTs) > 3000;
  const videoLive = !staleNow && connected && !shutterClosed;
  if (!videoLive) {
    $('waitingText').textContent = !connected ? '连接断开，正在重连…'
      : (shutterClosed ? '镜头盖已合上，打开后即可看到画面' : '正在等待画面…');
  }
  $('waiting').classList.toggle('hide', videoLive);
  $('liveChip').className = videoLive ? 'chip live' : 'chip off';
  $('tileMic').className = 'tile' + (!connected ? ' off' : (hwMicMuted ? ' warn' : ''));
  $('tileMicTxt').textContent = !connected ? '—' : (hwMicMuted ? '已静音' : '正常');
  $('hwMicBadge').className = hwMicMuted ? 'chip warn' : 'chip hide';

  const tc = $('tileCall');
  if (inCall) { tc.className = 'tile busy'; $('tileCallTxt').textContent = '通话中'; }
  else if (otherInCall) { tc.className = 'tile busy'; $('tileCallTxt').textContent = '其他手机占线'; }
  else { tc.className = 'tile' + (connected ? '' : ' off'); $('tileCallTxt').textContent = connected ? '空闲' : '—'; }

  const cb = $('callBadge');
  if (inCall) { cb.className = 'chip info'; $('callBadgeText').textContent = '通话已接通'; }
  else if (otherInCall) { cb.className = 'chip warn'; $('callBadgeText').textContent = '另一台手机正在通话'; }
  else { cb.className = 'chip hide'; }

  // listen button
  const listening = connected && (listenOnly || inCall);
  $('btnListen').disabled = !connected || inCall || callStarting;
  $('btnListen').classList.toggle('on', listening);
  setIcon('iconListen', 'i-ear');
  $('txtListen').textContent = inCall ? '通话声音' : (listenOnly ? '正在监听' : '监听现场');
  if (!listening) {
    setVu('vuRx', 0);
    $('audioChip').className = 'chip off';
    setIcon('audioIcon', 'i-vol-off');
    $('audioStat').textContent = '静音';
  } else {
    $('audioChip').className = 'chip';
    setIcon('audioIcon', 'i-vol');
    const wantStat = hwMicMuted ? '设备静音' : (inCall ? '通话中' : '监听中');
    if (hwMicMuted) setVu('vuRx', 0);
    if (hwMicMuted || $('audioStat').textContent === '静音' || $('audioStat').textContent === '设备静音' || $('audioStat').textContent === (inCall ? '监听中' : '通话中')) $('audioStat').textContent = wantStat;
  }

  // call-only controls
  const bm = $('btnMic');
  bm.disabled = !inCall;
  bm.classList.toggle('on', inCall && micEnabled);
  bm.classList.toggle('muted', inCall && !micEnabled);
  setIcon('iconMic', (inCall && !micEnabled) ? 'i-mic-off' : 'i-mic');
  $('txtMic').textContent = (inCall && !micEnabled) ? '已静音' : '我的麦克风';
  if (!inCall || !micEnabled) setVu('vuTx', 0);

  const bc = $('btnCam');
  bc.disabled = !inCall || !localStream || localStream.getVideoTracks().length === 0;
  bc.classList.toggle('on', inCall && phoneCamEnabled && !bc.disabled);
  bc.classList.toggle('muted', inCall && !phoneCamEnabled && !bc.disabled);
  setIcon('iconCam', (inCall && !phoneCamEnabled) ? 'i-cam-off' : 'i-cam');
  $('txtCam').textContent = (inCall && !phoneCamEnabled) ? '画面已关' : '我的画面';
  $('btnSwitchCam').disabled = bc.disabled || !phoneCamEnabled;

  // call button
  const btnCall = $('btnCall');
  if (inCall) {
    btnCall.className = 'call-btn stop';
    btnCall.disabled = false;
    setIcon('iconCall', 'i-hang');
    $('txtCall').textContent = '挂断';
    updateCallTimer();
  } else if (otherInCall) {
    btnCall.className = 'call-btn';
    btnCall.disabled = true;
    setIcon('iconCall', 'i-phone');
    $('txtCall').textContent = '另一台手机正在通话';
    $('callSub').textContent = '';
  } else {
    btnCall.className = 'call-btn';
    btnCall.disabled = !connected || callStarting;
    setIcon('iconCall', 'i-phone');
    $('txtCall').textContent = '视频通话';
    $('callSub').textContent = '· 会唤醒设备屏幕';
  }
}

function updateCallTimer() {
  if (!inCall || !callStartTs) return;
  const s = Math.floor((Date.now() - callStartTs) / 1000);
  $('callSub').textContent = `· ${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

function setTxt(el, s) { if (el && el.textContent !== s) el.textContent = s; }
function setCls(el, c) { if (el && el.className !== c) el.className = c; }
let clockTimer = 0;
function tickClock() {
  if (document.hidden) return;
  const d = new Date();
  setTxt($('clockChip'), `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`);
  updateCallTimer();
  const stale = !lastFrameTs || (performance.now() - lastFrameTs) > 3000;
  const videoLive = !stale && connected && !shutterClosed;
  setCls($('liveChip'), videoLive ? 'chip live' : 'chip off');
  if (stale) {
    frameCount = 0;
    lastFpsTime = performance.now();
    setTxt($('fpsText'), '-- FPS');
  }
  if (stale || shutterClosed || !connected) {
    setTxt($('waitingText'), !connected ? '连接断开，正在重连…'
      : (shutterClosed ? '镜头盖已合上，打开后即可看到画面' : '正在等待画面…'));
  }
  const waitEl = $('waiting');
  if (waitEl && waitEl.classList.contains('hide') !== videoLive) waitEl.classList.toggle('hide', videoLive);
  if (connected && (listenOnly || inCall) && (hwMicMuted || !lastAudioRxTs || (performance.now() - lastAudioRxTs) > 1500)) {
    setVu('vuRx', 0);
    setTxt($('audioStat'), hwMicMuted ? '设备静音' : (inCall ? '通话中' : '监听中'));
  }
}
function syncClockTimer() {
  if (!document.hidden && !_embedHidden() && !/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) { if (!clockTimer) clockTimer = setInterval(tickClock, 1000); }
  else if (clockTimer) { clearInterval(clockTimer); clockTimer = 0; }
}
syncClockTimer();
tickClock();

function toggleTheater() {
  const on = !document.body.classList.contains('theater');
  document.body.classList.toggle('theater', on);
  const icon = on ? '#i-exit' : '#i-full';
  document.querySelectorAll('#theaterBtn use, .hdr-btn:not(.theme-btn) use').forEach(u => u.setAttribute('href', icon));
  try {
    if (on && document.documentElement.requestFullscreen && !document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else if (!on && document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    }
  } catch (e) {}
}
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement && document.body.classList.contains('theater')) {
    document.body.classList.remove('theater');
    document.querySelectorAll('#theaterBtn use, .hdr-btn:not(.theme-btn) use').forEach(u => u.setAttribute('href', '#i-full'));
  }
});

/* ---------------- Video ---------------- */
function drawVideoFrame(bmp) {
  if (!bmp) return;
  const swapWh = (rotDeg % 180) !== 0;
  const cw = swapWh ? bmp.height : bmp.width;
  const ch = swapWh ? bmp.width : bmp.height;
  if (canvas.width !== cw || canvas.height !== ch) {
    canvas.width = cw;
    canvas.height = ch;
    $('stage').style.setProperty('--ar', `${cw} / ${ch}`);
  }
  ctx.save();
  ctx.translate(cw / 2, ch / 2);
  if (flipH) ctx.scale(-1, 1);
  if (rotDeg !== 0) ctx.rotate((rotDeg * Math.PI) / 180);
  ctx.drawImage(bmp, -bmp.width / 2, -bmp.height / 2);
  ctx.restore();
}

function applyOrientMsg(msg) {
  let changed = false;
  if (typeof msg.rot_deg === 'number') {
    const nd = ((msg.rot_deg % 360) + 360) % 360;
    if (nd !== rotDeg) { rotDeg = nd; changed = true; }
  }
  if (typeof msg.flip_h === 'boolean') {
    if (msg.flip_h !== flipH) { flipH = msg.flip_h; changed = true; }
  }
  if (changed && lastBmp) {
    drawVideoFrame(lastBmp);
  }
}

async function ensureAudioCtx() {
  if (!audioCtx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    audioCtx = new AC();
  }
  if (audioCtx.state !== 'running') {
    try { await audioCtx.resume(); } catch (e) {}
  }
  try {
    const b = audioCtx.createBuffer(1, 1, 22050);
    const s = audioCtx.createBufferSource();
    s.buffer = b;
    s.connect(audioCtx.destination);
    s.start(0);
  } catch (e) {}
}

async function pumpJpegQueue(myWs) {
  if (decodingFrame) return;
  decodingFrame = true;
  try {
    while (pendingJpeg) {
      const jpg = pendingJpeg;
      pendingJpeg = null;
      try {
        const bmp = await createImageBitmap(new Blob([jpg], { type: 'image/jpeg' }));
        if (ws !== myWs) { try { bmp.close(); } catch (e) {} break; }
        if (lastBmp) {
          try { lastBmp.close(); } catch (e) {}
        }
        lastBmp = bmp;
        drawVideoFrame(bmp);
        lastFrameTs = performance.now();
        if (!shutterClosed && !$('waiting').classList.contains('hide')) $('waiting').classList.add('hide');
        if (!shutterClosed && $('liveChip').className !== 'chip live') $('liveChip').className = 'chip live';
        frameCount++;
        const now = performance.now();
        if (now - lastFpsTime >= 1000) {
          $('fpsText').textContent = `${frameCount} FPS`;
          frameCount = 0;
          lastFpsTime = now;
        }
      } catch (e) {}
    }
  } finally {
    decodingFrame = false;
  }
}

function syncVideoRx() {
  if (ws && ws.readyState === 1 && !/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) {
    try { ws.send(JSON.stringify({ type: 'video', enable: !(document.hidden || _embedHidden()) })); } catch (e) {}
  }
}

let wsReconnectTimer = null;
let hiddenJpegTs = 0, callConfirmed = false, lastAudioResumeTry = 0;
function connectWS() {
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  hiddenJpeg = null; hiddenJpegTs = 0; pendingJpeg = null;
  if (ws && ws.readyState <= 1) {
    try { ws.onclose = null; ws.close(); } catch (e) {}
  }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const myWs = new WebSocket(`${proto}//${location.host}/ws${(document.hidden || _embedHidden()) ? '?video=0' : ''}`);
  ws = myWs;
  myWs.binaryType = 'arraybuffer';

  myWs.onopen = () => {
    if (ws !== myWs) return;
    connected = true;
    renderUi();
    if (listenOnly) myWs.send(JSON.stringify({ type: 'listen', enable: true }));
    if (saverOn) sendSaver();
    if (document.hidden || _embedHidden()) syncVideoRx();
  };

  myWs.onclose = () => {
    if (ws !== myWs) return;
    connected = false;
    otherInCall = false;
    hiddenJpeg = null; hiddenJpegTs = 0; pendingJpeg = null;
    if (inCall) stopCallLocal();
    renderUi();
    if (!(document.hidden || _embedHidden()) || inCall || listenOnly) {
      wsReconnectTimer = setTimeout(connectWS, 2000);
    }
  };

  myWs.onmessage = async (ev) => {
    if (ws !== myWs) return;
    if (typeof ev.data === 'string') {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'status') {
        const prevSc = shutterClosed, prevMm = hwMicMuted, prevOc = otherInCall;
        shutterClosed = !!msg.shutter_closed;
        if (shutterClosed) { hiddenJpeg = null; hiddenJpegTs = 0; pendingJpeg = null; }
        hwMicMuted = !!msg.hw_mic_muted;
        applyOrientMsg(msg);
        if (inCall && msg.in_call && msg.is_call_owner) callConfirmed = true;
        const staleCallMs = callConfirmed ? 3000 : 8000;
        if (inCall && ((msg.in_call && !msg.is_call_owner) || (!msg.in_call && callStartTs && (Date.now() - callStartTs) > staleCallMs))) {
          stopCallLocal();
        } else if (!inCall && !callStarting && msg.in_call && msg.is_call_owner) {
          if (ws && ws.readyState === 1) {
            try { ws.send(JSON.stringify({ type: 'stop_call' })); } catch (e) {}
          }
        }
        otherInCall = !!(msg.in_call && !msg.is_call_owner && !inCall);
        if (shutterClosed !== prevSc || hwMicMuted !== prevMm || otherInCall !== prevOc) renderUi();
      } else if (msg.type === 'orient') {
        applyOrientMsg(msg);
      } else if (msg.type === 'call_busy') {
        otherInCall = true;
        if (inCall) stopCallLocal(); else renderUi();
        toast('设备正在和另一台手机通话，请稍后再试');
      } else if (msg.type === 'call_ended_by_device') {
        if (inCall) {
          stopCallLocal();
          toast('设备那边挂断了通话');
        }
      }
      return;
    }

    const buf = new Uint8Array(ev.data);
    if (buf.length < 2) return;
    const tag = buf[0];

    if (tag === 1) {
      if (shutterClosed) { hiddenJpeg = null; hiddenJpegTs = 0; pendingJpeg = null; return; }
      if ((document.hidden || _embedHidden()) && lastBmp) { hiddenJpeg = buf.subarray(1); hiddenJpegTs = performance.now(); return; }
      hiddenJpeg = null; hiddenJpegTs = 0;
      pendingJpeg = buf.subarray(1);
      pumpJpegQueue(myWs);
    } else if (tag === 2) {
      if ((!listenOnly && !inCall) || !audioCtx) return;
      const sampleCount = (buf.byteLength - 1) >> 1;
      if (sampleCount <= 0) return;
      const dv = new DataView(ev.data, 1, sampleCount << 1);
      let peak = 0;
      for (let i = 0; i < sampleCount; i++) {
        const av = Math.abs(dv.getInt16(i << 1, true) * (1 / 32768));
        if (av > peak) peak = av;
      }
      lastAudioRxTs = performance.now();
      const pct = hwMicMuted ? 0 : Math.min(100, Math.round(peak * 140));
      setVu('vuRx', pct);
      $('audioStat').textContent = hwMicMuted ? '设备静音' : `${pct}%`;
      if (!inCall && (vRecStream || vRecStarting || playingMsgId)) { nextPlayTime = 0; return; }
      if (audioCtx.state !== 'running') {
        nextPlayTime = 0;
        const nowMs = performance.now();
        if (!document.hidden && !_embedHidden() && nowMs - lastAudioResumeTry > 1000) {
          lastAudioResumeTry = nowMs;
          audioCtx.resume().catch(() => {});
        }
        return;
      }
      const abuf = audioCtx.createBuffer(1, sampleCount, 16000);
      const f32 = abuf.getChannelData(0);
      for (let i = 0; i < sampleCount; i++) {
        f32[i] = dv.getInt16(i << 1, true) * (1 / 32768);
      }
      const src = audioCtx.createBufferSource();
      src.buffer = abuf;
      src.connect(audioCtx.destination);
      const now = audioCtx.currentTime;
      if (nextPlayTime < now || nextPlayTime > now + 0.35) {
        nextPlayTime = now + 0.03;
      }
      src.start(nextPlayTime);
      nextPlayTime += abuf.duration;
    }
  };
}

/* ---------------- Actions ---------------- */
async function toggleListen() {
  if (!connected || inCall || callStarting) { renderUi(); return; }
  await ensureAudioCtx();
  if (!connected || inCall || callStarting) { renderUi(); return; }  // during a call the audio is always on
  listenOnly = !listenOnly;
  if (!listenOnly) {
    nextPlayTime = 0;
    lastAudioRxTs = 0;
    if (audioCtx && audioCtx.state === 'running') audioCtx.suspend().catch(() => {});
  }
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'listen', enable: listenOnly }));
  if (listenOnly) $('audioStat').textContent = hwMicMuted ? '设备静音' : '监听中';
  renderUi();
}

function bindVideoTrackEnd(vt, seq) {
  if (!vt) return;
  vt.onended = () => {
    if (inCall && callSeq === seq && localStream && !localStream.getVideoTracks().some(t => t.readyState === 'live')) {
      phoneCamEnabled = false;
      $('pipBox').classList.remove('active');
      if (ws && ws.readyState === 1) { try { ws.send(JSON.stringify({ type: 'cam_off' })); } catch (e) {} }
      renderUi();
    }
  };
}

async function toggleCall() {
  if (inCall) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'stop_call' }));
    stopCallLocal();
    return;
  }
  if (callStarting || otherInCall) return;
  if (vRecStream || vRecStarting) stopPhoneVoiceRec(false);
  stopPhoneVoicePlay(true);
  callStarting = true;
  renderUi();
  try {
    await ensureAudioCtx();
    try {
      localStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: { facingMode: facingMode, width: { ideal: 480 }, height: { ideal: 360 } }
      });
      phoneCamEnabled = true;
    } catch (err) {
      try {
        localStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
          video: false
        });
        phoneCamEnabled = false;
        toast('没拿到摄像头权限，先用语音通话');
      } catch (err2) {
        toast('需要允许麦克风权限才能通话');
        return;
      }
    }

    if (audioCtx && audioCtx.state !== 'running') {
      try { await audioCtx.resume(); } catch (e) {}
    }

    if (!connected || otherInCall || document.hidden || _embedHidden() || !ws || ws.readyState !== 1) {
      localStream.getTracks().forEach(t => t.stop());
      localStream = null;
      if (!listenOnly && audioCtx && audioCtx.state === 'running') audioCtx.suspend().catch(() => {});
      return;
    }

    inCall = true;
    callConfirmed = false;
    callSeq++;
    micEnabled = true;
    otherInCall = false;
    callStartTs = Date.now();
    try {
      ws.send(JSON.stringify({ type: 'start_call' }));
    } catch (e) {
      stopCallLocal();
      return;
    }
  } finally {
    callStarting = false;
    renderUi();
  }
  if (!inCall) return;
  const myCallSeq = callSeq;

  if (localStream.getVideoTracks().length > 0) {
    bindVideoTrackEnd(localStream.getVideoTracks()[0], myCallSeq);
    localVideo.srcObject = localStream;
    localVideo.play().catch(() => {});
    localVideo.style.transform = (facingMode === 'user') ? 'scaleX(-1)' : 'none';
    $('pipBox').classList.add('active');
    let sendingFrame = false;
    sendVideoTimer = setInterval(() => {
      if (sendingFrame || !inCall || !phoneCamEnabled || !ws || ws.readyState !== 1 || ws.bufferedAmount > 128000) return;
      const vt = localStream && localStream.getVideoTracks()[0];
      if (!vt || vt.readyState !== 'live' || vt.muted) return;
      if (localVideo.videoWidth > 0 && localVideo.videoHeight > 0) {
        const vw = localVideo.videoWidth, vh = localVideo.videoHeight;
        const sc = 480 / Math.max(vw, vh);
        const tw = Math.max(2, Math.round(vw * sc));
        const th = Math.max(2, Math.round(vh * sc));
        if (sendCanvas.width !== tw) sendCanvas.width = tw;
        if (sendCanvas.height !== th) sendCanvas.height = th;
        sendCtx.drawImage(localVideo, 0, 0, tw, th);
        sendingFrame = true;
        sendCanvas.toBlob(async (blob) => {
          try {
            if (blob && inCall && callSeq === myCallSeq && phoneCamEnabled && ws && ws.readyState === 1) {
              const ab = await blob.arrayBuffer();
              if (!inCall || callSeq !== myCallSeq || !phoneCamEnabled || !ws || ws.readyState !== 1) return;
              const out = new Uint8Array(1 + ab.byteLength);
              out[0] = 1;
              out.set(new Uint8Array(ab), 1);
              ws.send(out.buffer);
            }
          } finally { sendingFrame = false; }
        }, 'image/jpeg', 0.65);
      }
    }, 100);
  }

  try {
    const aTrack0 = localStream.getAudioTracks()[0];
    if (aTrack0) {
      aTrack0.onended = () => {
        if (inCall && callSeq === myCallSeq) {
          toast('手机麦克风被系统中断，通话已结束');
          if (ws && ws.readyState === 1) { try { ws.send(JSON.stringify({ type: 'stop_call' })); } catch (e) {} }
          stopCallLocal();
        }
      };
    }
    micSrcNode = audioCtx.createMediaStreamSource(localStream);
    scriptNode = audioCtx.createScriptProcessor(2048, 1, 1);
    muteGainNode = audioCtx.createGain();
    muteGainNode.gain.value = 0.0;
    const inRate = audioCtx.sampleRate;
    let txBuf = null, txOut = null, txDv = null, txSamples = 0, phase = 0;

    scriptNode.onaudioprocess = (e) => {
      if (!inCall || !micEnabled || !ws || ws.readyState !== 1 || ws.bufferedAmount > 65536) return;
      const input = e.inputBuffer.getChannelData(0);
      const inLen = input.length;
      const ratio = inRate / 16000.0;
      const outLen = Math.max(0, Math.floor((inLen - phase) / ratio));
      if (!outLen) { phase = Math.max(0, phase - inLen); return; }
      if (outLen !== txSamples) {
        txSamples = outLen;
        txBuf = new ArrayBuffer(1 + outLen * 2);
        txOut = new Uint8Array(txBuf);
        txOut[0] = 2;
        txDv = new DataView(txBuf, 1, outLen * 2);
      }
      let peak = 0;
      for (let i = 0; i < outLen; i++) {
        const p0 = phase + i * ratio;
        const idx0 = Math.floor(p0);
        const idx1 = Math.min(inLen, Math.max(idx0 + 1, Math.floor(p0 + ratio)));
        let acc = 0;
        for (let k = idx0; k < idx1; k++) acc += input[k];
        let s = acc / (idx1 - idx0);
        const as = s < 0 ? -s : s;
        if (as > peak) peak = as;
        if (s > 1) s = 1; else if (s < -1) s = -1;
        txDv.setInt16(i << 1, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
      }
      phase = (phase + outLen * ratio) - inLen;
      if (phase < 0 || phase >= ratio) phase = 0;
      setVu('vuTx', Math.min(100, Math.round(peak * 160)));
      ws.send(txOut);
    };
    micSrcNode.connect(scriptNode);
    scriptNode.connect(muteGainNode);
    muteGainNode.connect(audioCtx.destination);
  } catch (err) {
    console.error('Mic capture setup error:', err);
    micEnabled = false;
    toast('麦克风初始化失败，请重试');
  }
  renderUi();
}

let switchingCam = false;
async function switchPhoneCam() {
  if (!inCall || !localStream || !localStream.getVideoTracks().length || switchingCam) return;
  switchingCam = true;
  const myCallSeq = callSeq;
  const nextMode = (facingMode === 'user') ? 'environment' : 'user';
  let vStream = null;
  let added = false;
  try {
    vStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: nextMode, width: { ideal: 480 }, height: { ideal: 360 } },
      audio: false
    });
    if (!inCall || !localStream || callSeq !== myCallSeq) {
      vStream.getTracks().forEach(t => t.stop());
      return;
    }
    const newTrack = vStream.getVideoTracks()[0];
    if (!newTrack) { vStream.getTracks().forEach(t => t.stop()); return; }
    newTrack.enabled = phoneCamEnabled;
    bindVideoTrackEnd(newTrack, myCallSeq);
    const oldTracks = localStream.getVideoTracks();
    oldTracks.forEach(t => { localStream.removeTrack(t); t.stop(); });
    localStream.addTrack(newTrack);
    added = true;
    facingMode = nextMode;
    localVideo.srcObject = null;
    localVideo.srcObject = localStream;
    localVideo.play().catch(() => {});
    localVideo.style.transform = (facingMode === 'user') ? 'scaleX(-1)' : 'none';
  } catch (e) {
    if (vStream && !added) { try { vStream.getTracks().forEach(t => t.stop()); } catch (_) {} }
    toast('无法切换前后摄像头');
  } finally {
    switchingCam = false;
  }
}

function stopCallLocal() {
  inCall = false;
  callConfirmed = false;
  callSeq++;
  switchingCam = false;
  callStartTs = 0;
  if (!listenOnly) {
    nextPlayTime = 0;
    lastAudioRxTs = 0;
    if (audioCtx && audioCtx.state === 'running') audioCtx.suspend().catch(() => {});
  }
  if (sendVideoTimer) { clearInterval(sendVideoTimer); sendVideoTimer = null; }
  if (micSrcNode) { try { micSrcNode.disconnect(); } catch(e){} micSrcNode = null; }
  if (scriptNode) { try { scriptNode.onaudioprocess = null; scriptNode.disconnect(); } catch(e){} scriptNode = null; }
  if (muteGainNode) { try { muteGainNode.disconnect(); } catch(e){} muteGainNode = null; }
  if (localStream) {
    localStream.getTracks().forEach(t => t.stop());
    localStream = null;
  }
  localVideo.srcObject = null;
  $('pipBox').classList.remove('active');
  renderUi();
}

function toggleMic() {
  if (!inCall) return;
  micEnabled = !micEnabled;
  renderUi();
}

function togglePhoneCam() {
  if (!inCall || !localStream) return;
  if (!localStream.getVideoTracks().length) {
    toast('当前为纯语音通话，无摄像头画面');
    return;
  }
  phoneCamEnabled = !phoneCamEnabled;
  localStream.getVideoTracks().forEach(t => t.enabled = phoneCamEnabled);
  $('pipBox').classList.toggle('active', phoneCamEnabled);
  if (!phoneCamEnabled && ws && ws.readyState === 1) {
    try { ws.send(JSON.stringify({ type: 'cam_off' })); } catch (e) {}
  }
  renderUi();
}
function takeSnapshot() {
  if (!connected) { toast('连接已断开，无法抓拍'); return; }
  if (shutterClosed) { toast('镜头盖已合上，无法抓拍'); return; }
  if (!lastFrameTs || !lastBmp || (performance.now() - lastFrameTs) > 3000) { toast('还没有画面，稍等一下'); return; }
  const f = $('flash');
  f.classList.remove('go'); void f.offsetWidth; f.classList.add('go');
  const d = new Date();
  const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}_${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
  const name = `thinksmart-view-${stamp}.jpg`;
  const url = canvas.toDataURL('image/jpeg', 0.92);
  saveShot(url, name);
  addShotThumb(url, name, `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`);
  toast('已抓拍并保存');
}

function saveShot(url, name) {
  const a = document.createElement('a');
  a.download = name;
  a.href = url;
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.appendChild(a);
  try { a.click(); } catch (e) {}
  setTimeout(() => { try { a.remove(); } catch (e) {} }, 200);
}

const shots = [];
function addShotThumb(url, name, label) {
  shots.unshift({ url, name, label });
  if (shots.length > 12) shots.pop();
  const row = $('shotsRow');
  const empty = row.querySelector('.shots-empty');
  if (empty) empty.remove();
  const el = document.createElement('div');
  el.className = 'shot';
  el.innerHTML = `<img alt=""><i>${label}</i>`;
  el.querySelector('img').src = url;
  el.onclick = () => saveShot(url, name);
  row.prepend(el);
  while (row.children.length > 12) row.lastElementChild.remove();
  $('shotsCount').textContent = `${shots.length} 张 · 点缩略图可再次保存`;
}

renderUi();
renderSaver();
if (!/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) connectWS();   // 留言、倒数日两块不连摄像头
document.addEventListener('visibilitychange', () => {
  syncClockTimer();
  if (document.hidden) {
    if (typeof stopPhoneVoiceRec === 'function' && (vRecStream || vRecStarting)) stopPhoneVoiceRec(false);
    if (!inCall && !callStarting && !listenOnly) {
      if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
      hiddenJpeg = null; hiddenJpegTs = 0; pendingJpeg = null;
      if (ws && ws.readyState <= 1) {
        const oldWs = ws;
        ws = null;
        connected = false;
        try { oldWs.onclose = null; oldWs.close(); } catch (e) {}
        renderUi();
      }
    } else {
      syncVideoRx();
    }
  } else {
    syncVideoRx();
    if (hiddenJpeg && !shutterClosed && (performance.now() - hiddenJpegTs) <= 3000 && ws && ws.readyState === 1) {
      pendingJpeg = hiddenJpeg;
      hiddenJpeg = null; hiddenJpegTs = 0;
      pumpJpegQueue(ws);
    } else {
      hiddenJpeg = null; hiddenJpegTs = 0;
    }
    tickClock();
    if (inCall || listenOnly) {
      if (inCall && localVideo && localVideo.srcObject) localVideo.play().catch(() => {});
      if (audioCtx && (audioCtx.state === 'suspended' || audioCtx.state === 'interrupted')) audioCtx.resume().catch(() => {});
    }
    if ((!ws || ws.readyState > 1) && (!_embedHidden() || inCall || listenOnly) && !/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) connectWS();
    if (Array.isArray(msgListCache) && msgListCache.some(m => m.source === 'screen' && m.kind !== 'voice' && !m.read_at)) {
      renderMsgs(msgListCache, true);
    }
  }
});
window.addEventListener('pagehide', () => {
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  if (msgPollTimer) { clearInterval(msgPollTimer); msgPollTimer = 0; }
  if (alPollTimer) { clearInterval(alPollTimer); alPollTimer = 0; }
  hiddenJpeg = null; hiddenJpegTs = 0; pendingJpeg = null;
  if (typeof stopPhoneVoiceRec === 'function') stopPhoneVoiceRec(false);
  if (typeof stopPhoneVoicePlay === 'function') stopPhoneVoicePlay(false);
  if (inCall && ws && ws.readyState === 1) {
    try { ws.send(JSON.stringify({ type: 'stop_call' })); } catch (e) {}
  }
  if (inCall || localStream) stopCallLocal();
  if (audioCtx && audioCtx.state === 'running') audioCtx.suspend().catch(() => {});
  if (ws && ws.readyState <= 1) {
    const oldWs = ws;
    ws = null;
    connected = false;
    try { oldWs.onclose = null; oldWs.close(); } catch (e) {}
  }
});
window.addEventListener('pageshow', (ev) => {
  if (ev.persisted) {
    renderUi();
    syncClockTimer();
    tickClock();
    syncVideoRx();
    if (inCall || listenOnly) {
      if (inCall && localVideo && localVideo.srcObject) localVideo.play().catch(() => {});
      if (audioCtx && (audioCtx.state === 'suspended' || audioCtx.state === 'interrupted')) audioCtx.resume().catch(() => {});
    }
    if ((!ws || ws.readyState > 1) && (!_embedHidden() || inCall || listenOnly) && !/^(msg|cd)$/.test(document.documentElement.dataset.embed || '')) connectWS();
    if (_msgVisibleInEmbed()) { loadMsgs(); syncMsgPollTimer(); loadAlarms(); syncAlarmPollTimer(); }
  }
});
/* ---------------- Message board (round 109): two-way text & voice (phone <-> screen) ---------------- */
let msgInFlight = 0, msgLoading = false, msgReqSeq = 0, msgSig = '', msgEtag = '', msgListCache = [], msgPollTimer = 0;
// 2026-11-12: who is sending (remembered on this phone), reminders, the screen's quick replies
let msgFamily = ['爸爸', '妈妈', '萌萌', '朵朵'], msgWhoSel = '', msgQuick = null;
try { msgWhoSel = localStorage.getItem('msg.who') || ''; } catch (e) {}
function renderWho(need) {
  const box = $('msgWho');
  box.classList.toggle('need', !!need);
  box.innerHTML = '<span>我是</span>' + msgFamily.map(n => `<button type="button" class="${n === msgWhoSel ? 'on' : ''}" data-who="${msgEsc(n)}">${msgEsc(n)}</button>`).join('');
  box.querySelectorAll('button').forEach(b => { b.onclick = () => { msgWhoSel = b.dataset.who; try { localStorage.setItem('msg.who', msgWhoSel); } catch (e) {} renderWho(false); }; });
}
function needWho() {
  if (msgWhoSel && msgFamily.includes(msgWhoSel)) return false;
  renderWho(true); toast('先点一下「我是」谁'); return true;
}
function toggleMsgWhen() {
  const on = !$('msgWhen').classList.contains('on');
  $('msgWhen').classList.toggle('on', on); $('msgTimeBtn').classList.toggle('on', on);
  $('msgSend').textContent = on ? '定时' : '发送';
}
function msgRepeatPick() {
  const v = $('msgRepeat').value || '';
  if (!v) return null;
  return v[0] === 'w' ? { repeat: 'weekly', days: [Number(v.slice(1))] } : { repeat: v };
}
function syncMsgRepeat() {
  $('msgDay').style.display = msgRepeatPick() ? 'none' : '';   // a repeating one starts on the next day that fits
}
function msgDue() {
  if (!$('msgWhen').classList.contains('on')) return null;
  const t = ($('msgClock').value || '').split(':');
  if (t.length < 2) return NaN;
  const hh = Number(t[0]), mm = Number(t[1]);
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) return NaN;
  const rp = msgRepeatPick();
  const tpNow = new Date(Date.now() + 8 * 3600000);
  const dayOff = rp ? 0 : Number($('msgDay').value || 0);
  let targetMs = Date.UTC(tpNow.getUTCFullYear(), tpNow.getUTCMonth(), tpNow.getUTCDate() + dayOff, hh, mm, 0, 0) - 8 * 3600000;
  if (rp && targetMs < Date.now() + 60000) targetMs += 86400000;
  return Math.floor(targetMs / 1000);
}
const MSG_RECALL_SEC = 120;
let msgManage = false, msgRecallT = 0;
function toggleMsgManage() {
  msgManage = !msgManage;
  $('msgManage').classList.toggle('on', msgManage);
  $('msgManage').textContent = msgManage ? '完成' : '管理';
  renderMsgs(msgListCache, true);
}
function msgRepeatLbl(m) {
  return m.repeat === 'daily' ? '每天' : (m.repeat === 'workdays' ? '工作日' : (m.repeat === 'weekly' ? '每周' + (m.days || []).map(d => '一二三四五六日'[d - 1] || '').join('') : ''));
}
async function msgPostSmall(path, body) {
  const r = await fetch(path, { method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => null);
  if (!r.ok || !j || !j.ok) throw new Error((j && j.error) || '没有成功，请稍后再试');
  return j;
}
async function recallMsg(id) {
  try { await msgPostSmall('api/message/recall', { id }); toast('已撤回'); }
  catch (e) { toast(e.message); }
  msgEtag = ''; msgSig = ''; loadMsgs();
}
async function deleteMsg(btn) {
  if (!btn.classList.contains('sure')) {   // the second tap deletes
    btn.classList.add('sure'); btn.textContent = '确定删除';
    setTimeout(() => { if (btn.isConnected) { btn.classList.remove('sure'); btn.textContent = '删除'; } }, 3000);
    return;
  }
  btn.disabled = true;
  try { await msgPostSmall('api/message/delete', { id: btn.dataset.did }); toast('已删除'); }
  catch (e) { toast(e.message); btn.disabled = false; }
  msgEtag = ''; msgSig = ''; loadMsgs();
}
function fillQuick(list) {
  if (!Array.isArray(list)) return;
  msgQuick = list;
  const ta = $('quickEdit');
  if (document.activeElement !== ta && !ta.dataset.dirty) ta.value = list.join('\n');
}
async function saveQuick() {
  const ta = $('quickEdit');
  const quick = ta.value.split('\n').map(x => x.trim()).filter(Boolean);
  try {
    const r = await fetch('api/message/quick', { method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' }, body: JSON.stringify({ quick }) });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) { ta.dataset.dirty = ''; fillQuick(j.quick); toast('屏幕快捷回复已更新'); msgEtag = ''; }
    else toast((j && j.error) || '保存失败');
  } catch (e) { toast('保存失败，请检查网络'); }
}
async function cancelRemind(id) {
  try {
    const r = await fetch('api/message/cancel', { method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' }, body: JSON.stringify({ id }) });
    if (r.ok) { toast('已取消这条提醒'); msgEtag = ''; msgSig = ''; loadMsgs(); } else toast('取消失败（可能已经提醒过了）');
  } catch (e) { toast('取消失败，请检查网络'); }
}
function syncMsgBtns() {
  $('msgSend').disabled = msgInFlight > 0 || !!vRecStream || vRecStarting;
  $('msgVoiceBtn').disabled = msgInFlight > 0;
}
let playingMsgId = '', msgAudioEl = null, msgBlobUrl = '', voicePlaySeq = 0, voicePlayAc = null;
function stopPhoneVoicePlay(rerender = false) {
  ++voicePlaySeq;
  if (voicePlayAc) { try { voicePlayAc.abort(); } catch (e) {} voicePlayAc = null; }
  if (msgAudioEl) { try { msgAudioEl.onended = msgAudioEl.onerror = null; msgAudioEl.pause(); } catch (e) {} msgAudioEl = null; }
  if (msgBlobUrl) { try { URL.revokeObjectURL(msgBlobUrl); } catch (e) {} msgBlobUrl = ''; }
  const had = !!playingMsgId;
  playingMsgId = '';
  if (rerender && had) renderMsgs(msgListCache, true);
}
const ackedPhoneIds = new Set();
function ackPhoneMsg(id) {
  if (!id || ackedPhoneIds.has(id)) return;
  ackedPhoneIds.add(id);
  fetch('api/message/ack', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' },
    body: JSON.stringify({ id })
  }).then(res => res.ok ? res.json() : null).then(j => {
    if (j && j.ok) {
      msgEtag = '';
      msgListCache = msgListCache.map(x => x.id === id ? Object.assign({}, x, { read_at: Math.floor(Date.now() / 1000) }) : x);
      renderMsgs(msgListCache, true);
    } else {
      ackedPhoneIds.delete(id);
    }
  }).catch(() => { ackedPhoneIds.delete(id); });
}
function msgPad(n) { return String(n).padStart(2, '0'); }
function msgTime(ts) {
  const d = new Date(ts * 1000 + 8 * 3600000), now = new Date(Date.now() + 8 * 3600000);
  const hm = msgPad(d.getUTCHours()) + ':' + msgPad(d.getUTCMinutes());
  const sameDay = d.getUTCFullYear() === now.getUTCFullYear() && d.getUTCMonth() === now.getUTCMonth() && d.getUTCDate() === now.getUTCDate();
  return sameDay ? '今天 ' + hm : (d.getUTCMonth() + 1) + '/' + d.getUTCDate() + ' ' + hm;
}
function msgEsc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function maybeAckVisibleReplies() {
  const _emAck = document.documentElement.dataset.embed || '';
  if (!document.hidden && (!_emAck || _emAck === 'msg') && $('msgBox').offsetParent !== null && window.innerWidth > 0) {
    const shown = msgManage ? msgListCache : msgListCache.slice(0, 6);
    shown.forEach(m => {
      if (m.source === 'screen' && m.kind !== 'voice' && !m.read_at) ackPhoneMsg(m.id);
    });
  }
}
let _wasZeroW = (window.innerWidth === 0);
window.addEventListener('resize', () => {
  if (window.innerWidth === 0) { _wasZeroW = true; return; }
  if (_wasZeroW) {
    _wasZeroW = false;
    if (embedActive && !document.hidden && typeof window.tsEmbedSetActive === 'function') window.tsEmbedSetActive(true);
  }
  if (msgListCache.length) maybeAckVisibleReplies();
});
function renderMsgs(list, force = false) {
  msgListCache = Array.isArray(list) ? list : [];
  const tzDayKey = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
  const sig = tzDayKey + '|' + playingMsgId + '|' + msgManage + '|' + JSON.stringify(msgListCache.map(m => [m.id, m.read_at, m.kind, m.source, m.who, m.transcript, m.due, m.fired, m.no_speech, m.repeat, m.days]));
  if (!force && sig === msgSig) {
    maybeAckVisibleReplies();
    return;
  }
  msgSig = sig;
  const shown = msgManage ? msgListCache : msgListCache.slice(0, 6);
  $('msgList').innerHTML = shown.map(m => {
    const fromScr = m.source === 'screen';
    const isVoice = m.kind === 'voice';
    const sec = Math.max(1, Math.round(Number(m.duration) || 3));
    const isPl = playingMsgId === m.id;
    const isRem = !!m.due, pending = isRem && !m.fired;
    const whoTxt = m.who || (fromScr ? '家里' : '');
    const whoHtml = isRem ? `<span class="who"><svg class="i"><use href="#i-alarm"/></svg>${m.who ? '提醒' + msgEsc(m.who) : '提醒'}</span>` : (whoTxt ? `<span class="who">${msgEsc(whoTxt)}${fromScr ? '' : ''}</span>` : '');
    const trHtml = isVoice && m.transcript ? `<span class="tr">“${msgEsc(m.transcript)}”</span>` : (isVoice && m.no_speech ? '<span class="tr dim">（没听清）</span>' : '');
    const bodyHtml = isVoice
      ? whoHtml + `<button type="button" class="v-pill${isPl ? ' playing' : ''}" data-vid="${msgEsc(m.id)}" data-unr="${fromScr && !m.read_at ? '1' : '0'}">${isPl ? '■ 正在播放' : '▶ 点击收听'} · ${sec}″</button>` + trHtml
      : (fromScr && !whoTxt ? '<span class="msg-src">屏幕回复</span>' : '') + whoHtml + (m.tag === 'home' ? '<svg class="i"><use href="#i-house"/></svg>' : '') + msgEsc(m.text);
    const rpl = pending ? msgRepeatLbl(m) : '';
    const timeLbl = isRem ? (pending ? (rpl ? rpl + ' ' + msgTime(m.due).replace(/^(今天|\d+\/\d+) /, '') : msgTime(m.due)) + ' 提醒' : msgTime(m.due) + ' 已提醒') : msgTime(m.at) + (fromScr ? ' 屏幕发来' : ' 发送');
    const canRecall = !fromScr && !isRem && Date.now() / 1000 - (Number(m.at) || 0) < MSG_RECALL_SEC;
    const stLbl = msgManage ? `<button type="button" class="rdel" data-did="${msgEsc(m.id)}">删除</button>`
      : (pending ? `<button type="button" class="rcancel" data-rid="${msgEsc(m.id)}">${rpl ? '不再提醒' : '取消'}</button>` : (m.read_at ? ('已读 · ' + msgTime(m.read_at).replace('今天 ', '')) : (fromScr ? '新回复' : (isRem ? '在屏幕上' : '未读'))))
      + (!msgManage && canRecall ? `<button type="button" class="rrecall" data-xid="${msgEsc(m.id)}">撤回</button>` : '');
    return `<div class="msg-item${m.read_at ? ' read' : ''}${fromScr ? ' from-screen' : ''}${isRem ? ' remind' : ''}"><div class="mt">${bodyHtml}<span class="mtime">${timeLbl}</span></div><div class="ms">${stLbl}</div></div>`;
  }).join('');
  $('msgList').querySelectorAll('.v-pill').forEach(b => {
    b.onclick = () => playPhoneVoice(b.dataset.vid, b.dataset.unr === '1');
  });
  $('msgList').querySelectorAll('.rcancel').forEach(b => { b.onclick = () => cancelRemind(b.dataset.rid); });
  $('msgList').querySelectorAll('.rrecall').forEach(b => { b.onclick = () => { b.disabled = true; recallMsg(b.dataset.xid); }; });
  clearTimeout(msgRecallT); msgRecallT = 0;   // 「撤回」 disappears when its 2 minutes are up
  const recallLeft = shown.filter(m => m.source !== 'screen' && !m.due).map(m => (Number(m.at) || 0) + MSG_RECALL_SEC - Date.now() / 1000).filter(x => x > 0);
  if (recallLeft.length) msgRecallT = setTimeout(() => { msgRecallT = 0; renderMsgs(msgListCache, true); }, Math.min(...recallLeft) * 1000 + 300);
  $('msgList').querySelectorAll('.rdel').forEach(b => { b.onclick = () => deleteMsg(b); });
  maybeAckVisibleReplies();
}
async function playPhoneVoice(id, markRead) {
  if (!id) return;
  if (playingMsgId === id) {
    stopPhoneVoicePlay(true);
    return;
  }
  if (inCall || callStarting) {
    toast('正在通话中，挂断后可收听语音留言');
    return;
  }
  if (vRecStream || vRecStarting) {
    toast('正在录音，请先发送或取消');
    return;
  }
  stopPhoneVoicePlay(false);
  const mySeq = ++voicePlaySeq;
  playingMsgId = id;
  renderMsgs(msgListCache, true);
  const ac = typeof AbortController === 'function' ? new AbortController() : null;
  voicePlayAc = ac;
  const tm = ac ? setTimeout(() => ac.abort(), 8000) : 0;
  try {
    const r = await fetch('api/message/audio?id=' + encodeURIComponent(id), { headers: { 'X-Intercom-Msg': '1' }, signal: ac ? ac.signal : undefined });
    if (tm) clearTimeout(tm);
    if (mySeq !== voicePlaySeq) return;
    if (!r.ok) throw new Error('audio');
    const blob = await r.blob();
    if (mySeq !== voicePlaySeq || playingMsgId !== id) return;
    msgBlobUrl = URL.createObjectURL(blob);
    const el = new Audio(msgBlobUrl);
    msgAudioEl = el;
    el.onended = el.onerror = () => {
      if (mySeq === voicePlaySeq && playingMsgId === id) stopPhoneVoicePlay(true);
    };
    await el.play();
    if (mySeq !== voicePlaySeq) { try { el.pause(); } catch (e) {} return; }
    if (markRead) ackPhoneMsg(id);
  } catch (e) {
    if (tm) clearTimeout(tm);
    if (mySeq !== voicePlaySeq) return;
    stopPhoneVoicePlay(true);
    toast('语音播放失败，请重试');
  }
}

let vRecStream = null, vRecStarting = false, vRecSeq = 0, vRecCtx = null, vRecSrc = null, vRecProc = null, vRecChunks = [], vRecSamples = 0, vRecTimer = 0, vRecStart = 0;
function buildWav16k(chunks, totalSamples) {
  const dataLen = totalSamples * 2;
  const buf = new ArrayBuffer(44 + dataLen);
  const dv = new DataView(buf);
  const wr = (off, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); };
  wr(0, 'RIFF'); dv.setUint32(4, 36 + dataLen, true); wr(8, 'WAVE');
  wr(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, 16000, true); dv.setUint32(28, 32000, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  wr(36, 'data'); dv.setUint32(40, dataLen, true);
  const out = new Int16Array(buf, 44, totalSamples);
  let pos = 0;
  for (const c of chunks) { out.set(c, pos); pos += c.length; }
  return new Blob([buf], { type: 'audio/wav' });
}
async function togglePhoneVoiceRec() {
  if (vRecStream) { stopPhoneVoiceRec(true); return; }
  if (msgInFlight > 0 || vRecStarting) return;
  if ($('msgWhen').classList.contains('on')) { toast('语音留言暂不支持定时提醒，请先点时钟取消定时'); return; }
  if (needWho()) return;
  if (inCall || callStarting) { toast('正在通话中，挂断后可发语音留言'); return; }
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { toast('当前浏览器不支持麦克风录音'); return; }
  stopPhoneVoicePlay(true);
  const mySeq = ++vRecSeq;
  vRecStarting = true;
  syncMsgBtns();
  try {
    vRecCtx = new AC();
    try {
      const sb = vRecCtx.createBuffer(1, 1, 22050);
      const ss = vRecCtx.createBufferSource();
      ss.buffer = sb; ss.connect(vRecCtx.destination); ss.start(0);
    } catch (e) {}
    if (vRecCtx.state !== 'running') vRecCtx.resume().catch(() => {});
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
    if (mySeq !== vRecSeq || inCall || callStarting || document.hidden) {
      try { stream.getTracks().forEach(t => t.stop()); } catch (e) {}
      if (mySeq === vRecSeq) stopPhoneVoiceRec(false);
      return;
    }
    vRecStream = stream;
    const aTrk = stream.getAudioTracks()[0];
    if (aTrk) {
      aTrk.onended = () => {
        if (mySeq === vRecSeq && vRecStream === stream) {
          toast('麦克风被系统中断');
          stopPhoneVoiceRec(vRecSamples >= 16000);
        }
      };
    }
    syncMsgBtns();
    if (vRecCtx && vRecCtx.state !== 'running') {
      try { await Promise.race([vRecCtx.resume(), new Promise(r => setTimeout(r, 800))]); } catch (e) {}
    }
    if (mySeq !== vRecSeq || !vRecStream || !vRecCtx || inCall || callStarting || document.hidden) {
      if (mySeq === vRecSeq) stopPhoneVoiceRec(false);
      return;
    }
    if (vRecCtx.state !== 'running') {
      stopPhoneVoiceRec(false);
      toast('录音初始化失败，请再点一次');
      return;
    }
    vRecSrc = vRecCtx.createMediaStreamSource(vRecStream);
    vRecProc = vRecCtx.createScriptProcessor(2048, 1, 1);
    const mute = vRecCtx.createGain(); mute.gain.value = 0;
    const inRate = vRecCtx.sampleRate || 48000;
    let vPhase = 0;
    vRecChunks = []; vRecSamples = 0; vRecStart = Date.now();
    vRecProc.onaudioprocess = ev => {
      if (!vRecStream) return;
      const inp = ev.inputBuffer.getChannelData(0);
      const ratio = inRate / 16000;
      const out = [];
      while (vPhase < inp.length) {
        const j0 = Math.floor(vPhase);
        const j1 = Math.min(inp.length, Math.max(j0 + 1, Math.floor(vPhase + ratio)));
        let sum = 0;
        for (let j = j0; j < j1; j++) sum += inp[j];
        let s = sum / (j1 - j0);
        if (s > 1) s = 1; else if (s < -1) s = -1;
        out.push(s < 0 ? s * 0x8000 : s * 0x7FFF);
        vPhase += ratio;
      }
      vPhase -= inp.length;
      if (!out.length) return;
      const pcm = new Int16Array(out);
      const n = pcm.length;
      vRecChunks.push(pcm);
      vRecSamples += n;
      if (vRecSamples >= 16000 * 60) {
        if (vRecProc) vRecProc.onaudioprocess = null;
        setTimeout(() => { if (vRecStream) stopPhoneVoiceRec(true); }, 0);
      }
    };
    vRecSrc.connect(vRecProc); vRecProc.connect(mute); mute.connect(vRecCtx.destination);
    $('vrecBar').classList.add('on');
    $('vrecTime').textContent = '正在录音 0:00 / 1:00';
    vRecTimer = setInterval(() => {
      const elapsedMs = Date.now() - vRecStart;
      if (elapsedMs >= 61500) {
        if (vRecStream) stopPhoneVoiceRec(true);
        return;
      }
      const s = Math.min(60, Math.floor(elapsedMs / 1000));
      $('vrecTime').textContent = `正在录音 ${Math.floor(s / 60)}:${msgPad(s % 60)} / 1:00`;
    }, 250);
  } catch (e) {
    if (mySeq === vRecSeq) {
      stopPhoneVoiceRec(false);
      toast('需要允许麦克风权限才能发语音留言');
    }
  } finally {
    if (mySeq === vRecSeq) { vRecStarting = false; syncMsgBtns(); }
  }
}
async function stopPhoneVoiceRec(commit) {
  ++vRecSeq;
  vRecStarting = false;
  if (vRecTimer) { clearInterval(vRecTimer); vRecTimer = 0; }
  $('vrecBar').classList.remove('on');
  if (vRecProc) { try { vRecProc.onaudioprocess = null; vRecProc.disconnect(); } catch (e) {} vRecProc = null; }
  if (vRecSrc) { try { vRecSrc.disconnect(); } catch (e) {} vRecSrc = null; }
  if (vRecStream) { try { vRecStream.getTracks().forEach(t => t.stop()); } catch (e) {} vRecStream = null; }
  if (vRecCtx) { try { vRecCtx.close(); } catch (e) {} vRecCtx = null; }
  syncMsgBtns();
  const chunks = vRecChunks, total = vRecSamples;
  vRecChunks = []; vRecSamples = 0;
  if (!commit) return;
  if (total < 16000) { toast('说话时间太短啦（至少 1 秒）'); return; }
  msgInFlight++; syncMsgBtns();
  const ctl = new AbortController(), tid = setTimeout(() => ctl.abort(), 30000);
  try {
    const wavBlob = buildWav16k(chunks, total);
    const r = await fetch('api/message/voice?who=' + encodeURIComponent(msgWhoSel || ''), {
      method: 'POST', cache: 'no-store', signal: ctl.signal,
      headers: { 'Content-Type': 'audio/wav', 'X-Intercom-Msg': '1' }, body: wavBlob
    });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) {
      toast('语音留言已发到家里的屏幕');
      ++msgReqSeq; msgEtag = '';
      if (j.message && j.message.id) {
        renderMsgs([j.message, ...msgListCache.filter(x => x.id !== j.message.id)].slice(0, 20), true);
      } else { msgSig = ''; loadMsgs(); }
    } else toast((j && j.error) || '语音发送失败，请稍后重试');
  } catch (e) { toast('语音发送失败，请检查网络'); }
  finally { clearTimeout(tid); msgInFlight = Math.max(0, msgInFlight - 1); syncMsgBtns(); }
}
async function loadMsgs() {
  if (document.hidden || _embedHidden() || msgLoading) return;
  msgLoading = true;
  const seq = ++msgReqSeq;
  const ctl = new AbortController(), tid = setTimeout(() => ctl.abort(), 8000);
  try {
    const hdrs = { 'X-Intercom-Msg': '1' };
    if (msgEtag) hdrs['If-None-Match'] = msgEtag;
    const r = await fetch('api/messages', { cache: 'no-store', headers: hdrs, signal: ctl.signal });
    if (seq !== msgReqSeq) return;
    if (r.status === 304) { if (msgListCache.length) renderMsgs(msgListCache); return; }
    if (!r.ok) { msgLoadFailed(); return; }
    const j = await r.json();
    if (seq !== msgReqSeq) return;
    const et = r.headers.get('ETag');
    if (et) msgEtag = et;
    if (j && Array.isArray(j.family) && j.family.length) { const fam = j.family.filter(x => typeof x === 'string'); if (fam.join() !== msgFamily.join()) { msgFamily = fam; renderWho(false); } }
    if (j) fillQuick(j.quick);
    if (j && Array.isArray(j.messages)) renderMsgs(j.messages);
  } catch (e) { if (seq === msgReqSeq) msgLoadFailed(); } finally {
    clearTimeout(tid);
    msgLoading = false;
  }
}
// 2026-11-18 Claude: when nothing could be loaded, say so (an empty board looked like there were no messages)
function msgLoadFailed() {
  if (msgListCache.length) return;
  msgSig = '';
  $('msgList').innerHTML = '<div class="msg-err">留言暂时读不到，网络恢复后会自动刷新</div>';
}
async function sendMsg() {
  const inp = $('msgInput');
  const text = inp.value.trim();
  if (!text || msgInFlight > 0 || vRecStream || vRecStarting) return;
  if (needWho()) return;
  const due = msgDue();
  if (due !== null && (!Number.isFinite(due) || due < Date.now() / 1000 + 30)) { toast('提醒时间要选将来的时间'); return; }
  const payload = { text, who: msgWhoSel };
  if (due !== null) payload.due = due;
  const rp = due !== null ? msgRepeatPick() : null;
  if (rp) Object.assign(payload, rp);
  msgInFlight++; syncMsgBtns();
  const ctl = new AbortController(), tid = setTimeout(() => ctl.abort(), 10000);
  try {
    const r = await fetch('api/message', { method: 'POST', cache: 'no-store', signal: ctl.signal,
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' }, body: JSON.stringify(payload) });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) {
      inp.value = ''; inp.blur(); toast(due !== null ? (rp ? `${msgRepeatLbl(rp)} ${$('msgClock').value} 的提醒已设好` : '定时提醒已设好') : '已发送到家里的屏幕');
      if (due !== null) toggleMsgWhen();
      ++msgReqSeq;
      msgEtag = '';
      if (j.message && j.message.id && typeof j.message.text === 'string' && typeof j.message.at === 'number') {
        renderMsgs([j.message, ...msgListCache.filter(x => x.id !== j.message.id)].slice(0, 20));
      } else {
        msgSig = ''; loadMsgs();
      }
    }
    else toast((j && j.error) || '发送失败，请稍后再试');
  } catch (e) { toast(e && e.name === 'AbortError' ? '发送超时，请检查网络' : '发送失败，请检查网络'); }
  finally { clearTimeout(tid); msgInFlight = Math.max(0, msgInFlight - 1); syncMsgBtns(); }
}
function _msgVisibleInEmbed() { const em = document.documentElement.dataset.embed || ''; return !em || em === 'msg'; }
function syncMsgPollTimer() {
  if (!document.hidden && !_embedHidden() && _msgVisibleInEmbed()) { if (!msgPollTimer) msgPollTimer = setInterval(loadMsgs, 5000); }
  else if (msgPollTimer) { clearInterval(msgPollTimer); msgPollTimer = 0; }
}
renderWho(false);
$('quickEdit').addEventListener('input', () => { $('quickEdit').dataset.dirty = '1'; });
/* ---- 2026-11-25: 闹钟与提醒管理（与留言板同在 闹钟留言 页，闹钟在前） ---- */
let alData = null, alEtag = '', alSeq = 0, alBusy = false, alDelId = '', alDelT = 0, alPollTimer = 0;
function toggleAlarmForm(forceOpen) {
  const f = $('alarmForm'), btn = $('alarmAddBtn');
  if (!f || !btn) return;
  const open = typeof forceOpen === 'boolean' ? forceOpen : !f.classList.contains('open');
  f.classList.toggle('open', open);
  btn.classList.toggle('open', open);
  btn.textContent = open ? '收起' : '＋ 新建';
}
function pickAlarmPreset(tm, lbl) {
  if ($('alarmClockInp')) $('alarmClockInp').value = tm;
  if ($('alarmLabelInp')) $('alarmLabelInp').value = lbl;
}
function renderAlarms() {
  const box = $('alarmList'), emp = $('alarmEmpty'), hint = $('alarmHint');
  if (!box || !emp || !alData) return;
  const ring = !!alData.ringing, rInfo = alData.ringing_info || {};
  const ringBar = $('alarmRingBar');
  if (ringBar) {
    ringBar.classList.toggle('on', ring);
    if (ring && $('alarmRingTxt')) {
      $('alarmRingTxt').textContent = '闹钟正在响铃' + (rInfo.label ? '：' + rInfo.label : '');
    }
  }
  const missed = Array.isArray(alData.missed) ? alData.missed : [];
  const missBar = $('alarmMissedBar');
  if (missBar) {
    missBar.classList.toggle('on', missed.length > 0);
    if (missed.length && $('alarmMissedTxt')) {
      $('alarmMissedTxt').textContent = `有 ${missed.length} 条漏响提醒：${missed.map(m => m.label || m.kind || '闹钟').join('、')}`;
    }
  }
  if ($('alarmVolRange') && document.activeElement !== $('alarmVolRange')) {
    const v = Number.isFinite(Number(alData.volume)) ? Number(alData.volume) : 70;
    $('alarmVolRange').value = v;
    if ($('alarmVolVal')) $('alarmVolVal').textContent = v + '%';
  }
  const tones = alData.tones && typeof alData.tones === 'object' ? alData.tones : {};
  const toneKeys = Object.keys(tones);
  if ($('alarmToneSel') && toneKeys.length) {
    $('alarmToneSel').innerHTML = toneKeys.map(k => `<option value="${escHtml(k)}">${escHtml(tones[k])}</option>`).join('');
    if (alData.tone && tones[alData.tone]) $('alarmToneSel').value = alData.tone;
  }
  const items = Array.isArray(alData.items) ? alData.items : [];
  if (!items.length) {
    box.innerHTML = '';
    emp.textContent = '还没有设置闹钟。点右上角「＋ 新建」设置，大屏到点自动响铃。';
    emp.style.display = 'block';
    if (hint) hint.textContent = '到点响铃唤醒屏幕';
    return;
  }
  emp.style.display = 'none';
  if (hint) hint.textContent = `共 ${items.length} 条闹钟`;
  const kLbl = { alarm: '闹钟', timer: '倒计时', reminder: '提醒' };
  const rptLbl = { daily: '每天', workdays: '工作日', weekends: '周末', weekly: '每周', monthly: '每月' };
  box.innerHTML = items.map(it => {
    const idStr = String(it.id);
    const sure = alDelId === idStr;
    const tmStr = it.when || (it.at ? msgTime(it.at) : '');
    const kTxt = kLbl[it.kind] || '闹钟';
    const rTxt = rptLbl[it.repeat] || it.repeat || '';
    const subParts = [it.label || kTxt, it.in ? '约 ' + it.in + ' 后' : '', it.snoozed ? '贪睡中' : ''].filter(Boolean);
    return `<div class="al-item"><div class="al-item-l"><div class="al-item-t"><span class="al-time">${escHtml(tmStr)}</span><span class="al-tag">${escHtml(rTxt || kTxt)}</span></div><div class="al-item-sub">${escHtml(subParts.join(' · ') || '闹钟提醒')}</div></div><button type="button" class="cd-ibtn del${sure ? ' sure' : ''}" onclick="deletePhoneAlarm('${escHtml(idStr)}')">${sure ? '确定删除' : '删除'}</button></div>`;
  }).join('');
}
function alarmLoadFailed() {
  if (alData) return;
  if ($('alarmEmpty')) {
    $('alarmEmpty').textContent = '闹钟暂时读不到，网络恢复后会自动刷新';
    $('alarmEmpty').style.display = 'block';
  }
}
async function loadAlarms(force = false) {
  if (!force && (document.hidden || _embedHidden())) return;
  const mySeq = ++alSeq;
  const ctl = new AbortController(), tid = setTimeout(() => ctl.abort(), 8000);
  try {
    const hdrs = { 'X-Intercom-Msg': '1' };
    if (!force && alEtag) hdrs['If-None-Match'] = alEtag;
    const r = await fetch('api/alarms', { cache: 'no-store', headers: hdrs, signal: ctl.signal });
    if (mySeq !== alSeq) return;
    if (r.status === 304) return;
    if (!r.ok) { alarmLoadFailed(); return; }
    const j = await r.json();
    if (mySeq !== alSeq || !j || !j.ok) return;
    const et = r.headers.get('ETag');
    if (et) alEtag = et;
    alData = j;
    renderAlarms();
  } catch (e) { if (mySeq === alSeq) alarmLoadFailed(); }
  finally { clearTimeout(tid); }
}
async function alarmAct(action, extra = {}) {
  try {
    const r = await fetch('api/alarms', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' },
      body: JSON.stringify({ action, ...extra })
    });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) {
      alEtag = '';
      if (action === 'stop') toast('已停止大屏响铃');
      else if (action === 'snooze') toast('已延后 10 分钟再响');
      else if (action === 'volume') toast('闹钟音量已保存');
      else if (action === 'tone') toast('闹钟铃声已切换');
      await loadAlarms(true);
    } else toast((j && j.error) || '操作失败');
  } catch (e) { toast('网络繁忙，请稍后再试'); }
}
async function savePhoneAlarm() {
  if (alBusy) return;
  const tm = ($('alarmClockInp') && $('alarmClockInp').value || '').trim();
  if (!/^\d{2}:\d{2}$/.test(tm)) { toast('请选择闹钟时间'); return; }
  const rVal = ($('alarmRepeatSel') && $('alarmRepeatSel').value) || '';
  const payload = { action: 'create', kind: 'alarm', time: tm, label: ($('alarmLabelInp') && $('alarmLabelInp').value || '').trim() };
  if (rVal && rVal[0] === 'w' && rVal.length === 2) {
    payload.repeat = 'weekly';
    payload.weekdays = [Number(rVal.slice(1))];
  } else if (rVal === 'weekends') {
    payload.repeat = 'weekly';
    payload.weekdays = [6, 7];
  } else if (rVal) {
    payload.repeat = rVal;
  }
  alBusy = true;
  try {
    const r = await fetch('api/alarms', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' },
      body: JSON.stringify(payload)
    });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) {
      toast('闹钟已同步到大屏');
      if ($('alarmLabelInp')) $('alarmLabelInp').value = '';
      toggleAlarmForm(false);
      alEtag = '';
      await loadAlarms(true);
    } else toast((j && j.error) || '创建闹钟失败');
  } catch (e) { toast('网络繁忙，请稍后再试'); }
  finally { alBusy = false; }
}
async function deletePhoneAlarm(id) {
  if (alDelId !== id) {
    alDelId = id;
    clearTimeout(alDelT);
    alDelT = setTimeout(() => { alDelId = ''; renderAlarms(); }, 3000);
    renderAlarms();
    return;
  }
  alDelId = '';
  clearTimeout(alDelT);
  await alarmAct('delete', { id: Number(id) });
}
function syncAlarmPollTimer() {
  if (!document.hidden && !_embedHidden() && _msgVisibleInEmbed()) {
    if (!alPollTimer) alPollTimer = setInterval(loadAlarms, 6000);
  } else if (alPollTimer) { clearInterval(alPollTimer); alPollTimer = 0; }
}
if (_msgVisibleInEmbed()) { loadMsgs(); syncMsgPollTimer(); loadAlarms(); syncAlarmPollTimer(); }
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && _msgVisibleInEmbed()) {
    if (msgPollTimer) { clearInterval(msgPollTimer); msgPollTimer = 0; }
  if (alPollTimer) { clearInterval(alPollTimer); alPollTimer = 0; }
    if (alPollTimer) { clearInterval(alPollTimer); alPollTimer = 0; }
    loadMsgs(); loadAlarms();
  }
  syncMsgPollTimer(); syncAlarmPollTimer();
});
/* ---- 2026-11-13: Phone Countdown Management ---- */
const PH_LM = ['','正月','二月','三月','四月','五月','六月','七月','八月','九月','十月','冬月','腊月'];
const PH_LD = ['','初一','初二','初三','初四','初五','初六','初七','初八','初九','初十','十一','十二','十三','十四','十五','十六','十七','十八','十九','二十','廿一','廿二','廿三','廿四','廿五','廿六','廿七','廿八','廿九','三十'];
let phCds = [], phCdEtag = '', phCdMode = 'once', phCdCal = 'solar', phCdEditId = '', phCdBusy = false, phCdDelId = '', phCdDelT = 0;
let phCdOpenId = '', phCdFam = ['爸爸', '妈妈', '萌萌', '朵朵'], phCdAll = false;
function togglePhoneCdAll() { phCdAll = !phCdAll; phCdOpenId = ''; phCdDelId = ''; renderPhoneCds(); }
// 2027-06-20 -> 「2027年6月20日 周日」; this year's dates leave the year out (the same wording as the screen at home)
function phCdDate(iso, long, noYear) {
  const mm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  if (!mm) return iso || '';
  const yy = +mm[1], mo = +mm[2], dd = +mm[3];
  if (noYear) return `${mo}月${dd}日`;
  const wd = '日一二三四五六'[new Date(yy, mo - 1, dd).getDay()];
  return `${(long || yy !== new Date().getFullYear()) ? yy + '年' : ''}${mo}月${dd}日 ${long ? '星期' : '周'}${wd}`;
}
function togglePhoneCd(id) { phCdOpenId = phCdOpenId === id ? '' : id; phCdDelId = ''; renderPhoneCds(); }
function renderCdFamChips() {
  const box = $('cdQuickChips');
  if (!box) return;
  box.querySelectorAll('.cd-qchip:not(.pre)').forEach(el => el.remove());
  const first = box.firstElementChild;
  for (const w of phCdFam) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'cd-qchip'; b.textContent = w; b.onclick = () => cdPickWord(w);
    box.insertBefore(b, first);
  }
}

(() => {
  const ms = $('cdLunarM'), ds = $('cdLunarD'), ys = $('cdLunarY');
  const td = new Date();
  if (ms && ds) {
    ms.innerHTML = PH_LM.slice(1).map((n, i) => `<option value="${i + 1}">${n}</option>`).join('');
    ds.innerHTML = PH_LD.slice(1).map((n, i) => `<option value="${i + 1}">${n}</option>`).join('');
  }
  if (ys) {
    const yrs = [];
    for (let yr = 1920; yr <= td.getFullYear() + 20; yr++) yrs.push(`<option value="${yr}"${yr === td.getFullYear() ? ' selected' : ''}>${yr}年</option>`);
    ys.innerHTML = yrs.join('');
  }
  const defOnce = new Date(td.getTime() + 30 * 86400000);
  const iso = `${defOnce.getFullYear()}-${String(defOnce.getMonth() + 1).padStart(2, '0')}-${String(defOnce.getDate()).padStart(2, '0')}`;
  if ($('cdSolarInp')) $('cdSolarInp').value = iso;
})();

let phConv = null, phConvSeq = 0;
async function previewPhoneCd() {
  phConv = null;
  const bar = $('cdPrevBar');
  if (!bar || !$('cdForm').classList.contains('open')) return;
  const now = new Date();
  let y = now.getFullYear(), m = now.getMonth() + 1, d = now.getDate(), lp = 0;
  const syRaw = Number($('cdStartYear').value) || 0;
  if (phCdCal === 'lunar') {
    y = phCdMode === 'yearly' ? (syRaw >= 1920 && syRaw <= 2099 ? syRaw : now.getFullYear()) : (Number($('cdLunarY').value) || now.getFullYear());
    m = Number($('cdLunarM').value) || 1;
    d = Number($('cdLunarD').value) || 1;
    lp = $('cdLunarLeap').checked ? 1 : 0;
  } else {
    const sv = $('cdSolarInp').value || '';
    const mm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(sv);
    if (!mm) return;
    y = +mm[1]; m = +mm[2]; d = +mm[3];
  }
  const seq = ++phConvSeq;
  try {
    const r = await fetch(`api/countdown/convert?cal=${phCdCal}&y=${y}&m=${m}&d=${d}&leap=${lp}${phCdMode === 'yearly' ? '&yearly=1' : ''}`, { cache: 'no-store', headers: { 'X-Intercom-Msg': '1' } });
    if (seq !== phConvSeq) return;
    if (!r.ok) { bar.innerHTML = '<span>请选择有效日期</span>'; return; }
    const j = await r.json();
    if (seq !== phConvSeq) return;
    if (!j || !j.ok) { bar.innerHTML = '<span>请选择有效日期</span>'; return; }
    phConv = j;
    if (phCdMode === 'yearly') {
      const occY = Number(j.next_yearly_occ_year) || +(j.next_yearly_solar || '').slice(0, 4) || now.getFullYear();
      const catGuess = $('cdCatSel').value || (($('cdTitle').value || '').includes('生日') ? 'birthday' : 'anniv');
      const ageS = (syRaw >= 1920 && occY > syRaw) ? ` · 满 ${occY - syRaw}${catGuess === 'birthday' ? '周岁' : '周年'}` : '';
      bar.innerHTML = `<span>下一次：${escHtml(phCdDate(j.next_yearly_solar, true))}${j.lunar_label ? ' · ' + escHtml(j.lunar_label) : ''}${escHtml(ageS)}</span><b>${j.next_yearly_days === 0 ? '就是今天' : '还有 ' + j.next_yearly_days + ' 天'}</b>`;
    } else if (phCdMode === 'countup') {
      const el = -j.days_from_today;
      bar.innerHTML = `<span>开始：${escHtml(phCdDate(j.solar, true))} · ${escHtml(j.lunar_label || '')}</span><b>${el >= 0 ? '已经 ' + el + ' 天' : Math.abs(el) + ' 天后开始'}</b>`;
    } else {
      const rem = j.days_from_today;
      bar.innerHTML = `<span>日期：${escHtml(phCdDate(j.solar, true))} · ${escHtml(j.lunar_label || '')}</span><b>${rem === 0 ? '就是今天' : (rem > 0 ? '还有 ' + rem + ' 天' : '已过 ' + Math.abs(rem) + ' 天')}</b>`;
    }
  } catch (e) {}
}

function syncCdFormUi() {
  $('cdModeSeg').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.m === phCdMode));
  $('cdCalSeg').style.display = 'flex';
  $('cdCalSeg').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.c === phCdCal));
  const useLunar = (phCdCal === 'lunar');
  $('cdSolarRow').style.display = useLunar ? 'none' : 'flex';
  $('cdLunarRow').style.display = useLunar ? 'flex' : 'none';
  if ($('cdLunarY')) $('cdLunarY').style.display = (useLunar && phCdMode !== 'yearly') ? '' : 'none';
  $('cdYearlyExtra').style.display = phCdMode === 'yearly' ? 'flex' : 'none';
  $('cdSaveBtn').textContent = phCdEditId ? '保存修改' : '添加到屏幕';
  previewPhoneCd();
}
function setCdMode(m) {
  phCdMode = m;
  if (m === 'countup' && !phCdEditId) {
    const n = new Date();
    $('cdSolarInp').value = `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}`;
  }
  syncCdFormUi();
}
function setCdCal(c) {
  if (c !== phCdCal && phConv) {
    if (c === 'lunar' && phConv.lunar_m) {
      $('cdLunarM').value = String(phConv.lunar_m); $('cdLunarD').value = String(phConv.lunar_d || 1);
      $('cdLunarLeap').checked = !!phConv.lunar_leap;
      if (phConv.lunar_y && $('cdLunarY').querySelector(`option[value="${phConv.lunar_y}"]`)) $('cdLunarY').value = String(phConv.lunar_y);
    } else if (c === 'solar') {
      const sol = phCdMode === 'yearly' ? (phConv.next_yearly_solar || phConv.solar) : phConv.solar;
      if (sol) $('cdSolarInp').value = sol;
    }
  }
  phCdCal = c; syncCdFormUi();
}
function toggleCdForm(forceOpen) {
  const f = $('cdForm'), btn = $('cdAddBtn');
  const open = typeof forceOpen === 'boolean' ? forceOpen : !f.classList.contains('open');
  if (!open) { phCdEditId = ''; phConv = null; phConvSeq++; }
  else if (!f.classList.contains('open') && !phCdEditId) {
    $('cdTitle').value = '';
    $('cdStartYear').value = '';
    $('cdCatSel').value = '';
    $('cdPinChk').checked = false;
    $('cdLunarLeap').checked = false;
    phCdMode = 'once'; phCdCal = 'solar';
  }
  f.classList.toggle('open', open);
  btn.classList.toggle('open', open);
  btn.textContent = open ? '收起' : '＋ 添加';
  syncCdFormUi();
}
function cdPickWord(w) {
  const inp = $('cdTitle');
  const fam = phCdFam;
  let v = inp.value.trim();
  const hit = fam.find(p => v.startsWith(p));
  inp.value = ((hit ? w + v.slice(hit.length) : w + v)).slice(0, 24);
}
function cdPickPreset(suf, m, c, cat) {
  const inp = $('cdTitle');
  const fam = phCdFam;
  let v = inp.value.trim();
  const hit = fam.find(p => v.startsWith(p)) || '';
  inp.value = (hit + suf).slice(0, 24);
  phCdMode = m;
  $('cdCatSel').value = cat;
  setCdCal(c);
}
function editPhoneCd(id) {
  const it = phCds.find(x => x.id === id);
  if (!it) return;
  phCdEditId = it.id;
  phConv = null; phConvSeq++; phCdDelId = '';
  $('cdTitle').value = it.title || '';
  $('cdCatSel').value = it.category || '';
  $('cdPinChk').checked = !!it.pinned;
  $('cdStartYear').value = it.start_year || '';
  if (it.kind === 'yearly_lunar') {
    phCdMode = 'yearly'; phCdCal = 'lunar';
    $('cdLunarM').value = String(it.lunar_month || 1);
    $('cdLunarD').value = String(it.lunar_day || 1);
    $('cdLunarLeap').checked = !!it.lunar_leap;
  } else if (it.kind === 'yearly_solar') {
    phCdMode = 'yearly'; phCdCal = 'solar';
    let yr = it.start_year || new Date().getFullYear();
    if (Number(it.month) === 2 && Number(it.day) === 29 && !((yr % 4 === 0 && yr % 100 !== 0) || yr % 400 === 0)) yr = 2024;
    $('cdSolarInp').value = `${yr}-${String(it.month || 1).padStart(2, '0')}-${String(it.day || 1).padStart(2, '0')}`;
  } else {
    phCdMode = it.kind === 'countup' ? 'countup' : 'once';
    phCdCal = 'solar';
    if (it.date) $('cdSolarInp').value = it.date;
  }
  $('cdForm').classList.add('open');
  $('cdAddBtn').classList.add('open');
  $('cdAddBtn').textContent = '收起';
  syncCdFormUi();
  try { $('cdBox').scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (e) {}
}
function renderPhoneCds() {
  const box = $('cdList'), emp = $('cdEmpty'), hint = $('cdHint');
  if (!phCds.length) {
    box.innerHTML = '';
    emp.style.display = 'block';
    hint.textContent = '生日 · 考试 · 正数日';
    return;
  }
  emp.style.display = 'none';
  hint.textContent = `共 ${phCds.length} 条`;
  const few = phCds.length <= 4;
  const shown = (phCdAll || few) ? phCds : phCds.slice(0, 3);
  const moreHtml = few ? '' : `<button type="button" class="cd-more" onclick="togglePhoneCdAll()">${phCdAll ? '收起，只看最近 3 条' : '展开全部 ' + phCds.length + ' 条'}</button>`;
  box.innerHTML = shown.map(it => {
    const isPast = it.kind === 'once' && it.days < 0;
    const isTd = it.kind !== 'countup' && it.days === 0;
    const dLbl = it.kind === 'countup' ? (it.days >= 0 ? '已经' : '还有') : (isTd ? '' : (isPast ? '已过' : '还有'));
    const dNum = isTd ? '<b>今天</b>' : `<b>${Math.abs(it.days)}<i>天</i></b>`;
    const sub = it.kind === 'countup' ? `${phCdDate(it.target_date)} 起`
      : (it.kind === 'yearly_lunar' ? `每年${(it.lunar_str || '').replace(/^农历/, '')} · ${phCdDate(it.target_date, false, true)}`
        : (it.kind === 'yearly_solar' ? `每年 · ${phCdDate(it.target_date)}` : phCdDate(it.target_date)));
    const sure = phCdDelId === it.id, open = phCdOpenId === it.id, eid = escHtml(it.id);
    return `<div class="cd-it${isPast ? ' past' : ''}${isTd ? ' today' : ''}${open ? ' open' : ''}" data-cat="${escHtml(it.category || 'life')}">
      <button type="button" class="cd-it-main" aria-expanded="${open}" onclick="togglePhoneCd('${eid}')">
        <div class="cd-it-l">
          <div class="cd-it-t"><i class="cd-dot"></i><span class="cd-name">${escHtml(it.title)}</span>${it.anniv_label ? `<span class="cd-tag">${escHtml(it.anniv_label)}</span>` : ''}${it.pinned ? '<span class="cd-tag pin">置顶</span>' : ''}</div>
          <div class="cd-it-sub">${escHtml(sub)}</div>
        </div>
        <div class="cd-days">${dLbl ? `<small>${dLbl}</small>` : ''}${dNum}</div>
        <svg class="cd-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div class="cd-it-acts">
        <button type="button" class="cd-ibtn${it.pinned ? ' on' : ''}" onclick="pinPhoneCd('${eid}',${!it.pinned})">${it.pinned ? '取消置顶' : '置顶到屏幕主页'}</button>
        <button type="button" class="cd-ibtn" onclick="editPhoneCd('${eid}')">编辑</button>
        <button type="button" class="cd-ibtn del${sure ? ' sure' : ''}" onclick="delPhoneCd('${eid}')">${sure ? '确认删除' : '删除'}</button>
      </div>
    </div>`;
  }).join('') + moreHtml;
}
function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
let phCdSeq = 0, cdPollTimer = 0;
async function loadCds(force = false) {
  if (!force && (document.hidden || _embedHidden())) return;
  const mySeq = ++phCdSeq;
  const ctl = new AbortController(), tid = setTimeout(() => ctl.abort(), 8000);
  try {
    const hdrs = { 'X-Intercom-Msg': '1' };
    if (!force && phCdEtag) hdrs['If-None-Match'] = phCdEtag;
    const r = await fetch('api/countdowns', { cache: 'no-store', headers: hdrs, signal: ctl.signal });
    if (mySeq !== phCdSeq) return;
    if (r.status === 304) return;
    if (!r.ok) { cdLoadFailed(); return; }
    const j = await r.json();
    if (mySeq !== phCdSeq) return;
    const et = r.headers.get('ETag');
    if (et) phCdEtag = et;
    if (j && Array.isArray(j.family) && j.family.length) {
      const fam = j.family.filter(x => typeof x === 'string' && x).slice(0, 8);
      if (fam.length && fam.join('|') !== phCdFam.join('|')) { phCdFam = fam; renderCdFamChips(); }
    }
    if (j && Array.isArray(j.items)) { phCds = j.items; $('cdEmpty').textContent = '还没有倒数日。点右上角「＋ 添加」，家里的屏幕会同步显示。'; renderPhoneCds(); }
  } catch (e) { if (mySeq === phCdSeq) cdLoadFailed(); }
  finally { clearTimeout(tid); }
}
// 2026-11-18 Claude: a failed first load is not 「还没有倒数日」
function cdLoadFailed() {
  if (phCds.length) return;
  $('cdEmpty').textContent = '倒数日暂时读不到，网络恢复后会自动刷新';
  $('cdEmpty').style.display = 'block';
}
async function savePhoneCd() {
  if (phCdBusy) return;
  const title = $('cdTitle').value.trim();
  if (!title) { toast('先起个名字'); $('cdTitle').focus(); return; }
  const payload = { title, pinned: $('cdPinChk').checked };
  if (phCdEditId) payload.id = phCdEditId;
  if ($('cdCatSel').value) payload.category = $('cdCatSel').value;
  if (phCdMode === 'yearly') {
    const sy = $('cdStartYear').value.trim();
    if (sy) {
      const syn = Number(sy);
      if (!Number.isInteger(syn) || syn < 1920 || syn > 2099) { toast('起始年份需在 1920–2099 之间'); return; }
      payload.start_year = syn;
    }
    if (phCdCal === 'lunar') {
      payload.kind = 'yearly_lunar';
      payload.lunar_month = Number($('cdLunarM').value);
      payload.lunar_day = Number($('cdLunarD').value);
      payload.lunar_leap = $('cdLunarLeap').checked;
    } else {
      const sv = $('cdSolarInp').value;
      const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(sv);
      if (!m) { toast('请选择公历日期'); return; }
      payload.kind = 'yearly_solar';
      payload.month = Number(m[2]);
      payload.day = Number(m[3]);
    }
  } else if (phCdCal !== 'lunar') {
    payload.kind = phCdMode;
    const sv = $('cdSolarInp').value;
    if (!sv) { toast('请选择日期'); return; }
    payload.date = sv;
  }
  phCdBusy = true;
  try {
    if (phCdMode !== 'yearly' && phCdCal === 'lunar') {
      payload.kind = phCdMode;
      if (!phConv || !phConv.solar) await previewPhoneCd();
      if (!phConv || !phConv.solar) { toast('请检查农历日期是否有效'); return; }
      payload.date = phConv.solar;
    }
    const r = await fetch('api/countdown', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' },
      body: JSON.stringify(payload)
    });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) {
      toast(phCdEditId ? '倒数日已更新' : '已同步到家里的屏幕');
      toggleCdForm(false);
      phCdEtag = '';
      await loadCds(true);
    } else {
      toast((j && j.error) || '保存失败');
    }
  } catch (e) { toast('网络繁忙，请稍后再试'); }
  finally { phCdBusy = false; }
}
async function pinPhoneCd(id, pinned) {
  try {
    const r = await fetch('api/countdown/pin', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' },
      body: JSON.stringify({ id, pinned })
    });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) { toast(pinned ? '已置顶到屏幕主页' : '已取消置顶'); phCdEtag = ''; loadCds(true); }
  } catch (e) {}
}
async function delPhoneCd(id) {
  if (phCdDelId !== id) {
    phCdDelId = id;
    clearTimeout(phCdDelT);
    phCdDelT = setTimeout(() => { phCdDelId = ''; renderPhoneCds(); }, 3000);
    renderPhoneCds();
    return;
  }
  phCdDelId = '';
  clearTimeout(phCdDelT);
  try {
    const r = await fetch('api/countdown/delete', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Intercom-Msg': '1' },
      body: JSON.stringify({ id })
    });
    const j = await r.json().catch(() => null);
    if (r.ok && j && j.ok) { toast('已删除'); phCdEtag = ''; loadCds(true); }
  } catch (e) {}
}
function _cdVisibleInEmbed() { const em = document.documentElement.dataset.embed || ''; return !em || em === 'cd'; }
function syncCdPollTimer() {
  if (!_cdVisibleInEmbed() || document.hidden || _embedHidden()) {
    if (cdPollTimer) { clearInterval(cdPollTimer); cdPollTimer = 0; }
    return;
  }
  if (!cdPollTimer) cdPollTimer = setInterval(() => { if (!document.hidden && !_embedHidden()) loadCds(); }, 12000);
}
if (_cdVisibleInEmbed()) {
  loadCds();
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !_embedHidden()) loadCds(); syncCdPollTimer(); });
  window.addEventListener('pagehide', () => { if (cdPollTimer) { clearInterval(cdPollTimer); cdPollTimer = 0; } });
  window.addEventListener('pageshow', (ev) => { if (ev.persisted && _cdVisibleInEmbed() && !_embedHidden()) { loadCds(); syncCdPollTimer(); } });
  syncCdPollTimer();
}
/* 2026-11-20 Claude：给管理后台用：离开对讲时问一句还在不在通话；后台切深浅色时跟着变 */
window.tsCallBusy = () => !!(inCall || callStarting || listenOnly);
window.tsEmbedSetActive = (active) => {
  embedActive = !!active;
  syncClockTimer();
  if (!embedActive) {
    if (typeof stopPhoneVoiceRec === 'function' && (vRecStream || vRecStarting)) stopPhoneVoiceRec(false);
    if (typeof stopPhoneVoicePlay === 'function') stopPhoneVoicePlay(false);
    if (wsReconnectTimer && !inCall && !listenOnly) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    syncVideoRx();
    syncCdPollTimer();
    syncMsgPollTimer();
    syncAlarmPollTimer();
  } else {
    const _em = document.documentElement.dataset.embed || '';
    if (!document.hidden && _em !== 'msg' && _em !== 'cd' && (!ws || ws.readyState > 1)) connectWS();
    else syncVideoRx();
    if (hiddenJpeg && !shutterClosed && (performance.now() - hiddenJpegTs) <= 3000 && ws && ws.readyState === 1) {
      pendingJpeg = hiddenJpeg;
      hiddenJpeg = null; hiddenJpegTs = 0;
      pumpJpegQueue(ws);
    } else {
      hiddenJpeg = null; hiddenJpegTs = 0;
    }
    if (_msgVisibleInEmbed()) { loadMsgs(); syncMsgPollTimer(); loadAlarms(); syncAlarmPollTimer(); }
    if (_cdVisibleInEmbed()) { loadCds(); syncCdPollTimer(); }
  }
};
window.addEventListener('message', e => {
  if (window.parent !== window && e.source === window.parent && e.data && e.data.ts === 'admin-embed' && e.data.active === false) {
    window.tsEmbedSetActive(false);
  }
});
window.tsSetTheme = pref => {
  const t = pref === 'light' || pref === 'dark' ? pref : 'auto';
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  try { t === 'auto' ? localStorage.removeItem('ts-theme') : localStorage.setItem('ts-theme', t); } catch (e) {}
  renderTheme();
};
</script>
</body>
</html>
"""
HTML_PAGE_BYTES = HTML_PAGE.encode("utf-8")
HTML_PAGE_ETAG = f'"{hashlib.sha1(HTML_PAGE_BYTES).hexdigest()[:16]}"'


_ANY_CALL = object()


class IntercomServer:
    def __init__(self):
        self.clients = set()
        self.all_ws = set()
        self.cam_proc = None
        self.cam_task = None
        self.mic_proc = None
        self.mic_task = None
        self.spk_proc = None
        self.ui_proc = None
        self.ui_task = None
        self.in_call = False
        self.call_owner = None
        self.listeners = set()
        self.frame_seq = 0
        self.sensor_rot = 0
        self.flip_h = True
        self._pending_sensor_rot = None
        self._pending_sensor_count = 0
        self.saved_spk_vol = None
        self.saved_ambient = None
        self._last_spk_respawn = 0.0
        self.ui_stdin_lock = asyncio.Lock()
        self.camera_lock = asyncio.Lock()
        self.mic_lock = asyncio.Lock()
        self.call_lock = asyncio.Lock()
        self.mic_restarts = []
        self.cam_restarts = []      # monotonic times of unexpected camera-helper exits
        self.frame_div = {}         # ws -> send every Nth frame (2 = data-saver mode)
        self.video_paused = set()   # ws with hidden/background tab skipping camera TX
        self._active_ev = asyncio.Event()
        self._cam_backoff_until = 0.0
        self._mic_backoff_until = 0.0
        self.sensor_rot = self.read_sensor_rot()

    @property
    def rot_deg(self) -> int:
        return self.sensor_rot % 360

    def _resolve_accel_xy(self):
        if ACCEL_X_PATH.exists() and ACCEL_Y_PATH.exists():
            return ACCEL_X_PATH, ACCEL_Y_PATH
        cached = getattr(self, "_cached_accel_xy", None)
        if cached is not None and cached[0].exists() and cached[1].exists():
            return cached
        try:
            for d in sorted(Path("/sys/bus/iio/devices").glob("iio:device*")):
                xp, yp = d / "in_accel_x_raw", d / "in_accel_y_raw"
                if xp.exists() and yp.exists():
                    self._cached_accel_xy = (xp, yp)
                    return xp, yp
        except Exception:
            pass
        return ACCEL_X_PATH, ACCEL_Y_PATH

    def read_sensor_rot(self) -> int:
        try:
            xp, yp = self._resolve_accel_xy()
            x = int(xp.read_text().strip())
            y = int(yp.read_text().strip())
            ax, ay = abs(x), abs(y)
            # Deadband when device is lying nearly flat on its back/screen (< ~0.29g in XY plane)
            if max(ax, ay) < 150:
                return self.sensor_rot
            # Hysteresis near 45-degree diagonal to prevent oscillation
            if abs(ax - ay) < 70:
                return self.sensor_rot
            if ax >= ay:
                return 0 if x >= 0 else 180
            else:
                return 90 if y < 0 else 270
        except Exception:
            return self.sensor_rot

    def orient_dict(self, mtype: str = "orient") -> dict:
        return {
            "type": mtype,
            "rot_deg": self.rot_deg,
            "sensor_rot": self.sensor_rot,
            "flip_h": self.flip_h,
        }

    def ubuntu_env(self):
        env = getattr(self, "_ubuntu_env", None)
        if env is None:
            env = os.environ.copy()
            env["HOME"] = "/home/ubuntu"
            env["USER"] = "ubuntu"
            env["LOGNAME"] = "ubuntu"
            env["XDG_RUNTIME_DIR"] = "/run/user/1000"
            env["PULSE_SERVER"] = "unix:/run/user/1000/pulse/native"
            env["WAYLAND_DISPLAY"] = "wayland-1"
            self._ubuntu_env = env
        return env

    async def send_to_ui(self, ftype: int, data: bytes, drop_if_busy: bool = False):
        if not self.ui_proc or self.ui_proc.returncode is not None or not self.ui_proc.stdin:
            return
        if drop_if_busy and self.ui_stdin_lock.locked():
            return
        async with self.ui_stdin_lock:
            if not self.ui_proc or self.ui_proc.returncode is not None or not self.ui_proc.stdin:
                return
            try:
                tr = getattr(self.ui_proc.stdin, "transport", None)
                if drop_if_busy and tr is not None and tr.get_write_buffer_size() > 65536:
                    return
                self.ui_proc.stdin.write(struct.pack("<BI", ftype, len(data)))
                self.ui_proc.stdin.write(data)
                await asyncio.wait_for(self.ui_proc.stdin.drain(), timeout=0.5)
            except Exception:
                pass

    def read_gpio_switches(self):
        shutter_closed = False
        hw_mic_muted = False
        found = 0
        try:
            with open("/sys/kernel/debug/gpio", "r") as f:
                for line in f:
                    if "gpio87 " in line:
                        shutter_closed = (" low " in line)
                        found |= 1
                        if found == 3:
                            break
                    elif "gpio86 " in line:
                        hw_mic_muted = (" high " in line)
                        found |= 2
                        if found == 3:
                            break
        except Exception:
            pass
        return shutter_closed, hw_mic_muted

    def set_webkit_paused(self, paused: bool):
        if not paused and MIRROR_FLAG_PATH.exists():
            return
        screen_blanked = False
        if not paused and BACKLIGHT_PATH.exists():
            try:
                screen_blanked = int(BACKLIGHT_PATH.read_text().strip()) <= 10
            except Exception:
                screen_blanked = False
        sig = signal.SIGSTOP if paused else signal.SIGCONT
        my_pid = os.getpid()
        try:
            entries = os.scandir("/proc")
        except Exception:
            return
        with entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    pid = int(entry.name)
                    if pid == my_pid or entry.stat(follow_symlinks=False).st_uid != 1000:
                        continue
                    with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                        parts = f.read(512).split(b"\x00")[:4]
                    raw0 = parts[0] if parts else b""
                    is_webproc = raw0 == b"WebKitWebProcess" or raw0.endswith(b"/WebKitWebProcess")
                    is_net_or_kiosk = (
                        raw0 == b"WebKitNetworkProcess"
                        or raw0.endswith(b"/WebKitNetworkProcess")
                        or any(
                            p == b"/usr/local/bin/thinksmart-kiosk.py" or p.endswith(b"/thinksmart-kiosk.py")
                            for p in parts[:4]
                        )
                    )
                    if is_webproc and screen_blanked and not paused:
                        continue
                    if is_webproc or is_net_or_kiosk:
                        try:
                            with open(os.path.join(entry.path, "stat"), "r", encoding="utf-8", errors="ignore") as f:
                                stat_txt = f.read(256)
                            if ")" in stat_txt:
                                pstate = stat_txt.rsplit(")", 1)[1].split(None, 1)[0]
                                if paused and pstate in ("T", "t", "Z", "X"):
                                    continue
                                if not paused and pstate not in ("T", "t"):
                                    continue
                        except OSError:
                            pass
                        os.kill(pid, sig)
                except Exception:
                    pass

    def wake_screen(self, hold=True, preserve_hold=False):
        if not hold and (INTERCOM_CALL_FLAG.exists() or MIRROR_FLAG_PATH.exists()):
            return
        try:
            hint_path = Path("/run/user/1000/thinksmart-screen.json")
            data = {}  # 2026-11-06: no invented brightness/idle defaults; idle-blanker applies its own
            if hint_path.exists() and not hint_path.is_symlink():
                try:
                    loaded = json.loads(_read_regular_file(hint_path))
                    if isinstance(loaded, dict):
                        data.update(loaded)
                except Exception:
                    pass
            eff_hold = bool(hold or (preserve_hold and data.get("hold") is True))
            data["seq"] = time.time_ns()
            data["hold"] = eff_hold
            idle_val = data.get("idle")
            if not eff_hold and type(idle_val) is int and idle_val > 0:
                data["after"] = idle_val
            else:
                data.pop("after", None)
            tmp = hint_path.with_name(f".{hint_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            created = False
            try:
                if hint_path.is_symlink():
                    raise OSError("Refusing symlink hint path")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
                created = True
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    try:
                        os.fchmod(tf.fileno(), 0o644)
                        os.fchown(tf.fileno(), 1000, 1000)
                    except OSError:
                        pass
                    tf.write(json.dumps(data))
                if hint_path.is_symlink():
                    raise OSError("Refusing symlink hint path")
                os.replace(tmp, hint_path)
            finally:
                if created:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass
        if hold:
            try:
                bl = BACKLIGHT_PATH
                if int(bl.read_text().strip()) <= 50:
                    ts_now = time.time()
                    sunrise_active = False
                    try:
                        sr = json.loads(_read_regular_file(Path("/run/user/1000/thinksmart-sunrise.json")))
                        if isinstance(sr, dict) and "stopped" not in sr:
                            st, ramp, shold = sr.get("start"), sr.get("ramp"), sr.get("hold")
                            if (
                                all(type(v) in (int, float) and math.isfinite(v) for v in (st, ramp, shold))
                                and 0 < ramp <= 3600 and 0 <= shold <= 7200 and st <= ts_now < st + ramp + shold
                            ):
                                sunrise_active = True
                    except Exception:
                        pass
                    night_low = False
                    if not sunrise_active:
                        try:
                            nv = json.loads(_read_regular_file(Path("/run/user/1000/thinksmart-night-view")))
                            if isinstance(nv, dict) and type(nv.get("until")) in (int, float) and ts_now < nv["until"]:
                                night_low = True
                        except Exception:
                            pass
                        if not night_low:
                            lt = time.localtime(ts_now)
                            night_low = (0, 0) <= (lt.tm_hour, lt.tm_min) < (6, 30)
                    if night_low:
                        target_bl = 1400
                    else:
                        mode_d = {}
                        try:
                            m = json.loads(_read_regular_file(Path("/var/lib/thinksmart-monitor/display.json")))
                            if isinstance(m, dict):
                                mode_d = m
                        except Exception:
                            pass
                        b = data.get("brightness")
                        valid_b = type(b) in (int, float) and math.isfinite(b) and 0 <= b <= 100
                        anchor = mode_d.get("anchor")
                        anchor_b = b if valid_b else 100
                        is_auto = mode_d.get("auto") is True and type(anchor) in (int, float) and math.isfinite(anchor) and anchor == anchor_b
                        target_bl = None
                        if not is_auto and valid_b:
                            target_bl = 1400 + round((4095 - 1400) * max(0, min(100, b)) / 100)
                        if target_bl is None:
                            target_bl = 1600 if is_auto else 2000
                            for ub_path in (Path("/run/user/1000/user_brightness"), Path("/tmp/user_brightness")):
                                if ub_path.exists() and not ub_path.is_symlink():
                                    try:
                                        v = int(_read_regular_file(ub_path, 64).strip())
                                        if 10 < v <= 4095:
                                            target_bl = v
                                            break
                                    except Exception:
                                        pass
                        if data.get("dim"):
                            target_bl = min(1600, target_bl)
                    bl.write_text(f"{target_bl}\n")
            except Exception:
                pass

    @staticmethod
    def _ws_backpressured(ws, max_buf: int) -> bool:
        writer = getattr(ws, "_writer", None)
        if writer is None:
            return False
        proto = getattr(writer, "protocol", None)
        if proto is not None and (getattr(proto, "_paused", False) or getattr(proto, "_drain_waiter", None) is not None):
            return True
        tr = getattr(writer, "transport", None)
        return bool(tr is not None and tr.get_write_buffer_size() > max_buf)

    async def _send_json_fast(self, ws, payload: dict, max_buf: int | None = 131072, timeout: float = 0.35):
        if ws is None or getattr(ws, "closed", True):
            return
        if max_buf is not None and self._ws_backpressured(ws, max_buf):
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.send_json(payload), timeout=timeout)

    async def broadcast_status(self):
        if not self.clients:
            return
        shutter_closed, hw_mic_muted = self.read_gpio_switches()
        base_msg = self.orient_dict("status")
        base_msg["shutter_closed"] = shutter_closed
        base_msg["hw_mic_muted"] = hw_mic_muted
        base_msg["in_call"] = self.in_call
        for ws in list(self.clients):
            msg = dict(base_msg)
            msg["is_call_owner"] = (ws is self.call_owner)
            await self._send_json_fast(ws, msg)

    async def sync_ui_orient(self):
        data = bytes([(self.rot_deg // 90) & 3, int(self.flip_h), (self.sensor_rot // 90) & 3])
        await self.send_to_ui(3, data, drop_if_busy=False)

    async def broadcast_orient(self, exclude_ws=None):
        msg = self.orient_dict("orient")
        for c in list(self.clients):
            if c is not exclude_ws:
                await self._send_json_fast(c, msg)

    async def ensure_camera_running(self):
        async with self.camera_lock:
            return await self._ensure_camera_running()

    @staticmethod
    async def _kill_stale(proc):
        if proc is not None:
            try:
                if getattr(proc, "stdin", None):
                    proc.stdin.close()
            except Exception:
                pass
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass

    def _wake_status_loop(self):
        ev = getattr(self, "_active_ev", None)
        if ev is not None:
            ev.set()

    async def _ensure_camera_running(self):
        if not self.in_call and time.monotonic() < getattr(self, "_cam_backoff_until", 0.0):
            return
        self._wake_status_loop()
        if (
            self.cam_proc is not None
            and self.cam_proc.returncode is None
            and self.cam_task is not None
            and not self.cam_task.done()
        ):
            return
        # 2026-11-07 Claude: the reader task is gone but the helper may still run and hold the camera;
        # stop it first, or the new helper cannot open the camera and the old one is orphaned
        await self._kill_stale(self.cam_proc)
        print("[intercom] Starting s5kc505a_cam_helper...", flush=True)
        self.cam_proc = await asyncio.create_subprocess_exec(
            "/usr/local/bin/s5kc505a_cam_helper",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.cam_task = asyncio.create_task(self.camera_read_loop())

    def _has_video_demand(self, shutter_closed: bool = False) -> bool:
        if shutter_closed:
            return False
        return bool(self.in_call or any(c not in getattr(self, "video_paused", ()) for c in self.clients))

    async def stop_camera_if_idle(self, fast: bool = False, shutter_closed: bool | None = None):
        if len(self.clients) == 0:
            await self.stop_call(fast=fast)
            await self.stop_mic_if_idle()
        async with self.camera_lock:
            return await self._stop_camera_if_idle(fast=fast, shutter_closed=shutter_closed)

    async def _stop_camera_if_idle(self, fast: bool = False, shutter_closed: bool | None = None):
        if shutter_closed is None and self._has_video_demand(shutter_closed=False):
            shutter_closed = bool(self.read_gpio_switches()[0])
        sc = bool(shutter_closed)
        if not sc and len(self.clients) == 0 and self.in_call:
            return
        if self._has_video_demand(shutter_closed=sc):
            return
        proc, ctask, self.cam_proc, self.cam_task = self.cam_proc, self.cam_task, None, None
        if proc and proc.returncode is None:
            print("[intercom] No clients left, stopping camera (0% CPU idle)...", flush=True)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=1.2 if fast else 3.0)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=1.0 if fast else 2.0)
                except Exception:
                    pass
        if ctask and ctask is not asyncio.current_task() and not ctask.done():
            ctask.cancel()
            await asyncio.gather(ctask, return_exceptions=True)

    async def camera_read_loop(self):
        proc = self.cam_proc
        good_frames = 0
        try:
            while proc and proc.returncode is None and proc.stdout:
                hdr = await proc.stdout.readexactly(4)
                flen = struct.unpack("<I", hdr)[0]
                if flen == 0 or flen > 2_000_000:
                    break
                jpg = await proc.stdout.readexactly(flen)
                self.frame_seq += 1
                good_frames += 1
                if good_frames == 30 and self.cam_restarts:
                    self.cam_restarts.clear()

                # s5kc505a_cam_helper natively outputs 15fps JPEGs; forward every frame to WebSocket viewers
                payload = None
                dead = []
                for ws in list(self.clients):
                    if ws in getattr(self, "video_paused", ()):
                        continue
                    div = self.frame_div.get(ws, 1)
                    if div > 1 and self.frame_seq % div:
                        continue
                    try:
                        if self._ws_backpressured(ws, 65536):
                            continue
                        if payload is None:
                            payload = b"\x01" + jpg
                        await asyncio.wait_for(ws.send_bytes(payload), timeout=0.4)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        dead.append(ws)
                dead_owner = None
                for ws in dead:
                    self.clients.discard(ws)
                    self.listeners.discard(ws)
                    self.frame_div.pop(ws, None)
                    self.video_paused.discard(ws)
                    if ws is self.call_owner:
                        dead_owner = ws
                if dead_owner is not None and self.call_owner is dead_owner:
                    await self.stop_call(expected_owner=dead_owner)
                if dead:
                    await self.stop_mic_if_idle()
                    if not self._has_video_demand():
                        await self.stop_camera_if_idle()
                        break

                if self.in_call and self.ui_proc and self.ui_proc.returncode is None and (self.frame_seq % 2 == 0):
                    await self.send_to_ui(2, jpg, drop_if_busy=True)
        except Exception:
            pass
        await self.camera_ended(proc)

    async def camera_ended(self, proc):
        """The helper stopped sending frames. If viewers are still watching and nobody asked it to
        stop, restart it (at most 5 times a minute) instead of leaving their picture frozen."""
        def _demand_now():
            if not self._has_video_demand(shutter_closed=False):
                return False
            return self._has_video_demand(shutter_closed=bool(self.read_gpio_switches()[0]))

        if proc is None or proc is not self.cam_proc or not _demand_now():
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            rc = proc.returncode
        if proc is not self.cam_proc or not _demand_now():
            return
        now = time.monotonic()
        self.cam_restarts = [t for t in self.cam_restarts if now - t < 60] + [now]
        if len(self.cam_restarts) > 5:
            print(f"[intercom] Camera helper keeps failing (rc={rc}); backing off 15s", flush=True)
            self._cam_backoff_until = now + 15.0
            async with self.camera_lock:
                if proc is self.cam_proc:
                    self.cam_proc, self.cam_task = None, None
            return
        print(f"[intercom] Camera helper exited unexpectedly (rc={rc}); restarting in 1s", flush=True)
        await asyncio.sleep(1.0)
        async with self.camera_lock:
            if proc is self.cam_proc and _demand_now():
                self.cam_proc = None
                await self._ensure_camera_running()

    async def ensure_mic_running(self):
        async with self.mic_lock:
            return await self._ensure_mic_running()

    async def _ensure_mic_running(self):
        if not self.in_call and time.monotonic() < getattr(self, "_mic_backoff_until", 0.0):
            return
        if (
            self.mic_proc is not None
            and self.mic_proc.returncode is None
            and self.mic_task is not None
            and not self.mic_task.done()
        ):
            return
        self._wake_status_loop()
        await self._kill_stale(self.mic_proc)
        print("[intercom] Starting PipeWire mic capture (thinksmart_mic)...", flush=True)
        self.mic_proc = await asyncio.create_subprocess_exec(
            "/usr/bin/parec",
            "--device=thinksmart_mic",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=40",
            user="ubuntu",
            group="ubuntu",
            env=self.ubuntu_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.mic_task = asyncio.create_task(self.mic_read_loop())

    async def stop_mic_if_idle(self):
        async with self.mic_lock:
            return await self._stop_mic_if_idle()

    async def _stop_mic_if_idle(self):
        if len(self.listeners) > 0 or self.in_call:
            return
        proc, mtask, self.mic_proc, self.mic_task = self.mic_proc, self.mic_task, None, None
        if proc and proc.returncode is None:
            print("[intercom] Stopping PipeWire mic capture...", flush=True)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
        if mtask and mtask is not asyncio.current_task() and not mtask.done():
            mtask.cancel()
            await asyncio.gather(mtask, return_exceptions=True)

    async def mic_read_loop(self):
        proc = self.mic_proc
        tx_buf = bytearray(1 + 1280)
        tx_buf[0] = 2
        tx_mv = memoryview(tx_buf)
        good_chunks = 0
        try:
            while proc and proc.returncode is None and proc.stdout:
                pcm = await proc.stdout.readexactly(1280)
                if not pcm:
                    break
                good_chunks += 1
                if good_chunks == 50 and self.mic_restarts:
                    self.mic_restarts.clear()
                if not self.listeners and not (self.in_call and self.call_owner is not None):
                    continue
                tx_buf[1:] = _scale_pcm_s16le(pcm, 8.0)
                targets = set(self.listeners)
                if self.in_call and self.call_owner is not None:
                    targets.add(self.call_owner)
                dead = []
                for ws in targets:
                    try:
                        if self._ws_backpressured(ws, 32768):
                            continue
                        await asyncio.wait_for(ws.send_bytes(tx_mv), timeout=0.25)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        dead.append(ws)
                dead_owner = None
                for ws in dead:
                    self.clients.discard(ws)
                    self.listeners.discard(ws)
                    self.frame_div.pop(ws, None)
                    self.video_paused.discard(ws)
                    if ws is self.call_owner:
                        dead_owner = ws
                if dead_owner is not None and self.call_owner is dead_owner:
                    await self.stop_call(expected_owner=dead_owner)
                if dead:
                    await self.stop_mic_if_idle()
                    if not self._has_video_demand():
                        await self.stop_camera_if_idle()
                    if not self.listeners and not self.in_call:
                        break
        except Exception:
            pass
        await self.mic_ended(proc)

    async def mic_ended(self, proc):
        if proc is None or proc is not self.mic_proc or not (self.listeners or self.in_call):
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            rc = proc.returncode
        if proc is not self.mic_proc or not (self.listeners or self.in_call):
            return
        now = time.monotonic()
        self.mic_restarts = [t for t in self.mic_restarts if now - t < 60] + [now]
        if len(self.mic_restarts) > 5 and not self.in_call:
            print(f"[intercom] Mic capture keeps failing (rc={rc}); backing off 15s", flush=True)
            self._mic_backoff_until = now + 15.0
            async with self.mic_lock:
                if proc is self.mic_proc:
                    self.mic_proc, self.mic_task = None, None
            return
        retry_delay = 3.0 if len(self.mic_restarts) > 5 else 1.0
        print(f"[intercom] Mic capture exited (rc={rc}); restarting in {retry_delay:.0f}s", flush=True)
        await asyncio.sleep(retry_delay)
        async with self.mic_lock:
            if proc is self.mic_proc and (self.listeners or self.in_call):
                self.mic_proc = None
                await self._ensure_mic_running()

    async def get_speaker_volume(self):
        try:
            p = await asyncio.create_subprocess_exec(
                "/usr/bin/pactl", "get-sink-volume",
                "alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink",
                user="ubuntu",
                group="ubuntu",
                env=self.ubuntu_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(p.communicate(), timeout=2.0)
            except BaseException as exc:
                with contextlib.suppress(ProcessLookupError, OSError):
                    p.kill()
                with contextlib.suppress(BaseException):
                    await p.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return None
            m = re.search(r"(\d+)%", out.decode("utf-8", "ignore"))
            if m:
                return f"{m.group(1)}%"
        except Exception:
            pass
        return None

    async def get_speaker_mute(self):
        try:
            p = await asyncio.create_subprocess_exec(
                "/usr/bin/pactl", "get-sink-mute",
                "alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink",
                user="ubuntu",
                group="ubuntu",
                env=self.ubuntu_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(p.communicate(), timeout=2.0)
            except BaseException as exc:
                with contextlib.suppress(ProcessLookupError, OSError):
                    p.kill()
                with contextlib.suppress(BaseException):
                    await p.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return None
            txt = out.decode("utf-8", "ignore").lower()
            if "yes" in txt or "是" in txt:
                return True
            if "no" in txt or "否" in txt:
                return False
        except Exception:
            pass
        return None

    async def set_speaker_mute(self, muted: bool):
        try:
            p = await asyncio.create_subprocess_exec(
                "/usr/bin/pactl", "set-sink-mute",
                "alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink",
                "1" if muted else "0",
                user="ubuntu",
                group="ubuntu",
                env=self.ubuntu_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(p.wait(), timeout=2.0)
            except BaseException as exc:
                with contextlib.suppress(ProcessLookupError, OSError):
                    p.kill()
                with contextlib.suppress(BaseException):
                    await p.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
        except Exception:
            pass

    @staticmethod
    async def _get_json_safe(sess, url: str, timeout_s: float = 1.5):
        try:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
                if r.status == 200:
                    return await r.json()
                await r.read()
        except Exception:
            pass
        return None

    async def set_speaker_volume(self, vol_pct: str):
        try:
            p = await asyncio.create_subprocess_exec(
                "/usr/bin/pactl", "set-sink-volume",
                "alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink",
                vol_pct,
                user="ubuntu",
                group="ubuntu",
                env=self.ubuntu_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(p.wait(), timeout=2.0)
            except BaseException as exc:
                with contextlib.suppress(ProcessLookupError, OSError):
                    p.kill()
                with contextlib.suppress(BaseException):
                    await p.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
        except Exception:
            pass

    _BT_PAUSE_CODE = (
        'import dbus; bus = dbus.SystemBus(private=True)\n'
        'try:\n'
        '    om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")\n'
        '    for p, ifs in om.GetManagedObjects().items():\n'
        '        mp = ifs.get("org.bluez.MediaPlayer1")\n'
        '        if mp and mp.get("Status") == "playing":\n'
        '            dbus.Interface(bus.get_object("org.bluez", p), "org.bluez.MediaPlayer1").Pause()\n'
        'finally:\n'
        '    bus.close()\n'
    )

    @classmethod
    def pause_bluetooth_media(cls):
        """Pause any playing Bluetooth A2DP/AVRCP phone stream via BlueZ SystemBus (/usr/bin/python3 has python3-dbus)."""
        try:
            subprocess.run(
                ["/usr/bin/python3", "-c", cls._BT_PAUSE_CODE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
        except Exception:
            pass

    def _write_call_flag(self, payload_str: str = "1\n"):
        try:
            flag_tmp = INTERCOM_CALL_FLAG.with_name(f".{INTERCOM_CALL_FLAG.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            created = False
            try:
                if INTERCOM_CALL_FLAG.is_symlink():
                    raise OSError("Refusing symlink call flag")
                fd = os.open(flag_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
                created = True
                with os.fdopen(fd, "w", encoding="utf-8") as ff:
                    try:
                        os.fchmod(ff.fileno(), 0o644)
                        os.fchown(ff.fileno(), 1000, 1000)
                    except OSError:
                        pass
                    ff.write(payload_str)
                if INTERCOM_CALL_FLAG.is_symlink():
                    raise OSError("Refusing symlink call flag")
                os.replace(flag_tmp, INTERCOM_CALL_FLAG)
            finally:
                if created:
                    try:
                        flag_tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            pass

    async def start_call(self, owner_ws=None) -> bool:
        async with self.call_lock:
            self._took_call_now = False
            try:
                return await self._start_call(owner_ws)
            except BaseException:
                if self._took_call_now:
                    await asyncio.shield(self._stop_call())
                raise
            finally:
                self._took_call_now = False

    async def _start_call(self, owner_ws=None) -> bool:
        self._wake_status_loop()
        if owner_ws is not None and owner_ws.closed:
            return False
        had_stale_reset = False
        prev_amb = None
        prev_amb_owned = False
        if self.in_call and (self.ui_proc is None or (self.ui_proc is not None and self.ui_proc.returncode is not None)):
            print("[intercom] Call UI died previously; resetting stale call before restarting", flush=True)
            had_stale_reset = True
            prev_amb = self.saved_ambient
            prev_amb_owned = bool(getattr(self, "saved_ambient_owned", False))
            await self._stop_call(fast=True)
        if self.in_call:
            if self.call_owner is not None and self.call_owner is not owner_ws and not self.call_owner.closed:
                return False
            if self.call_owner is not owner_ws:
                self.call_owner = owner_ws
                await self.send_to_ui(4, b"")
            if not self.read_gpio_switches()[0]:
                await self.ensure_camera_running()
            await self.ensure_mic_running()
            await self.broadcast_status()
            return True
        self._took_call_now = True
        self.in_call = True
        self.call_owner = owner_ws
        self.saved_ambient = prev_amb if had_stale_reset else None
        self.saved_spk_mute = None
        self.saved_spk_vol = None
        self._write_call_flag("1\n")
        if MIRROR_FLAG_PATH.exists() or Path("/run/user/1000/uxplay-audio-active").exists():
            print("[intercom] Preempting active AirPlay session for incoming two-way call...", flush=True)
            try:
                await asyncio.to_thread(subprocess.run, ["/usr/bin/pkill", "-x", "uxplay"], check=False, timeout=1.5)
            except Exception:
                pass
        self.sensor_rot = self.read_sensor_rot()
        print("[intercom] Starting two-way video/audio call!", flush=True)
        self.wake_screen(hold=True)
        self.set_webkit_paused(True)
        ring_vol = None
        try:
            _rp = Path("/home/ubuntu/.local/share/thinksmart-client/alarms/ring.json")
            if _rp.exists() and not _rp.is_symlink():
                _rv = json.loads(_read_regular_file(_rp)).get("volume")
                if isinstance(_rv, (int, float)) and not isinstance(_rv, bool) and math.isfinite(_rv) and 0 <= _rv <= 100:
                    ring_vol = f"{int(round(_rv))}%"
        except Exception:
            pass
        ms_vol_fb = None
        self.saved_ambient_owned = prev_amb_owned if had_stale_reset else False
        try:
            _to = aiohttp.ClientTimeout(total=2.2)
            async with aiohttp.ClientSession(timeout=_to, trust_env=False) as sess:
                try:
                    ms_d, hs_d = await asyncio.gather(
                        self._get_json_safe(sess, "http://127.0.0.1:8091/api/music/status?lite=1", 1.5),
                        self._get_json_safe(sess, "http://127.0.0.1:8095/api/home/state", 1.5),
                    )
                    if isinstance(ms_d, dict):
                        rv = ms_d.get("volume")
                        if isinstance(rv, (int, float)) and not isinstance(rv, bool):
                            ms_vol_fb = f"{max(0, min(100, int(round(rv))))}%"
                        amb = ms_d.get("ambient")
                        if isinstance(amb, dict) and isinstance(amb.get("preset"), str) and amb["preset"]:
                            saved_a = {"preset": amb["preset"]}
                            if isinstance(amb.get("layer"), str) and amb["layer"]:
                                saved_a["layer"] = amb["layer"]
                            if isinstance(amb.get("layer_gain"), (int, float)) and not isinstance(amb.get("layer_gain"), bool):
                                saved_a["layer_gain"] = amb["layer_gain"]
                            self.saved_ambient = saved_a
                            if isinstance(hs_d, dict):
                                if "ambient_owned" in hs_d:
                                    self.saved_ambient_owned = bool(hs_d.get("ambient_owned"))
                                else:
                                    foc_ph = ((hs_d.get("study") or {}).get("focus") or {}).get("phase")
                                    tmr_ph = ((hs_d.get("timer") or {}) if isinstance(hs_d.get("timer"), dict) else {}).get("phase")
                                    self.saved_ambient_owned = bool(foc_ph == "focus" or tmr_ph in ("running", "paused"))
                        elif ms_d.get("playing") and not ms_d.get("paused"):
                            self.saved_ambient = None
                            self.saved_ambient_owned = False
                except Exception:
                    pass
                async def _post_quiet(u, **kw):
                    async with sess.post(u, **kw) as r:
                        await r.read()

                _pause_tasks = [
                    _post_quiet("http://127.0.0.1:8090/api/end"),
                    _post_quiet("http://127.0.0.1:8090/api/alarms/stop", json={}),
                    _post_quiet("http://127.0.0.1:8090/api/alarm-tone", json={"stop": True}),
                    _post_quiet("http://127.0.0.1:8091/api/music/control", json={"action": "pause", "source": "intercom"}),
                    _post_quiet("http://127.0.0.1:8095/api/home/messages/play", json={"stop": True}, headers={"Host": "127.0.0.1:8095"}),
                    _post_quiet("http://127.0.0.1:8095/api/home/voice_reply", json={"action": "cancel"}, headers={"Host": "127.0.0.1:8095"}),
                    asyncio.to_thread(self.pause_bluetooth_media),
                ]
                if os.path.lexists("/run/systemd/units/invocation:thinksmart-tutor.service"):
                    _pause_tasks.append(_post_quiet("http://127.0.0.1:8096/api/tutor/voice_record", json={"action": "cancel"}, headers={"Host": "127.0.0.1:8096"}))
                await asyncio.gather(*_pause_tasks, return_exceptions=True)
        except Exception:
            pass
        # Re-assert screen hold after XiaoZhi /api/end & /api/alarms/stop so Screen.release() cannot clear it
        self.wake_screen(hold=True)
        self.saved_spk_mute = await self.get_speaker_mute()
        self.saved_spk_vol = ring_vol or await self.get_speaker_volume() or ms_vol_fb or "40%"
        self._write_call_flag(json.dumps({"vol": self.saved_spk_vol, "mute": bool(self.saved_spk_mute)}) + "\n")
        if self.saved_spk_mute:
            await self.set_speaker_mute(False)
        await self.set_speaker_volume("85%")
        if not self.read_gpio_switches()[0]:
            await self.ensure_camera_running()
        await self.ensure_mic_running()

        await self._kill_stale(self.spk_proc)
        self.spk_proc = await asyncio.create_subprocess_exec(
            "/usr/bin/pacat",
            "--playback",
            "--device=alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=40",
            user="ubuntu",
            group="ubuntu",
            env=self.ubuntu_env(),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        await self._kill_stale(self.ui_proc)
        self.ui_proc = await asyncio.create_subprocess_exec(
            "/usr/bin/python3", "-B", "/usr/local/bin/thinksmart-intercom-ui.py",
            user="ubuntu",
            group="ubuntu",
            env=self.ubuntu_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if owner_ws is not None and owner_ws.closed:
            raise ConnectionResetError("caller disconnected during start_call")
        await self.sync_ui_orient()
        self.ui_task = asyncio.create_task(self.ui_watch_loop())
        await self.broadcast_status()
        return True

    async def ui_watch_loop(self):
        proc = self.ui_proc
        try:
            while proc and proc.stdout:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "ignore").strip()
                if "HANGUP" in text:
                    if proc is not self.ui_proc or not self.in_call:
                        break
                    print("[intercom] Device touchscreen tapped HANGUP!", flush=True)
                    prev_owner = self.call_owner
                    targets = [prev_owner] if prev_owner in self.clients else list(self.clients)
                    for ws in targets:
                        await self._send_json_fast(ws, {"type": "call_ended_by_device"}, max_buf=None, timeout=0.25)
                    if proc is self.ui_proc and self.in_call:
                        await self.stop_call(expected_ui_proc=proc)
                    break
        except Exception:
            pass
        if proc is self.ui_proc and self.in_call:
            print("[intercom] Call window exited unexpectedly; ending call", flush=True)
            prev_owner = self.call_owner
            targets = [prev_owner] if prev_owner in self.clients else list(self.clients)
            for ws in targets:
                await self._send_json_fast(ws, {"type": "call_ended_by_device"}, max_buf=None, timeout=0.25)
            if proc is self.ui_proc and self.in_call:
                await self.stop_call(expected_ui_proc=proc)

    async def stop_call(self, expected_owner=_ANY_CALL, expected_ui_proc=_ANY_CALL, fast: bool = False):
        async with self.call_lock:
            if expected_owner is not _ANY_CALL and self.call_owner is not expected_owner:
                return
            if expected_ui_proc is not _ANY_CALL and self.ui_proc is not expected_ui_proc:
                return
            await self._stop_call(fast=fast)

    async def _stop_call(self, fast: bool = False):
        if not self.in_call:
            self.call_owner = None
            return
        self.in_call = False
        self.call_owner = None
        print("[intercom] Stopping two-way call...", flush=True)

        async def _kill_fast(proc):
            if not proc:
                return
            try:
                if getattr(proc, "stdin", None):
                    proc.stdin.close()
            except Exception:
                pass
            if proc.returncode is not None:
                return
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=0.3)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass

        ui_p, spk_p, ui_t = self.ui_proc, self.spk_proc, self.ui_task
        self.ui_proc = None
        self.spk_proc = None
        self.ui_task = None
        saved_amb = self.saved_ambient
        saved_amb_owned = getattr(self, "saved_ambient_owned", False)
        self.saved_ambient = None
        self.saved_ambient_owned = False
        saved_mute = getattr(self, "saved_spk_mute", None)
        self.saved_spk_mute = None
        saved_vol = self.saved_spk_vol
        self.saved_spk_vol = None

        async def _teardown():
            if ui_t and ui_t is not asyncio.current_task():
                ui_t.cancel()
            await asyncio.gather(_kill_fast(ui_p), _kill_fast(spk_p), self.stop_mic_if_idle())
            if saved_vol:
                with contextlib.suppress(Exception):
                    await self.set_speaker_volume(saved_vol)
            if saved_mute:
                with contextlib.suppress(Exception):
                    await self.set_speaker_mute(True)
            try:
                INTERCOM_CALL_FLAG.unlink(missing_ok=True)
            except OSError:
                pass
            if saved_vol:
                m_v = re.match(r"^(\d+)%$", str(saved_vol))
                if m_v:
                    try:
                        v_int = max(0, min(100, int(m_v.group(1))))
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.4 if fast else 1.2), trust_env=False) as sess:
                            async with sess.post("http://127.0.0.1:8090/api/volume", json={"volume": v_int}) as r:
                                await r.read()
                    except Exception:
                        pass
            if not MIRROR_FLAG_PATH.exists():
                self.wake_screen(hold=False)
                self.set_webkit_paused(False)
            if not fast and saved_amb and not MIRROR_FLAG_PATH.exists() and not Path("/run/user/1000/uxplay-audio-active").exists():
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5), trust_env=False) as sess:
                        hs_d, ms_d = await asyncio.gather(
                            self._get_json_safe(sess, "http://127.0.0.1:8095/api/home/state", 1.5),
                            self._get_json_safe(sess, "http://127.0.0.1:8091/api/music/status?lite=1", 1.5),
                        )
                        if isinstance(ms_d, dict) and not ms_d.get("playing") and not ms_d.get("ambient"):
                            allow_restore = not saved_amb_owned
                            if saved_amb_owned and isinstance(hs_d, dict):
                                foc_ph = ((hs_d.get("study") or {}).get("focus") or {}).get("phase")
                                tmr_ph = ((hs_d.get("timer") or {}) if isinstance(hs_d.get("timer"), dict) else {}).get("phase")
                                allow_restore = bool(foc_ph == "focus" or tmr_ph in ("running", "paused"))
                            if allow_restore:
                                async with sess.post(
                                    "http://127.0.0.1:8095/api/home/music",
                                    json={"action": "ambient", "rebind_owner": bool(saved_amb_owned), **saved_amb},
                                ) as r_amb:
                                    await r_amb.read()
                except Exception:
                    pass
            if not fast:
                await self.broadcast_status()

        restore_t = asyncio.create_task(_teardown())
        cancelled = False
        while not restore_t.done():
            try:
                await asyncio.shield(restore_t)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError()

    async def status_loop(self):
        tick = 0
        while True:
            if not self.clients and not self.in_call and self.cam_proc is None and self.mic_proc is None:
                ev = getattr(self, "_active_ev", None)
                if ev is not None:
                    ev.clear()
                    if not self.clients and not self.in_call and self.cam_proc is None and self.mic_proc is None:
                        _trim_idle_heap()
                        await ev.wait()
                else:
                    await asyncio.sleep(1.5)
                continue
            await asyncio.sleep(0.5)
            tick += 1

            if self._pending_sensor_rot is not None or tick % 4 == 1:
                new_srot = self.read_sensor_rot()
                if new_srot != self.sensor_rot:
                    if new_srot == self._pending_sensor_rot:
                        self._pending_sensor_count += 1
                    else:
                        self._pending_sensor_rot = new_srot
                        self._pending_sensor_count = 1
                    if self._pending_sensor_count >= 2:
                        self.sensor_rot = new_srot
                        self._pending_sensor_rot = None
                        self._pending_sensor_count = 0
                        print(
                            f"[intercom] Gravity sensor orientation -> {self.sensor_rot}°",
                            flush=True,
                        )
                        await self.sync_ui_orient()
                        await self.broadcast_orient()
                else:
                    self._pending_sensor_rot = None
                    self._pending_sensor_count = 0

            if tick % 4 == 0:
                now_m = time.monotonic()
                shutter_closed, _ = self.read_gpio_switches()
                if self._has_video_demand(shutter_closed=shutter_closed) and self.cam_proc is None and now_m >= getattr(self, "_cam_backoff_until", 0.0):
                    if getattr(self, "_cam_backoff_until", 0.0) > 0.0:
                        self._cam_backoff_until = 0.0
                        self.cam_restarts = self.cam_restarts[-5:]
                    self.cam_restarts = [t for t in self.cam_restarts if now_m - t < 60]
                    if len(self.cam_restarts) <= 5:
                        await self.ensure_camera_running()
                elif not self._has_video_demand(shutter_closed=shutter_closed) and self.cam_proc is not None:
                    await self.stop_camera_if_idle(shutter_closed=shutter_closed)
                if (self.listeners or self.in_call) and self.mic_proc is None and now_m >= getattr(self, "_mic_backoff_until", 0.0):
                    if getattr(self, "_mic_backoff_until", 0.0) > 0.0:
                        self._mic_backoff_until = 0.0
                        self.mic_restarts = self.mic_restarts[-5:]
                    self.mic_restarts = [t for t in self.mic_restarts if now_m - t < 60]
                    if len(self.mic_restarts) <= 5:
                        await self.ensure_mic_running()
                if self.clients:
                    await self.broadcast_status()

    async def handle_index(self, request):
        headers = {
            "Cache-Control": "no-cache",
            "ETag": HTML_PAGE_ETAG,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",   # 2026-11-20: 管理后台（同一网址）要嵌入本页
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "media-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'self'"
            ),
        }
        if request.headers.get("If-None-Match") == HTML_PAGE_ETAG:
            return web.Response(status=304, headers=headers)
        return web.Response(
            body=HTML_PAGE_BYTES,
            content_type="text/html",
            charset="utf-8",
            headers=headers,
        )

    async def handle_ws(self, request):
        hdr_l = {k.lower(): v for k, v in request.headers.items()}
        if not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
            raise web.HTTPForbidden()
        if len(self.clients) >= 8 or len(self.all_ws) >= 12:
            raise web.HTTPTooManyRequests(text="连接数已达上限")
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, heartbeat=20.0, timeout=2.0)
        self.all_ws.add(ws)
        try:
            await ws.prepare(request)
        except BaseException:
            self.all_ws.discard(ws)
            raise

        try:
            if not self.clients and not self.in_call:
                self.sensor_rot = self.read_sensor_rot()
                self._pending_sensor_rot = None
                self._pending_sensor_count = 0
            shutter_closed, hw_mic_muted = self.read_gpio_switches()
            init_msg = self.orient_dict("status")
            init_msg["shutter_closed"] = shutter_closed
            init_msg["hw_mic_muted"] = hw_mic_muted
            init_msg["in_call"] = self.in_call
            init_msg["is_call_owner"] = (ws is self.call_owner)
            await ws.send_json(init_msg)
            if request.query.get("video") == "0":
                self.video_paused.add(ws)
            self.clients.add(ws)
            self._wake_status_loop()
            if not shutter_closed and ws not in self.video_paused:
                await self.ensure_camera_running()
            last_start_mono = 0.0

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    mtype = data.get("type")
                    if mtype == "listen":
                        if data.get("enable"):
                            self.listeners.add(ws)
                            await self.ensure_mic_running()
                        else:
                            self.listeners.discard(ws)
                            await self.stop_mic_if_idle()
                    elif mtype == "start_call":
                        now_m = time.monotonic()
                        rem_cd = 1.0 - (now_m - last_start_mono)
                        if not self.in_call and rem_cd > 0:
                            await asyncio.sleep(min(1.0, rem_cd))
                            if ws.closed:
                                break
                        last_start_mono = time.monotonic()
                        try:
                            ok = await self.start_call(owner_ws=ws)
                        except ConnectionResetError:
                            break
                        except Exception as exc:
                            print(f"[intercom] start_call failed: {exc}", flush=True)
                            await self._send_json_fast(ws, {"type": "call_ended_by_device"})
                            continue
                        if not ok:
                            await self._send_json_fast(ws, {"type": "call_busy"})
                    elif mtype == "stop_call":
                        if ws is self.call_owner or self.call_owner is None:
                            await self.stop_call(expected_owner=ws if ws is self.call_owner else None)
                    elif mtype == "cam_off":
                        if self.in_call and ws is self.call_owner:
                            await self.send_to_ui(4, b"")
                    elif mtype == "saver":
                        self.frame_div[ws] = 2 if data.get("enable") else 1
                    elif mtype == "video":
                        if data.get("enable"):
                            self.video_paused.discard(ws)
                            if not self.read_gpio_switches()[0]:
                                await self.ensure_camera_running()
                        else:
                            self.video_paused.add(ws)
                            await self.stop_camera_if_idle()
                elif msg.type == WSMsgType.BINARY:
                    if not self.in_call or ws is not self.call_owner:
                        continue
                    b = msg.data
                    if len(b) < 2:
                        continue
                    tag = b[0]
                    if tag == 1 and self.ui_proc and self.ui_proc.returncode is None:
                        if len(b) <= 1_572_865:
                            await self.send_to_ui(1, memoryview(b)[1:], drop_if_busy=True)
                    elif tag == 2 and len(b) <= 65537:
                        if (self.spk_proc is None or self.spk_proc.returncode is not None) and (time.monotonic() - self._last_spk_respawn) >= 1.5:
                            self._last_spk_respawn = time.monotonic()
                            if self.spk_proc is not None and self.spk_proc.stdin:
                                try:
                                    self.spk_proc.stdin.close()
                                except Exception:
                                    pass
                            proc = None
                            try:
                                proc = await asyncio.create_subprocess_exec(
                                    "/usr/bin/pacat",
                                    "--playback",
                                    "--device=alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink",
                                    "--format=s16le",
                                    "--rate=16000",
                                    "--channels=1",
                                    "--latency-msec=40",
                                    user="ubuntu",
                                    group="ubuntu",
                                    env=self.ubuntu_env(),
                                    stdin=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.DEVNULL,
                                )
                                if not self.in_call or ws is not self.call_owner or (self.spk_proc is not None and self.spk_proc.returncode is None):
                                    if proc.stdin:
                                        proc.stdin.close()
                                    await self._kill_stale(proc)
                                else:
                                    self.spk_proc = proc
                                    proc = None
                            except BaseException as exc:
                                if proc is not None:
                                    try:
                                        if proc.stdin:
                                            proc.stdin.close()
                                        proc.kill()
                                        await proc.wait()
                                    except Exception:
                                        pass
                                if isinstance(exc, asyncio.CancelledError):
                                    raise
                        if self.spk_proc and self.spk_proc.returncode is None:
                            try:
                                n = (len(b) - 1) >> 1
                                if n > 0 and self.spk_proc.stdin:
                                    tr = getattr(self.spk_proc.stdin, "transport", None)
                                    if tr is not None and tr.get_write_buffer_size() > 32768:
                                        continue
                                    self.spk_proc.stdin.write(_scale_pcm_s16le(memoryview(b)[1:1 + n * 2], 2.5))
                                    await asyncio.wait_for(self.spk_proc.stdin.drain(), timeout=0.5)
                            except asyncio.TimeoutError:
                                stale_spk = self.spk_proc
                                self.spk_proc = None
                                await self._kill_stale(stale_spk)
                            except Exception:
                                pass
        finally:
            self.all_ws.discard(ws)
            self.clients.discard(ws)
            self.listeners.discard(ws)
            self.frame_div.pop(ws, None)
            self.video_paused.discard(ws)
            if ws is self.call_owner:
                await self.stop_call(expected_owner=ws)
            await self.stop_mic_if_idle()
            await self.stop_camera_if_idle()

        return ws


# Study companion parent page (thinksmart-study, home LAN :8094) is also offered remotely under
# /study/ here, so it inherits this entry's VPN-only access (us-optimized nginx 18443 allow list,
# reverse SSH 127.0.0.1:18091) without another tunnel port. Only the page and its four parent
# endpoints are passed on; requests reach the study service from 127.0.0.1, so its loopback-only
# kiosk API (/api/study/*) must never be reachable through this path.
STUDY_PARENT = "http://127.0.0.1:8094"
STUDY_PATHS = re.compile(r"^(|favicon\.svg|apple-touch-icon\.png|api/parent/(state|day|days|toggle))$")
STUDY_FWD_REQ = ("content-type", "origin", "x-study-parent", "host", "if-none-match")
STUDY_FWD_RESP = (
    "Content-Type",
    "Cache-Control",
    "ETag",
    "Referrer-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Content-Security-Policy",
)


# Bookmark / home-screen icons for this page (blue camera), kept as files so they can be redrawn
ICON_DIR = Path("/usr/local/share/thinksmart-intercom")
ICONS = {"favicon.svg": "image/svg+xml", "apple-touch-icon.png": "image/png"}
_ICON_CACHE = {}


async def handle_icon(request):
    name = request.match_info["name"]
    path = ICON_DIR / name
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 262144:
            raise OSError("Invalid icon")
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        cached = _ICON_CACHE.get(name)
        if cached and cached[0] == sig:
            data = cached[1]
        else:
            sig, data = _read_regular_bytes(path, 262144)
            _ICON_CACHE[name] = (sig, data)
    except OSError:
        raise web.HTTPNotFound()
    return web.Response(
        body=data,
        content_type=ICONS[name],
        headers={"Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


async def handle_study_redirect(request):
    qs = f"?{request.query_string}" if request.query_string else ""
    raise web.HTTPPermanentRedirect(f"/study/{qs}")


def _via_local_tunnel(peer) -> bool:
    return peer is None or str(peer).startswith("127.") or str(peer) in ("::1", "::ffff:127.0.0.1")


async def handle_study(request):
    tail = request.match_info.get("tail", "")
    if not STUDY_PATHS.fullmatch(tail) or request.method not in ("GET", "POST"):
        raise web.HTTPNotFound()
    body = None
    if request.method == "POST":
        chunks = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            chunks.extend(chunk)
            if len(chunks) > 4096:
                raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(chunks))
        body = bytes(chunks)
    import aiohttp as _ah, contextlib as _cl, ipaddress as _ip, urllib.parse as _up
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    sfs = (hdr_l.get("sec-fetch-site") or "").strip().lower()
    if sfs and sfs not in ("same-origin", "none"):
        raise web.HTTPForbidden()
    if request.method == "POST" and hdr_l.get("x-study-parent") != "1":
        raise web.HTTPForbidden()

    def _local_hn(hn):
        if not hn or ".." in hn:
            return False
        try:
            pip = _ip.ip_address(hn.split("%", 1)[0])
            if getattr(pip, "ipv4_mapped", None) is not None:
                pip = pip.ipv4_mapped
            return not (pip.is_global or pip.is_multicast or pip.is_unspecified)
        except ValueError:
            if "%" in hn:
                return False
            return hn == "localhost" or "." not in hn or hn.endswith((".local", ".lan", ".home", ".internal", ".home.arpa"))

    _rg = getattr(request, "get", None)
    allow_wan_dir = bool(_rg("ts_allow_wan_dir", False) if callable(_rg) else getattr(request, "ts_allow_wan_dir", False))
    _peer = getattr(request, "remote", None)
    _via_fn = globals().get("_via_local_tunnel")
    via_tunnel = _via_fn(_peer) if _via_fn else (_peer is None or str(_peer).startswith("127.") or str(_peer) in ("::1", "::ffff:127.0.0.1"))
    xfp = (hdr_l.get("x-forwarded-proto") or "").split(",")[0].strip().lower() if via_tunnel else ""
    raw_host = (hdr_l.get("host") or "").strip()
    rh_hn = None
    if raw_host:
        if any(c in raw_host for c in ("/", "\\", "?", "#", "@", " ", "\t", "\r", "\n")):
            raise web.HTTPForbidden()
        try:
            rh_sp = _up.urlsplit(f"//{raw_host}")
            rh_port = rh_sp.port
            rh_hn = (rh_sp.hostname or "").lower().rstrip(".")
        except ValueError:
            raise web.HTTPForbidden()
        if not rh_hn or rh_sp.username is not None or rh_sp.password is not None or rh_sp.path or rh_sp.query or rh_sp.fragment:
            raise web.HTTPForbidden()
        if not allow_wan_dir and (rh_port == 18091 or xfp == "http") and not _local_hn(rh_hn):
            raise web.HTTPForbidden()
    xfh = (hdr_l.get("x-forwarded-host") or "").split(",")[0].strip() if (via_tunnel and rh_hn in ("127.0.0.1", "localhost", "::1")) else ""
    in_host = xfh or raw_host
    in_hn = None
    if in_host:
        if any(c in in_host for c in ("/", "\\", "?", "#", "@", " ", "\t", "\r", "\n")):
            raise web.HTTPForbidden()
        try:
            in_sp = _up.urlsplit(f"//{in_host}")
            in_port = in_sp.port
            in_hn = (in_sp.hostname or "").lower().rstrip(".")
        except ValueError:
            raise web.HTTPForbidden()
        if not in_hn or in_sp.username is not None or in_sp.password is not None or in_sp.path or in_sp.query or in_sp.fragment:
            raise web.HTTPForbidden()
        if not allow_wan_dir and (in_port == 18091 or xfp == "http") and not _local_hn(in_hn):
            raise web.HTTPForbidden()
    orig = hdr_l.get("origin")
    if orig is not None:
        try:
            po = _up.urlsplit(orig)
            po_port = po.port
        except ValueError:
            raise web.HTTPForbidden()
        if po.scheme not in ("http", "https") or not po.hostname or po.path or po.query or po.fragment or po.username is not None:
            raise web.HTTPForbidden()
        po_hn = po.hostname.lower().rstrip(".")
        if not allow_wan_dir and po.scheme == "http" and not _local_hn(po_hn):
            raise web.HTTPForbidden()
        if in_hn and in_hn not in ("127.0.0.1", "localhost", "::1"):
            if po_hn != in_hn:
                raise web.HTTPForbidden()
            if xfp in ("http", "https") and po.scheme != xfp:
                raise web.HTTPForbidden()
            def_p = 443 if po.scheme == "https" else 80
            if (in_port is not None or not _local_hn(po_hn)) and (po_port or def_p) != (in_port or def_p):
                raise web.HTTPForbidden()
        elif po_hn not in ("127.0.0.1", "localhost", "::1") and not ((via_tunnel or allow_wan_dir) and po.scheme == "https" and po_port in (None, 443, 18443) and sfs == "same-origin"):
            raise web.HTTPForbidden()
    up = _up.urlsplit(STUDY_PARENT)
    headers = {k: v for k, v in request.headers.items() if k.lower() in STUDY_FWD_REQ and k.lower() not in ("host", "origin")}
    if up.netloc:
        headers["Host"] = up.netloc
        if orig is not None:
            headers["Origin"] = f"{up.scheme}://{up.netloc}"
    try:
        _app = getattr(request, "app", None)
        _sk = globals().get("STUDY_SESSION_KEY")
        sess = (_app[_sk] if (_sk is not None and _sk in _app) else _app.get("study_session")) if _app is not None else None
        cm = _cl.nullcontext(sess) if sess else _ah.ClientSession(timeout=_ah.ClientTimeout(total=15), trust_env=False)
        async with cm as s, s.request(request.method, f"{STUDY_PARENT}/{tail}", params=request.query,
                                      headers=headers, data=body, allow_redirects=False) as r:
            resp_chunks = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                resp_chunks.extend(chunk)
                if len(resp_chunks) > 1048576:
                    return web.Response(status=502, text="学习陪伴响应过大", content_type="text/plain")
            out = {k: r.headers[k] for k in STUDY_FWD_RESP if k in r.headers}
            _fr = globals().get("_admin_frameable")    # 2026-11-20: 管理后台同源嵌入（老测试只抽出本函数时没有这个助手）
            if _fr:
                out = _fr(out)
            return web.Response(status=r.status, body=bytes(resp_chunks), headers=out)
    except (_ah.ClientError, asyncio.TimeoutError):
        return web.Response(status=502, text="学习陪伴服务暂时不可用", content_type="text/plain")


# ---- Round 109: message board. The phone page posts here; we relay to thinksmart-home on loopback
# (its add-message route only accepts X-TS-Internal without an Origin, i.e. only this relay).
HOME_API = "http://127.0.0.1:8095"
CLIENT_API = "http://127.0.0.1:8090"


def _req_allow_wan_dir(request) -> bool:
    getter = getattr(request, "get", None)
    if callable(getter):
        return bool(getter("ts_allow_wan_dir", False))
    return bool(getattr(request, "ts_allow_wan_dir", False))


def _msg_origin_ok(hdr_l, peer=None, allow_wan_dir: bool = False) -> bool:
    import ipaddress as _ip, urllib.parse as _up
    sfs = (hdr_l.get("sec-fetch-site") or "").strip().lower()
    if sfs and sfs not in ("same-origin", "none"):
        return False

    def _local_hn(hn):
        if not hn or ".." in hn:
            return False
        try:
            pip = _ip.ip_address(hn.split("%", 1)[0])
            if getattr(pip, "ipv4_mapped", None) is not None:
                pip = pip.ipv4_mapped
            return not (pip.is_global or pip.is_multicast or pip.is_unspecified)
        except ValueError:
            if "%" in hn:
                return False
            return hn == "localhost" or "." not in hn or hn.endswith((".local", ".lan", ".home", ".internal", ".home.arpa"))

    via_tunnel = _via_local_tunnel(peer)
    xfp = (hdr_l.get("x-forwarded-proto") or "").split(",")[0].strip().lower() if via_tunnel else ""
    raw_host = (hdr_l.get("host") or "").strip()
    rh_hn = None
    if raw_host:
        if any(c in raw_host for c in ("/", "\\", "?", "#", "@", " ", "\t", "\r", "\n")):
            return False
        try:
            rh_sp = _up.urlsplit(f"//{raw_host}")
            rh_port = rh_sp.port
            rh_hn = (rh_sp.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if not rh_hn or rh_sp.username is not None or rh_sp.password is not None or rh_sp.path or rh_sp.query or rh_sp.fragment:
            return False
        if not allow_wan_dir and (rh_port == 18091 or xfp == "http") and not _local_hn(rh_hn):
            return False
    xfh = (hdr_l.get("x-forwarded-host") or "").split(",")[0].strip() if (via_tunnel and rh_hn in ("127.0.0.1", "localhost", "::1")) else ""
    in_host = xfh or raw_host
    in_hn = None
    if in_host:
        if any(c in in_host for c in ("/", "\\", "?", "#", "@", " ", "\t", "\r", "\n")):
            return False
        try:
            in_sp = _up.urlsplit(f"//{in_host}")
            in_port = in_sp.port
            in_hn = (in_sp.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if not in_hn or in_sp.username is not None or in_sp.password is not None or in_sp.path or in_sp.query or in_sp.fragment:
            return False
        if not allow_wan_dir and (in_port == 18091 or xfp == "http") and not _local_hn(in_hn):
            return False
    orig = hdr_l.get("origin")
    if orig is None:
        return True
    try:
        po = _up.urlsplit(orig)
        po_port = po.port
    except ValueError:
        return False
    if po.scheme not in ("http", "https") or not po.hostname or po.path or po.query or po.fragment or po.username is not None:
        return False
    po_hn = po.hostname.lower().rstrip(".")
    if not allow_wan_dir and po.scheme == "http" and not _local_hn(po_hn):
        return False
    if allow_wan_dir and in_hn and in_hn not in ("127.0.0.1", "localhost", "::1"):
        def_p = 443 if po.scheme == "https" else 80
        return po_hn == in_hn and (po_port or def_p) == (in_port or def_p)
    if in_hn and in_hn not in ("127.0.0.1", "localhost", "::1"):
        return po_hn == in_hn
    return ((peer is None or via_tunnel) and po_hn in ("127.0.0.1", "localhost", "::1")) or (via_tunnel and po.scheme == "https" and po_port in (None, 443, 18443) and sfs == "same-origin")


_MSG_RELAY_CACHE = {"up_etag": "", "etag": "", "body": b""}
_MSG_RELAY_EPOCH = 0


async def _read_stream_bounded(stream, max_bytes: int) -> bytes:
    if hasattr(stream, "iter_chunked") and not type(stream).__module__.startswith("unittest.mock"):
        buf = bytearray()
        async for chunk in stream.iter_chunked(min(65536, max_bytes + 1)):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                break
        return bytes(buf)
    return await stream.read(max_bytes + 1)


async def keepalive_304(_req, resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if resp.status == 304:
        resp.headers["Content-Length"] = "0"


async def handle_messages(request):
    global _MSG_RELAY_EPOCH
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    body = None
    if request.method == "POST":
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        chunks = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            chunks.extend(chunk)
            if len(chunks) > 4096:
                raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(chunks))
        try:
            data = json.loads(bytes(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest()
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise web.HTTPBadRequest()
        body = {"text": data["text"][:400]}
        if isinstance(data.get("who"), str) and 0 < len(data["who"]) <= 8:   # 2026-11-12: 署名, reminders
            body["who"] = data["who"]
        if data.get("due") is not None:
            if isinstance(data["due"], bool) or not isinstance(data["due"], (int, float)) or not math.isfinite(data["due"]):
                raise web.HTTPBadRequest()
            body["due"] = data["due"]
            if isinstance(data.get("repeat"), str) and 0 < len(data["repeat"]) <= 12:   # 每天 / 工作日 / 每周
                body["repeat"] = data["repeat"]
            if isinstance(data.get("days"), list) and len(data["days"]) <= 7 and all(type(x) is int for x in data["days"]):
                body["days"] = data["days"]
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    try:
        if body is None:
            my_ep = _MSG_RELAY_EPOCH
            up_hdr = {"X-TS-Internal": "1"}
            if _MSG_RELAY_CACHE["up_etag"]:
                up_hdr["If-None-Match"] = _MSG_RELAY_CACHE["up_etag"]
            cm = sess.get(f"{HOME_API}/api/home/messages", params={"limit": "20"}, headers=up_hdr)
        else:
            _MSG_RELAY_EPOCH += 1
            _MSG_RELAY_CACHE.update(up_etag="", etag="", body=b"")
            cm = sess.post(f"{HOME_API}/api/home/messages", json=body, headers={"X-TS-Internal": "1"})
        async with cm as r:
            if body is None and r.status == 304:
                if my_ep == _MSG_RELAY_EPOCH and _MSG_RELAY_CACHE["etag"]:
                    etag, raw_out = _MSG_RELAY_CACHE["etag"], _MSG_RELAY_CACHE["body"]
                    if hdr_l.get("if-none-match") == etag:
                        return web.Response(status=304, headers={"Cache-Control": "no-store", "ETag": etag})
                    return web.Response(body=raw_out, status=200, content_type="application/json", charset="utf-8",
                                        headers={"Cache-Control": "no-store", "ETag": etag})
                my_ep = _MSG_RELAY_EPOCH
                async with sess.get(f"{HOME_API}/api/home/messages", params={"limit": "20"}, headers={"X-TS-Internal": "1"}) as r2:
                    raw = await _read_stream_bounded(r2.content, 65536)
                    r_status = r2.status
                    r_up_et = r2.headers.get("ETag") or ""
            else:
                raw = await _read_stream_bounded(r.content, 65536)
                r_status = r.status
                r_up_et = r.headers.get("ETag") or ""
            if len(raw) > 65536:
                return web.json_response({"ok": False, "error": "响应过大"}, status=502)
            decode_ok = True
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                payload = {}
                decode_ok = False
            if not isinstance(payload, dict):
                payload = {}
                decode_ok = False
            def _pub_msg(m):
                d = {k: m.get(k) for k in ("id", "text", "at", "read_at")}
                if m.get("source") == "screen":
                    d["source"] = "screen"
                if m.get("kind") == "voice":
                    d["kind"] = "voice"
                    d["duration"] = m.get("duration") or 0
                for k in ("who", "transcript", "tag"):   # 2026-11-12
                    if isinstance(m.get(k), str) and m.get(k):
                        d[k] = m[k][:400]
                if isinstance(m.get("due"), (int, float)) and not isinstance(m.get("due"), bool) and math.isfinite(m["due"]) and m["due"]:
                    d["due"] = m["due"]
                    d["fired"] = bool(m.get("fired"))
                    if m.get("repeat") in ("daily", "workdays", "weekly"):
                        d["repeat"] = m["repeat"]
                        if isinstance(m.get("days"), list):
                            d["days"] = [x for x in m["days"] if type(x) is int and 1 <= x <= 7][:7]
                if m.get("no_speech") is True:
                    d["no_speech"] = True
                return d

            if body is None:
                out = {"messages": [_pub_msg(m) for m in (payload.get("messages") or []) if isinstance(m, dict)]}
                for k in ("quick", "family"):
                    if isinstance(payload.get(k), list):
                        out[k] = [x for x in payload[k] if isinstance(x, str)][:12]
                if r_status == 200 and decode_ok:
                    raw_out = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    etag = f'"{hashlib.blake2b(raw_out, digest_size=8).hexdigest()}"'
                    up_et = r_up_et
                    if up_et and my_ep == _MSG_RELAY_EPOCH:
                        _MSG_RELAY_CACHE.update(up_etag=up_et, etag=etag, body=raw_out)
                    if hdr_l.get("if-none-match") == etag:
                        return web.Response(status=304, headers={"Cache-Control": "no-store", "ETag": etag})
                    return web.Response(body=raw_out, status=200, content_type="application/json", charset="utf-8",
                                        headers={"Cache-Control": "no-store", "ETag": etag})
            else:
                m = payload.get("message")
                out = {"ok": bool(payload.get("ok")), "error": payload.get("error")}
                if isinstance(m, dict):
                    out["message"] = _pub_msg(m)
            resp = web.json_response(out, status=r_status if r_status in (200, 400, 429) else 502)
            resp.headers["Cache-Control"] = "no-store"
            return resp
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "家里的屏幕暂时连不上"}, status=502)


async def handle_message_voice(request):
    global _MSG_RELAY_EPOCH
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    if request.content_type not in ("audio/wav", "audio/x-wav", "application/octet-stream"):
        raise web.HTTPUnsupportedMediaType()
    chunks = bytearray()
    async for chunk in request.content.iter_chunked(16384):
        chunks.extend(chunk)
        if len(chunks) > 2 * 1024 * 1024:   # 2026-11-12: up to 60 s
            raise web.HTTPRequestEntityTooLarge(max_size=2 * 1024 * 1024, actual_size=len(chunks))
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    _MSG_RELAY_EPOCH += 1
    _MSG_RELAY_CACHE.update(up_etag="", etag="", body=b"")
    try:
        who = request.query.get("who") or ""
        async with sess.post(
            f"{HOME_API}/api/home/messages/voice",
            params={"who": who} if 0 < len(who) <= 8 else None,
            data=io.BytesIO(chunks),
            headers={"Content-Type": "audio/wav", "X-TS-Internal": "1"},
            timeout=aiohttp.ClientTimeout(total=28),
        ) as r:
            raw = await _read_stream_bounded(r.content, 16384)
            if len(raw) > 16384:
                return web.json_response({"ok": False, "error": "响应过大"}, status=502)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            m = payload.get("message") if isinstance(payload, dict) else None
            out = {"ok": bool(payload.get("ok")) if isinstance(payload, dict) else False,
                   "error": payload.get("error") if isinstance(payload, dict) else None}
            if isinstance(m, dict):
                out["message"] = {k: m.get(k) for k in ("id", "text", "at", "read_at", "source", "kind", "duration", "who")}
            return web.json_response(out, status=r.status if r.status in (200, 400, 429) else 502,
                                     headers={"Cache-Control": "no-store"})
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return web.json_response({"ok": False, "error": "家里的屏幕暂时连不上"}, status=502)


async def handle_message_audio(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    msg_id = (request.query.get("id") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{12}", msg_id):
        raise web.HTTPBadRequest()
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    up_hdr = {"X-TS-Internal": "1"}
    if hdr_l.get("if-none-match"):
        up_hdr["If-None-Match"] = hdr_l["if-none-match"]
    try:
        async with sess.get(f"{HOME_API}/api/home/messages/audio/{msg_id}", headers=up_hdr) as r:
            if r.status == 304:
                return web.Response(status=304, headers={"ETag": r.headers.get("ETag", ""), "Cache-Control": "private, max-age=3600"})
            if r.status != 200:
                raise web.HTTPNotFound()
            raw = await _read_stream_bounded(r.content, 400 * 1024)
            if len(raw) > 400 * 1024:
                raise web.HTTPBadGateway()
            return web.Response(
                body=raw,
                content_type="audio/mpeg",
                headers={"ETag": r.headers.get("ETag", ""), "Cache-Control": "private, max-age=3600"},
            )
    except web.HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise web.HTTPBadGateway()


async def handle_message_ack(request):
    global _MSG_RELAY_EPOCH
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    raw = await _read_stream_bounded(request.content, 1024)
    if len(raw) > 1024:
        raise web.HTTPRequestEntityTooLarge(max_size=1024, actual_size=len(raw))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest()
    msg_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(msg_id, str) or not re.fullmatch(r"[0-9a-f]{12}", msg_id):
        raise web.HTTPBadRequest()
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    _MSG_RELAY_EPOCH += 1
    _MSG_RELAY_CACHE.update(up_etag="", etag="", body=b"")
    try:
        async with sess.post(f"{HOME_API}/api/home/messages/ack_phone", json={"id": msg_id}, headers={"X-TS-Internal": "1"}) as r:
            await _read_stream_bounded(r.content, 4096)
            return web.json_response({"ok": r.status == 200}, status=200 if r.status == 200 else 404, headers={"Cache-Control": "no-store"})
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False}, status=502)


async def _relay_small_post(request, home_path, limit=4096):
    """2026-11-12: quick replies and reminder cancel, same checks as the other message routes."""
    global _MSG_RELAY_EPOCH
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    raw = await _read_stream_bounded(request.content, limit)
    if len(raw) > limit:
        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(raw))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest()
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    _MSG_RELAY_EPOCH += 1
    _MSG_RELAY_CACHE.update(up_etag="", etag="", body=b"")
    try:
        async with sess.post(f"{HOME_API}{home_path}", json=data, headers={"X-TS-Internal": "1"}) as r:
            raw_up = await _read_stream_bounded(r.content, 16384)
            try:
                payload = json.loads(raw_up.decode("utf-8")) if raw_up and len(raw_up) <= 16384 else {}
            except (ValueError, UnicodeDecodeError):
                payload = {}
            return web.json_response(payload if isinstance(payload, dict) else {"ok": False},
                                     status=r.status if r.status in (200, 400, 404) else 502, headers={"Cache-Control": "no-store"})
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "家里的屏幕暂时连不上"}, status=502)


async def handle_message_quick(request):
    return await _relay_small_post(request, "/api/home/messages/quick")


async def handle_message_cancel(request):
    return await _relay_small_post(request, "/api/home/messages/cancel", 1024)


def _client_loopback_headers(with_origin: bool = False) -> dict:
    host_netloc = urllib.parse.urlsplit(CLIENT_API).netloc or "127.0.0.1:8090"
    hdrs = {"Host": host_netloc}
    if with_origin:
        hdrs["Origin"] = CLIENT_API.rstrip("/")
    return hdrs


async def handle_alarms_get(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    up_hdr = _client_loopback_headers()
    if hdr_l.get("if-none-match"):
        up_hdr["If-None-Match"] = hdr_l["if-none-match"]
    try:
        async with sess.get(f"{CLIENT_API.rstrip('/')}/api/alarms", headers=up_hdr) as r:
            if r.status == 304:
                return web.Response(status=304, headers={"ETag": r.headers.get("ETag", ""), "Cache-Control": "no-store"})
            raw = await _read_stream_bounded(r.content, 65536)
            if r.status != 200 or len(raw) > 65536:
                return web.json_response({"ok": False, "error": "读取闹钟失败"}, status=502)
            hdrs_out = {"Cache-Control": "no-store"}
            if r.headers.get("ETag"):
                hdrs_out["ETag"] = r.headers["ETag"]
            return web.Response(body=raw, content_type="application/json", charset="utf-8", headers=hdrs_out)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "家里的屏幕暂时连不上"}, status=502)


async def handle_alarms_post(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    raw = await _read_stream_bounded(request.content, 4096)
    if len(raw) > 4096:
        raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(raw))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest()

    act = str(data.get("action") or "").strip()
    if act == "create":
        up_path = "/api/alarms/create"
        up_body = {
            k: data[k]
            for k in ("kind", "label", "say", "repeat", "weekdays", "month_day", "time", "date", "days_from_today", "hours", "minutes", "seconds")
            if k in data and data[k] is not None
        }
    elif act == "delete":
        up_path = "/api/alarms/delete"
        up_body = {"id": data.get("id")}
    elif act == "snooze":
        up_path = "/api/alarms/snooze"
        up_body = {}
    elif act == "stop":
        up_path = "/api/alarms/stop"
        up_body = {}
    elif act == "clear_missed":
        up_path = "/api/alarms/missed/clear"
        up_body = {}
    elif act == "volume":
        up_path = "/api/alarm-volume"
        up_body = {"volume": data.get("volume")}
    elif act == "tone":
        up_path = "/api/alarm-tone"
        up_body = {"tone": data.get("tone")}
    else:
        return web.json_response({"ok": False, "error": "不支持的闹钟操作"}, status=400)

    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    try:
        async with sess.post(
            f"{CLIENT_API.rstrip('/')}{up_path}",
            json=up_body,
            headers=_client_loopback_headers(with_origin=True),
        ) as r:
            raw_up = await _read_stream_bounded(r.content, 16384)
            try:
                payload = json.loads(raw_up.decode("utf-8")) if raw_up and len(raw_up) <= 16384 else {}
            except (ValueError, UnicodeDecodeError):
                payload = {}
            return web.json_response(
                payload if isinstance(payload, dict) else {"ok": False},
                status=r.status if r.status in (200, 400, 404) else 502,
                headers={"Cache-Control": "no-store"},
            )
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "家里的屏幕暂时连不上"}, status=502)

async def handle_countdowns_get(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    up_hdr = {"X-TS-Internal": "1"}
    if hdr_l.get("if-none-match"):
        up_hdr["If-None-Match"] = hdr_l["if-none-match"]
    try:
        async with sess.get(f"{HOME_API}/api/home/countdowns", headers=up_hdr) as r:
            if r.status == 304:
                return web.Response(status=304, headers={"ETag": r.headers.get("ETag", ""), "Cache-Control": "no-store"})
            raw = await _read_stream_bounded(r.content, 131072)
            if r.status != 200 or len(raw) > 131072:
                return web.json_response({"ok": False, "error": "读取倒数日失败"}, status=502)
            hdrs_out = {"Cache-Control": "no-store"}
            if r.headers.get("ETag"):
                hdrs_out["ETag"] = r.headers["ETag"]
            return web.Response(body=raw, content_type="application/json", charset="utf-8", headers=hdrs_out)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "家里的屏幕暂时连不上"}, status=502)


async def handle_countdown_convert(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    qs = request.query_string
    try:
        async with sess.get(f"{HOME_API}/api/home/countdowns/convert?{qs}", headers={"X-TS-Internal": "1"}) as r:
            raw = await _read_stream_bounded(r.content, 16384)
            if len(raw) > 16384:
                return web.json_response({"ok": False, "error": "换算服务不可用"}, status=502)
            return web.Response(status=r.status, body=raw, content_type="application/json", charset="utf-8", headers={"Cache-Control": "no-store"})
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "换算服务不可用"}, status=502)


async def handle_countdown_save(request):
    return await _relay_small_post(request, "/api/home/countdowns", 4096)


async def handle_countdown_pin(request):
    return await _relay_small_post(request, "/api/home/countdowns/pin", 1024)


async def handle_countdown_delete(request):
    return await _relay_small_post(request, "/api/home/countdowns/delete", 1024)


TUTOR_API = "http://127.0.0.1:8096"
TUTOR_MOBILE_GET_MAP = {
    "": "/api/tutor/mobile_asset/mobile.html",
    "manifest.json": "/api/tutor/mobile_asset/manifest.json",
    "favicon.svg": "/api/tutor/mobile_asset/favicon.svg",
    "apple-touch-icon.png": "/api/tutor/mobile_asset/apple-touch-icon.png",
    "api/mobile/state": "/api/tutor/mobile/state",
    "api/mobile/page.jpg": "/api/tutor/mobile/page.jpg",
}


async def handle_tutor_redirect(request):
    qs = f"?{request.query_string}" if request.query_string else ""
    raise web.HTTPPermanentRedirect(f"/tutor/{qs}")


async def handle_tutor_mobile(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    tail = request.match_info.get("tail", "")
    if request.method == "GET" and tail in TUTOR_MOBILE_GET_MAP:
        up_path = TUTOR_MOBILE_GET_MAP[tail]
        body = None
    elif request.method == "POST" and tail == "api/mobile/upload":
        if hdr_l.get("x-tutor-mobile") != "1":
            raise web.HTTPForbidden()
        chunks = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            chunks.extend(chunk)
            if len(chunks) > 2662400:
                return web.json_response({"ok": False, "error": "图片过大，请调小选框重试"}, status=413)
        if not chunks:
            raise web.HTTPBadRequest()
        body = io.BytesIO(chunks)
        up_path = "/api/tutor/mobile/upload"
    else:
        raise web.HTTPNotFound()

    def _host_only(h_str: str) -> str:
        if not h_str:
            return ""
        import urllib.parse as _up
        try:
            return (_up.urlsplit(f"//{h_str}").hostname or "").lower()
        except ValueError:
            return h_str.split(":")[0].lower()

    _peer = getattr(request, "remote", None)
    _via_fn = globals().get("_via_local_tunnel")
    via_tunnel = _via_fn(_peer) if _via_fn else (_peer is None or str(_peer).startswith("127.") or str(_peer) in ("::1", "::ffff:127.0.0.1"))
    xfp = (hdr_l.get("x-forwarded-proto") or "").split(",")[0].strip().lower() if via_tunnel else ""
    raw_h = (hdr_l.get("host") or "").strip()
    raw_h_only = _host_only(raw_h)
    xfh = (hdr_l.get("x-forwarded-host") or "").split(",")[0].strip() if (via_tunnel and raw_h_only in ("127.0.0.1", "localhost", "::1")) else ""
    entry_host = xfh or raw_h
    entry_h_only = _host_only(entry_host)
    up_headers = {"Host": "127.0.0.1:8096", "X-TS-Internal": "1"}
    eff_proto = xfp if xfp in ("https", "http") else "http"
    if via_tunnel and xfp == "https" and entry_host and entry_h_only not in ("127.0.0.1", "localhost", "::1"):
        up_headers["X-Tutor-Entry-Base"] = f"https://{entry_host}"
    elif entry_host and entry_h_only not in ("127.0.0.1", "localhost", "::1"):
        _rw_fn = globals().get("_req_allow_wan_dir")
        _po_fn = globals().get("_peer_ok")
        if (_rw_fn and _rw_fn(request)) or (_po_fn and _po_fn(_peer)):
            up_headers["X-Tutor-Entry-Base"] = f"{eff_proto}://{entry_host}"
    if "if-none-match" in hdr_l:
        up_headers["If-None-Match"] = request.headers["If-None-Match"]
    if body is not None:
        up_headers["Content-Type"] = "application/json"

    import aiohttp as _ah, contextlib as _cl
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    cm = _cl.nullcontext(sess) if sess else _ah.ClientSession(timeout=_ah.ClientTimeout(total=36), trust_env=False)
    try:
        async with cm as s, s.request(
            request.method, f"{TUTOR_API}{up_path}", params=request.query,
            headers=up_headers, data=body, allow_redirects=False,
            timeout=_ah.ClientTimeout(total=36 if body is not None else 20),
        ) as r:
            resp_chunks = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                resp_chunks.extend(chunk)
                if len(resp_chunks) > 3145728:
                    return web.Response(status=502, text="AI讲题响应过大", content_type="text/plain")
            resp_bytes = bytes(resp_chunks)
            if tail == "api/mobile/upload" and r.status == 200:
                srv_obj = _app_item(request, INTERCOM_SRV_KEY, "intercom_srv")
                if srv_obj is not None and not MIRROR_FLAG_PATH.exists() and not INTERCOM_CALL_FLAG.exists():
                    should_wake = True
                    with contextlib.suppress(Exception):
                        rj = json.loads(resp_bytes.decode("utf-8"))
                        if rj.get("opened_phone_view") is False:
                            should_wake = False
                    if should_wake:
                        with contextlib.suppress(Exception):
                            try:
                                srv_obj.wake_screen(hold=False, preserve_hold=True)
                            except TypeError:
                                srv_obj.wake_screen(hold=False)
            out_hdrs = {k: r.headers[k] for k in STUDY_FWD_RESP if k in r.headers}
            _fr = globals().get("_admin_frameable")    # 2026-11-20: 管理后台同源嵌入（老测试只抽出本函数时没有这个助手）
            if _fr:
                out_hdrs = _fr(out_hdrs)
            return web.Response(status=r.status, body=resp_bytes, headers=out_hdrs)
    except (_ah.ClientError, asyncio.TimeoutError):
        if tail.startswith("api/"):
            return web.json_response({"ok": False, "error": "AI讲题服务暂时未就绪"}, status=502)
        return web.Response(status=502, text="AI讲题服务暂时未就绪", content_type="text/plain")


async def handle_tutor_upload(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    raw = await _read_stream_bounded(request.content, 640 * 1024)
    if not raw or len(raw) > 640 * 1024:
        return web.json_response({"ok": False, "error": "图片过大，请重试"}, status=413)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise web.HTTPBadRequest()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest()
    sess = _app_item(request, STUDY_SESSION_KEY, "study_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    srv_obj = _app_item(request, INTERCOM_SRV_KEY, "intercom_srv")
    try:
        async with sess.post(
            f"{TUTOR_API}/api/tutor/phone_upload",
            json=data,
            headers={"X-TS-Internal": "1", "Host": "127.0.0.1:8096"},
        ) as r:
            raw_up = await _read_stream_bounded(r.content, 65536)
            if len(raw_up) > 65536:
                return web.json_response({"ok": False, "error": "AI讲题响应过大"}, status=502)
            up = json.loads(raw_up.decode("utf-8"))
            if r.status == 200 and srv_obj is not None and not MIRROR_FLAG_PATH.exists() and not INTERCOM_CALL_FLAG.exists():
                if not (isinstance(up, dict) and up.get("opened_phone_view") is False):
                    with contextlib.suppress(Exception):
                        try:
                            srv_obj.wake_screen(hold=False, preserve_hold=True)
                        except TypeError:
                            srv_obj.wake_screen(hold=False)
            return web.json_response(up, status=r.status if r.status in (200, 400) else 502)
    except Exception:
        return web.json_response({"ok": False, "error": "AI讲题服务暂时未就绪"}, status=502)



async def handle_message_recall(request):
    return await _relay_small_post(request, "/api/home/messages/recall", 1024)


async def handle_message_delete(request):
    return await _relay_small_post(request, "/api/home/messages/delete", 1024)


# ---- 2026-11-20 Claude：管理后台 /admin/（付总要求）。页面和接口都在 thinksmart-admin（127.0.0.1:8097），
# 这里只原样转发，和手机页同一个网址。上传歌曲流式转发（不进内存）；完整面板的实时数据走 WebSocket。
ADMIN_API = "http://127.0.0.1:8097"
ADMIN_SESSION_KEY = web.AppKey("admin_session", aiohttp.ClientSession)
ADMIN_FWD_REQ = ("accept", "accept-encoding", "accept-language", "content-type", "content-length", "cookie", "range", "if-range",
                 "if-none-match", "if-modified-since", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
                 "user-agent", "x-ts-admin", "x-ts-wan", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-for", "x-real-ip")
ADMIN_FWD_RESP = ("Content-Type", "Content-Length", "Content-Encoding", "Content-Disposition", "Content-Range", "Accept-Ranges",
                  "Cache-Control", "ETag", "Last-Modified", "Location", "Retry-After", "Vary", "X-Frame-Options", "X-Content-Type-Options",
                  "Content-Security-Policy", "Referrer-Policy", "Permissions-Policy")
ADMIN_WS_FWD = ("cookie", "origin", "user-agent", "sec-fetch-site", "x-forwarded-proto", "x-forwarded-host",
                "x-forwarded-for", "x-real-ip", "x-ts-orig-host", "x-ts-peer", "x-ts-wan")
ADMIN_STATE_DIR = Path(os.environ.get("ADMIN_STATE", "/var/lib/thinksmart-admin"))
_WAN_CFG_CACHE = {"sig": "unset", "data": {}}
_WAN_SESS_CACHE = {"sig": "unset", "data": {}}
_ADMIN_WS = {"n": 0}
_ADMIN_WS_OPEN = set()
_ADMIN_HTTP_REQS = set()
# 付总定的规矩（2026-11-20）：对讲页和管理后台默认仅限 VPS 隧道（本机）或家里局域网（2026-11-23 加公网外网开关）。
_LAN_NETS = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fe80::/10", "fc00::/7")


def _read_cached_admin_json(name: str, cache: dict, default):
    p = ADMIN_STATE_DIR / name
    try:
        if p.parent.is_symlink() or p.is_symlink():
            return default
        st = p.stat()
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        cache["sig"] = None
        cache["data"] = default
        return default
    if sig != cache["sig"]:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            cache["data"] = raw if isinstance(raw, type(default)) else default
        except Exception:
            cache["data"] = default
        cache["sig"] = sig
    return cache["data"]


def _wan_access_enabled() -> bool:
    d = _read_cached_admin_json("wan_access.json", _WAN_CFG_CACHE, {})
    return bool(isinstance(d, dict) and d.get("enabled"))


def _wan_session_valid(request) -> bool:
    sess_map = None
    now = int(time.time())
    for tok in (request.cookies.get("ts_wan"), request.cookies.get("ts_admin")):
        if not tok or not isinstance(tok, str) or len(tok) > 200:
            continue
        if sess_map is None:
            sess_map = _read_cached_admin_json("sessions.json", _WAN_SESS_CACHE, {})
            if not isinstance(sess_map, dict):
                return False
        rec = sess_map.get(hashlib.sha256(tok.encode()).hexdigest())
        if not isinstance(rec, dict):
            continue
        try:
            seen = int(rec.get("seen", 0))
            ttl = int(rec.get("ttl", 12 * 3600))
        except (TypeError, ValueError):
            continue
        if (now - seen) <= ttl:
            return True
    return False


_LAN_NET_OBJS = None


def _peer_ok(remote) -> bool:
    global _LAN_NET_OBJS
    import ipaddress as _ip
    if _LAN_NET_OBJS is None:
        _LAN_NET_OBJS = tuple(_ip.ip_network(n) for n in _LAN_NETS)
    try:
        ip = _ip.ip_address(str(remote or "").split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LAN_NET_OBJS)


def _admin_fwd_headers(headers, peer: str) -> dict:
    """转给管理后台的请求头。隧道来的（本机地址）保留 nginx 加的访问者 IP；局域网直连的不信这些头，只报连接地址。"""
    via_tunnel = bool(peer) and _via_local_tunnel(peer)
    fwd = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl not in ADMIN_FWD_REQ:
            continue
        if not via_tunnel and (kl.startswith("x-forwarded-") or kl == "x-real-ip" or kl == "x-ts-wan"):
            continue
        fwd[k] = v
    if not _peer_ok(peer):
        fwd["X-TS-WAN"] = "1"
    fwd["X-TS-Orig-Host"] = headers.get("Host", "")
    fwd["X-TS-Peer"] = str(peer or "")
    return fwd


def _admin_frameable(hdrs: dict) -> dict:
    """管理后台（同一网址）要把学习陪伴、拍题页嵌进来：只放行同源嵌入，别的网站仍然不能套。"""
    if "X-Frame-Options" in hdrs:
        hdrs["X-Frame-Options"] = "SAMEORIGIN"
    csp = hdrs.get("Content-Security-Policy")
    if csp:
        hdrs["Content-Security-Policy"] = csp.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    return hdrs


def _admin_url(raw_path: str, ws: bool = False):
    from yarl import URL
    base = ADMIN_API.replace("http://", "ws://", 1) if ws else ADMIN_API
    return URL(base + raw_path, encoded=True)       # 保留百分号编码（Clash 节点名里的 / 之类）


async def handle_admin_redirect(request):
    qs = f"?{request.query_string}" if request.query_string else ""
    raise web.HTTPPermanentRedirect("/admin/" + qs)


async def handle_admin(request):
    sess = _app_item(request, ADMIN_SESSION_KEY, "admin_session")
    if sess is None:
        raise web.HTTPServiceUnavailable()
    raw_path = request.raw_path
    path_only = raw_path.partition("?")[0]
    if (
        not raw_path.startswith("/admin/")
        or any(c in raw_path for c in ("\\", "\r", "\n", " "))
        or "%2e" in path_only.lower()
        or "/.." in path_only
        or ".." in urllib.parse.unquote(path_only).split("/")
    ):
        raise web.HTTPNotFound()
    fwd = _admin_fwd_headers(request.headers, request.remote or "")
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _admin_ws(request, sess, raw_path, fwd)
    body = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.body_exists:
        async def _body():
            async for chunk in request.content.iter_chunked(262144):
                yield chunk
        body = _body()
    else:
        fwd = {k: v for k, v in fwd.items() if k.lower() not in ("content-length", "content-type")}
    out = None
    try:
        async with sess.request(request.method, _admin_url(raw_path), headers=fwd, data=body,
                                allow_redirects=False) as r:
            _ADMIN_HTTP_REQS.add(r)
            disc_task = None
            eof_ok = False

            async def _watch_disc():
                try:
                    while True:
                        await asyncio.sleep(3.0)
                        tr = request.transport
                        if tr is None or tr.is_closing():
                            r.close()
                            return
                except asyncio.CancelledError:
                    return

            try:
                cl_hdr = r.headers.get("Content-Length")
                ctype_low = (r.headers.get("Content-Type") or "").lower()
                if (
                    (cl_hdr is not None and cl_hdr.isdigit() and int(cl_hdr) <= 524288 and "event-stream" not in ctype_low)
                    or request.method == "HEAD"
                    or r.status in (204, 304)
                ):
                    raw_b = b"" if (request.method == "HEAD" or r.status in (204, 304)) else await r.read()
                    resp_fast = web.Response(status=r.status, body=raw_b)
                    for k in ADMIN_FWD_RESP:
                        if k in r.headers and k.lower() != "transfer-encoding":
                            resp_fast.headers[k] = r.headers[k]
                    for c in r.headers.getall("Set-Cookie", []):
                        resp_fast.headers.add("Set-Cookie", c)
                    eof_ok = True
                    return resp_fast
                out = web.StreamResponse(status=r.status)
                for k in ADMIN_FWD_RESP:
                    if k in r.headers:
                        out.headers[k] = r.headers[k]
                for c in r.headers.getall("Set-Cookie", []):
                    out.headers.add("Set-Cookie", c)
                await out.prepare(request)
                if request.method != "HEAD" and r.status not in (204, 304):
                    disc_task = asyncio.create_task(_watch_disc())
                    async for chunk in r.content.iter_chunked(262144):
                        await out.write(chunk)
                await out.write_eof()
                eof_ok = True
                return out
            finally:
                if disc_task is not None:
                    if not disc_task.done():
                        disc_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await disc_task
                if not eof_ok:
                    r.close()
                _ADMIN_HTTP_REQS.discard(r)
    except (aiohttp.ClientError, aiohttp.http_exceptions.HttpProcessingError, asyncio.TimeoutError, ConnectionError, OSError):
        if out is not None and out.prepared:
            if request.transport is not None:
                request.transport.close()
            return out
        return web.json_response({"ok": False, "error": "管理后台暂时没有响应"}, status=502)


async def _admin_ws(request, sess, raw_path, fwd):
    if _ADMIN_WS["n"] >= 8:
        raise web.HTTPTooManyRequests(text="连接太多了")
    _ADMIN_WS["n"] += 1
    hdrs = {k: v for k, v in fwd.items() if k.lower() in ADMIN_WS_FWD}
    up = None
    try:
        try:
            up = await sess.ws_connect(_admin_url(raw_path, ws=True), headers=hdrs, heartbeat=30.0,
                                       max_msg_size=4 * 1024 * 1024)
        except aiohttp.WSServerHandshakeError as e:
            raise web.HTTPUnauthorized() if e.status == 401 else (web.HTTPForbidden() if e.status == 403 else web.HTTPBadGateway())
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise web.HTTPBadGateway()
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
    except BaseException:
        _ADMIN_WS["n"] -= 1
        if up is not None:
            with contextlib.suppress(Exception):
                await up.close()
        raise
    pair = (ws, up)
    _ADMIN_WS_OPEN.add(pair)

    async def _pump(src, dst):
        with contextlib.suppress(ConnectionError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
            async for msg in src:
                if msg.type == WSMsgType.TEXT:
                    await asyncio.wait_for(dst.send_str(msg.data), timeout=15.0)
                elif msg.type == WSMsgType.BINARY:
                    await asyncio.wait_for(dst.send_bytes(msg.data), timeout=15.0)
                else:
                    break
    t1 = asyncio.ensure_future(_pump(ws, up))
    t2 = asyncio.ensure_future(_pump(up, ws))
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _ADMIN_WS_OPEN.discard(pair)
        for t in (t1, t2):
            t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        _ADMIN_WS["n"] -= 1
        with contextlib.suppress(Exception):
            await up.close()
        with contextlib.suppress(Exception):
            await ws.close()
    return ws


async def handle_intercom_status(request):
    hdr_l = {k.lower(): v for k, v in request.headers.items()}
    if hdr_l.get("x-intercom-msg") != "1" or not _msg_origin_ok(hdr_l, request.remote, allow_wan_dir=_req_allow_wan_dir(request)):
        raise web.HTTPForbidden()
    srv = _app_item(request, INTERCOM_SRV_KEY, "intercom_srv")
    if srv is None:
        raise web.HTTPServiceUnavailable()
    shutter_closed, hw_mic_muted = srv.read_gpio_switches()
    return web.json_response({
        "ok": True, "in_call": bool(srv.in_call), "viewers": len(srv.clients), "listeners": len(srv.listeners),
        "shutter_closed": bool(shutter_closed), "hw_mic_muted": bool(hw_mic_muted),
        "mirroring": MIRROR_FLAG_PATH.exists(),
    }, headers={"Cache-Control": "no-store"})


async def main():
    had_call = False
    saved_crash_vol = None
    saved_crash_mute = None
    try:
        if INTERCOM_CALL_FLAG.exists() and not INTERCOM_CALL_FLAG.is_symlink():
            had_call = True
            try:
                raw_cf = _read_regular_file(INTERCOM_CALL_FLAG, max_bytes=256).strip()
                if raw_cf.startswith("{"):
                    cf_obj = json.loads(raw_cf)
                    cf_v = cf_obj.get("vol")
                    if isinstance(cf_v, str) and re.match(r"^(\d+)%$", cf_v):
                        saved_crash_vol = cf_v
                    if isinstance(cf_obj.get("mute"), bool):
                        saved_crash_mute = cf_obj["mute"]
            except Exception:
                pass
        INTERCOM_CALL_FLAG.unlink(missing_ok=True)
    except OSError:
        pass
    srv = IntercomServer()
    srv.set_webkit_paused(False)
    if had_call and not MIRROR_FLAG_PATH.exists():
        srv.wake_screen(hold=False)
    if saved_crash_vol:
        await srv.set_speaker_volume(saved_crash_vol)
        try:
            v_int = max(0, min(100, int(saved_crash_vol[:-1])))
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.2), trust_env=False) as sess:
                async with sess.post("http://127.0.0.1:8090/api/volume", json={"volume": v_int}) as r:
                    await r.read()
        except Exception:
            pass
    if saved_crash_mute is not None:
        await srv.set_speaker_mute(saved_crash_mute)

    shutting_down = [False]

    @web.middleware
    async def intercom_guard(request, handler):
        if shutting_down[0]:
            raise web.HTTPServiceUnavailable()
        via_tun = _via_local_tunnel(request.remote)
        wan_hdr = via_tun and request.headers.get("X-TS-WAN") == "1"
        wan_dir = False
        if not _peer_ok(request.remote):        # 2026-11-20: 只放行隧道（本机）和家里局域网（开启外网开关后放行受控公网）
            if not _wan_access_enabled():
                raise web.HTTPForbidden()
            wan_dir = True
        p_req = request.path
        is_admin_p = (p_req == "/admin" or p_req.startswith("/admin/"))
        if wan_hdr or wan_dir:
            if not _wan_access_enabled():
                if is_admin_p:
                    if p_req.startswith("/admin/api/"):
                        return web.json_response({"ok": False, "error": "外网访问未开启"}, status=403, headers={"Cache-Control": "no-store"})
                    return web.Response(
                        status=403, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store"},
                        text="<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>外网访问未开启</title><body style='font-family:system-ui;background:#0b0f17;color:#eef2f8;display:grid;place-items:center;min-height:90vh;margin:0;padding:24px;text-align:center'><div style='max-width:360px;background:#141a26;border:1px solid rgba(255,255,255,.1);border-radius:20px;padding:24px'><h3 style='margin:0 0 10px'>外网访问未开启</h3><p style='margin:0;color:#8a93a6;font-size:14px;line-height:1.6'>当前管理后台仅限家里内网及专属隧道访问。如需从公网访问，请先在内网进入后台打开「外网访问」按钮。</p></div></body>",
                    )
                raise web.HTTPForbidden()
            if not is_admin_p and not _wan_session_valid(request):
                raise web.HTTPForbidden()
        allow_wan_dir = ((wan_dir or wan_hdr) and (is_admin_p or _wan_session_valid(request)))
        request["ts_allow_wan_dir"] = allow_wan_dir
        if not _msg_origin_ok({k.lower(): v for k, v in request.headers.items()}, request.remote, allow_wan_dir=allow_wan_dir):
            raise web.HTTPForbidden()
        return await handler(request)

    app = web.Application(client_max_size=768 * 1024, middlewares=[intercom_guard])
    app.on_response_prepare.append(keepalive_304)
    app[STUDY_SESSION_KEY] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False)
    app.router.add_get("/", srv.handle_index)
    app.router.add_get("/ws", srv.handle_ws)
    app.router.add_get(r"/{name:favicon\.svg|apple-touch-icon\.png}", handle_icon)
    app.router.add_get("/study", handle_study_redirect)
    app.router.add_route("*", "/study/{tail:.*}", handle_study)
    app.router.add_get("/tutor", handle_tutor_redirect)
    app.router.add_route("*", "/tutor/{tail:.*}", handle_tutor_mobile)
    app.router.add_get("/api/messages", handle_messages)
    app.router.add_post("/api/message", handle_messages)
    app.router.add_post("/api/message/voice", handle_message_voice)
    app.router.add_get("/api/message/audio", handle_message_audio)
    app.router.add_post("/api/message/ack", handle_message_ack)
    app.router.add_post("/api/message/quick", handle_message_quick)
    app.router.add_post("/api/message/cancel", handle_message_cancel)
    app.router.add_post("/api/message/recall", handle_message_recall)
    app.router.add_post("/api/message/delete", handle_message_delete)
    app.router.add_get("/api/countdowns", handle_countdowns_get)
    app.router.add_get("/api/countdown/convert", handle_countdown_convert)
    app.router.add_post("/api/countdown", handle_countdown_save)
    app.router.add_post("/api/countdown/pin", handle_countdown_pin)
    app[INTERCOM_SRV_KEY] = srv
    app.router.add_post("/api/countdown/delete", handle_countdown_delete)
    app.router.add_get("/api/alarms", handle_alarms_get)
    app.router.add_post("/api/alarms", handle_alarms_post)
    app.router.add_post("/api/tutor/upload", handle_tutor_upload)
    app.router.add_get("/api/intercom/status", handle_intercom_status)
    app.router.add_get("/admin", handle_admin_redirect)
    app.router.add_route("*", "/admin/{tail:.*}", handle_admin)
    app[ADMIN_SESSION_KEY] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=3600), trust_env=False, auto_decompress=False)

    async def _close_study_session(app_obj):
        sess = _app_item(app_obj, STUDY_SESSION_KEY, "study_session")
        if sess and not sess.closed:
            await sess.close()

    app.on_cleanup.append(_close_study_session)

    async def _close_admin_session(app_obj):
        sess = _app_item(app_obj, ADMIN_SESSION_KEY, "admin_session")
        if sess and not sess.closed:
            await sess.close()

    app.on_cleanup.append(_close_admin_session)

    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3.0)
    await runner.setup()
    # 2026-11-20 Claude（付总要求：VPS 节点或局域网两种进法）：除了隧道用的本机地址，也在家里局域网的 IPv4 上监听；
    # 只开 IPv4（设备有公网 IPv6，不能开），来源由 intercom_guard 的 _peer_ok 只放行本机和私网地址。
    site = web.TCPSite(runner, "0.0.0.0", 18091)
    await site.start()
    print("[intercom] Listening on http://0.0.0.0:18091 (loopback tunnel + home LAN only)", flush=True)
    status_task = asyncio.create_task(srv.status_loop())
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    try:
        await stopping.wait()
    finally:
        shutting_down[0] = True
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)
        for r_up in list(_ADMIN_HTTP_REQS):
            with contextlib.suppress(Exception):
                r_up.close()
        for sk_k in (STUDY_SESSION_KEY, ADMIN_SESSION_KEY):
            s_up = app.get(sk_k)
            if s_up is not None and not s_up.closed:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(s_up.close(), timeout=0.8)
        all_live_ws = list(srv.clients | srv.all_ws)
        close_coros = [ws.close() for ws in all_live_ws]
        for ws_d, ws_u in list(_ADMIN_WS_OPEN):
            close_coros.append(ws_u.close())
            close_coros.append(ws_d.close())
        if close_coros:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*close_coros, return_exceptions=True), timeout=1.2)
        for ws_left in all_live_ws + [ws_d for ws_d, _ in list(_ADMIN_WS_OPEN)]:
            tr = getattr(getattr(ws_left, "_req", None), "transport", None)
            if tr is not None:
                with contextlib.suppress(Exception):
                    tr.close()
        srv.clients.clear()
        srv.all_ws.clear()
        srv.listeners.clear()
        await srv.stop_call(fast=True)
        await srv.stop_mic_if_idle()
        await srv.stop_camera_if_idle(fast=True)
        with contextlib.suppress(OSError):
            INTERCOM_CALL_FLAG.unlink(missing_ok=True)
        srv.set_webkit_paused(False)
        await site.stop()
        await runner.cleanup()


if __name__ == "__main__":
    os.umask(0o077)
    asyncio.run(main())
