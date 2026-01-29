import json
from datetime import datetime, timedelta
from typing import TypedDict, List, Optional, Annotated
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from sqlmodel import select
from app.core.config import get_settings
from app.core.database import async_session
from app.models.workflow import (
    Workflow,
    Action,
    AuditLog,
    WorkflowStatus,
    ActionStatus,
    ActionType,
)
from app.models.user import User
from app.services.calendar import CalendarService


settings = get_settings()


# Agent State
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    workflow_id: str
    user_id: str
    user_email: Optional[str]  # Organizer's email
    user_timezone: Optional[str]  # User's timezone (e.g., "Asia/Dhaka")
    user_request: str

    # Extracted info
    meeting_title: Optional[str]
    meeting_duration: Optional[int]  # minutes
    attendees: List[str]
    preferred_timeframe: Optional[str]
    parsed_timezone: Optional[str]  # Timezone extracted from request (e.g., "BST" -> "Asia/Dhaka")

    # Calendar data
    user_events: List[dict]
    attendee_availability: dict
    suggested_slots: List[dict]
    inaccessible_calendars: List[str]  # Attendee emails whose calendars couldn't be accessed
    conflict_info: Optional[dict]  # Info about why preferred time isn't available
    preferred_datetime: Optional[str]  # Parsed preferred time (ISO format)

    # User selection
    selected_slot: Optional[dict]

    # Actions to execute
    proposed_actions: List[dict]

    # Status
    current_step: str
    needs_user_input: bool
    error: Optional[str]


# Initialize LLM
llm = ChatOpenAI(
    model="gpt-4-turbo-preview",
    api_key=settings.OPENAI_API_KEY,
    temperature=0,
)


async def _parse_timeframe(
    preferred_timeframe: str | None,
    user_timezone: str | None = None
) -> tuple[datetime, datetime, datetime | None, str | None]:
    """Parse natural language timeframe into start and end datetime.

    Args:
        preferred_timeframe: Natural language timeframe (e.g., "next tuesday at 7am BST")
        user_timezone: User's default timezone from profile (e.g., "Asia/Dhaka")

    Returns:
        tuple: (search_start, search_end, preferred_time or None, timezone_str or None)
        - search_start/end: Range to search for available slots (in UTC)
        - preferred_time: Exact preferred datetime if user specified one (in UTC)
        - timezone_str: IANA timezone string for the meeting (e.g., "Asia/Dhaka")
    """
    from zoneinfo import ZoneInfo

    now = datetime.utcnow()

    if not preferred_timeframe:
        # Default: next 2 weeks
        return now, now + timedelta(days=14), None, user_timezone

    # Use LLM to parse natural language timeframe WITH timezone detection
    today_str = now.strftime("%A, %B %d, %Y")

    # Calculate what "next friday" would be for reference
    days_until_friday = (4 - now.weekday()) % 7
    if days_until_friday == 0:
        days_until_friday = 7  # If today is Friday, "next Friday" is in 7 days
    next_friday = now + timedelta(days=days_until_friday)
    next_friday_str = next_friday.strftime("%Y-%m-%d")

    prompt = f"""Today is {today_str} (this is the CURRENT date, not a hypothetical).

Parse this timeframe: "{preferred_timeframe}"

For reference:
- Today's date: {now.strftime("%Y-%m-%d")}
- Next Friday: {next_friday_str}
- User's default timezone: {user_timezone or "UTC"}

IMPORTANT: Detect timezone from the request. Look for:
1. Timezone abbreviations:
   - BST (Bangladesh Standard Time) → "Asia/Dhaka"
   - EST/EDT/Eastern → "America/New_York"
   - PST/PDT/Pacific → "America/Los_Angeles"
   - CST/CDT/Central → "America/Chicago"
   - IST → "Asia/Kolkata"
   - GMT/UTC → "UTC"
2. Country/region names:
   - "Bangladesh time" → "Asia/Dhaka"
   - "Eastern time" / "New York time" → "America/New_York"
   - "Pacific time" / "LA time" / "California time" → "America/Los_Angeles"
   - "India time" → "Asia/Kolkata"
   - "UK time" / "London time" → "Europe/London"
3. If NO timezone is mentioned at all, use user's default: "{user_timezone or 'UTC'}"

Return the start date, end date, time, and timezone for the meeting.
Examples:
- "next friday at 7am BST" → {{"start_date": "{next_friday_str}", "end_date": "{next_friday_str}", "preferred_hour": 7, "preferred_minute": 0, "timezone": "Asia/Dhaka"}}
- "tomorrow at 3:30PM EST" → {{"start_date": "...", "end_date": "...", "preferred_hour": 15, "preferred_minute": 30, "timezone": "America/New_York"}}
- "next monday at 2PM" → {{"start_date": "...", "end_date": "...", "preferred_hour": 14, "preferred_minute": 0, "timezone": "{user_timezone or 'UTC'}"}}

Respond ONLY with JSON (no markdown, no explanation):
{{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "preferred_hour": null or number, "preferred_minute": null or number, "timezone": "IANA timezone string"}}
"""

    try:
        response = await llm.ainvoke(prompt)
        content = response.content.strip()

        # Clean up response
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        parsed = json.loads(content.strip())

        start_date = datetime.strptime(parsed["start_date"], "%Y-%m-%d")
        end_date = datetime.strptime(parsed["end_date"], "%Y-%m-%d")
        timezone_str = parsed.get("timezone") or user_timezone or "UTC"

        # Track preferred time if specified
        preferred_time = None
        if parsed.get("preferred_hour") is not None:
            preferred_hour = int(parsed["preferred_hour"])
            preferred_minute = int(parsed.get("preferred_minute") or 0)

            # Create time in the specified timezone, then convert to UTC
            local_time = start_date.replace(hour=preferred_hour, minute=preferred_minute)

            try:
                # Convert local time to UTC
                local_tz = ZoneInfo(timezone_str)
                local_dt = local_time.replace(tzinfo=local_tz)
                utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
                preferred_time = utc_dt.replace(tzinfo=None)  # Store as naive UTC

                # Also adjust search start
                start_date = preferred_time
            except Exception:
                # If timezone conversion fails, treat as UTC
                preferred_time = local_time
                start_date = start_date.replace(hour=preferred_hour, minute=preferred_minute)

        # End date should cover the full day
        end_date = end_date.replace(hour=23, minute=59)

        return start_date, end_date, preferred_time, timezone_str

    except Exception:
        # Fallback to default
        return now, now + timedelta(days=14), None, user_timezone


async def parse_request(state: AgentState) -> AgentState:
    """Parse user's meeting request to extract details"""
    prompt = f"""Analyze this meeting request and extract the following information:

Request: "{state["user_request"]}"

Extract:
1. Meeting title/topic - REQUIRED. If not explicitly stated, generate a descriptive title based on the request (e.g., "Meeting with [person name]", "Team Sync", etc.)
2. Duration in minutes (default 30 if not specified)
3. Attendees (email addresses only - extract any email addresses mentioned)
4. Preferred timeframe (e.g., "next week", "tomorrow afternoon", "next friday at 2PM")

IMPORTANT: meeting_title must NEVER be null. Always generate a reasonable title.

Respond in JSON format (no markdown):
{{
    "meeting_title": "string (required, never null)",
    "duration_minutes": number,
    "attendees": ["list of email addresses"],
    "preferred_timeframe": "string or null"
}}
"""

    response = await llm.ainvoke(prompt)

    try:
        # Extract JSON from response
        content = response.content
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        parsed = json.loads(content.strip())

        # Ensure meeting_title is never null
        meeting_title = parsed.get("meeting_title")
        if not meeting_title:
            meeting_title = "Scheduled Meeting"

        return {
            **state,
            "meeting_title": meeting_title,
            "meeting_duration": parsed.get("duration_minutes", 30),
            "attendees": parsed.get("attendees", []),
            "preferred_timeframe": parsed.get("preferred_timeframe"),
            "current_step": "check_calendars",
        }
    except Exception as e:
        return {
            **state,
            "error": f"Failed to parse request: {str(e)}",
            "current_step": "error",
        }


async def check_calendars(state: AgentState) -> AgentState:
    """Check user's calendar and attendee availability"""
    async with async_session() as session:
        # Get user
        result = await session.execute(select(User).where(User.id == state["user_id"]))
        user = result.scalar_one_or_none()

        if not user or not user.google_access_token:
            return {
                **state,
                "error": "No calendar access. Please reconnect Google Calendar.",
                "current_step": "error",
            }

        # Store user's email and timezone for later use
        user_email = user.email
        user_timezone = getattr(user, 'timezone', None) or "UTC"

        # Log progress
        await _log_progress(
            state["workflow_id"], "calendar_read", "Checking your calendar..."
        )

        try:
            calendar = CalendarService(
                access_token=user.google_access_token,
                refresh_token=user.google_refresh_token,
            )

            # Determine time range based on user's preferred timeframe (with timezone support)
            time_min, time_max, preferred_time, parsed_timezone = await _parse_timeframe(
                state.get("preferred_timeframe"),
                user_timezone
            )

            # Get user's events
            events = await calendar.get_events(
                time_min=time_min,
                time_max=time_max,
            )

            user_events = [
                {
                    "id": e.id,
                    "summary": e.summary,
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                }
                for e in events
            ]

            # Find free slots based on organizer's and attendees' calendars
            attendees = state.get("attendees", [])

            # Log attendee availability check if there are attendees
            if attendees:
                await _log_progress(
                    state["workflow_id"],
                    "checking_attendees",
                    f"Checking availability for {len(attendees)} attendee(s)..."
                )

            await _log_progress(
                state["workflow_id"],
                "finding_slots",
                "Finding available time slots..."
            )

            slots, inaccessible_calendars, conflict_info = await calendar.find_free_slots(
                duration_minutes=state["meeting_duration"],
                time_min=time_min,
                time_max=time_max,
                attendee_emails=attendees,
                preferred_time=preferred_time,
            )

            # Log inaccessible calendars to UI if any
            if inaccessible_calendars:
                await _log_progress(
                    state["workflow_id"],
                    "inaccessible_calendars",
                    f"Could not check availability for: {', '.join(inaccessible_calendars)}. They need to log into the app to grant calendar access."
                )

            # Convert slots from UTC to the user's timezone for display
            display_timezone = parsed_timezone or user_timezone or "UTC"
            try:
                from zoneinfo import ZoneInfo
                display_tz = ZoneInfo(display_timezone)
                utc_tz = ZoneInfo("UTC")
            except Exception:
                display_tz = None

            suggested_slots = []
            for i, slot in enumerate(slots[:5]):  # Top 5 slots
                if display_tz:
                    # Convert UTC to user's timezone for display
                    start_utc = slot.start.replace(tzinfo=utc_tz)
                    end_utc = slot.end.replace(tzinfo=utc_tz)
                    start_local = start_utc.astimezone(display_tz)
                    end_local = end_utc.astimezone(display_tz)
                    suggested_slots.append({
                        "id": f"slot_{i}",
                        "start": start_local.isoformat(),
                        "end": end_local.isoformat(),
                        "start_utc": slot.start.isoformat(),
                        "end_utc": slot.end.isoformat(),
                        "duration_minutes": state["meeting_duration"],
                        "timezone": display_timezone,
                    })
                else:
                    suggested_slots.append({
                        "id": f"slot_{i}",
                        "start": slot.start.isoformat(),
                        "end": slot.end.isoformat(),
                        "start_utc": slot.start.isoformat(),
                        "end_utc": slot.end.isoformat(),
                        "duration_minutes": state["meeting_duration"],
                        "timezone": "UTC",
                    })

            # Handle case when no slots are found
            if not suggested_slots:
                await _log_progress(
                    state["workflow_id"],
                    "no_slots_found",
                    "No available time slots found in the requested time range. Try a different date or time."
                )
                return {
                    **state,
                    "user_email": user_email,
                    "error": "No available time slots found. All slots in the requested time range may be busy.",
                    "current_step": "error",
                }

            # Convert conflict_info to dict for JSON serialization
            conflict_dict = None
            if conflict_info:
                conflict_dict = {
                    "requested_start": conflict_info.requested_start.isoformat(),
                    "requested_end": conflict_info.requested_end.isoformat(),
                    "conflicting_calendars": conflict_info.conflicting_calendars,
                    "reason": conflict_info.reason,
                }

            return {
                **state,
                "user_email": user_email,
                "user_timezone": user_timezone,
                "parsed_timezone": parsed_timezone,
                "user_events": user_events,
                "suggested_slots": suggested_slots,
                "inaccessible_calendars": inaccessible_calendars,
                "conflict_info": conflict_dict,
                "preferred_datetime": preferred_time.isoformat() if preferred_time else None,
                "current_step": "await_slot_selection",
                "needs_user_input": True,
            }

        except Exception as e:
            return {
                **state,
                "user_email": user_email,
                "user_timezone": user_timezone,
                "error": f"Calendar error: {str(e)}",
                "current_step": "error",
            }


async def await_slot_selection(state: AgentState) -> AgentState:
    """Wait for user to select a time slot"""
    # Update workflow to awaiting approval and send options to frontend
    async with async_session() as session:
        result = await session.execute(
            select(Workflow).where(Workflow.id == state["workflow_id"])
        )
        workflow = result.scalar_one_or_none()

        if workflow:
            workflow.status = WorkflowStatus.AWAITING_APPROVAL
            workflow.agent_state = json.dumps(state)
            await session.commit()

    # Send time slot options via WebSocket (include conflict info for user feedback)
    await _send_slot_options(
        state["workflow_id"],
        state["suggested_slots"],
        state.get("inaccessible_calendars", []),
        state.get("conflict_info"),
    )

    return state  # Agent pauses here until user selects


async def prepare_actions(state: AgentState) -> AgentState:
    """Prepare actions based on user's slot selection"""
    if not state.get("selected_slot"):
        return {
            **state,
            "error": "No slot selected",
            "current_step": "error",
        }

    slot = state["selected_slot"]

    # Build attendee list: include both the invited attendees AND the organizer
    # Google Calendar will send invite emails to all attendees automatically
    organizer_email = state.get("user_email")
    all_attendees = list(state["attendees"])  # Copy the list

    # Add organizer to attendees if not already included
    if organizer_email and organizer_email not in all_attendees:
        all_attendees.append(organizer_email)

    # Get timezone for the event (use parsed timezone from request, or user's default)
    event_timezone = state.get("parsed_timezone") or state.get("user_timezone") or "UTC"

    # Only action needed: Create Calendar Event
    # Google Calendar automatically sends email invites to all attendees
    actions = [
        {
            "action_type": ActionType.CALENDAR_CREATE.value,
            "title": "Create Calendar Event",
            "description": f"Create meeting: {state['meeting_title']} (Google Calendar will send invites to all {len(all_attendees)} attendees)",
            "payload": {
                "summary": state["meeting_title"],
                "start": slot["start"],
                "end": slot["end"],
                "attendees": all_attendees,
                "description": "Meeting scheduled via Smart Task Executor",
                "add_meet_link": True,
                "timezone": event_timezone,  # Use the parsed/user timezone
            },
            "requires_approval": True,
            "api_name": "Google Calendar",
            "estimated_cost": 0.0,
            "order": 0,
        },
    ]

    return {
        **state,
        "proposed_actions": actions,
        "current_step": "await_action_approval",
        "needs_user_input": True,
    }


async def await_action_approval(state: AgentState) -> AgentState:
    """Save actions to database and wait for user approval"""
    async with async_session() as session:
        result = await session.execute(
            select(Workflow).where(Workflow.id == state["workflow_id"])
        )
        workflow = result.scalar_one_or_none()

        if not workflow:
            return {**state, "error": "Workflow not found", "current_step": "error"}

        # Create action records
        for action_data in state["proposed_actions"]:
            action = Action(
                workflow_id=state["workflow_id"],
                action_type=ActionType(action_data["action_type"]),
                title=action_data["title"],
                description=action_data["description"],
                payload=json.dumps(action_data["payload"]),
                requires_approval=action_data["requires_approval"],
                api_name=action_data.get("api_name"),
                estimated_cost=action_data.get("estimated_cost", 0.0),
                order=action_data.get("order", 0),
                status=ActionStatus.PENDING,
            )
            session.add(action)

        workflow.status = WorkflowStatus.AWAITING_APPROVAL
        workflow.agent_state = json.dumps(state)

        audit_log = AuditLog(
            workflow_id=state["workflow_id"],
            event_type="actions_proposed",
            message=f"Proposed {len(state['proposed_actions'])} actions for approval",
        )
        session.add(audit_log)

        await session.commit()

    # Send approval request via WebSocket
    await _send_approval_request(state["workflow_id"])

    return state


def route_after_parse(state: AgentState) -> str:
    """Route after parsing request"""
    if state.get("error"):
        return "error"
    return "check_calendars"


def route_after_calendars(state: AgentState) -> str:
    """Route after checking calendars"""
    if state.get("error"):
        return "error"
    return "await_slot_selection"


def route_after_slot_selection(state: AgentState) -> str:
    """Route after slot selection"""
    if state.get("selected_slot"):
        return "prepare_actions"
    return END  # Wait for user input


def route_after_prepare(state: AgentState) -> str:
    """Route after preparing actions"""
    if state.get("error"):
        return "error"
    return "await_action_approval"


def handle_error(state: AgentState) -> AgentState:
    """Handle errors in the workflow"""
    return state


# Build the graph
def create_meeting_coordinator_graph():
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("parse_request", parse_request)
    graph.add_node("check_calendars", check_calendars)
    graph.add_node("await_slot_selection", await_slot_selection)
    graph.add_node("prepare_actions", prepare_actions)
    graph.add_node("await_action_approval", await_action_approval)
    graph.add_node("error", handle_error)

    # Add edges
    graph.set_entry_point("parse_request")

    graph.add_conditional_edges(
        "parse_request",
        route_after_parse,
        {
            "check_calendars": "check_calendars",
            "error": "error",
        },
    )

    graph.add_conditional_edges(
        "check_calendars",
        route_after_calendars,
        {
            "await_slot_selection": "await_slot_selection",
            "error": "error",
        },
    )

    graph.add_conditional_edges(
        "await_slot_selection",
        route_after_slot_selection,
        {
            "prepare_actions": "prepare_actions",
            END: END,
        },
    )

    graph.add_conditional_edges(
        "prepare_actions",
        route_after_prepare,
        {
            "await_action_approval": "await_action_approval",
            "error": "error",
        },
    )

    graph.add_edge("await_action_approval", END)
    graph.add_edge("error", END)

    return graph.compile()


# Agent entry points
async def run_meeting_coordinator(workflow_id: str, user_id: str):
    """Main entry point to run the meeting coordinator agent"""
    async with async_session() as session:
        result = await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )
        workflow = result.scalar_one_or_none()

        if not workflow:
            return

        initial_state: AgentState = {
            "messages": [],
            "workflow_id": workflow_id,
            "user_id": user_id,
            "user_email": None,  # Will be populated in check_calendars
            "user_timezone": None,  # Will be populated in check_calendars
            "user_request": workflow.user_request,
            "meeting_title": None,
            "meeting_duration": None,
            "attendees": [],
            "preferred_timeframe": None,
            "parsed_timezone": None,  # Will be populated from request parsing
            "user_events": [],
            "attendee_availability": {},
            "suggested_slots": [],
            "inaccessible_calendars": [],
            "conflict_info": None,
            "preferred_datetime": None,
            "selected_slot": None,
            "proposed_actions": [],
            "current_step": "parse_request",
            "needs_user_input": False,
            "error": None,
        }

        graph = create_meeting_coordinator_graph()

        # Run until user input is needed
        final_state = await graph.ainvoke(initial_state)

        # Save state
        workflow.agent_state = json.dumps(final_state)
        await session.commit()


async def continue_after_selection(workflow_id: str, option_id: str, user_id: str):
    """Continue agent after user selects a time slot"""
    async with async_session() as session:
        result = await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )
        workflow = result.scalar_one_or_none()

        if not workflow or not workflow.agent_state:
            return

        state = json.loads(workflow.agent_state)

        # Find selected slot
        selected_slot = None
        for slot in state.get("suggested_slots", []):
            if slot["id"] == option_id:
                selected_slot = slot
                break

        if not selected_slot:
            return

        state["selected_slot"] = selected_slot
        state["current_step"] = "prepare_actions"
        state["needs_user_input"] = False

        # Directly call prepare_actions and await_action_approval
        # instead of re-running the entire graph (which would re-check calendars)
        state = await prepare_actions(state)

        if not state.get("error"):
            state = await await_action_approval(state)

        workflow.agent_state = json.dumps(state)
        await session.commit()


# Helper functions
async def _log_progress(workflow_id: str, event_type: str, message: str):
    """Log progress to database and WebSocket"""
    async with async_session() as session:
        audit_log = AuditLog(
            workflow_id=workflow_id,
            event_type=event_type,
            message=message,
        )
        session.add(audit_log)
        await session.commit()

    # Send via WebSocket
    from app.api.websockets.connection_manager import manager

    await manager.broadcast_progress(workflow_id, "progress", {"message": message})


async def _send_slot_options(
    workflow_id: str,
    slots: List[dict],
    inaccessible_calendars: List[str] = None,
    conflict_info: dict = None,
):
    """Send time slot options to frontend via WebSocket"""
    from app.api.websockets.connection_manager import manager

    # Build message based on whether there was a conflict
    if conflict_info:
        message = f"Your preferred time is not available: {conflict_info['reason']}. Here are alternative times that work for everyone:"
    else:
        message = "Please select a time slot"

    await manager.broadcast_progress(
        workflow_id,
        "slot_selection",
        {
            "slots": slots,
            "message": message,
            "inaccessible_calendars": inaccessible_calendars or [],
            "conflict_info": conflict_info,
        },
    )


async def _send_approval_request(workflow_id: str):
    """Notify frontend that actions are ready for approval"""
    from app.api.websockets.connection_manager import manager

    await manager.broadcast_progress(
        workflow_id,
        "approval_required",
        {"message": "Actions ready for approval"},
    )
