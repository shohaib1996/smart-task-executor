from typing import List, Optional
import httpx
from pydantic import BaseModel, EmailStr

from app.core.config import get_settings


class EmailMessage(BaseModel):
    to: List[EmailStr]
    subject: str
    body: str
    html_body: Optional[str] = None
    cc: List[EmailStr] = []
    bcc: List[EmailStr] = []


class EmailService:
    """Service for sending emails via SendGrid or similar"""

    def __init__(self, api_key: str = None):
        settings = get_settings()
        self.api_key = api_key or getattr(settings, "SENDGRID_API_KEY", None)
        self.from_email = getattr(settings, "FROM_EMAIL", "noreply@smarttask.app")
        self.base_url = "https://api.sendgrid.com/v3"

    async def send_email(self, message: EmailMessage) -> dict:
        """Send an email via SendGrid"""
        if not self.api_key:
            # Mock response for development
            return {
                "success": True,
                "message_id": "mock-" + str(hash(message.subject)),
                "mock": True,
            }

        payload = {
            "personalizations": [
                {
                    "to": [{"email": email} for email in message.to],
                    "cc": [{"email": email} for email in message.cc] if message.cc else None,
                    "bcc": [{"email": email} for email in message.bcc] if message.bcc else None,
                }
            ],
            "from": {"email": self.from_email},
            "subject": message.subject,
            "content": [
                {"type": "text/plain", "value": message.body},
            ],
        }

        if message.html_body:
            payload["content"].append(
                {"type": "text/html", "value": message.html_body}
            )

        # Remove None values from personalizations
        payload["personalizations"][0] = {
            k: v for k, v in payload["personalizations"][0].items() if v
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/mail/send",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code in [200, 202]:
                return {
                    "success": True,
                    "message_id": response.headers.get("X-Message-Id"),
                }
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code,
                }

    async def send_meeting_notification(
        self,
        to_email: str,
        meeting_title: str,
        meeting_datetime: str,
        meeting_duration: str,
        meeting_link: str = None,
        organizer_name: str = "Smart Task",
    ) -> dict:
        """Send a meeting notification email"""
        body = f"""Hi,

You have been invited to a meeting:

{meeting_title}

When: {meeting_datetime}
Duration: {meeting_duration}
"""
        if meeting_link:
            body += f"\nJoin: {meeting_link}\n"

        body += f"""
Best regards,
{organizer_name}

---
This meeting was scheduled via Smart Task Executor.
"""

        html_body = f"""
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <h2 style="color: #2563eb;">Meeting Invitation</h2>
    <h3>{meeting_title}</h3>
    <p><strong>When:</strong> {meeting_datetime}</p>
    <p><strong>Duration:</strong> {meeting_duration}</p>
    {"<p><a href='" + meeting_link + "' style='background: #2563eb; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;'>Join Meeting</a></p>" if meeting_link else ""}
    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
    <p style="color: #666; font-size: 12px;">This meeting was scheduled via Smart Task Executor.</p>
</body>
</html>
"""

        message = EmailMessage(
            to=[to_email],
            subject=f"Meeting Invitation: {meeting_title}",
            body=body,
            html_body=html_body,
        )

        return await self.send_email(message)
