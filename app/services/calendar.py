from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel

from app.services.token_service import TokenService


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


class ConflictInfo(BaseModel):
    """Information about a scheduling conflict"""
    requested_start: datetime
    requested_end: datetime
    conflicting_calendars: List[str]  # Email addresses of people who have conflicts
    reason: str  # Human-readable explanation


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
                    attendees=[a.get("email", "") for a in event.get("attendees", [])],
                    location=event.get("location"),
                    description=event.get("description"),
                    meet_link=event.get("hangoutLink"),
                )
            )

        return events

    async def _get_attendee_busy_periods(
        self,
        attendee_email: str,
        time_min: datetime,
        time_max: datetime,
    ) -> Tuple[List[Tuple[datetime, datetime]], Optional[str]]:
        """
        Get busy periods for an attendee using their own stored tokens.

        Args:
            attendee_email: Attendee's email address
            time_min: Start of search range
            time_max: End of search range

        Returns:
            Tuple of (list of busy periods, error message if any)
        """
        # Try to get valid tokens for this attendee from the database
        access_token, refresh_token, error = await TokenService.get_valid_tokens_by_email(attendee_email)

        if error:
            return [], error

        try:
            # Create a calendar service using the attendee's tokens
            attendee_credentials = Credentials(
                token=access_token,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
            )
            attendee_service = build("calendar", "v3", credentials=attendee_credentials)

            # Query their calendar's freebusy
            body = {
                "timeMin": time_min.isoformat() + "Z",
                "timeMax": time_max.isoformat() + "Z",
                "items": [{"id": "primary"}],  # Check their primary calendar
            }

            freebusy_result = attendee_service.freebusy().query(body=body).execute()
            calendar_busy = freebusy_result.get("calendars", {}).get("primary", {})

            if "errors" in calendar_busy:
                return [], f"Could not access calendar for {attendee_email}"

            busy_periods = []
            for busy in calendar_busy.get("busy", []):
                busy_start = datetime.fromisoformat(busy["start"].replace("Z", "+00:00"))
                busy_end = datetime.fromisoformat(busy["end"].replace("Z", "+00:00"))
                busy_periods.append((busy_start, busy_end))

            return busy_periods, None

        except Exception as e:
            return [], f"Error checking calendar for {attendee_email}: {str(e)}"

    async def find_free_slots(
        self,
        duration_minutes: int,
        time_min: datetime = None,
        time_max: datetime = None,
        calendar_ids: List[str] = None,
        attendee_emails: List[str] = None,
        working_hours: tuple = (0, 24),
        allow_weekends: bool = True,
        preferred_time: datetime = None,
    ) -> tuple[List[TimeSlot], List[str], Optional[ConflictInfo]]:
        """Find available time slots based on organizer's and attendees' calendars.

        Args:
            duration_minutes: Length of the meeting
            time_min: Start of search range
            time_max: End of search range
            calendar_ids: Organizer's calendar IDs
            attendee_emails: List of attendee email addresses
            working_hours: Tuple of (start_hour, end_hour)
            allow_weekends: Whether to include weekend slots
            preferred_time: User's preferred meeting time (if specified)

        Returns:
            tuple: (list of free slots, list of inaccessible calendars, conflict info if preferred time has conflicts)
        """
        if time_min is None:
            time_min = datetime.utcnow()
        if time_max is None:
            time_max = time_min + timedelta(days=7)
        if calendar_ids is None:
            calendar_ids = ["primary"]
        if attendee_emails is None:
            attendee_emails = []

        # Collect busy periods and track inaccessible calendars
        busy_periods = []
        busy_periods_by_calendar: Dict[str, List[Tuple[datetime, datetime]]] = {}
        inaccessible_calendars = []

        # First, get organizer's busy periods using their own service
        body = {
            "timeMin": time_min.isoformat() + "Z",
            "timeMax": time_max.isoformat() + "Z",
            "items": [{"id": cal_id} for cal_id in calendar_ids],
        }

        try:
            freebusy_result = self.service.freebusy().query(body=body).execute()
        except Exception:
            freebusy_result = {"calendars": {}}

        # Process organizer's calendars
        for calendar_id in calendar_ids:
            calendar_busy = freebusy_result.get("calendars", {}).get(calendar_id, {})
            if "errors" in calendar_busy:
                continue

            busy_periods_by_calendar[calendar_id] = []
            for busy in calendar_busy.get("busy", []):
                busy_start = datetime.fromisoformat(busy["start"].replace("Z", "+00:00"))
                busy_end = datetime.fromisoformat(busy["end"].replace("Z", "+00:00"))
                busy_periods.append((busy_start, busy_end))
                busy_periods_by_calendar[calendar_id].append((busy_start, busy_end))

        # Now check each attendee using THEIR OWN tokens
        for attendee_email in attendee_emails:
            attendee_busy, error = await self._get_attendee_busy_periods(
                attendee_email, time_min, time_max
            )

            if error:
                # Attendee not registered or token issue
                inaccessible_calendars.append(attendee_email)
            else:
                # Successfully got their availability
                busy_periods_by_calendar[attendee_email] = attendee_busy
                busy_periods.extend(attendee_busy)

        # Sort busy periods
        busy_periods.sort(key=lambda x: x[0])

        # Normalize busy periods to naive datetimes (remove timezone info for comparison)
        normalized_busy_periods = []
        for busy_start, busy_end in busy_periods:
            # Convert to naive datetime if offset-aware
            if busy_start.tzinfo is not None:
                busy_start = busy_start.replace(tzinfo=None)
            if busy_end.tzinfo is not None:
                busy_end = busy_end.replace(tzinfo=None)
            normalized_busy_periods.append((busy_start, busy_end))

        # Find free slots
        free_slots = []
        current = time_min
        max_iterations = 1000  # Safety limit to prevent infinite loops
        iterations = 0

        while current < time_max and iterations < max_iterations:
            iterations += 1

            # Skip non-working hours
            if current.hour < working_hours[0]:
                current = current.replace(hour=working_hours[0], minute=0)
            elif current.hour >= working_hours[1]:
                current = (current + timedelta(days=1)).replace(
                    hour=working_hours[0], minute=0
                )
                continue

            # Skip weekends (unless allowed)
            if not allow_weekends and current.weekday() >= 5:
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
            for busy_start, busy_end in normalized_busy_periods:
                if not (slot_end <= busy_start or current >= busy_end):
                    is_free = False
                    # Move to end of busy period
                    current = busy_end
                    break

            if is_free:
                free_slots.append(TimeSlot(start=current, end=slot_end, available=True))
                current = slot_end
            # Note: if not free, current was already moved to busy_end

            # Stop if we have enough slots
            if len(free_slots) >= 10:
                break

        # Safety check - if we hit max iterations, just return what we have

        # Check if preferred time has conflicts
        conflict_info = None
        if preferred_time:
            preferred_end = preferred_time + timedelta(minutes=duration_minutes)
            conflicting_calendars = []

            for calendar_id, periods in busy_periods_by_calendar.items():
                for busy_start, busy_end in periods:
                    # Normalize timezone for comparison
                    if busy_start.tzinfo is not None:
                        busy_start = busy_start.replace(tzinfo=None)
                    if busy_end.tzinfo is not None:
                        busy_end = busy_end.replace(tzinfo=None)
                    # Check if preferred time overlaps with this busy period
                    overlaps = not (preferred_end <= busy_start or preferred_time >= busy_end)
                    if overlaps:
                        conflicting_calendars.append(calendar_id)
                        break  # Only count each calendar once

            if conflicting_calendars:
                # Build human-readable reason
                # Replace "primary" with "You" for the user's own calendar
                display_names = ["You" if c == "primary" else c for c in conflicting_calendars]

                if len(display_names) == 1:
                    if display_names[0] == "You":
                        reason = "You already have a meeting scheduled at this time"
                    else:
                        reason = f"{display_names[0]} has a conflict at this time"
                else:
                    names = ", ".join(display_names[:-1]) + f" and {display_names[-1]}"
                    reason = f"{names} have conflicts at this time"

                conflict_info = ConflictInfo(
                    requested_start=preferred_time,
                    requested_end=preferred_end,
                    conflicting_calendars=conflicting_calendars,
                    reason=reason,
                )

        return free_slots[:10], inaccessible_calendars, conflict_info

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
        event = (
            self.service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )

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

    async def delete_event(self, event_id: str, calendar_id: str = "primary") -> bool:
        """Delete a calendar event"""
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True
