"""
app.py - Odoo Automation Web App
"""
import os, io, traceback, sqlite3, sys, uuid, shutil
from datetime import datetime
from pathlib import Path
import pandas as pd
from flask import Flask, request, jsonify, send_file, send_from_directory, render_template_string, session, redirect, url_for, render_template, g
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

# Setup path for Ginesys Automation V4.5
GINESYS_ROOT = Path(__file__).resolve().parent / "Odoo_Ginesys_DC_TransferOut_Automation_V4_5_WorkDateFix"
if str(GINESYS_ROOT) not in sys.path:
    sys.path.insert(0, str(GINESYS_ROOT))

import config_loader as ginesys_config_loader
import auth_store as ginesys_auth_store
from ginesys_auth import GinesysLoginConflict, login_portal_user, request_ginesys_token
from ginesys_client import GinesysClient, GinesysAuthenticationError
import job_store as ginesys_job_store
import parser as ginesys_parser
import posting as ginesys_posting
import result_writer as ginesys_result_writer
import utils as ginesys_utils
import audit_store as ginesys_audit_store

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'technosport_super_secret_key')
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

DATABASE = os.environ.get('HISTORY_DB_PATH', 'history.db')

USER_OUTPUTS = {}

def get_user_key():
    return session.get("user_email") or session.get("username") or "default_user"

def set_user_output(warehouse, output_combined, output_ext, output_int, stats):
    user_key = get_user_key()
    USER_OUTPUTS[user_key] = {
        "warehouse": warehouse,
        "output": output_combined,
        "output_external": output_ext,
        "output_internal": output_int,
        "stats": stats
    }

def get_user_output():
    user_key = get_user_key()
    return USER_OUTPUTS.get(user_key, {})

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS logs
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                       timestamp TEXT,
                       channel TEXT,
                       warehouse TEXT,
                       ebo_type TEXT,
                       invoices INTEGER,
                       sku_lines INTEGER,
                       total_qty INTEGER,
                       missing INTEGER)''')
        # Check if warehouse & user_email columns exist for existing dbs
        cur = db.execute("PRAGMA table_info(logs)")
        cols = [c[1] for c in cur.fetchall()]
        if 'warehouse' not in cols:
            db.execute("ALTER TABLE logs ADD COLUMN warehouse TEXT")
        if 'user_email' not in cols:
            db.execute("ALTER TABLE logs ADD COLUMN user_email TEXT")
        
        # User authentication table
        db.execute('''CREATE TABLE IF NOT EXISTS app_users
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                       email TEXT UNIQUE NOT NULL,
                       password_hash TEXT NOT NULL,
                       created_at TEXT NOT NULL)''')
        
        cur_users = db.execute("SELECT COUNT(*) FROM app_users")
        if cur_users.fetchone()[0] == 0:
            admin_hash = generate_password_hash("admin")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db.execute("INSERT INTO app_users (email, password_hash, created_at) VALUES (?, ?, ?)",
                       ("admin@technosport.in", admin_hash, now_str))
            db.execute("INSERT INTO app_users (email, password_hash, created_at) VALUES (?, ?, ?)",
                       ("admin", admin_hash, now_str))
            
        db.commit()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def process_files(pl_bytes, cdb_bytes, prod_bytes, ebo_type="Auto", warehouse="Bagalur"):
    import re
    # Load
    pl   = pd.read_excel(io.BytesIO(pl_bytes))
    cdb  = pd.read_excel(io.BytesIO(cdb_bytes))
    prod = pd.read_excel(io.BytesIO(prod_bytes))
    pl.columns   = [str(c).strip() for c in pl.columns]
    cdb.columns  = [str(c).strip() for c in cdb.columns]
    prod.columns = [str(c).strip() for c in prod.columns]

    def find_col(df, *names):
        lower_map = {str(c).strip().lower(): str(c).strip() for c in df.columns}
        for n in names:
            if n.lower() in lower_map:
                return lower_map[n.lower()]
        return None

    def clean_barcode(series):
        def fmt(x):
            if pd.isnull(x): return ""
            sx = str(x).strip()
            if sx == "" or sx.lower() == "nan": return ""
            try:
                return str(int(float(sx)))
            except:
                return sx.replace(".0", "")
        return series.apply(fmt)

    # Normalize barcodes
    prod_barcode_col = find_col(prod, "Barcode", "barcode")
    if not prod_barcode_col:
        raise ValueError(f"Product Master is missing a 'Barcode' column. Found columns: {', '.join(prod.columns)}")
    prod["_barcode"] = clean_barcode(prod[prod_barcode_col])

    pl_barcode_col = find_col(pl, "Barcode", "Client SKU ID / EAN", "barcode")
    if not pl_barcode_col:
        raise ValueError(f"Packing List is missing a 'Barcode' column. Found columns: {', '.join(pl.columns)}")
        
    pl["_barcode"]   = clean_barcode(pl[pl_barcode_col])

    barcode_to_id = dict(zip(prod["_barcode"], prod["ID"]))

    logs = []

    WH_STATE_MAP = {
        "bhiwandi": "MH",
        "kolkata":  "WB",
        "gurgaon":  "HR",
        "bagalur":  "TN"
    }
    WH_CODE_MAP = {
        "bhiwandi": "TSPL-BHIWANDI",
        "kolkata":  "TSPL-KOLKATA",
        "gurgaon":  "TSPL-GURGAON",
        "bagalur":  "TSPL-HO WH1"
    }
    WH_SOURCE_LOC_MAP = {
        "bhiwandi": "BHIWANDI/STOCK",
        "kolkata":  "KOLKATA/STOCK",
        "gurgaon":  "GURGAON/STOCK",
        "bagalur":  "HOSUR/STOCK"
    }
    WH_OPS_TYPE_MAP = {
        "bhiwandi": "D2C WH - BHIWANDI",
        "kolkata":  "D2C WH - KOLKATA",
        "gurgaon":  "D2C WH - GURGAON",
        "bagalur":  "D2C WH - HOSUR"
    }

    wh_key = str(warehouse).strip().lower()
    wh_code = WH_CODE_MAP.get(wh_key, warehouse.strip())
    wh_source_loc = WH_SOURCE_LOC_MAP.get(wh_key, "WH/STOCK")
    wh_ops_type = WH_OPS_TYPE_MAP.get(wh_key, "D2C WH")
    target_state = WH_STATE_MAP.get(wh_key, "")

    is_auto = (ebo_type == "Auto" or wh_key in ["bhiwandi", "kolkata", "gurgaon"])

    # Clean IDs for lookup
    def clean_id(x):
        if pd.isnull(x): return ""
        sx = str(x).strip()
        if sx == "" or sx.lower() == "nan": return ""
        try:
            return str(int(float(sx)))
        except:
            return sx.replace(".0", "")

    pl_del_col = find_col(pl, "Shipping Partner Location ID", "shipping partner location id", "INCREFF DEL_ID", "del_id")
    cdb_del_col = find_col(cdb, "INCREFF DEL_ID", "INCREFF_DEL_ID", "del_id", "Shipping Partner Location ID")

    if pl_del_col:
        pl["_del_id"] = pl[pl_del_col].apply(clean_id)
    else:
        pl["_del_id"] = ""
        logs.append(f"Packing List missing 'Shipping Partner Location ID'. Found: {', '.join(pl.columns)}")

    if cdb_del_col:
        cdb["_del_id"] = cdb[cdb_del_col].apply(clean_id)
        cdb_lookup = cdb.drop_duplicates(subset=["_del_id"]).set_index("_del_id")
    else:
        cdb_lookup = pd.DataFrame()
        logs.append(f"Customer DB missing 'INCREFF DEL_ID'. Found: {', '.join(cdb.columns)}")

    cdb_cust_col = find_col(cdb, "Customer", "customer")
    cdb_cust_id_col = find_col(cdb, "Customer/External ID", "customer/id", "customer external id")
    cdb_deliv_col = find_col(cdb, "Delivery Address", "delivery address")
    cdb_deliv_id_col = find_col(cdb, "Delivery Address/External ID", "delivery address/id", "delivery address external id")
    cdb_store_name_col = find_col(cdb, "Store Name", "store name")
    # Store stock location: in Customer DB, user added 'Source Location' to hold store stock location (e.g. EBOWG/Stock, SQUAR/Stock)
    cdb_store_loc_col = find_col(cdb, "Source Location", "source location", "Destination Location", "destination location")

    # Bagalur internal fuzzy fallback
    store_map = {}
    if wh_key == "bagalur" and not is_auto:
        if cdb_store_name_col and cdb_store_loc_col:
            for _, r in cdb.iterrows():
                sn = str(r[cdb_store_name_col]).strip()
                dl = str(r[cdb_store_loc_col]).strip()
                if sn and dl and dl.lower() != "nan":
                    store_map[sn] = dl

        def get_significant_words(s):
            words = set(re.findall(r'\w+', str(s).lower()))
            words = {w for w in words if w not in ['ebo', 'store', 'stores', 'tspl', 'the', 'of', 'in', 'tn']}
            replacements = {'tup': 'tiruppur'}
            return {replacements.get(w, w) for w in words}

        def find_best_destination(extracted, smap):
            for sn, dest in smap.items():
                if sn.lower() == extracted.lower():
                    return dest
            ext_words = get_significant_words(extracted)
            best_dest = ""
            max_overlap = 0
            for sn, dest in smap.items():
                sn_words = get_significant_words(sn)
                overlap = len(ext_words.intersection(sn_words))
                if overlap > max_overlap:
                    max_overlap = overlap
                    best_dest = dest
            if max_overlap >= 1:
                return best_dest
            return ""

    external_rows = []
    internal_rows = []
    external_inv_set = set()
    internal_inv_set = set()

    for inv_no, grp in pl.groupby("System Invoice No", sort=False):
        grp = grp.reset_index(drop=True)
        # Pivot & sum duplicate SKUs within invoice group
        if "_barcode" in grp.columns:
            disp_col = "Dispatched Quantity" if "Dispatched Quantity" in grp.columns else None
            agg_rules = {}
            for col in grp.columns:
                if col != "_barcode":
                    if col == disp_col:
                        agg_rules[col] = "sum"
                    else:
                        agg_rules[col] = "first"
            if agg_rules:
                grp = grp.groupby("_barcode", as_index=False, sort=False).agg(agg_rules)

        channel_order = str(grp["Channel Order ID"].iloc[0]) if "Channel Order ID" in grp.columns else str(inv_no)
        del_id = grp["_del_id"].iloc[0]

        cdb_row = {}
        if del_id and del_id in cdb_lookup.index:
            cdb_row = cdb_lookup.loc[del_id].to_dict()
        else:
            logs.append(f"Customer DB record not found for Delivery ID: '{del_id}' (Invoice: {inv_no}, Order: {channel_order})")

        # Determine Internal vs External
        if is_auto:
            cust_val = str(cdb_row.get(cdb_cust_col, "")) if cdb_cust_col else ""
            m = re.search(r'\(\s*([A-Za-z]{2})\s*\)', cust_val)
            cust_state = m.group(1).upper() if m else ""
            is_order_internal = (bool(cust_state) and cust_state == target_state)
        else:
            # Bagalur manual selection
            is_order_internal = ebo_type in ["Internal", "Internal EBO"]

        if is_order_internal:
            internal_inv_set.add(inv_no)
            store_loc = str(cdb_row.get(cdb_store_loc_col, "")).strip() if cdb_store_loc_col else ""
            if not store_loc or store_loc.lower() == "nan":
                if wh_key == "bagalur" and not is_auto:
                    parts = channel_order.split('_')
                    extracted_store = "_".join(parts[1:-1]).strip() if len(parts) >= 3 else channel_order.strip()
                    store_loc = find_best_destination(extracted_store, store_map)
                else:
                    store_name = cdb_row.get(cdb_store_name_col, channel_order)
                    logs.append(f"Internal store location not defined in Customer DB for '{store_name}' (Del ID: {del_id})")
                    store_loc = ""

            for i, row in grp.iterrows():
                is_first = (i == 0)
                barcode  = row["_barcode"]
                prod_id  = barcode_to_id.get(barcode, "")
                if not prod_id:
                    logs.append(f"Barcode not found: {barcode} (SKU: {row.get('Client SKU ID / EAN','')})")

                internal_rows.append({
                    "Source Location":      wh_source_loc if is_first else "",
                    "Destination Location": store_loc if is_first else "",
                    "Operations Type":      wh_ops_type if is_first else "",
                    "Style No":             "MIX" if is_first else "",
                    "Remarks":              channel_order if is_first else "",
                    "Operations/Product":   prod_id,
                    "Operations/Done":      row.get("Dispatched Quantity", 0),
                })
        else:
            external_inv_set.add(inv_no)
            c_name = cdb_row.get(cdb_cust_col, "") if cdb_cust_col else ""
            c_id = cdb_row.get(cdb_cust_id_col, "") if cdb_cust_id_col else ""
            d_name = cdb_row.get(cdb_deliv_col, "") if cdb_deliv_col else ""
            d_id = cdb_row.get(cdb_deliv_id_col, "") if cdb_deliv_id_col else ""

            for i, row in grp.iterrows():
                is_first = (i == 0)
                barcode  = row["_barcode"]
                prod_id  = barcode_to_id.get(barcode, "")
                if not prod_id:
                    logs.append(f"Barcode not found: {barcode} (SKU: {row.get('Client SKU ID / EAN','')})")

                external_rows.append({
                    "Customer/Name":                           c_name if is_first else "",
                    "Customer/ID":                             c_id if is_first else "",
                    "Delivery Address/Name":                   d_name if is_first else "",
                    "Delivery Address/ID":                     d_id if is_first else "",
                    "Style No":                                "MIX" if is_first else "",
                    "Warehouse":                               wh_code if is_first else "",
                    "Remarks":                                 channel_order if is_first else "",
                    "Order Lines/Product/Internal Reference":  str(row.get("Client SKU ID / EAN","")),
                    "Order Lines/Product/ID":                  prod_id,
                    "Order Lines/Quantity":                    row.get("Dispatched Quantity", 0),
                })

    ext_df = pd.DataFrame(external_rows, columns=[
        "Customer/Name", "Customer/ID", "Delivery Address/Name", "Delivery Address/ID",
        "Style No", "Warehouse", "Remarks",
        "Order Lines/Product/Internal Reference", "Order Lines/Product/ID", "Order Lines/Quantity"
    ]) if external_rows else pd.DataFrame()

    int_df = pd.DataFrame(internal_rows, columns=[
        "Source Location", "Destination Location", "Operations Type",
        "Style No", "Remarks", "Operations/Product", "Operations/Done"
    ]) if internal_rows else pd.DataFrame()

    out_dict = {}
    if len(ext_df) > 0:
        out_dict["Sales Orders"] = ext_df
    if len(int_df) > 0:
        out_dict["Internal Transfers"] = int_df

    total_qty = int(pl["Dispatched Quantity"].sum()) if "Dispatched Quantity" in pl.columns else 0

    stats = {
        "invoices":          int(pl["System Invoice No"].nunique()),
        "sku_lines":         int(len(pl)),
        "total_qty":         total_qty,
        "has_external":      len(ext_df) > 0,
        "has_internal":      len(int_df) > 0,
        "external_invoices": len(external_inv_set),
        "internal_invoices": len(internal_inv_set),
        "external_lines":    len(ext_df),
        "internal_lines":    len(int_df),
        "missing":           len(logs),
        "logs":              logs,
    }
    return out_dict, stats

def process_lfr_files(main_bytes, mrp_bytes, odoo_bytes, pm_bytes, margin, customer_name):
    import re
    main_xls = pd.read_excel(io.BytesIO(main_bytes), sheet_name=None)
    all_rows = []
    logs = []
    
    def clean_sku(s):
        s = str(s).strip().upper()
        if s.isdigit():
            return s
        if len(s) > 8:
            match = re.search(r'(\d{3,5})$', s)
            if match:
                price = match.group(1)
                base_sku = s[:-len(price)]
                if len(base_sku) >= 5:
                    return base_sku
        return s

    def get_from_map(mapping, sku_key, default=None):
        if not sku_key or pd.isnull(sku_key):
            return default
        s_raw = str(sku_key).strip().upper()
        if s_raw in mapping:
            return mapping[s_raw]
        s_clean = clean_sku(s_raw)
        if s_clean in mapping:
            return mapping[s_clean]
        stripped = re.sub(r'\d{3,5}$', '', s_raw)
        if stripped in mapping:
            return mapping[stripped]
        return default

    for sheet_name, df in main_xls.items():
        df.columns = [str(c).strip() for c in df.columns]
        sku_col = next((c for c in df.columns if "CLIENT SKU" in c.upper()), None) or next((c for c in df.columns if "SKU" in c.upper() or "BARCODE" in c.upper()), None)
        if not sku_col:
            # Silently skip summary/metadata sheets (Sheet1, Sheet2, etc.) — not an error
            continue
        order_col = next((c for c in df.columns if "CHANNEL ORDER ID" in c.upper() or "ORDER ID" in c.upper()), None)
        df = df.dropna(subset=[sku_col])
        for _, row in df.iterrows():
            order_id = str(row[order_col]) if order_col and pd.notnull(row[order_col]) else sheet_name
            sku = str(row[sku_col]).strip()
            if sku:
                all_rows.append({"Channel Order ID": order_id, "Client SKU ID": sku})
                
    if not all_rows:
        raise ValueError("No valid data found in LFR main file.")
        
    main_df = pd.DataFrame(all_rows)
    main_df['clean_sku'] = main_df['Client SKU ID'].apply(clean_sku)
    
    mrp_df = pd.read_excel(io.BytesIO(mrp_bytes))
    
    # Try to find the actual header row
    header_idx = -1
    for i, row in mrp_df.head(10).iterrows():
        row_strs = [str(x).upper() for x in row.tolist()]
        has_sku = any("SKU" in x or "BARCODE" in x or "ITEM" in x for x in row_strs)
        has_mrp = any("MRP" in x or "PRICE" in x for x in row_strs)
        if has_sku and has_mrp:
            header_idx = i
            break
            
    if header_idx != -1:
        mrp_df.columns = mrp_df.iloc[header_idx].tolist()
        mrp_df = mrp_df.iloc[header_idx+1:].reset_index(drop=True)
        
    mrp_df.columns = [str(c).strip() for c in mrp_df.columns]
    mrp_sku_col = next((c for c in mrp_df.columns if "CLIENT SKU" in c.upper()), None) or next((c for c in mrp_df.columns if "SKU" in c.upper() or "BARCODE" in c.upper() or "ITEM" in c.upper()), None)
    mrp_price_col = next((c for c in mrp_df.columns if "MRP" in c.upper() or "PRICE" in c.upper()), None)
    
    if not mrp_sku_col or not mrp_price_col:
        raise ValueError(f"MRP file missing SKU or MRP column. Found: {', '.join(mrp_df.columns)}")
        
    mrp_df['clean_sku'] = mrp_df[mrp_sku_col].apply(clean_sku)
    mrp_map = {}
    for _, row in mrp_df.dropna(subset=[mrp_sku_col, mrp_price_col]).iterrows():
        try:
            mrp_val = float(row[mrp_price_col])
            raw_s = str(row[mrp_sku_col]).strip().upper()
            mrp_map[raw_s] = mrp_val
            mrp_map[clean_sku(raw_s)] = mrp_val
        except:
            pass
            
    pivot_df = main_df.groupby(['Channel Order ID', 'clean_sku']).size().reset_index(name='Count of Client SKU ID')
    pivot_df.rename(columns={'clean_sku': 'Client SKU ID'}, inplace=True)
    
    pivot_df['MRP'] = pivot_df['Client SKU ID'].apply(lambda s: get_from_map(mrp_map, s, 0.0))
    pivot_df['DIS'] = (pivot_df['MRP'] * (1.0 - margin)).round(2)
    pivot_df['UNIT PRICE'] = (pivot_df['DIS'] / 1.05).round(2)
    
    odoo_df = pd.read_excel(io.BytesIO(odoo_bytes))
    
    pm_df = pd.read_excel(io.BytesIO(pm_bytes))
    pm_df.columns = [str(c).strip() for c in pm_df.columns]
    sku_to_odoo_id = {}
    pm_ref_col = next((c for c in pm_df.columns if "INTERNAL REFERENCE" in c.upper() or "SKU" in c.upper()), None)
    pm_id_col = next((c for c in pm_df.columns if "ID" == c.upper()), None)
    
    if pm_ref_col and pm_id_col:
        for _, row in pm_df.dropna(subset=[pm_ref_col, pm_id_col]).iterrows():
            raw_ref = str(row[pm_ref_col]).strip().upper()
            odoo_id = str(row[pm_id_col]).strip()
            sku_to_odoo_id[raw_ref] = odoo_id
            sku_to_odoo_id[clean_sku(raw_ref)] = odoo_id
    else:
        logs.append(f"Product Master missing Internal Reference or ID column.")

    def get_significant_words(s):
        words = set(re.findall(r'\w+', str(s).lower()))
        return {w for w in words if w not in ['ebo', 'store', 'stores', 'tspl', 'the', 'of', 'in', 'tn', 'lfr', 'djt']}
    
    remarks_col = next((c for c in odoo_df.columns if "REMARKS" in c.upper()), None)
    odoo_template_rows = []
    if remarks_col:
        for _, row in odoo_df.dropna(subset=[remarks_col]).iterrows():
            odoo_template_rows.append(row.to_dict())
            
    final_rows = []
    for order_id, grp in pivot_df.groupby('Channel Order ID', sort=False):
        best_template = None
        if odoo_template_rows:
            order_words = get_significant_words(order_id)
            if order_words:
                max_overlap = 0
                for tmpl in odoo_template_rows:
                    rem_words = get_significant_words(tmpl[remarks_col])
                    overlap = len(order_words.intersection(rem_words))
                    if overlap > max_overlap:
                        max_overlap = overlap
                        best_template = tmpl
        
        if not best_template:
            best_template = {c: "" for c in odoo_df.columns}
            if remarks_col:
                best_template[remarks_col] = order_id
            logs.append(f"Could not find exact Odoo template match for '{order_id}'.")
            
        grp = grp.reset_index(drop=True)
        for i, row in grp.iterrows():
            is_first = (i == 0)
            new_row = {}
            for c in odoo_df.columns:
                if str(c).startswith('order_line/'):
                    continue
                new_row[c] = best_template.get(c, "") if is_first else ""
                
            clean_s = str(row['Client SKU ID'])
            new_row['order_line/product_id/id'] = get_from_map(sku_to_odoo_id, clean_s, "")
            if not new_row['order_line/product_id/id']:
                logs.append(f"Odoo Product ID not found for SKU: {clean_s}")
                
            new_row['order_line/product_uom_qty'] = row['Count of Client SKU ID']
            new_row['order_line/price_unit'] = row['UNIT PRICE']
            new_row['Client SKU ID'] = clean_s
            
            final_rows.append(new_row)
            
    final_df = pd.DataFrame(final_rows)
    
    stats = {
        "invoices": int(pivot_df['Channel Order ID'].nunique()),
        "sku_lines": len(final_df),
        "total_qty": int(pivot_df['Count of Client SKU ID'].sum()),
        "missing": len(logs),
        "logs": logs
    }
    return final_df, stats


# ── HTML template (loaded from file) ──────────────────────────
def get_html():
    path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()

@app.route("/")
@login_required
def index():
    operators = []
    try:
        operators = ginesys_auth_store.list_users()
    except Exception:
        traceback.print_exc()
    return render_template_string(get_html(), ginesys_operators=operators)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email_input = str(request.form.get("email") or request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")
        
        if not email_input or not password:
            return render_template_string(get_login_html(), error="Please enter both Email ID and Password.", active_tab="login", login_email=email_input)
        
        db = get_db()
        cur = db.execute("SELECT * FROM app_users WHERE LOWER(email) = LOWER(?)", (email_input,))
        user = cur.fetchone()
        
        valid = False
        if user and check_password_hash(user["password_hash"], password):
            valid = True
        elif email_input.lower() == "admin" and password == "admin":
            valid = True
        
        if valid:
            session['logged_in'] = True
            session['user_email'] = user["email"] if user else email_input
            return redirect(url_for("index"))
        else:
            return render_template_string(get_login_html(), error="Invalid Email ID or Password. Please try again.", active_tab="login", login_email=email_input)
            
    return render_template_string(get_login_html(), active_tab="login")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = str(request.form.get("email", "")).strip().lower()
        password = str(request.form.get("password", ""))
        confirm_password = str(request.form.get("confirm_password", ""))
        
        if not email or "@" not in email or "." not in email:
            return render_template_string(get_login_html(), signup_error="Please enter a valid Email ID (e.g., user@domain.com).", active_tab="register", signup_email=email)
        
        if len(password) < 6:
            return render_template_string(get_login_html(), signup_error="Password must be at least 6 characters long.", active_tab="register", signup_email=email)
            
        if password != confirm_password:
            return render_template_string(get_login_html(), signup_error="Passwords do not match. Please re-type your password.", active_tab="register", signup_email=email)
            
        db = get_db()
        cur = db.execute("SELECT * FROM app_users WHERE LOWER(email) = LOWER(?)", (email,))
        if cur.fetchone():
            return render_template_string(get_login_html(), signup_error="An account with this Email ID already exists. Please log in.", active_tab="register", signup_email=email)
            
        pwd_hash = generate_password_hash(password)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute("INSERT INTO app_users (email, password_hash, created_at) VALUES (?, ?, ?)", (email, pwd_hash, now_str))
        db.commit()
        
        # Automatically log in the newly registered user
        session['logged_in'] = True
        session['user_email'] = email
        return redirect(url_for("index"))
        
    return redirect(url_for("login"))

@app.route("/logout")
def logout():
    session.pop('logged_in', None)
    session.pop('user_email', None)
    return redirect(url_for("login"))

@app.route("/process", methods=["POST"])
@login_required
def process():
    try:
        channel = request.form.get("channel", "EBO")
        warehouse = request.form.get("warehouse", "Bagalur")
        ebo_type = request.form.get("ebo_type", "Internal")
        
        if channel == "EBO":
            for key in ["packing_list","customer_db","product_master"]:
                if key not in request.files:
                    return jsonify({"success": False, "error": f"Missing file: {key}"}), 400
            out_df, stats = process_files(
                request.files["packing_list"].read(),
                request.files["customer_db"].read(),
                request.files["product_master"].read(),
                ebo_type=ebo_type,
                warehouse=warehouse
            )
        elif channel == "LFR":
            for key in ["lfr_main", "lfr_mrp", "lfr_odoo", "lfr_pm"]:
                if key not in request.files:
                    return jsonify({"success": False, "error": f"Missing file: {key}"}), 400
            margin_str = request.form.get("lfr_margin")
            customer = request.form.get("lfr_customer")
            if not margin_str:
                return jsonify({"success": False, "error": "Missing margin configuration for LFR."}), 400
            try:
                margin = float(str(margin_str).replace('%', '').strip())
                if margin > 1:
                    margin = margin / 100.0
            except:
                return jsonify({"success": False, "error": f"Invalid margin value: {margin_str}"}), 400
                
            out_df, stats = process_lfr_files(
                request.files["lfr_main"].read(),
                request.files["lfr_mrp"].read(),
                request.files["lfr_odoo"].read(),
                request.files["lfr_pm"].read(),
                margin,
                customer
            )
        else:
            return jsonify({"success": False, "error": f"Automation logic for {channel} is not yet implemented."}), 400

        app.config["_current_warehouse"] = warehouse

        # Prepare outputs
        buf_combined = io.BytesIO()
        with pd.ExcelWriter(buf_combined, engine="openpyxl") as writer:
            has_written = False
            if isinstance(out_df, dict):
                for sname, df in out_df.items():
                    if len(df) > 0:
                        df.to_excel(writer, index=False, sheet_name=sname)
                        has_written = True
            else:
                out_df.to_excel(writer, index=False, sheet_name="Sales Orders")
                has_written = True
            if not has_written:
                pd.DataFrame().to_excel(writer, index=False, sheet_name="Orders")
        buf_combined.seek(0)
        app.config["_output"] = buf_combined.getvalue()

        # External output
        if isinstance(out_df, dict) and "Sales Orders" in out_df and len(out_df["Sales Orders"]) > 0:
            buf_ext = io.BytesIO()
            with pd.ExcelWriter(buf_ext, engine="openpyxl") as writer:
                out_df["Sales Orders"].to_excel(writer, index=False, sheet_name="Sales Orders")
            buf_ext.seek(0)
            app.config["_output_external"] = buf_ext.getvalue()
        elif not isinstance(out_df, dict):
            app.config["_output_external"] = buf_combined.getvalue()
        else:
            app.config["_output_external"] = None

        # Internal output
        if isinstance(out_df, dict) and "Internal Transfers" in out_df and len(out_df["Internal Transfers"]) > 0:
            buf_int = io.BytesIO()
            with pd.ExcelWriter(buf_int, engine="openpyxl") as writer:
                out_df["Internal Transfers"].to_excel(writer, index=False, sheet_name="Internal Transfers")
            buf_int.seek(0)
            app.config["_output_internal"] = buf_int.getvalue()
        else:
            app.config["_output_internal"] = None

        app.config["_stats"]  = stats
        
        # Save per-user output to ensure session isolation
        ext_data = buf_ext.getvalue() if 'buf_ext' in locals() and buf_ext else None
        int_data = buf_int.getvalue() if 'buf_int' in locals() and buf_int else None
        comb_data = buf_combined.getvalue()
        
        set_user_output(warehouse, comb_data, ext_data, int_data, stats)
        app.config["_output"] = comb_data

        # Log to DB with user_email
        db = get_db()
        ebo_summary = ebo_type
        if ebo_type == "Auto":
            ebo_summary = f"Auto (Ext:{stats.get('external_invoices',0)} / Int:{stats.get('internal_invoices',0)})"

        db.execute("INSERT INTO logs (timestamp, channel, warehouse, ebo_type, invoices, sku_lines, total_qty, missing, user_email) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), channel, warehouse if channel == "EBO" else customer, ebo_summary if channel == "EBO" else "LFR Retail", stats['invoices'], stats['sku_lines'], stats['total_qty'], stats['missing'], get_user_key()))
        db.commit()
        
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/history")
@login_required
def api_history():
    db = get_db()
    cur = db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    history = [dict(r) for r in rows]
    return jsonify({"success": True, "history": history, "currentUser": get_user_key()})

@app.route("/api/lfr_customers")
@login_required
def api_lfr_customers():
    try:
        url = "https://docs.google.com/spreadsheets/d/1qfPsSLnWU3TYAkPZf_yjX8d5VSkTTcMvCDXOW8BGWO4/export?format=csv&gid=98292881"
        df = pd.read_csv(url)
        
        customers = []
        # Find the customer column (might have trailing space)
        cust_col = next((c for c in df.columns if "CUSTOMER" in c.upper() and "NAME" not in c.upper()), None)
        margin_col = next((c for c in df.columns if "MARGIN" in c.upper()), None)
        
        if cust_col and margin_col:
            for _, row in df.dropna(subset=[cust_col]).iterrows():
                c_name = str(row[cust_col]).strip()
                if c_name:
                    c_margin = row[margin_col]
                    # Clean margin format if needed, but we can just pass it as string/number
                    customers.append({"name": c_name, "margin": str(c_margin)})
        
        return jsonify({"success": True, "customers": customers})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/download")
@login_required
def download():
    file_type = request.args.get("type", "all")
    u_data = get_user_output()
    wh = u_data.get("warehouse", app.config.get("_current_warehouse", "warehouse")).lower()
    
    if file_type == "external":
        data = u_data.get("output_external") or app.config.get("_output_external")
        fname = f"odoo_external_sales_{wh}.xlsx"
    elif file_type == "internal":
        data = u_data.get("output_internal") or app.config.get("_output_internal")
        fname = f"odoo_internal_transfers_{wh}.xlsx"
    else:
        data = u_data.get("output") or app.config.get("_output")
        fname = f"odoo_combined_orders_{wh}.xlsx"

    if not data:
        data = u_data.get("output") or app.config.get("_output")
        fname = "odoo_sales_orders.xlsx"

    if not data:
        return "No output ready for this user session", 404

    return send_file(
        io.BytesIO(data), as_attachment=True,
        download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# ── Ginesys Transfer Out Helpers and Endpoints ─────────────────

def get_ginesys_operator(operator_id=None):
    if operator_id:
        try:
            op = ginesys_auth_store.get_user(int(operator_id))
            if op:
                return op
        except Exception:
            pass
    users = ginesys_auth_store.list_users()
    admin = next((u for u in users if u.get("role") == "admin" and u.get("active")), None)
    if admin:
        return ginesys_auth_store.get_user(admin["id"])
    active = next((u for u in users if u.get("active")), None)
    if active:
        return ginesys_auth_store.get_user(active["id"])
    return None

@app.route("/api/ginesys/operators", methods=["GET"])
@login_required
def api_ginesys_operators():
    try:
        users = ginesys_auth_store.list_users()
        return jsonify({"success": True, "operators": users})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/ginesys/config", methods=["GET"])
@login_required
def api_ginesys_config():
    try:
        c = ginesys_config_loader.load_config()
        errors, warnings = ginesys_config_loader.check_config(c)
        return jsonify({
            "success": True,
            "baseUrl": c.base_url,
            "ownerSiteCode": c.owner_site_code,
            "dcDocCode": c.dc_doc_code,
            "outStockPointCode": c.out_stock_point_code,
            "docSchemeTN": c.doc_code_tn,
            "docSchemeInterstate": c.doc_code_interstate,
            "discountFactor": c.factor,
            "siteCount": len(c.sites),
            "warnings": warnings,
            "errors": errors
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/ginesys/test-connection", methods=["POST"])
@login_required
def api_ginesys_test_connection():
    try:
        data = request.get_json(silent=True) or request.form or {}
        op_id = data.get("operator_id")
        logout_existing = bool(data.get("logout_existing_session"))
        operator = get_ginesys_operator(op_id)
        if not operator:
            return jsonify({"success": False, "error": "No authorized Ginesys operator found."}), 404
        
        c = ginesys_config_loader.load_config()
        auth = ginesys_auth_store.get_ginesys_auth(operator["id"])
        
        if logout_existing:
            login_portal_user(c.base_url, operator, auth["username"], auth["password"], logout_existing_session=True)
            operator = ginesys_auth_store.get_user(operator["id"])
        
        client = GinesysClient(c, operator=operator)
        client.validate_token()
        refreshed = ginesys_auth_store.public_user(ginesys_auth_store.get_user(operator["id"]))
        return jsonify({
            "success": True,
            "message": f"Connection verified with Ginesys Web for {operator.get('display_name') or operator.get('username')}.",
            "user": refreshed
        })
    except GinesysLoginConflict as exc:
        return jsonify({"success": False, "requiresSessionLogout": True, "error": str(exc)}), 409
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if "license has exceeded" in msg or "555555555" in msg:
            msg = "Ginesys user license limit exceeded. Please ask other active users to log out of Ginesys Web or select a different operator."
        return jsonify({"success": False, "error": msg}), 500

@app.route("/api/ginesys/validate", methods=["POST"])
@login_required
def api_ginesys_validate():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"success": False, "error": "Please upload at least one Odoo raw Excel/CSV file."}), 400
    
    job_id = uuid.uuid4().hex
    job_upload_dir = ginesys_job_store.UPLOADS / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    seen_names = set()
    
    try:
        for f in files:
            name = ginesys_utils.safe_filename(f.filename or "input.xlsx")
            if Path(name).suffix.lower() not in {".xlsx", ".xls", ".csv"}:
                raise ValueError(f"Unsupported file type: {name}. Only .xlsx, .xls, .csv files are supported.")
            if name.casefold() in seen_names:
                raise ValueError(f"Duplicate filename uploaded ({name}). Rename one to prevent accidental quantity duplication.")
            seen_names.add(name.casefold())
            path = job_upload_dir / name
            f.save(path)
            paths.append(path)
        
        c = ginesys_config_loader.load_config()
        combo_file = ginesys_config_loader.MASTER_DIR / "combo_mapping.xlsx"
        parsed = ginesys_parser.parse_files(paths, c, combo_file)
        
        op_id = request.form.get("operator_id")
        operator = get_ginesys_operator(op_id)
        
        precheck = ginesys_posting.precheck_documents(parsed["documents"], c, operator=operator)
        ready = bool(parsed["documents"])
        status = "VALIDATED" if ready else "BLOCKED"
        
        job = {
            "job_id": job_id,
            "documents": parsed["documents"],
            "warnings": parsed["warnings"],
            "stats": parsed["stats"],
            "combo_mappings": parsed["combo_mappings"],
            "ginesys_precheck": precheck,
            "ready": ready,
            "status": status,
            "operator_id": operator["id"] if operator else None,
            "rate_handling": "Exact barcode lookup from Utility/SelectItem; pricing is carried into DC/Save",
        }
        ginesys_job_store.save_job(job_id, job)
        
        preview = [{
            "reference": d["reference"],
            "date": d["date"],
            "store": d["store_name"],
            "siteCode": d["site_code"],
            "state": d.get("state_code"),
            "gstAppl": d.get("gst_appl"),
            "gstinConfigured": bool(d.get("site_gstin")),
            "docScheme": d.get("doc_scheme_code") or d.get("invoice_doc_code"),
            "lines": d["line_count"],
            "qty": d["total_qty"],
            "ginesysStatus": d["ginesys_precheck"]["status"],
            "dcNumber": d["ginesys_precheck"].get("dc_number") or d["ginesys_precheck"].get("dc_code"),
            "transferNumber": d["ginesys_precheck"].get("transfer_number") or d["ginesys_precheck"].get("transfer_code"),
        } for d in parsed["documents"][:100]]
        
        return jsonify({
            "success": True,
            "jobId": job_id,
            "ready": ready,
            "summary": {
                "stores": len({str(d["store_name"]).strip().casefold() for d in parsed["documents"] if d.get("store_name")}),
                "documents": len(parsed["documents"]),
                "sourceLines": parsed["stats"]["source_lines"],
                "validLines": parsed["stats"]["valid_lines"],
                "comboLines": parsed["stats"]["combo_lines"],
                "totalQty": sum(float(d["total_qty"]) for d in parsed["documents"]),
                **precheck["summary"],
            },
            "warnings": parsed["warnings"][:30],
            "preview": preview,
        })
    except GinesysLoginConflict as exc:
        return jsonify({"success": False, "requiresSessionLogout": True, "error": str(exc)}), 409
    except Exception as exc:
        shutil.rmtree(job_upload_dir, ignore_errors=True)
        traceback.print_exc()
        return jsonify({"success": False, "error": str(exc)}), 422

@app.route("/api/ginesys/post/<job_id>", methods=["POST"])
@login_required
def api_ginesys_post(job_id):
    try:
        data = request.get_json(silent=True) or request.form or {}
        op_id = data.get("operator_id")
        c = ginesys_config_loader.load_config()
        config_errors, _ = ginesys_config_loader.check_config(c)
        if config_errors:
            return jsonify({"success": False, "error": "Live posting blocked: " + "; ".join(config_errors)}), 422
        
        job = ginesys_job_store.load_job(job_id)
        if not job.get("ready"):
            return jsonify({"success": False, "error": "This job is not ready for posting. Please validate again."}), 422
        
        target_op_id = op_id or job.get("operator_id")
        operator = get_ginesys_operator(target_op_id)
        if not operator or not operator.get("ginesys_username"):
            return jsonify({"success": False, "error": "No authorized Ginesys operator configured."}), 422
        
        ginesys_parser.refresh_document_site_config(job["documents"], c)
        posted = ginesys_posting.post_documents(job["documents"], c, operator=operator)
        
        result_name = f"Ginesys_Posting_Result_{job_id[:8]}.xlsx"
        result_path = ginesys_job_store.RESULTS / result_name
        ginesys_result_writer.write_result(
            result_path,
            job["documents"],
            posted["results"],
            posted["api_calls"],
            validation_status="POSTED"
        )
        
        job["post_results"] = posted["results"]
        job["api_calls"] = posted["api_calls"]
        job["post_summary"] = posted["summary"]
        ginesys_job_store.save_job(job_id, job)
        
        # Log to history.db
        try:
            db = get_db()
            op_name = operator.get("display_name") or operator.get("ginesys_username")
            total_q = int(sum(float(d.get("total_qty", 0)) for d in job["documents"]))
            db.execute(
                "INSERT INTO logs (timestamp, channel, warehouse, ebo_type, invoices, sku_lines, total_qty, missing) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "GINESYS",
                 f"{op_name} (Site {c.owner_site_code})",
                 f"Adhoc DC + Transfer Out ({posted['summary']['success']} Posted / {posted['summary']['failed']} Failed)",
                 len(job["documents"]),
                 job["stats"].get("valid_lines", len(job["documents"])),
                 total_q,
                 posted["summary"].get("failed", 0))
            )
            db.commit()
        except Exception:
            pass
        
        return jsonify({
            "success": True,
            "summary": posted["summary"],
            "results": posted["results"],
            "downloadUrl": f"/api/ginesys/results/{result_name}"
        })
    except GinesysLoginConflict as exc:
        return jsonify({"success": False, "requiresSessionLogout": True, "error": str(exc)}), 409
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(exc)}), 500

@app.route("/api/ginesys/results/<path:filename>", methods=["GET"])
@login_required
def api_ginesys_result_file(filename):
    safe_name = ginesys_utils.safe_filename(filename)
    return send_from_directory(ginesys_job_store.RESULTS, safe_name, as_attachment=True)

@app.route("/api/ginesys/retry/<job_id>", methods=["POST"])
@login_required
def api_ginesys_retry(job_id):
    try:
        data = request.get_json(silent=True) or request.form or {}
        op_id = data.get("operator_id")
        c = ginesys_config_loader.load_config()
        job = ginesys_job_store.load_job(job_id)
        
        target_op_id = op_id or job.get("operator_id")
        operator = get_ginesys_operator(target_op_id)
        if not operator or not operator.get("ginesys_username"):
            return jsonify({"success": False, "error": "No authorized Ginesys operator configured."}), 422
        
        ginesys_parser.refresh_document_site_config(job["documents"], c)
        posted = ginesys_posting.post_documents(job["documents"], c, operator=operator)
        
        result_name = f"Ginesys_Posting_Result_{job_id[:8]}.xlsx"
        result_path = ginesys_job_store.RESULTS / result_name
        ginesys_result_writer.write_result(
            result_path,
            job["documents"],
            posted["results"],
            posted["api_calls"],
            validation_status="POSTED"
        )
        
        job["post_results"] = posted["results"]
        job["api_calls"] = posted["api_calls"]
        job["post_summary"] = posted["summary"]
        ginesys_job_store.save_job(job_id, job)
        
        return jsonify({
            "success": True,
            "summary": posted["summary"],
            "results": posted["results"],
            "downloadUrl": f"/api/ginesys/results/{result_name}"
        })
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(exc)}), 500

def get_login_html():
    path = os.path.join(os.path.dirname(__file__), "templates", "login.html")
    with open(path, encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    init_db()
    app.run(debug=True, port=9000, use_reloader=False, threaded=True)
