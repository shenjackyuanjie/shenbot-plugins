# Socket.IO + oicq 插件 API 2.0.6

本文与 [ICA 类型声明](../ica_typing.py)、[PluginHost 声明](../shenbot_api.py) 及
[管理员查询插件](../plugins/bridge_query.py) 一起维护。运行时对象由 Bot 的 Rust/PyO3
绑定提供，插件只在 `TYPE_CHECKING` 分支导入类型声明。

## 范围和结果语义

本轮对齐以 Icalingua++ 独立 **Socket.IO + oicq Bridge** 的事件注册、类型和实际实现为准，
当前参考 Bridge 的 `protocolVersion` 为 `2.26.7`，本指南对应 Bot **0.9.4** / ICA API **2.0.6**。
Bot 发布版本、Python ICA API、Bridge 服务版本和 `protocolVersion` 分别管理；版本变更见
[更新日志](../CHANGELOG.md)。本文不扩展 Milky、OneBot 或 Tailchat 的兼容行为，
也不将 native 的窗口、截图、录音、拖拽、消息菜单等桌面 UI 搬到插件宿主。

| 结果 | 含义 |
|---|---|
| `emit_event`、`send_message`、`send_raw_message`、`send_file`、`send_merged_forward` 返回 `True` | 本地发送/传输请求被受理，不是 QQ 最终执行成功，也不是消息 ID 回执 |
| `request_event` 返回 JSON | 收到对应 Socket.IO ACK；仍需按该事件的真实结果结构判断业务成功 |
| 查询方法返回 `list[IcaNewMessage]` | 可以直接供插件分析，不会将历史逐条重放到 `on_ica_message` |
| `upload_file` 返回 hash | 临时文件字节已按分片协议上传，可作为后续发送的资源引用 |
| `upload_group_file` 返回 `dict` | 成功 ACK 保留本体结果；明确失败 ACK 抛出 `RuntimeError` |

查询/ACK 超时及已知不可达事件会抛出 `RuntimeError`。参数校验错误与传输失败不是空查询结果；
插件应分别处理错误和 `[]`。已知 `{"error": ...}`、`{"ok": false}`、`"error"` 等失败形状
转换为显式异常，成功结果保留本体结构。Bridge 部分查询也会用空数组、空串或普通错误消息
表示业务失败；无法确定其含义时不补造状态，插件仍需按各事件合同判断。

## 管理员显式查询示例

[bridge_query.py](../plugins/bridge_query.py) 是实际启用插件，不是仅供复制的类型示例：

| 命令 | 行为 |
|---|---|
| `/bot-history [N]` | 查询当前房间本地消息首屏，展示本页末尾 N 条，默认 5，允许 1–20 |
| `/bot-search <关键词>` | 查询当前房间本地消息，关键词 1–200 字符，展示本页前 5 条 |
| 回复消息后 `/bot-media` | 读取回复快照的 `files`/兼容单 `file`，展示附件类别数量与正文摘要 |

只有 `client.status.admins` 内的账号可以使用，Bot 自身消息会被忽略。每次明确命令最多发送
一条回复摘要；昵称、正文限长，协议标记转成纯文本，媒体仅统计类别，不下载、回发附件或展示
临时 URL/fid。插件没有定时任务，不扫描其他房间，不自动摘要新消息，也不回放历史到消息 hook。
旧 Bot 仍可加载该插件；用户明确调用缺失的新 API 时收到升级提示。

时间按 UTC 展示；这是毫秒 `time` 字段的格式化，不用原消息时间做发送时间。
`fetch_messages(room_id)` 首屏在 Bridge 内部会更新该房间的未读/提及状态，并可能上报最近一条
QQ 已读，所以 `/bot-history` 不是“完全没有副作用的数据库只读”。`/bot-search` 仅查询本地存储。

## 接收消息结构

`IcaNewMessage` 保留旧 `id/content/sender_id/sender_name/room_id/is_reply/reply_msg_*` 等接口，
已有插件可继续使用。新增结构字段：

| 字段/方法 | 类型与用途 |
|---|---|
| `files` | `list[dict]`，附件的 `type/url/fid/size` 及其他 Bridge 字段 |
| `file` | `dict | None`，可选单附件；多附件优先使用 `files` |
| `at` | `bool | Literal["all"] | None`；`True` 表示提及当前账号，`"all"` 表示全体成员，不是 QQ 号列表 |
| `time` | `int | None`，有原消息时间时规范化为毫秒，缺失时为 `None`；`0` 是有效时间 |
| `code` | JSON，常见为 `str | None`；不要假定所有 code 都是转发资源 ID |
| `reply` | `dict | None`，结构化 `replyMessage` 快照，可能包含附件 |
| `deleted/hide/reveal/markdown` | `bool`，消息状态或呈现标记 |
| `button_rows` | `list`，Bridge 按钮结构 |
| `recall_info` | `str | None`，撤回附加信息，保留本体结构 |
| `raw_data` / `to_dict()` | 完整规范化 Bridge 消息 `dict`，适合持久化/业务转换；不是原始协议链 |
| `raw_msg` | JSON 或 `None`，消息携带的原协议链；规范化消息可能没有该链 |
| `to_forward_node()` | `dict`，构造成本体 `FakeMessage`；优先原链，否则按可用正文/图片/语音 fid 重建 |

媒体 URL、cookies、原始消息及回复正文可能含敏感数据。根据任务需要消费相应字段，不将整个
`raw_data` 或 ACK 原样输出到日志/聊天。`to_forward_node()` 对普通附件仅保留说明，不冒造
可发送的协议资源；转换是降级重建，不承诺所有消息都能无损转发。

**发送消息的 `IcaSendMessage.at` 与接收 `IcaNewMessage.at` 不同。** 发送端是 mention 数组：
`[{"id": 123, "text": "昵称"}, {"id": "all"}]`；`id` 为整数或 `"all"`，可选 `text` 为字符串。

## 查询与缓存

以下是公开方法的参数顺序和默认值；timeout 单位为秒：

```python
get_loaded_messages(room_id, limit=200)
fetch_messages(room_id, options=None, timeout=15.0)
search_messages(room_id, keyword, offset=0, sender_id=None,
                start_time=None, end_time=None, timeout=15.0)
fetch_messages_by_sender(room_id, sender_id, offset=0,
                         snapshot_time=None, timeout=15.0)
fetch_messages_around(room_id, message_id, before=20, after=20, timeout=15.0)
fetch_image_messages(room_id, offset=0, end_time=None, timeout=15.0)
```

- `room_id > 0` 为私聊对方 QQ，`room_id < 0` 为负群号。跨房间 `search_messages(0, ...)`
  的结果使用各自消息的真实房间；管理员示例插件固定当前房间，不开放该模式。
- `get_loaded_messages` 仅读 Bot 已加载缓存，不新建请求。缓存不是数据库全量历史。
- `fetch_messages` 的 `options` 形如 `{"before": {"time": 毫秒, "id": "消息ID"}}`
  或 `{"after": {...}}`，不能同时设置两种方向。无游标首屏有上述未读副作用。
- 本体本地查询固定页大小：普通页、搜索、发送者页为 20，图片页为 30；没有统一 `total/hasMore`
  对象。使用实际返回数量和事件对应的 offset/游标继续查询，不把空页当超时。
- 搜索时间、发送者快照上界、图片 `end_time` 和游标时间均为毫秒。
- 查询只访问 Bridge 已存消息，不自动拉取 QQ 全部漫游历史。QQ `fetchHistory`/`fetch7DaysHistory`
  是其他无 ACK 事件，若使用通用发事件接口，需要单独消费其业务推送和本体限制。

## 构造新消息、原始节点与转发

```python
new_message(room_id, content="", reply=None)  # -> IcaSendMessage
send_raw_message(raw_msg, room_id)          # -> bool
get_forward_messages(res_id, file_name=None, timeout=15.0)
send_merged_forward(nodes, target, dm=None, origin=None)  # -> bool
```

- `new_message` 可以把查询结果的 `to_dict()` 作为 `reply`；`room_id=0` 拒绝发送。
  构造消息不会自动发送，仍需明确调用 `send_message`。
- `send_raw_message` 接受**含 type 的原始消息节点 JSON 数组字符串**；非法 JSON、单对象或其他
  非节点数组抛 `ValueError`。API 2.0.6 按本体要求发送 JSON 字符串 `content` 和 `at=[]`。
  它是 `sendMessage(messageType="raw")`，不是任意 Socket.IO event 入口。
- `get_forward_messages` 返回转发资源内部消息，`room_id=0`，不归入当前房间历史；如果需要
  发送到真实会话，显式指定目标，不能从资源 ID 推断房间。
- `send_merged_forward` 的 `nodes` 是 **FakeMessage 数组**，不是规范化 Message 数组。
  推荐使用 `[record.to_forward_node() for record in records]`；`origin` 为整数或 `None`。
- FakeMessage 使用 `user_id/message/nickname/time/id/consistent/bubble_id` 等字段，其中
  `message` 是原始节点列表，**time 是 Unix 秒**，与接收/查询 Message 的毫秒时间不同。

在受管理的显式命令分支中，可以分析查询结果后构造回复；不要在加载插件时自动执行下述操作：

```python
records = client.search_messages(msg.room_id, keyword, timeout=15.0)
if records:
    reply = client.new_message(msg.room_id, "已找到对应的本地记录。", reply=records[0].to_dict())
    accepted = client.send_message(reply)  # accepted 只代表传输受理
```

## 业务目录、文件与专用角色

```python
get_system_messages(timeout=15.0)  # -> 按 flag 索引的 dict
get_friends(timeout=15.0)          # -> list[dict]
get_groups(timeout=15.0)           # -> list[dict]
upload_file(file_name, data)      # data: bytes；返回临时文件 hash
send_file(room_id, file_name, data, content="", mime="application/octet-stream",
          reply=None, at=None)    # -> bool，只表示受理
upload_group_file(group_id, file_name, data, parent="/")  # -> {ok, error?}
file_manager_request(group_id, event, args=None, timeout=15.0)  # -> JSON
```

本地文件由调用者显式读取为 `bytes`；API 不根据接收消息的 URL 自动下载文件。文件上传使用
SHA-256、`requestUpload` 和缺片续传 `uploadFile`；普通消息引用已上传 hash，群文件上传调用
`uploadGroupFile`。临时资源被后续发送/上传消费后可能清理，失败重试不能假定旧 hash 仍可用。

`send_file` 保留 `content/reply/at` 参数，但本体的普通 hash 附件分支会提前返回：
**`reply` 和 `at` 不会加入 QQ 文件链**，`content` 仅可能用于私聊回显描述，不保证随文件
发送正文。因此不能把这些参数理解为“文件同时附带文字、回复或 @”；需要额外文本时，
应由用户显式操作触发独立消息，并分别判断其传输受理结果。

`upload_group_file` 和 `file_manager_request` 的 `group_id` 均为**正群号**，不是负 room_id。
`file_manager_request` 会申请群 token 并建立 `role="fileMgr"` 专用连接，它不是在主连接随便
`request_event("ls", ...)`。本体实际文件管理参数为：

| event | args |
|---|---|
| `ls` | `[目录FID, 起始偏移]`，根目录为 `"/"` |
| `download` / `stat` / `rm` | `[FID]` |
| `mkdir` | `[名字]`，本体此事件没有 parent 参数 |
| `mv` | `[FID, 目标目录FID]` |
| `rename` | `[FID, 新名字]` |

`ls` 返回条目数组；下载返回含 URL 的对象；其他操作按各自真实 ACK 判断 `error`。
示例查询根目录：`client.file_manager_request(group_id, "ls", ["/", 0])`。

## 通用事件及已知 Bridge 边界

```python
emit_event(event, args=None)                    # -> bool
request_event(event, args=None, timeout=15.0)    # -> JSON
```

`args` 是**位置参数列表**，不是一个请求对象；可选参数要保留占位：
`client.request_event("searchMessages", [room_id, keyword, 0, None, None, None])`。
不要把回调当作 JSON 参数传入，宿主负责 ACK。

账号/群管理可复用已有连接发送本体已注册事件，而不需要建立第二个主连接。例如：

| 操作 | 调用形状与结果 |
|---|---|
| 查询登录设备 | `request_event("getLoginDevices")` |
| 查询单个成员资料 | `request_event("getGroupMemberInfo", [正群号, QQ, False])` |
| 重登 | `emit_event("reLogin")` |
| 提交短信码 | `emit_event("submitSmsCode", ["验证码"])`；`"sendSmsCode"` 表示请求发送短信 |
| 滑块 ticket | `emit_event("login-slider-ticket", ["ticket"])` |
| 修改好友备注 | `emit_event("setFriendRemark", [QQ, "备注"])` |
| 修改本人群名片 | `emit_event("setGroupNick", [正群号, "名片"])` |
| 踢出成员 | `emit_event("setGroupKick", [正群号, QQ])`；QQ 权限和执行结果仍需后续确认 |

这些只是调用形状，不会由示例插件自动执行。登录挑战资料可通过 Bridge 事件 hook 消费，
实际账号密码、ticket、Cookie 等只在用户明确操作时提供，不写入示例或日志。

当前独立 oicq Bridge 已确认的边界仍然有效：

1. 主 Socket 未注册 `reportRead`、`stopFetchingHistory`，`getPrivateFileUrl` 虽注册但 oicq
   实现抛未实现。新通用 API 对这些已知不可达点提前报 `RuntimeError`。旧 `report_read`
   为保持兼容仍只发事件，不能据其返回判断 QQ 已读成功。首屏 `fetchMessages` 的内部读回执
   是另一条仍存在的路径。
2. `sendMessage`、`makeForward`、审批、禁言/踢人/退群、备注、在线状态、设备删除、登录等
   多数修改事件没有 wire ACK。`request_event` 拒绝对已知无 ACK 操作伪造结果，改用
   `emit_event` 并消费提示/状态推送。对未知扩展事件保留请求能力，timeout 为真实等待上限。
3. `messageSuccess/messageError/notifyError/addMessageText` 等是广播推送，没有统一请求 ID；
   不能用最近一次提示认定某次文件或群管理操作成功。多客户端也可能收到同一操作提示。
4. Bridge 对部分 `hide/reveal/update` 和元数据修改只写存储，不立即广播；Bot 新接口不会
   补造远端多客户端一致性。消息缓存、房间未读与 QQ 读回执是不同状态。
5. 部分服务端历史拉取、设备错误回调和媒体 URL 处理仍有已知实现限制；API 对齐不等于
   修复 Bridge 服务端。群公告仍是额外 Cookie + QQ Web CGI 业务，不是新增公告 Socket 事件，
   本轮插件 API 不提供 native 群公告 UI 或 HTTP 业务封装。

## 更新与 Bridge 推送 hook

原有消息、系统消息、申请和删除 hook 保持参数顺序。API 2.0.6 新增两个 **client 在前**的 hook：

```python
if TYPE_CHECKING:
    from ica_typing import IcaClient, JSON


def on_ica_message_updated(client: IcaClient, room_id: int, message_id: str, patch: dict) -> None:
    # patch 是增量字段，不是完整新消息。QQ 撤回通常含 deleted=True。
    if patch.get("deleted"):
        client.info("收到消息撤回状态更新")


def on_ica_bridge_event(client: IcaClient, event: str, args: list[JSON]) -> None:
    # 按事件名消费宿主转交的推送，不原样输出登录挑战/聊天 payload。
    if event == "messageError":
        client.warn("Bridge 上报了一条消息操作错误，请核对原请求")
```

`renewMessage` 不应当只写日志：撤回、媒体后处理等更新由 `on_ica_message_updated` 的 patch
交给插件；业务需要完整上下文时再读 `get_loaded_messages`。这不会自动重跑新消息处理规则。
Bridge 原事件形状由 `on_ica_bridge_event` 提供，适合登录运维、状态和操作提示，不替代请求
ACK，也不要求每个插件实现两个 hook。管理员查询插件刻意只注册普通新消息 hook。

## 离线验证

从仓库根运行：

```powershell
uv run --no-project --python 3.12 -m unittest discover -s tests -p test_bridge_query.py -v
uv run --no-project --python 3.12 -m unittest discover -s tests -p test_oicq_api_contract.py -v
uv run --no-project --python 3.12 -m unittest discover -s tests -v
uv run --no-project --python 3.12 --with ruff ruff check plugins/bridge_query.py tests/test_bridge_query.py tests/test_oicq_api_contract.py tests/test_plugin_contract.py
ruff check .
```

[查询插件 mock 测试](../tests/test_bridge_query.py) 使用合成消息/客户端，覆盖管理员授权、
当前房间限制、分页摘要、at/time 语义、媒体隐去资源、输入边界、旧宿主、异常和协议标记处理。
[仓库契约测试](../tests/test_plugin_contract.py) 同时检查插件 manifest、hook 与类型导入约定。
测试不读取运行配置、真实聊天数据，不连 Bridge，也不发送或重放历史消息。
