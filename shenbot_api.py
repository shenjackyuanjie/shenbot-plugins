from __future__ import annotations

import datetime

from typing import Callable, Union

__version__: str = "0.9.0"
_version_: str = __version__

_ica_version_: str = "2.0.1"
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

    def __init__(self, **kwargs):
        ...

    def add_item(self, key: str, value: value_type, replace: bool = True) -> bool:
        ...

    def have_value(self, layer1: str, layer2: str | None = None) -> bool:
        ...

    def get_value(self, layer1: str, layer2: str | None = None) -> value_type:
        ...

    def get_default_toml(self) -> str:
        ...

    def get_current_toml(self) -> str:
        ...

    def read_toml_str(self, value: str) -> str:
        ...


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
        homepage: str | None = None
    ) -> None:
        ...

    def __str__(self) -> str:
        ...

    def config_str(self) -> str:
        ...


class Scheduler:
    def __init__(self, func: Callable, schdule_time: datetime.timedelta) -> None:
        """
        创建一个计划任务

        func: 要执行的函数
        schdule_time: 计划任务的等待时长

        added: bot-0.9.0
        """
        ...

    def start(self):
        """开始任务"""



class CommandHelper:
    """
    用来帮助注册+处理消息
    """

    def __init__(self):
        ...
