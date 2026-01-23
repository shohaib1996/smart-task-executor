from pydantic import BaseModel, EmailStr
from typing import Optional


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[str] = None


class GoogleAuthURL(BaseModel):
    auth_url: str


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    name: str
    picture: Optional[str] = None

    class Config:
        from_attributes = True
