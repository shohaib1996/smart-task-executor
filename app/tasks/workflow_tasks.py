import json
import asyncio
from datetime import datetime
from celery import shared_task
from sqlmodel import select

from app.tasks.celery_app import celery_app
from app.core.database import async_session
from app.models.workflow import Workflow, Action, AuditLog, WorkflowStatus, ActionStatus


def run_async(coro):
    """Helper to run async code in Celery tasks"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        return loop.run_until_complete(coro)
    finally:
        # Don't close the loop - reuse it for subsequent tasks
        pass


@celery_app.task(bind=True, max_retries=3)
def start_workflow_task(self, workflow_id: str, user_id: str):
    """Start processing a workflow with the AI agent"""
    run_async(_start_workflow_async(workflow_id, user_id))


async def _start_workflow_async(workflow_id: str, user_id: str):
    """Async implementation of workflow start"""
    async with async_session() as session:
        # Get workflow
        result = await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )
        workflow = result.scalar_one_or_none()

        if not workflow:
            return

        # Update status
        workflow.status = WorkflowStatus.INVESTIGATING
        workflow.updated_at = datetime.utcnow()

        # Log
        audit_log = AuditLog(
            workflow_id=workflow_id,
            event_type="agent_started",
            message="AI agent started investigating request",
        )
        session.add(audit_log)
        await session.commit()

        # Send WebSocket update
        await _send_progress_update(workflow_id, "investigating", "Analyzing your request...")

    # Run the agent
    from app.agents.meeting_coordinator import run_meeting_coordinator
    await run_meeting_coordinator(workflow_id, user_id)


@celery_app.task(bind=True, max_retries=3)
def execute_approved_actions(self, workflow_id: str):
    """Execute approved actions in a workflow"""
    run_async(_execute_actions_async(workflow_id))


async def _execute_actions_async(workflow_id: str):
    """Async implementation of action execution"""
    async with async_session() as session:
        # Get approved actions
        result = await session.execute(
            select(Action)
            .where(
                Action.workflow_id == workflow_id,
                Action.status == ActionStatus.APPROVED,
            )
            .order_by(Action.order)
        )
        actions = result.scalars().all()

        if not actions:
            return

        # Get workflow
        wf_result = await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )
        workflow = wf_result.scalar_one_or_none()

        if not workflow:
            return

        # Get user for credentials
        from app.models.user import User
        user_result = await session.execute(
            select(User).where(User.id == workflow.user_id)
        )
        user = user_result.scalar_one_or_none()

        executed_count = 0
        failed_count = 0

        for action in actions:
            try:
                action.status = ActionStatus.EXECUTING
                await session.commit()

                await _send_progress_update(
                    workflow_id,
                    "executing",
                    f"Executing: {action.title}",
                )

                # Execute the action
                result = await _execute_single_action(action, user)

                action.status = ActionStatus.COMPLETED
                action.result = json.dumps(result)
                action.executed_at = datetime.utcnow()
                executed_count += 1

                # Log success
                audit_log = AuditLog(
                    workflow_id=workflow_id,
                    event_type="action_completed",
                    message=f"Action completed: {action.title}",
                    details=json.dumps({"action_id": action.id, "result": result}),
                )
                session.add(audit_log)

            except Exception as e:
                action.status = ActionStatus.FAILED
                action.error_message = str(e)
                failed_count += 1

                # Log failure
                audit_log = AuditLog(
                    workflow_id=workflow_id,
                    event_type="action_failed",
                    message=f"Action failed: {action.title} - {str(e)}",
                    details=json.dumps({"action_id": action.id, "error": str(e)}),
                )
                session.add(audit_log)

            await session.commit()

        # Update workflow status
        if failed_count == 0:
            workflow.status = WorkflowStatus.COMPLETED
            workflow.summary = f"Successfully executed {executed_count} actions"
        elif executed_count > 0:
            workflow.status = WorkflowStatus.COMPLETED
            workflow.summary = f"Executed {executed_count} actions, {failed_count} failed"
        else:
            workflow.status = WorkflowStatus.FAILED
            workflow.error_message = "All actions failed"

        workflow.completed_at = datetime.utcnow()
        workflow.updated_at = datetime.utcnow()

        # Final log
        audit_log = AuditLog(
            workflow_id=workflow_id,
            event_type="workflow_completed",
            message=workflow.summary or workflow.error_message,
        )
        session.add(audit_log)

        await session.commit()

        await _send_progress_update(
            workflow_id,
            "completed",
            workflow.summary or "Workflow completed",
        )


async def _execute_single_action(action: Action, user) -> dict:
    """Execute a single action based on its type"""
    from app.models.workflow import ActionType
    from app.services.calendar import CalendarService
    from app.services.email import EmailService
    from app.services.notion import NotionService

    payload = json.loads(action.payload)

    if action.action_type == ActionType.CALENDAR_CREATE:
        service = CalendarService(
            access_token=user.google_access_token,
            refresh_token=user.google_refresh_token,
        )
        event = await service.create_event(
            summary=payload.get("summary"),
            start=datetime.fromisoformat(payload.get("start")),
            end=datetime.fromisoformat(payload.get("end")),
            attendees=payload.get("attendees", []),
            description=payload.get("description"),
            add_meet_link=payload.get("add_meet_link", True),
        )
        return {"event_id": event.id, "meet_link": event.meet_link}

    elif action.action_type == ActionType.EMAIL_SEND:
        service = EmailService()
        result = await service.send_meeting_notification(
            to_email=payload.get("to_email"),
            meeting_title=payload.get("meeting_title"),
            meeting_datetime=payload.get("meeting_datetime"),
            meeting_duration=payload.get("meeting_duration"),
            meeting_link=payload.get("meeting_link"),
        )
        return result

    elif action.action_type == ActionType.NOTION_CREATE:
        service = NotionService()
        page = await service.create_meeting_notes(
            parent_id=payload.get("parent_id"),
            meeting_title=payload.get("meeting_title"),
            meeting_date=payload.get("meeting_date"),
            attendees=payload.get("attendees", []),
            agenda=payload.get("agenda"),
        )
        return {"page_id": page.id, "url": page.url}

    else:
        raise ValueError(f"Unknown action type: {action.action_type}")


@celery_app.task(bind=True)
def process_user_selection(self, workflow_id: str, option_id: str, user_id: str):
    """Process user's selection (e.g., time slot selection)"""
    run_async(_process_selection_async(workflow_id, option_id, user_id))


async def _process_selection_async(workflow_id: str, option_id: str, user_id: str):
    """Async implementation of selection processing"""
    async with async_session() as session:
        result = await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )
        workflow = result.scalar_one_or_none()

        if not workflow:
            return

        # Store selection in agent state
        agent_state = json.loads(workflow.agent_state or "{}")
        agent_state["selected_option"] = option_id
        workflow.agent_state = json.dumps(agent_state)

        # Log
        audit_log = AuditLog(
            workflow_id=workflow_id,
            event_type="user_selection",
            message=f"User selected option: {option_id}",
        )
        session.add(audit_log)

        await session.commit()

    # Continue agent processing
    from app.agents.meeting_coordinator import continue_after_selection
    await continue_after_selection(workflow_id, option_id, user_id)


async def _send_progress_update(workflow_id: str, event: str, message: str):
    """Send progress update via WebSocket"""
    try:
        from app.api.websockets.connection_manager import manager
        await manager.broadcast_progress(
            workflow_id,
            event,
            {"message": message, "timestamp": datetime.utcnow().isoformat()},
        )
    except Exception:
        pass  # Ignore WebSocket errors
