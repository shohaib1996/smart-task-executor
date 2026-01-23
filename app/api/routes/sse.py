import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException, status
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.workflow import Workflow, WorkflowStatus

router = APIRouter()


async def workflow_event_generator(workflow_id: str, user_id: str):
    """Generate SSE events for workflow progress"""
    from app.core.database import async_session

    while True:
        async with async_session() as session:
            result = await session.execute(
                select(Workflow).where(
                    Workflow.id == workflow_id,
                    Workflow.user_id == user_id,
                )
            )
            workflow = result.scalar_one_or_none()

            if workflow is None:
                yield {
                    "event": "error",
                    "data": json.dumps({"message": "Workflow not found"}),
                }
                break

            # Send current status
            yield {
                "event": "status",
                "data": json.dumps({
                    "workflow_id": workflow.id,
                    "status": workflow.status.value,
                    "updated_at": workflow.updated_at.isoformat(),
                }),
            }

            # Check if workflow is complete
            if workflow.status in [
                WorkflowStatus.COMPLETED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
            ]:
                yield {
                    "event": "complete",
                    "data": json.dumps({
                        "workflow_id": workflow.id,
                        "status": workflow.status.value,
                        "summary": workflow.summary,
                        "error_message": workflow.error_message,
                    }),
                }
                break

        # Poll every 2 seconds
        await asyncio.sleep(2)


@router.get("/workflows/{workflow_id}/stream")
async def stream_workflow(
    workflow_id: str,
    current_user: User = Depends(get_current_user),
):
    """SSE endpoint for streaming workflow updates"""
    return EventSourceResponse(
        workflow_event_generator(workflow_id, current_user.id)
    )
