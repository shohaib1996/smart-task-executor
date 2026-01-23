from datetime import datetime, timedelta
from typing import List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel


class CalendarEvent(BaseModel):
    id: str
    summary: str
    start: datetime
    end: datetime
    attendees: List[str] = []
    location: Optional[str] = None
    description: Optional[str] = None
    meet_link: Optional[str] = None


class TimeSlot(BaseModel):
    start: datetime
    end: datetime
    available: bool = True


class CalendarService:
    """Service for interacting with Google Calendar API"""

    def __init__(self, access_token: str, refresh_token: str = None):
        self.credentials = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
        )
        self.service = build("calendar", "v3", credentials=self.credentials)

    async def get_events(
        self,
        calendar_id: str = "primary",
        time_min: datetime = None,
        time_max: datetime = None,
        max_results: int = 50,
    ) -> List[CalendarEvent]:
        """Get events from a calendar"""
        if time_min is None:
            time_min = datetime.utcnow()
        if time_max is None:
            time_max = time_min + timedelta(days=7)

        events_result = (
            self.service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat() + "Z",
                timeMax=time_max.isoformat() + "Z",
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = []
        for event in events_result.get("items", []):
            start = event["start"].get("dateTime", event["start"].get("date"))
            end = event["end"].get("dateTime", event["end"].get("date"))

            events.append(
                CalendarEvent(
                    id=event["id"],
                    summary=event.get("summary", "No title"),
                    start=datetime.fromisoformat(start.replace("Z", "+00:00")),
                    end=datetime.fromisoformat(end.replace("Z", "+00:00")),
                    attendees=[
                        a.get("email", "") for a in event.get("attendees", [])
                    ],
                    location=event.get("location"),
                    description=event.get("description"),
                    meet_link=event.get("hangoutLink"),
                )
            )

        return events

    async def find_free_slots(
        self,
        duration_minutes: int,
        time_min: datetime = None,
        time_max: datetime = None,
        calendar_ids: List[str] = None,
        working_hours: tuple = (9, 17),
    ) -> List[TimeSlot]:
        """Find available time slots across calendars"""
        if time_min is None:
            time_min = datetime.utcnow()
        if time_max is None:
            time_max = time_min + timedelta(days=7)
        if calendar_ids is None:
            calendar_ids = ["primary"]

        # Get busy times from freebusy API
        body = {
            "timeMin": time_min.isoformat() + "Z",
            "timeMax": time_max.isoformat() + "Z",
            "items": [{"id": cal_id} for cal_id in calendar_ids],
        }

        freebusy_result = self.service.freebusy().query(body=body).execute()

        # Collect all busy periods
        busy_periods = []
        for calendar_id in calendar_ids:
            calendar_busy = freebusy_result["calendars"].get(calendar_id, {})
            for busy in calendar_busy.get("busy", []):
                busy_periods.append(
                    (
                        datetime.fromisoformat(busy["start"].replace("Z", "+00:00")),
                        datetime.fromisoformat(busy["end"].replace("Z", "+00:00")),
                    )
                )

        # Sort busy periods
        busy_periods.sort(key=lambda x: x[0])

        # Find free slots
        free_slots = []
        current = time_min

        while current < time_max:
            # Skip non-working hours
            if current.hour < working_hours[0]:
                current = current.replace(hour=working_hours[0], minute=0)
            elif current.hour >= working_hours[1]:
                current = (current + timedelta(days=1)).replace(
                    hour=working_hours[0], minute=0
                )
                continue

            # Skip weekends
            if current.weekday() >= 5:
                current = current + timedelta(days=1)
                continue

            slot_end = current + timedelta(minutes=duration_minutes)

            # Check if slot is within working hours
            if slot_end.hour > working_hours[1] or (
                slot_end.hour == working_hours[1] and slot_end.minute > 0
            ):
                current = (current + timedelta(days=1)).replace(
                    hour=working_hours[0], minute=0
                )
                continue

            # Check if slot conflicts with busy periods
            is_free = True
            for busy_start, busy_end in busy_periods:
                if not (slot_end <= busy_start or current >= busy_end):
                    is_free = False
                    # Move to end of busy period
                    current = busy_end
                    break

            if is_free:
                free_slots.append(
                    TimeSlot(start=current, end=slot_end, available=True)
                )
                current = slot_end
            else:
                # Already moved to end of busy period
                pass

        return free_slots[:10]  # Return top 10 slots

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        attendees: List[str] = None,
        description: str = None,
        location: str = None,
        add_meet_link: bool = True,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        """Create a new calendar event"""
        event_body = {
            "summary": summary,
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        }

        if attendees:
            event_body["attendees"] = [{"email": email} for email in attendees]

        if description:
            event_body["description"] = description

        if location:
            event_body["location"] = location

        if add_meet_link:
            event_body["conferenceData"] = {
                "createRequest": {"requestId": f"meet-{datetime.utcnow().timestamp()}"}
            }

        event = (
            self.service.events()
            .insert(
                calendarId=calendar_id,
                body=event_body,
                conferenceDataVersion=1 if add_meet_link else 0,
                sendUpdates="all" if attendees else "none",
            )
            .execute()
        )

        return CalendarEvent(
            id=event["id"],
            summary=event.get("summary", ""),
            start=start,
            end=end,
            attendees=attendees or [],
            location=location,
            description=description,
            meet_link=event.get("hangoutLink"),
        )

    async def update_event(
        self,
        event_id: str,
        summary: str = None,
        start: datetime = None,
        end: datetime = None,
        attendees: List[str] = None,
        description: str = None,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        """Update an existing calendar event"""
        # Get current event
        event = self.service.events().get(
            calendarId=calendar_id, eventId=event_id
        ).execute()

        # Update fields
        if summary:
            event["summary"] = summary
        if start:
            event["start"] = {"dateTime": start.isoformat(), "timeZone": "UTC"}
        if end:
            event["end"] = {"dateTime": end.isoformat(), "timeZone": "UTC"}
        if attendees is not None:
            event["attendees"] = [{"email": email} for email in attendees]
        if description:
            event["description"] = description

        updated = (
            self.service.events()
            .update(calendarId=calendar_id, eventId=event_id, body=event)
            .execute()
        )

        return CalendarEvent(
            id=updated["id"],
            summary=updated.get("summary", ""),
            start=datetime.fromisoformat(
                updated["start"]["dateTime"].replace("Z", "+00:00")
            ),
            end=datetime.fromisoformat(
                updated["end"]["dateTime"].replace("Z", "+00:00")
            ),
            attendees=[a.get("email", "") for a in updated.get("attendees", [])],
            meet_link=updated.get("hangoutLink"),
        )

    async def delete_event(
        self, event_id: str, calendar_id: str = "primary"
    ) -> bool:
        """Delete a calendar event"""
        self.service.events().delete(
            calendarId=calendar_id, eventId=event_id
        ).execute()
        return True
