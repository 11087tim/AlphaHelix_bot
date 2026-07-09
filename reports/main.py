from __future__ import annotations

import logging
import sys

if __package__:
    from .config import ConfigError, load_config
    from .fetcher import run_fetch
    from .extract import run_extract
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from reports.config import ConfigError, load_config
    from reports.fetcher import run_fetch
    from reports.extract import run_extract

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reports")

USAGE = "用法：python -m reports.main [fetch|extract]"


def main(mode: str) -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 1

    if mode == "fetch":
        return run_fetch(cfg)
    if mode == "extract":
        return run_extract(cfg)
    logger.error("未知模式：%s\n%s", mode, USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "fetch"))
