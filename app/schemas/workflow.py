from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
from app.models.workflow import WorkflowStatus, ActionStatus, ActionType


# Request schemas
class WorkflowCreate(BaseModel):
    user_request: str


class ActionApproval(BaseModel):
    action_id: str
    approved: bool


class WorkflowApproval(BaseModel):
    workflow_id: str
    actions: List[ActionApproval]


class ActionEdit(BaseModel):
    action_id: str
    payload: dict


# Response schemas
class ActionResponse(BaseModel):
    id: str
    action_type: ActionType
    title: str
    description: str
    payload: dict
    status: ActionStatus
    requires_approval: bool
    api_name: Optional[str] = None
    estimated_cost: float = 0.0
    order: int
    result: Optional[dict] = None
    error_message: Optional[str] = None
    executed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AuditLogResponse(BaseModel):
    id: str
    event_type: str
    message: str
    details: Optional[dict] = None
    timestamp: datetime

    class Config:
        from_attributes = True


class WorkflowResponse(BaseModel):
    id: str
    user_request: str
    status: WorkflowStatus
    summary: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    actions: List[ActionResponse] = []

    class Config:
        from_attributes = True


class WorkflowDetailResponse(WorkflowResponse):
    audit_logs: List[AuditLogResponse] = []


# SSE Event schemas
class SSEEvent(BaseModel):
    event: str  # "progress", "approval_required", "completed", "error"
    data: dict


class ProgressEvent(BaseModel):
    message: str
    step: str
    progress_percent: Optional[int] = None


class ApprovalRequiredEvent(BaseModel):
    workflow_id: str
    actions: List[ActionResponse]
    message: str


class TimeSlotOption(BaseModel):
    id: str
    datetime: datetime
    duration_minutes: int
    attendees_available: List[str]
    notes: Optional[str] = None


class TimeSlotSelectionEvent(BaseModel):
    workflow_id: str
    options: List[TimeSlotOption]
    message: str
