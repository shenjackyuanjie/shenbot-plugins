# 插件市场更新日志

宿主版本对应 [shenbot_api.py](shenbot_api.py) 的 `__version__`；ICA/Tailchat API 与各插件的 `VERSION` 分别管理。

## shenbot 0.9.5 / ICA API 2.0.6

- 宿主声明由 **0.9.4 → 0.9.5**，ICA API 仍为 **2.0.6**，Tailchat API 仍为 **2.0.0**；方法、hook 和插件版本保持不变。
- 同步 Bot 接收兼容说明：历史/引用文件可没有下载 URL，附件 `url` 规范化为空字符串，仍保留 `fid` 与大小；坏可选字段或引用不会连带丢失合法正文。
- `raw_data` 保留完整输入，结构化 getter 返回可用的规范化值；未知接收 @ 标记和无效原时间返回 `None`。关键消息身份、来源房间和更新补丁继续严格校验。
- 无效 Bridge 事件首次警告，同类重复事件每 30 秒最多一次聚合提示，不原样记录消息内容；同步版本契约与 API 指南。

## shenbot 0.9.4 / ICA API 2.0.6

### 版本与 API

- 宿主声明由 **0.9.3 → 0.9.4**，ICA API 由 **2.0.5 → 2.0.6**；旧 `_version_` 别名继续指向宿主版本，Tailchat API 保持 **2.0.0**。
- [ICA 类型声明](ica_typing.py) 同步 Bot 的结构化媒体、引用、时间、规范化 JSON 与协议节点链；区分接收 `at` 标记和发送 mention 数组，消息时间为毫秒，FakeMessage 时间为秒。
- 补齐真实返回消息的历史/关键词/发言人/上下文/图片查询、有界缓存访问、通用 emit/ACK、联系人/系统申请、转发、分片文件与专用 fileMgr 接口。
- 新增 client-first `on_ica_message_updated` 与 `on_ica_bridge_event` 声明和契约检查，旧消息、回复及撤回接口保持兼容。
- [API 指南](docs/oicq-plugin-api.md) 说明传输受理、真实 ACK 与 QQ 最终执行成功的区别，以及 `reportRead` 未注册、普通文件回复/@ 不进入 QQ 文件链等服务端边界。Bridge 协议仍为 **2.26.7**；JSON 字节数组上传需同步独立 Bridge 的 **4 MiB** 接收上限。

### 查询插件 0.1.0

- 首次提供 [bridge_query](plugins/bridge_query.py) **0.1.0**：管理员显式使用 `/bot-history [1..20]`、`/bot-search <关键词>`、回复消息 `/bot-media`。
- 只查询当前房间，每次输出一条限长纯文本摘要；不下载媒体、不展示资源 URL、不自动查询或重放历史消息。旧宿主缺少对应 API 时提示升级。

### 测试与依赖

- 增加查询插件与 API 契约离线测试；规范依赖名称解析，接受版本约束、extras、marker 和具名 URL。
- 既有 CQP 测试仅在测试侧 mock 外部 tswn 转换，保留单人/双人目标路由覆盖，不修改插件生产逻辑。
- **58 项离线测试通过**，新插件和专项测试 Ruff 检查通过。现有 requirements 已允许最新版，Python 3.12 最新依赖组合解析通过；未更改实际安装环境。
