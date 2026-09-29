# Odoo Automation Pipeline

Automates processing of Increff / EBO data and uploading to Odoo.

## Project Structure

```
odoo automation/
│
├── input/                        ← Place your 3 Excel files here
│   ├── increff_packing_list.xlsx
│   ├── ebo_customer_db.xlsx
│   └── odoo_product_master.xlsx
│
├── output/                       ← Generated output files land here
│
├── logs/                         ← Log files
│
├── config.py                     ← ⚙️  API credentials + file paths
├── main.py                       ← 🚀 Run this to execute full pipeline
├── inspect_inputs.py             ← 🔍 Run this first to inspect columns
│
├── utils/
│   ├── file_loader.py            ← Loads & validates input Excel files
│   ├── odoo_api.py               ← Odoo XML-RPC API wrapper
│   └── logger.py                 ← Logging setup
│
└── processors/
    ├── output_builder.py         ← Builds output files from inputs
    └── odoo_uploader.py          ← Uploads outputs to Odoo
```

## Setup

```bash
pip install -r requirements.txt
```

## Usage

### Step 1 — Place input files
Copy your Excel files into the `/input` folder with these exact names:
- `increff_packing_list.xlsx`
- `ebo_customer_db.xlsx`
- `odoo_product_master.xlsx`

### Step 2 — Configure credentials
Edit `config.py` and fill in your Odoo URL, database, username, and API key.

### Step 3 — Inspect inputs
```bash
python inspect_inputs.py
```

### Step 4 — Run full pipeline
```bash
python main.py
```
