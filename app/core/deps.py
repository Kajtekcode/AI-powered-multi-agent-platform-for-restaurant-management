"""
FastAPI dependencies for auth & multi-tenancy.

- get_current_user / get_current_tenant: resolve the logged-in user from the
  JWT cookie, set the tenant context, and return ORM objects.
- require_onboarding(min_step): hard gate — blocks endpoints until the tenant
  has progressed far enough through onboarding (returns 403 + CTA payload).

The tenant_id ALWAYS comes from the verified JWT, never from request input.
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import decode_access_token
from app.core.tenant_context import set_tenant
from app.models.tenant import Tenant, User, OnboardingStep, step_rank

logger = logging.getLogger(__name__)

COOKIE_NAME = "rgos_session"


def _token_from_request(request: Request) -> Optional[str]:
    # Prefer httpOnly cookie; allow Authorization: Bearer for API clients
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        return tok
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nie zalogowano")
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesja wygasła lub nieprawidłowa")
    user = db.query(User).filter_by(id=payload.get("sub"), is_active=True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Konto nieaktywne")
    # Bind tenant context for the repository layer
    set_tenant(user.tenant_id)
    return user


def get_current_tenant(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Tenant:
    tenant = db.query(Tenant).filter_by(id=user.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Brak konta restauracji")
    return tenant


def require_onboarding(min_step: OnboardingStep):
    """
    Dependency factory — gate an endpoint behind an onboarding step.
    Usage: dependencies=[Depends(require_onboarding(OnboardingStep.research_done))]
    """
    min_rank = step_rank(min_step)

    def _checker(tenant: Tenant = Depends(get_current_tenant)):
        if step_rank(tenant.onboarding_step) < min_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "reason": "onboarding_incomplete",
                    "message": "Ta funkcja odblokuje się po dokończeniu konfiguracji.",
                    "current_step": tenant.onboarding_step.value,
                    "required_step": min_step.value,
                    "cta_url": "/onboarding",
                },
            )
        return tenant

    return _checker
