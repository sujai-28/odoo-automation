# ============================================================
#  utils/file_loader.py — Input Excel File Loader
# ============================================================

import pandas as pd
import os
import logging

logger = logging.getLogger(__name__)


def load_excel(filepath: str, sheet=0, header=0) -> pd.DataFrame:
    """Load an Excel file and return a cleaned DataFrame."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"❌ File not found: {filepath}")
    
    logger.info(f"Loading: {filepath}")
    df = pd.read_excel(filepath, sheet_name=sheet, header=header)
    
    # Strip whitespace from string columns
    df.columns = df.columns.str.strip()
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].str.strip() if hasattr(df[col], "str") else df[col]
    
    logger.info(f"  → {len(df)} rows, {len(df.columns)} columns loaded")
    return df


def load_all_inputs(config: dict) -> dict:
    """
    Load all three input files and return a dict of DataFrames.
    
    Returns:
        {
            "increff_packing_list": DataFrame,
            "ebo_customer_db":      DataFrame,
            "odoo_product_master":  DataFrame,
        }
    """
    from config import INPUT_FILES, SHEET_NAMES
    
    frames = {}
    for key in INPUT_FILES:
        filepath  = INPUT_FILES[key]
        sheet     = SHEET_NAMES.get(key, 0)
        frames[key] = load_excel(filepath, sheet=sheet)
    
    return frames


def preview_inputs(frames: dict, n: int = 5):
    """Print a quick preview of each loaded DataFrame."""
    for name, df in frames.items():
        print(f"\n{'='*60}")
        print(f"  {name.upper().replace('_', ' ')}")
        print(f"  Columns: {list(df.columns)}")
        print(f"  Shape  : {df.shape}")
        print(f"{'='*60}")
        print(df.head(n).to_string(index=False))
