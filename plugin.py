"""AstrBot Bridge 插件

在ON_START时注册WS自定义消息处理器（tool_sync / tool_result），
接收AstrBot推送的工具定义并动态注册代理BaseTool类。
"""

from typing import List, Tuple, Type, Union

from src.plugin_system import BasePlugin, register_plugin
from src.plugin_system.base.component_types import EventHandlerInfo, ToolInfo
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.config_types import ConfigField
from src.common.logger import get_logger

from .astrbot_bridge_handler import AstrBotBridgeInitHandler

logger = get_logger("astrbot_bridge_plugin")


@register_plugin
class AstrBotBridgePlugin(BasePlugin):
    """AstrBot Bridge 插件 — 将AstrBot工具注入到MaiBot"""

    plugin_name: str = "astrbot_bridge"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = ""

    config_schema: dict = {}

    def get_plugin_components(
        self,
    ) -> List[
        Union[
            Tuple[EventHandlerInfo, Type[BaseEventHandler]],
            Tuple[ToolInfo, Type[BaseTool]],
        ]
    ]:
        """返回插件组件列表"""
        return [
            (AstrBotBridgeInitHandler.get_handler_info(), AstrBotBridgeInitHandler),
        ]
