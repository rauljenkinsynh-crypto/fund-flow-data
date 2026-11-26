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
import gzip
import html
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
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

# -- FRED 信用利差（OAS，2026-11-25 接入；与 DGS10 同款 API 路径）--
# 六档 ICE BofA OAS，单位百分点（%）。FRED 数据 T+1，当日值次日早上才出，
# 最新值截至日一律以 FRED 返回的日期为准，不得写当天日期。
# 序列窗口：最近 3 年（observation_start 动态算），供面板画走势；FRED
# 网站自 2026-04 起只显示近 3 年，API 全量则太重，3 年窗口与之对齐。
# 锚点（2026-11-25 用 fredgraph.csv 直取验证）：2026-11-24 高收益 3.72%、
# 投资级 0.98%、BBB 1.22%、BB 2.25%、单 B 4.05%、CCC 14.05%；
# CCC−B 缺口当日 = 14.05 − 4.05 = 10.00 个百分点。
FRED_CREDIT_SERIES = {
    "BAMLH0A0HYM2": "高收益债 OAS",
    "BAMLC0A0CM": "投资级 OAS",
    "BAMLC0A4CBBB": "BBB 级 OAS",
    "BAMLH0A1HYBB": "BB 级 OAS",
    "BAMLH0A2HYB": "单 B 级 OAS",
    "BAMLH0A3HYC": "CCC 及以下 OAS",
}
FRED_CREDIT_SHORT_KEYS = {
    "BAMLH0A0HYM2": "hy",
    "BAMLC0A0CM": "ig",
    "BAMLC0A4CBBB": "bbb",
    "BAMLH0A1HYBB": "bb",
    "BAMLH0A2HYB": "b",
    "BAMLH0A3HYC": "ccc",
}
FRED_CREDIT_B_SERIES = "BAMLH0A2HYB"    # 缺口公式的减项
FRED_CREDIT_CCC_SERIES = "BAMLH0A3HYC"  # 缺口公式的被减项
FRED_CREDIT_WINDOW_DAYS = 1095  # 最近 3 年

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

# -- ETF 流量（etf-flows.json）：TickerInside + 发行商文件自算（2026-11-25 第一阶段）--
# 方案依据：hidden_files/etf-flows-combined-plan-20261125.md（全文）。
# TickerInside series.json：461 只 × 最近 20 个收盘日；本仪表盘只取 7 只
# （SOXX、SPY、XLP、XLV、XLE、XLU、XLK）；flow_usd 为 null 的日原样保留
# “未测得”，绝不填 0、不插值；滚动保留最近 60 个收盘日（历史靠逐日累积）。
# 自算公式：当日流量（美元）= 当日 NAV ×（当日份额 − 前一日份额）；快照文件
# data/etf-selfcalc-snapshots.json 由 fetch_etf_flows 自己读写（上线首日无
# 昨日快照，流量记 null，绝不写 0）。SMH 自算主路（VanEck NAV 历史 XLSX，
# 第二阶段；持仓 XLSX 无基金级份额/净值，见 parse_vaneck_nav_history）。
# QQQ/VOO 自算主路（新浪 gb_ 行情份额，2026-11-25 接入）：Invesco 自家接口
# 无份额字段（实测 2026-11-25：shareclasses 只有总资产+业绩 8 个字段），
# 新浪第 19 字段为份额数（价格×份额=总市值字段，实测逐项一致）。
TICKERINSIDE_SERIES_URL = "https://tickerinside.com/api/v1/etf-flows/series.json"
TICKERINSIDE_TICKERS = ["SOXX", "SPY", "XLP", "XLV", "XLE", "XLU", "XLK"]
TICKERINSIDE_ATTRIBUTION = {
    "credit": "TickerInside",
    "url": "https://tickerinside.com/api/v1/etf-flows/series.json",
    "note": "须署名 TickerInside + 链接；每次只取最新文件，不攒整档历史。",
}
# 锚点（2026-11-25 实测值）：SOXX 2026-11-11 行 flow_usd 约 +1,065,556,947 美元。
# 注意：方案 §7 另写该日“占总资产 2.38%”，与种子 AUM 约 $350 亿不自洽
# （$10.66 亿 / $350 亿 = 3.05%，2.38% 对应 AUM 约 $448 亿）；锚点只核对
# flow_usd 本身，pct/AUM 一律以文件实值为准，偏差进 notes，不强行凑数。
TICKERINSIDE_ANCHOR = {
    "ticker": "SOXX",
    "date": "2026-11-11",
    "flow_usd": 1065556947.0,
    "tolerance": 0.20,  # 偏差 >20% 只记警告，不阻塞整源
}
# 种子 AUM（亿美元单位 usd_100m；SOXX 约 $350 亿，见 space-changes §1.3 种子对照）：
# TickerInside 反推 AUM（= flow / flow_pct）与种子偏差 >20% 时 aum 记 null。
SEED_AUM_100M = {"SOXX": 350.0}
ETF_TRACKED_ORDER = [
    "SOXX", "SMH", "VOO", "SPY", "QQQ", "IBIT",
    "XLP", "XLV", "XLE", "XLU", "XLK", "TLT", "GLD",
]
ETF_LAG_SWITCH_TRADING_DAYS = 3  # TickerInside 滞后超 3 个交易日自动切自算备用
ETF_DAILY_KEEP_DAYS = 60         # daily 序列滚动保留最近 60 个收盘日
ETF_SELFCALC_SNAPSHOTS_FILENAME = "etf-selfcalc-snapshots.json"
# fetch_etf_flows 的 out_dir：run_fetch 在调用前经模块级变量传入
# （run_fetch 只写 fetcher 自己的目标文件，快照读写须在 fetch_etf_flows 内做）；
# 直接调用 fetch_etf_flows(out_dir) 时以参数为准。
ETF_FLOWS_OUT_DIR = None

# -- BlackRock / iShares v1 holdings CSV（TLT、SOXX 已验证）--
BLACKROCK_HOLDINGS_URL_TEMPLATE = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
    "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US"
    "&portfolioId={portfolio_id}&userType=individual&component=holdings"
)
# portfolioId 已验证（2026-11-25 实测）：TLT 239454、SOXX 239705。
# XLK 是 SPDR（State Street）产品，没有 iShares portfolioId，不走 BlackRock 路；
# 方案 §九“BlackRock 覆盖 SOXX/XLK”一说有误，XLK 自算只走 SSGA。
BLACKROCK_PORTFOLIO_IDS = {"TLT": 239454, "SOXX": 239705}
# 净值合理性区间（美元，按 2026-11-25 市价定的宽带；超出即该标的 failed，不发布离谱数）
BLACKROCK_NAV_RANGES = {"TLT": (40.0, 200.0), "SOXX": (100.0, 400.0)}

# -- State Street / SPDR 基金页（美国版 URL，2026-11-25 逐只验证）--
# 页内“Fund Net Asset Value”表含 NAV 与 Shares Outstanding；欧洲版 URL 会出现
# 361,80 M 这类逗号小数格式，绝不用（解析时做美国格式校验，见 parse_ssga_fund_page）。
# 2026-11-24 实测值：GLD NAV $331.98 / 360.40M；SPY $718.51 / 1,071.28M；
# XLP $91.86 / 170.12M；XLV $184.63 / 262.82M；XLE $60.39 / 627.75M；
# XLU $43.40 / 533.50M；XLK $157.03 / 676.06M。
SSGA_FUND_PAGES = {
    "GLD": "https://www.ssga.com/us/en/intermediary/etfs/spdr-gold-shares-gld",
    "SPY": "https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-sp-500-etf-trust-spy",
    "XLP": "https://www.ssga.com/us/en/individual/etfs/state-street-consumer-staples-select-sector-spdr-etf-xlp",
    "XLV": "https://www.ssga.com/us/en/individual/etfs/state-street-health-care-select-sector-spdr-etf-xlv",
    "XLE": "https://www.ssga.com/us/en/intermediary/etfs/state-street-energy-select-sector-spdr-etf-xle",
    "XLU": "https://www.ssga.com/us/en/institutional/etfs/state-street-utilities-select-sector-spdr-etf-xlu",
    "XLK": "https://www.ssga.com/us/en/individual/etfs/state-street-technology-select-sector-spdr-etf-xlk",
}
# 净值合理性区间（美元）：XLU 下限 30 而非方案写的 50——2026-11-25 实测 XLU NAV
# 为 $43.40，已低于 50，按方案区间会天天误判 failed；其余按方案。
SSGA_NAV_RANGES = {
    "GLD": (200.0, 500.0), "SPY": (400.0, 800.0),
    "XLP": (50.0, 300.0), "XLV": (50.0, 300.0), "XLE": (50.0, 300.0),
    "XLU": (30.0, 300.0), "XLK": (100.0, 400.0),
}
# -- VanEck / SMH：NAV 历史 XLSX（第二阶段，2026-11-25 实测）--
# 持仓 XLSX（.../holdings/download/xlsx/，方案 §4.3 的原定文件）只有持仓明细，
# 没有基金级份额/净值，算不了流量——实测 2026-11-25：单表 31 行，
# 只有 Number/Ticker/持仓名/FIGI/持仓股数/市值/% of Net Assets。
# 改取 NAV 历史文件 .../downloads/fundhistoprices/：逐日 NAV（4 位小数）+
# 总资产 AUM（精确到分），份额 = 总资产 / NAV（NAV 定义即总资产 / 份额，
# 定义上精确；2026-11-24 实测 NAV 427.3930、AUM 57,053,492,930.04、
# 份额 133,491,875.00，与基金页 NAV $427.39、总资产 $57.05B 交叉一致）。
# 该文件同样有 cookie 门（无 cookie 的请求 302 到 /corp/en/disabled-cookies/），
# 但服务端只要求回传它在 301 响应里设置的会话/地区 cookie，
# urllib + CookieJar 持久化 cookie 即可取到（2026-11-25 本机实测 200，
# 文件 322,492 字节）；不需要 Playwright（本机沙箱 Chromium 反而过不了
# egress 代理隧道；Actions 直连时 Playwright 可作备用，但本脚本不依赖它）。
# 许可：同 BlackRock，只存衍生数（日期、份额、净值、流量），绝不存文件原文。
VANECK_NAV_HISTORY_URL = (
    "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh"
    "/downloads/fundhistoprices/"
)
# 净值合理性区间（美元，按 2026-11-25 市价定的宽带；SMH 近期 NAV 约 $400–487）
VANECK_NAV_RANGES = {"SMH": (100.0, 800.0)}

# -- 新浪财经美股行情（QQQ、VOO 份额主路；SOXX、TLT、IBIT 交叉核对）--
# 接口 http://hq.sinajs.cn/list=gb_qqq,gb_voo,gb_soxx,gb_tlt,gb_ibit，须带
# Referer http://finance.sina.com.cn（不带回 Forbidden）；返回 GBK 编码，
# 一行一个品种，逗号分隔；数字为 ASCII，可直接解析。
# 字段（从 0 数，2026-11-25 12:39 EST 实测逐项核对）：1=最新价，12=总市值，
# 19=份额数（总股本；注意：任务书初版写 20 是错的，实测价格×第 19 字段才
# 等于总市值字段，第 20 字段恒为 0），25=美东时间（如 "Nov 25 12:39PM EST"，
# 无年份），26=前收。实测值：QQQ 价 622.28、份额 694,250,000；
# VOO 价 651.98、份额 1,548,595,410（VOO 前收 660.71 与 Vanguard 官方
# 11-24 市价逐分一致；QQQ 份额与 Invesco 总资产反推隐含 NAV $639.03，
# 溢价 0.15%，合理）。
# 流量只能用收盘价算：只有美东收盘后（跑的当前美东时间 ≥16:00，且第 25
# 字段时间 ≥16:00，即字段 1 为收盘价）才写快照；盘中跑跳过快照、源仍记 ok。
# 快照日期取第 25 字段的美东日期，NAV 口径取收盘价（字段 1）。
# 13 只覆盖矩阵（2026-11-25 13:03 一次批量实测，全 38 字段逐项核对）：
# - 主路（selfcalc-sina）：QQQ 694,250,000、VOO 1,548,595,410（与 12:24
#   取值一致，份额盘中不更新、隔夜更新）。
# - 交叉核对（xcheck-sina，只核对不算流量）：SOXX 267,500,000、TLT
#   600,000,000 与 BlackRock 11-24 快照精确一致；IBIT 1,329,760,000
#   （价 33.705，总市值 $44.72B 自洽），Farside 不给份额、无主源可比。
# - 不接：XLV、GLD 份额与总市值均为 0（无数据）；SPY/XLK/XLP/XLE/XLU/
#   SMH 与发行商 11-24 快照差 0.7%–2.5%（SPY 1.6%、XLK 0.7%、XLP 2.5%、
#   XLE 1.1%、XLU 1.2%、SMH 2.4%），新浪不公布份额截至日、无法判定是滞后
#   还是口径差——六只绝不接入新浪快照，也不许用新浪覆盖主源。
# 铁律：交叉核对快照存进同一快照文件、键加 _xcheck 后缀（绝不占主源键位，
# 不覆盖 BlackRock/Farside 快照）；SOXX/TLT 同日份额偏差 >1% 只在 note
# 里标出、不改数；IBIT 只存快照、不算流量、不改 Farside 的数。
SINA_GB_URL = "http://hq.sinajs.cn/list=gb_qqq,gb_voo,gb_soxx,gb_tlt,gb_ibit"
SINA_REFERER = "http://finance.sina.com.cn"
SINA_TICKERS = ["QQQ", "VOO"]
SINA_XCHECK_TICKERS = ["SOXX", "TLT", "IBIT"]
# 净值合理性区间（美元，按 2026-11-25 市价定的宽带；超出即该标的 failed）
SINA_NAV_RANGES = {
    "QQQ": (300.0, 900.0), "VOO": (300.0, 900.0),
    "SOXX": (50.0, 300.0), "TLT": (30.0, 200.0), "IBIT": (10.0, 100.0),
}

# 自算分工：TLT、GLD、SMH、QQQ、VOO 以自算为主路；7 只 TickerInside 标的以自算为备用
# （滞后 >3 个交易日自动切换；快照每天都取，保持备用快照温热，切换当日即有昨日数可算）。
SELFCALC_PRIMARY = {
    "TLT": "blackrock", "GLD": "ssga", "SMH": "vaneck",
    "QQQ": "sina", "VOO": "sina",
}
SELFCALC_BACKUP = {
    "SOXX": "blackrock",
    "SPY": "ssga", "XLP": "ssga", "XLV": "ssga",
    "XLE": "ssga", "XLU": "ssga", "XLK": "ssga",
}

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
# 通用请求头：与 scripts/probe.py 的 BROWSER_HEADERS 逐字一致的浏览器头
# （2026-11-25 改：只换 UA、不补齐整组头会被 Akamai 当“冒充浏览器”拦；
# Accept-Encoding 只写 gzip, deflate、不写 br，因为标准库没有 brotli 解压）。
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# 数据文件清单（输出文件名 -> 源说明）
SOURCES = [
    "ici",
    "farside-btc",
    "farside-eth",
    "sosovalue-btc",
    "sosovalue-eth",
    "fred-dgs10",
    "fred-credit",
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


def _decode_body(body, content_encoding):
    """按 Content-Encoding 解压响应体：gzip / deflate；无编码或未知编码原样返回。

    请求头已声明 Accept-Encoding: gzip, deflate，服务器可能返回压缩体；
    不解压就直接解析会失败。未知编码不猜、原样返回（调用方解析失败即报失败）。
    """
    if not content_encoding:
        return body
    encoding = content_encoding.strip().lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    return body


def _redact_url(url):
    """把 URL 里 api_key 的值替换成 <redacted>，供报错信息使用，防 key 泄漏。"""
    return re.sub(r"api_key=[^&\s\"']*", "api_key=<redacted>", url)


def http_request(url, method="GET", headers=None, data=None, timeout=HTTP_TIMEOUT,
                 cookies=False):
    """带重试的 HTTP 请求。返回 (status, body_bytes)。

    重试：网络异常 / 5xx / 429 时指数退避，最多 3 次。
    不重试：4xx（除 429 外）直接抛 FetchError；403 会如实记录状态码，
    绝不把“被反爬挡住”写成成功。
    cookies=True 时用 CookieJar 持久化 cookie（VanEck 的 cookie 门需要：
    服务端在重定向响应里下发会话/地区 cookie，回传即 200；无 cookie 的
    请求会被 302 到 /corp/en/disabled-cookies/）。默认 False，其他源行为不变。
    """
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(url, data=body, headers=merged, method=method)
        try:
            if cookies:
                jar = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler(),
                    urllib.request.HTTPCookieProcessor(jar),
                )
                response = opener.open(request, timeout=timeout)
            else:
                response = urllib.request.urlopen(request, timeout=timeout)
            with response as resp:
                raw = resp.read()
                try:
                    decoded = _decode_body(raw, resp.headers.get("Content-Encoding"))
                except Exception as exc:
                    raise FetchError(f"响应解压失败 {_redact_url(url)} :: {exc}")
                return resp.status, decoded
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace") if exc.fp else ""
            last_error = FetchError(
                f"HTTP {exc.code} {_redact_url(url)} :: {detail}", status=exc.code
            )
            if exc.code == 429 or exc.code >= 500:
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    log(f"HTTP {exc.code}，{wait}s 后重试（第 {attempt}/{MAX_RETRIES} 次）")
                    time.sleep(wait)
                    continue
            raise last_error
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = FetchError(f"网络异常 {_redact_url(url)} :: {exc}")
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
        raise SchemaError(f"返回不是合法 JSON（{_redact_url(url)}，HTTP {status}）: {exc}")


def post_json(url, payload, headers=None, timeout=HTTP_TIMEOUT):
    post_headers = dict(headers or {})
    post_headers.setdefault("Content-Type", "application/json")
    status, body = http_request(
        url, method="POST", headers=post_headers, data=payload, timeout=timeout
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SchemaError(f"返回不是合法 JSON（{_redact_url(url)}，HTTP {status}）: {exc}")


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


def _latest_fred_value(rows):
    """取 FRED observations 行里最后一个非 None 值的 (date, value)；全是 None 则返回 (None, None)。"""
    for row in reversed(rows or []):
        if row.get("value") is not None:
            return row["date"], row["value"]
    return None, None


def compute_ccc_minus_b(rows_b, rows_ccc):
    """CCC−B 缺口：按日期对齐单 B 与 CCC 两档 OAS 序列，相减。

    返回 [{"date", "gap"}]（两档日期并集，按日期排序；当日任一档无值则
    gap 为 None，不编 0、不插值）。
    """
    by_date_b = {r["date"]: r["value"] for r in rows_b or []}
    by_date_ccc = {r["date"]: r["value"] for r in rows_ccc or []}
    out = []
    for date in sorted(set(by_date_b) | set(by_date_ccc)):
        vb = by_date_b.get(date)
        vc = by_date_ccc.get(date)
        gap = (vc - vb) if (vb is not None and vc is not None) else None
        out.append({"date": date, "gap": gap})
    return out


def build_fred_credit_rows(series_rows):
    """把六档 OAS 序列拼成逐日总表：[{date, hy, ig, bbb, bb, b, ccc, ccc_minus_b}]。

    series_rows: {series_id: parse_fred_json 行}；某档当日无值记 None，
    不填 0、不插值。日期取六档并集，按日期排序。
    """
    short = FRED_CREDIT_SHORT_KEYS
    by_series = {
        series_id: {r["date"]: r["value"] for r in rows}
        for series_id, rows in (series_rows or {}).items()
    }
    dates = set()
    for mapping in by_series.values():
        dates.update(mapping)
    panel = []
    for date in sorted(dates):
        row = {"date": date}
        for series_id, key in short.items():
            row[key] = by_series.get(series_id, {}).get(date)
        vb = row["b"]
        vc = row["ccc"]
        row["ccc_minus_b"] = (vc - vb) if (vb is not None and vc is not None) else None
        panel.append(row)
    return panel


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


# ------------------------------------------------- ETF 流量：离线解析与自算 ---
# 以下函数均为纯函数（不联网），tests/test_fetch.py 用 fixture 离线测试。
# 任何一处结构对不上就抛 SchemaError，调用方按标的记 failed，绝不猜数。


def parse_tickerinside_series(obj):
    """解析 TickerInside series.json。

    结构：{date, dates[20], columns: ["close","flow_usd","growth_pct",
    "flow_pct_of_assets","previous_close"], series: {TICKER: [[date, flow_usd,
    growth_pct, flow_pct_of_assets, previous_close], ...]}}。
    只取 TICKERINSIDE_TICKERS 的 7 只；flow_usd 为 null 的日原样保留
    （None = 未测得），绝不填 0、不插值。
    某只标的在 series 里完全缺失 -> SchemaError（整路失败，调用方切自算备用）。
    SOXX 2026-11-11 锚点行：在 20 日窗口内时必须存在（缺失 -> SchemaError）；
    锚点值与文档值偏差 >20% 只记警告；窗口已滑过 11-11 时跳过核对并记注。
    返回 {"tickers": {T: [row...]（按日期升序）}, "latest_date": ...,
    "warnings": [...]}；row = {"date","flow_usd","flow_pct_of_assets"}。
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("series"), dict):
        raise SchemaError("TickerInside series.json 缺少 series 字段")
    columns = obj.get("columns")
    if not isinstance(columns, list) or "flow_usd" not in columns:
        raise SchemaError("TickerInside series.json 缺少 columns/flow_usd 列")
    flow_idx = columns.index("flow_usd")
    pct_idx = columns.index("flow_pct_of_assets") if "flow_pct_of_assets" in columns else None
    tickers = {}
    for ticker in TICKERINSIDE_TICKERS:
        raw_rows = obj["series"].get(ticker)
        if raw_rows is None:
            raise SchemaError(
                f"TickerInside series.json 缺少 {ticker}（该标的完全缺失，整路失败）"
            )
        if not isinstance(raw_rows, list) or not raw_rows:
            raise SchemaError(f"TickerInside series.json 的 {ticker} 行为空")
        rows = []
        for raw in raw_rows:
            if not isinstance(raw, (list, tuple)) or len(raw) <= flow_idx:
                raise SchemaError(f"TickerInside {ticker} 行结构不符：{str(raw)[:200]}")
            date = str(raw[0])[:10]
            if not _DATE_RE.match(date):
                raise SchemaError(f"TickerInside {ticker} 行日期不合法：{str(raw[0])[:50]}")
            flow = raw[flow_idx]
            if flow is not None:
                flow = parse_number(flow)
                if flow is None:
                    raise SchemaError(f"TickerInside {ticker} {date} 行 flow_usd 无法解析：{raw[flow_idx]!r}")
            pct = None
            if pct_idx is not None and len(raw) > pct_idx and raw[pct_idx] is not None:
                pct = parse_number(raw[pct_idx])
            rows.append({"date": date, "flow_usd": flow, "flow_pct_of_assets": pct})
        rows.sort(key=lambda r: r["date"])
        tickers[ticker] = rows
    all_dates = [r["date"] for rows in tickers.values() for r in rows]
    warnings = []
    anchor = TICKERINSIDE_ANCHOR
    soxx_rows = {r["date"]: r for r in tickers[anchor["ticker"]]}
    if anchor["date"] in soxx_rows:
        anchor_flow = soxx_rows[anchor["date"]]["flow_usd"]
        if anchor_flow is None:
            warnings.append(
                f"锚点 {anchor['ticker']} {anchor['date']} 行 flow_usd 为 null（未测得），"
                "无法核对锚点值。"
            )
        elif abs(anchor_flow - anchor["flow_usd"]) / anchor["flow_usd"] > anchor["tolerance"]:
            warnings.append(
                f"锚点 {anchor['ticker']} {anchor['date']} flow_usd 为 "
                f"{anchor_flow:,.0f} 美元，与文档值 {anchor['flow_usd']:,.0f} 美元"
                f"偏差超 {anchor['tolerance']:.0%}，只记警告（不阻塞整源）。"
            )
    elif all_dates and min(all_dates) <= anchor["date"] <= max(all_dates):
        raise SchemaError(
            f"锚点 {anchor['ticker']} {anchor['date']} 行缺失（该日在窗口内却无行）"
        )
    else:
        warnings.append(
            f"锚点 {anchor['date']} 已滑出 20 日窗口（窗口 {min(all_dates)}–{max(all_dates)}），"
            "跳过锚点核对。"
        )
    return {"tickers": tickers, "latest_date": max(all_dates), "warnings": warnings}


def parse_blackrock_holdings_csv(text):
    """解析 BlackRock v1 holdings CSV（text/csv）。

    前 8 行元数据取 Shares Outstanding 与截至日（"Fund Holdings as of,
    Nov 24, 2026" 格式）；持仓表 Market Value 列求和，净值 = Σ市值 / 份额。
    截至日逐请求摇摆：同一 URL 有时返回 ISO 格式 "2026-11-24"，故先试
    "%b %d, %Y"，失败再试 "%Y-%m-%d"，两种都失败才报错。
    债券版列名无 Ticker 列，不依赖持仓行结构，只依赖前 8 行 + Market Value 列。
    字段缺失 / 无持仓行 -> SchemaError。
    返回 {"date","shares","nav","total_market_value_usd"}。
    """
    import csv
    import io

    rows = [row for row in csv.reader(io.StringIO(text), skipinitialspace=True)]
    shares = None
    asof_raw = None
    mv_idx = None
    header_idx = None
    for idx, row in enumerate(rows):
        if not row:
            continue
        first = row[0].strip()
        if first == "Shares Outstanding" and len(row) > 1:
            shares = parse_number(row[1])
        elif first == "Fund Holdings as of" and len(row) > 1:
            asof_raw = row[1].strip().strip('"')
        if mv_idx is None:
            cells = [c.strip() for c in row]
            if "Market Value" in cells:
                mv_idx = cells.index("Market Value")
                header_idx = idx
    if shares is None or shares <= 0:
        raise SchemaError("BlackRock CSV 缺少 Shares Outstanding（或份额 ≤0）")
    if not asof_raw:
        raise SchemaError("BlackRock CSV 缺少 Fund Holdings as of 截至日")
    asof = None
    for fmt in ("%b %d, %Y", "%Y-%m-%d"):
        try:
            asof = datetime.strptime(asof_raw, fmt).strftime("%Y-%m-%d")
            break
        except ValueError:
            continue
    if asof is None:
        raise SchemaError(
            "BlackRock CSV 截至日无法解析："
            f"{asof_raw!r}（已试 %b %d, %Y 与 %Y-%m-%d 两种格式）"
        )
    if mv_idx is None:
        raise SchemaError("BlackRock CSV 未找到 Market Value 列")
    total_mv = 0.0
    holdings_seen = 0
    for row in rows[header_idx + 1:]:
        if not row or len(row) <= mv_idx:
            continue
        if row[0].strip().lower().startswith("total"):
            continue  # 合计行不重复计入
        value = parse_number(row[mv_idx])
        if value is None:
            continue
        total_mv += value
        holdings_seen += 1
    if holdings_seen == 0 or total_mv <= 0:
        raise SchemaError("BlackRock CSV 持仓表无有效 Market Value 行")
    return {
        "date": asof,
        "shares": shares,
        "nav": total_mv / shares,
        "total_market_value_usd": total_mv,
    }


_US_NUMBER_RE = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")


def _parse_us_number(raw):
    """按美国格式解析数字：千分位逗号 + 点小数；欧洲格式（如 361,80、
    逗号作小数点）直接返回 None，不猜。"""
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace("\u00a0", "").strip()
    if not _US_NUMBER_RE.match(text):
        return None
    return parse_number(text)


def _parse_ssga_date(raw):
    """解析 SSGA 日期文本（"Nov 24 2026" / "24-Nov-2026" / "as of Nov 24 2026"）。
    失败返回 None，不抛错（调用方决定是否回退）。"""
    if not raw:
        return None
    text = str(raw).strip()
    text = re.sub(r"^as of\s+", "", text, flags=re.IGNORECASE)
    text = text.replace(",", "").replace(".", "")
    for fmt in ("%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_ssga_quick_info(html_text):
    """从 <input id="fund-quick-info"> 的转义 JSON 取 NAV/总资产/截至日。

    真实页面结构（2026-11-25 实测 GLD/SPY/XLU 三页，均为 HTTP 200 原始 HTML）：
    文本里根本没有 "Fund Net Asset Value as of <日期>" 这句话，NAV/Shares 表
    也只以 JSON 转义串嵌在别的 blob 里——首跑 7 只全失败就是因为解析器按手写
    夹具的表格式去找，真实页面里根本没有那张表（Farside 同款教训）。
    数据在 fund-quick-info 的 value 里：attrs.nav.originalValue 为精确 NAV，
    attrs.aum.originalValue 为精确总资产（美元），截至日取
    attrs.nav.asOfDateSimple（"Nov 24 2026"）或 attrs.nav-date.value
    （"24-Nov-2026"）；顶层 asOfDate 是页面日期（如 Nov 25），不是 NAV 截至日，
    绝不能用。份额无单独字段：shares = 总资产 / NAV（定义上精确；
    GLD 360.40M、SPY 1071.28M、XLU 533.50M 三页反推值与已核实数一致）。
    """
    tag_pos = html_text.find('id="fund-quick-info"')
    if tag_pos < 0:
        raise SchemaError("SSGA 基金页未找到 fund-quick-info 数据块")
    input_start = html_text.rfind("<input", 0, tag_pos)
    if input_start < 0:
        raise SchemaError("SSGA 基金页 fund-quick-info 不在 input 标签内")
    tag_end = html_text.find('"/>', tag_pos)
    if tag_end < 0:
        raise SchemaError("SSGA 基金页 fund-quick-info 标签未闭合")
    value_start = html_text.find('value="', input_start, tag_end)
    if value_start < 0:
        raise SchemaError("SSGA 基金页 fund-quick-info 缺少 value")
    try:
        data = json.loads(html.unescape(html_text[value_start + 7:tag_end]))
    except ValueError as exc:
        raise SchemaError(f"SSGA 基金页 fund-quick-info JSON 无法解析：{exc}")
    attrs = data.get("attrs") or {}
    nav_attr = attrs.get("nav") or {}
    aum_attr = attrs.get("aum") or {}
    nav = None
    nav_precise = False
    raw_precise = nav_attr.get("originalValue")
    if raw_precise is not None:
        nav = parse_number(str(raw_precise))
        if nav is not None:
            nav_precise = True
    if nav is None:
        nav = _parse_us_number(nav_attr.get("value"))
    if nav is None or nav <= 0:
        raise SchemaError("SSGA 基金页 fund-quick-info 缺少 NAV")
    aum = None
    raw_aum = aum_attr.get("originalValue")
    if raw_aum is not None:
        aum = parse_number(str(raw_aum))
    if aum is None or aum <= 0:
        raise SchemaError("SSGA 基金页 fund-quick-info 缺少总资产（无法反推份额）")
    asof = None
    for candidate in (
        nav_attr.get("asOfDateSimple"),
        (attrs.get("nav-date") or {}).get("value"),
        nav_attr.get("asOfDate"),
    ):
        asof = _parse_ssga_date(candidate)
        if asof:
            break
    if asof is None:
        raise SchemaError("SSGA 基金页 fund-quick-info 缺少 NAV 截至日")
    return {"date": asof, "shares": aum / nav, "nav": nav, "nav_precise": nav_precise}


def _parse_ssga_table(html_text):
    """旧表格式解析（手写夹具走这条路；真实页面无此表，仅作回退）。"""
    date_match = re.search(
        r"Fund Net Asset Value as of\s+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})",
        html_text,
    )
    if not date_match:
        raise SchemaError("SSGA 基金页未找到 Fund Net Asset Value as of 截至日")
    asof = _parse_ssga_date(date_match.group(1))
    if asof is None:
        raise SchemaError(f"SSGA 基金页截至日无法解析：{date_match.group(1)!r}")
    parser = _TableParser()
    parser.feed(html_text)
    nav_value = None
    shares_value = None
    for table in parser.tables:
        for row in table:
            if len(row) < 2:
                continue
            label = row[0].strip()
            value = row[1].strip()
            # 表头 NAV 格含说明文字（"NAV The market value of a mutual fund's…"），
            # 但 NAV Per Basket 是另一行，须排除。
            if nav_value is None and (
                label == "NAV"
                or (label.startswith("NAV") and "market value" in label.lower()
                    and not label.startswith("NAV Per"))
            ):
                nav_value = _parse_us_number(value)
                if nav_value is None:
                    raise SchemaError(f"SSGA 基金页 NAV 格式不符（疑似欧洲格式）：{value!r}")
            if shares_value is None and label.startswith("Shares Outstanding"):
                amount = value[:-1].strip() if value.endswith("M") else value
                number = _parse_us_number(amount)
                if number is None:
                    raise SchemaError(
                        f"SSGA 基金页 Shares Outstanding 格式不符（疑似欧洲格式）：{value!r}"
                    )
                if not value.endswith("M"):
                    raise SchemaError(
                        f"SSGA 基金页 Shares Outstanding 缺少 M（百万股）单位：{value!r}"
                    )
                shares_value = number * 1_000_000
    if nav_value is None:
        raise SchemaError("SSGA 基金页未找到 NAV 行")
    if shares_value is None or shares_value <= 0:
        raise SchemaError("SSGA 基金页未找到 Shares Outstanding 行")
    nav = nav_value
    nav_precise = False
    for match in re.finditer(r'"nav"\s*:\s*"?(\d+\.\d+)"?', html_text, re.IGNORECASE):
        candidate = parse_number(match.group(1))
        if candidate is None:
            continue
        if abs(candidate - nav_value) / nav_value <= 0.01 and len(match.group(1).split(".")[1]) > len(
            str(nav_value).split(".")[1] if "." in str(nav_value) else ""
        ):
            nav = candidate
            nav_precise = True
    return {"date": asof, "shares": shares_value, "nav": nav, "nav_precise": nav_precise}


def parse_ssga_fund_page(html_text):
    """解析 SSGA 美国版基金页 HTML，返回 {"date","shares","nav","nav_precise"}。

    优先走 fund-quick-info JSON（真实页面结构，2026-11-25 实测 GLD/SPY/XLU）；
    找不到时回退旧表格式（手写夹具）。两路都无数据才 SchemaError。
    截至日取页面实际携带的 NAV 截至日（通常为 T+1，即前一日收盘），
    绝不硬找当日日期；页面确实没有任何可用净值时才记 failed。
    """
    errors = []
    try:
        return _parse_ssga_quick_info(html_text)
    except SchemaError as exc:
        errors.append(str(exc))
    try:
        return _parse_ssga_table(html_text)
    except SchemaError as exc:
        errors.append(str(exc))
    raise SchemaError("SSGA 基金页未找到可用净值（" + "；".join(errors) + "）")


_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XLSX_RELS_PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _xlsx_sheet_xml(xlsx_bytes):
    """取 XLSX 第一个工作表的 XML 与共享字符串表（只用标准库 zipfile + ElementTree）。

    返回 (sheet_root, shared_strings)。不是合法 zip/xlsx -> SchemaError。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    except zipfile.BadZipFile as exc:
        raise SchemaError(f"VanEck 返回不是合法 XLSX（zip 打不开）: {exc}")
    try:
        names = zf.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sst.findall(f"{{{_XLSX_NS}}}si"):
                shared.append(
                    "".join(t.text or "" for t in si.iter(f"{{{_XLSX_NS}}}t"))
                )
        sheet_path = None
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rel_map = {
                rel.get("Id"): rel.get("Target")
                for rel in rels.findall(f"{{{_XLSX_RELS_PKG_NS}}}Relationship")
            }
            for sheet in wb.iter(f"{{{_XLSX_NS}}}sheet"):
                target = rel_map.get(sheet.get(f"{{{_XLSX_REL_NS}}}id"))
                if target:
                    sheet_path = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
                    break
        if sheet_path is None or sheet_path not in names:
            candidates = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
            if not candidates:
                raise SchemaError("VanEck XLSX 里没有工作表（xl/worksheets/ 为空）。")
            sheet_path = candidates[0]
        return ET.fromstring(zf.read(sheet_path)), shared
    except SchemaError:
        raise
    except Exception as exc:
        raise SchemaError(f"VanEck XLSX 结构解析失败: {exc}")


def _xlsx_cell_value(cell, shared):
    """读一个 <c> 单元格的值（字符串形式）；空单元格返回 None。"""
    ctype = cell.get("t")
    if ctype == "s":
        v = cell.find(f"{{{_XLSX_NS}}}v")
        if v is None or v.text is None:
            return None
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return None
    if ctype == "inlineStr":
        is_el = cell.find(f"{{{_XLSX_NS}}}is")
        if is_el is None:
            return None
        return "".join(x.text or "" for x in is_el.iter(f"{{{_XLSX_NS}}}t"))
    v = cell.find(f"{{{_XLSX_NS}}}v")
    if v is None or v.text is None:
        return None
    return v.text


def _parse_vaneck_date(raw):
    """解析 VanEck 日期（"11/24/2026" 或 ISO）；失败返回 None。"""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_vaneck_nav_history(xlsx_bytes):
    """解析 VanEck NAV 历史 XLSX，返回最新交易日的 {"date","nav","aum_usd","shares"}。

    列：Date / NAV / AUM（表头行按名字找，不按固定行号；数值在文件里是
    带逗号的字符串，如 '57,053,492,930.04'，按美国格式解析）。
    份额 = 总资产 / NAV（NAV 定义即总资产 / 份额，定义上精确）。
    周末行（与周五同一组数）在文件里存在：只取周一至周五的行，
    避免周日快照算出 0 流量行；无工作日行时回退到最新行。
    任一字段缺失 / 无可用数据行 -> SchemaError（该标的 failed，不编数）。"""
    sheet_root, shared = _xlsx_sheet_xml(xlsx_bytes)
    rows = []
    for row_el in sheet_root.iter(f"{{{_XLSX_NS}}}row"):
        cells = {}
        for cell in row_el.findall(f"{{{_XLSX_NS}}}c"):
            ref = cell.get("r") or ""
            col = re.match(r"([A-Z]+)", ref)
            if not col:
                continue
            val = _xlsx_cell_value(cell, shared)
            if val is not None:
                cells[col.group(1)] = val
        if cells:
            rows.append(cells)
    header_idx = None
    colmap = None
    for i, cells in enumerate(rows):
        inv = {str(v).strip().lower(): k for k, v in cells.items()}
        if "date" in inv and "nav" in inv and "aum" in inv:
            header_idx = i
            colmap = {"date": inv["date"], "nav": inv["nav"], "aum": inv["aum"]}
            break
    if header_idx is None:
        raise SchemaError("VanEck NAV 历史 XLSX 找不到 Date/NAV/AUM 表头（文件格式变了）。")
    valid = []
    for cells in rows[header_idx + 1:]:
        date_iso = _parse_vaneck_date(cells.get(colmap["date"]))
        nav = _parse_us_number(cells.get(colmap["nav"]))
        aum = _parse_us_number(cells.get(colmap["aum"]))
        if date_iso is None or nav is None or aum is None:
            continue
        if nav <= 0 or aum <= 0:
            continue
        valid.append((date_iso, nav, aum))
    if not valid:
        raise SchemaError("VanEck NAV 历史 XLSX 无可用数据行（Date/NAV/AUM 均缺失）。")
    weekday = [r for r in valid if datetime.strptime(r[0], "%Y-%m-%d").weekday() < 5]
    pool = weekday or valid
    date_iso, nav, aum = max(pool, key=lambda r: r[0])
    return {"date": date_iso, "nav": nav, "aum_usd": aum, "shares": aum / nav}


_SINA_GB_LINE_RE = re.compile(r'^var hq_str_gb_([a-z0-9]+)="([^"]*)";?\s*$')
_SINA_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_SINA_US_TIME_RE = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+"
    r"(\d{1,2}):(\d{2})(AM|PM)\s+[A-Z]+$"
)


def parse_sina_gb_line(line):
    """解析新浪 gb_ 行：var hq_str_gb_qqq="..."; 返回 dict。

    字段（从 0 数）：1=最新价，12=总市值，19=份额数（总股本），25=美东时间，
    26=前收。校验：价格>0、份额>0、总市值>0、|价格×份额 − 总市值| / 总市值
    < 0.5%，否则 SchemaError。GBK 解码在调用方做（errors="replace"），
    名字字段（GBK 中文）不解析，只取 ASCII 数字字段。
    返回：{"ticker","price","shares","mktcap","us_time","prev_close"}。
    """
    match = _SINA_GB_LINE_RE.match(line.strip())
    if not match:
        raise SchemaError(f"新浪 gb_ 行格式不对：{line[:80]!r}")
    symbol, body = match.group(1), match.group(2)
    fields = body.split(",")
    if len(fields) < 27:
        raise SchemaError(
            f"新浪 gb_{symbol} 字段不足（{len(fields)} 个，至少 27 个）。"
        )
    try:
        price = float(fields[1])
        mktcap = float(fields[12])
        shares = float(fields[19])
        prev_close = float(fields[26])
    except ValueError:
        raise SchemaError(f"新浪 gb_{symbol} 价格/市值/份额/前收不是数字。")
    us_time = fields[25].strip()
    if not us_time:
        raise SchemaError(f"新浪 gb_{symbol} 第 25 字段（美东时间）为空。")
    if not (price > 0 and shares > 0 and mktcap > 0):
        raise SchemaError(
            f"新浪 gb_{symbol} 价格/市值/份额须 >0（价 {price}、"
            f"份额 {shares}、市值 {mktcap}）。"
        )
    deviation = abs(price * shares - mktcap) / mktcap
    if deviation >= 0.005:
        raise SchemaError(
            f"新浪 gb_{symbol} 自洽校验失败：价格×份额与总市值偏差"
            f" {deviation:.2%}（≥0.5%），疑似字段错位，不发布。"
        )
    return {
        "ticker": symbol.upper(),
        "price": price,
        "shares": shares,
        "mktcap": mktcap,
        "us_time": us_time,
        "prev_close": prev_close,
    }


def _sina_date_from_us_time(us_time, today):
    """把新浪第 25 字段 "Nov 25 4:00PM EST" 转成 (ISO 日期, 是否收盘后)。

    字段无年份：取与 today（UTC 日期字符串）最近且不晚于 today 的年份
    （跨年边沿：today 为 1 月 1 日、字段为 Dec 31 时取上一年）。
    是否收盘后：字段时间 ≥16:00 ET（此时字段 1 为收盘价，可算流量）。
    格式不对 -> SchemaError。"""
    match = _SINA_US_TIME_RE.match(us_time.strip())
    if not match:
        raise SchemaError(f"新浪美东时间格式不对：{us_time!r}")
    month_name, day, hour, minute, ampm = match.groups()
    hour = int(hour)
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    from datetime import date as _date

    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    parsed = _date(today_date.year, _SINA_MONTHS[month_name], int(day))
    if parsed > today_date:
        parsed = _date(today_date.year - 1, _SINA_MONTHS[month_name], int(day))
    is_close = (hour * 60 + int(minute)) >= 16 * 60
    return parsed.isoformat(), is_close


def _fetch_vaneck_xlsx_bytes(url):
    """取 VanEck 文件（cookie 门）：经 http_request(cookies=True) 持久化 cookie，
    跟随重定向。失败 -> FetchError（调用方记该标的 failed）。"""
    _, body = http_request(url, cookies=True)
    return body


def _fetch_vaneck_snapshot(ticker):
    """取 VanEck NAV 历史 XLSX，返回快照 {"date","shares","nav","source"}。
    任一字段缺失 / 净值越界 -> SchemaError（调用方记该标的 failed）。"""
    body = _fetch_vaneck_xlsx_bytes(VANECK_NAV_HISTORY_URL)
    parsed = parse_vaneck_nav_history(body)
    check_nav_range(ticker, parsed["nav"], VANECK_NAV_RANGES)
    return {
        "date": parsed["date"],
        "shares": parsed["shares"],
        "nav": parsed["nav"],
        "source": "selfcalc-vaneck",
    }


def _now_et():
    """当前美东时间（naive datetime）。ZoneInfo 不可用时退回 UTC-5（EST 口径）；
    三班 cron 距 16:00 边界均 ≥1 小时，1 小时误差不影响 gating 判定。
    测试可 patch 本函数以固定时间。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(timezone.utc).astimezone(
            ZoneInfo("America/New_York")
        ).replace(tzinfo=None)
    except Exception:
        from datetime import timedelta

        return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)


def _fetch_sina_snapshot(ticker, today=None, source="selfcalc-sina"):
    """取新浪 gb_ 行情，返回快照 {"date","shares","nav","source"}。

    source：主路传 "selfcalc-sina"（QQQ/VOO）；交叉核对传 "xcheck-sina"
    （SOXX/TLT/IBIT，只核对不算流量）。
    流量只能用收盘价算：只有跑的当前美东时间 ≥16:00 且第 25 字段时间
    ≥16:00（即字段 1 为收盘价）时才写快照；否则返回 {"skipped": True,
    "note": ...}（调用方不写快照、不算流量，但源仍记 ok）。
    任一字段缺失 / 自洽校验失败 / 净值越界 -> SchemaError（调用方记 failed）。
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    _, body = http_request(SINA_GB_URL, headers={"Referer": SINA_REFERER})
    text = body.decode("gbk", "replace")
    rows = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = parse_sina_gb_line(line)
        except SchemaError:
            if f"gb_{ticker.lower()}" in line:
                raise
            continue
        rows[parsed["ticker"]] = parsed
    if ticker not in rows:
        raise SchemaError(f"新浪行情缺 {ticker} 行（{SINA_GB_URL}）。")
    parsed = rows[ticker]
    date_et, is_close = _sina_date_from_us_time(parsed["us_time"], today)
    now_et = _now_et()
    if now_et.hour * 60 + now_et.minute < 16 * 60:
        return {
            "skipped": True,
            "note": (
                f"新浪盘中跳过快照（当前美东 {now_et.strftime('%H:%M')} 未收盘，"
                "字段 1 不是收盘价，流量只能用收盘价算）。"
            ),
        }
    if not is_close:
        return {
            "skipped": True,
            "note": (
                f"新浪盘中跳过快照（美东时间 {parsed['us_time']} 未收盘，"
                "字段 1 不是收盘价，流量只能用收盘价算）。"
            ),
        }
    check_nav_range(ticker, parsed["price"], SINA_NAV_RANGES)
    return {
        "date": date_et,
        "shares": parsed["shares"],
        "nav": parsed["price"],
        "source": source,
    }


def _sina_xcheck_note(ticker, snap, main_snap):
    """交叉核对 note：新浪快照与同 run 主源快照比对同日份额，只写字、不改数。

    main_snap 为 None（主源快照缺失）或日期不一致 -> 未比对；
    同日份额偏差 >1% -> 标出差异、以主源为准；≤1% -> 记一致。
    IBIT 无主源份额可比，不走本函数（调用方另写 note）。"""
    if main_snap is None:
        return "新浪交叉核对：主源快照缺失，未比对；数未改。"
    if snap["date"] != main_snap.get("date"):
        return (
            f"新浪交叉核对：快照日期与主源不一致（新浪 {snap['date']}，"
            f"主源 {main_snap.get('date')}），未比对；数未改。"
        )
    main_shares = main_snap["shares"]
    deviation = abs(snap["shares"] - main_shares) / main_shares
    if deviation > 0.01:
        return (
            f"新浪交叉核对：同日份额与主源差 {deviation:.2%}（>1%），"
            "数未改，以主源为准。"
        )
    return f"新浪交叉核对：同日份额与主源一致（差 {deviation:.2%}）。"


def check_nav_range(ticker, nav, ranges):
    """净值合理性检查：超出区间 -> SchemaError（该标的当日 failed，不发布离谱数）。"""
    lo, hi = ranges[ticker]
    if nav is None or not (lo <= nav <= hi):
        raise SchemaError(
            f"{ticker} 净值 ${nav} 超出合理区间 ${lo:g}–${hi:g}"
            "（按 2026-11-25 市价定的宽带），该标的当日 failed。"
        )


_SPLIT_MULTIPLES = (2.0, 3.0, 4.0, 0.5, 1.0 / 3.0, 0.25)


def detect_split(prev_shares, shares, prev_nav, nav):
    """拆股判定：份额按干净倍数跳变（2/3/4 倍或 1/2、1/3、1/4，误差 ≤1%）
    且净值按反比跳变（误差 ≤5%）。返回倍数 m（今日份额 ≈ m × 昨日份额），否则 None。"""
    if not prev_shares or not shares or not prev_nav or not nav:
        return None
    ratio = shares / prev_shares
    for multiple in _SPLIT_MULTIPLES:
        if abs(ratio - multiple) / multiple <= 0.01:
            inverse = 1.0 / multiple
            if abs(nav / prev_nav - inverse) / inverse <= 0.05:
                return multiple
    return None


def compute_selfcalc_flow(today, prev):
    """自算流量：当日流量（美元）= 当日 NAV ×（当日份额 − 前一日份额）。

    today/prev: {"date","shares","nav"}。返回 (flow_usd_or_None, note)。
    首日（无昨日快照）或快照日期未前进 -> 流量 None，绝不写 0。
    大额申赎（单日份额变化 >5% 但不符合干净倍数）照常算流量，不当异常丢掉。
    """
    if prev is None:
        return None, "首日快照：无昨日份额，流量记 null（向前累积，不补历史）。"
    if today["date"] <= prev["date"]:
        return None, (
            f"快照日期未前进（今日 {today['date']} ≤ 上日 {prev['date']}），"
            "沿用上一日流量，不重复算。"
        )
    multiple = detect_split(prev["shares"], today["shares"], prev["nav"], today["nav"])
    if multiple is not None:
        adjusted_prev = prev["shares"] * multiple
        flow = today["nav"] * (today["shares"] - adjusted_prev)
        return flow, (
            f"拆股：份额按 {multiple:g} 倍跳变且净值按反比跳变，"
            "拆股前份额已按倍数折算后再算流量。"
        )
    return today["nav"] * (today["shares"] - prev["shares"]), ""


def trading_days_between(date1, date2):
    """date1 < date2 时两者之间（不含 date1、含 date2）的周一至周五天数。
    节假日未扣除，是近似口径（只用于滞后 >3 个交易日的切换判断）。"""
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    start = _date.fromisoformat(date1)
    end = _date.fromisoformat(date2)
    if end <= start:
        return 0
    count = 0
    current = start + _timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += _timedelta(days=1)
    return count


def merge_daily_series(prev_daily, new_rows, limit=ETF_DAILY_KEEP_DAYS):
    """daily 序列滚动合并去重：按日期合并（新行覆盖旧行），按日期排序，
    只保留最近 limit 个收盘日。行格式 {"date","flow_usd_100m","source"}；
    flow 为 None 的行原样保留（未测得），不填 0。"""
    merged = {}
    for row in list(prev_daily or []) + list(new_rows or []):
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or "")
        if not _DATE_RE.match(date):
            continue
        merged[date] = {
            "date": date,
            "flow_usd_100m": row.get("flow_usd_100m"),
            "source": row.get("source"),
        }
    ordered = [merged[date] for date in sorted(merged)]
    return ordered[-limit:] if limit else ordered


def _usd_to_100m(usd):
    """美元 -> 亿美元（1 单位 = 1 亿美元）；None 保持 None，不编 0。"""
    return None if usd is None else round(usd / 1e8, 4)


def _etf_entry(ticker, date_et, flow_100m, aum_100m, pct_of_aum, source,
              as_of_date, status, daily, note, alternate_flow=None):
    """组装一只标的的 tracked entry（schema_version 1 口径）。
    flag_over_5pct 由本函数按 |pct_of_aum| > 5 算出；pct 为 null 时 flag 为
    false 并在 note 里注明。"""
    entry = {
        "ticker": ticker,
        "date_et": date_et,
        "daily_flow_usd_100m": flow_100m,
        "aum_usd_100m": aum_100m,
        "pct_of_aum": pct_of_aum,
        "flag_over_5pct": bool(pct_of_aum is not None and abs(pct_of_aum) > 5),
        "source": source,
        "as_of_date": as_of_date,
        "status": status,
        "daily": daily or [],
        "note": note or "",
    }
    if alternate_flow is not None:
        entry["alternate_flow"] = alternate_flow
    if pct_of_aum is None:
        entry["note"] = (entry["note"] + "；" if entry["note"] else "") + (
            "pct 为 null，flag_over_5pct 记 false。"
        )
    return entry


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


def _fred_series_url(series_id, key, observation_start=None):
    """拼 FRED observations URL（与 DGS10 同款路径；key 只放进请求 URL，绝不进输出文件）。"""
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={key}&file_type=json"
    )
    if observation_start:
        url += f"&observation_start={observation_start}"
    return url


def fetch_fred_credit():
    """FRED 信用利差（六档 ICE BofA OAS，日频，T+1）。

    六档逐一取数：任一档取不到（联网失败 / 结构不符 / 全是 null），
    整个源失败并在错误里写明是哪一档，不用 0 或插值填充、不出半份文件。
    最新值截至日取 FRED 返回的日期，绝不写当天日期。
    防 key 泄漏：get_json 的报错里会带完整请求 URL（含 key），此处捕获后
    一律把 key 替换成 <redacted> 再抛出，错误进 status.json 也不带 key。
    """
    key = os.environ.get(FRED_ENV_KEY)
    if not key:
        raise FetchError(f"缺少 Secret {FRED_ENV_KEY}（未配置，无法调用 FRED API）")
    start = (datetime.now(timezone.utc).date() - timedelta(days=FRED_CREDIT_WINDOW_DAYS)).isoformat()
    series_rows = {}
    for series_id in FRED_CREDIT_SERIES:
        url = _fred_series_url(series_id, key, observation_start=start)
        try:
            obj = get_json(url)
            rows = parse_fred_json(obj)
        except (FetchError, SchemaError) as exc:
            safe = str(exc).replace(key, "<redacted>")
            raise FetchError(f"FRED 信用利差 {series_id} 取数失败：{safe}")
        latest_date, latest_value = _latest_fred_value(rows)
        if latest_value is None:
            raise FetchError(f"FRED 信用利差 {series_id} 无有效值（返回行全部为 null）")
        series_rows[series_id] = rows
    panel = build_fred_credit_rows(series_rows)
    gap_rows = compute_ccc_minus_b(
        series_rows[FRED_CREDIT_B_SERIES], series_rows[FRED_CREDIT_CCC_SERIES]
    )
    gap_date, gap_value = _latest_fred_value(
        [{"date": r["date"], "value": r["gap"]} for r in gap_rows]
    )
    series_meta = {}
    for series_id, label in FRED_CREDIT_SERIES.items():
        latest_date, latest_value = _latest_fred_value(series_rows[series_id])
        series_meta[series_id] = {
            "label": label,
            "unit": "percent",
            "latest_value": latest_value,
            "latest_date": latest_date,
        }
    as_of = max(meta["latest_date"] for meta in series_meta.values())
    return _payload(
        "https://api.stlouisfed.org/fred/series/observations?file_type=json"
        "（6 档 ICE BofA OAS，api_key 已脱敏）",
        panel,
        extra={
            "series": series_meta,
            "derived": {
                "ccc_minus_b": {
                    "label": "CCC−B 缺口",
                    "formula": "BAMLH0A3HYC − BAMLH0A2HYB",
                    "unit": "percent",
                    "latest_value": gap_value,
                    "latest_date": gap_date,
                }
            },
            "as_of": as_of,
            "unit": "percent",
            "notes": [
                "FRED 数据 T+1：当日值次日早上才出；最新值截至日为 FRED 返回的日期，不是今天。",
                "序列窗口为最近 3 年（observation_start 动态算），供面板画走势。",
                "value 为 null 的日期表示当日无值，已记为 null，未编 0、未插值。",
                "CCC−B 缺口 = CCC 及以下 OAS − 单 B 级 OAS；当日任一档无值则缺口为 null。",
            ],
        },
    )


def fetch_farside(kind):
    """Farside BTC/ETH ETF 流量表。kind 为 'btc' 或 'eth'。"""
    url = FARSIDE_URLS[kind]
    _, body = http_request(url)
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
            _, body = http_request(url)
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


def _fetch_blackrock_snapshot(ticker):
    """取 BlackRock v1 holdings CSV，返回快照 {"date","shares","nav","source"}。
    任一字段缺失 / 净值越界 -> SchemaError（调用方记该标的 failed）。"""
    portfolio_id = BLACKROCK_PORTFOLIO_IDS[ticker]
    url = BLACKROCK_HOLDINGS_URL_TEMPLATE.replace("{portfolio_id}", str(portfolio_id))
    _, body = http_request(url)
    parsed = parse_blackrock_holdings_csv(body.decode("utf-8", "replace"))
    check_nav_range(ticker, parsed["nav"], BLACKROCK_NAV_RANGES)
    return {
        "date": parsed["date"],
        "shares": parsed["shares"],
        "nav": parsed["nav"],
        "source": "selfcalc-blackrock",
    }


def _fetch_ssga_snapshot(ticker):
    """取 SSGA 美国版基金页，返回快照 {"date","shares","nav","source"}。
    任一字段缺失 / 净值越界 -> SchemaError（调用方记该标的 failed）。"""
    url = SSGA_FUND_PAGES[ticker]
    _, body = http_request(url)
    parsed = parse_ssga_fund_page(body.decode("utf-8", "replace"))
    check_nav_range(ticker, parsed["nav"], SSGA_NAV_RANGES)
    return {
        "date": parsed["date"],
        "shares": parsed["shares"],
        "nav": parsed["nav"],
        "source": "selfcalc-ssga",
    }


def _load_selfcalc_snapshots(out_dir):
    """读快照文件 data/etf-selfcalc-snapshots.json；不存在/损坏返回 {}。"""
    if not out_dir:
        return {}
    return _load_json_file(os.path.join(out_dir, ETF_SELFCALC_SNAPSHOTS_FILENAME))


def _save_selfcalc_snapshots(out_dir, snapshots):
    """写快照文件（只存衍生数：日期、份额、净值、来源；绝不存发行商持仓文件原文）。"""
    if not out_dir:
        return
    _write_json_file(
        os.path.join(out_dir, ETF_SELFCALC_SNAPSHOTS_FILENAME), snapshots
    )


def _weekly_from_ici(out_dir, prev_payload):
    """weekly 口径：从同 run 的 ici.json 取最新 etf 报告 Total（无 etf 则用
    combined），百万美元→亿美元（/10）；ici.json 不存在时沿用上一次
    etf-flows.json 的 weekly；再没有时用种子值 4571.0 / 2026-11-18 并标 source "seed"。"""
    ici = _load_json_file(os.path.join(out_dir, "ici.json")) if out_dir else {}
    for report in ("etf", "combined"):
        rows = [
            row for row in ici.get("rows") or []
            if isinstance(row, dict) and row.get("report") == report
            and row.get("total") is not None
        ]
        if rows:
            latest = max(rows, key=lambda r: r["date"])
            return {
                "coverage_week_end_et": latest["date"],
                "equity_etf_usd_100m": round(latest["total"] / 10.0, 1),
                "source": "ici",
                "note": f"ICI {report} 报告最新周 Total（百万美元→亿美元）。",
            }
    prev_weekly = (prev_payload or {}).get("weekly")
    if isinstance(prev_weekly, dict) and prev_weekly.get("equity_etf_usd_100m") is not None:
        copied = dict(prev_weekly)
        copied["source"] = "previous"
        return copied
    return {
        "coverage_week_end_et": "2026-11-18",
        "equity_etf_usd_100m": 4571.0,
        "source": "seed",
        "note": "11 月 18 日当周美股基金 +$457.1 亿，为 9 月 23 日以来最大（种子值，暂无 live 路）。",
    }


def _is_fresh(date_et, today):
    """ headline 日期距今天 ≤2 个交易日即新鲜（live）；否则 stale。"""
    if not date_et:
        return False
    return trading_days_between(date_et, today) <= 2


def fetch_etf_flows(out_dir=None, today=None):
    """ETF 流量：TickerInside 7 只 + 发行商文件自算 + 新浪 QQQ/VOO 份额
    + 新浪 SOXX/TLT/IBIT 份额交叉核对 + Farside IBIT。

    合并规则（方案 §五 + 任务书 §3）：
    - 每只标的只有一个主路：7 只以 TickerInside 为准，滞后 >3 个交易日自动切
      自算备用并标注来源切换（本次 TickerInside 最新 11-11，已滞后约 10 个
      交易日，切换立即生效）；TLT、GLD、SMH、QQQ、VOO 以自算为准（QQQ/VOO
      用新浪 gb_ 行情份额，收盘后才写快照，盘中跳过）；IBIT 读同 run 的
      data/farside-btc.json。
    - 冲突：同一标的两路同日都有数且差值 >10% -> 两路都保留（alternate_flow），
      面板备注“两路不一致”，展示以主路为准；≤10% 只存主路。
    - 任一标的取不到/字段缺失 -> 该标的当日 failed，保留上一日好快照，绝不拿
      旧份额算假流量；≥1 只标的有新鲜 live 数 -> 整源 ok；全部可取数标的都
      失败 -> 抛 FetchError（run_fetch 会保留旧文件、status 标 failed）。
    - 自算快照每天都取（12 只全取，保持备用快照温热，切换当日即有昨日数可算）；
      快照文件 data/etf-selfcalc-snapshots.json 由本函数自己读写。
    - 交叉核对（SOXX/TLT/IBIT 新浪份额）：快照存同一文件、键加 _xcheck 后缀，
      source 记 xcheck-sina；只比对同日份额、偏差 >1% 只写 note，绝不参与
      流量计算、绝不覆盖主源；IBIT 只存快照；取数失败/盘中跳过只记 note。
    out_dir 缺省时用模块级 ETF_FLOWS_OUT_DIR（run_fetch 传入）；today 缺省为
    当天 UTC 日期（测试可传入固定日期以消除日期敏感性）。
    """
    out_dir = out_dir or ETF_FLOWS_OUT_DIR
    today = today or datetime.now(timezone.utc).date().isoformat()
    notes = []
    prev_payload = (
        _load_json_file(os.path.join(out_dir, "etf-flows.json")) if out_dir else {}
    )
    prev_tracked = {
        entry.get("ticker"): entry
        for entry in prev_payload.get("tracked") or []
        if isinstance(entry, dict) and entry.get("ticker")
    }
    snapshots = _load_selfcalc_snapshots(out_dir)
    new_snapshots = dict(snapshots)

    # --- TickerInside 路 ---
    ti = None
    try:
        ti = parse_tickerinside_series(get_json(TICKERINSIDE_SERIES_URL))
        notes.extend(f"TickerInside：{warning}" for warning in ti["warnings"])
    except (FetchError, SchemaError) as exc:
        notes.append(f"TickerInside 取数失败（{exc}），7 只主路标的改走自算备用。")
    ti_latest = ti["latest_date"] if ti else None
    ti_lag = trading_days_between(ti_latest, today) if ti_latest else None
    if ti_lag is not None:
        notes.append(
            f"TickerInside 最新收盘日 {ti_latest}，滞后约 {ti_lag} 个交易日（截至 {today}）。"
        )
        if ti_lag > ETF_LAG_SWITCH_TRADING_DAYS:
            notes.append(
                "滞后超 3 个交易日，7 只主路标的自动切自算备用并标注来源切换。"
            )
    if ti:
        soxx_rows = {row["date"]: row for row in ti["tickers"]["SOXX"]}
        if soxx_rows.get("2026-11-10", {}).get("flow_usd") is None and "2026-11-10" in soxx_rows:
            notes.append(
                "SOXX 2026-11-10 在 TickerInside 为 null（未测得；该日单日 −$41.3 亿"
                "为种子数），面板须用种子数展示该日标红。"
            )

    # --- 自算路：12 只全取快照（主路 TLT/GLD/SMH/QQQ/VOO + 7 只备用），失败只影响该标的 ---
    selfcalc = {}
    for ticker, kind in {**SELFCALC_PRIMARY, **SELFCALC_BACKUP}.items():
        try:
            if kind == "blackrock":
                snap = _fetch_blackrock_snapshot(ticker)
            elif kind == "ssga":
                snap = _fetch_ssga_snapshot(ticker)
            elif kind == "sina":
                snap = _fetch_sina_snapshot(ticker, today=today)
            else:  # vaneck
                snap = _fetch_vaneck_snapshot(ticker)
        except (FetchError, SchemaError) as exc:
            selfcalc[ticker] = {"error": str(exc)}
            continue
        prev_snap = snapshots.get(ticker)
        if isinstance(snap, dict) and snap.get("skipped"):
            # 新浪盘中跳过：不写快照、不算流量；有旧快照则沿用旧快照展示，
            # 无旧快照则 entry 段保留 seed（不记 failed：这不是取数失败）。
            selfcalc[ticker] = {
                "snapshot": prev_snap,
                "flow_usd": None,
                "flow_note": snap["note"],
                "skipped": True,
            }
            continue
        if prev_snap and snap["date"] < prev_snap.get("date", ""):
            # 发行商给了比快照更旧的文件：不回退快照，不算流量。
            selfcalc[ticker] = {
                "snapshot": prev_snap,
                "flow_usd": None,
                "flow_note": (
                    f"发行商文件截至日 {snap['date']} 早于已存快照 "
                    f"{prev_snap.get('date')}，不回退快照、不算流量。"
                ),
            }
            continue
        flow, flow_note = compute_selfcalc_flow(snap, prev_snap)
        selfcalc[ticker] = {"snapshot": snap, "flow_usd": flow, "flow_note": flow_note}
        new_snapshots[ticker] = snap

    # --- 新浪交叉核对快照：SOXX、TLT、IBIT（只核对，绝不参与流量计算）---
    # 铁律：快照存进同一快照文件、键加 _xcheck 后缀（绝不占主源键位，不覆盖
    # BlackRock/Farside 快照）；SOXX/TLT 与同 run 主源快照比对同日份额，
    # 偏差 >1% 只在 note 里标出、不改数；IBIT 无主源份额可比，只存快照。
    # 取数失败 / 盘中跳过只记 note，不记 failed、不阻塞整源、不影响
    # failed_live_capable 计数。
    xcheck = {}
    for ticker in SINA_XCHECK_TICKERS:
        try:
            snap = _fetch_sina_snapshot(ticker, today=today, source="xcheck-sina")
        except (FetchError, SchemaError) as exc:
            xcheck[ticker] = {
                "snapshot": None,
                "note": f"新浪交叉核对取数失败（{exc}），主源数未动。",
            }
            continue
        if isinstance(snap, dict) and snap.get("skipped"):
            xcheck[ticker] = {
                "snapshot": None,
                "note": snap["note"].replace(
                    "新浪盘中跳过快照", "新浪交叉核对盘中跳过快照"
                ),
            }
            continue
        new_snapshots[f"{ticker}_xcheck"] = snap
        if ticker == "IBIT":
            note = (
                f"新浪交叉核对份额快照 {snap['shares']:,.0f}"
                f"（{snap['date']}，仅存快照、未算流量，未动 Farside 数）。"
            )
        else:
            main_snap = (selfcalc.get(ticker) or {}).get("snapshot")
            note = _sina_xcheck_note(ticker, snap, main_snap)
        xcheck[ticker] = {"snapshot": snap, "note": note}
    _save_selfcalc_snapshots(out_dir, new_snapshots)

    def _selfcalc_aum_pct(ticker):
        """自算标的的 AUM（NAV×份额）与 pct（流量/AUM）；无流量时 pct 为 None。"""
        record = selfcalc.get(ticker) or {}
        snap = record.get("snapshot")
        if not snap:
            return None, None
        aum_100m = round(snap["nav"] * snap["shares"] / 1e8, 4)
        flow = record.get("flow_usd")
        pct = None
        if flow is not None and aum_100m:
            pct = round(flow / (aum_100m * 1e8) * 100, 4)
        return aum_100m, pct

    def _retained_entry(ticker, status, extra_note):
        """失败时保留上一日 entry 的数值，只改 status 与 note（不编新数）。

        上一日无任何数据时（首跑、或上日亦无 entry），note 里不许写
        “显示上一日数据”——文件里并没有上一日的数据，面板实际按规则回退
        到种子数；改写“回退种子数（无上一日数据）”。"""
        prev = prev_tracked.get(ticker) or {}
        if not (prev.get("date_et") or prev.get("daily")):
            extra_note = extra_note.replace(
                "显示上一日数据", "回退种子数（无上一日数据）"
            )
        note = (prev.get("note") or "") + ("；" if prev.get("note") else "") + extra_note
        return _etf_entry(
            ticker,
            prev.get("date_et"),
            prev.get("daily_flow_usd_100m"),
            prev.get("aum_usd_100m"),
            prev.get("pct_of_aum"),
            prev.get("source") or "seed",
            prev.get("as_of_date"),
            status,
            prev.get("daily") or [],
            note,
        )

    entries = {}
    failed_live_capable = 0  # 12 只可取数标的（7 只 TI + TLT + GLD + SMH + QQQ + VOO）中当日 failed 的只数

    # --- 7 只 TickerInside 主路标的 ---
    for ticker in TICKERINSIDE_TICKERS:
        prev_entry = prev_tracked.get(ticker) or {}
        prev_daily = prev_entry.get("daily") or []
        ti_rows = []
        if ti:
            ti_rows = [
                {
                    "date": row["date"],
                    "flow_usd_100m": _usd_to_100m(row["flow_usd"]),
                    "source": "tickerinside",
                }
                for row in ti["tickers"][ticker]
            ]
        ti_latest_row = ti["tickers"][ticker][-1] if ti else None
        ti_flow_100m = (
            _usd_to_100m(ti_latest_row["flow_usd"])
            if ti_latest_row and ti_latest_row["flow_usd"] is not None
            else None
        )
        ti_date = ti_latest_row["date"] if ti_latest_row else None
        record = selfcalc.get(ticker) or {}
        snap = record.get("snapshot")
        sc_flow_100m = _usd_to_100m(record.get("flow_usd"))
        sc_date = snap.get("date") if snap else None
        switched = ti is None or (
            ti_lag is not None and ti_lag > ETF_LAG_SWITCH_TRADING_DAYS
        )
        daily = merge_daily_series(prev_daily, ti_rows)
        if snap and sc_flow_100m is not None:
            # 自算行并入 daily：已切换时主路是自算，同日覆盖；未切换时主路是
            # TickerInside，自算同日行不覆盖（另一路只进 alternate_flow）。
            if switched or sc_date not in {row["date"] for row in daily}:
                daily = merge_daily_series(
                    daily,
                    [{"date": sc_date, "flow_usd_100m": sc_flow_100m,
                      "source": snap["source"]}],
                )
        sc_error = record.get("error")

        # 主路选择：切自算后以自算为准；未切时以 TickerInside 为准，
        # 仅当 TickerInside 当日未测得（null）时用自算补。
        primary = None  # (flow_100m, date, source, pct, aum, note)
        if switched:
            if sc_flow_100m is not None:
                aum_100m, pct = _selfcalc_aum_pct(ticker)
                note = "来源切换：TickerInside 滞后超 3 个交易日，改用自算备用。"
                if record.get("flow_note"):
                    note += record["flow_note"]
                primary = (sc_flow_100m, sc_date, snap["source"], pct, aum_100m, note)
            elif sc_error:
                entries[ticker] = _retained_entry(
                    ticker, "failed",
                    f"今日取数失败（自算备用：{sc_error}），显示上一日数据。",
                )
                failed_live_capable += 1
                continue
            elif snap is not None:
                # 自算首日（或快照日期未前进）：无新流量，暂显示 TickerInside 旧数。
                if ti_flow_100m is not None:
                    primary = _ti_headline(
                        ticker, ti_latest_row, ti_flow_100m, notes, snapshot=snap
                    )
                    primary = (
                        primary[0], primary[1], primary[2], primary[3], primary[4],
                        primary[5] + "自算首日无流量，暂显示 TickerInside 旧数。",
                    )
                else:
                    entries[ticker] = _etf_entry(
                        ticker, ti_date, None, None, None, "tickerinside",
                        ti_date, "unmeasured", daily,
                        "TickerInside 当日未测得，且自算首日无流量。",
                    )
                    continue
            else:
                entries[ticker] = _retained_entry(
                    ticker, "failed", "今日取数失败（TickerInside 与自算备用均无数据），显示上一日数据。"
                )
                failed_live_capable += 1
                continue
        else:
            if ti_flow_100m is not None:
                primary = _ti_headline(
                    ticker, ti_latest_row, ti_flow_100m, notes, snapshot=snap
                )
            elif sc_flow_100m is not None:
                aum_100m, pct = _selfcalc_aum_pct(ticker)
                note = "TickerInside 当日未测得，改用自算备用。"
                if record.get("flow_note"):
                    note += record["flow_note"]
                primary = (sc_flow_100m, sc_date, snap["source"], pct, aum_100m, note)
            else:
                entries[ticker] = _etf_entry(
                    ticker, ti_date, None, None, None, "tickerinside",
                    ti_date, "unmeasured", daily, "TickerInside 当日未测得，且自算无流量。",
                )
                continue
        flow_100m, headline_date, source, pct, aum_100m, note = primary
        # 冲突：两路同日都有数且差值 >10% -> 两路都保留，展示以主路为准。
        alternate_flow = None
        if (
            ti_flow_100m is not None and sc_flow_100m is not None
            and ti_date == sc_date and abs(ti_flow_100m) >= 0.1
        ):
            other = sc_flow_100m if source == "tickerinside" else ti_flow_100m
            if abs(other - flow_100m) / abs(flow_100m) > 0.10:
                alternate_flow = other
                note += "两路不一致（同日两路流量差值 >10%），展示以主路为准。"
        status = "live" if _is_fresh(headline_date, today) else "stale"
        entries[ticker] = _etf_entry(
            ticker, headline_date, flow_100m, aum_100m, pct, source,
            headline_date, status, daily, note, alternate_flow=alternate_flow,
        )

    # --- TLT、GLD：自算主路 ---
    for ticker in ("TLT", "GLD"):
        prev_entry = prev_tracked.get(ticker) or {}
        prev_daily = prev_entry.get("daily") or []
        record = selfcalc.get(ticker) or {}
        snap = record.get("snapshot")
        sc_flow_100m = _usd_to_100m(record.get("flow_usd"))
        daily = prev_daily
        if snap and sc_flow_100m is not None:
            daily = merge_daily_series(
                daily,
                [{"date": snap["date"], "flow_usd_100m": sc_flow_100m,
                  "source": snap["source"]}],
            )
        if record.get("error"):
            entries[ticker] = _retained_entry(
                ticker, "failed", f"今日取数失败（自算：{record['error']}），显示上一日数据。"
            )
            failed_live_capable += 1
            continue
        if snap is None:
            entries[ticker] = _retained_entry(
                ticker, "failed", "今日取数失败（自算无快照），显示上一日数据。"
            )
            failed_live_capable += 1
            continue
        aum_100m, pct = _selfcalc_aum_pct(ticker)
        if sc_flow_100m is None:
            entries[ticker] = _etf_entry(
                ticker, snap["date"], None, aum_100m, None, snap["source"],
                snap["date"], "unmeasured", daily,
                record.get("flow_note") or "自算无流量。",
            )
            continue
        status = "live" if _is_fresh(snap["date"], today) else "stale"
        note = record.get("flow_note") or ""
        entries[ticker] = _etf_entry(
            ticker, snap["date"], sc_flow_100m, aum_100m, pct, snap["source"],
            snap["date"], status, daily, note,
        )

    # --- SMH：自算主路（VanEck NAV 历史 XLSX；持仓 XLSX 无基金级份额/净值）---
    ticker = "SMH"
    prev_entry = prev_tracked.get(ticker) or {}
    prev_daily = prev_entry.get("daily") or []
    record = selfcalc.get(ticker) or {}
    snap = record.get("snapshot")
    sc_flow_100m = _usd_to_100m(record.get("flow_usd"))
    daily = prev_daily
    if snap and sc_flow_100m is not None:
        daily = merge_daily_series(
            daily,
            [{"date": snap["date"], "flow_usd_100m": sc_flow_100m,
              "source": snap["source"]}],
        )
    if record.get("error"):
        entries[ticker] = _retained_entry(
            ticker, "failed", f"今日取数失败（自算：{record['error']}），显示上一日数据。"
        )
        failed_live_capable += 1
    elif snap is None:
        entries[ticker] = _retained_entry(
            ticker, "failed", "今日取数失败（自算无快照），显示上一日数据。"
        )
        failed_live_capable += 1
    else:
        aum_100m, pct = _selfcalc_aum_pct(ticker)
        if sc_flow_100m is None:
            entries[ticker] = _etf_entry(
                ticker, snap["date"], None, aum_100m, None, snap["source"],
                snap["date"], "unmeasured", daily,
                record.get("flow_note") or "自算无流量。",
            )
        else:
            status = "live" if _is_fresh(snap["date"], today) else "stale"
            note = record.get("flow_note") or ""
            entries[ticker] = _etf_entry(
                ticker, snap["date"], sc_flow_100m, aum_100m, pct, snap["source"],
                snap["date"], status, daily, note,
            )

    # --- IBIT：读同 run 的 data/farside-btc.json ---
    farside = (
        _load_json_file(os.path.join(out_dir, "farside-btc.json")) if out_dir else {}
    )
    ibit_rows = []
    ibit_latest = None
    for row in farside.get("rows") or []:
        if not isinstance(row, dict):
            continue
        funds = row.get("funds") or {}
        if funds.get("IBIT") is None:
            continue
        flow_100m = round(funds["IBIT"] / 100.0, 4)  # Farside 表格数值为百万美元（假设，未验证）
        ibit_rows.append(
            {"date": row["date"], "flow_usd_100m": flow_100m, "source": "farside"}
        )
    ibit_rows.sort(key=lambda r: r["date"])
    prev_ibit = prev_tracked.get("IBIT") or {}
    ibit_daily = merge_daily_series(prev_ibit.get("daily") or [], ibit_rows[-ETF_DAILY_KEEP_DAYS:])
    if ibit_rows:
        ibit_latest = ibit_rows[-1]
        status = "live" if _is_fresh(ibit_latest["date"], today) else "stale"
        entries["IBIT"] = _etf_entry(
            "IBIT", ibit_latest["date"], ibit_latest["flow_usd_100m"], None, None,
            "farside", ibit_latest["date"], status, ibit_daily,
            "单位假设未验证（Farside 表格数值为百万美元，daily_flow_usd_100m = 该值/100）"
            "；Farside 未给 AUM，pct 未算。",
        )
    else:
        entries["IBIT"] = _etf_entry(
            "IBIT", prev_ibit.get("date_et"), None, None, None, "seed",
            prev_ibit.get("as_of_date"), "seed", prev_ibit.get("daily") or [],
            "Farside 文件不存在或无 IBIT，未编数。",
        )

    # --- QQQ、VOO：自算主路（新浪 gb_ 行情份额；收盘后才写快照）---
    for ticker in ("QQQ", "VOO"):
        prev_entry = prev_tracked.get(ticker) or {}
        prev_daily = prev_entry.get("daily") or []
        record = selfcalc.get(ticker) or {}
        snap = record.get("snapshot")
        sc_flow_100m = _usd_to_100m(record.get("flow_usd"))
        daily = prev_daily
        if snap and sc_flow_100m is not None:
            daily = merge_daily_series(
                daily,
                [{"date": snap["date"], "flow_usd_100m": sc_flow_100m,
                  "source": snap["source"]}],
            )
        if record.get("error"):
            entries[ticker] = _retained_entry(
                ticker, "failed", f"今日取数失败（自算：{record['error']}），显示上一日数据。"
            )
            failed_live_capable += 1
            continue
        if snap is None:
            # 无快照：新浪盘中跳过且从未有过快照 -> 保留 seed entry（不记 failed）。
            entries[ticker] = _retained_entry(
                ticker, "seed",
                record.get("flow_note") or "新浪盘中跳过快照，首个快照等收盘后（美东 16:00 后）的跑。",
            )
            continue
        aum_100m, pct = _selfcalc_aum_pct(ticker)
        if sc_flow_100m is None:
            entries[ticker] = _etf_entry(
                ticker, snap["date"], None, aum_100m, None, snap["source"],
                snap["date"], "unmeasured", daily,
                record.get("flow_note") or "自算无流量。",
            )
            continue
        status = "live" if _is_fresh(snap["date"], today) else "stale"
        note = record.get("flow_note") or ""
        entries[ticker] = _etf_entry(
            ticker, snap["date"], sc_flow_100m, aum_100m, pct, snap["source"],
            snap["date"], status, daily, note,
        )

    live_count = sum(1 for entry in entries.values() if entry["status"] == "live")
    if failed_live_capable >= 12 and entries["IBIT"]["status"] == "seed":
        raise FetchError(
            "etf-flows 全部可取数标的都失败（7 只 TickerInside/自算备用 + TLT + GLD + SMH + QQQ + VOO），"
            "且 IBIT 无 Farside 数据；保留旧文件、status 标 failed。"
        )
    top_status = "live" if live_count >= 1 else "seed"
    if top_status == "seed":
        notes.append("本次无新鲜 live 数，整源 status 记 seed。")
    # 交叉核对 note 只追加文字，绝不改已发布的流量/来源/status。
    for ticker in SINA_XCHECK_TICKERS:
        record = xcheck.get(ticker) or {}
        note = record.get("note")
        if note and ticker in entries:
            prev_note = entries[ticker].get("note") or ""
            entries[ticker]["note"] = (
                (prev_note + "；" if prev_note else "") + note
            )
    tracked = [entries[ticker] for ticker in ETF_TRACKED_ORDER]
    return {
        "schema_version": 1,
        "source": "TickerInside / 自算（BlackRock、SSGA、VanEck 发行商文件；新浪 QQQ/VOO 份额、SOXX/TLT/IBIT 交叉核对） / Farside（IBIT）",
        "source_url": TICKERINSIDE_SERIES_URL,
        "fetched_at_utc": utcnow_iso(),
        "status": top_status,
        "attribution": TICKERINSIDE_ATTRIBUTION,
        "weekly": _weekly_from_ici(out_dir, prev_payload),
        "monthly_bond_etf_usd_100m": 7100.0,
        "ytd_etf_net_usd_100m": 17900.0,
        "tracked": tracked,
        "notes": notes
        + [
            "monthly_bond_etf_usd_100m 7100.0 为种子值（10 月债券 ETF，暂无 live 路）。",
            "ytd_etf_net_usd_100m 17900.0 为种子值（年初至今 ETF，暂无 live 路）。",
        ],
    }


def _ti_headline(ticker, ti_latest_row, ti_flow_100m, notes, snapshot=None):
    """TickerInside 主路 headline：流量取 TickerInside，aum/pct 取自算快照总资产现算。

    不采信 TickerInside 自带的 flow_pct_of_assets（实测：SOXX 11-11 pct 2.382%
    反推总资产 $447.3 亿，与 11-24 快照总资产 $335.7 亿差 27.8%——主要是日期差：
    11-11 收盘价 $155.40，11-24 快照 NAV $125.51，总资产两周内随市价跌了约 25%；
    种子 $350 亿是 11 月下旬口径，与 11-11 的总资产不是同一天，不能直接比。
    种子总资产不动（只有权威来源核实后才能改）。
    pct = 流量 / 快照总资产（NAV×份额）；快照截至日与流量日不同时在 note 里写明。
    快照总资产与种子值偏差超 5 倍或不足 0.2 倍时视为单位错误，aum/pct 记 null。
    无快照时 aum/pct 记 null，不编数。"""
    aum_100m = None
    pct = None
    note = ""
    if snapshot is not None:
        aum_100m = round(snapshot["nav"] * snapshot["shares"] / 1e8, 4)
        seed_aum = SEED_AUM_100M.get(ticker)
        if seed_aum and not (0.2 * seed_aum <= aum_100m <= 5.0 * seed_aum):
            notes.append(
                f"{ticker} 快照总资产 {aum_100m:.1f}（亿美元）与种子值"
                f" {seed_aum:.1f} 偏差超 5 倍（疑似单位错误），aum/pct 记 null。"
            )
            aum_100m = None
        elif ti_flow_100m is not None and aum_100m:
            pct = round(ti_flow_100m / aum_100m * 100, 4)
        snap_date = snapshot.get("date")
        if snap_date and snap_date != ti_latest_row["date"]:
            note = (
                f"pct 按快照日总资产现算（快照截至 {snap_date}，"
                f"流量日 {ti_latest_row['date']}）。"
            )
    else:
        note = "无自算快照，pct 未算。"
    return (
        ti_flow_100m,
        ti_latest_row["date"],
        "tickerinside",
        pct,
        aum_100m,
        note,
    )


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
    "fred-credit": (fetch_fred_credit, "fred-credit.json"),
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
    global ETF_FLOWS_OUT_DIR
    ETF_FLOWS_OUT_DIR = out_dir  # etf-flows 的快照读写与同 run 文件读取用
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
            # 各源 payload 的时间字段：旧源用 _payload() 的 fetched_at，
            # etf-flows 按数据契约（§1.2）用 fetched_at_utc；两者都认。
            entry["last_ok"] = payload.get("fetched_at") or payload.get("fetched_at_utc")
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
            "blocked 含两种：未配置（跳过不计退出码）与 HTTP 403 反爬拦截（计入失败）。"
            "etf-flows 已接入（TickerInside + 发行商文件自算 + Farside IBIT）；"
            "ETF.com 本身仍未配置（无公开 API），不作为取数源。",
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
        help="只跑指定源，逗号分隔，如 farside-btc,sosovalue-btc；默认 all（全部 9 个源）。",
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
