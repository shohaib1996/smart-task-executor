from datetime import datetime
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
from uuid import uuid4
from enum import Enum


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ActionType(str, Enum):
    CALENDAR_READ = "calendar_read"
    CALENDAR_CREATE = "calendar_create"
    CALENDAR_UPDATE = "calendar_update"
    CALENDAR_DELETE = "calendar_delete"
    EMAIL_SEND = "email_send"
    NOTION_CREATE = "notion_create"
    NOTION_UPDATE = "notion_update"


class Workflow(SQLModel, table=True):
    __tablename__ = "workflows"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)

    # Original user request
    user_request: str

    # Current status
    status: WorkflowStatus = Field(default=WorkflowStatus.PENDING)

    # Agent state (JSON stored as string for LangGraph state)
    agent_state: Optional[str] = None

    # Summary of what was accomplished
    summary: Optional[str] = None

    # Error message if failed
    error_message: Optional[str] = None

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    # Relationships
    actions: List["Action"] = Relationship(back_populates="workflow")
    audit_logs: List["AuditLog"] = Relationship(back_populates="workflow")


class Action(SQLModel, table=True):
    __tablename__ = "actions"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    workflow_id: str = Field(foreign_key="workflows.id", index=True)

    # Action details
    action_type: ActionType
    title: str
    description: str

    # Action payload (JSON as string)
    payload: str

    # Status
    status: ActionStatus = Field(default=ActionStatus.PENDING)

    # Requires approval?
    requires_approval: bool = True

    # Execution result
    result: Optional[str] = None
    error_message: Optional[str] = None

    # Cost tracking
    api_name: Optional[str] = None
    estimated_cost: float = 0.0
    actual_cost: float = 0.0

    # Order in workflow
    order: int = 0

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    executed_at: Optional[datetime] = None

    # Relationships
    workflow: Optional[Workflow] = Relationship(back_populates="actions")


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    workflow_id: str = Field(foreign_key="workflows.id", index=True)

    # Log entry
    event_type: str  # e.g., "user_request", "calendar_read", "approval_granted"
    message: str
    details: Optional[str] = None  # JSON for extra data

    # Timestamp
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    workflow: Optional[Workflow] = Relationship(back_populates="audit_logs")
