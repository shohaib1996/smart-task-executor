from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from app.core.security import decode_token
from app.api.websockets.connection_manager import manager

router = APIRouter()


@router.websocket("/ws/{workflow_id}")
async def workflow_websocket(
    websocket: WebSocket,
    workflow_id: str,
    token: str = Query(...),
):
    """WebSocket endpoint for real-time workflow updates"""
    # Verify token
    payload = decode_token(token)
    if payload is None:
        await websocket.close(code=4001, reason="Invalid token")
        return

    user_id = payload.get("sub")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token payload")
        return

    # Connect
    await manager.connect(websocket, user_id, workflow_id)

    try:
        while True:
            # Keep connection alive and handle incoming messages
            data = await websocket.receive_json()

            # Handle different message types from client
            message_type = data.get("type")

            if message_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif message_type == "select_option":
                # User selected an option (e.g., time slot)
                # This will be processed by the agent
                from app.tasks.workflow_tasks import process_user_selection
                process_user_selection.delay(
                    workflow_id,
                    data.get("option_id"),
                    user_id,
                )

    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id, workflow_id)


@router.websocket("/ws")
async def user_websocket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """WebSocket endpoint for general user notifications"""
    # Verify token
    payload = decode_token(token)
    if payload is None:
        await websocket.close(code=4001, reason="Invalid token")
        return

    user_id = payload.get("sub")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token payload")
        return

    # Connect
    await manager.connect(websocket, user_id)

    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
