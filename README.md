# 资金流仪表盘 — GitHub Actions 定时取数

本目录是仪表盘（Space slug `zi-jin-liu-jian-kong-yi-biao-pan`）v1 半自动项的
全自动取数方案：GitHub Actions 定时跑脚本取数，写进数据仓库的 `data/` 目录，
Space 服务端改读仓库 raw JSON 即可自动更新。不连仓库、不碰 live Space、
**不含任何 key 值**——本目录任何文件里都没有 key，key 只存在于仓库 Secrets。

> 未验证声明：Actions 出口能否绕过 SoSoValue / Farside / ICI 的反爬墙，
> **未经实测，本文件不宣称已绕过**。首次实测结果以运行后 `data/status.json` 为准。
> 每个未验证点在脚本顶部 CONFIG 区与下表里都明确标注。

## 仓库结构

```
<数据仓库根目录>/
├── .github/workflows/fund-flow-fetch.yml   # 从本目录 github-actions/.github/ 复制过去
├── scripts/fetch_all.py                   # 取数脚本（只用 Python 标准库）
├── data/                                  # 输出：每源一个 JSON + status.json
│   ├── ici.json
│   ├── farside-btc.json
│   ├── farside-eth.json
│   ├── sosovalue-btc.json
│   ├── sosovalue-eth.json
│   ├── fred-dgs10.json
│   ├── ai-chain.json
│   ├── etf-flows.json
│   └── status.json
└── tests/                                 # 离线测试（见 tests/TESTS.md）
```

本目录（`github-actions/`）内对照结构：

```
github-actions/
├── .github/workflows/fund-flow-fetch.yml
├── scripts/fetch_all.py
├── tests/
│   ├── test_fetch.py
│   ├── fixtures/        # 最小 fixture（文件头注明“fixture，非真实数据”）
│   └── TESTS.md         # 测试记录（每项：测了什么、怎么测、通过/未通过）
├── data/.gitkeep        # 空目录占位，首次运行后由脚本写满
└── README.md            # 本文件
```

## Secrets 清单（只写名字，不写值）

| Secret 名 | 用途 | 状态 |
|---|---|---|
| `SOSOVALUE_API_KEY` | SoSoValue API（BTC/ETH ETF 流量） | 已在 Space 服务端有存量，接入时由 parent 复制进仓库 Secrets |
| `FRED_API_KEY` | FRED API（DGS10，10 年期收益率） | 已在 Space 服务端有存量，接入时由 parent 复制进仓库 Secrets |

**key 的传递规则：由 parent 从 Space 服务端配置复制进仓库 Settings → Secrets → Actions，绝不经聊天明文传递，绝不写进任何文件。** 脚本只从环境变量读这两个名字；缺 key 时该源记 `failed`（错误写“缺少 Secret …”），不阻塞其他源。

其余源不需要 key：Farside（公开 HTML）、EDGAR companyfacts 与全文检索（免 key，但脚本按 SEC 要求带 User-Agent）。

## 部署步骤

1. **准备数据仓库**（本机 GitHub 未连接，需用户浏览器授权 + 安装 Muse GitHub App 并选仓库后才能评估；本步骤由 parent/用户操作，本搭建员不连仓库）：
   - 新建或选定一个公开仓库（Space 读 raw JSON，须公开；或按 Space 侧的读取方式定）。
2. **复制文件**：
   ```bash
   cp -r github-actions/.github <数据仓库根目录>/
   cp -r github-actions/scripts github-actions/tests <数据仓库根目录>/
   mkdir -p <数据仓库根目录>/data
   ```
3. **建 Secrets**：仓库 Settings → Secrets and variables → Actions → New repository secret，建上面两个 Secret（值由 parent 从 Space 服务端配置复制，见上表）。
4. **首次手动触发**：仓库 Actions → fund-flow-fetch → Run workflow（sources 留 `all`），跑完检查：
   - `data/status.json` 里每源的 status；
   - 有 `failed`/`blocked` 的源看 `error` 字段原文；
   - 确认 `data/` 有提交（commit 信息 `data: fund-flow fetch … [skip ci]`）。
5. **Space 接线**（另行安排，不在本任务内）：Space 服务端改读该仓库 `data/*.json` 的 raw 地址，按 `status.json` 的 `last_ok` 判断新鲜度，失败时显示旧数 + “已过期”标记。

## 定时表（workflow 内已配，cron 一律 UTC）

| 触发 | UTC | 对应 ET | 取数内容 |
|---|---|---|---|
| 每周三 `5 15 * * 3` | 15:05 | 10:05 EST / 11:05 EDT | ICI 周报（发布 10:00 ET 之后） |
| 每周二至周六 `0 2 * * 2-6` | 02:00 | 周一至周五 21:00 EST / 22:00 EDT | Farside / SoSoValue（收盘后，T+1 口径） |
| 每周日 `0 13 * * 0` | 13:00 | 08:00 EST / 09:00 EDT | EDGAR（AI 资金链，每周一次） |
| workflow_dispatch | 手动 | — | 可指定只跑部分源（`sources` 输入） |

每次触发都跑全部源（幂等）：各源独立写各自文件，任一源失败不阻塞其他源。

## 各源状态表

| 源 | 输出文件 | 口径依据 | 状态 |
|---|---|---|---|
| ICI 周报 | `data/ici.json` | 四页确切 URL（`scripts/fetch_all.py` 的 `ICI_URLS`），HTML 表格；流量单位百万美元、货币基金单位十亿美元；每次重取最近 5 周全量覆盖 | 解析逻辑已离线测；**未验证**：联网实测、反爬（Akamai 403 风险最高）等 Actions 首次运行；本机沙箱实测四页均 403（见 `tests/TESTS.md` §5），不代表 Actions 出口结论 |
| Farside BTC | `data/farside-btc.json` | `https://farside.co.uk/btc/`，HTML 表格 | 解析逻辑已离线测；**未验证**：联网实测、反爬、单位（假设百万美元）等 Actions 首次运行 |
| Farside ETH | `data/farside-eth.json` | `https://farside.co.uk/eth/`，HTML 表格 | 同上 |
| SoSoValue BTC | `data/sosovalue-btc.json` | v1 优先 `GET openapi.sosovalue.com/openapi/v1/etfs/summary-history`，v1 失败转 v2 `POST api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart`，头 `x-soso-api-key`；实际走通的路线（主机 + 版本）记在 `status.json` 的 `route` 字段 | 解析逻辑与 v1→v2 切换已离线测（mock）；**未验证**：两主机在 Actions 出口的实际通断、返回行数与窗口等首次运行 |
| SoSoValue ETH | `data/sosovalue-eth.json` | 同上，ETH 各取一次 | 同上 |
| FRED DGS10 | `data/fred-dgs10.json` | GET `…/fred/series/observations?series_id=DGS10&…&file_type=json` | 解析逻辑已离线测；**未验证**：联网实测等 Actions 首次运行 |
| AI 资金链 | `data/ai-chain.json` | EDGAR companyfacts（6 家：CRWV/ORCL/MSFT/GOOGL/META/AMZN，CIK 以 `EDGAR_CIK_BY_TICKER` 硬编码为准，ticker 动态解析只作备用）+ 全文检索兜底发债事件 | 解析逻辑已离线测；**未验证**：联网实测、全文检索端点、发债事件命中率等 Actions 首次运行 |
| ETF.com | `data/etf-flows.json` | 无公开 API（v1 结论） | **未验证**：脚本内 `ETF_COM_URL = None`，未配置跳过；取数方式待核查员报告，可能长期保持未配置 |
| ETF 流量（第一阶段） | `data/etf-flows.json` + `data/etf-selfcalc-snapshots.json`（快照累积） | TickerInside `series.json`（7 只：SOXX/SPY/XLP/XLV/XLE/XLU/XLK，null 行保留“未测得”，滚动 60 日）+ 发行商文件自算（流量 = NAV × 份额变化；BlackRock v1 CSV：TLT/SOXX，SSGA 美国版基金页：GLD/SPY/XLP/XLV/XLE/XLU/XLK；拆股按干净倍数折算，大额申赎照常算；首日流量 null）+ Farside IBIT；VOO/QQQ/SMH 本阶段记 seed；滞后 >3 个交易日自动切自算备用 | 解析与合并逻辑已离线测（mock）；**未验证**：Actions 出口对 BlackRock/SSGA/TickerInside 的实际通断、SSGA 精确 NAV 形态、flow_pct_of_assets 量级（首跑锚点核对）等首次运行；本机沙箱实测 TickerInside 与 BlackRock 通、SSGA 全 403（见 `tests/TESTS.md` §6），不代表 Actions 出口结论 |
| 价格（Yahoo） | —（不在此取） | 已在 Space 服务端自动跑 | 不在 Actions 里重复取价格 |

“已离线测”= `tests/test_fetch.py` 22 项全过（见 `tests/TESTS.md`）；
“未验证”= 须等仓库接通后 Actions 首次实测，任何一项都不得提前宣称已通。

## 输出格式与失败处理

- 每个 `data/<源>.json` 含 `fetched_at`（UTC，`2026-11-25T04:37:09Z` 格式）、
  `source_url`、`rows`（每行至少含 `date`；AI 资金链的 rows 为 6 家公司记录）。
  FRED 的 `source_url` 已把 key 抹成 `<redacted>`，日志与文件里永不出现 key 值。
- `data/status.json`：每源 `{status: ok/failed/blocked, error, last_ok, fetched_at}`。
  `blocked` 含两种：未配置（ETF.com，跳过、不计退出码）与 HTTP 403 反爬拦截（计入失败）。
  SoSoValue 两源成功时另含 `route: {host, version, endpoint, url}`，记录本次实际走通的主机 + 版本（v1 优先、v2 备选）。
- 任一源失败不阻塞其他源；失败时保留上一次的好文件（不覆写），status 里标 `stale: true`（有旧文件可 fallback 时）。
- 结构校验：返回结构与预期不符时抛错、只写失败状态，**绝不猜数**；数值缺失记 `null`，不编 0。
- 脚本退出码：`0` = 本次尝试的源全部成功；`2` = 部分成功、部分失败/被挡；`1` = 本次尝试的源全部失败。
  workflow 按原退出码结束运行（部分/全部失败时运行标红，是待修信号，不是事故）。

## 本地自查（不联网）

```bash
cd github-actions
python3 -m py_compile scripts/fetch_all.py scripts/probe.py
python3 -m unittest discover -s tests -v
# 只看未配置源的跳过行为（应退出 0，status 全 blocked）：
python3 scripts/fetch_all.py --only etf-flows --out-dir /tmp/ff-smoke
python3 scripts/fetch_all.py --help
# 首跑逐源验证（四档递进探测，只测可达性，不取业务数据、不写 data/）：
python3 scripts/probe.py https://www.ici.org/research/stats/flows
```

## 待总协调校正的点（2026-11-25 校正清单已落地 1/2/4，剩余如下）

1. ~~ICI：填 `ICI_URL` + 补 `parse_ici_*` 校验函数~~ — **已落地**（`ICI_URLS` 四页 + `parse_ici_flow_table` / `parse_ici_mmf_table`，45 项测试全过；本机沙箱实测四页均 Akamai 403，见 `tests/TESTS.md` §5）。
2. ETF.com：填取数方式（或确认长期不接，`ETF_COM_URL` 保持 `None`）。
3. ~~SoSoValue：按首次实测的真实请求体/返回字段校正 `SOSOVALUE_BODY` 与 `parse_sosovalue_json`~~ — **已落地**（v1 优先、v2 备选 + `route` 记录；两主机在 Actions 出口的实际通断仍待首跑实测）。
4. Farside：按实测校对表格数值单位（`unit_assumption` 字段）。
5. ~~EDGAR 全文检索：按实测确认 `efts.sec.gov/LATEST/search-index` 的查询参数与命中率~~ — CIK 硬编码部分**已落地**（`EDGAR_CIK_BY_TICKER`）；全文检索端点本身仍待实测确认。
