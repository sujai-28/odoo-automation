# ============================================================
#  utils/logger.py — Logging Setup
# ============================================================

import logging
import os
from datetime import datetime


def setup_logger(log_file: str = "logs/automation.log", level: str = "INFO"):
    """Configure root logger with console + file handlers."""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(fmt)

    # File handler (append mode)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(log_level)
    fh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(ch)
    root.addHandler(fh)

    logging.info(f"Logger initialised — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
