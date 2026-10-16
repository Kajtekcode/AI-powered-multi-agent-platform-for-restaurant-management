"""
GoPOS API Client — fixed to match actual API spec.

Key facts from gopos-openapi-v3.json:
- All list responses: {"data": [...]}   (NOT "content"/"items")
- Pagination: page=0-based, size=max 100, NO totalPages in response
  → stop when len(data) < size (last page)
- `include` param: array type, sent as comma-separated string "a,b,c"
- `status` param: array type, sent as repeated params or single value
- `organization_id` is a PATH param: /api/v3/{organization_id}/...
"""

import time
import logging
from typing import Any, Dict, Iterator, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_not_exception_type

from app.core.config import settings

logger = logging.getLogger(__name__)


class ReadOnlyViolationError(Exception):
    """Raised when a write is attempted while GOPOS_READ_ONLY=true."""


class GoPosAuthError(Exception):
    """Authentication / token error."""


class GoPosClient:

    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None,
                 organization_id: Optional[str] = None):
        # Per-tenant credentials when provided; otherwise fall back to global
        # settings (preserves the original single-tenant / CLI behavior).
        self.client_id = client_id or settings.gopos_client_id
        self.client_secret = client_secret or settings.gopos_client_secret
        self.organization_id = organization_id or settings.gopos_organization_id
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._client = httpx.Client(
            base_url=settings.gopos_base_url,
            timeout=30.0,
        )

    # ------------------------------------------------------------------ #
    # Auth                                                                  #
    # ------------------------------------------------------------------ #

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token

        logger.info("Fetching GoPOS OAuth token...")
        resp = self._client.post(
            "/oauth/token",
            data={
                "grant_type":      "organization",
                "client_id":       self.client_id,
                "client_secret":   self.client_secret,
                "organization_id": self.organization_id,
            },
        )
        if resp.status_code != 200:
            raise GoPosAuthError(
                f"Auth failed {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = now + data.get("expires_in", 3600)
        logger.info("GoPOS auth OK")
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ------------------------------------------------------------------ #
    # Core request                                                          #
    # ------------------------------------------------------------------ #

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_not_exception_type(ReadOnlyViolationError),
           reraise=True)
    def _request(self, method: str, path: str, *, write: bool = False, **kwargs) -> Any:
        if write and settings.gopos_read_only:
            raise ReadOnlyViolationError(
                f"Write blocked (GOPOS_READ_ONLY=true): {method.upper()} {path}"
            )
        # Inject org_id into path
        path = path.format(org=self.organization_id)

        resp = self._client.request(method, path, headers=self._headers(), **kwargs)

        if resp.status_code == 401:
            self._token = None  # force refresh
            resp = self._client.request(method, path, headers=self._headers(), **kwargs)

        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        return self._request("GET", path, params=params or {})

    # ------------------------------------------------------------------ #
    # Pagination                                                            #
    # ------------------------------------------------------------------ #
    # GoPOS response format: {"data": [...]}  — no totalPages
    # Pagination stops when len(data) < size (last page reached)
    # ------------------------------------------------------------------ #

    def _paginate(self, path: str, params: Optional[Dict] = None, size: int = 100) -> Iterator[Dict]:
        """Yield all items across pages. Response is always {"data": [...]}."""
        params = dict(params or {})
        params["size"] = size
        page = 0

        while True:
            params["page"] = page
            logger.debug(f"GET {path} page={page} params={params}")
            raw = self._request("GET", path, params=params)

            # GoPOS wraps everything in {"data": [...]}
            if isinstance(raw, dict):
                items = raw.get("data", [])
            elif isinstance(raw, list):
                items = raw  # shouldn't happen but be safe
            else:
                items = []

            if not items:
                break

            yield from items

            if len(items) < size:
                break  # last page

            page += 1

    def fetch_all(self, path: str, size: int = 100, **params) -> List[Dict]:
        """Convenience: return flat list from all pages."""
        return list(self._paginate(path, params, size=size))

    # ------------------------------------------------------------------ #
    # Orders                                                                #
    # ------------------------------------------------------------------ #

    def list_orders(
        self,
        closed_at_from: Optional[str] = None,
        closed_at_to:   Optional[str] = None,
        status:         Optional[str] = "CLOSED",
        include:        Optional[str] = "items,items.product,employee,transactions",
        size:           int = 100,
    ) -> Iterator[Dict]:
        """
        List orders page by page.
        `include` is comma-separated: "items,items.product,employee"
        `status`  is sent as a single value (GoPOS accepts it)
        """
        params: Dict[str, Any] = {}
        if closed_at_from:
            params["closed_at_from"] = closed_at_from
        if closed_at_to:
            params["closed_at_to"] = closed_at_to
        if status:
            params["status"] = status
        if include:
            # API accepts comma-separated (per their example: "value1,value2")
            params["include"] = include

        yield from self._paginate("/api/v3/{org}/orders", params, size=size)

    def get_order(self, order_id: str, include: str = "items,items.product,employee,transactions") -> Dict:
        return self._get(
            f"/api/v3/{{org}}/orders/{order_id}",
            {"include": include},
        )

    # ------------------------------------------------------------------ #
    # Reports                                                               #
    # ------------------------------------------------------------------ #

    def get_reports_orders(self, **params) -> List[Dict]:
        return self.fetch_all("/api/v3/reports/orders", **params)

    def get_reports_order_items(self, **params) -> List[Dict]:
        return self.fetch_all("/api/v3/reports/order_items", **params)

    # ------------------------------------------------------------------ #
    # POS Reports                                                           #
    # ------------------------------------------------------------------ #

    def list_pos_reports(
        self,
        closed_at_from: Optional[str] = None,
        closed_at_to:   Optional[str] = None,
    ) -> Iterator[Dict]:
        params: Dict[str, Any] = {}
        if closed_at_from:
            params["closed_at_from"] = closed_at_from
        if closed_at_to:
            params["closed_at_to"] = closed_at_to
        yield from self._paginate("/api/v3/{org}/pos_reports", params)

    # ------------------------------------------------------------------ #
    # Menu / Items                                                           #
    # ------------------------------------------------------------------ #

    def list_items(self, include: str = "category,item_group,tax,stock_info") -> List[Dict]:
        """
        include is comma-separated per GoPOS spec example.
        Valid values: item_group, category, tax, stock_info, modifier_groups, ...
        """
        return self.fetch_all("/api/v3/{org}/items", include=include)

    def list_categories(self) -> List[Dict]:
        return self.fetch_all("/api/v3/{org}/categories")

    def list_taxes(self) -> List[Dict]:
        """Tax rates: [{id, name, rate, code}] — Poland: 8% food, 23% drinks etc."""
        return self.fetch_all("/api/v3/{org}/taxes")

    # ------------------------------------------------------------------ #
    # Employees                                                             #
    # ------------------------------------------------------------------ #

    def list_employees(self) -> List[Dict]:
        return self.fetch_all("/api/v3/{org}/employees")

    def list_work_times(
        self,
        date_from: Optional[str] = None,
        date_to:   Optional[str] = None,
    ) -> List[Dict]:
        params: Dict[str, Any] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self.fetch_all("/api/v3/{org}/work_times", **params)

    # ------------------------------------------------------------------ #
    # Dimensions                                                            #
    # ------------------------------------------------------------------ #

    def list_directions(self) -> List[Dict]:
        return self.fetch_all("/api/v3/{org}/directions")

    def list_points_of_sale(self) -> List[Dict]:
        return self.fetch_all("/api/v3/{org}/points_of_sale")

    def list_payment_methods(self) -> List[Dict]:
        return self.fetch_all("/api/v3/{org}/payment_methods")

    # ------------------------------------------------------------------ #
    # Webhooks (write=True → blocked by read-only guard)                   #
    # ------------------------------------------------------------------ #

    def list_webhooks(self) -> List[Dict]:
        return self._get("/api/v3/{org}/webhooks")

    def register_webhook(self, name: str, url: str, resource_type: str) -> Dict:
        return self._request(
            "POST", "/api/v3/{org}/webhooks", write=True,
            json={"name": name, "url": url, "resource_type": resource_type},
        )

    # ------------------------------------------------------------------ #
    # Utility                                                               #
    # ------------------------------------------------------------------ #

    def ping(self) -> bool:
        try:
            self._get_token()
            return True
        except Exception as e:
            logger.error(f"GoPOS ping failed: {e}")
            return False

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


_gopos_client: Optional[GoPosClient] = None


def get_gopos_client() -> GoPosClient:
    global _gopos_client
    if _gopos_client is None:
        _gopos_client = GoPosClient()
    return _gopos_client
