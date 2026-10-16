"""
Webhook receiver — accepts POS event pushes.

Transitional tenant resolution: a webhook currently maps to the single connected
tenant (Round B will map the POS organization_id in the payload to the right
tenant). On an order event we trigger a throttled incremental sync via the
provider rather than calling the vendor client directly.
"""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/gopos")
async def receive_gopos_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    resource_type = body.get("type", "UNKNOWN")
    event_type = body.get("event_type", "UNKNOWN")
    resource_id = str(body.get("resource_id", ""))
    logger.info(f"Webhook: {resource_type} {event_type} id={resource_id}")

    if resource_type == "ORDER" and resource_id:
        try:
            from app.data.sync import active_tenant_ids, maybe_sync_tenant
            ids = active_tenant_ids()
            if ids:
                # one connected tenant today; Round B routes by org id
                maybe_sync_tenant(ids[0], min_interval_minutes=0)
                logger.info(f"Webhook triggered sync for tenant {ids[0]}")
        except Exception as e:
            logger.warning(f"Webhook sync failed for {resource_id}: {e}")

    return JSONResponse({"status": "ok"})


@router.get("/gopos/test")
async def webhook_test():
    return {"status": "ok", "message": "Webhook endpoint is active"}
