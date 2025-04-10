from __future__ import annotations

import datetime

from typing import Callable

class ConfigStorage:
    def __init__(self, **request_config):
        """
        通过 ConfigStorage 来请求配置信息

        GLOBAL_CONFIG = ConfigStorage(some_key='default_value')

        def aaa():
            use_config = GLOBAL_CONFIG.get('some_key')
            print(f"Using config: {use_config}")
        """
        ...


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
