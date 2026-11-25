# 离线测试记录（tests/TESTS.md）

测试时间：2026-11-25（UTC 约 05:00，本地跑；2026-11-25 校正清单 1/2/4 落地后复测）。
测试命令（在 `github-actions/` 目录下）：
```bash
python3 -m py_compile scripts/fetch_all.py scripts/probe.py
python3 -m unittest discover -s tests -v
python3 scripts/fetch_all.py --only etf-flows --out-dir /tmp/ff-smoke
python3 scripts/fetch_all.py --help
python3 scripts/probe.py https://example.com --timeout 15
```

## 1. 单元测试（解析逻辑，离线）— 45 项，全部通过

| # | 测了什么 | 怎么测 | 结果 |
|---|---|---|---|
| 1 | `parse_number` 普通/格式化数字 | `"1,234.5"`、`"$509.3"`、`42` | 通过 |
| 2 | `parse_number` 括号为负数 | `"(509.3)"` → `-509.3` | 通过 |
| 3 | `parse_number` 缺失值不编 0 | `"-"`、`"."`、`"—"`、`""`、`None`、`"n/a"` → 均为 `None` | 通过 |
| 4 | Farside 表格解析 | fixture `farside_btc.html`（Date/IBIT/FBTC/Total，含括号负数与 `-`） | 通过：2 行有效（非日期行丢弃），2026-11-23 total `-569.7`、IBIT `-509.3` |
| 5 | Farside 无有效表格 | fixture `farside_bad.html`（无表格） | 通过：抛 `SchemaError` |
| 6 | SoSoValue 形态 A | fixture `sosovalue_a.json`（`{code:0, data:{list:[...]}}`） | 通过：2 行按日期排序 |
| 7 | SoSoValue 形态 B | fixture `sosovalue_b.json`（`{data:[...]}`，含毫秒时间戳与字符串数字） | 通过：时间戳转 `2026-11-19`，字符串数字正常解析 |
| 8 | SoSoValue 错误码 | fixture `sosovalue_err.json`（`code:400101`） | 通过：抛 `SchemaError` |
| 9 | SoSoValue 未知结构 | `{"data": {"unexpected": true}}` | 通过：抛 `SchemaError` |
| 10 | FRED observations 解析 | fixture `fred.json`（3 行，含 `"."`） | 通过：`"."` → `null`，不编 0 |
| 11 | FRED 缺 observations | fixture `fred_bad.json` | 通过：抛 `SchemaError` |
| 12 | companyfacts 微软最新 10-Q | fixture `edgar_companyfacts_msft.json` | 通过：OCF $58.000B、capex $39.367B、capex/OCF 67.9%、净债务 −$29.028B（净现金），只取 10-Q 不取旧 10-K |
| 13 | companyfacts 空 facts | fixture `edgar_companyfacts_empty.json` | 通过：数值全 `None` + 留 `notes`，不崩溃 |
| 14 | companyfacts 缺 facts | `{"cik": "1"}` | 通过：抛 `SchemaError` |
| 15 | EDGAR 全文检索命中 | fixture `edgar_search.json` | 通过：1 条，form `8-K`、accession 正确 |
| 16 | EDGAR 全文检索 0 命中 | fixture `edgar_search_empty.json` | 通过：返回 `[]`，不是失败 |
| 17 | EDGAR 全文检索缺 hits | `{}` | 通过：抛 `SchemaError` |
| 18 | 退出码：全成功 | `decide_exit_code(8, 8)` | 通过：`0` |
| 19 | 退出码：部分失败 | `decide_exit_code(5, 8)` | 通过：`2` |
| 20 | 退出码：全失败 | `decide_exit_code(0, 8)` | 通过：`1` |
| 21 | 退出码：0 次尝试 | `decide_exit_code(0, 0)` | 通过：`0` |
| 22 | ICI 四页 URL 已填 + ETF.com 仍未配置 | 断言 `ICI_URLS` 为四页确切 URL，`ETF_COM_URL is None`（旧断言 `ICI_URL is None` 随本次修正失效，特此改写，见 `test_fetch.py` 注释） | 通过 |
| 23 | ICI 长期流量表解析 | fixture `ici_flows.html`（5 个周三列，Total 行） | 通过：5 周，最新周 2026-11-11 Total `-22,562`（百万美元） |
| 24 | ICI 综合/ETF 表共用流量解析 | 同 fixture，report 分别为 `combined` / `etf` | 通过：Total 一致 |
| 25 | ICI 货币基金表解析 | fixture `ici_mmf.html`（3 日期 + `$ Change` 列） | 通过：最新周 Total `7,976.44`（十亿美元），`$ Change` `-3.58` |
| 26 | ICI 表结构对不上 | fixture `ici_bad.html`（无日期列、无 Total 行） | 通过：流量/mff 解析均抛 `SchemaError` |
| 27 | `fetch_ici` 四页全量覆盖 | mock `http_request` 按 URL 返回对应 fixture | 通过：18 行（5+5+5+3），flows 最新周 Total `-22,562`，mmf 最新周 Total `7,976.44` + change `-3.58`，每行含 `date` |
| 28 | `fetch_ici` 一页失败则整源失败 | mock `http_request`，mmf 页抛 `FetchError` | 通过：抛 `FetchError`（不写半份数据） |
| 29 | SoSoValue v1 数组解析 + 当日 null 行跳过 | fixture `sosovalue_v1.json`（2026-11-24 行全为 null） | 通过：只剩 2 行已结算行，不记 0；字段 `total_net_inflow` 等齐全 |
| 30 | SoSoValue v1 错误码 | fixture `sosovalue_v1_err.json`（`code:400101`） | 通过：抛 `SchemaError` |
| 31 | SoSoValue v1 全部行 null | 单行全 null 数组 | 通过：抛 `SchemaError` |
| 32 | SoSoValue v1 缺 `total_net_inflow` 字段 | 行无该字段 | 通过：抛 `SchemaError` |
| 33 | v1 成功走 v1、不调 v2 | mock `get_json` 返回 v1 fixture，`post_json` 设为断言失败 | 通过：URL 含 `symbol=BTC&country_code=US`，route 为 v1 / `openapi.sosovalue.com` |
| 34 | v1 HTTP 失败 → v2 备选 | mock `get_json` 抛 `FetchError`，`post_json` 返回形态 A fixture | 通过：v2 请求体 `{"type": "us-eth-spot"}`，route 为 v2 / `api.sosovalue.xyz` |
| 35 | v1 全 null → v2 备选 | mock `get_json` 返回全 null 数组 | 通过：route 为 v2 |
| 36 | v1/v2 双失败 | 两 mock 均抛 `FetchError` | 通过：抛 `FetchError`，错误含 v1 与 v2 |
| 37 | EDGAR CIK 硬编码命中 | `_resolve_ciks()`（6 家全在表里，不联网） | 通过：6 个 CIK 与核查报告 §5.5 一致（CRWV 0001769628、ORCL 0001341439、MSFT 0000789019、GOOGL 0001652044、META 0001326801、AMZN 0001018724） |
| 38 | 6 家公司全被 CIK 覆盖 | 逐家核对 `AI_CHAIN_COMPANIES` | 通过：均为 10 位数字 |
| 39 | KNOWN_CAVEATS 原样保留 | 核对三条提示键与关键数字 | 通过：ORCL `11.363`、GOOGL `75.2` 均在 |
| 40 | probe 第 1/2 档记录状态/耗时/出口 | mock `urlopen` 返回 200 | 通过：状态 200、出口 IP/ASN、字节数、耗时均记录 |
| 41 | probe 第 2 档记录 HTTP 错误状态 | mock `urlopen` 抛 403 `HTTPError` | 通过：`http_status` 403、`error` 为 `HTTP 403` |
| 42 | probe curl_cffi 未装优雅降级 | `sys.modules` 置空 `curl_cffi` | 通过：`available` false，note 注明未安装 |
| 43 | probe Playwright 未装优雅降级 | `sys.modules` 置空 `playwright` | 通过：`available` false，note 注明未安装 |
| 44 | probe 单 URL 跑满四档 | mock `urlopen` + 置空两可选依赖 | 通过：四档名与顺序正确，前两档可用、后两档跳过 |
运行结果：`Ran 45 tests in 0.023s — OK`（Python 3.12.3）。
计数：原 1–21 项不变 + 第 22 项改写为 2 项（ICI URL 核对、ETF.com 仍未配置）+ 新增 22 项（ICI 6、SoSoValue v1 4、v1→v2 切换 4、CIK 3、probe 5）= 45 项。旧第 22 项（`ICI_URL is None`）因 ICI 填 URL 而改写，不是删除。

| # | 测了什么 | 怎么测 | 结果 |
|---|---|---|---|
| 1 | `parse_number` 普通/格式化数字 | `"1,234.5"`、`"$509.3"`、`42` | 通过 |
| 2 | `parse_number` 括号为负数 | `"(509.3)"` → `-509.3` | 通过 |
| 3 | `parse_number` 缺失值不编 0 | `"-"`、`"."`、`"—"`、`""`、`None`、`"n/a"` → 均为 `None` | 通过 |
| 4 | Farside 表格解析 | fixture `farside_btc.html`（Date/IBIT/FBTC/Total，含括号负数与 `-`） | 通过：2 行有效（非日期行丢弃），2026-11-23 total `-569.7`、IBIT `-509.3` |
| 5 | Farside 无有效表格 | fixture `farside_bad.html`（无表格） | 通过：抛 `SchemaError` |
| 6 | SoSoValue 形态 A | fixture `sosovalue_a.json`（`{code:0, data:{list:[...]}}`） | 通过：2 行按日期排序 |
| 7 | SoSoValue 形态 B | fixture `sosovalue_b.json`（`{data:[...]}`，含毫秒时间戳与字符串数字） | 通过：时间戳转 `2026-11-19`，字符串数字正常解析 |
| 8 | SoSoValue 错误码 | fixture `sosovalue_err.json`（`code:400101`） | 通过：抛 `SchemaError` |
| 9 | SoSoValue 未知结构 | `{"data": {"unexpected": true}}` | 通过：抛 `SchemaError` |
| 10 | FRED observations 解析 | fixture `fred.json`（3 行，含 `"."`） | 通过：`"."` → `null`，不编 0 |
| 11 | FRED 缺 observations | fixture `fred_bad.json` | 通过：抛 `SchemaError` |
| 12 | companyfacts 微软最新 10-Q | fixture `edgar_companyfacts_msft.json` | 通过：OCF $58.000B、capex $39.367B、capex/OCF 67.9%、净债务 −$29.028B（净现金），只取 10-Q 不取旧 10-K |
| 13 | companyfacts 空 facts | fixture `edgar_companyfacts_empty.json` | 通过：数值全 `None` + 留 `notes`，不崩溃 |
| 14 | companyfacts 缺 facts | `{"cik": "1"}` | 通过：抛 `SchemaError` |
| 15 | EDGAR 全文检索命中 | fixture `edgar_search.json` | 通过：1 条，form `8-K`、accession 正确 |
| 16 | EDGAR 全文检索 0 命中 | fixture `edgar_search_empty.json` | 通过：返回 `[]`，不是失败 |
| 17 | EDGAR 全文检索缺 hits | `{}` | 通过：抛 `SchemaError` |
| 18 | 退出码：全成功 | `decide_exit_code(8, 8)` | 通过：`0` |
| 19 | 退出码：部分失败 | `decide_exit_code(5, 8)` | 通过：`2` |
| 20 | 退出码：全失败 | `decide_exit_code(0, 8)` | 通过：`1` |
| 21 | 退出码：0 次尝试 | `decide_exit_code(0, 0)` | 通过：`0` |
| 22 | ICI/ETF.com 未配置锁定 | 断言 `fetch_all.ICI_URL is None` 且 `ETF_COM_URL is None` | 通过：防止凭猜测写 URL |

运行结果：`Ran 22 tests in 0.005s — OK`（Python 3.12.3）。

## 2. 脚本级冒烟（不联网）— 均符合设计

| # | 测了什么 | 怎么测 | 结果 |
|---|---|---|---|
| S1 | 未配置源跳过 | `--only etf-flows --out-dir /tmp/ff-smoke`（ICI 已填 URL，不再是未配置源，故 smoke 只跑 etf-flows） | 通过：退出码 `0`，`status.json` 的 etf-flows 为 `blocked`，未写数据文件 |
| S2 | 缺 key 的源失败 | `--only fred-dgs10`（无 `FRED_API_KEY` 环境变量） | 通过：退出码 `1`，`fred-dgs10` 记 `failed`（错误“缺少 Secret FRED_API_KEY”），未写数据文件，`status.json` 保留 ici/etf-flows 的旧条目 |
| S3 | 未知源参数 | `--only bogus` | 通过：退出码 `1`，打印可选源列表 |
| S4 | `--help` 不崩 | `python3 scripts/fetch_all.py --help`、`python3 scripts/probe.py --help` | 通过：退出码均为 `0` |
| S5 | probe 四档实测（无墙站） | `python3 scripts/probe.py https://example.com --timeout 15`（本机沙箱出口） | 通过：第 1/2 档 200（耗时 852/482ms），出口 IP `104.29.197.140`、ASN `AS13335 Cloudflare, Inc.`；第 3/4 档因 curl_cffi / Playwright 未装而优雅跳过（available=false，注明未装） |

## 3. Workflow YAML 校验 — 通过

`python3 -c "yaml.safe_load(...)"`：解析成功；3 条 cron（`5 15 * * 3`、`0 2 * * 2-6`、`0 13 * * 0`）、
`workflow_dispatch` inputs（`sources`、`reason`）、job `fetch` 均在位。
（注：PyYAML 把顶层 `on:` 读成布尔 `True`，是 YAML 1.1 旧习性，GitHub Actions 端无此问题。）

## 4. 未测 / 未验证（须等仓库接通后 Actions 首次实测）

- 任何真实联网取数：SoSoValue v1/v2、FRED GET、Farside HTML、EDGAR companyfacts/全文检索
  ——本机未连 GitHub、未跑 Actions，一律未测；反爬能否绕过**未验证，不宣称**。
- 退出码 `2` 的端到端（一次运行里一源成功、一源失败）：单元测试只覆盖了
  `decide_exit_code` 函数本身，整条 `run_fetch` 路径上的 `2` 须等至少一源真实跑通后才测得到。
- ETF.com：URL 待核查员报告，解析函数尚未实现（脚本内明确为 `None` + `NotConfigured`）。
- Farside 表格数值单位（假设百万美元）须按实测校对。
- EDGAR 全文检索端点 `efts.sec.gov/LATEST/search-index` 的查询参数与命中率未验证。
- ICI 表结构漂移：解析按核查报告 §1.2 的当前表结构写（日期列 + Total 行校验）；
  ICI 改版后须按首跑失败状态重新核对，脚本不做结构猜测。

## 5. 本机沙箱联网观察（不是 Actions 结论，仅记行为）

- `--only ici`（2026-11-25 05:00 UTC，本机沙箱出口）：四页全部 HTTP 403，
  Akamai `Access Denied`（`errors.edgesuite.net`，Reference #18.…），退出码 `1`，
  `status.json` 记 `failed`、未写 `ici.json`。证明两点：① ICI 不再是“未配置”，
  脚本确实发起了请求；② 失败路径如设计（整源失败、不写半份数据、不阻塞其他源）。
  **这只是本机沙箱出口的结果，不代表 GitHub Actions 出口能否通过**——Actions 首跑
  仍须用 `scripts/probe.py` 四档探测并以 `status.json` 为准。
- probe 在本机对无墙站（example.com）四档跑通（见 S5），第 3/4 档因未装依赖而跳过；
  curl_cffi / Playwright 在 Actions 运行器上同样默认未装，首跑前须在 workflow 或
  probe 调用方装好，否则第 3/4 档只能跳过（不报错）。

## 6. etf-flows 第一阶段（2026-11-25，离线 29 项新增，全部通过）

测试命令（在 `github-actions/` 目录下）：`python3 -m unittest discover -s tests -v`
结果：77 项全过（原有 48 项 + 新增 29 项）。

| # | 测了什么 | 怎么测 | 结果 |
|---|---|---|---|
| 49–53 | TickerInside 解析 | fixture `tickerinside_series.json`（SOXX 11-10 null 行、11-11 锚点行） | 通过：锚点 flow 1,065,556,947 美元；null 行保留；缺一只标的抛 `SchemaError`；锚点值偏差 >20% 只警告；锚点行在窗口内缺失抛错；pct 小数/百分数换算 |
| 54–57 | BlackRock CSV 解析 | fixture `blackrock_tlt.csv` | 通过：份额 600,000,000（2026-11-24）、Σ市值 $48,894,000,000、NAV $81.49；缺份额/缺 Market Value 列抛错；Total 合计行不重复计入 |
| 58–62 | SSGA 基金页解析 | fixture `ssga_gld.html` | 通过：份额 360.40M、精确 NAV 331.98279858 优先、截至日 2026-11-24；无精确值回退表内 $331.98；欧洲格式 361,80 M 拒绝；缺 NAV 抛错；NAV Per Basket 不误认 |
| 63–68 | 自算流量 | 内联快照对 | 通过：首日流量 null；GLD 实算 361.80M→360.40M × $331.98279858 = −$464,775,918（−4.6478 亿美元）复现；2:1 拆股折算后流量 0；−11.8% 大额申赎不误判为拆股、流量 −$41.3 亿；同日重跑无流量；净值越界抛错 |
| 69–70 | daily 合并 | 内联序列 | 通过：去重 + 60 日上限 + 新行覆盖；null 行保留 |
| 71 | 两路冲突 >10% | mock 全路（today=2026-11-12，未切换） | 通过：主路 TickerInside 10.6556，`alternate_flow` ≈ 28.42，note 写“两路不一致”；daily 11-11 仍是主路数 |
| 72 | 全量合并（已切换） | mock 全路 + 临时 out_dir（today=2026-11-25） | 通过：SOXX/SPY 切自算备用 live（流量 2.0 / 7.1851）；TLT 0.8135、GLD −7.9676 live；XLP 失败保留上日数、其余 4 只失败无旧数；IBIT 2.0（Farside）；VOO/QQQ/SMH seed；weekly 4571.0（ICI）；快照文件写回 11-25 |
| 73 | 上线首日 | mock 全路 + 空 out_dir | 通过：TLT/GLD 流量 null、status unmeasured、AUM 已知；快照文件已存；整源 status seed |
| 74 | 全部失败 | mock 全路抛错 + 空 out_dir | 通过：抛 `FetchError` |
| 75–76 | weekly 回退 | mock（仅 TLT 通）+ 有/无上日文件 | 通过：无 ici.json 时沿用上日 weekly（4200.0/2026-11-11，source previous）；再无时种子值 4571.0/2026-11-18（source seed） |
| 77 | etf-flows 真实 fetcher | mock 全路抛错 | 通过：抛 `FetchError` 而非旧占位的 `NotConfigured`；`ETF_COM_URL` 仍为 None（旧断言不动） |

### 6.1 本机沙箱实跑观察（不是 Actions 结论，仅记行为）

- `python3 scripts/fetch_all.py --only etf-flows --out-dir /tmp/ff-smoke2`（2026-11-25 15:44 UTC）：退出码 0，`etf-flows.json` + `etf-selfcalc-snapshots.json` + `status.json` 均写出，`status.json` 的 etf-flows 为 ok。
- **TickerInside 本机通**：最新收盘日 2026-11-11，滞后 10 个交易日 → 切换立即生效；SOXX 11-11 实测流量 10.6556（亿美元）= $1,065,556,947，与锚点完全一致；pct 实测 2.382%（证实 `flow_pct_of_assets` 是小数口径，换算逻辑正确）；反推 AUM 447.3（亿美元）与种子值 350.0 偏差 27.8% >20% → aum 按规则记 null 并写进 notes（种子 $350 亿可能已旧，待核对）。
- **BlackRock 本机通**：TLT 实测 600,000,000 股、NAV $81.80（2026-11-24），与方案锚点（600M 股、约 $81.49–$81.88）一致；SOXX 实测 267,500,000 股、NAV $125.51（在 100–400 区间内）。
- **SSGA 本机全 403**：GLD/SPY/XLP/XLV/XLE/XLU/XLK 在本机沙箱出口均取不到（与 ICI 本机 403 同类），对应标的记 failed；**不代表 Actions 出口结论**，首跑须以 `status.json` 为准。
- 同日重跑：快照日期未前进（11-24 = 11-24），流量记 null、不重复算，TLT AUM $490.80 亿正常写出。
- 附带修复：`run_fetch` 原取 `payload["fetched_at"]`，etf-flows 按数据契约用 `fetched_at_utc`，首轮实跑报 `KeyError` 后改为两者都认（`payload.get("fetched_at") or payload.get("fetched_at_utc")`）；旧源不受影响。
- 未验证（仍须 Actions 首跑）：SSGA 在 Actions 出口的通断；SSGA 精确 NAV（`331.98279858` 式 JSON 值）在原始 HTML 里的真实形态（本机渲染文本只见到 `$331.98`，代码找不到时安全回退表内值）；XLK 的 BlackRock portfolioId——已查明 XLK 是 SPDR 产品，无 iShares portfolioId，不走 BlackRock 路（方案 §九“BlackRock 覆盖 SOXX/XLK”一说有误，见 build log）。

## 7. etf-flows 第一阶段修复（2026-11-25，离线 84 项全过）

修 run #4 线上验证发现的三问题（见 build log 2026-11-25 11:03 条目）：

| # | 测了什么 | 怎么测 | 结果 |
|---|---|---|---|
| 78–81 | SSGA 真实页面结构 | 新夹具 `ssga_gld_real.html`（实取 GLD 页的 fund-quick-info input 原样摘录） | 通过：截至日取 NAV 的 2026-11-24（不用页面日期 Nov 25）；份额由总资产/NAV 反推 360,400,000；精确 NAV 331.98279858；无任何可用净值时抛 `SchemaError`；总资产为 0 时抛错 |
| 82–85 | TickerInside headline 改快照现算 | `_ti_headline` 单元测试（SOXX 快照 267.5M 股 @ $125.51） | 通过：aum 335.74（亿美元）、pct 3.17%（不用 TickerInside 自带 pct）；无快照时 aum/pct 记 null；份额单位错（267.5 股）触发 5 倍保护记 null；同日快照不写日期备注 |
| 86 | 失败措辞 | 合并测试 `test_full_fetch_switched_to_selfcalc` 追加断言 | 通过：XLP 有上一日数据写“显示上一日数据”；XLV/XLE/XLU/XLK 无上一日数据写“回退种子数”，不出现“显示上一日数据” |
| — | 旧 `_ti_pct_to_percent` 及 `test_pct_fraction_or_percent_to_percent` | 删除（TickerInside pct 不再采信） | 全量 84 项通过（77 − 1 + 8） |

### 7.1 本机沙箱实跑观察（修复后，2026-11-25 16:03 UTC，`/tmp/etf-live-check`）

- `python3 scripts/fetch_all.py --only etf-flows` 退出码 0，整源 live。**SSGA 7 只全通**：快照 9 只齐全、截至日均为 2026-11-24——GLD 360.40M @ $331.98279858、SPY 1,071,282,116 @ $718.51354、XLP 170.12M @ $91.863632、XLV 262.82M @ $184.628443、XLE 627.75M @ $60.391542、XLU 533.50M @ $43.401394、XLK 676.06M @ $157.033224，与 build log 已核实数一致（份额为总资产/NAV 反推，与公布值偏差 <0.001%）。
- SOXX headline：aum 335.7426、pct 3.1737（快照现算，注写明快照日与流量日不同）；SPY aum 7697.3071、pct 0.5495；7 只 TickerInside 标的均有快照总资产与 pct，不再记 null。
- TLT/GLD 首日（本机无旧快照）仍 unmeasured、流量 null，符合向前累积设计。
