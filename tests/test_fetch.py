#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金流仪表盘取数脚本 — 离线测试（不联网）。

跑法（仓库根目录，即 github-actions/ 下）：
    python3 -m unittest discover -s tests -v

只测解析逻辑（parse_* 纯函数）、退出码规则、SoSoValue v1/v2 切换
（mock 掉 get_json/post_json，不联网）与 probe 四档结构（mock 掉
urlopen 与可选依赖，不联网）；所有输入均为
tests/fixtures/ 下的最小 fixture（文件头注明“fixture，非真实数据”）。
联网取数（HTTP 状态、反爬拦截、Secrets）不在此测，
须等仓库接通后由 Actions 首次实测验证（见 tests/TESTS.md）。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import fetch_all  # noqa: E402
import probe  # noqa: E402
from fetch_all import (  # noqa: E402
    FetchError,
    SchemaError,
    _decode_body,
    _resolve_ciks,
    _ti_pct_to_percent,
    _usd_to_100m,
    check_nav_range,
    compute_selfcalc_flow,
    decide_exit_code,
    detect_split,
    fetch_etf_flows,
    fetch_sosovalue,
    merge_daily_series,
    parse_blackrock_holdings_csv,
    parse_companyfacts_json,
    parse_edgar_search_json,
    parse_farside_html,
    parse_fred_json,
    parse_ici_flow_table,
    parse_ici_mmf_table,
    parse_number,
    parse_sosovalue_json,
    parse_sosovalue_v1_json,
    parse_ssga_fund_page,
    parse_tickerinside_series,
    trading_days_between,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return fh.read()


def load_json_fixture(name):
    return json.loads(load_fixture(name))


class TestDecodeBody(unittest.TestCase):
    def test_gzip_and_deflate_roundtrip(self):
        # 回归：请求头声明 gzip/deflate 后，服务器返回的压缩体必须解开再解析。
        import gzip as _gzip
        import zlib as _zlib

        raw = b"<html>hello</html>"
        self.assertEqual(_decode_body(_gzip.compress(raw), "gzip"), raw)
        self.assertEqual(_decode_body(_zlib.compress(raw), "deflate"), raw)
        self.assertEqual(_decode_body(raw, None), raw)
        self.assertEqual(_decode_body(raw, "br"), raw)  # 未知编码原样返回


class TestParseNumber(unittest.TestCase):
    def test_plain_and_formatted(self):
        self.assertEqual(parse_number("1,234.5"), 1234.5)
        self.assertEqual(parse_number("$509.3"), 509.3)
        self.assertEqual(parse_number(42), 42.0)

    def test_parentheses_are_negative(self):
        self.assertEqual(parse_number("(509.3)"), -509.3)

    def test_missing_is_none_not_zero(self):
        for raw in ("-", ".", "—", "", None, "n/a"):
            self.assertIsNone(parse_number(raw), msg=f"raw={raw!r} 应为 None，不编 0")


class TestFarside(unittest.TestCase):
    def test_table_parses(self):
        # 短表真实结构：logo 行 + ticker 行 + Fee 行，日期为 DD Mon YYYY。
        rows = parse_farside_html(load_fixture("farside_btc.html"))
        self.assertEqual(len(rows), 2)  # 'Not a date' 行与 Total 汇总行被丢弃
        self.assertEqual(rows[0]["date"], "2026-11-20")
        self.assertEqual(rows[0]["total"], 100.0)
        self.assertEqual(rows[1]["date"], "2026-11-23")
        self.assertEqual(rows[1]["total"], -569.7)  # 括号负数
        self.assertEqual(rows[1]["funds"]["IBIT"], -509.3)
        self.assertEqual(rows[1]["funds"]["FBTC"], 12.5)

    def test_btc_all_data_parses(self):
        # BTC 全量表：首行即 Date | ticker…| Total，但日期仍为 DD Mon YYYY。
        rows = parse_farside_html(load_fixture("farside_btc_all.html"))
        self.assertEqual(len(rows), 2)  # Maximum 汇总行被丢弃
        self.assertEqual(rows[0]["date"], "2026-11-20")
        self.assertEqual(rows[0]["total"], 100.0)
        self.assertEqual(rows[1]["date"], "2026-11-23")
        self.assertEqual(rows[1]["total"], -569.7)
        self.assertEqual(rows[1]["funds"]["IBIT"], -509.3)

    def test_eth_all_data_parses(self):
        # ETH 全量表：首行首格为空 + ticker + Total，无 Date 格。
        rows = parse_farside_html(load_fixture("farside_eth.html"))
        self.assertEqual(len(rows), 2)  # Average 汇总行被丢弃
        self.assertEqual(rows[0]["date"], "2026-11-20")
        self.assertEqual(rows[0]["total"], 20.0)
        self.assertEqual(rows[1]["date"], "2026-11-23")
        self.assertEqual(rows[1]["total"], 5.0)
        self.assertEqual(rows[1]["funds"]["ETHA"], 10.0)
        self.assertEqual(rows[1]["funds"]["FETH"], -5.0)

    def test_no_valid_table_raises(self):
        with self.assertRaises(SchemaError):
            parse_farside_html(load_fixture("farside_bad.html"))


class TestSoSoValue(unittest.TestCase):
    def test_shape_a_code_data_list(self):
        rows = parse_sosovalue_json(load_json_fixture("sosovalue_a.json"))
        self.assertEqual(
            rows,
            [
                {"date": "2026-11-20", "net_flow_usd": 123000000.0},
                {"date": "2026-11-23", "net_flow_usd": -569700000.0},
            ],
        )

    def test_shape_b_data_list_with_timestamp(self):
        rows = parse_sosovalue_json(load_json_fixture("sosovalue_b.json"))
        self.assertEqual(rows[0]["date"], "2026-11-19")  # 毫秒时间戳转日期
        self.assertEqual(rows[0]["net_flow_usd"], -1000000.0)
        self.assertEqual(rows[1]["net_flow_usd"], 2500000.0)  # 字符串数字

    def test_error_code_raises(self):
        with self.assertRaises(SchemaError):
            parse_sosovalue_json(load_json_fixture("sosovalue_err.json"))

    def test_unknown_shape_raises(self):
        with self.assertRaises(SchemaError):
            parse_sosovalue_json({"data": {"unexpected": True}})


class TestFred(unittest.TestCase):
    def test_observations_parse(self):
        rows = parse_fred_json(load_json_fixture("fred.json"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {"date": "2026-11-20", "value": 4.92})
        self.assertIsNone(rows[1]["value"])  # '.' -> None，不编 0
        self.assertEqual(rows[2]["value"], 4.85)

    def test_missing_observations_raises(self):
        with self.assertRaises(SchemaError):
            parse_fred_json(load_json_fixture("fred_bad.json"))


class TestCompanyFacts(unittest.TestCase):
    def test_msft_latest_10q(self):
        rec = parse_companyfacts_json(
            load_json_fixture("edgar_companyfacts_msft.json"), "MSFT"
        )
        # 只取 10-Q（2026-09-30），不取 10-K（2026-06-30）的旧 OCF
        self.assertEqual(rec["ocf_usd"], 58000000000.0)
        self.assertEqual(rec["capex_cash_usd"], 39367000000.0)
        self.assertEqual(rec["capex_ocf_pct"], 67.9)
        self.assertEqual(rec["fcf_usd"], 58000000000.0 - 39367000000.0)
        self.assertEqual(rec["fiscal_period_end"], "2026-09-30")
        # 净债务 = (31.090B + 4.391B) - (20.281B + 44.228B) = -29.028B（净现金）
        self.assertEqual(rec["net_debt_usd"], -29028000000.0)
        self.assertEqual(rec["debt_repayments_usd"], 4850000000.0)
        self.assertEqual(rec["notes"], [])

    def test_empty_facts_yields_nulls_not_crash(self):
        rec = parse_companyfacts_json(
            load_json_fixture("edgar_companyfacts_empty.json"), "MSFT"
        )
        self.assertIsNone(rec["ocf_usd"])
        self.assertIsNone(rec["capex_ocf_pct"])
        self.assertIsNone(rec["net_debt_usd"])
        self.assertTrue(rec["notes"])  # 缺失要留 note，不静默

    def test_missing_facts_raises(self):
        with self.assertRaises(SchemaError):
            parse_companyfacts_json({"cik": "1"}, "MSFT")


class TestEdgarSearch(unittest.TestCase):
    def test_hits_parse(self):
        events = parse_edgar_search_json(load_json_fixture("edgar_search.json"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["form"], "8-K")
        self.assertEqual(events[0]["accession"], "0001193125-26-436133")

    def test_empty_hits_is_empty_not_error(self):
        events = parse_edgar_search_json(load_json_fixture("edgar_search_empty.json"))
        self.assertEqual(events, [])

    def test_missing_hits_raises(self):
        with self.assertRaises(SchemaError):
            parse_edgar_search_json({})


class TestExitCodes(unittest.TestCase):
    def test_all_ok(self):
        self.assertEqual(decide_exit_code(8, 8), 0)

    def test_partial_failure(self):
        self.assertEqual(decide_exit_code(5, 8), 2)

    def test_all_failed(self):
        self.assertEqual(decide_exit_code(0, 8), 1)

    def test_nothing_attempted(self):
        self.assertEqual(decide_exit_code(0, 0), 0)


class TestTickerInside(unittest.TestCase):
    def test_anchor_and_null_row_preserved(self):
        parsed = parse_tickerinside_series(load_json_fixture("tickerinside_series.json"))
        self.assertEqual(parsed["latest_date"], "2026-11-11")
        self.assertEqual(parsed["warnings"], [])  # 锚点值与文档值一致，无警告
        soxx = parsed["tickers"]["SOXX"]
        self.assertEqual(
            [row["date"] for row in soxx],
            ["2026-11-09", "2026-11-10", "2026-11-11"],
        )
        # 11-10 null 行原样保留（未测得），不填 0
        self.assertIsNone(soxx[1]["flow_usd"])
        self.assertIsNone(soxx[1]["flow_pct_of_assets"])
        # 锚点：SOXX 11-11 行 flow_usd = +1,065,556,947 美元
        self.assertEqual(soxx[2]["flow_usd"], 1065556947.0)
        self.assertEqual(soxx[2]["flow_pct_of_assets"], 0.0305)
        self.assertEqual(set(parsed["tickers"]), set(fetch_all.TICKERINSIDE_TICKERS))

    def test_missing_ticker_raises(self):
        obj = load_json_fixture("tickerinside_series.json")
        del obj["series"]["XLK"]
        with self.assertRaises(SchemaError):
            parse_tickerinside_series(obj)

    def test_anchor_value_deviation_warns_only(self):
        obj = load_json_fixture("tickerinside_series.json")
        obj["series"]["SOXX"][2][1] = 100000000  # 与锚点偏差超 20%
        parsed = parse_tickerinside_series(obj)
        self.assertEqual(parsed["tickers"]["SOXX"][2]["flow_usd"], 100000000.0)
        self.assertTrue(any("锚点" in w for w in parsed["warnings"]))

    def test_anchor_row_missing_in_window_raises(self):
        obj = load_json_fixture("tickerinside_series.json")
        obj["series"]["SOXX"] = obj["series"]["SOXX"][:2]  # 删掉 11-11 行
        with self.assertRaises(SchemaError):
            parse_tickerinside_series(obj)

    def test_pct_fraction_or_percent_to_percent(self):
        self.assertEqual(_ti_pct_to_percent(0.0305), 3.05)
        self.assertEqual(_ti_pct_to_percent(3.05), 3.05)
        self.assertIsNone(_ti_pct_to_percent(None))


class TestBlackRock(unittest.TestCase):
    def test_tlt_parses(self):
        parsed = parse_blackrock_holdings_csv(load_fixture("blackrock_tlt.csv"))
        # 份额 600,000,000（2026-11-24）；Σ市值 $48,894,000,000 / 份额 = $81.49
        self.assertEqual(parsed["date"], "2026-11-24")
        self.assertEqual(parsed["shares"], 600000000.0)
        self.assertEqual(parsed["total_market_value_usd"], 48894000000.0)
        self.assertAlmostEqual(parsed["nav"], 81.49, places=2)

    def test_missing_shares_raises(self):
        text = load_fixture("blackrock_tlt.csv").replace(
            'Shares Outstanding, "600,000,000.00"\n', ""
        )
        with self.assertRaises(SchemaError):
            parse_blackrock_holdings_csv(text)

    def test_missing_market_value_column_raises(self):
        text = load_fixture("blackrock_tlt.csv").replace("Market Value", "Mkt Val")
        with self.assertRaises(SchemaError):
            parse_blackrock_holdings_csv(text)

    def test_total_row_not_double_counted(self):
        text = load_fixture("blackrock_tlt.csv") + (
            "Total,Public,Fixed Income,48894000000,100.0,0,0,0,0,0,0,0,0\n"
        )
        parsed = parse_blackrock_holdings_csv(text)
        self.assertAlmostEqual(parsed["nav"], 81.49, places=2)


class TestSSGA(unittest.TestCase):
    def test_gld_parses_precise_nav(self):
        parsed = parse_ssga_fund_page(load_fixture("ssga_gld.html"))
        self.assertEqual(parsed["date"], "2026-11-24")
        self.assertEqual(parsed["shares"], 360400000.0)  # 360.40 M
        # 精确值 331.98279858 优先于表内 $331.98
        self.assertEqual(parsed["nav"], 331.98279858)
        self.assertTrue(parsed["nav_precise"])

    def test_precise_nav_missing_falls_back_to_table(self):
        text = load_fixture("ssga_gld.html").replace(
            '<script type="application/json">{"nav": 331.98279858}</script>', ""
        )
        parsed = parse_ssga_fund_page(text)
        self.assertEqual(parsed["nav"], 331.98)
        self.assertFalse(parsed["nav_precise"])

    def test_european_format_rejected(self):
        text = load_fixture("ssga_gld.html").replace("360.40 M", "361,80 M")
        with self.assertRaises(SchemaError):
            parse_ssga_fund_page(text)

    def test_missing_nav_raises(self):
        text = load_fixture("ssga_gld.html").replace(
            "<tr><th>NAV</th><td>$331.98</td></tr>\n", ""
        )
        with self.assertRaises(SchemaError):
            parse_ssga_fund_page(text)

    def test_nav_per_basket_not_mistaken_for_nav(self):
        # NAV Per Basket（$33,198,279.86）是另一行，绝不能当成 NAV
        parsed = parse_ssga_fund_page(load_fixture("ssga_gld.html"))
        self.assertLess(parsed["nav"], 1000)


class TestSelfCalcFlow(unittest.TestCase):
    def test_first_day_flow_is_none(self):
        today = {"date": "2026-11-25", "shares": 360400000.0, "nav": 331.98}
        flow, note = compute_selfcalc_flow(today, None)
        self.assertIsNone(flow)
        self.assertIn("首日", note)

    def test_gld_example_reproduces_minus_465m(self):
        # 方案 §4.2 实算：11-24 361.80M → 11-25 360.40M，NAV $331.98，流量约 −$4.65 亿
        prev = {"date": "2026-11-24", "shares": 361800000.0, "nav": 331.98}
        today = {"date": "2026-11-25", "shares": 360400000.0, "nav": 331.98279858}
        flow, note = compute_selfcalc_flow(today, prev)
        self.assertAlmostEqual(flow, -464775918.01, delta=1.0)
        self.assertAlmostEqual(_usd_to_100m(flow), -4.6478, places=4)
        self.assertEqual(note, "")

    def test_split_2_to_1_adjusted(self):
        prev = {"date": "2026-11-24", "shares": 100000000.0, "nav": 200.0}
        today = {"date": "2026-11-25", "shares": 200000000.0, "nav": 100.0}
        self.assertEqual(detect_split(100000000.0, 200000000.0, 200.0, 100.0), 2.0)
        flow, note = compute_selfcalc_flow(today, prev)
        self.assertEqual(flow, 0.0)  # 折算后份额不变，流量为 0（算出来的 0，不是填的 0）
        self.assertIn("拆股", note)

    def test_large_redemption_is_not_split(self):
        # SOXX 11-10 式 −11.8% 是真流量：份额比 0.882 不是干净倍数，照常算
        prev = {"date": "2026-11-09", "shares": 175000000.0, "nav": 200.0}
        today = {"date": "2026-11-10", "shares": 154350000.0, "nav": 200.0}
        self.assertIsNone(detect_split(175000000.0, 154350000.0, 200.0, 200.0))
        flow, _ = compute_selfcalc_flow(today, prev)
        self.assertAlmostEqual(_usd_to_100m(flow), -41.3, places=4)

    def test_same_day_rerun_produces_no_flow(self):
        prev = {"date": "2026-11-24", "shares": 600000000.0, "nav": 81.49}
        today = {"date": "2026-11-24", "shares": 601000000.0, "nav": 81.49}
        flow, note = compute_selfcalc_flow(today, prev)
        self.assertIsNone(flow)
        self.assertIn("未前进", note)

    def test_nav_range(self):
        check_nav_range("TLT", 81.49, fetch_all.BLACKROCK_NAV_RANGES)
        check_nav_range("GLD", 331.98, fetch_all.SSGA_NAV_RANGES)
        with self.assertRaises(SchemaError):
            check_nav_range("TLT", 500.0, fetch_all.BLACKROCK_NAV_RANGES)
        with self.assertRaises(SchemaError):
            check_nav_range("XLU", 20.0, fetch_all.SSGA_NAV_RANGES)


def _make_blackrock_csv(shares, date_str, market_values):
    """测试用 BlackRock CSV 生成器（shares 为股数，market_values 为持仓市值列表）。"""
    lines = [
        "iShares Test ETF",
        f'Fund Holdings as of, "{date_str}"',
        'Inception Date, "Jul 22, 2002"',
        f'Shares Outstanding, "{shares:,.2f}"',
        "Stock, 0",
        "Bond, 100",
        "Cash, 0",
        "Other, 0",
        "Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Par Value,CUSIP,ISIN,SEDOL,Price,Duration,YTM (%)",
    ]
    for idx, value in enumerate(market_values):
        lines.append(
            f"HOLDING {idx},Public,Fixed Income,{value},10.0,{value},{value},"
            f"CUSIP{idx},ISIN{idx},SEDOL{idx},100,20.5,4.9"
        )
    return "\n".join(lines) + "\n"


def _fake_http_tlt_only(url, headers=None, timeout=None):
    """测试替身：只有 TLT 的 BlackRock CSV 能取，其余发行商页一律 403。"""
    if "portfolioId=239454" in url:
        return 200, load_fixture("blackrock_tlt.csv").encode("utf-8")
    raise FetchError(f"HTTP 403 {url}")


class TestEtfFlowsMerge(unittest.TestCase):
    def test_merge_dedupe_and_cap_60(self):
        import datetime as _datetime

        base = _datetime.date(2026, 8, 1)
        prev = [
            {"date": (base + _datetime.timedelta(days=i)).isoformat(),
             "flow_usd_100m": float(i), "source": "tickerinside"}
            for i in range(70)
        ]
        new = [
            {"date": (base + _datetime.timedelta(days=69)).isoformat(),
             "flow_usd_100m": 999.0, "source": "tickerinside"},
            {"date": (base + _datetime.timedelta(days=70)).isoformat(),
             "flow_usd_100m": 70.0, "source": "tickerinside"},
        ]
        merged = merge_daily_series(prev, new)
        self.assertEqual(len(merged), 60)  # 只保留最近 60 个收盘日
        self.assertEqual(merged[-1]["date"], (base + _datetime.timedelta(days=70)).isoformat())
        # 2026-10-09（第 69 天）新行覆盖旧行
        self.assertEqual(merged[-2]["flow_usd_100m"], 999.0)
        self.assertEqual(
            [row["date"] for row in merged], sorted(row["date"] for row in merged)
        )

    def test_merge_keeps_null_rows(self):
        prev = [{"date": "2026-11-09", "flow_usd_100m": 5.0, "source": "tickerinside"}]
        new = [{"date": "2026-11-10", "flow_usd_100m": None, "source": "tickerinside"}]
        merged = merge_daily_series(prev, new)
        self.assertEqual(len(merged), 2)
        self.assertIsNone(merged[1]["flow_usd_100m"])  # null 行保留，不填 0

    def test_conflict_over_10pct_keeps_alternate(self):
        # 今天 2026-11-12：TickerInside 最新 11-11，滞后 1 个交易日，未切换，
        # 主路 TickerInside；自算同日流量 30.0 与主路 10.6556 差值 >10%。
        with tempfile.TemporaryDirectory() as out_dir:
            prev_snaps = load_json_fixture("etf_snapshots_prev.json")
            prev_snaps["SOXX"] = {
                "date": "2026-11-10", "shares": 175000000.0, "nav": 200.0,
                "source": "selfcalc-blackrock",
            }
            with open(os.path.join(out_dir, "etf-selfcalc-snapshots.json"), "w") as fh:
                json.dump(prev_snaps, fh)
            soxx_csv = _make_blackrock_csv(
                190000000, "Nov 11, 2026", [20000000000, 16000000000]
            )  # NAV 36000M/190M = 189.47；流量 = 189.47 × 15M ≈ $28.42 亿

            def fake_http(url, headers=None, timeout=None):
                if "portfolioId=239705" in url:
                    return 200, soxx_csv.encode("utf-8")
                raise FetchError(f"HTTP 403 {url}")

            with mock.patch.object(fetch_all, "get_json",
                                   return_value=load_json_fixture("tickerinside_series.json")):
                with mock.patch.object(fetch_all, "http_request", side_effect=fake_http):
                    payload = fetch_etf_flows(out_dir, today="2026-11-12")
        soxx = [e for e in payload["tracked"] if e["ticker"] == "SOXX"][0]
        self.assertEqual(soxx["source"], "tickerinside")  # 未切换，主路仍是 TickerInside
        self.assertAlmostEqual(soxx["daily_flow_usd_100m"], 10.6556, places=4)
        # 自算同日流量 189.47 × 15M / 1e8 ≈ 28.42，与主路差值 >10%，两路都保留
        self.assertIsNotNone(soxx.get("alternate_flow"))
        self.assertAlmostEqual(soxx["alternate_flow"], 28.4211, places=3)
        self.assertIn("两路不一致", soxx["note"])
        # daily 里 11-11 仍是主路 TickerInside 的数，不被自算覆盖
        daily_1111 = [r for r in soxx["daily"] if r["date"] == "2026-11-11"][0]
        self.assertAlmostEqual(daily_1111["flow_usd_100m"], 10.6556, places=4)
        self.assertEqual(daily_1111["source"], "tickerinside")

    def test_full_fetch_switched_to_selfcalc(self):
        # 今天 2026-11-25：TickerInside 最新 11-11，滞后 10 个交易日，7 只切自算备用。
        with tempfile.TemporaryDirectory() as out_dir:
            for name in ("etf-flows.json", "ici.json", "farside-btc.json",
                         "etf-selfcalc-snapshots.json"):
                src = {
                    "etf-flows.json": "etf_flows_prev.json",
                    "ici.json": "ici_etf.json",
                    "farside-btc.json": "farside_btc_min.json",
                    "etf-selfcalc-snapshots.json": "etf_snapshots_prev.json",
                }[name]
                with open(os.path.join(out_dir, name), "w") as fh:
                    fh.write(load_fixture(src))
            tlt_csv = load_fixture("blackrock_tlt.csv").replace(
                "Nov 24, 2026", "Nov 25, 2026"
            ).replace("600,000,000.00", "601,000,000.00")
            soxx_csv = _make_blackrock_csv(
                176000000, "Nov 25, 2026", [20000000000, 15200000000]
            )  # NAV 35200M/176M = $200.00；流量 = 200 × 1M = $2.00 亿
            gld_html = load_fixture("ssga_gld.html").replace(
                "Nov 24 2026", "Nov 25 2026"
            ).replace("360.40 M", "359.40 M")
            spy_html = (
                load_fixture("ssga_gld.html")
                .replace("Nov 24 2026", "Nov 25 2026")
                .replace("$331.98", "$718.51")
                .replace("360.40 M", "1,071.28 M")
                .replace("331.98279858", "718.51234567")
            )

            def fake_http(url, headers=None, timeout=None):
                if "portfolioId=239454" in url:
                    return 200, tlt_csv.encode("utf-8")
                if "portfolioId=239705" in url:
                    return 200, soxx_csv.encode("utf-8")
                if url == fetch_all.SSGA_FUND_PAGES["GLD"]:
                    return 200, gld_html.encode("utf-8")
                if url == fetch_all.SSGA_FUND_PAGES["SPY"]:
                    return 200, spy_html.encode("utf-8")
                raise FetchError(f"HTTP 403 {url}")  # 其余 SSGA 页取不到

            with mock.patch.object(fetch_all, "get_json",
                                   return_value=load_json_fixture("tickerinside_series.json")):
                with mock.patch.object(fetch_all, "http_request", side_effect=fake_http):
                    payload = fetch_etf_flows(out_dir, today="2026-11-25")
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["status"], "live")  # ≥1 只新鲜 live 数
            self.assertEqual(payload["source_url"], fetch_all.TICKERINSIDE_SERIES_URL)
            self.assertEqual(payload["attribution"]["credit"], "TickerInside")
            self.assertEqual(payload["weekly"]["equity_etf_usd_100m"], 4571.0)
            self.assertEqual(payload["weekly"]["coverage_week_end_et"], "2026-11-18")
            self.assertEqual(payload["weekly"]["source"], "ici")
            self.assertEqual(payload["monthly_bond_etf_usd_100m"], 7100.0)
            self.assertEqual(payload["ytd_etf_net_usd_100m"], 17900.0)
            self.assertTrue(any("滞后约 10 个交易日" in n for n in payload["notes"]))
            self.assertTrue(any("SOXX 2026-11-10" in n and "null" in n for n in payload["notes"]))
            by_ticker = {e["ticker"]: e for e in payload["tracked"]}
            self.assertEqual(
                [e["ticker"] for e in payload["tracked"]], fetch_all.ETF_TRACKED_ORDER
            )
            # SOXX：已切自算备用（BlackRock），流量 200 × 1M = $2.00 亿
            soxx = by_ticker["SOXX"]
            self.assertEqual(soxx["source"], "selfcalc-blackrock")
            self.assertEqual(soxx["status"], "live")
            self.assertAlmostEqual(soxx["daily_flow_usd_100m"], 2.0, places=4)
            self.assertIn("来源切换", soxx["note"])
            self.assertEqual(
                [r["date"] for r in soxx["daily"]],
                ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-25"],
            )
            self.assertIsNone(soxx["daily"][1]["flow_usd_100m"])  # 11-10 null 行保留
            # SPY：已切自算备用（SSGA），流量 718.51234567 × 1M ≈ $7.19 亿
            spy = by_ticker["SPY"]
            self.assertEqual(spy["source"], "selfcalc-ssga")
            self.assertEqual(spy["status"], "live")
            self.assertAlmostEqual(spy["daily_flow_usd_100m"], 7.1851, places=4)
            # TLT：自算主路，流量 = 48894M/601M × 1M ≈ $0.81 亿
            tlt = by_ticker["TLT"]
            self.assertEqual(tlt["source"], "selfcalc-blackrock")
            self.assertEqual(tlt["status"], "live")
            self.assertAlmostEqual(tlt["daily_flow_usd_100m"], 0.8135, places=2)
            # GLD：自算主路，流量 = 331.98279858 × (359.40M − 361.80M) ≈ −$7.97 亿
            gld = by_ticker["GLD"]
            self.assertEqual(gld["source"], "selfcalc-ssga")
            self.assertEqual(gld["status"], "live")
            self.assertAlmostEqual(gld["daily_flow_usd_100m"], -7.9676, places=4)
            # XLP/XLV/XLE/XLU/XLK：自算备用取不到，failed；XLP 有上日 entry 可保留，
            # 其余 4 只上日无 entry，date_et 为 None（不编数）
            self.assertEqual(by_ticker["XLP"]["status"], "failed")
            self.assertEqual(by_ticker["XLP"]["date_et"], "2026-11-11")
            self.assertAlmostEqual(by_ticker["XLP"]["daily_flow_usd_100m"], 1.0, places=4)
            for ticker in ("XLV", "XLE", "XLU", "XLK"):
                self.assertEqual(by_ticker[ticker]["status"], "failed", msg=ticker)
                self.assertIsNone(by_ticker[ticker]["date_et"], msg=ticker)
                self.assertIn("取数失败", by_ticker[ticker]["note"], msg=ticker)
            # IBIT：Farside 11-24 行 200.0（百万美元）→ 2.0（亿美元）
            ibit = by_ticker["IBIT"]
            self.assertEqual(ibit["source"], "farside")
            self.assertEqual(ibit["status"], "live")
            self.assertAlmostEqual(ibit["daily_flow_usd_100m"], 2.0, places=4)
            self.assertIsNone(ibit["aum_usd_100m"])
            self.assertIn("单位假设未验证", ibit["note"])
            # VOO、QQQ、SMH：本阶段记 seed，不编每日数
            for ticker in ("VOO", "QQQ", "SMH"):
                entry = by_ticker[ticker]
                self.assertEqual(entry["source"], "seed", msg=ticker)
                self.assertEqual(entry["status"], "seed", msg=ticker)
                self.assertIsNone(entry["daily_flow_usd_100m"], msg=ticker)
                self.assertFalse(entry["flag_over_5pct"], msg=ticker)
            # 快照文件已写回：TLT/GLD/SOXX/SPY 为 11-25，失败的 5 只未写入
            with open(os.path.join(out_dir, "etf-selfcalc-snapshots.json")) as fh:
                snaps = json.load(fh)
            for ticker in ("TLT", "GLD", "SOXX", "SPY"):
                self.assertEqual(snaps[ticker]["date"], "2026-11-25", msg=ticker)
            self.assertNotIn("XLP", snaps)

    def test_first_day_snapshots_only_no_flow(self):
        # 上线第一天：无上日快照，自算流量记 null；快照文件已存下当天数。
        with tempfile.TemporaryDirectory() as out_dir:
            tlt_csv = load_fixture("blackrock_tlt.csv")
            soxx_csv = _make_blackrock_csv(
                175000000, "Nov 24, 2026", [20000000000, 15000000000]
            )
            gld_html = load_fixture("ssga_gld.html")
            spy_html = load_fixture("ssga_gld.html").replace(
                "$331.98", "$718.51"
            ).replace("360.40 M", "1,071.28 M").replace("331.98279858", "718.51234567")

            def fake_http(url, headers=None, timeout=None):
                if "portfolioId=239454" in url:
                    return 200, tlt_csv.encode("utf-8")
                if "portfolioId=239705" in url:
                    return 200, soxx_csv.encode("utf-8")
                if url == fetch_all.SSGA_FUND_PAGES["GLD"]:
                    return 200, gld_html.encode("utf-8")
                if url == fetch_all.SSGA_FUND_PAGES["SPY"]:
                    return 200, spy_html.encode("utf-8")
                raise FetchError(f"HTTP 403 {url}")

            with mock.patch.object(fetch_all, "get_json",
                                   return_value=load_json_fixture("tickerinside_series.json")):
                with mock.patch.object(fetch_all, "http_request", side_effect=fake_http):
                    payload = fetch_etf_flows(out_dir, today="2026-11-25")
            by_ticker = {e["ticker"]: e for e in payload["tracked"]}
            # TLT/GLD：首日无流量，status unmeasured，但 AUM 已知（NAV×份额）
            self.assertEqual(by_ticker["TLT"]["status"], "unmeasured")
            self.assertIsNone(by_ticker["TLT"]["daily_flow_usd_100m"])
            self.assertAlmostEqual(by_ticker["TLT"]["aum_usd_100m"], 488.94, places=2)
            self.assertIn("首日", by_ticker["TLT"]["note"])
            self.assertEqual(by_ticker["GLD"]["status"], "unmeasured")
            self.assertIsNone(by_ticker["GLD"]["daily_flow_usd_100m"])
            # 整源无新鲜 live 数，status 记 seed（不是失败：快照已存，明日即有流量）
            self.assertEqual(payload["status"], "seed")
            with open(os.path.join(out_dir, "etf-selfcalc-snapshots.json")) as fh:
                snaps = json.load(fh)
            self.assertEqual(snaps["TLT"]["date"], "2026-11-24")
            self.assertEqual(snaps["GLD"]["shares"], 360400000.0)

    def test_all_failed_raises(self):
        with tempfile.TemporaryDirectory() as out_dir:
            with mock.patch.object(fetch_all, "get_json",
                                   side_effect=FetchError("HTTP 403 tickerinside")):
                with mock.patch.object(fetch_all, "http_request",
                                       side_effect=FetchError("HTTP 403 issuer")):
                    with self.assertRaises(FetchError):
                        fetch_etf_flows(out_dir, today="2026-11-25")

    def test_weekly_falls_back_to_previous(self):
        # ici.json 不存在时沿用上一次 etf-flows.json 的 weekly（4200.0 / 2026-11-11）
        with tempfile.TemporaryDirectory() as out_dir:
            with open(os.path.join(out_dir, "etf-flows.json"), "w") as fh:
                fh.write(load_fixture("etf_flows_prev.json"))
            with mock.patch.object(fetch_all, "get_json",
                                   return_value=load_json_fixture("tickerinside_series.json")):
                with mock.patch.object(
                    fetch_all, "http_request", side_effect=_fake_http_tlt_only
                ):
                    payload = fetch_etf_flows(out_dir, today="2026-11-25")
        self.assertEqual(payload["weekly"]["equity_etf_usd_100m"], 4200.0)
        self.assertEqual(payload["weekly"]["coverage_week_end_et"], "2026-11-11")
        self.assertEqual(payload["weekly"]["source"], "previous")

    def test_weekly_falls_back_to_seed(self):
        # ici.json 与上一次文件都不存在时用种子值 4571.0 / 2026-11-18
        with tempfile.TemporaryDirectory() as out_dir:
            with mock.patch.object(fetch_all, "get_json",
                                   return_value=load_json_fixture("tickerinside_series.json")):
                with mock.patch.object(
                    fetch_all, "http_request", side_effect=_fake_http_tlt_only
                ):
                    payload = fetch_etf_flows(out_dir, today="2026-11-25")
        self.assertEqual(payload["weekly"]["equity_etf_usd_100m"], 4571.0)
        self.assertEqual(payload["weekly"]["coverage_week_end_et"], "2026-11-18")
        self.assertEqual(payload["weekly"]["source"], "seed")


class TestSourceConfig(unittest.TestCase):
    def test_ici_urls_filled(self):
        # 2026-11-25 校正清单第 1 项：ICI 四页 URL 已填入（核查报告 §1.1），
        # 旧断言“ICI_URL is None”随本次修正失效，特此改为核对确切 URL。
        self.assertEqual(
            fetch_all.ICI_URLS,
            {
                "flows": "https://www.ici.org/research/stats/flows",
                "combined": "https://www.ici.org/research/stats/combined_flows",
                "etf": "https://www.ici.org/research/stats/etf_flows",
                "mmf": "https://www.ici.org/research/stats/mmf",
            },
        )

    def test_etf_com_still_unconfigured(self):
        # ETF.com 无公开 API（v1 结论），仍保持未配置，绝不凭猜测写 URL。
        self.assertIsNone(fetch_all.ETF_COM_URL)

    def test_etf_flows_has_real_fetcher(self):
        # etf-flows 已有真实 fetcher（TickerInside + 自算）：取数全失败时抛
        # FetchError，不再是旧占位的 NotConfigured。
        with mock.patch.object(fetch_all, "get_json",
                               side_effect=FetchError("HTTP 403 tickerinside")):
            with mock.patch.object(fetch_all, "http_request",
                                   side_effect=FetchError("HTTP 403 issuer")):
                with self.assertRaises(FetchError):
                    fetch_etf_flows(None, today="2026-11-25")
        self.assertIn("etf-flows", fetch_all.FETCHERS)
        self.assertEqual(fetch_all.FETCHERS["etf-flows"][1], "etf-flows.json")


class TestICI(unittest.TestCase):
    def test_flows_table_parses(self):
        parsed = parse_ici_flow_table(load_fixture("ici_flows.html"), "flows")
        self.assertEqual(parsed["unit"], "USD millions")
        self.assertEqual(len(parsed["dates"]), 5)  # 最近 5 周全量
        self.assertEqual(parsed["dates"][-1], "2026-11-11")
        self.assertEqual(parsed["total"]["2026-11-11"], -22562.0)
        self.assertEqual(parsed["categories"]["Total equity"]["2026-11-11"], -18234.0)
        self.assertEqual(parsed["categories"]["Domestic"]["2026-11-04"], 3000.0)

    def test_combined_and_etf_share_flow_parser(self):
        for report in ("combined", "etf"):
            parsed = parse_ici_flow_table(load_fixture("ici_flows.html"), report)
            self.assertEqual(parsed["report"], report)
            self.assertEqual(parsed["total"]["2026-11-11"], -22562.0)

    def test_mmf_table_parses(self):
        parsed = parse_ici_mmf_table(load_fixture("ici_mmf.html"))
        self.assertEqual(parsed["unit"], "USD billions")
        self.assertEqual(parsed["dates"], ["2026-11-04", "2026-11-11", "2026-11-18"])
        self.assertEqual(parsed["total"]["2026-11-18"], 7976.44)
        self.assertEqual(parsed["total_change"], -3.58)  # $ Change 列，括号为负

    def test_bad_table_raises(self):
        with self.assertRaises(SchemaError):
            parse_ici_flow_table(load_fixture("ici_bad.html"), "flows")
        with self.assertRaises(SchemaError):
            parse_ici_mmf_table(load_fixture("ici_bad.html"))

    def test_fetch_ici_full_overwrite(self):
        # 四页全取：flows/combined/etf 各 5 周 + mmf 3 周 = 18 行；
        # 最近 5 周全量覆盖（整体覆写，不追加）。
        fixtures = {
            "flows": load_fixture("ici_flows.html"),
            "combined": load_fixture("ici_flows.html"),
            "etf": load_fixture("ici_flows.html"),
            "mmf": load_fixture("ici_mmf.html"),
        }

        def fake_http(url, headers=None, timeout=None):
            for report, page_url in fetch_all.ICI_URLS.items():
                if url == page_url:
                    return 200, fixtures[report].encode("utf-8")
            raise AssertionError(f"未预期的 URL：{url}")

        with mock.patch.object(fetch_all, "http_request", side_effect=fake_http):
            payload = fetch_all.fetch_ici()
        self.assertEqual(len(payload["rows"]), 5 + 5 + 5 + 3)
        flows_latest = [
            r for r in payload["rows"]
            if r["report"] == "flows" and r["date"] == "2026-11-11"
        ][0]
        self.assertEqual(flows_latest["total"], -22562.0)
        mmf_latest = [
            r for r in payload["rows"]
            if r["report"] == "mmf" and r["date"] == "2026-11-18"
        ][0]
        self.assertEqual(mmf_latest["total"], 7976.44)
        self.assertEqual(mmf_latest["change_vs_prior_week"], -3.58)
        self.assertTrue(all("date" in r for r in payload["rows"]))

    def test_fetch_ici_one_page_fails_whole_source(self):
        # 任一页失败，整源失败（不写半份数据）。
        def fake_http(url, headers=None, timeout=None):
            if url == fetch_all.ICI_URLS["mmf"]:
                raise FetchError("HTTP 403 https://www.ici.org/research/stats/mmf")
            return 200, load_fixture("ici_flows.html").encode("utf-8")

        with mock.patch.object(fetch_all, "http_request", side_effect=fake_http):
            with self.assertRaises(FetchError):
                fetch_all.fetch_ici()


class TestSoSoValueV1(unittest.TestCase):
    def test_v1_array_parses_and_skips_null_today(self):
        rows = parse_sosovalue_v1_json(load_json_fixture("sosovalue_v1.json"))
        # 当日行（2026-11-24，total_net_inflow 为 null）被跳过，不记 0
        self.assertEqual(
            rows,
            [
                {
                    "date": "2026-11-20",
                    "net_flow_usd": 123000000.0,
                    "total_value_traded_usd": 1800000000.0,
                    "total_net_assets_usd": 144000000000.0,
                    "cum_net_inflow_usd": 58569700000.0,
                },
                {
                    "date": "2026-11-23",
                    "net_flow_usd": -569700000.0,
                    "total_value_traded_usd": 2100000000.0,
                    "total_net_assets_usd": 145000000000.0,
                    "cum_net_inflow_usd": 58000000000.0,
                },
            ],
        )

    def test_v1_error_code_raises(self):
        with self.assertRaises(SchemaError):
            parse_sosovalue_v1_json(load_json_fixture("sosovalue_v1_err.json"))

    def test_v1_all_null_rows_raises(self):
        with self.assertRaises(SchemaError):
            parse_sosovalue_v1_json(
                [{"date": "2026-11-24", "total_net_inflow": None,
                  "total_value_traded": 1, "total_net_assets": None,
                  "cum_net_inflow": None}]
            )

    def test_v1_missing_flow_field_raises(self):
        with self.assertRaises(SchemaError):
            parse_sosovalue_v1_json([{"date": "2026-11-23"}])


class TestSoSoValueFallback(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            os.environ, {"SOSOVALUE_API_KEY": "test-key-not-real"}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_v1_success_uses_v1_and_never_calls_v2(self):
        v1_fixture = load_json_fixture("sosovalue_v1.json")
        with mock.patch.object(
            fetch_all, "get_json", return_value=v1_fixture
        ) as mock_get:
            with mock.patch.object(
                fetch_all, "post_json",
                side_effect=AssertionError("v1 成功时不应调用 v2"),
            ):
                payload = fetch_sosovalue("BTC")
        called_url = mock_get.call_args[0][0]
        self.assertIn("openapi.sosovalue.com/openapi/v1/etfs/summary-history", called_url)
        self.assertIn("symbol=BTC", called_url)
        self.assertIn("country_code=US", called_url)
        self.assertEqual(payload["route"]["version"], "v1")
        self.assertEqual(payload["route"]["host"], "openapi.sosovalue.com")
        self.assertEqual(len(payload["rows"]), 2)  # 当日 null 行已跳过

    def test_v1_http_failure_falls_back_to_v2(self):
        v2_fixture = load_json_fixture("sosovalue_a.json")
        with mock.patch.object(
            fetch_all, "get_json", side_effect=FetchError("HTTP 401 v1")
        ):
            with mock.patch.object(
                fetch_all, "post_json", return_value=v2_fixture
            ) as mock_post:
                payload = fetch_sosovalue("ETH")
        called_body = mock_post.call_args[0][1]
        self.assertEqual(called_body, {"type": "us-eth-spot"})
        self.assertEqual(payload["route"]["version"], "v2")
        self.assertEqual(payload["route"]["host"], "api.sosovalue.xyz")
        self.assertEqual(payload["rows"][0]["date"], "2026-11-20")

    def test_v1_all_null_falls_back_to_v2(self):
        all_null = [{"date": "2026-11-24", "total_net_inflow": None,
                     "total_value_traded": 1, "total_net_assets": None,
                     "cum_net_inflow": None}]
        with mock.patch.object(fetch_all, "get_json", return_value=all_null):
            with mock.patch.object(
                fetch_all, "post_json",
                return_value=load_json_fixture("sosovalue_a.json"),
            ):
                payload = fetch_sosovalue("BTC")
        self.assertEqual(payload["route"]["version"], "v2")

    def test_both_fail_raises(self):
        with mock.patch.object(
            fetch_all, "get_json", side_effect=FetchError("HTTP 401 v1")
        ):
            with mock.patch.object(
                fetch_all, "post_json", side_effect=FetchError("HTTP 401 v2")
            ):
                with self.assertRaises(FetchError) as ctx:
                    fetch_sosovalue("BTC")
        self.assertIn("v1", str(ctx.exception))
        self.assertIn("v2", str(ctx.exception))


class TestEdgarCik(unittest.TestCase):
    def test_hardcoded_ciks(self):
        # 校正清单第 4 项：CIK 硬编码（SEC company_tickers.json 核对），
        # 6 家全在表里时 _resolve_ciks 不联网直接返回。
        self.assertEqual(
            _resolve_ciks(),
            {
                "CRWV": "0001769628",
                "ORCL": "0001341439",
                "MSFT": "0000789019",
                "GOOGL": "0001652044",
                "META": "0001326801",
                "AMZN": "0001018724",
            },
        )

    def test_all_companies_covered(self):
        mapping = _resolve_ciks()
        for company in fetch_all.AI_CHAIN_COMPANIES:
            self.assertIn(company["ticker"], mapping)
            self.assertRegex(mapping[company["ticker"]], r"^\d{10}$")

    def test_known_caveats_untouched(self):
        # KNOWN_CAVEATS 三条人工复核提示保留原样
        self.assertEqual(
            set(fetch_all.KNOWN_CAVEATS),
            {"ORCL", "CRWV", "GOOGL"},
        )
        self.assertIn("11.363", fetch_all.KNOWN_CAVEATS["ORCL"])
        self.assertIn("75.2", fetch_all.KNOWN_CAVEATS["GOOGL"])


class _FakeResponse:
    """urllib 响应的最小替身（with 语句 + status + read）。"""

    def __init__(self, status=200, body=b"<html>ok</html>"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]


class TestProbe(unittest.TestCase):
    def test_urllib_tier_records_status_elapsed_egress(self):
        egress = {"ip": "1.2.3.4", "asn": "AS12345 Example"}
        with mock.patch.object(
            probe.urllib.request, "urlopen",
            return_value=_FakeResponse(status=200, body=b"x" * 100),
        ):
            result = probe.probe_urllib(
                "https://example.invalid/", probe.PLAIN_HEADERS, 5, egress,
                "1-urllib-plain",
            )
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["egress_ip"], "1.2.3.4")
        self.assertEqual(result["egress_asn"], "AS12345 Example")
        self.assertEqual(result["content_bytes"], 100)
        self.assertIsInstance(result["elapsed_ms"], int)
        self.assertIsNone(result["error"])

    def test_urllib_tier_records_http_error_status(self):
        import urllib.error

        egress = {"ip": "未能获取", "asn": "未能获取"}
        with mock.patch.object(
            probe.urllib.request, "urlopen",
            side_effect=urllib.error.HTTPError(
                "https://example.invalid/", 403, "Forbidden", {}, None
            ),
        ):
            result = probe.probe_urllib(
                "https://example.invalid/", probe.BROWSER_HEADERS, 5, egress,
                "2-urllib-browser-headers",
            )
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(result["error"], "HTTP 403")

    def test_curl_cffi_missing_degrades_gracefully(self):
        with mock.patch.dict(sys.modules, {"curl_cffi": None}):
            result = probe.probe_curl_cffi("https://example.invalid/", 5, {})
        self.assertFalse(result["available"])
        self.assertIsNone(result["http_status"])
        self.assertIn("未安装", result["note"])

    def test_playwright_missing_degrades_gracefully(self):
        with mock.patch.dict(
            sys.modules, {"playwright": None, "playwright.sync_api": None}
        ):
            result = probe.probe_playwright("https://example.invalid/", 5, {})
        self.assertFalse(result["available"])
        self.assertIsNone(result["http_status"])
        self.assertIn("未安装", result["note"])

    def test_probe_url_has_four_tiers(self):
        egress = {"ip": "1.2.3.4", "asn": "AS12345 Example"}
        with mock.patch.object(
            probe.urllib.request, "urlopen",
            return_value=_FakeResponse(status=200, body=b"ok"),
        ):
            with mock.patch.dict(sys.modules, {"curl_cffi": None}):
                with mock.patch.dict(
                    sys.modules,
                    {"playwright": None, "playwright.sync_api": None},
                ):
                    result = probe.probe_url(
                        "https://example.invalid/", 5, egress
                    )
        self.assertEqual(
            [t["tier"] for t in result["tiers"]],
            ["1-urllib-plain", "2-urllib-browser-headers",
             "3-curl_cffi", "4-playwright"],
        )
        self.assertTrue(result["tiers"][0]["available"])
        self.assertTrue(result["tiers"][1]["available"])
        self.assertFalse(result["tiers"][2]["available"])
        self.assertFalse(result["tiers"][3]["available"])


if __name__ == "__main__":
    unittest.main()
