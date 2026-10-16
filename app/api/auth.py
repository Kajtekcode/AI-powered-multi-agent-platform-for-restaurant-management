"""
Auth API — registration, login, logout, current-user.

Registration creates BOTH a tenant (the restaurant account) and its first
owner user in one step. Sessions are JWT in an httpOnly cookie.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, get_current_tenant, COOKIE_NAME
from app.core.security import hash_password, verify_password, create_access_token
from app.models.tenant import Tenant, User, UserRole, OnboardingStep

logger = logging.getLogger(__name__)
router = APIRouter()


class RegisterBody(BaseModel):
    email: EmailStr
    password: str
    restaurant_name: str
    city: str | None = None

    @field_validator("password")
    @classmethod
    def _pw_len(cls, v):
        if len(v) < 8:
            raise ValueError("Hasło musi mieć co najmniej 8 znaków")
        return v


class LoginBody(BaseModel):
    email: EmailStr
    password: str


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True,
        secure=settings.cookie_secure, samesite="lax",
        max_age=settings.jwt_expire_hours * 3600, path="/",
    )


@router.post("/register", status_code=201)
def register(body: RegisterBody, response: Response, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="Konto z tym adresem e-mail już istnieje")

    tenant = Tenant(name=body.restaurant_name.strip(), city=(body.city or "").strip() or None,
                    onboarding_step=OnboardingStep.registered)
    db.add(tenant)
    db.flush()

    user = User(tenant_id=tenant.id, email=body.email.lower(),
                password_hash=hash_password(body.password), role=UserRole.owner)
    db.add(user)
    db.commit()

    token = create_access_token(user.id, tenant.id)
    _set_session_cookie(response, token)
    logger.info(f"New tenant registered: {tenant.name} ({tenant.id})")
    return {"tenant_id": tenant.id, "user_id": user.id, "onboarding_step": tenant.onboarding_step.value}


@router.post("/login")
def login(body: LoginBody, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Nieprawidłowy e-mail lub hasło")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Konto nieaktywne")

    user.last_login_at = datetime.utcnow()
    db.commit()

    token = create_access_token(user.id, user.tenant_id)
    _set_session_cookie(response, token)
    return {"tenant_id": user.tenant_id, "user_id": user.id}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "logged_out"}


@router.get("/me")
def me(user: User = Depends(get_current_user), tenant: Tenant = Depends(get_current_tenant)):
    return {
        "user": {"id": user.id, "email": user.email, "role": user.role.value},
        "tenant": {
            "id": tenant.id, "name": tenant.name, "city": tenant.city,
            "onboarding_step": tenant.onboarding_step.value,
            "subscription_status": tenant.subscription_status.value,
        },
    }
