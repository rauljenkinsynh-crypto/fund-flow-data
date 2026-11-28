#!/usr/bin/env python3
"""SSGA 轮询哨兵：判断 SSGA 是否公布了比已存快照更新的基金数据。

用途：供 .github/workflows/fund-flow-ssga-poll.yml 的 hourly 轮询调用。
只做一件事——取 SPY 的 SSGA 基金页，解析其截至日，与
data/etf-selfcalc-snapshots.json 里已存的 SPY 快照日期比对：

- 页面截至日 > 已存快照日  -> run_fetch=true（SSGA 已公布新数据，值得跑一趟取数）
- 页面截至日 <= 已存快照日 -> run_fetch=false（还没公布，本小时跳过）
- 已存快照缺失            -> run_fetch=true（首跑或快照丢失，跑一趟）
- 取页/解析/读文件任一步失败 -> run_fetch=false（下一小时轮询重试；
  当日 02:00 UTC 的收盘全量跑仍会覆盖其他源，不会因此丢数）

只用一只 SPY 作哨兵：六只 SSGA 标的同属同一套 SSGA 发布系统，
同日数据同一时间上线；只取一页，把轮询成本压到最低。
只用 Python 标准库；不写任何数据文件，只读快照、只读 SSGA 页面。

用法：
    python3 scripts/ssga_poll_guard.py [--data-dir <目录>]
    缺省 data 目录为 <仓库根目录>/data（脚本位于 scripts/ 时自动推断）。
    在 Actions 里把 run_fetch=<true|false> 写进 $GITHUB_OUTPUT（若设置）；
    本地运行时打印 GUARD run_fetch=<true|false> 供人工判断。
    退出码恒为 0（哨兵本身不失败，失败只影响本次是否跑取数）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_all  # noqa: E402  （复用 SSGA_FUND_PAGES / parse_ssga_fund_page / http_request）


def _load_stored_spy_date(data_dir):
    """读已存快照里 SPY 的日期；缺失/损坏返回 None。"""
    path = os.path.join(data_dir, "etf-selfcalc-snapshots.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            snapshots = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"GUARD 快照文件读不到（{path}）：{exc}")
        return None
    if not isinstance(snapshots, dict):
        return None
    snap = snapshots.get("SPY") or {}
    return snap.get("date") or None


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="SSGA 轮询哨兵（只读判断，不写数据）。")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="快照所在 data 目录，缺省为 <仓库根目录>/data。",
    )
    args = parser.parse_args(argv)
    if args.data_dir:
        data_dir = args.data_dir
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(script_dir)
        data_dir = os.path.join(repo_root, "data")

    stored_date = _load_stored_spy_date(data_dir)
    print(f"GUARD 已存 SPY 快照日：{stored_date}")

    run_fetch = True
    reason = ""
    try:
        url = fetch_all.SSGA_FUND_PAGES["SPY"]
        _status, body = fetch_all.http_request(url)  # http_request 返回 (status, body_bytes)
        parsed = fetch_all.parse_ssga_fund_page(body.decode("utf-8", "replace"))
        live_date = parsed["date"]
        print(f"GUARD SSGA 页面 SPY 截至日：{live_date}")
    except Exception as exc:  # noqa: BLE001  哨兵失败只跳过本次，不让 workflow 标红
        print(f"GUARD 取 SSGA 页面失败（{type(exc).__name__}: {exc}），本小时跳过。")
        run_fetch = False
        reason = f"guard-error: {type(exc).__name__}"
    else:
        if stored_date is None:
            reason = "无已存 SPY 快照，跑一趟。"
            run_fetch = True
        elif live_date > stored_date:
            reason = f"SSGA 已公布新数据（{stored_date} -> {live_date}），跑一趟。"
            run_fetch = True
        else:
            reason = f"SSGA 尚未公布新数据（页面仍为 {live_date}），本小时跳过。"
            run_fetch = False

    print(f"GUARD run_fetch={str(run_fetch).lower()}（{reason}）")
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"run_fetch={str(run_fetch).lower()}\n")
            fh.write(f"reason={reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
