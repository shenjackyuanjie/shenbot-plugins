# shenbot 插件市场

本仓库存放 [shenbot](https://github.com/shenjackyuanjie/icalingua-bridge-bot) 的 Python 插件。

- `ica_typing.py`：ICA/Tailchat 消息、客户端和 hook 的类型声明。
- `shenbot_api.py`：Rust 内置模块 `shenbot_api` 的类型声明兼测试占位实现。
- `plugins/*.py`：Bot 会发现并加载的插件。
- `plugins/*.py_disable`：明确停用的插件源码；不会被 Bot 扫描。
- `plugins/<name>/`：插件共用的辅助包或资源，不作为独立插件加载。

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
| `on_tailchat_message` | `(TailchatReciveMessage, TailchatClient) -> None` |

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
