# ============================================================
#  processors/odoo_uploader.py — Odoo Data Uploader
# ============================================================
#
#  This module takes the output DataFrames built by
#  output_builder.py and uploads them to Odoo via API.
#
#  Upload logic will be filled once output format is defined.
# ============================================================

import logging
from utils.odoo_api import OdooAPI

logger = logging.getLogger(__name__)


def upload_all(odoo: OdooAPI, outputs: dict):
    """
    Upload all output DataFrames to the appropriate Odoo models.
    
    Args:
        odoo:    Authenticated OdooAPI instance
        outputs: Dict of { output_name: DataFrame }
    """
    if not outputs:
        logger.warning("No outputs to upload — skipping.")
        return
    
    for name, df in outputs.items():
        logger.info(f"\n📤 Uploading: {name} ({len(df)} records)")
        
        # -------------------------------------------------------
        # 🔧 UPLOAD LOGIC GOES HERE
        #    Will map each output file to the correct Odoo model
        #    and field mapping once user defines requirements
        # -------------------------------------------------------
        
        logger.info(f"  ⚠️  Upload for '{name}' not yet configured")


def _df_to_records(df, field_map: dict) -> list[dict]:
    """
    Convert a DataFrame to a list of Odoo-compatible record dicts
    using a column → odoo_field mapping.
    
    Args:
        df:        Source DataFrame
        field_map: { "Excel Column Name": "odoo.field.name" }
    
    Returns:
        List of dicts ready for OdooAPI.create() or .write()
    """
    records = []
    for _, row in df.iterrows():
        rec = {}
        for excel_col, odoo_field in field_map.items():
            val = row.get(excel_col)
            # Skip NaN / None values
            if val is not None and str(val).strip().lower() not in ("nan", ""):
                rec[odoo_field] = val
        records.append(rec)
    return records
