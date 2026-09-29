# ============================================================
#  utils/odoo_api.py — Odoo XML-RPC API Wrapper
# ============================================================

import xmlrpc.client
import logging

logger = logging.getLogger(__name__)


class OdooAPI:
    """
    Thin wrapper around Odoo's XML-RPC interface.
    Supports read, search, create, write (update), and upload operations.
    """

    def __init__(self, url: str, db: str, username: str, api_key: str):
        self.url      = url.rstrip("/")
        self.db       = db
        self.username = username
        self.api_key  = api_key
        self.uid      = None

        self._common  = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models  = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    # ----------------------------------------------------------
    # Authentication
    # ----------------------------------------------------------
    def authenticate(self) -> int:
        """Authenticate and store the user ID. Returns uid."""
        logger.info(f"Authenticating to {self.url} as {self.username} ...")
        self.uid = self._common.authenticate(self.db, self.username, self.api_key, {})
        if not self.uid:
            raise ConnectionError("❌ Odoo authentication failed. Check credentials in config.py")
        logger.info(f"✅ Authenticated — uid: {self.uid}")
        return self.uid

    # ----------------------------------------------------------
    # Core Operations
    # ----------------------------------------------------------
    def search_read(self, model: str, domain: list, fields: list, limit: int = 0) -> list:
        """Search and read records from an Odoo model."""
        return self._models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "search_read",
            [domain],
            {"fields": fields, "limit": limit},
        )

    def search(self, model: str, domain: list) -> list:
        """Return record IDs matching domain."""
        return self._models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "search", [domain],
        )

    def read(self, model: str, ids: list, fields: list) -> list:
        """Read specific fields from records by ID."""
        return self._models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "read", [ids], {"fields": fields},
        )

    def create(self, model: str, values: dict) -> int:
        """Create a new record and return its ID."""
        return self._models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "create", [values],
        )

    def write(self, model: str, ids: list, values: dict) -> bool:
        """Update records by ID."""
        return self._models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "write", [ids, values],
        )

    def get_fields(self, model: str) -> dict:
        """Return field definitions for a model (useful for debugging)."""
        return self._models.execute_kw(
            self.db, self.uid, self.api_key,
            model, "fields_get", [],
            {"attributes": ["string", "type", "required"]},
        )

    # ----------------------------------------------------------
    # Bulk helpers
    # ----------------------------------------------------------
    def bulk_create(self, model: str, records: list[dict]) -> list:
        """Create multiple records. Returns list of new IDs."""
        ids = []
        for i, rec in enumerate(records, 1):
            try:
                new_id = self.create(model, rec)
                ids.append(new_id)
                logger.info(f"  [{i}/{len(records)}] Created ID {new_id}")
            except Exception as e:
                logger.error(f"  [{i}/{len(records)}] FAILED: {e} | Record: {rec}")
        return ids

    def bulk_write(self, model: str, records: list[dict], id_field: str = "id") -> int:
        """
        Update multiple records. Each dict in records must contain the id_field.
        Returns count of successful updates.
        """
        success = 0
        for i, rec in enumerate(records, 1):
            rec_id = rec.pop(id_field)
            try:
                self.write(model, [rec_id], rec)
                success += 1
                logger.info(f"  [{i}/{len(records)}] Updated ID {rec_id}")
            except Exception as e:
                logger.error(f"  [{i}/{len(records)}] FAILED ID {rec_id}: {e}")
        return success
