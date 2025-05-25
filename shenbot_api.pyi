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

def python_config_path() -> str:
    """
    返回 Python 插件配置的目录
    added: 2.0.1
    """


class ConfigStorage:
    """
    如果你的配置项目多到了得要用子页面
    那就用我吧

    ```python
    sub_config = ConfigStorage(some_thing=None)
    # xxx=xxx only
    sub_config.add(key='value')
    ```

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


class PluginConfigs:
    """
    用于请求配置信息

    ```python
    from shenbot_api import ConfigStorage, ConfigTable

    table = ConfigTable(some_thing=None)
    GLOBAL_CONFIG = ConfigStorage(some_key='default_value')
    GLOBAL_CONFIG = ConfigStorage()
                        .add(somekey="default_value")

    def aaa():
        use_config = GLOBAL_CONFIG.get('some_key')
        print(f"Using config: {use_config}")

    ```
    ```python
    class Cfg(ConfigStorage):
        some_value: str = "default_value"
        some_other_value: int = 123

    GLOBAL_CONFIG = Cfg.default()
    ```

    """
    def __init__(self, **request_config):
        ...

    @classmethod
    def default(cls):
        return cls()

class Scheduler:
    def __init__(self, func: Callable, schdule_time: datetime.timedelta) -> None:
        """
        创建一个计划任务

        func: 要执行的函数
        schdule_time: 计划任务的等待时长

        added: 0.9.0
        """
        ...

    def start(self):
        """开始任务"""



class CommanderHelper:
    """
    用来帮助注册+处理消息
    """

    def __init__(self):
        ...
