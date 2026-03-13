"""AstrBot Bridge ON_START EventHandler

在MaiBot启动时:
1. 注册WS自定义消息处理器，接收来自AstrBot的tool_sync和tool_result消息。
2. Monkey-patch legacy MessageServer.send_message，
   使 platform="astrbot" 的回复走 API Server 而非旧 WS Server。
"""

from typing import Tuple, Optional

from src.common.logger import get_logger
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import EventType, MaiMessages, CustomEventHandlerResult

logger = get_logger("astrbot_bridge_handler")

ASTRBOT_PLATFORM = "astrbot"


class AstrBotBridgeInitHandler(BaseEventHandler):
    """ON_START事件处理器 — 注册tool_sync/tool_result处理器 + send_message路由补丁"""

    event_type = EventType.ON_START
    handler_name = "astrbot_bridge_init"
    handler_description = "在启动时注册AstrBot工具桥接的WS消息处理器并修补消息路由"
    weight = 0

    async def execute(
        self, message: MaiMessages | None
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        """在ON_START时初始化工具桥接"""
        try:
            from src.common.message import get_global_api
            from .tool_bridge import (
                _handle_tool_sync_ws, _handle_tool_result_ws,
            )

            api = get_global_api()

            # ── 1. Register custom handlers on API Server ──
            extra_server = getattr(api, "extra_server", None)
            if extra_server and hasattr(extra_server, "config"):
                extra_server.config.custom_handlers["custom_tool_sync"] = _handle_tool_sync_ws
                extra_server.config.custom_handlers["custom_tool_result"] = _handle_tool_result_ws
                logger.info("[AstrBot Bridge] 工具桥接初始化成功 (API Server custom_handlers)")
            else:
                # Fallback: register on MessageServer layer (legacy mode)
                from .tool_bridge import init_astrbot_tool_bridge
                init_astrbot_tool_bridge(api)
                logger.info("[AstrBot Bridge] 工具桥接初始化成功 (MessageServer fallback)")

            # ── 2. Monkey-patch send_message for astrbot platform routing ──
            if extra_server:
                self._patch_send_message(api, extra_server)

            return True, True, None, None, None
        except Exception as e:
            logger.error(f"[AstrBot Bridge] 工具桥接初始化失败: {e}", exc_info=True)
            return False, True, None, None, None

    @staticmethod
    def _patch_send_message(api, extra_server) -> None:
        """Wrap legacy api.send_message so 'astrbot' platform goes through API Server.

        Legacy flow:  api.send_message(msg) → ws_connection.send_message(platform, dict)
                      → platform_websockets[platform]  → FAIL (astrbot not there)

        Patched flow: if platform == 'astrbot', build APIMessageBase and call
                      extra_server.send_message() instead.
        """
        original_send = api.send_message

        async def patched_send_message(message):
            platform = getattr(
                getattr(message, "message_info", None), "platform", None
            )

            if platform == ASTRBOT_PLATFORM:
                # Route through API Server
                try:
                    from maim_message.message import APIMessageBase, MessageDim

                    platform_map = getattr(api, "platform_map", {})
                    target_api_key = platform_map.get(ASTRBOT_PLATFORM, "")

                    msg_dim = MessageDim(api_key=target_api_key, platform=ASTRBOT_PLATFORM)
                    api_message = APIMessageBase(
                        message_info=message.message_info,
                        message_segment=message.message_segment,
                        message_dim=msg_dim,
                    )

                    results = await extra_server.send_message(api_message)
                    success = any(results.values()) if results else False
                    if success:
                        logger.debug(f"[AstrBot Bridge] 消息已通过 API Server 发送至 astrbot 平台")
                    else:
                        logger.warning(f"[AstrBot Bridge] API Server 发送失败，回退 legacy")
                        return await original_send(message)
                    return success
                except Exception as e:
                    logger.warning(f"[AstrBot Bridge] API Server 路由异常: {e}，回退 legacy")
                    return await original_send(message)
            else:
                return await original_send(message)

        api.send_message = patched_send_message
        logger.info("[AstrBot Bridge] 已安装 send_message 路由补丁 (astrbot → API Server)")
