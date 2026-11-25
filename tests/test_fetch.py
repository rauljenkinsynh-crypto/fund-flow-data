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
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import fetch_all  # noqa: E402
import probe  # noqa: E402
from fetch_all import (  # noqa: E402
    FetchError,
    SchemaError,
    _resolve_ciks,
    decide_exit_code,
    fetch_sosovalue,
    parse_companyfacts_json,
    parse_edgar_search_json,
    parse_farside_html,
    parse_fred_json,
    parse_ici_flow_table,
    parse_ici_mmf_table,
    parse_number,
    parse_sosovalue_json,
    parse_sosovalue_v1_json,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return fh.read()


def load_json_fixture(name):
    return json.loads(load_fixture(name))


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
        rows = parse_farside_html(load_fixture("farside_btc.html"))
        self.assertEqual(len(rows), 2)  # 'Not a date' 行被丢弃
        self.assertEqual(rows[0]["date"], "2026-11-20")
        self.assertEqual(rows[0]["total"], 100.0)
        self.assertEqual(rows[1]["date"], "2026-11-23")
        self.assertEqual(rows[1]["total"], -569.7)  # 括号负数
        self.assertEqual(rows[1]["funds"]["IBIT"], -509.3)
        self.assertEqual(rows[1]["funds"]["FBTC"], 12.5)

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
