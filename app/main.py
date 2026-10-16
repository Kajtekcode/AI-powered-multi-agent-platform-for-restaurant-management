"""
Restaurant Growth OS — FastAPI Application
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.api import chat, dashboard, recipes, sync, webhooks, notifications, research, invoices

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown tasks."""
    logger.info(f"Starting {settings.app_title} v{settings.app_version}")

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

    # Initialize multi-tenant Postgres schema (identity, domain, future tables).
    # Production uses Alembic migrations; this is a safety net for first run.
    from app.core.db import init_db
    init_db()
    logger.info("Multi-tenant database ready")

    # Background scheduler — ON by default (set DISABLE_SCHEDULER=true to opt out).
    if os.getenv("DISABLE_SCHEDULER", "false").lower() != "true":
        from app.core.scheduler import start_scheduler
        start_scheduler()
        logger.info("Background scheduler started")

    # Startup catch-up sync for all connected tenants — covers downtime
    import threading

    def _startup_sync():
        try:
            from app.data.sync import sync_all_tenants
            result = sync_all_tenants()
            logger.info(f"Startup catch-up sync: {result['tenants']} tenants")
        except Exception as e:
            logger.error(f"Startup sync failed: {e}")

    threading.Thread(target=_startup_sync, name="startup-sync", daemon=True).start()

    # Telegram bot (two-way CEO chat) — starts whenever configured
    if settings.telegram_configured:
        from app.core.telegram_bot import start_telegram_bot
        start_telegram_bot()

    yield

    # Shutdown
    logger.info("Shutting down...")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_title,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs" if not settings.is_production else None,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [os.getenv("ALLOWED_ORIGIN", "")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static files
    static_path = Path(__file__).parent / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    # API routes
    from app.api import auth as auth_api, onboarding as onboarding_api
    app.include_router(auth_api.router, prefix="/api/auth", tags=["auth"])
    app.include_router(onboarding_api.router, prefix="/api/onboarding", tags=["onboarding"])
    app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
    app.include_router(recipes.router, prefix="/api/recipes", tags=["recipes"])
    app.include_router(sync.router, prefix="/api/sync", tags=["sync"])
    app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
    app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"])
    app.include_router(research.router, tags=["research"])
    app.include_router(invoices.router, tags=["invoices"])

    # Page routes (self-contained Alpine.js SPAs)
    templates_dir = Path(__file__).parent / "templates"
    dashboard_html_path = templates_dir / "dashboard.html"
    login_html_path = templates_dir / "login.html"
    onboarding_html_path = templates_dir / "onboarding.html"
    onboarding_gopos_path = templates_dir / "onboarding_gopos.html"

    def _page(path: Path) -> HTMLResponse:
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return _page(login_html_path)

    @app.get("/onboarding", response_class=HTMLResponse)
    async def onboarding_page():
        return _page(onboarding_html_path)

    @app.get("/onboarding/gopos", response_class=HTMLResponse)
    async def onboarding_gopos_page():
        return _page(onboarding_gopos_path)

    # PWA: service worker must be served at root to control full scope
    @app.get("/sw.js")
    async def service_worker():
        sw = Path(__file__).parent / "static" / "sw.js"
        return HTMLResponse(sw.read_text(encoding="utf-8"), media_type="application/javascript")

    @app.get("/manifest.json")
    async def manifest():
        import json as _json
        mf = Path(__file__).parent / "static" / "manifest.json"
        return JSONResponse(_json.loads(mf.read_text(encoding="utf-8")))

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        """Gate: no session → /login; onboarding incomplete → /onboarding; else dashboard."""
        from app.core.deps import _token_from_request
        from app.core.security import decode_access_token
        from app.models.tenant import Tenant, OnboardingStep
        from app.core.db import SessionLocal

        token = _token_from_request(request)
        payload = decode_access_token(token) if token else None
        if not payload:
            return RedirectResponse("/login", status_code=302)

        db = SessionLocal()
        try:
            tenant = db.query(Tenant).filter_by(id=payload.get("tid")).first()
        finally:
            db.close()
        if not tenant:
            return RedirectResponse("/login", status_code=302)
        # GoPOS connection is the only hard gate. Once connected, the dashboard
        # is the home — audit/recipes/briefing are completed from within the app.
        from app.models.tenant import step_rank
        if step_rank(tenant.onboarding_step) < step_rank(OnboardingStep.gopos_connected):
            return RedirectResponse("/onboarding", status_code=302)
        return _page(dashboard_html_path)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": settings.app_version,
            "gopos_configured": settings.gopos_configured,
            "llm_configured": settings.llm_configured,
        }

    return app


app = create_app()
