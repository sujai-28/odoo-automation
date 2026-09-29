# ============================================================
#  config.py — Odoo Automation Central Configuration
# ============================================================

# ----------------------------------------------------------
# ODOO API CREDENTIALS
# Fill these in when you provide your API details
# ----------------------------------------------------------
ODOO_CONFIG = {
    "url": "",            # e.g. "https://yourcompany.odoo.com"
    "db": "",             # e.g. "yourcompany"
    "username": "",       # e.g. "admin@yourcompany.com"
    "api_key": "",        # Odoo API key (Settings > Technical > API Keys)
}

# ----------------------------------------------------------
# INPUT FILE PATHS
# Place your Excel files in the /input folder
# ----------------------------------------------------------
INPUT_FILES = {
    "increff_packing_list": "input/increff_packing_list.xlsx",
    "ebo_customer_db":      "input/ebo_customer_db.xlsx",
    "odoo_product_master":  "input/odoo_product_master.xlsx",
}

# ----------------------------------------------------------
# OUTPUT FILE PATHS
# Generated files will be saved in the /output folder
# ----------------------------------------------------------
OUTPUT_DIR = "output/"

# ----------------------------------------------------------
# SHEET NAME CONFIGURATION
# Update these if your Excel files use different sheet names
# ----------------------------------------------------------
SHEET_NAMES = {
    "increff_packing_list": 0,   # 0 = first sheet, or use sheet name string
    "ebo_customer_db":      0,
    "odoo_product_master":  0,
}

# ----------------------------------------------------------
# LOGGING
# ----------------------------------------------------------
LOG_FILE = "logs/automation.log"
LOG_LEVEL = "INFO"   # DEBUG / INFO / WARNING / ERROR
