# Odoo → Ginesys DC + Transfer Out Automation

## End-to-end operating process

**Version:** V4.5 Bearer WebAPI  
**Purpose:** Read Odoo invoice/internal-transfer exports, create a Ginesys Adhoc Delivery Challan (DC), calculate GST, and create the linked GST Transfer Out.

This note describes the current working process. Please review the items marked **Confirm/Change** before using the automation for bulk posting.

---

## 1. Files and folders

Keep these files together in the project folder:

| File/folder | Purpose |
|---|---|
| `Run Portal.bat` | Starts the local web portal. |
| `Check Configuration.bat` | Checks global configuration. |
| `.env` | Global configuration and credential-encryption key. Never share this file. |
| `Master/Ginesys_Config.xlsx` | Main API configuration and Odoo-store-to-Ginesys site mapping. |
| `Master/site_master.xlsx` | Current site/GSTIN master. The `#Site#_EBO` sheet is read automatically. |
| `Master/site_webapi_overrides.json` | Optional site-specific state, GSTIN, GST flag, OU, tax, or scheme overrides. |
| `Master/site_state_overrides.json` | Optional state-abbreviation overrides. |
| `Master/combo_mapping.xlsx` | Optional parent-combo-to-child barcode mapping. |
| `portal.db` | Local duplicate/recovery history. Keep it during normal operation. |
| `portal-data/results` | Validation and posting result workbooks. |
| `portal-data/jobs` | Saved job data used by Retry Failed/recovery. |

Do not delete `portal.db`, `portal-data/jobs`, or the result workbooks during normal use.

---

## 2. One-time setup

1. Extract the project to a local folder.
2. Run **`Setup or Repair Environment.bat`**.
3. Open the project-root **`.env`**.
4. Set `GINESYS_CREDENTIAL_ENCRYPTION_KEY` to a stable Fernet key. User Ginesys credentials are entered in the Admin Portal and encrypted in `portal.db`.
5. Confirm the important values are present:

   ```text
   GINESYS_GST_APPL=Y
   GINESYS_TRADE_GROUP_CODE=2
   GINESYS_DOC_SCHEME_SAME_STATE=215
   GINESYS_DOC_SCHEME_INTERSTATE=215
   HOST=127.0.0.1
   PORT=5055
   ```

   The port shown in the project `.env` and in the startup console is authoritative. A fresh package may have a different default port.

6. Run **`Check Configuration.bat`**, then use **Test Connection** for every user in the Admin Portal.
7. Run **`Run Portal.bat`**.
8. Open the URL printed in the console, normally `http://localhost:5055` for the current setup. Confirm that the page header shows **V4.5**.

### Per-user authentication rule

Bearer sessions can expire, but the portal renews them automatically. If renewal is rejected, verify that user's saved Ginesys credentials and run Test Connection. Never paste passwords or tokens into Excel, screenshots, email, or chat.

There is no separate portal password. Admin authorizes Ginesys usernames and assigns portal roles; each user signs in with their own Ginesys Web password. Error `012` requires explicit confirmation before the existing Ginesys Web session is logged out.

---

## 3. Site master maintenance

### 3.1 Main site mapping

`Master/Ginesys_Config.xlsx`, sheet `SITE_MASTER`, maps the Odoo store/customer name to:

- Ginesys destination site code
- Ginesys destination site name
- state information
- optional tax/OU/agent/transporter values
- optional document scheme and GST flag

The Odoo store name in the uploaded file must match an active mapping.

### 3.2 GSTIN source

`Master/site_master.xlsx` is read automatically. The preferred sheet is `#Site#_EBO`; the first sheet is used if that sheet is absent. The automation matches by `odoo_name` first and by site `Code` second, then reads the destination `GSTIN`.

For every destination site, verify:

1. GSTIN is present and exactly 15 characters.
2. GSTIN begins with the configured numeric GST state code.
3. `GST Applicable` is `Y`.
4. State code is correct (`33` = Tamil Nadu, `27` = Maharashtra, etc.).
5. Site code and Odoo store name are correct.

The automation blocks a selected site before posting if its GSTIN is blank, malformed, or has a state-prefix mismatch. Correct the master and validate/retry again.

### 3.3 Overrides

Use `Master/site_webapi_overrides.json` only for exceptions that cannot be maintained in the workbook. Overrides take precedence over workbook values. Typical fields are:

```json
{
  "Exact Odoo store name": {
    "state": "MH",
    "gst_state_code": "27",
    "gstin": "27...",
    "site_tax_code": 4,
    "ou_code": 1,
    "doc_scheme_code": 215,
    "gst_appl": "Y"
  }
}
```

After any master/override change, run validation again. A retry also refreshes the stored job with the current site master; it does not require creating a new DC.

---

## 4. Current GST and scheme rules

All transfers in this deployment are treated as **GST outward-supply transactions**.

- `gstAppl` / `gSTAppl`: `Y`
- Trade group: `2`
- Owner site: `228`
- Owner GST state: `33`
- Owner GSTIN: configured owner GSTIN (`33AAFCT5162N1Z1` in the current setup)
- Same-state Transfer Out GST scheme: `215`
- Interstate Transfer Out GST scheme: `215`

Scheme selection compares the destination GST state with owner state `33`. A site-specific `doc_scheme_code` override takes precedence. Confirm any new scheme with one controlled test before bulk posting.

---

## 5. Accepted Odoo input files

Supported file types are `.xlsx`, `.xls`, and `.csv`. Invoice and Internal Transfer files may be uploaded together.

The parser supports these logical fields:

| Logical field | Typical Odoo columns |
|---|---|
| Reference | `Invoice lines/Number` or `Operations/Reference` |
| Date | `Invoice lines/Date` or `Operations/Date` |
| Barcode | `Invoice lines/Product/Barcode` or `Operations/Product/Barcode` |
| SKU (optional reference) | Product/Internal Reference |
| Quantity | Invoice quantity or `Operations/Qty Done` |
| Destination | `Delivery Address/Display Name` or `Customer` |

Before upload, confirm:

- barcode is present for every item row;
- quantity is numeric and greater than zero;
- reference, date, and destination are present;
- destination name exists in the site master;
- the same file is not selected twice;
- each selected filename is unique;
- the Odoo source is the intended date/period.

Blank group/subtotal rows are ignored. Item rows with errors block the complete upload; no partial document is posted.

If `combo_mapping.xlsx` contains a matching parent SKU/EAN, the parent is split into child barcodes. Duplicate SKU/barcode lines within a document are consolidated before posting.

---

## 6. Daily operating procedure

### Step 1 — Check readiness

1. Confirm the correct Odoo export files are available.
2. Confirm the destination sites and GSTINs are updated in the master.
3. Confirm your portal connection status is connected.
4. Use **Test Ginesys Connection** if authentication has not recently succeeded.
5. Confirm the portal is open on the URL/port printed by `Run Portal.bat`.

### Step 2 — Upload and validate

1. In the portal, choose **Upload Odoo Raw Files**.
2. Select one or more Odoo `.xlsx`, `.xls`, or `.csv` files.
3. Click **Validate & Build Posting Preview**.
4. Review every document in the preview:
   - Odoo reference;
   - source date;
   - store/customer;
   - destination site code;
   - state and GSTIN availability;
   - GST flag;
   - Transfer Out scheme;
   - item line count and total quantity;
   - warnings.
5. Download the validation workbook if a record-by-record review is required.

Do not post while validation is blocked or while any site, GSTIN, state, scheme, quantity, or barcode mapping is incorrect. Correct the source/master and validate again.

### Step 3 — Approve live posting

1. Tick the confirmation stating that live Ginesys Adhoc DC and Transfer Out transactions will be created.
2. Click **POST TO GINESYS** once.
3. Do not refresh or click the button repeatedly while the request is running.

The portal serializes posting and performs duplicate/recovery checks, but repeated manual clicks should still be avoided.

### Step 4 — Review the posting result

For each document, verify:

- status is `SUCCESS` or `ALREADY_POSTED`;
- DC number is present;
- Transfer Out number is present;
- item count/quantity is expected;
- the transaction is visible in Ginesys;
- GST/scheme is correct.

Download the generated `Ginesys_Posting_Result_<job-id>.xlsx` workbook and retain it with the source export.

---

## 7. What the automation does in Ginesys

For each validated document, the sequence is:

1. Search `DC/GetAdhocList` for an existing matching Odoo marker/DC.
2. Look up every barcode exactly through `Utility/SelectItem`.
3. Require one exact barcode match; zero or multiple matches block the document.
4. Request a non-zero challan code and packet barcode from `DC/GetPacketBarcode`.
5. Create the Adhoc DC through `DC/Save` using returned Ginesys item/pricing data.
6. Persist the DC code/number immediately in local recovery storage.
7. Search Ginesys again before creating Transfer Out.
8. Read the DC through `SI/GetDCDetails`.
9. Calculate item GST through `SI/CalculateItemCharges`.
10. Create the GST Transfer Out through `SI/Save` with header/item GST charges.
11. Save the final status and produce the result workbook.

The DC and SI save calls are not blindly retried because they are non-idempotent. Recovery checks are used before a retry so a lost response does not create another transaction.

---

## 8. Retry and recovery procedure

Use **Retry Failed Only** when a document has a recoverable failure.

### If the DC was created but Transfer Out failed

1. Do not create a new DC manually.
2. Correct the relevant master/configuration issue if applicable.
3. Click **Retry Failed Only**.
4. The retry reuses the stored DC and refreshes current site/GST settings.
5. Confirm the final Transfer Out number in Ginesys.

### If the error says duplicate Document No.

1. Search Ginesys using the Odoo reference, DC number, remarks marker, and/or Transfer Out number.
2. If the transaction already exists and is `Invoiced`, do not post again. Record the existing Transfer Out number.
3. Treat the local failed result as stale/retry information and keep the existing Ginesys transaction.
4. Only use a new document number if the business owner confirms that the original transaction does not belong to this source document.

### If the result says outcome is unknown

Do not immediately retry. First check Ginesys and run the normal recovery/retry path. The result may have been committed even if the browser lost the response.

---

## 9. Result workbooks

### Validation workbook

Validation results are reviewed directly in the portal; a separate validation workbook is not generated.

### Posting workbook

`Ginesys_Posting_Result_<job-id>.xlsx` contains:

- **Summary:** Odoo reference, source date, store, site, scheme, DC code/number, Transfer Out code/number, status, and error.
- **Items:** Odoo SKU, returned Ginesys item, exact barcode, quantity, MRP, basic rate, discount factor, discount, final rate, and WSP.
- **API_Log:** Ginesys username, endpoint, HTTP status, success, safe message, request ID, and duration. Bearer tokens and request headers are not written here.

Recommended filing convention: keep the source Odoo export, validation workbook, posting workbook, and any manual Ginesys verification reference together.

---

## 10. Common errors and action

| Error | Meaning | Action |
|---|---|---|
| `Session is invalid` / `Session has expired` / HTTP 401 | Credentials or renewed token were rejected | Ask an admin to verify the user's saved Ginesys credentials, then run Test Connection. |
| Only `localhost:5055` opens | The portal is using the configured port | Use the URL printed by `Run Portal.bat`; check `PORT` in `.env`. |
| No SITE_MASTER mapping | Odoo destination name is not mapped | Add/correct the exact store mapping, then validate again. |
| GSTIN blank/invalid/state mismatch | Destination GST data is incomplete or inconsistent | Correct `Master/site_master.xlsx`, `SITE_MASTER`, or the JSON override. |
| GST outward-supply scheme inappropriate | GST flag and scheme do not agree | Confirm `GST_APPL=Y`, trade group `2`, and GST scheme `215` or the approved site override. |
| Exact barcode match not found | Ginesys item lookup returned zero or multiple identical-barcode rows | Correct the barcode/source or Ginesys item master before posting. |
| DC created, Transfer Out not created | SI/Save failed after DC creation | Use Retry Failed Only; do not create another DC. |
| Duplicate Document No. | The document number already exists in Ginesys, often because a prior attempt completed | Search Ginesys first; do not repost an already invoiced transaction. |
| Network/server error during Save | Save outcome may be unknown | Check Ginesys before retrying; use recovery. |
| Configuration blocked before posting | Required token/master/config value is missing | Read the displayed error, correct the source, and validate again. |

---

## 11. Date handling

- The Odoo document date is preserved in the parsed document and result workbook.
- `GINESYS_WORK_DATE` controls the active Ginesys work/posting date.
- When `GINESYS_WORK_DATE` is blank, the automation uses the current system date for the active Ginesys work date.
- Set `GINESYS_WORK_DATE=YYYY-MM-DD` only when Ginesys is deliberately operating on another work date.

Confirm the required date policy with Finance/SCM before backdated or period-end posting.

---

## 12. Security and operational controls

- Treat `portal.db` and the encryption key as secrets; back them up separately and securely.
- Keep the portal bound to `127.0.0.1` unless an authenticated, protected deployment is deliberately configured.
- Do not share `.env`, token screenshots, browser headers, or raw API request payloads.
- Keep `portal.db` and job files for recovery; back them up with restricted access.
- Do not delete or manually edit a job after a DC has been created unless the recovery impact is understood.
- Verify live Ginesys results after every batch, especially the first interstate transaction after a scheme/configuration change.

---

## 13. Operator sign-off checklist

Before posting:

- [ ] Correct source Odoo file and period selected.
- [ ] Each posting user's Ginesys connection tested.
- [ ] Site mapping reviewed.
- [ ] Destination GSTIN/state reviewed.
- [ ] GST flag is `Y`.
- [ ] Scheme is correct for same-state/interstate.
- [ ] References and quantities are correct.
- [ ] Validation shows no blocking errors.
- [ ] Live-post confirmation is intentionally selected.

After posting:

- [ ] Every expected document has `SUCCESS` or `ALREADY_POSTED`.
- [ ] DC number is recorded.
- [ ] Transfer Out number is recorded.
- [ ] Ginesys status is verified as expected.
- [ ] Result workbook is filed with the source export.
- [ ] Any failure is handled through recovery/Retry Failed Only.

---

## 14. Items to confirm or update

Please review and confirm these business decisions:

1. Is `PORT=5055` the permanent operator URL, or should the package default be standardized?
2. Is scheme `215` approved as the GST Transfer Out numbering scheme for both same-state and interstate transfers?
3. Should every transfer remain GST outward supply (`GST_APPL=Y`) without exceptions?
4. Is the current `Master/site_master.xlsx` the single approved source for destination GSTINs?
5. Should `GINESYS_WORK_DATE` remain blank for daily posting, or be set by the operator for each accounting date?
6. What is the approved business action when the Odoo reference already exists in Ginesys?
7. Are any additional approval, filing, or reconciliation reports required after posting?
