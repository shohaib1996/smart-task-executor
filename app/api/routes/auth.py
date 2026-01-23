from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from google.oauth2 import id_token
from google.auth.transport import requests
from google_auth_oauthlib.flow import Flow
import httpx

from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import create_access_token, get_current_user
from app.models.user import User
from app.schemas.auth import Token, GoogleAuthURL, UserResponse

router = APIRouter()
settings = get_settings()

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


def get_google_flow() -> Flow:
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
            }
        },
        scopes=GOOGLE_SCOPES,
    )
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    return flow


@router.get("/google", response_model=GoogleAuthURL)
async def google_auth():
    """Get Google OAuth authorization URL"""
    flow = get_google_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return GoogleAuthURL(auth_url=auth_url)


@router.get("/google/callback")
async def google_callback(
    code: str,
    session: AsyncSession = Depends(get_session),
):
    """Handle Google OAuth callback"""
    flow = get_google_flow()

    try:
        flow.fetch_token(code=code)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to fetch token: {str(e)}",
        )

    credentials = flow.credentials

    # Get user info from Google
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to get user info from Google",
            )
        user_info = response.json()

    # Find or create user
    result = await session.execute(
        select(User).where(User.email == user_info["email"])
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            email=user_info["email"],
            name=user_info.get("name", ""),
            picture=user_info.get("picture"),
            google_access_token=credentials.token,
            google_refresh_token=credentials.refresh_token,
            google_token_expires_at=credentials.expiry,
        )
        session.add(user)
    else:
        user.google_access_token = credentials.token
        if credentials.refresh_token:
            user.google_refresh_token = credentials.refresh_token
        user.google_token_expires_at = credentials.expiry

    await session.commit()
    await session.refresh(user)

    # Create JWT token
    access_token = create_access_token(data={"sub": user.id})

    # Redirect to frontend with token
    return RedirectResponse(
        url=f"{settings.FRONTEND_URL}/auth/callback?token={access_token}"
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_user),
):
    """Get current authenticated user info"""
    return UserResponse.model_validate(current_user)


@router.post("/logout")
async def logout(current_user: User = Depends(get_current_user)):
    """Logout user (client should discard token)"""
    return {"message": "Logged out successfully"}
