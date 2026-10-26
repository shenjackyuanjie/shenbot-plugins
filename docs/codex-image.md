# Codex 生图与持久存档

## 当前实施边界

群命令已接入：`/codex image` 完成审核、润色、预览确认、生成与原群回图，`direct` 跳过润色。
生图只走订阅图片后端，不启动 DSH、不改 profile、不创建 DSH 会话，也不调用公共计费接口。
额度查询与生图共用同一份白名单和插件配置。

## 连接与数据边界

本地 Codex 配置的 main 段增加 pg_connection；连接串由用户授权后从已有 DSH PG 配置一次性复制。
运行时只读取 Bot 自己的配置，不再读取 DSH profile。配置必须保持 Git 忽略；不回显连接串，
不提交凭据。驱动错误转换为固定提示，不把 DSN、SQL 参数、原始异常发到群里。

正式 schema 固定为 codex，初始化只创建本模块的表；发现同名 schema 有未知数据时拒绝接管。
迁移在事务和迁移锁内执行，不修改其他 schema，不创建扩展或提升数据库用户权限。
当前结构版本为 3：版本 1 补建 `jobs.image_params`，版本 2 给 `job_assets` 加 `position` 并把主键扩成 `(job_id, role, position)`；每次升级都不改动已有数据。
现有依赖已经包含 psycopg、Pillow，无需再引入 ORM 或独立数据库服务。

## 归档内容

| 表 | 内容 |
| --- | --- |
| schema_version | 本模块结构版本，重复初始化不覆盖数据 |
| jobs | 命令来源、账号/群/发起者、原始及润色提示词、确认摘要、生成参数（尺寸/质量/背景）、有效期和任务状态 |
| requests | 每次前审、润色、后审、生图及额度请求的阶段、模型、请求正文、响应、HTTP 状态、上游请求 ID、usage 和时间 |
| assets | 实际图片 bytea、SHA-256、MIME、大小与尺寸，按摘要去重 |
| job_assets | 任务与参考图（可多张，按引用顺序）、生成原图、QQ 发送副本之间的关联 |
| events | 草稿就绪、确认、取消、回图及恢复等审计事件 |

参考图与原图都保存二进制，不只记录本地路径或临时 CDN URL。备份 codex schema 时包含图片数据；
目前不自动删除或清理存档，也不复制群历史。认证头、API Key、Token、Cookie 和数据库连接信息不属于归档内容。
大图可能明显增加数据库及备份体积，后续按真实用量决定是否增加保留策略，不提前搭建对象存储。

## 审核和确认的不变量

- 前审和后审是两个独立请求，各只提供 pass、nsfw 两个函数工具。
- 使用 Chat Completions 的 tool_choice=required，并关闭思考模式；程序仍要求恰好一个合法工具调用。
  多个调用、缺少调用、参数异常、截断或请求失败均不放行，不降级成 auto 或从正文推断通过。
- 润色是中间的独立调用，不提供审核工具；两次审核的实际工具调用和归一化 decision 都进入 requests。
- direct 模式跳过润色，前审与后审仍各发一次独立请求，审核对象都是原始描述。
- 一条消息最多引用 5 张图片，全部按引用顺序存档并一起送审、一起作为改图参考；摘要列表同样按位置比较，顺序变了就不算同一份草稿。
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
/codex image <描述>            文字生图；引用图片（最多 5 张）时按改图处理
/codex image direct <描述>     跳过润色，直接按原描述生成
/codex image set <编号>        确认前调整尺寸、质量与背景
/codex image attach <编号>     引用含图片的消息，追加参考图并重新审核
/codex image confirm [编号]    确认后生成并回图；只剩一个待确认任务时可省略
/codex image cancel <编号>     取消草稿或任务
/codex image status <编号>     查看任务状态
/codex image help              生图帮助
~~~

`confirm`、`cancel`、`status`、`set`、`attach` 都可以省略编号，此时使用本人在当前群
唯一待确认的任务；有多个时会回消息列出现有编号，不做猜测。

宿主配置的管理员（`IcaClient.admins`）可以用 `confirm` / `cancel` 处理本群其他人的任务：
机器人账号与来源群仍然强制匹配，只是不再限制发起者，回执会标注“管理员代办”。

生成参数可写在描述前后，缺省一律为 auto：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| --size | auto 或 512–2048 的 宽x高 | 例如 1024x1536 |
| --quality | auto/low/medium/high/xhigh/max | 上游渲染质量档位 |
| --background | auto/transparent/opaque | 需要透明背景时指定 |

参数在创建任务时写入 `jobs.image_params`，预览展示的就是确认后发给上游的值。
确认前用 `/codex image set <编号> --quality high` 调整，只合并给出的键、不接受描述文本；
确认命令本身不带参数，一经确认便不能再改。重放同一来源不会改写已记录参数。

确认前还可以用 `/codex image attach <编号>` 追加参考图：单条消息最多 5 张、总量 16 MB 以内。
参考图参与审核，所以追加会作废已冻结的草稿并把任务退回审核状态，重新跑前审与后审后才给新预览；
原任务跑过润色就继续润色，direct 任务保持直出。

预览会展示模式、提示词、参数与 8 位任务编号，10 分钟内有效。确认只生效一次，
且只有原群、原发起者、当前机器人账号三者同时匹配才可操作。引用多张图片或非图片会被拒绝，
不会静默退化成文生图。

## 并发与排队

只有收费的生成步骤受限：同时最多 `image_concurrency`（默认 3）个任务在生成，
其余确认进入队列；等待超过 `image_queue_limit`（默认 5）个时直接拒绝确认，
提示稍后再确认，草稿在有效期内仍可确认。

创建任务、审核、润色、调整参数都不占这两个名额，一个慢任务不会挡住别人下一条命令。
插件卸载时排队的确认按取消处理，不再发起请求。

## 模型调用

| 阶段 | 模型 | 思考 | 工具 |
| --- | --- | --- | --- |
| 前置审核 | deepseek-flash | 关闭 | 仅 pass/nsfw，tool_choice=required |
| 润色 | deepseek-flash | 开启，reasoning_effort=high | 无 |
| 后置审核 | deepseek-flash | 关闭 | 仅 pass/nsfw，tool_choice=required |
| 生图 | gpt-image-2.5-flare | — | 订阅图片接口，尺寸/质量/背景取任务冻结值 |

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
