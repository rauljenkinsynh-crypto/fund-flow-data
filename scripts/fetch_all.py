#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资金流仪表盘 — GitHub Actions 定时取数脚本（fetch_all.py）

只用 Python 标准库（urllib / json / html.parser），无需 pip 安装依赖。
在 GitHub Actions 里由 .github/workflows/fund-flow-fetch.yml 定时调用；
本地可用 `python3 scripts/fetch_all.py --help` 查看用法（不联网的解析逻辑
见 tests/test_fetch.py，可离线跑通）。

输出：<仓库根目录>/data/<源>.json + data/status.json
退出码：0 = 本次尝试的源全部成功；2 = 部分成功、部分失败/被挡；
        1 = 本次尝试的源全部失败；未配置的源（ICI / ETF.com）不计入退出码。

====================================================================
CONFIG 区 — 全部取数口径集中在此。未验证点一律标注，未经核查不得改口径。
====================================================================
已确切（核查员 2026-11-25 报告钉死，直接用；校正见
hidden_files/github-actions-reconciliation-20261125.md 校正清单 1/2/4）：
  - ICI：四页确切 URL（见 ICI_URLS），HTML 表格；流量单位百万美元、
    货币基金单位十亿美元；历史周会被修正，每次重取最近 5 周全量覆盖。
  - SoSoValue：v1 优先 — GET https://openapi.sosovalue.com/openapi/v1
    /etfs/summary-history（symbol + country_code 必填，limit 默认 50/最大 300；
    字段 date/total_net_inflow/total_value_traded/total_net_assets/
    cum_net_inflow；历史窗口最近 1 个月；T+1，当日行流量字段为 null 须跳过）；
    v2 POST https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart
    保留作备选。同一 symbol 先跑 v1，v1 失败再跑 v2；status.json 记录
    该 symbol 实际走通的是哪条（主机 + 版本）。
  - FRED：GET https://api.stlouisfed.org/fred/series/observations
    ?series_id=DGS10&api_key=...&file_type=json。
  - Farside：全量表 https://farside.co.uk/bitcoin-etf-flow-all-data/ 与
    https://farside.co.uk/ethereum-etf-flow-all-data/，HTML 表格（2026-11-25
    修正：短表 /btc/、/eth/ 只有约 10 天，且表头无 Date 格、日期为 DD Mon YYYY，
    旧解析器认不出；见 parse_farside_html）。
  - 价格（Yahoo）已在 Space 服务端自动跑，本脚本绝不重复取价格。
  - EDGAR companyfacts：https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json，
    免 key，必须带 User-Agent（含联系方式，SEC 要求）；6 家公司 CIK
    以 EDGAR_CIK_BY_TICKER 硬编码为准，ticker 动态解析只作备用。

未验证 / 待 Actions 首次实测（不要自己编）：
  - ETF_COM_URL：ETF.com 无公开 API（v1 结论），本文件目前写 None
    （= 未配置），可能长期保持未配置。
  - SoSoValue v1/v2 在 Actions 出口的实际通断、返回行数与窗口：
    首跑同时探测两主机、两版本，以实测为准；结构对不上时本脚本只写
    失败状态，绝不猜数。
  - Farside 表格数值的单位（按页面惯例假定为百万美元）未验证，
    输出文件里以 unit_assumption 字段明示，等实测校对。
  - EDGAR 全文检索端点 efts.sec.gov/LATEST/search-index 未验证，
    仅作发债事件兜底；查不到就标“未能获取”，绝不编。
  - Actions 出口能否绕过 SoSoValue / Farside / ICI 的反爬墙：未经实测，
    本文件与 README 均不宣称已绕过；首次实测结果以 status.json 为准。
    首跑逐源验证用 scripts/probe.py（四档递进探测，只测可达性）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

# ---------------------------------------------------------------- CONFIG ---
# -- SoSoValue：v1 优先、v2 备选（2026-11-25 校正清单第 2 项）--
# v1：官方文档 Base URL openapi.sosovalue.com/openapi/v1，
# GET /etfs/summary-history，symbol + country_code 必填，limit 默认 50/最大 300。
# v2：POST api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart，
# 不在当前 v1 文档索引中，仅有公开代码佐证，作备选。
SOSOVALUE_V1_URL = "https://openapi.sosovalue.com/openapi/v1/etfs/summary-history"
SOSOVALUE_V2_URL = "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"
SOSOVALUE_BODY = {"type": "us-{symbol_lower}-spot"}  # v2 请求体，类型枚举见核查报告 §4.2
SOSOVALUE_ENV_KEY = "SOSOVALUE_API_KEY"  # 只读环境变量名；key 值绝不写进文件

# -- FRED（口径已确切）--
FRED_URL_TEMPLATE = (
    "https://api.stlouisfed.org/fred/series/observations"
    "?series_id=DGS10&api_key={key}&file_type=json"
)
FRED_ENV_KEY = "FRED_API_KEY"  # 只读环境变量名；key 值绝不写进文件

# -- Farside（口径已确切：URL 与 HTML 表格）--
# 2026-11-25 修正：改取全量表（短表只有约 10 天，且每次成功运行都会把
# farside-*.json 覆写成只有 10 天）；全量表一次拿到完整历史。
FARSIDE_URLS = {
    "btc": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "eth": "https://farside.co.uk/ethereum-etf-flow-all-data/",
}

# -- ICI：四页确切 URL（2026-11-25 校正清单第 1 项，核查报告 §1.1）--
# 长期流量每周三 10:00 ET 发布；综合篇同日同时；ETF 单篇晚约 6 天；
# 货币基金每周四 15:30 ET（感恩节周提前至周三，定时任务不能写死周四）。
# 解析见 parse_ici_flow_table / parse_ici_mmf_table。
ICI_URLS = {
    "flows": "https://www.ici.org/research/stats/flows",
    "combined": "https://www.ici.org/research/stats/combined_flows",
    "etf": "https://www.ici.org/research/stats/etf_flows",
    "mmf": "https://www.ici.org/research/stats/mmf",
}
ICI_INDEX_URL = "https://www.ici.org/topics/weekly-estimated-flow-reports"
ETF_COM_URL = None      # 待核查：ETF.com 每日流量的确切 URL 与格式（v1 结论：无公开 API，可能长期保持未配置）

# -- SEC EDGAR（免 key，必须带 User-Agent）--
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_COMPANYFACTS_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"  # 全文检索兜底，未验证
EDGAR_USER_AGENT = (
    "fund-flow-dashboard/1.0 (research; contact info@stellariausa.com)"
)
# AI 资金链 6 家公司 CIK：硬编码为主（2026-11-25 校正清单第 4 项，
# 来源 SEC 官网 company_tickers.json，核查报告 §5.5 已逐条核对）；
# ticker 动态解析（_resolve_ciks 的 company_tickers.json 路径）只作备用，
# 仅当某家 ticker 不在下表时才走。
EDGAR_CIK_BY_TICKER = {
    "CRWV": "0001769628",
    "ORCL": "0001341439",
    "MSFT": "0000789019",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "AMZN": "0001018724",
}
AI_CHAIN_COMPANIES = [
    {"ticker": "CRWV", "name_cn": "CoreWeave", "name_en": "CoreWeave Inc"},
    {"ticker": "ORCL", "name_cn": "甲骨文", "name_en": "Oracle Corporation"},
    {"ticker": "MSFT", "name_cn": "微软", "name_en": "Microsoft Corporation"},
    {"ticker": "GOOGL", "name_cn": "Alphabet", "name_en": "Alphabet Inc."},
    {"ticker": "META", "name_cn": "Meta", "name_en": "Meta Platforms, Inc."},
    {"ticker": "AMZN", "name_cn": "亚马逊", "name_en": "Amazon.com, Inc."},
]
# 下列口径提醒来自 2026-11-24 已核实采集报告（见 hidden_files/ai-funding-chain-20261124.md），
# 是静态注释、不是取数结果；companyfacts 取不到这些细分时，输出里只标“未能获取”，不推算。
KNOWN_CAVEATS = {
    "ORCL": "OCF 含客户预付款（2027 财年 Q1 为 $11.363B），companyfacts 无法自动剔除，capex/OCF 须人工复核。",
    "CRWV": "公司定义 capex 含融资租赁取得资产，大于现金购置口径；companyfacts 只取现金购置口径，双口径须人工并列。",
    "GOOGL": "有价证券含受短期限制的 SpaceX 股份（2026 Q3 为 $75.2B），净现金口径须人工复核。",
}

# -- 通用网络参数 --
HTTP_TIMEOUT = 30        # 秒，单次请求超时
MAX_RETRIES = 3          # 含首次，共最多 3 次
BACKOFF_BASE_SECONDS = 2  # 指数退避：2s、4s（第 3 次失败后不再等）
# 通用请求头：浏览器 UA（2026-11-25 改：脚本 UA 会被 Farside 的 Cloudflare
# 按 UA 拦；出口探测已证明完整浏览器头在 ICI 与 Farside 上都返回 200）。
# 与 scripts/probe.py 的 BROWSER_HEADERS 保持同一 UA。
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 数据文件清单（输出文件名 -> 源说明）
SOURCES = [
    "ici",
    "farside-btc",
    "farside-eth",
    "sosovalue-btc",
    "sosovalue-eth",
    "fred-dgs10",
    "ai-chain",
    "etf-flows",
]


# ------------------------------------------------------------- 异常与工具 ---
class FetchError(Exception):
    """联网取数失败（超时 / HTTP 错误 / key 缺失等）。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class SchemaError(Exception):
    """返回结构与预期不符——绝不猜数，直接失败。"""


class NotConfigured(Exception):
    """源未配置（URL 待核查），本次跳过，不计入退出码。"""


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message):
    print(f"[{utcnow_iso()}] {message}", file=sys.stderr, flush=True)


def parse_number(raw):
    """把 '$1,234.5' / '(12.3)' / '-' / '.' 等转成 float 或 None。

    括号表示负数（财务表格惯例）；'-'、'.'、'—'、空串表示缺失，不编 0。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace("$", "").replace(",", "").replace("\u00a0", "")
    if text in ("", "-", ".", "—", "--", "n/a", "N/A"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def decide_exit_code(ok_count, attempted_count):
    """退出码规则：0 全成功；2 部分成功；1 全部失败；0 次尝试也返回 0。"""
    if attempted_count == 0:
        return 0
    if ok_count == attempted_count:
        return 0
    if ok_count == 0:
        return 1
    return 2


def http_request(url, method="GET", headers=None, data=None, timeout=HTTP_TIMEOUT):
    """带重试的 HTTP 请求。返回 (status, body_bytes)。

    重试：网络异常 / 5xx / 429 时指数退避，最多 3 次。
    不重试：4xx（除 429 外）直接抛 FetchError；403 会如实记录状态码，
    绝不把“被反爬挡住”写成成功。
    """
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(url, data=body, headers=merged, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace") if exc.fp else ""
            last_error = FetchError(
                f"HTTP {exc.code} {url} :: {detail}", status=exc.code
            )
            if exc.code == 429 or exc.code >= 500:
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    log(f"HTTP {exc.code}，{wait}s 后重试（第 {attempt}/{MAX_RETRIES} 次）")
                    time.sleep(wait)
                    continue
            raise last_error
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = FetchError(f"网络异常 {url} :: {exc}")
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log(f"网络异常，{wait}s 后重试（第 {attempt}/{MAX_RETRIES} 次）")
                time.sleep(wait)
                continue
            raise last_error
    raise last_error  # pragma: no cover - 循环内必定 return 或 raise


def get_json(url, headers=None, timeout=HTTP_TIMEOUT):
    status, body = http_request(url, method="GET", headers=headers, timeout=timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SchemaError(f"返回不是合法 JSON（{url}，HTTP {status}）: {exc}")


def post_json(url, payload, headers=None, timeout=HTTP_TIMEOUT):
    post_headers = dict(headers or {})
    post_headers.setdefault("Content-Type", "application/json")
    status, body = http_request(
        url, method="POST", headers=post_headers, data=payload, timeout=timeout
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SchemaError(f"返回不是合法 JSON（{url}，HTTP {status}）: {exc}")


# ------------------------------------------------------------- 离线解析 ---
# 以下 parse_* 均为纯函数（不联网），tests/test_fetch.py 用 fixture 离线测试。
# 任何一处结构对不上就抛 SchemaError，调用方只写失败状态，绝不猜数。


class _TableParser(HTMLParser):
    """抽取页面里全部 <table>：每表为行列表，每行为单元格文本列表。"""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ""

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(self._cell.strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_farside_date(cell):
    """Farside 日期转 ISO：接受 ISO（2026-11-23）与 'DD Mon YYYY'（23 Nov 2026）。

    后者是页面真实格式；strptime 的 %b 在 Actions 跑手（Ubuntu，C.UTF-8）
    下解析英文月份缩写。转不出返回 None（该行跳过，不编数）。
    """
    cell = cell.strip()
    if _DATE_RE.match(cell):
        return cell
    try:
        return datetime.strptime(cell, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _find_farside_header_row(table):
    """在表的前 6 行里找 ticker 行：非空单元格最多、且末个非空格为 Total。

    logo 行只有 1 个非空格（Total），Fee 行末格为空，都不会被误认；
    数据行的末格是数字，也不会被误认。未找到返回 None。
    """
    best_idx, best_count = None, 0
    for idx, row in enumerate(table[:6]):
        non_empty = [c for c in row if c.strip()]
        if not non_empty or non_empty[-1].lower() != "total":
            continue
        if len(non_empty) > best_count:
            best_idx, best_count = idx, len(non_empty)
    return best_idx


def parse_farside_html(html_text):
    """解析 Farside BTC/ETH 页面 HTML 表格。

    真实页面结构（2026-11-25 实测）：
    - 短表（/btc/、/eth/）第 1 行是发行商 logo 图片（文本为空）+ 末格 Total，
      第 2 行是 ticker，第 3 行是 Fee；全表无 'Date' 格。
    - 全量表（bitcoin/ethereum-etf-flow-all-data/）BTC 第 1 行即
      Date | ticker…| Total；ETH 全量表第 1 行首格为空 + ticker + Total，无 Date。
    - 日期一律 'DD Mon YYYY'（如 23 Nov 2026），不是 ISO。
    因此表头判定改为找 ticker 行；日期列取该行含 'date' 的格，否则取第 0 列；
    Total 列取含 'total' 的格，否则取末列。返回 rows：[{date, total, funds}]。
    无符合条件的表 / 无有效数据行 -> SchemaError。
    """
    parser = _TableParser()
    parser.feed(html_text)
    for table in parser.tables:
        if not table:
            continue
        header_idx = _find_farside_header_row(table)
        if header_idx is None:
            continue
        header = table[header_idx]
        lower = [c.lower() for c in header]
        date_idx = lower.index("date") if "date" in lower else 0
        total_idx = lower.index("total") if "total" in lower else len(header) - 1
        rows = []
        for raw_row in table[header_idx + 1:]:
            if len(raw_row) <= max(date_idx, total_idx):
                continue
            date = _parse_farside_date(raw_row[date_idx])
            if date is None:
                continue
            funds = {}
            for idx, name in enumerate(header):
                if idx in (date_idx, total_idx) or idx >= len(raw_row):
                    continue
                name = name.strip()
                if not name:
                    continue
                value = parse_number(raw_row[idx])
                if value is not None:
                    funds[name] = value
            rows.append(
                {"date": date, "total": parse_number(raw_row[total_idx]), "funds": funds}
            )
        if rows:
            rows.sort(key=lambda r: r["date"])
            return rows
    raise SchemaError("Farside 页面未找到含 Date/Total 列的有效表格（可能被反爬替换或改版）")


_ICI_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _ici_parse_date(cell):
    """ICI 表头日期 'M/D/YYYY' -> 'YYYY-MM-DD'；不是日期返回 None。"""
    match = _ICI_DATE_RE.match(str(cell).strip())
    if not match:
        return None
    month, day, year = (int(match.group(i)) for i in (1, 2, 3))
    try:
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def _ici_find_dated_table(html_text):
    """在页面全部 <table> 里找含日期列的那张表。

    返回 (header_row_index, table, date_cols, change_col)，其中
    date_cols 为 [(列号, ISO 日期)]，change_col 为 '$ Change' 列号或 None。
    表头不一定是第 0 行（标题行可能在上），按首个含日期的行定位；
    无含日期列的表 -> SchemaError。
    """
    parser = _TableParser()
    parser.feed(html_text)
    for table in parser.tables:
        for row_idx, row in enumerate(table):
            date_cols = []
            change_col = None
            for idx, cell in enumerate(row):
                iso = _ici_parse_date(cell)
                if iso is not None:
                    date_cols.append((idx, iso))
                elif "change" in str(cell).lower():
                    change_col = idx
            if date_cols:
                return row_idx, table, date_cols, change_col
    raise SchemaError("ICI 页面未找到含日期列的表格（可能被反爬替换或改版）")


def parse_ici_flow_table(html_text, report):
    """解析 ICI 流量类 HTML 表（flows / combined / etf）。

    表结构（核查报告 §1.2）：单位百万美元；列为最近 5 个周三日期；
    行首为分类名，末行为 Total。返回
    {"report", "unit": "USD millions", "dates": [ISO...],
     "categories": {分类名: {ISO 日期: 数值或 None}},
     "total": {ISO 日期: 数值或 None}}。
    无 Total 行 / 最新周 Total 为空 -> SchemaError，绝不猜数。
    """
    header_idx, table, date_cols, _ = _ici_find_dated_table(html_text)
    categories = {}
    for raw_row in table[header_idx + 1:]:
        if not raw_row:
            continue
        label = str(raw_row[0]).strip()
        if not label:
            continue
        values = {}
        for idx, iso in date_cols:
            values[iso] = parse_number(raw_row[idx]) if idx < len(raw_row) else None
        categories[label] = values
    if "Total" not in categories:
        raise SchemaError(f"ICI {report} 表缺少 Total 行（表结构对不上）")
    dates = sorted({iso for _, iso in date_cols})
    latest = dates[-1]
    if categories["Total"].get(latest) is None:
        raise SchemaError(f"ICI {report} 表最新周（{latest}）Total 为空（表结构对不上）")
    return {
        "report": report,
        "unit": "USD millions",
        "dates": dates,
        "categories": categories,
        "total": {d: categories["Total"].get(d) for d in dates},
    }


def parse_ici_mmf_table(html_text, report="mmf"):
    """解析 ICI 货币基金资产 HTML 表。

    表结构（核查报告 §1.2）：单位十亿美元；列为最近 3 个周三日期 +
    '$ Change' 列；行首为分类名，末行为 Total。返回同流量表结构，
    另加 "total_change": Total 行 $ Change 列数值（十亿美元，可为 None）。
    无 Total 行 / 最新周 Total 为空 -> SchemaError，绝不猜数。
    """
    header_idx, table, date_cols, change_col = _ici_find_dated_table(html_text)
    categories = {}
    total_change = None
    for raw_row in table[header_idx + 1:]:
        if not raw_row:
            continue
        label = str(raw_row[0]).strip()
        if not label:
            continue
        values = {}
        for idx, iso in date_cols:
            values[iso] = parse_number(raw_row[idx]) if idx < len(raw_row) else None
        categories[label] = values
        if label == "Total" and change_col is not None and change_col < len(raw_row):
            total_change = parse_number(raw_row[change_col])
    if "Total" not in categories:
        raise SchemaError(f"ICI {report} 表缺少 Total 行（表结构对不上）")
    dates = sorted({iso for _, iso in date_cols})
    latest = dates[-1]
    if categories["Total"].get(latest) is None:
        raise SchemaError(f"ICI {report} 表最新周（{latest}）Total 为空（表结构对不上）")
    return {
        "report": report,
        "unit": "USD billions",
        "dates": dates,
        "categories": categories,
        "total": {d: categories["Total"].get(d) for d in dates},
        "total_change": total_change,
    }


def _sosovalue_rows_from_list(items):
    """SoSoValue 返回列表的归一化：兼容 date/netInflow 等多种字段名。"""
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise SchemaError(f"SoSoValue 列表元素不是对象：{item!r:.200}")
        date = (
            item.get("date")
            or item.get("time")
            or item.get("timestamp")
            or item.get("day")
        )
        if isinstance(date, (int, float)):  # 毫秒时间戳
            date = datetime.fromtimestamp(date / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        date = str(date)[:10] if date else None
        flow = None
        for key in ("netInflow", "net_flow", "totalNetInflow", "inflow", "netFlow"):
            if key in item and item[key] is not None:
                flow = parse_number(item[key])
                break
        if date is None or not _DATE_RE.match(date):
            raise SchemaError(f"SoSoValue 行缺少合法 date：{item!r:.200}")
        rows.append({"date": date, "net_flow_usd": flow})
    if not rows:
        raise SchemaError("SoSoValue 返回空列表")
    rows.sort(key=lambda r: r["date"])
    return rows


def parse_sosovalue_json(obj):
    """解析 SoSoValue historicalInflowChart 返回。

    兼容两种形态：{"code": 0, "data": {"list": [...]}} 与 {"data": [...]}。
    code 非 0 / 结构不符 -> SchemaError（错误原文写入 error，不猜数）。
    """
    if not isinstance(obj, dict):
        raise SchemaError(f"SoSoValue 返回不是对象：{str(obj)[:200]}")
    if "code" in obj and obj.get("code") not in (0, "0", "SUCCESS"):
        raise SchemaError(f"SoSoValue 返回错误码：code={obj.get('code')} msg={obj.get('msg')}")
    data = obj.get("data")
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        return _sosovalue_rows_from_list(data["list"])
    if isinstance(data, list):
        return _sosovalue_rows_from_list(data)
    raise SchemaError(
        "SoSoValue 返回结构不符（data 既非列表、也不含 list 字段）"
        f"，顶层键：{sorted(obj.keys())}"
    )


def parse_sosovalue_v1_json(obj):
    """解析 SoSoValue v1 GET /etfs/summary-history 返回。

    文档形态为 JSON 数组（倒序，最新在前），每行字段 date /
    total_net_inflow / total_value_traded / total_net_assets /
    cum_net_inflow（美元）；也兼容 {"code": 0, "data": [...]} 包法。
    T+1 结算：当日行 total_net_inflow 为 null -> 跳过该行（不记 0）。
    行缺少 total_net_inflow 字段本身 / 日期不合法 / 全部行被跳过 ->
    SchemaError，绝不猜数。
    """
    if isinstance(obj, dict):
        if "code" in obj and obj.get("code") not in (0, "0", "SUCCESS"):
            raise SchemaError(
                f"SoSoValue v1 返回错误码：code={obj.get('code')} msg={obj.get('msg')}"
            )
        data = obj.get("data")
        if data is None:
            raise SchemaError("SoSoValue v1 返回结构不符（dict 形态缺少 data 数组）")
    elif isinstance(obj, list):
        data = obj
    else:
        raise SchemaError(f"SoSoValue v1 返回不是对象/数组：{str(obj)[:200]}")
    if not isinstance(data, list):
        raise SchemaError("SoSoValue v1 返回 data 不是数组")
    rows = []
    for item in data:
        if not isinstance(item, dict):
            raise SchemaError(f"SoSoValue v1 列表元素不是对象：{item!r:.200}")
        date = item.get("date")
        if date is None or not _DATE_RE.match(str(date)[:10]):
            raise SchemaError(f"SoSoValue v1 行缺少合法 date：{item!r:.200}")
        if "total_net_inflow" not in item:
            raise SchemaError(f"SoSoValue v1 行缺少 total_net_inflow 字段：{item!r:.200}")
        flow = item["total_net_inflow"]
        if flow is None:
            continue  # T+1 当日行未结算，跳过
        rows.append(
            {
                "date": str(date)[:10],
                "net_flow_usd": parse_number(flow),
                "total_value_traded_usd": parse_number(item.get("total_value_traded")),
                "total_net_assets_usd": parse_number(item.get("total_net_assets")),
                "cum_net_inflow_usd": parse_number(item.get("cum_net_inflow")),
            }
        )
    if not rows:
        raise SchemaError("SoSoValue v1 返回无已结算行（全部 total_net_inflow 为 null）")
    rows.sort(key=lambda r: r["date"])
    return rows


def parse_fred_json(obj):
    """解析 FRED observations 返回：{"observations": [{"date", "value"}]}。

    value 为 '.' 表示当日无值 -> None，不编 0。
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("observations"), list):
        raise SchemaError("FRED 返回缺少 observations 列表")
    rows = []
    for item in obj["observations"]:
        if not isinstance(item, dict) or "date" not in item:
            raise SchemaError(f"FRED observations 行结构不符：{item!r:.200}")
        rows.append({"date": str(item["date"])[:10], "value": parse_number(item.get("value"))})
    if not rows:
        raise SchemaError("FRED observations 为空")
    rows.sort(key=lambda r: r["date"])
    return rows


def _latest_fact_value(entries, forms=("10-Q",)):
    """从 companyfacts 的某 tag 的 unit 列表里取最新一期数值。

    只取 form 在 forms 内的记录，按 end 日期倒序；返回
    (value, end, filed) 或 (None, None, None)。
    """
    candidates = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if forms and entry.get("form") not in forms:
            continue
        if entry.get("val") is None or entry.get("end") is None:
            continue
        candidates.append(entry)
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda e: (str(e["end"]), str(e.get("filed", ""))), reverse=True)
    best = candidates[0]
    return parse_number(best["val"]), str(best["end"])[:10], str(best.get("filed", ""))[:10] or None


def parse_companyfacts_json(obj, ticker):
    """解析一家公司的 EDGAR companyfacts，取最新 10-Q 的 OCF / capex / 债务。

    返回公司记录：数值取不到时为 None 并记入 notes，绝不编数；
    顶层结构不对（无 facts/us-gaap）-> SchemaError。
    资产负债表类 tag 取最新 10-Q 或 10-K（以 end 最新者为准）。
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("facts"), dict):
        raise SchemaError(f"{ticker} companyfacts 缺少 facts 字段")
    gaap = obj["facts"].get("us-gaap") or {}
    dei = obj["facts"].get("dei") or {}

    def tag_units(name, namespace="us-gaap"):
        source = gaap if namespace == "us-gaap" else dei
        tag = source.get(name) or {}
        units = tag.get("unit") or {}
        for unit_name in ("USD", "USD/shares", "shares"):
            if units.get(unit_name):
                return units[unit_name]
        for value in units.values():
            if isinstance(value, list):
                return value
        return []

    ocf, ocf_end, ocf_filed = _latest_fact_value(
        tag_units("NetCashProvidedByOperatingActivities")
    )
    capex, _, _ = _latest_fact_value(
        tag_units("PaymentsToAcquirePropertyPlantAndEquipment")
    )
    debt_proceeds, _, _ = _latest_fact_value(
        tag_units("ProceedsFromIssuanceOfLongTermDebt")
    )
    debt_repayments, _, _ = _latest_fact_value(
        tag_units("RepaymentsOfLongTermDebt")
    )
    # 资产负债表：10-Q 或 10-K 中 end 最新者
    balance_forms = ("10-Q", "10-K")
    cash, _, _ = _latest_fact_value(
        tag_units("CashAndCashEquivalentsAtCarryingValue"), forms=balance_forms
    )
    securities, _, _ = _latest_fact_value(
        tag_units("MarketableSecuritiesCurrent"), forms=balance_forms
    )
    debt_noncurrent, debt_end, _ = _latest_fact_value(
        tag_units("LongTermDebtNoncurrent"), forms=balance_forms
    )
    debt_current, _, _ = _latest_fact_value(
        tag_units("LongTermDebtCurrent"), forms=balance_forms
    )
    if debt_current is None:  # 部分公司用 DebtCurrent
        debt_current, _, _ = _latest_fact_value(
            tag_units("DebtCurrent"), forms=balance_forms
        )

    notes = []
    if ocf is None:
        notes.append("OCF 未能获取（companyfacts 无最新 10-Q 的 NetCashProvidedByOperatingActivities）。")
    if capex is None:
        notes.append("capex 未能获取（companyfacts 无最新 10-Q 的 PaymentsToAcquirePropertyPlantAndEquipment）。")
    if debt_proceeds is None and debt_repayments is None:
        notes.append("本季发债/偿还额未能获取（companyfacts 无对应 tag）。")
    if cash is None or debt_noncurrent is None:
        notes.append("资产负债表项目不完整，净债务未能计算。")

    liquid = None
    if cash is not None:
        liquid = cash + (securities or 0.0)
    total_debt = None
    if debt_noncurrent is not None:
        total_debt = debt_noncurrent + (debt_current or 0.0)
    net_debt = None
    if total_debt is not None and liquid is not None:
        net_debt = total_debt - liquid

    capex_ocf_pct = None
    fcf = None
    if ocf is not None and capex is not None:
        capex_ocf_pct = round(capex / ocf * 100, 1) if ocf != 0 else None
        fcf = ocf - capex

    return {
        "ticker": ticker,
        "fiscal_period_end": ocf_end or debt_end,
        "filed_date": ocf_filed,
        "ocf_usd": ocf,
        "capex_cash_usd": capex,
        "capex_ocf_pct": capex_ocf_pct,
        "fcf_usd": fcf,
        "debt_proceeds_usd": debt_proceeds,
        "debt_repayments_usd": debt_repayments,
        "cash_usd": cash,
        "securities_usd": securities,
        "debt_noncurrent_usd": debt_noncurrent,
        "debt_current_usd": debt_current,
        "total_debt_usd": total_debt,
        "net_debt_usd": net_debt,
        "notes": notes,
    }


def parse_edgar_search_json(obj):
    """解析 EDGAR 全文检索（efts）返回：{"hits": {"hits": [{"_source": {...}}]}}。

    无命中返回空列表（不是失败）；结构不符 -> SchemaError。
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("hits"), dict):
        raise SchemaError("EDGAR 全文检索返回缺少 hits 字段")
    hits = obj["hits"].get("hits")
    if not isinstance(hits, list):
        raise SchemaError("EDGAR 全文检索返回 hits.hits 不是列表")
    events = []
    for hit in hits:
        source = hit.get("_source") or {}
        events.append(
            {
                "form": source.get("form"),
                "filing_date": source.get("file_date"),
                "company": source.get("display_names"),
                "accession": source.get("accession_num"),
                "title": (source.get("title") or "")[:200] or None,
            }
        )
    return events


# ------------------------------------------------------------- 联网取数 ---
# 每个 fetch_* 返回可直接写入 data/<源>.json 的 payload（不含 status 字段，
# status 由主流程统一写进 data/status.json）。
# 任一源失败只影响该源：异常由主流程捕获，旧文件保留、status 标 stale。


def _payload(source_url, rows, extra=None):
    payload = {
        "fetched_at": utcnow_iso(),
        "source_url": source_url,
        "rows": rows,
    }
    if extra:
        payload.update(extra)
    return payload


def fetch_sosovalue_v1(symbol, key):
    """SoSoValue v1：GET /etfs/summary-history（首选）。

    返回 (rows, route)；route 含实际走通的主机 + 版本，供 status.json 记录。
    任何失败（HTTP 错误 / 结构不符 / 全部行未结算）都抛异常，由调用方转 v2。
    """
    import urllib.parse

    params = urllib.parse.urlencode(
        {"symbol": symbol, "country_code": "US", "limit": 50}
    )
    url = f"{SOSOVALUE_V1_URL}?{params}"
    obj = get_json(url, headers={"x-soso-api-key": key})
    rows = parse_sosovalue_v1_json(obj)
    route = {
        "host": "openapi.sosovalue.com",
        "version": "v1",
        "endpoint": "/etfs/summary-history",
        "url": url,
    }
    return rows, route


def fetch_sosovalue_v2(symbol, key):
    """SoSoValue v2：POST /openapi/v2/etf/historicalInflowChart（备选）。

    返回 (rows, route)。请求体 type 为 us-btc-spot / us-eth-spot（核查报告 §4.2）。
    """
    body = {
        k: v.replace("{symbol_lower}", symbol.lower())
        for k, v in SOSOVALUE_BODY.items()
    }
    obj = post_json(
        SOSOVALUE_V2_URL,
        body,
        headers={"x-soso-api-key": key},
    )
    rows = parse_sosovalue_json(obj)
    route = {
        "host": "api.sosovalue.xyz",
        "version": "v2",
        "endpoint": "/openapi/v2/etf/historicalInflowChart",
        "url": SOSOVALUE_V2_URL,
    }
    return rows, route


def fetch_sosovalue(symbol):
    """SoSoValue BTC/ETH ETF 每日净流入：v1 优先，v1 失败再跑 v2。

    key 只从环境变量 SOSOVALUE_API_KEY 读（经请求头 x-soso-api-key 发送，
    绝不进 URL、日志与输出文件）。两条都失败才整体失败。
    """
    key = os.environ.get(SOSOVALUE_ENV_KEY)
    if not key:
        raise FetchError(f"缺少 Secret {SOSOVALUE_ENV_KEY}（未配置，无法调用 SoSoValue）")
    errors = []
    try:
        rows, route = fetch_sosovalue_v1(symbol, key)
    except (FetchError, SchemaError) as exc:
        errors.append(f"v1（openapi.sosovalue.com）：{exc}")
        log(f"[sosovalue-{symbol.lower()}] v1 失败，转 v2 备选：{exc}")
        try:
            rows, route = fetch_sosovalue_v2(symbol, key)
        except (FetchError, SchemaError) as exc2:
            errors.append(f"v2（api.sosovalue.xyz）：{exc2}")
            raise FetchError(f"SoSoValue v1 与 v2 均失败：{'; '.join(errors)}")
    return _payload(
        route["url"],
        rows,
        extra={
            "symbol": symbol,
            "route": route,
            "unit": "USD",
            "notes": [
                "v1 优先、v2 备选；实际走通的路线见 route（主机 + 版本），并同步写入 status.json。",
                "v1 历史窗口据官方文档仅最近 1 个月，长历史须靠本仓库逐日累积。",
                "T+1 结算：当日行 total_net_inflow 为 null 时已跳过，未记 0。",
            ],
        },
    )


def fetch_fred_dgs10():
    """FRED DGS10（10 年期国债收益率，日频）。"""
    key = os.environ.get(FRED_ENV_KEY)
    if not key:
        raise FetchError(f"缺少 Secret {FRED_ENV_KEY}（未配置，无法调用 FRED API）")
    url = FRED_URL_TEMPLATE.replace("{key}", key)
    obj = get_json(url)
    rows = parse_fred_json(obj)
    return _payload(
        url.split("&api_key=")[0] + "&api_key=<redacted>&file_type=json",
        rows,
        extra={
            "series_id": "DGS10",
            "unit": "percent",
            "notes": ["value 为 '.' 的日期表示当日无值，已记为 null，未编 0。"],
        },
    )


def fetch_farside(kind):
    """Farside BTC/ETH ETF 流量表。kind 为 'btc' 或 'eth'。"""
    url = FARSIDE_URLS[kind]
    _, body = http_request(url, headers={"Accept": "text/html"})
    rows = parse_farside_html(body.decode("utf-8", "replace"))
    return _payload(
        url,
        rows,
        extra={
            "symbol": kind.upper(),
            "unit": "as-printed",
            "unit_assumption": "Farside 表格数值为百万美元（按页面惯例的假设，未验证，等 Actions 实测校对）。",
            "notes": ["total 为当日全部 ETF 合计净流入（流出为负）。"],
        },
    )


def fetch_ici():
    """ICI 周报（长期基金+ETF 流量、货币基金资产）。

    四页全取（flows / combined / etf / mmf），每次重取最近 5 周全量覆盖
    （历史周会被修正，不只追加最新一周）；输出 ici.json 每次整体覆写，
    不做增量追加。任一页取数失败或结构对不上，整源失败（只写失败状态，
    保留上一次的好文件，绝不猜数、绝不写半份数据）。
    行格式：{"date", "report", "total", "unit", "categories": {分类: 数值}}；
    mmf 的最新周行另含 "change_vs_prior_week"（十亿美元，$ Change 列）。
    """
    pages = [
        ("flows", ICI_URLS["flows"], parse_ici_flow_table),
        ("combined", ICI_URLS["combined"], parse_ici_flow_table),
        ("etf", ICI_URLS["etf"], parse_ici_flow_table),
        ("mmf", ICI_URLS["mmf"], parse_ici_mmf_table),
    ]
    rows = []
    errors = []
    for report, url, parser in pages:
        try:
            _, body = http_request(url, headers={"Accept": "text/html"})
            parsed = parser(body.decode("utf-8", "replace"), report)
        except (FetchError, SchemaError) as exc:
            errors.append(f"{report}（{url}）：{exc}")
            continue
        for date in parsed["dates"]:
            row = {
                "date": date,
                "report": report,
                "total": parsed["total"].get(date),
                "unit": parsed["unit"],
                "categories": {
                    name: values.get(date)
                    for name, values in parsed["categories"].items()
                },
            }
            if report == "mmf" and date == parsed["dates"][-1]:
                row["change_vs_prior_week"] = parsed.get("total_change")
            rows.append(row)
    if errors:
        raise FetchError(f"ICI {len(errors)} 页失败：{'; '.join(errors)}")
    if not rows:
        raise SchemaError("ICI 四页均未解析出任何行")
    rows.sort(key=lambda r: (r["date"], r["report"]))
    return _payload(
        ICI_INDEX_URL,
        rows,
        extra={
            "source_urls": ICI_URLS,
            "unit": "流量/综合/ETF：USD millions；货币基金：USD billions",
            "notes": [
                "每次重取最近 5 周全量覆盖（历史周会被修正），ici.json 整体覆写，不追加。",
                "flows 每周三 10:00 ET 发布；etf 单篇晚约 6 天；mmf 每周四 15:30 ET，节假日周提前至周三。",
                "判据 A（货币基金）取 mmf 最新周 Total 行 $ Change；判据 B（ETF 总量）取 combined 或 etf 表 Total 行最新周。",
            ],
        },
    )


def fetch_etf_flows():
    """ETF.com 每日流量。

    未配置：ETF_COM_URL 为 None。v1 结论为无公开 API，本源可能长期保持
    未配置（走种子数 + 人工更新），此处只作占位，不编 URL、不编数。
    """
    if ETF_COM_URL is None:
        raise NotConfigured(
            "ETF.com 源未配置：无公开 API（v1 核查结论），确切取数方式待核查员报告；报告前本源保持 blocked，不编数。"
        )
    raise NotConfigured("ETF_COM_URL 已填但解析函数尚未实现（总协调校正时补上）。")


def _resolve_ciks():
    """把 ticker 解析为 10 位 CIK：硬编码 EDGAR_CIK_BY_TICKER 为主。

    6 家全在硬编码表里时直接返回，不联网；仅当某家 ticker 不在表里时，
    才回退到 company_tickers.json 动态解析（备用路径，仍须显式 User-Agent）。
    """
    missing = [c["ticker"] for c in AI_CHAIN_COMPANIES if c["ticker"] not in EDGAR_CIK_BY_TICKER]
    if not missing:
        return dict(EDGAR_CIK_BY_TICKER)
    obj = get_json(EDGAR_TICKERS_URL, headers={"User-Agent": EDGAR_USER_AGENT})
    if not isinstance(obj, dict):
        raise SchemaError("company_tickers.json 返回结构不符")
    mapping = dict(EDGAR_CIK_BY_TICKER)
    for entry in obj.values():
        if isinstance(entry, dict) and entry.get("ticker") and entry.get("cik_str") is not None:
            mapping[str(entry["ticker"]).upper()] = str(entry["cik_str"]).zfill(10)
    still_missing = [t for t in missing if t not in mapping]
    if still_missing:
        raise SchemaError(f"company_tickers.json 缺少 ticker：{still_missing}")
    return mapping


def _search_debt_events(company):
    """发债事件兜底：EDGAR 全文检索该公司近期的票据发行相关 8-K。

    未验证端点；任何失败都只记入 note，不阻塞 ai-chain.json，
    查不到就标“未能获取”，绝不编事件。
    """
    import urllib.parse

    query = f'"{company["name_en"]}" notes offering'
    params = urllib.parse.urlencode(
        {"q": query, "dateRange": "custom", "forms": "8-K"}
    )
    url = f"{EDGAR_SEARCH_URL}?{params}"
    try:
        obj = get_json(url, headers={"User-Agent": EDGAR_USER_AGENT})
        events = parse_edgar_search_json(obj)
        return events, url
    except (FetchError, SchemaError) as exc:
        return [], f"{url}（检索失败：{exc}）"


def fetch_ai_chain():
    """AI 资金链：6 家公司最新季 OCF / capex / 债务（EDGAR companyfacts）。

    单家公司失败不阻塞其余 5 家（该家记 error 条目）；6 家全失败才整体失败。
    发债事件用 EDGAR 全文检索兜底，查不到标“未能获取”。
    """
    cik_map = _resolve_ciks()
    companies = []
    ok_count = 0
    for company in AI_CHAIN_COMPANIES:
        ticker = company["ticker"]
        record = {
            "ticker": ticker,
            "name_cn": company["name_cn"],
            "name_en": company["name_en"],
            "status": "failed",
        }
        try:
            cik = cik_map[ticker]
            url = EDGAR_COMPANYFACTS_TEMPLATE.replace("{cik10}", cik)
            obj = get_json(url, headers={"User-Agent": EDGAR_USER_AGENT})
            parsed = parse_companyfacts_json(obj, ticker)
            record.update(parsed)
            record["cik"] = cik
            record["source_url"] = url
            record["status"] = "ok"
            ok_count += 1
            if ticker in KNOWN_CAVEATS:
                record.setdefault("notes", []).append("口径提醒：" + KNOWN_CAVEATS[ticker])
        except (FetchError, SchemaError) as exc:
            record["error"] = str(exc)
            record.setdefault("notes", []).append("本季数字未能获取，原因见 error；绝不编数。")
        # 发债事件兜底（best-effort，不阻塞）
        events, search_url = _search_debt_events(company)
        record["debt_events"] = events
        record["debt_events_source"] = search_url
        if not events:
            record.setdefault("notes", []).append(
                "发债事件：EDGAR 全文检索无命中或检索未验证，标为“未能获取”。"
            )
        companies.append(record)
    if ok_count == 0:
        raise FetchError("AI 资金链 6 家公司 companyfacts 全部失败（见各公司 error）。")
    return _payload(
        "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
        companies,
        extra={
            "unit": "USD",
            "ok_companies": ok_count,
            "total_companies": len(AI_CHAIN_COMPANIES),
            "notes": [
                "口径：最新已公布 10-Q 的单季度；资产负债表取最新 10-Q/10-K。",
                "capex 为现金购置口径（PaymentsToAcquirePropertyPlantAndEquipment）；"
                "公司定义口径（含融资租赁）companyfacts 无法自动区分，须人工并列。",
                "净债务 =（长期债非流动 + 一年内到期）−（现金及等价物 + 有价证券），租赁负债不计入。",
                "发债事件检索端点未验证；无命中只标“未能获取”。",
            ],
        },
    )


# 每源的取数函数与输出文件名
FETCHERS = {
    "ici": (fetch_ici, "ici.json"),
    "farside-btc": (lambda: fetch_farside("btc"), "farside-btc.json"),
    "farside-eth": (lambda: fetch_farside("eth"), "farside-eth.json"),
    "sosovalue-btc": (lambda: fetch_sosovalue("BTC"), "sosovalue-btc.json"),
    "sosovalue-eth": (lambda: fetch_sosovalue("ETH"), "sosovalue-eth.json"),
    "fred-dgs10": (fetch_fred_dgs10, "fred-dgs10.json"),
    "ai-chain": (fetch_ai_chain, "ai-chain.json"),
    "etf-flows": (fetch_etf_flows, "etf-flows.json"),
}


# ------------------------------------------------------------- 输出与状态 ---
def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_file(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def run_fetch(selected, out_dir):
    """跑选定的源。返回 (ok_count, attempted_count, status_entries)。

    规则：任一源失败不阻塞其他源；失败时保留上一次的好文件（不覆写），
    status.json 里该源标 failed/blocked 并保留 last_ok。
    未配置的源（NotConfigured）记 blocked，不计入 attempted。
    """
    os.makedirs(out_dir, exist_ok=True)
    prev_status = _load_json_file(os.path.join(out_dir, "status.json"))
    prev_sources = prev_status.get("sources") or {}
    status_entries = {}
    ok_count = 0
    attempted_count = 0
    for name in selected:
        func, filename = FETCHERS[name]
        target = os.path.join(out_dir, filename)
        prev_entry = prev_sources.get(name) or {}
        entry = {
            "status": "failed",
            "error": None,
            "fetched_at": utcnow_iso(),
            "last_ok": prev_entry.get("last_ok"),
        }
        try:
            payload = func()
        except NotConfigured as exc:
            entry["status"] = "blocked"
            entry["error"] = str(exc)
            log(f"[{name}] BLOCKED（未配置，跳过）：{exc}")
        except (FetchError, SchemaError) as exc:
            attempted_count += 1
            status_label = "blocked" if getattr(exc, "status", None) == 403 else "failed"
            entry["status"] = status_label
            entry["error"] = str(exc)[:500]
            log(f"[{name}] {status_label.upper()}：{exc}（保留上一次的好文件）")
        except Exception as exc:  # 兜底：任何未预料异常都不阻塞其他源
            attempted_count += 1
            entry["status"] = "failed"
            entry["error"] = f"未预料异常 {type(exc).__name__}: {exc}"[:500]
            log(f"[{name}] FAILED（未预料异常）：{exc}")
        else:
            attempted_count += 1
            ok_count += 1
            _write_json_file(target, payload)
            entry["status"] = "ok"
            entry["error"] = None
            entry["last_ok"] = payload["fetched_at"]
            if isinstance(payload.get("route"), dict):
                # SoSoValue：status.json 记录该 symbol 实际走通的是哪条（主机 + 版本）
                entry["route"] = payload["route"]
            log(f"[{name}] OK：{len(payload.get('rows') or [])} 行 -> {filename}")
        if entry["status"] in ("failed", "blocked") and entry.get("last_ok"):
            entry["stale"] = True  # 有旧文件可 fallback，标记为 stale 而非彻底无数
        status_entries[name] = entry
    # 未被本次选中的源：沿用上一次 status（不改写）
    for name, prev_entry in prev_sources.items():
        if name not in status_entries:
            status_entries[name] = prev_entry
    status_payload = {
        "fetched_at": utcnow_iso(),
        "sources": status_entries,
        "notes": [
            "status ok/failed/blocked 按源分别记录；failed/blocked 时保留上一次的好文件并标 stale。",
            "blocked 含两种：未配置（ETF.com 待核查，跳过不计退出码）与 HTTP 403 反爬拦截（计入失败）。",
            "SoSoValue 各 symbol 的 route 字段记录本次实际走通的主机 + 版本（v1 优先、v2 备选）。",
            "Actions 出口能否绕过反爬墙未验证，首次实测结果以本文件为准。",
        ],
    }
    _write_json_file(os.path.join(out_dir, "status.json"), status_payload)
    return ok_count, attempted_count, status_entries


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="资金流仪表盘 GitHub Actions 取数脚本（只用标准库）。"
    )
    parser.add_argument(
        "--only",
        default="all",
        help="只跑指定源，逗号分隔，如 farside-btc,sosovalue-btc；默认 all（全部 8 个源）。",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="输出目录，默认 <仓库根目录>/data（脚本位于 scripts/ 时自动推断；否则用 ./data）。",
    )
    args = parser.parse_args(argv)
    if args.only.strip().lower() == "all":
        selected = list(SOURCES)
    else:
        selected = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in selected if s not in FETCHERS]
        if unknown:
            print(f"未知源：{unknown}，可选：{SOURCES}", file=sys.stderr)
            return 1
        if not selected:
            print("未指定任何源。", file=sys.stderr)
            return 1
    if args.out_dir:
        out_dir = args.out_dir
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(script_dir)
        out_dir = os.path.join(repo_root, "data")
    ok_count, attempted_count, _ = run_fetch(selected, out_dir)
    code = decide_exit_code(ok_count, attempted_count)
    log(
        f"完成：{ok_count}/{attempted_count} 源成功"
        + ("（其余为未配置跳过）" if attempted_count < len(selected) else "")
        + f"，退出码 {code}"
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
