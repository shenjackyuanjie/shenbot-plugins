from __future__ import annotations

import datetime

from typing import Callable, Union

__version__: str = "0.9.6"
_version_: str = __version__

_ica_version_: str = "2.0.6"
_tailchat_version_: str = "2.0.0"


def python_plugin_path() -> str:
    """
    返回 Python 插件的目录
    added: 2.0.1
    """
    ...


def python_config_path() -> str:
    """
    返回 Python 插件配置的目录
    added: 2.0.1
    """
    ...


def debug(message: str) -> None:
    """
    向宿主日志输出调试信息，默认级别下不显示
    added: bot-0.9.7
    """
    ...


def info(message: str) -> None:
    """
    向宿主日志输出信息
    added: bot-0.9.7
    """
    ...


def warn(message: str) -> None:
    """
    向宿主日志输出警告信息
    added: bot-0.9.7
    """
    ...


class ConfigStorage:
    """
    ```python
    sub_config = ConfigStorage(some_thing=None)
    # xxx=xxx only
    sub_config.add(key='value')
    ```

    added: bot-0.9.0
    """

    value_type = Union[str, int, float, bool, list, dict]

    def __init__(self, **kwargs): ...

    def add_item(self, key: str, value: value_type, replace: bool = True) -> bool: ...

    def have_value(self, layer1: str, layer2: str | None = None) -> bool: ...

    def get_value(
        self, layer1: str, layer2: str | None = None
    ) -> value_type | None: ...

    def get_default_toml(self) -> str: ...

    def get_current_toml(self) -> str: ...

    def read_toml_str(self, value: str) -> None: ...


class PluginManifest:
    """
    用于写入基本信息

    ```python
    from shenbot_api import ConfigStorage

    GLOBAL_CONFIG = ConfigStorage(some_key='default_value')
    GLOBAL_CONFIG = ConfigStorage()
                        .add(somekey="default_value")

    def aaa():
        use_config = GLOBAL_CONFIG.get_value('some_key')
        print(f"Using config: {use_config}")

    ```
    added: bot-0.9.0
    """

    plugin_id: str
    name: str
    version: str
    description: str | None
    authors: list[str]
    homepage: str | None
    # config: dict[str, ConfigStorage]

    def __init__(
        self,
        plugin_id: str,
        name: str,
        version: str,
        description: str | None = None,
        config: dict[str, ConfigStorage] | None = None,
        authors: list[str] | None = None,
        homepage: str | None = None,
    ) -> None: ...

    def __str__(self) -> str: ...

    def config_str(self) -> str: ...

    def config(self, key: str) -> ConfigStorage | None: ...

    def config_unchecked(self, key: str) -> ConfigStorage: ...


class IcaConnectionState:
    """主连接和所属插件的最新快照，不携带聊天数据或可缓存的 client。"""

    @property
    def generation(self) -> int: ...

    @property
    def account(self) -> int | None: ...

    @property
    def ready(self) -> bool:
        """认证平台账号已知、QQ 在线且插件当前代已提交。"""
        ...

    @property
    def plugin_active(self) -> bool: ...


class IcaConnectionWatch:
    def wait_changed(self) -> IcaConnectionState | None:
        """首次立即返回快照，后续阻塞等变化并释放 GIL；关闭/旧代退役返回 None。

        同一订阅只允许一个等待者；宿主最多同时保留 64 个未关闭订阅。
        不使用 Scheduler 或 HTTP 轮询，不保证重放中间每一个状态。
        """
        ...

    def send_text(
        self,
        generation: int,
        account: int,
        room_id: int,
        text: str,
        timeout: float,
    ) -> bool:
        """在当前连接和插件代检查后发一次纯文本；timeout 为剩余预算，须在 (0, 5] 秒。

        等额度和传输均计入同一个期限，受 PluginHost 在途限额和卸载排空监管。
        true 仅表示 Socket.IO 受理，不返回 QQ 引用；异常按可能已外发处理，不盲重试。
        不重试、不自动切换到新连接/新账号，不记录正文或平台原始错误。
        """
        ...

    def close(self) -> None:
        """幂等关闭；唤醒等待者、取消该订阅的在途发送并释放订阅额度。"""
        ...


def watch_ica_connection() -> IcaConnectionWatch:
    """仅从受 PluginHost 管理的 hook/on_load/on_unload 创建，绑定插件代际。

    on_load 可在尚未连接时创建订阅；候选代提交后会被事件唤醒，无需先收聊天。
    使用方负责 on_unload 先 close，再 join 自己的线程，不留下脱管后台任务。
    """
    ...


class Scheduler:
    def __init__(
        self, func: Callable[[], None], schdule_time: datetime.timedelta
    ) -> None:
        """
        创建一个计划任务

        func: 要执行的函数
        schdule_time: 计划任务的等待时长

        added: bot-0.9.0
        """
        ...

    def start(self) -> None:
        """从受 PluginHost 管理的 hook 或生命周期函数启动任务。"""
        ...
