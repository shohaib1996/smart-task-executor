from typing import Dict, List
from fastapi import WebSocket
import json


class ConnectionManager:
    """Manages WebSocket connections for real-time updates"""

    def __init__(self):
        # Map user_id -> list of active connections
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # Map workflow_id -> list of active connections
        self.workflow_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str, workflow_id: str = None):
        """Accept a new WebSocket connection"""
        await websocket.accept()

        # Add to user connections
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

        # Add to workflow connections if specified
        if workflow_id:
            if workflow_id not in self.workflow_connections:
                self.workflow_connections[workflow_id] = []
            self.workflow_connections[workflow_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str, workflow_id: str = None):
        """Remove a WebSocket connection"""
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

        if workflow_id and workflow_id in self.workflow_connections:
            if websocket in self.workflow_connections[workflow_id]:
                self.workflow_connections[workflow_id].remove(websocket)
            if not self.workflow_connections[workflow_id]:
                del self.workflow_connections[workflow_id]

    async def send_to_user(self, user_id: str, message: dict):
        """Send a message to all connections for a user"""
        if user_id in self.active_connections:
            disconnected = []
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.append(connection)

            # Clean up disconnected connections
            for conn in disconnected:
                self.active_connections[user_id].remove(conn)

    async def send_to_workflow(self, workflow_id: str, message: dict):
        """Send a message to all connections watching a workflow"""
        if workflow_id in self.workflow_connections:
            disconnected = []
            for connection in self.workflow_connections[workflow_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.append(connection)

            # Clean up disconnected connections
            for conn in disconnected:
                self.workflow_connections[workflow_id].remove(conn)

    async def broadcast_progress(self, workflow_id: str, event: str, data: dict):
        """Broadcast a progress event for a workflow"""
        message = {
            "type": event,
            "workflow_id": workflow_id,
            "data": data,
        }
        await self.send_to_workflow(workflow_id, message)


# Global connection manager instance
manager = ConnectionManager()
