# shenbot 插件市场

本仓库存放 [shenbot](https://github.com/shenjackyuanjie/icalingua-bridge-bot) 的 Python 插件。

- [ica_typing.py](ica_typing.py)：ICA/Tailchat 消息、客户端和 hook 的类型声明。
- [shenbot_api.py](shenbot_api.py)：Rust 内置模块 `shenbot_api` 的类型声明兼测试占位实现。
- `plugins/*.py`：Bot 会发现并加载的插件。
- `plugins/*.py_disable`：明确停用的插件源码；不会被 Bot 扫描。
- `plugins/<name>/`：插件共用的辅助包或资源，不作为独立插件加载。

## shenbot 0.9.5 / Socket.IO + oicq API 2.0.6

宿主声明同步到 **0.9.5**，ICA API 为 **2.0.6**，Tailchat API 保持 **2.0.0**。
版本和插件变更见 [更新日志](CHANGELOG.md)。

本轮以 Icalingua++ 实际 **Socket.IO + oicq Bridge** 对齐 Bot 可消费的数据与操作 API，
不扩展 Milky/OneBot，也不复制 native 桌面 UI。Python API `2.0.6` 与 Bridge 的服务版本、
`protocolVersion` 分开；完整方法签名、消息结构、事件参数及边界见
[oicq 插件 API 指南](docs/oicq-plugin-api.md)。

- 接收消息新增 `files/file/at/time/code/reply/raw_data/raw_msg`、状态字段、`to_dict()` 和
  `to_forward_node()`；保留旧 `reply_msg_*` 等接口。接收 `at` 是 `bool|'all'|None`，
  与发送 `IcaSendMessage.at` 的 mention 数组不同；接收 `time` 为毫秒或 `None`。
- 历史、关键词/发送者搜索、上下文、图片查询返回 `list[IcaNewMessage]`，可直接分析。
  `get_loaded_messages` 仅读缓存，查询不会重放到新消息 hook。
- 通用 `emit_event/request_event`、目录/系统申请、合并转发与分片文件/专用 fileMgr 接口复用
  Bot 连接。发送 `bool` 仅表示传输受理，不表示 QQ 最终成功；仅对真实有 ACK 的事件等待结果。
- 本体主连接未接通 `reportRead/stopFetchingHistory`，oicq 的 `getPrivateFileUrl` 未实现。
  新通用 API 对这些已知不可达点提前报错；旧 `report_read` 保留兼容，不能当已读成功回执。
- [bridge_query.py](plugins/bridge_query.py) 是实际管理员命令插件：`/bot-history [1-20]`、
  `/bot-search <关键词>`、回复消息 `/bot-media`。只操作当前房间、输出限长纯文本摘要，
  不自动发送、不下载媒体、不逐条回放历史。历史首屏会触发本体未读/提及状态更新。

## 插件文件规范

每个启用插件必须：

1. 使用 `from __future__ import annotations`，避免类型标注在运行时导入 IDE 类型文件。
2. 只在 `if TYPE_CHECKING:` 中导入 `ica_typing`。
3. 定义字符串常量 `VERSION`。
4. 定义唯一的 `PLUGIN_MANIFEST = PluginManifest(...)`，并使用 `version=VERSION`。
5. 只暴露 Bot 支持的 hook，参数顺序与类型保持一致。

最小示例：

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from shenbot_api import PluginManifest

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage

VERSION = "0.1.0"

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="example",
    name="示例插件",
    version=VERSION,
    description="示例",
    authors=["author"],
)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    pass
```

### 支持的 hook

| Hook | 签名 |
|---|---|
| `on_load` | `() -> None` |
| `on_unload` | `() -> None` |
| `on_ica_message` | `(IcaNewMessage, IcaClient) -> None` |
| `on_ica_system_message` | `(IcaNewMessage, IcaClient) -> None` |
| `on_ica_delete_message` | `(IcaType.MessageId, IcaClient) -> None` |
| `on_ica_join_request` | `(IcaJoinRequest, IcaClient) -> None` |
| `on_ica_message_updated` | `(client: IcaClient, room_id: int, message_id: str, patch: dict) -> None` |
| `on_ica_bridge_event` | `(client: IcaClient, event: str, args: list[JSON]) -> None` |
| `on_tailchat_message` | `(TailchatReciveMessage, TailchatClient) -> None` |

2.0.6 新增的更新/Bridge 事件 hook **client 在前**，与旧消息 hook 参数顺序不同。
更新 hook 用于消费撤回、媒体后处理等 patch；Bridge 事件 hook 提供宿主转交的原事件参数。
按事件名处理并保留敏感 payload，不把广播操作提示当作单次请求 ACK；
[使用示例](docs/oicq-plugin-api.md#更新与-bridge-推送-hook)。

`on_reload`、`on_config` 和 `require_config` 不是当前 PluginHost hook。配置应通过
`PluginManifest.config` / `ConfigStorage` 声明，并在 `on_load` 中读取。

## PluginHost 生命周期约束

- Hook 表在插件加载或重载成功时形成快照；运行中重新绑定 `on_*` 函数不会立即生效。
- 插件不能在自己的 hook、`on_load` 或 `on_unload` 中重载、启停自身。
- `Scheduler.start()` 必须从受管理的 hook 或生命周期函数调用。Scheduler 会绑定当前插件代，卸载时由宿主取消。
- 普通 hook 允许并发执行；共享可变状态必须自行加锁或使用线程安全对象。
- 自建线程、HTTP 服务和子进程不受 PluginHost 自动管理。插件必须在 `on_unload` 中停止长期后台资源；无法立即取消的一次性线程至少要避免在旧插件代卸载后继续回发消息。
- 不要依赖插件模块的 `__name__`。每次加载使用独立内部模块名，`__file__` 仍指向实际插件文件。

## 验证

```powershell
uv run --no-project --python 3.12 -m unittest discover -s tests -v
ruff check .
```

仓库级契约测试会检查启用插件的 manifest、版本、hook 签名、类型导入和后台线程生命周期入口。
