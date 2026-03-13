"""AstrBot Tool Bridge — receives tool definitions from AstrBot via WS and registers
them as MaiBot BaseTool proxy classes so MaiBot's LLM ToolExecutor can call them.

Protocol:
  AstrBot → MaiBot:  {"is_custom_message": true, "message_type_name": "tool_sync",
                       "content": {"tools": [...]}}
  MaiBot → AstrBot:  {"is_custom_message": true, "message_type_name": "tool_call",
                       "content": {"call_id": "...", "name": "...", "args": {...}}}
  AstrBot → MaiBot:  {"is_custom_message": true, "message_type_name": "tool_result",
                       "content": {"call_id": "...", "result": {"content": "..."}}}
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional, Type

from src.common.logger import get_logger
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.component_types import (
    ComponentType,
    ToolInfo,
    ToolParamType,
)
from src.plugin_system.core.component_registry import component_registry

logger = get_logger("astrbot_tool_bridge")

# Map JSON Schema types to MaiBot ToolParamType
# MaiBot only has: STRING, INTEGER, FLOAT, BOOLEAN
_JSON_TYPE_MAP: Dict[str, ToolParamType] = {
    "string": ToolParamType.STRING,
    "integer": ToolParamType.INTEGER,
    "number": ToolParamType.FLOAT,
    "boolean": ToolParamType.BOOLEAN,
    "array": ToolParamType.STRING,    # fallback: serialize as string
    "object": ToolParamType.STRING,   # fallback: serialize as string
}

# Pending tool_call futures: call_id → asyncio.Future[str]
_pending_calls: Dict[str, asyncio.Future] = {}
# Connection UUID for the AstrBot WS connection (set during tool_sync)
_astrbot_connection_uuid: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_astrbot_tool_bridge(message_server) -> None:
    """Register custom message handlers on the MessageServer (legacy fallback).

    Call once during MaiBot startup (after plugins are loaded).
    """
    message_server.register_custom_message_handler("tool_sync", _handle_tool_sync)
    message_server.register_custom_message_handler("tool_result", _handle_tool_result)
    logger.info("[AstrBot Bridge] Tool bridge initialized — waiting for tool_sync from AstrBot")


# ---------------------------------------------------------------------------
# WS-layer handler wrappers (for server_ws_api.py custom_handlers)
# server_ws_api.py calls handler(message_data, metadata) — both are dicts
# ---------------------------------------------------------------------------


async def _handle_tool_sync_ws(message_data: dict, metadata: dict) -> None:
    """Adapter for server_ws_api.py custom_handlers['custom_tool_sync']."""
    global _astrbot_connection_uuid
    # Store the connection UUID so we can send tool_call back to this connection
    conn_uuid = metadata.get("uuid", "")
    if conn_uuid:
        _astrbot_connection_uuid = conn_uuid
        logger.info(f"[AstrBot Bridge] Stored AstrBot connection UUID: {conn_uuid}")
    # message_data has: type, platform, content
    await _handle_tool_sync({
        "content": message_data.get("content", {}),
        "platform": message_data.get("platform", metadata.get("platform", "astrbot")),
    })


async def _handle_tool_result_ws(message_data: dict, metadata: dict) -> None:
    """Adapter for server_ws_api.py custom_handlers['custom_tool_result']."""
    logger.info(f"[AstrBot Bridge] Received tool_result via WS: {str(message_data)[:200]}")
    await _handle_tool_result({
        "content": message_data.get("content", message_data),
    })


# ---------------------------------------------------------------------------
# Handler: tool_sync
# ---------------------------------------------------------------------------


async def _handle_tool_sync(message: dict) -> None:
    """Receive tool definitions from AstrBot and register proxy BaseTool classes."""
    content = message.get("content", {})
    tools: List[dict] = content.get("tools", [])
    platform = message.get("platform", "astrbot")

    if not tools:
        logger.warning("[AstrBot Bridge] Received empty tool_sync")
        return

    registered = 0
    for tool_def in tools:
        name = tool_def.get("name", "")
        if not name:
            continue

        description = tool_def.get("description", "")
        parameters_schema = tool_def.get("parameters", {})

        # Convert JSON Schema parameters to MaiBot ToolParamType format
        maibot_params = _convert_parameters(parameters_schema)

        # Create dynamic BaseTool subclass
        proxy_class = _create_proxy_tool_class(
            tool_name=name,
            tool_description=description,
            tool_params=maibot_params,
            source_platform=platform,
        )

        # Build ToolInfo
        tool_info = ToolInfo(
            name=name,
            description=description,
            component_type=ComponentType.TOOL,
            enabled=True,
            plugin_name="astrbot_bridge",
            tool_description=description,
            tool_parameters=maibot_params,
        )

        # Register in component_registry
        ok = component_registry.register_component(tool_info, proxy_class)
        if ok:
            registered += 1
            logger.info(f"[AstrBot Bridge] Registered proxy tool: {name}")
        else:
            logger.warning(f"[AstrBot Bridge] Failed to register tool: {name} (may already exist)")

    logger.info(f"[AstrBot Bridge] tool_sync complete — {registered}/{len(tools)} tools registered")


# ---------------------------------------------------------------------------
# Handler: tool_result
# ---------------------------------------------------------------------------


async def _handle_tool_result(message: dict) -> None:
    """Receive a tool execution result from AstrBot and resolve the pending future."""
    content = message.get("content", {})
    call_id = content.get("call_id", "")
    result_data = content.get("result", {})
    result_text = result_data.get("content", "")

    logger.info(f"[AstrBot Bridge] Processing tool_result: call_id={call_id}, result_len={len(str(result_text))}")
    logger.info(f"[AstrBot Bridge] Pending calls: {list(_pending_calls.keys())}")

    future = _pending_calls.pop(call_id, None)
    if future and not future.done():
        future.set_result(result_text)
        logger.info(f"[AstrBot Bridge] Resolved tool_result for call_id={call_id}")
    else:
        logger.warning(f"[AstrBot Bridge] No pending future for call_id={call_id} (future={future})")


# ---------------------------------------------------------------------------
# Remote tool call (MaiBot → AstrBot)
# ---------------------------------------------------------------------------


async def call_remote_tool(
    platform: str,
    tool_name: str,
    tool_args: dict,
    timeout: float = 30.0,
) -> str:
    """Send a tool_call to AstrBot via WS and wait for the result.

    Args:
        platform: The AstrBot platform identifier.
        tool_name: Name of the tool to call.
        tool_args: Arguments dict.
        timeout: Max seconds to wait for result.

    Returns:
        Result string from AstrBot.
    """
    from src.common.message import get_global_api

    call_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_calls[call_id] = future

    # Send tool_call via the API Server (extra_server) WS connection
    # AstrBot connects through the API Server, not the legacy MessageServer
    api = get_global_api()
    extra_server = getattr(api, "extra_server", None)

    if extra_server:
        await extra_server.send_custom_message(
            message_type="custom_tool_call",
            payload={
                "call_id": call_id,
                "name": tool_name,
                "args": tool_args,
            },
            connection_uuid=_astrbot_connection_uuid,
        )
    else:
        # Fallback: legacy MessageServer
        await api.send_custom_message(
            platform=platform,
            message_type_name="tool_call",
            message={
                "call_id": call_id,
                "name": tool_name,
                "args": tool_args,
            },
        )
    logger.info(f"[AstrBot Bridge] Sent tool_call: {tool_name}(call_id={call_id})")

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        _pending_calls.pop(call_id, None)
        logger.error(f"[AstrBot Bridge] tool_call timeout: {tool_name}(call_id={call_id})")
        return f"Tool call timed out after {timeout}s"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _convert_parameters(
    schema: dict,
) -> list:
    """Convert JSON Schema parameters to MaiBot's ToolParamType tuple list.

    Returns list of (name, ToolParamType, description, required, enum_values).
    """
    result = []
    properties = schema.get("properties", {})
    required_list = schema.get("required", [])

    for param_name, param_info in properties.items():
        json_type = param_info.get("type", "string")
        param_type = _JSON_TYPE_MAP.get(json_type, ToolParamType.STRING)
        description = param_info.get("description", "")
        is_required = param_name in required_list
        enum_values = param_info.get("enum", None)

        result.append((param_name, param_type, description, is_required, enum_values))

    return result


def _create_proxy_tool_class(
    tool_name: str,
    tool_description: str,
    tool_params: list,
    source_platform: str,
) -> Type[BaseTool]:
    """Dynamically create a BaseTool subclass that proxies calls to AstrBot."""

    class AstrBotProxyTool(BaseTool):
        name = tool_name
        description = tool_description
        parameters = tool_params
        available_for_llm = True

        _source_platform = source_platform

        async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
            # Remove internal llm_called marker if present
            args = {k: v for k, v in function_args.items() if k != "llm_called"}

            result_text = await call_remote_tool(
                platform=self._source_platform,
                tool_name=self.name,
                tool_args=args,
            )
            return {"content": result_text}

    # Give the class a unique name for debugging
    AstrBotProxyTool.__name__ = f"AstrBotProxy_{tool_name}"
    AstrBotProxyTool.__qualname__ = f"AstrBotProxy_{tool_name}"

    return AstrBotProxyTool
