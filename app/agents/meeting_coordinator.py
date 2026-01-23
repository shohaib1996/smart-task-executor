import json
from datetime import datetime, timedelta
from typing import TypedDict, List, Optional, Annotated
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from sqlmodel import select

from app.core.config import get_settings
from app.core.database import async_session
from app.models.workflow import Workflow, Action, AuditLog, WorkflowStatus, ActionStatus, ActionType
from app.models.user import User
from app.services.calendar import CalendarService


settings = get_settings()


# Agent State
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    workflow_id: str
    user_id: str
    user_request: str

    # Extracted info
    meeting_title: Optional[str]
    meeting_duration: Optional[int]  # minutes
    attendees: List[str]
    preferred_timeframe: Optional[str]

    # Calendar data
    user_events: List[dict]
    attendee_availability: dict
    suggested_slots: List[dict]

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


async def parse_request(state: AgentState) -> AgentState:
    """Parse user's meeting request to extract details"""
    prompt = f"""Analyze this meeting request and extract the following information:

Request: "{state['user_request']}"

Extract:
1. Meeting title/topic
2. Duration in minutes (default 30 if not specified)
3. Attendees (email addresses or names)
4. Preferred timeframe (e.g., "next week", "tomorrow afternoon")

Respond in JSON format:
{{
    "meeting_title": "string",
    "duration_minutes": number,
    "attendees": ["list of attendees"],
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

        return {
            **state,
            "meeting_title": parsed.get("meeting_title"),
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
        result = await session.execute(
            select(User).where(User.id == state["user_id"])
        )
        user = result.scalar_one_or_none()

        if not user or not user.google_access_token:
            return {
                **state,
                "error": "No calendar access. Please reconnect Google Calendar.",
                "current_step": "error",
            }

        # Log progress
        await _log_progress(state["workflow_id"], "calendar_read", "Checking your calendar...")

        try:
            calendar = CalendarService(
                access_token=user.google_access_token,
                refresh_token=user.google_refresh_token,
            )

            # Determine time range
            time_min = datetime.utcnow()
            time_max = time_min + timedelta(days=14)  # Next 2 weeks

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

            # Find free slots
            await _log_progress(state["workflow_id"], "finding_slots", "Finding available time slots...")

            slots = await calendar.find_free_slots(
                duration_minutes=state["meeting_duration"],
                time_min=time_min,
                time_max=time_max,
            )

            suggested_slots = [
                {
                    "id": f"slot_{i}",
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "duration_minutes": state["meeting_duration"],
                }
                for i, slot in enumerate(slots[:5])  # Top 5 slots
            ]

            return {
                **state,
                "user_events": user_events,
                "suggested_slots": suggested_slots,
                "current_step": "await_slot_selection",
                "needs_user_input": True,
            }

        except Exception as e:
            return {
                **state,
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

    # Send time slot options via WebSocket
    await _send_slot_options(state["workflow_id"], state["suggested_slots"])

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
    start = datetime.fromisoformat(slot["start"])
    end = datetime.fromisoformat(slot["end"])

    # Prepare actions
    actions = [
        {
            "action_type": ActionType.CALENDAR_CREATE.value,
            "title": "Create Calendar Event",
            "description": f"Create meeting: {state['meeting_title']}",
            "payload": {
                "summary": state["meeting_title"],
                "start": slot["start"],
                "end": slot["end"],
                "attendees": state["attendees"],
                "description": f"Meeting scheduled via Smart Task Executor",
                "add_meet_link": True,
            },
            "requires_approval": True,
            "api_name": "Google Calendar",
            "estimated_cost": 0.0,
            "order": 0,
        },
    ]

    # Add email notification for each attendee
    for i, attendee in enumerate(state["attendees"]):
        actions.append({
            "action_type": ActionType.EMAIL_SEND.value,
            "title": f"Send Email to {attendee}",
            "description": f"Notify {attendee} about the meeting",
            "payload": {
                "to_email": attendee,
                "meeting_title": state["meeting_title"],
                "meeting_datetime": start.strftime("%A, %B %d, %Y at %I:%M %p"),
                "meeting_duration": f"{state['meeting_duration']} minutes",
            },
            "requires_approval": True,
            "api_name": "SendGrid",
            "estimated_cost": 0.001,
            "order": i + 1,
        })

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


def should_continue(state: AgentState) -> str:
    """Determine next step based on current state"""
    if state.get("error"):
        return "error"

    step = state.get("current_step", "parse_request")

    if step == "parse_request":
        return "check_calendars"
    elif step == "check_calendars":
        return "await_slot_selection"
    elif step == "await_slot_selection":
        if state.get("selected_slot"):
            return "prepare_actions"
        return END  # Wait for user input
    elif step == "prepare_actions":
        return "await_action_approval"
    elif step == "await_action_approval":
        return END  # Wait for user approval
    else:
        return END


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
        should_continue,
        {
            "check_calendars": "check_calendars",
            "error": "error",
        }
    )

    graph.add_conditional_edges(
        "check_calendars",
        should_continue,
        {
            "await_slot_selection": "await_slot_selection",
            "error": "error",
        }
    )

    graph.add_conditional_edges(
        "await_slot_selection",
        should_continue,
        {
            "prepare_actions": "prepare_actions",
            END: END,
        }
    )

    graph.add_conditional_edges(
        "prepare_actions",
        should_continue,
        {
            "await_action_approval": "await_action_approval",
            "error": "error",
        }
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
            "user_request": workflow.user_request,
            "meeting_title": None,
            "meeting_duration": None,
            "attendees": [],
            "preferred_timeframe": None,
            "user_events": [],
            "attendee_availability": {},
            "suggested_slots": [],
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

        graph = create_meeting_coordinator_graph()

        # Continue from prepare_actions
        final_state = await graph.ainvoke(state)

        workflow.agent_state = json.dumps(final_state)
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


async def _send_slot_options(workflow_id: str, slots: List[dict]):
    """Send time slot options to frontend via WebSocket"""
    from app.api.websockets.connection_manager import manager
    await manager.broadcast_progress(
        workflow_id,
        "slot_selection",
        {"slots": slots, "message": "Please select a time slot"},
    )


async def _send_approval_request(workflow_id: str):
    """Notify frontend that actions are ready for approval"""
    from app.api.websockets.connection_manager import manager
    await manager.broadcast_progress(
        workflow_id,
        "approval_required",
        {"message": "Actions ready for approval"},
    )
