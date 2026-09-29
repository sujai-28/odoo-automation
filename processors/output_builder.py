# ============================================================
#  processors/output_builder.py — Output File Generator
# ============================================================
# 
#  🔧 THIS FILE WILL BE FILLED IN once the user defines 
#     how the output files should be created from the 3 inputs.
#
#  Inputs available:
#    - frames["increff_packing_list"]  : DataFrame
#    - frames["ebo_customer_db"]       : DataFrame
#    - frames["odoo_product_master"]   : DataFrame
#
#  Expected outputs:
#    - TBD (user will specify)
# ============================================================

import pandas as pd
import os
import logging

logger = logging.getLogger(__name__)


def build_outputs(frames: dict, output_dir: str) -> dict:
    """
    Main entry point for generating output files.
    
    Args:
        frames:     dict of input DataFrames keyed by file name
        output_dir: folder path where output Excel files will be saved
    
    Returns:
        dict of { output_name: DataFrame } that were saved
    """
    os.makedirs(output_dir, exist_ok=True)
    
    increff  = frames["increff_packing_list"]
    ebo_db   = frames["ebo_customer_db"]
    products = frames["odoo_product_master"]
    
    outputs = {}
    
    # -------------------------------------------------------
    # 🔧 OUTPUT LOGIC GOES HERE
    #    Will be implemented once user specifies requirements
    # -------------------------------------------------------
    
    logger.info("Output builder: placeholder — awaiting output specifications")
    return outputs


def save_output(df: pd.DataFrame, filename: str, output_dir: str):
    """Save a DataFrame to Excel in the output directory."""
    path = os.path.join(output_dir, filename)
    df.to_excel(path, index=False)
    logger.info(f"✅ Saved: {path} ({len(df)} rows)")
    return path
