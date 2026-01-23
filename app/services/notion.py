from typing import Optional, List, Dict, Any
import httpx
from pydantic import BaseModel

from app.core.config import get_settings


class NotionPage(BaseModel):
    id: str
    url: str
    title: str


class NotionService:
    """Service for interacting with Notion API"""

    def __init__(self, api_key: str = None):
        settings = get_settings()
        self.api_key = api_key or getattr(settings, "NOTION_API_KEY", None)
        self.base_url = "https://api.notion.com/v1"
        self.notion_version = "2022-06-28"

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Notion-Version": self.notion_version,
        }

    async def create_page(
        self,
        parent_id: str,
        title: str,
        content: List[Dict[str, Any]] = None,
        properties: Dict[str, Any] = None,
        is_database: bool = False,
    ) -> NotionPage:
        """Create a new page in Notion"""
        if not self.api_key:
            # Mock response for development
            return NotionPage(
                id="mock-page-id",
                url="https://notion.so/mock-page",
                title=title,
            )

        # Build parent reference
        if is_database:
            parent = {"database_id": parent_id}
        else:
            parent = {"page_id": parent_id}

        # Build properties
        if properties is None:
            properties = {}

        # Add title property
        if is_database:
            properties["Name"] = {"title": [{"text": {"content": title}}]}
        else:
            properties["title"] = {"title": [{"text": {"content": title}}]}

        payload = {
            "parent": parent,
            "properties": properties,
        }

        # Add content blocks if provided
        if content:
            payload["children"] = content

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/pages",
                json=payload,
                headers=self._get_headers(),
            )

            if response.status_code == 200:
                data = response.json()
                return NotionPage(
                    id=data["id"],
                    url=data["url"],
                    title=title,
                )
            else:
                raise Exception(f"Notion API error: {response.text}")

    async def create_meeting_notes(
        self,
        parent_id: str,
        meeting_title: str,
        meeting_date: str,
        attendees: List[str],
        agenda: str = None,
    ) -> NotionPage:
        """Create a meeting notes page with template"""
        content = [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Meeting Details"}}]
                },
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"📅 Date: {meeting_date}"}}
                    ]
                },
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": f"👥 Attendees: {', '.join(attendees)}"},
                        }
                    ]
                },
            },
            {
                "object": "block",
                "type": "divider",
                "divider": {},
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Agenda"}}]
                },
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": agenda or "Add agenda items here..."},
                        }
                    ]
                },
            },
            {
                "object": "block",
                "type": "divider",
                "divider": {},
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Notes"}}]
                },
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": ""}}]
                },
            },
            {
                "object": "block",
                "type": "divider",
                "divider": {},
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Action Items"}}]
                },
            },
            {
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": "Add action items..."}}],
                    "checked": False,
                },
            },
        ]

        return await self.create_page(
            parent_id=parent_id,
            title=f"{meeting_title} - {meeting_date}",
            content=content,
        )

    async def search_pages(
        self,
        query: str,
        filter_type: str = None,
    ) -> List[NotionPage]:
        """Search for pages in Notion"""
        if not self.api_key:
            return []

        payload = {"query": query}

        if filter_type:
            payload["filter"] = {"property": "object", "value": filter_type}

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/search",
                json=payload,
                headers=self._get_headers(),
            )

            if response.status_code == 200:
                data = response.json()
                pages = []
                for result in data.get("results", []):
                    title = ""
                    if result.get("properties", {}).get("title"):
                        title_prop = result["properties"]["title"]
                        if title_prop.get("title"):
                            title = title_prop["title"][0]["text"]["content"]
                    elif result.get("properties", {}).get("Name"):
                        name_prop = result["properties"]["Name"]
                        if name_prop.get("title"):
                            title = name_prop["title"][0]["text"]["content"]

                    pages.append(
                        NotionPage(
                            id=result["id"],
                            url=result["url"],
                            title=title,
                        )
                    )
                return pages
            else:
                return []
