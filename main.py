# ============================================================
#  main.py — Odoo Automation Pipeline Entry Point
# ============================================================
#
#  PIPELINE FLOW:
#    1. Load 3 input Excel files
#    2. Preview columns (optional)
#    3. Build output files (logic TBD)
#    4. Connect to Odoo API
#    5. Upload outputs to Odoo
# ============================================================

import sys
import logging

from config import ODOO_CONFIG, OUTPUT_DIR, LOG_FILE, LOG_LEVEL
from utils.logger import setup_logger
from utils.file_loader import load_all_inputs, preview_inputs
from utils.odoo_api import OdooAPI
from processors.output_builder import build_outputs
from processors.odoo_uploader import upload_all


def main():
    # ----------------------------------------------------------
    # 0. Setup logging
    # ----------------------------------------------------------
    setup_logger(LOG_FILE, LOG_LEVEL)
    logger = logging.getLogger("main")
    logger.info("=" * 60)
    logger.info("  ODOO AUTOMATION PIPELINE — STARTED")
    logger.info("=" * 60)

    # ----------------------------------------------------------
    # 1. Load input Excel files
    # ----------------------------------------------------------
    logger.info("\n📂 STEP 1: Loading input files...")
    try:
        frames = load_all_inputs({})
    except FileNotFoundError as e:
        logger.error(str(e))
        logger.error("👉 Place your Excel files in the /input folder and update config.py paths.")
        sys.exit(1)

    # ----------------------------------------------------------
    # 2. Preview (comment out in production)
    # ----------------------------------------------------------
    preview_inputs(frames, n=3)

    # ----------------------------------------------------------
    # 3. Build output files
    # ----------------------------------------------------------
    logger.info("\n⚙️  STEP 2: Building output files...")
    outputs = build_outputs(frames, OUTPUT_DIR)

    if not outputs:
        logger.warning("No output files generated — check processors/output_builder.py")
    else:
        logger.info(f"✅ Outputs generated: {list(outputs.keys())}")

    # ----------------------------------------------------------
    # 4. Connect to Odoo
    # ----------------------------------------------------------
    logger.info("\n🔗 STEP 3: Connecting to Odoo...")
    odoo = OdooAPI(**ODOO_CONFIG)
    try:
        odoo.authenticate()
    except ConnectionError as e:
        logger.error(str(e))
        logger.error("👉 Check ODOO_CONFIG in config.py")
        sys.exit(1)

    # ----------------------------------------------------------
    # 5. Upload to Odoo
    # ----------------------------------------------------------
    logger.info("\n📤 STEP 4: Uploading to Odoo...")
    upload_all(odoo, outputs)

    logger.info("\n" + "=" * 60)
    logger.info("  PIPELINE COMPLETED ✅")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
