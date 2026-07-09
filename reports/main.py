from __future__ import annotations

import logging
import sys

if __package__:
    from .config import ConfigError, load_config
    from .fetcher import run_fetch
    from .extract import run_extract
    from .fidelity_eval import run_eval
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from reports.config import ConfigError, load_config
    from reports.fetcher import run_fetch
    from reports.extract import run_extract
    from reports.fidelity_eval import run_eval

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reports")

USAGE = "用法：python -m reports.main [fetch|extract|eval [股號 年 季]]"


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "fetch"
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 1

    if mode == "fetch":
        return run_fetch(cfg)
    if mode == "extract":
        return run_extract(cfg)
    if mode == "eval":
        # eval [股號 年 季]，未給則用 config 第一檔/年/季
        stock = argv[1] if len(argv) > 1 else cfg.stocks[0]
        year = int(argv[2]) if len(argv) > 2 else cfg.years[0]
        quarter = int(argv[3]) if len(argv) > 3 else cfg.quarters[-1]
        return run_eval(cfg, stock, year, quarter)
    logger.error("未知模式：%s\n%s", mode, USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
