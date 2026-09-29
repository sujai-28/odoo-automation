# Odoo to Ginesys Adhoc DC + Transfer Out — Bearer WebAPI

This V4.5 Bearer WebAPI package is the upgraded version of the supplied V3 portal. It preserves the existing Odoo parsing, SITE_MASTER mapping, combo split, duplicate consolidation, validation preview, SQLite recovery, Retry Failed, and result Excel workflow while replacing the failing Public API with the Bearer-authenticated endpoints used by Ginesys Web.

V4.5 uses `http://localhost:5064` by default and shows `V4.5` at the top of the page. This avoids accidentally reconnecting to an older portal. The correct result workbook always contains `Summary`, `Items`, and `API_Log` sheets; a one-sheet `Results` workbook came from the obsolete prototype.

When `GINESYS_WORK_DATE` is blank, Ginesys API calls use the current date as the active work/posting date. The Odoo source document date remains unchanged in the validation and result workbooks. Set `GINESYS_WORK_DATE=YYYY-MM-DD` only when Ginesys is deliberately operating on another work date.

The shipped `GINESYS_AVAILABLE_SITE_CODES` is the complete 71-site list from the successful captured `GetAdhocList` request, rather than the obsolete prototype's owner-site-only value. If the Ginesys user's site permissions change, replace this configurable list with the current `availableSites` array from a successful Ginesys Web request.

## Working API flow

```text
Odoo Invoice / Internal Transfer export
  -> parse, map site, split combos, consolidate duplicates
  -> DC/GetAdhocList duplicate/recovery check
  -> Utility/SelectItem exact barcode lookup
  -> DC/GetPacketBarcode challan code/barcode allocation
  -> DC/Save
  -> persist DC code immediately
  -> DC/GetAdhocList recovery check
  -> SI/GetDCDetails
  -> SI/CalculateItemCharges
  -> SI/Save
  -> result Excel with DC and Transfer Out numbers
```

`Utility/SelectItem` must return exactly one row whose barcode is identical to the Odoo barcode. Zero or multiple exact matches block posting. The barcode is the sole item identity used for posting. The Odoo SKU column and value are optional, are never compared with the Ginesys item ID, and are retained only as reference data in the result workbook.

Immediately before `DC/Save`, the portal calls `DC/GetPacketBarcode` once and places its returned non-zero challan code and packet barcode into the save payload. If either value is missing, the DC is blocked before Save. This mirrors the Ginesys Web add-document sequence and prevents the validation error that occurs when `code=0` or `packetBarcode` is blank.

## First setup

1. Extract the ZIP to a normal local folder.
2. Run `Setup or Repair Environment.bat`.
3. Open `.env` and set:

   ```text
   GINESYS_CREDENTIAL_ENCRYPTION_KEY=YOUR_FERNET_KEY
   ```

   Generate it with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Do not change it after users are created.
4. Run `Check Configuration.bat` to validate global configuration.
5. Run `Run Portal.bat`, sign in with the administrator's Ginesys username/password, then authorize each additional Ginesys username and role in Admin. Users supply their own password when signing in.

The portal binds to `127.0.0.1` by default. Do not change `HOST` to `0.0.0.0` unless you add an authenticated reverse proxy, HTTPS, and CSRF protection.

The portal obtains a Bearer token from `/WebAPI/token` for the logged-in user, caches it encrypted, and renews it before expiry or after HTTP 401.

Portal login uses the same username and password as Ginesys Web; there is no separate portal password. Admin controls which Ginesys usernames are authorized and whether each is an operator or administrator. If Ginesys returns error `012`, the portal asks before using the same `Session/KillUser` flow as Ginesys Web and retrying login.

## Configuration defaults

- Tenant: `https://technosport.ginesys.cloud`
- Owner site: `228`
- Owner site type: `OS-OO-CM`
- Owner GST state: `33`
- Owner GSTIN: `33AAFCT5162N1Z1`
- DC document: `210`
- Source stock point: `650443`
- Price list/type: `12` / `M`
- Trade group: `2` (captured GST Transfer Out request)
- Party GL: `6`
- GST applicability flag: `Y` (required for every transfer)
- Transfer Out GL: `35`
- Discount/markdown factor: `60`
- Configured same-state GST document scheme: `215`
- Configured interstate GST document scheme: `215`

The previous positional TN/interstate assumption has been removed. Scheme selection now compares numeric GST state codes, then uses explicit configuration:

```text
GINESYS_DOC_SCHEME_SAME_STATE=215
GINESYS_DOC_SCHEME_INTERSTATE=215
```

All transfers are treated as GST outward supply, so the active default Transfer Out scheme is `215` for both same-state and interstate unless a site-specific `doc_scheme_code` in `Master/site_webapi_overrides.json` takes precedence.

## Site metadata

The existing `Master/Ginesys_Config.xlsx` SITE_MASTER remains the source of Odoo store-to-Ginesys site mapping. `Master/site_master.xlsx` is automatically read as the destination GSTIN source using its `odoo_name` (with site code fallback). SITE_MASTER may also include optional `State Code`, `GST State Code`, `GSTIN`, `Document Scheme Code`, and `GST Applicable` columns. JSON overrides still take precedence. State abbreviations in store names or `Master/site_state_overrides.json` remain supported and are converted to numeric GST state codes.

Optional/exception WebAPI fields belong in `Master/site_webapi_overrides.json`:

```json
{
  "Exact Odoo store name": {
    "state": "TN",
    "gst_state_code": "33",
    "gstin": "33...",
    "site_tax_code": 4,
    "ou_code": 1,
    "doc_scheme_code": 133,
    "gst_appl": "Y"
  }
}
```

Same-state sites default to the observed owner GSTIN if no override exists. Every transfer is treated as a GST outward supply. For interstate sites, add the actual destination GSTIN. A selected document with a missing GSTIN is blocked before any Ginesys save call rather than guessed or posted partially.

`GINESYS_SELECT_ITEM_FIELD=Barcode` is configurable because Ginesys selection metadata may vary by tenant. Regardless of the search field, the response is filtered locally to one identical barcode. Odoo and Ginesys may use different SKU aliases without blocking the document.

`GINESYS_WORK_DATE` is optional. Leave it blank to preserve each source document date, or set an explicit `YYYY-MM-DD` Ginesys posting/work date when required.

## Duplicate and failure protection

- A deterministic Odoo document key and `portal.db` prevent local duplicate posting.
- A deterministic `ODOO-<key> | <reference>` marker is saved in DC remarks/UDF.
- `DC/GetAdhocList` is paged and checks that exact marker or a known DC code before DC creation and again before Transfer Out creation.
- `DC/Save` and `SI/Save` are never automatically retried because they are non-idempotent.
- If a save response is lost or returns a server/network error, status is recorded as outcome unknown. Clicking Retry Failed first reconciles Ginesys and only creates a document when no existing transaction is found.
- The DC code is persisted immediately after DC creation. A later Transfer Out failure resumes from that DC.
- Posting requests are serialized to prevent two browser clicks from racing past duplicate checks.
- A Bearer authentication failure stops further Ginesys calls for the batch.

Do not delete `portal.db` unless you deliberately want to remove local duplicate/recovery history. Keep it with the project during normal use and backup.

## Safe first live test

1. Add/verify the destination site metadata.
2. Use a test file with one document, one barcode, and quantity 1.
3. Validate and confirm store, destination site, scheme, quantity, and warnings.
4. Post and verify the DC's barcode, MRP/basic rate/discount/final rate in Ginesys.
5. Verify the linked Transfer Out number and scheme.
6. Test one interstate destination separately before bulk interstate posting.

## Result workbook

- `Summary`: Odoo reference, site, scheme, DC code/number, Transfer Out code/number, status and error.
- `Items`: Odoo SKU, returned Ginesys item, exact barcode, quantity, MRP, basic rate, discount factor, discount, final rate and WSP.
- `API_Log`: endpoint, HTTP status, success, safe message and duration. Tokens and request headers/bodies are never written.

## Supported source formats

Invoice exports use columns such as `Invoice lines/Number`, `Invoice lines/Date`, product barcode, quantity, and `Delivery Address/Display Name`. Product internal reference/SKU is optional.

Internal Transfer exports use `Operations/Reference`, `Operations/Date`, product barcode, `Operations/Qty Done`, and `Customer`. Product internal reference/SKU is optional.

The technical Odoo aliases already supported by V3 remain supported.
