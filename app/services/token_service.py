"""
Token Service - Handles Google OAuth token refresh and validation
"""
from datetime import datetime, timedelta
from typing import Optional, Tuple
import httpx
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import async_session
from app.models.user import User

settings = get_settings()


class TokenService:
    """Service for managing Google OAuth tokens"""

    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

    @staticmethod
    async def refresh_google_token(refresh_token: str) -> Optional[dict]:
        """
        Use refresh token to get a new access token from Google.

        Returns:
            dict with 'access_token' and 'expires_in' if successful, None otherwise
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    TokenService.GOOGLE_TOKEN_URL,
                    data={
                        "client_id": settings.GOOGLE_CLIENT_ID,
                        "client_secret": settings.GOOGLE_CLIENT_SECRET,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )

                if response.status_code == 200:
                    return response.json()
                else:
                    print(f"Token refresh failed: {response.status_code} - {response.text}")
                    return None

            except Exception as e:
                print(f"Token refresh error: {str(e)}")
                return None

    @staticmethod
    async def get_valid_tokens_for_user(
        user: User,
        session: AsyncSession
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Get valid access token for a user, refreshing if necessary.

        Args:
            user: User object
            session: Database session for updating tokens

        Returns:
            Tuple of (access_token, error_message)
            - (token, None) if successful
            - (None, error) if failed
        """
        if not user.google_refresh_token:
            return None, "User has no refresh token. They need to re-login."

        # Check if token is expired or about to expire (within 5 minutes)
        token_needs_refresh = False

        if not user.google_access_token:
            token_needs_refresh = True
        elif user.google_token_expires_at:
            # Add buffer of 5 minutes
            if user.google_token_expires_at <= datetime.utcnow() + timedelta(minutes=5):
                token_needs_refresh = True
        else:
            # No expiry info, try to refresh to be safe
            token_needs_refresh = True

        if token_needs_refresh:
            # Refresh the token
            token_data = await TokenService.refresh_google_token(user.google_refresh_token)

            if not token_data:
                return None, "Failed to refresh token. User may need to re-login."

            # Update user's tokens in database
            user.google_access_token = token_data["access_token"]
            user.google_token_expires_at = datetime.utcnow() + timedelta(
                seconds=token_data.get("expires_in", 3600)
            )

            # If Google returns a new refresh token, update it
            if "refresh_token" in token_data:
                user.google_refresh_token = token_data["refresh_token"]

            await session.commit()
            await session.refresh(user)

        return user.google_access_token, None

    @staticmethod
    async def get_valid_tokens_by_email(email: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Get valid access token for a user by their email.

        Args:
            email: User's email address

        Returns:
            Tuple of (access_token, refresh_token, error_message)
            - (access_token, refresh_token, None) if successful
            - (None, None, error) if failed
        """
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.email == email)
            )
            user = result.scalar_one_or_none()

            if not user:
                return None, None, f"User {email} is not registered in the app."

            if not user.google_refresh_token:
                return None, None, f"User {email} has no calendar access. They need to re-login."

            access_token, error = await TokenService.get_valid_tokens_for_user(user, session)

            if error:
                return None, None, error

            return access_token, user.google_refresh_token, None
