import json
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.workflow import Workflow, Action, AuditLog, WorkflowStatus, ActionStatus
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowResponse,
    WorkflowDetailResponse,
    WorkflowApproval,
    ActionEdit,
    ActionResponse,
    AuditLogResponse,
)
from app.tasks.workflow_tasks import start_workflow_task

router = APIRouter()


@router.post("/", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    workflow_data: WorkflowCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Create a new workflow from user request"""
    workflow = Workflow(
        user_id=current_user.id,
        user_request=workflow_data.user_request,
        status=WorkflowStatus.PENDING,
    )
    session.add(workflow)

    # Create initial audit log
    audit_log = AuditLog(
        workflow_id=workflow.id,
        event_type="user_request",
        message=f"Workflow initiated: {workflow_data.user_request[:100]}...",
    )
    session.add(audit_log)

    await session.commit()
    await session.refresh(workflow)

    # Trigger async workflow processing
    start_workflow_task.delay(workflow.id, current_user.id)

    return WorkflowResponse.model_validate(workflow)


@router.get("/", response_model=List[WorkflowResponse])
async def list_workflows(
    skip: int = 0,
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """List user's workflows"""
    result = await session.execute(
        select(Workflow)
        .where(Workflow.user_id == current_user.id)
        .order_by(Workflow.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    workflows = result.scalars().all()
    return [WorkflowResponse.model_validate(w) for w in workflows]


@router.get("/{workflow_id}", response_model=WorkflowDetailResponse)
async def get_workflow(
    workflow_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Get workflow details including actions and audit logs"""
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.user_id == current_user.id,
        )
    )
    workflow = result.scalar_one_or_none()

    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found",
        )

    # Load actions
    actions_result = await session.execute(
        select(Action).where(Action.workflow_id == workflow_id).order_by(Action.order)
    )
    actions = actions_result.scalars().all()

    # Load audit logs
    logs_result = await session.execute(
        select(AuditLog)
        .where(AuditLog.workflow_id == workflow_id)
        .order_by(AuditLog.timestamp)
    )
    audit_logs = logs_result.scalars().all()

    # Extract pending slots, conflict info, and inaccessible calendars from agent_state
    pending_slots = []
    conflict_info = None
    inaccessible_calendars = []
    if workflow.status == WorkflowStatus.AWAITING_APPROVAL and workflow.agent_state:
        try:
            agent_state = json.loads(workflow.agent_state)
            # Check if we're waiting for slot selection (no selected_slot yet)
            if agent_state.get("suggested_slots") and not agent_state.get(
                "selected_slot"
            ):
                pending_slots = agent_state["suggested_slots"]
                conflict_info = agent_state.get("conflict_info")
                inaccessible_calendars = agent_state.get("inaccessible_calendars", [])
        except (json.JSONDecodeError, KeyError):
            pass

    # Build response
    workflow_dict = {
        "id": workflow.id,
        "user_request": workflow.user_request,
        "status": workflow.status,
        "summary": workflow.summary,
        "error_message": workflow.error_message,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
        "completed_at": workflow.completed_at,
        "actions": [
            ActionResponse(
                id=a.id,
                action_type=a.action_type,
                title=a.title,
                description=a.description,
                payload=json.loads(a.payload),
                status=a.status,
                requires_approval=a.requires_approval,
                api_name=a.api_name,
                estimated_cost=a.estimated_cost,
                order=a.order,
                result=json.loads(a.result) if a.result else None,
                error_message=a.error_message,
                executed_at=a.executed_at,
            )
            for a in actions
        ],
        "audit_logs": [
            AuditLogResponse(
                id=log.id,
                event_type=log.event_type,
                message=log.message,
                details=json.loads(log.details) if log.details else None,
                timestamp=log.timestamp,
            )
            for log in audit_logs
        ],
        "pending_slots": pending_slots,
        "conflict_info": conflict_info,
        "inaccessible_calendars": inaccessible_calendars,
    }

    return WorkflowDetailResponse(**workflow_dict)


@router.post("/{workflow_id}/approve")
async def approve_actions(
    workflow_id: str,
    approval: WorkflowApproval,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Approve or reject actions in a workflow"""
    # Verify workflow ownership
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.user_id == current_user.id,
        )
    )
    workflow = result.scalar_one_or_none()

    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found",
        )

    if workflow.status != WorkflowStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow is not awaiting approval",
        )

    # Update action statuses
    for action_approval in approval.actions:
        action_result = await session.execute(
            select(Action).where(
                Action.id == action_approval.action_id,
                Action.workflow_id == workflow_id,
            )
        )
        action = action_result.scalar_one_or_none()

        if action:
            action.status = (
                ActionStatus.APPROVED
                if action_approval.approved
                else ActionStatus.REJECTED
            )

    # Log approval
    approved_count = sum(1 for a in approval.actions if a.approved)
    rejected_count = len(approval.actions) - approved_count

    audit_log = AuditLog(
        workflow_id=workflow_id,
        event_type="approval_decision",
        message=f"User approved {approved_count} actions, rejected {rejected_count}",
    )
    session.add(audit_log)

    # Update workflow status to executing
    workflow.status = WorkflowStatus.EXECUTING
    await session.commit()

    # Trigger execution of approved actions
    from app.tasks.workflow_tasks import execute_approved_actions

    execute_approved_actions.delay(workflow_id)

    return {
        "message": "Actions processed",
        "approved": approved_count,
        "rejected": rejected_count,
    }


@router.put("/{workflow_id}/actions/{action_id}")
async def edit_action(
    workflow_id: str,
    action_id: str,
    edit: ActionEdit,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Edit an action's payload before approval"""
    # Verify workflow ownership
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.user_id == current_user.id,
        )
    )
    workflow = result.scalar_one_or_none()

    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found",
        )

    if workflow.status != WorkflowStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only edit actions while awaiting approval",
        )

    # Get and update action
    action_result = await session.execute(
        select(Action).where(
            Action.id == action_id,
            Action.workflow_id == workflow_id,
        )
    )
    action = action_result.scalar_one_or_none()

    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action not found",
        )

    action.payload = json.dumps(edit.payload)

    # Log edit
    audit_log = AuditLog(
        workflow_id=workflow_id,
        event_type="action_edited",
        message=f"Action '{action.title}' was edited by user",
        details=json.dumps({"action_id": action_id}),
    )
    session.add(audit_log)

    await session.commit()

    return {"message": "Action updated"}


@router.delete("/{workflow_id}/actions/{action_id}")
async def remove_action(
    workflow_id: str,
    action_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Remove an action from workflow (mark as skipped)"""
    # Verify workflow ownership
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.user_id == current_user.id,
        )
    )
    workflow = result.scalar_one_or_none()

    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found",
        )

    if workflow.status != WorkflowStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only remove actions while awaiting approval",
        )

    # Get and update action
    action_result = await session.execute(
        select(Action).where(
            Action.id == action_id,
            Action.workflow_id == workflow_id,
        )
    )
    action = action_result.scalar_one_or_none()

    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action not found",
        )

    action.status = ActionStatus.SKIPPED

    # Log removal
    audit_log = AuditLog(
        workflow_id=workflow_id,
        event_type="action_removed",
        message=f"Action '{action.title}' was removed by user",
        details=json.dumps({"action_id": action_id}),
    )
    session.add(audit_log)

    await session.commit()

    return {"message": "Action removed"}


@router.post("/{workflow_id}/cancel")
async def cancel_workflow(
    workflow_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Cancel a workflow"""
    result = await session.execute(
        select(Workflow).where(
            Workflow.id == workflow_id,
            Workflow.user_id == current_user.id,
        )
    )
    workflow = result.scalar_one_or_none()

    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found",
        )

    if workflow.status in [WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow cannot be cancelled",
        )

    workflow.status = WorkflowStatus.CANCELLED

    audit_log = AuditLog(
        workflow_id=workflow_id,
        event_type="workflow_cancelled",
        message="Workflow cancelled by user",
    )
    session.add(audit_log)

    await session.commit()

    return {"message": "Workflow cancelled"}
