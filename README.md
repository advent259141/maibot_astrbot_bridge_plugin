# MaiBot Plugin: AstrBot Bridge

MaiBot 侧的桥接插件，配合 AstrBot 的 `astrbot_plugin_maibot` 插件使用。负责接收 AstrBot 推送的工具定义、处理工具调用结果，以及修补消息回复路由，确保 MaiBot 的回复能正确发回 AstrBot。

## 功能

- 🛠️ **工具桥接** — 接收 AstrBot 通过 WebSocket 推送的工具定义（`custom_tool_sync`），注册为 MaiBot 的 `BaseTool` 代理，使 MaiBot 的 LLM ToolExecutor 能远程调用 AstrBot 侧的工具
- 📩 **工具结果回传** — 处理 AstrBot 返回的工具调用结果（`custom_tool_result`），将结果注入等待中的 Future
- 🔀 **消息路由补丁** — Monkey-patch `MessageServer.send_message`，当目标平台为 `astrbot` 时，将消息通过 API Server（`extra_server`）路由，而非旧版 WS Server（解决 "未找到目标平台: astrbot" 问题）

## 安装

将整个 `maibot_astrbot_bridge_plugin/` 目录放入 MaiBot 的 `plugins/` 目录即可，MaiBot 启动时会自动加载。

## 前置条件

- MaiBot `>= 0.10.0`
- MaiBot 配置中需开启 API Server（`enable_api_server: true`）
- `maim_message >= 0.6.0`（支持 API Server 和自定义消息类型）
- AstrBot 侧需安装 `astrbot_plugin_maibot` 插件

## 工作原理

```
AstrBot                          MaiBot
┌─────────────────┐              ┌─────────────────────────────┐
│ astrbot_plugin   │    WS连接    │  API Server (extra_server)  │
│ _maibot          │◄───────────►│  ↓ custom_tool_sync         │
│                  │             │  ↓ custom_tool_result        │
│ MaiBotWSClient   │             │  maibot_astrbot_bridge_plugin│
│  ├ send_message  │             │  ├ tool_bridge.py            │
│  └ recv_response │             │  ├ astrbot_bridge_handler.py │
└─────────────────┘              │  └ send_message patch ──────┤
                                 │                              │
                                 │  MessageServer (legacy)      │
                                 │  └ send_message() ─► patch ─┤
                                 │    routes "astrbot" via      │
                                 │    API Server instead        │
                                 └─────────────────────────────┘
```

### 消息路由补丁

MaiBot 的消息发送走 `MessageServer.send_message()`（旧版 WS Server），但 AstrBot 连接的是 API Server。桥接插件在 `ON_START` 时 monkey-patch `send_message`：

- `platform == "astrbot"` → 构造 `APIMessageBase`，通过 `extra_server.send_message()` 发送
- 其他平台 → 走原有逻辑

## 文件结构

```
maibot_astrbot_bridge_plugin/
├── _manifest.json              # 插件清单
├── plugin.py                   # 插件入口，声明组件
├── astrbot_bridge_handler.py   # ON_START 事件处理器（注册 handler + 路由补丁）
└── tool_bridge.py              # 工具同步/调用/结果处理的核心逻辑
```
