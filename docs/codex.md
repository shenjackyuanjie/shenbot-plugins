# Codex 订阅查询

插件：[codex.py](../plugins/codex.py)，版本 **0.6.0**。

在白名单群中发送完整命令 `/codex check`，查询 **DSH 当前选中的 Codex 账号**：

- 套餐档位，例如 Plus、Pro；
- 5 小时、每周的剩余与已用百分比；窗口按接口实际时长识别；
- 额度重置时间及剩余时长，例如 `重置：2026-10-30 01:10:39（还剩 6 天 0 小时 5 分钟）`；
- 查询时间固定为 UTC+8；倒计时基于同一次查询时间计算，精确到分钟；不足一分钟和重置时间已到时分别提示，未知时间不推算；
- 接口返回时展示额外 Credits。

在同一白名单群发送 `/codex` 或 `/codex help` 可查看命令、自动提醒和 reset 警告规则。帮助直接在本地生成，
不读取登录文件、不请求额度接口、不占用查询槽位或冷却，正在查询或限流期间也可查看。

私聊、其他群、Bot 自己的消息不响应查询或帮助。白名单群内的成员均可使用，不额外要求 Bot 管理员。

`/codex check` 始终查询 DSH 当前 Codex 订阅的额度与重置时间，与生图/文本后端的选择无关。
OpenAI API 的计费是另一条链路：费用和账单在 OpenAI 控制台核对，插件不查询 API 余额，
也不会把订阅额度当成 API 余额展示。

## 复用方式

插件每次只读 DSH_HOME 下已有的 `.credentials.yaml`，按
`records["codex-subscription/accounts"].payload.activeId` 选择当前账号，
复用内存中的 access token 向 DSH 本身使用的官方 `wham/usage` 接口发起 GET。

**不创建账号、不增加登录流程、不保存或刷新 Token、不写 DSH 配置、不启动模型会话。**
不读取 CC Switch / Codex CLI 的另一套登录，也不会失败后静默切到别的账号。
账号文件读取兼容当前 DSH credentials v1、dsh-codex-subscription accounts vault v1
（本机验证的订阅插件为 3.1.4）；格式改变时提示更新，不猜测其他记录。

只投影套餐、额度和时间，不向群或日志发送邮箱、账号标识、认证头、原始响应和错误正文。
HTTP 请求拒绝重定向，避免向其他服务转发认证。支持系统和环境中的 HTTPS 代理。

## 生图与文本后端：订阅 / OpenAI API

生图（`/codex image`）与文本外审/润色的后端由管理员配置，两条链路可以分别选择：

| 配置 | 订阅模式（默认） | OpenAI API 模式 |
| --- | --- | --- |
| `image_backend` | `codex`：Codex 订阅图片接口，消耗订阅额度 | `openai`：api.openai.com 图片接口，按 API 计费 |
| `text_backend` | `deepseek`：DeepSeek，前审/润色/后审 | `openai`：gpt-6-luna，reasoning.effort=high |

- 两把 Key 严格分用途：`openai_text_api_key` 只用于外审与润色，`openai_image_api_key` 只用于生图，互不借用。
  它们直接写在本地 `config/codex.toml`（`config*` 已被 gitignore），插件加载时读取；
  Key 不进入日志、异常文本、PG 存档或回执。
- 本机直连 api.openai.com 会超时，API 模式需要 `openai_proxy`（例如 `http://127.0.0.1:7897`）；
  留空则不使用代理。订阅额度查询链路不受该代理配置影响。
- 切换后端只影响新任务：任务创建时把后端、模型、端点与推理强度冻结进 `jobs.execution_config`，
  确认和手动重试都按冻结值执行，重载配置不会让已建任务改走另一条计费通道。
- 不做自动重试、自动换模型或自动换后端；上游 401/403/404/429 只停止并按中文错误说明给手动操作建议。
  本地审核拒绝的任务在任何模式下都不能被普通用户强制重试。
- API 模式的参数范围更窄：质量只接受 auto/low/medium/high，尺寸宽高需能被 16 整除，创建或调整时即拒绝。

## 配置

先在 **Bot 所用 Python 环境** 安装新增依赖 PyYAML，或更新完整依赖：

```powershell
python -m pip install -r requirements.txt
```

加载插件后，宿主在当前插件配置目录生成 `codex.toml`。配置示例：

```toml
[main]
# 命令白名单：使用正群号；ICA 的负 room_id 也接受并归一化。空列表不响应任何群。
allowed_groups = [123456789, 987654321]
# 额度自动提醒只发这些群，支持多个；留空则沿用 allowed_groups。
alert_groups = [123456789]
# 已登录 DSH 的数据根目录，不是 profiles/web，也不是插件安装目录。
# 留空依次使用 Bot 进程的 DSH_HOME 环境变量、当前用户 ~/.dsh。
dsh_home = "D:/path/to/existing-dsh-home"
timeout_seconds = 15
cooldown_seconds = 15

# 生图 / 文本后端：codex|openai、deepseek|openai。
image_backend = "codex"
text_backend = "deepseek"
codex_image_model = "gpt-image-2.5-flare"
# API 模式专用：两把用途独立的 Key 直接写在本地配置里，不要提交。
openai_text_api_key = "sk-...（外审与润色）"
openai_image_api_key = "sk-...（生图）"
openai_base_url = "https://api.openai.com/v1"
openai_text_model = "gpt-6-luna"
openai_image_model = "gpt-image-2.5-flare"
openai_reasoning_effort = "high"
openai_proxy = "http://127.0.0.1:7897"
# 可选：组织级已花费查询（需要 Admin key + api.usage.read）；留空则 check 不发额外请求。
openai_usage_api_key = "sk-admin-..."
```

修改后通过宿主重载 `codex` 插件。真实群号与本机路径只放本地、Git 忽略的配置，
不要硬编码进插件或公共文档。`allowed_groups` 支持多个群，每群分别记录提醒进度，
某个群发送失败不会让其他群重复收到已受理的提醒。`alert_groups` 把自动提醒限制到指定群：
白名单里其他群仍可手动 `/codex check`，但不会收到每两分钟的额度变动提醒。Bot 运行用户需要有权限读取同一份 DSH 登录文件。

配置范围：超时 1–60 秒；查询冷却 1–3600 秒；默认均为 15 秒。
并发命令共用一个查询槽位，避免同时请求；收到 HTTP 429 时遵守 Retry-After。
不缓存额度，冷却结束后的命令重新读取当前账号和实时额度。

## OpenAI API 用量（可选）

`openai_usage_api_key` 为空时，`/codex check` 保持原样：只查 Codex 订阅额度，不发任何额外请求、不显示 OpenAI 段。

填入组织 Admin key（需要 `api.usage.read`）后，订阅额度下方会追加一段组织级已花费：

~~~text
OpenAI API 用量（最近 7 天，已花费估算，不是余额）
合计：$0.12
11-01：$0.05
10-31：$0.07
余额与账单请到 OpenAI 控制台查看
~~~

- 数据来自 `GET /v1/organization/costs`（`bucket_width=1d`，最近 7 天），汇总金额并按天列出最近两天，始终标注「已花费（估算）」。
- **没有任何接口能返回剩余余额**：`dashboard/billing/credit_grants` 只接受浏览器 session key，secret key（包括 Admin key）一律拿不到余额。余额与充值只能看 OpenAI 控制台，插件不编造这个数字。
- 缺 `api.usage.read`（HTTP 403）时只显示一行提示：需要 Admin key（Settings → Organization → Admin keys），不会整段抛出 403 噪音。
- 其他失败（网络、代理、401、5xx）不影响订阅额度展示，只在用量段写简短原因。
- 用量结果缓存 5 分钟并沿用 check 冷却；重载插件会清空缓存。请求走 `openai_proxy`，不重试、不跟随重定向。

## 每两分钟自动提醒与 reset 警告

配置了白名单群后自动启用，不需要额外命令或订阅渠道：

- 加载后尽快进行首次查询，只记录当前额度基准，不发送启动提醒。
- 之后每 **120 秒（2 分钟）**检查一次，分别跟踪接口实际返回的 5 小时、每周等额度窗口。
- 剩余额度向下跨过 **95%、90%、85%……5%、0%** 时，只向白名单群通知。
  例如 96% → 94% 提醒跨过 95%；94% → 91% 不提醒；91% → 89% 提醒跨过 90%。
- 同一账号、同一窗口的**剩余额度大于上次有效记录**时，报告 `⚠️ 每周额度 reset：剩余 23% → 100%`。
  任何上涨都报告，不要求涨到 100%，也不要求接口重置时间变化；余额相同不重复提醒。
- 上次剩余额度不是 0% 时，附上 `浪费了23%额度😭`，百分比使用**上一次实际余额**，不是上涨幅度或本周期最低值。
  例如 0% → 100% 只报告 reset，不显示浪费提示。
- 一次检查跨过多个档位、多个窗口变化，或同时发生下降与 reset，合并成一条消息，附当前套餐、额度和重置时间。
- 实际余额与本周期最低余额分别记录；有重置时间时，同周期轻微回升会报告 reset，但再次下降不会重复提醒已报告档位。
- 接口重置时间变化时重新建立下降提醒基准；缺少重置时间时以额度回升识别新周期。
  首次查询、新出现的窗口、DSH 切换账号静默建立基准，不跨账号比较余额或计算浪费。
- 查询失败或窗口未返回时保留基准，不当作 0%；恢复成功后可发现期间跨过的档位。
- 手动 `/codex check` 和轮询共用查询槽位、超时与限流保护，手动查询不会清除提醒基准。

调度复用宿主 `Scheduler`；发送复用 `watch_ica_connection()` 的当前连接和插件代检查，
不依赖群里先有人说话，不缓存旧 client。QQ 连接不可用时跳过本轮，下轮重新取得连接。
插件卸载会取消宿主定时任务并关闭正在使用的连接订阅，旧插件代不再发送。
此功能需要宿主提供 `Scheduler` 和 `watch_ica_connection()`。

提醒基准仅保存在内存，Bot 或插件重启后首次检查重新建立基准，不补发重启前的历史档位。
多群各自记录通知进度；明确未受理的发送留待下轮检查，发送结果不确定时不盲目重发。
自动轮询错误仅在日志记录脱敏类别，不每两分钟向群刷错误；`/codex check` 可查看具体故障提示。

## DSH 重启、离线及失败恢复

| 情况 | 行为与下一步 |
| --- | --- |
| DSH 暂未运行或正在重启，但现有登录态有效 | 直接查询官方额度接口，不依赖 DSH 的端口、进程、启动 Token 或 Web Cookie |
| DSH 已刷新登录或切换账号 | 下次查询重新读取同一登录文件，无需重启 Bot |
| 登录过期或上游返回 401 | 提示启动 DSH，在设置 → Codex 订阅刷新额度或重新登录；插件不自行使用 refresh token |
| 登录文件不存在、权限不足或格式变化 | 返回对应提示，不尝试其他账号或订阅渠道 |
| 网络失败或超时 | 返回网络/代理提示；槽位释放，冷却后可重试，不把上次额度冒充实时结果 |
| HTTP 403 | 提示检查网络、代理、账号权限，不直接断言登录过期 |
| HTTP 429 | 按 Retry-After 延后查询，缺少有效时间时等待 60 秒 |
| 插件卸载或重载时仍在查询 | 有界的一次性请求可以完成，但旧插件代不再回发结果 |

DSH 恢复后不需要重新配置插件；只要它仍使用原来的数据目录，后续命令就会读取更新后的登录态。
移动 DSH_HOME 时同步修改插件配置。

## 信息边界与验证

套餐档位不等于订阅到期日或续费日，接口未返回的字段不推算。额度百分比也不是绝对剩余 token 数。
窗口缺失显示“未返回”，不显示成 0。

离线回归（不访问真实登录文件、不调用远端接口、不发送 QQ 消息）：

```powershell
uv run --no-project --no-cache --python 3.12 --with pyyaml -m unittest discover -s tests -p "test_codex*.py" -v
ruff check plugins/codex.py tests/test_codex.py tests/test_codex_poll.py
```

仅保留关键业务回归：当前账号选择与只读复用、过期登录、实际额度摘要及倒计时、群白名单、
本地帮助及权限、单查询失败恢复、上游限流、跨档去重、上涨 reset 与未用额度、重置/切账号、
轮询失败保留基准、多群独立发送进度，以及两分钟调度与旧插件代停止。
不为每种时间单位、提示文案、HTTP 错误码或模拟线程故障分别堆测试。
