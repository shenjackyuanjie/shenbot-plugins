# Codex 生图与持久存档

## 当前实施边界

这是直连生图的归档基础层，尚未接入群命令。现有额度查询仍按原实现运行；
本阶段不启动 DSH、不改 profile、不创建 DSH 会话、不请求真实模型或生成图片。
后续客户端及命令接线会复用此层，不能把建好表当成生图功能已上线。

## 连接与数据边界

本地 Codex 配置的 main 段增加 pg_connection；连接串由用户授权后从已有 DSH PG 配置一次性复制。
运行时只读取 Bot 自己的配置，不再读取 DSH profile。配置必须保持 Git 忽略；不回显连接串，
不提交凭据。驱动错误转换为固定提示，不把 DSN、SQL 参数、原始异常发到群里。

正式 schema 固定为 codex，初始化只创建本模块的表；发现同名 schema 有未知数据时拒绝接管。
迁移在事务和迁移锁内执行，不修改其他 schema，不创建扩展或提升数据库用户权限。
现有依赖已经包含 psycopg、Pillow，无需再引入 ORM 或独立数据库服务。

## 归档内容

| 表 | 内容 |
| --- | --- |
| schema_version | 本模块结构版本，重复初始化不覆盖数据 |
| jobs | 命令来源、账号/群/发起者、原始及润色提示词、确认摘要、有效期和任务状态 |
| requests | 每次前审、润色、后审、生图及额度请求的阶段、模型、请求正文、响应、HTTP 状态、上游请求 ID、usage 和时间 |
| assets | 实际图片 bytea、SHA-256、MIME、大小与尺寸，按摘要去重 |
| job_assets | 任务与参考图、生成原图、QQ 发送副本之间的关联 |
| events | 草稿就绪、确认、取消、回图及恢复等审计事件 |

参考图与原图都保存二进制，不只记录本地路径或临时 CDN URL。备份 codex schema 时包含图片数据；
目前不自动删除或清理存档，也不复制群历史。认证头、API Key、Token、Cookie 和数据库连接信息不属于归档内容。
大图可能明显增加数据库及备份体积，后续按真实用量决定是否增加保留策略，不提前搭建对象存储。

## 审核和确认的不变量

- 前审和后审是两个独立请求，各只提供 pass、nsfw 两个函数工具。
- 使用 Chat Completions 的 tool_choice=required，并关闭思考模式；程序仍要求恰好一个合法工具调用。
  多个调用、缺少调用、参数异常、截断或请求失败均不放行，不降级成 auto 或从正文推断通过。
- 润色是中间的独立调用，不提供审核工具；两次审核的实际工具调用和归一化 decision 都进入 requests。
- 两条不同、同属当前任务且阶段分别为 pre_review/post_review 的 pass 记录齐备后才能冻结草稿。
- 摘要涵盖原始提示词、润色提示词和参考图片摘要；审核开始后不能替换参考图，冻结后不能改提示词。
- 确认原子校验账号、群、发起者、状态和十分钟有效期；同一平台消息重放仍对应原任务。
- 每个任务最多建立一条 generate 请求。结果未知时保留原记录，不允许重启后自动再生成。
- 取消不代表上游未执行；迟到结果仍可归档，但不能把已取消任务恢复为可发送状态。

## 客户端接线要求

外部请求前必须先提交 started 记录；数据库不可用时不进入收费请求。
响应需先可靠保存再反馈给用户；后续直连层需要处理外部完成但数据库暂不可用的结果待补记，
恢复只补记或补投递已有结果，不自动重放模型生成。此条在归档模块之外实施，当前尚未接线。
HTTP/Socket.IO 受理仅记为 submitted，不能伪称 QQ 最终送达。

## 命令

~~~text
/codex image <描述>            文字生图；引用一张图片时按改图处理
/codex image confirm <编号>    本人确认后生成并回图
/codex image cancel <编号>     取消草稿或任务
/codex image status <编号>     查看任务状态
/codex image help              生图帮助
~~~

预览会展示润色后的提示词与 8 位任务编号，10 分钟内有效。确认只生效一次，
且只有原群、原发起者、当前机器人账号三者同时匹配才可操作。引用多张图片或非图片会被拒绝，
不会静默退化成文生图。

## 模型调用

| 阶段 | 模型 | 思考 | 工具 |
| --- | --- | --- | --- |
| 前置审核 | deepseek-flash | 关闭 | 仅 pass/nsfw，tool_choice=required |
| 润色 | deepseek-flash | 开启，reasoning_effort=high | 无 |
| 后置审核 | deepseek-flash | 关闭 | 仅 pass/nsfw，tool_choice=required |
| 生图 | gpt-image-2.5-flare | — | 订阅图片接口，quality=auto |

审核关闭思考是使用 required 的前提；润色使用 high 思考是为了让提示词更完整。
两次审核分别记录，任何一步失败、超时或裁决异常都停在生图之前。

## 配置

本地 `config/codex.toml` 的 `[main]` 增加：

~~~toml
# 从现有 DSH PostgreSQL 配置一次性复制；运行时不再读取 DSH profile。
pg_connection = "postgresql://...（本地保密，不提交）"
# 只写入本机配置；不要放进仓库、日志或聊天。
deepseek_api_key = "..."
deepseek_base_url = "https://api.deepseek.com"
deepseek_model = "deepseek-flash"
~~~

缺少密钥或连接时，生图命令会明确提示不可用，其他额度查询功能不受影响。


普通测试不读取真实连接。显式执行 PG 业务验证时，在插件仓库中运行：

~~~powershell
$env:CODEX_TEST_PG_CONFIG = (Resolve-Path config/codex.toml).Path
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt -m unittest discover -s tests -p test_codex_archive.py -v
~~~

测试只读取该本地配置中的连接串，在随机 codex_test schema 使用虚构主体和内存构造的小图片。
覆盖双审核闸门、跨群隔离、并发一次性确认、不确定结果不重新生成、重启后取回图片及认证字段剔除；
结束前核对 schema 名称，只删除本测试创建的 schema，不触碰正式 codex 或 DSH 的表。
