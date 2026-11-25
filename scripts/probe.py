#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资金流仪表盘 — 首跑逐源验证探测器（probe.py）

用途：GitHub Actions 首跑前/首跑时，对给定 URL 做四档递进探测并输出 JSON，
判断出口能否过反爬墙（ICI 的 Akamai、Farside/ETF.com 的 Cloudflare）。
四档（按核查报告 §7.3 / 校验清单首跑检查表）：
  1. 普通 urllib 请求（标准 UA）
  2. 完整浏览器请求头（urllib）
  3. curl_cffi（TLS 指纹仿真）
  4. Playwright 真浏览器（headless Chromium）

每档只记录 HTTP 状态码、耗时、出口 IP/ASN、响应字节数；
不解析、不保存业务数据，不写 data/ 文件。
纯标准库为主：curl_cffi / Playwright 只作可选依赖，未安装时对应档位
标记 available=false 并优雅跳过（注明未装），不报错、不中断其余档位。

用法：
    python3 scripts/probe.py https://www.ici.org/research/stats/flows
    python3 scripts/probe.py <url1> <url2> --timeout 20
输出：JSON（stdout），含 probed_at、egress（出口 IP/ASN）、每 URL 每档结果。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PLAIN_HEADERS = {"User-Agent": "fund-flow-probe/1.0 (python-urllib)"}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# 出口 IP/ASN 查询（公开服务，无需 key；取不到标“未能获取”）
EGRESS_URL = "https://ipinfo.io/json"

# 只读响应前 64KB 计字节数，不解析、不保存业务数据。
MAX_READ_BYTES = 65536


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_egress(timeout=10):
    """取出口公网 IP 与 ASN（org 字段）。取不到返回“未能获取”，不报错。"""
    try:
        request = urllib.request.Request(EGRESS_URL, headers=PLAIN_HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            obj = json.loads(response.read().decode("utf-8"))
        return {
            "ip": obj.get("ip") or "未能获取",
            "asn": obj.get("org") or "未能获取",
        }
    except Exception:
        return {"ip": "未能获取", "asn": "未能获取"}


def _tier(name, egress, status=None, elapsed_ms=None, content_bytes=None,
          error=None, available=True, note=None):
    result = {
        "tier": name,
        "available": available,
        "http_status": status,
        "elapsed_ms": elapsed_ms,
        "egress_ip": (egress or {}).get("ip", "未能获取"),
        "egress_asn": (egress or {}).get("asn", "未能获取"),
        "content_bytes": content_bytes,
        "error": error,
    }
    if note:
        result["note"] = note
    return result


def probe_urllib(url, headers, timeout, egress, tier_name):
    """第 1/2 档：urllib 请求（标准 UA / 完整浏览器请求头）。"""
    start = time.monotonic()
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_READ_BYTES)
            status = response.status
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return _tier(tier_name, egress, status=status, elapsed_ms=elapsed_ms,
                     content_bytes=len(body))
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return _tier(tier_name, egress, status=exc.code, elapsed_ms=elapsed_ms,
                     error=f"HTTP {exc.code}")
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return _tier(tier_name, egress, elapsed_ms=elapsed_ms,
                     error=f"{type(exc).__name__}: {exc}")


def probe_curl_cffi(url, timeout, egress):
    """第 3 档：curl_cffi（Chrome TLS/HTTP2 指纹）。未安装时优雅降级。"""
    try:
        from curl_cffi import requests as crequests
    except ImportError:
        return _tier("3-curl_cffi", egress, available=False,
                     note="curl_cffi 未安装（pip install curl_cffi 后可用），本档跳过")
    start = time.monotonic()
    try:
        response = crequests.get(
            url, headers=BROWSER_HEADERS, impersonate="chrome", timeout=timeout
        )
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return _tier("3-curl_cffi", egress, status=response.status_code,
                     elapsed_ms=elapsed_ms,
                     content_bytes=len(response.content[:MAX_READ_BYTES]))
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return _tier("3-curl_cffi", egress, elapsed_ms=elapsed_ms,
                     error=f"{type(exc).__name__}: {exc}")


def probe_playwright(url, timeout, egress):
    """第 4 档：Playwright 真浏览器（headless Chromium）。未安装时优雅降级。

    只取响应状态码，不读页面内容（不取业务数据）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _tier("4-playwright", egress, available=False,
                     note="Playwright 未安装（pip install playwright && "
                     "playwright install chromium 后可用），本档跳过")
    start = time.monotonic()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    user_agent=BROWSER_HEADERS["User-Agent"]
                )
                response = page.goto(url, timeout=timeout * 1000)
                status = response.status if response is not None else None
            finally:
                browser.close()
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return _tier("4-playwright", egress, status=status, elapsed_ms=elapsed_ms)
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return _tier("4-playwright", egress, elapsed_ms=elapsed_ms,
                     error=f"{type(exc).__name__}: {exc}")


def probe_url(url, timeout, egress):
    """对一个 URL 跑四档递进探测。"""
    return {
        "url": url,
        "tiers": [
            probe_urllib(url, PLAIN_HEADERS, timeout, egress, "1-urllib-plain"),
            probe_urllib(url, BROWSER_HEADERS, timeout, egress, "2-urllib-browser-headers"),
            probe_curl_cffi(url, timeout, egress),
            probe_playwright(url, timeout, egress),
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="逐源四档递进探测（只测可达性：状态码/耗时/出口 IP/ASN，"
        "不取业务数据、不写 data/ 文件）。"
    )
    parser.add_argument("urls", nargs="+", help="要探测的 URL（一个或多个）")
    parser.add_argument("--timeout", type=int, default=30,
                        help="单档超时秒数，默认 30")
    args = parser.parse_args(argv)
    egress = get_egress()
    report = {
        "probed_at": utcnow_iso(),
        "egress": egress,
        "notes": [
            "本工具只记录每档 HTTP 状态码、耗时与出口 IP/ASN，不解析、不保存业务数据，不写 data/ 文件。",
            "curl_cffi / Playwright 未安装时对应档位标记 available=false 并跳过，不报错。",
            "判定标准：状态码 200 且非质询页才算通过；200 但返回质询页/403 文本算不通过（本工具不判内容，须人工核对）。",
        ],
        "results": [probe_url(url, args.timeout, egress) for url in args.urls],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
