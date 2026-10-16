"""
POS provider factory.

Resolves the correct POSProvider for a tenant based on tenant.pos_type and the
tenant's stored (encrypted) connection credentials. This is the single place
that decides "which POS" — everything else just calls the returned provider.

Adding a new POS later:
  1. write app/pos/<vendor>.py with a @register_provider subclass
  2. store its creds in a connection row
  3. set tenant.pos_type = "<vendor>"
No other code changes.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.tenant import Tenant, GoposConnection
from app.pos.base import POSProvider, get_provider_class
import app.pos.gopos  # noqa: F401 — registers GoPosProvider

logger = logging.getLogger(__name__)


def build_provider_for_tenant(db: Session, tenant: Tenant) -> Optional[POSProvider]:
    """
    Construct the tenant's POS provider, or None if not connected yet.
    Credentials are decrypted only here, in memory, at call time.
    """
    pos_type = (tenant.pos_type or "gopos").lower()

    if pos_type == "gopos":
        conn = db.query(GoposConnection).filter_by(tenant_id=tenant.id).first()
        if not conn:
            logger.info(f"Tenant {tenant.id} has no GoPOS connection yet")
            return None
        cls = get_provider_class("gopos")
        return cls(
            client_id=decrypt_secret(conn.client_id_encrypted),
            client_secret=decrypt_secret(conn.client_secret_encrypted),
            organization_id=conn.organization_id,
        )

    # Future POS vendors resolve here by their own connection model.
    raise NotImplementedError(
        f"POS type '{pos_type}' not implemented yet. Registered: register a "
        f"@register_provider subclass and a connection model."
    )
