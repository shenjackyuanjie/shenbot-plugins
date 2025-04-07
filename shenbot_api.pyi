from __future__ import annotations

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
