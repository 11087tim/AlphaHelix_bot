from __future__ import annotations

import logging
import sys

if __package__:
    from .config import ConfigError, load_config
    from .fetcher import run_fetch
    from .extract import run_extract
    from .fidelity_eval import run_eval
    from .analyze import analyze_report
    from .aggregate import run_aggregate
    from .contract_liability import run_contract_liability
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from reports.config import ConfigError, load_config
    from reports.fetcher import run_fetch
    from reports.extract import run_extract
    from reports.fidelity_eval import run_eval
    from reports.analyze import analyze_report
    from reports.aggregate import run_aggregate
    from reports.contract_liability import run_contract_liability

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reports")

USAGE = ("用法：python -m reports.main [fetch [all]|extract|eval|analyze|aggregate|cl [股號 ...]]\n"
         "  fetch all：全市場模式（FinMind 拉上市+上櫃普通股清單，忽略 config 的 stocks）")


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "fetch"
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 1

    if mode == "fetch":
        if len(argv) > 1 and argv[1] == "all":
            # 全市場模式：清單來自 FinMind，config 的 stocks 僅作為手動模式清單
            from reports import finmind_client
            tickers = finmind_client.all_market_tickers(finmind_client.get_token())
            cfg.stocks = [t["stock_id"] for t in tickers]
            if cfg.min_interval_sec < 1.0:
                cfg.min_interval_sec = 1.0  # 長時間爬 MOPS 放寬限速，避免被封
            logger.info("全市場模式：%d 檔（上市+上櫃普通股，FinMind TaiwanStockInfo），"
                        "限速 %.1fs", len(cfg.stocks), cfg.min_interval_sec)
        return run_fetch(cfg)
    if mode == "extract":
        return run_extract(cfg)
    if mode in ("eval", "analyze"):
        # [股號 年 季]，未給則用 config 第一檔/年/最後一季
        stock = argv[1] if len(argv) > 1 else cfg.stocks[0]
        year = int(argv[2]) if len(argv) > 2 else cfg.years[0]
        quarter = int(argv[3]) if len(argv) > 3 else cfg.quarters[-1]
        if mode == "eval":
            return run_eval(cfg, stock, year, quarter)
        return analyze_report(cfg, stock, year, quarter)
    if mode == "aggregate":
        # aggregate [股號 [季數]]，預設近 8 季
        stock = argv[1] if len(argv) > 1 else cfg.stocks[0]
        n = int(argv[2]) if len(argv) > 2 else 8
        return run_aggregate(cfg, stock, n)
    if mode == "cl":
        # cl [股號 [季數]]：合約負債深讀（性質/認列時點/營收EPS推估），預設近 8 季
        stock = argv[1] if len(argv) > 1 else cfg.stocks[0]
        n = int(argv[2]) if len(argv) > 2 else 8
        return run_contract_liability(cfg, stock, n)
    if mode == "nowcast":
        # nowcast [股號 ...]：月營收估當季/次季營收與 EPS 基線，未給股號跑全清單
        from reports.nowcast import run_nowcast
        return run_nowcast(cfg, argv[1:] or None)
    logger.error("未知模式：%s\n%s", mode, USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
