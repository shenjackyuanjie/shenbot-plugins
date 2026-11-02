# Codex 生图与持久存档

## 当前实施边界

群命令已接入：`/codex image` 完成审核、润色、预览确认、生成与原群回图，`direct` 跳过润色。
生图与文本审核有两条后端：Codex 订阅（默认）与 OpenAI 官方 API（独立计费），由管理员按部署选择。
订阅模式不启动 DSH、不改 profile、不创建 DSH 会话，也不调用公共计费接口；API 模式全部请求走
api.openai.com，外审/润色与生图各用一把专用 Key，不使用订阅额度。
额度查询与生图共用同一份白名单和插件配置；配置热重载只影响新任务。

## 连接与数据边界

本地 Codex 配置的 main 段增加 pg_connection；连接串由用户授权后从已有 DSH PG 配置一次性复制。
运行时只读取 Bot 自己的配置，不再读取 DSH profile。配置必须保持 Git 忽略；不回显连接串，
不提交凭据。驱动错误转换为固定提示，不把 DSN、SQL 参数、原始异常发到群里。

正式 schema 固定为 codex，初始化只创建本模块的表；发现同名 schema 有未知数据时拒绝接管。
迁移在事务和迁移锁内执行，不修改其他 schema，不创建扩展或提升数据库用户权限。
当前结构版本为 5：版本 1 补建 `jobs.image_params`，版本 2 给 `job_assets` 加 `position` 并把主键扩成 `(job_id, role, position)`；版本 3 给任务与请求增加 `generation_attempt`，生成唯一索引改为 `(job_id, generation_attempt)`；版本 4 增加 `jobs.execution_config`，冻结任务所用后端、模型、端点与推理强度。历史请求默认属于首次尝试（0），历史任务保持空快照并沿用旧后端，不借迁移套用当前全局配置；生图服务首次使用时在迁移锁内自动升级。
现有依赖已经包含 psycopg、Pillow，无需再引入 ORM 或独立数据库服务。

## 归档内容

| 表 | 内容 |
| --- | --- |
| schema_version | 本模块结构版本，重复初始化不覆盖数据 |
| jobs | 命令来源、账号/群/发起者、原始及润色提示词、确认摘要、生成参数（尺寸/质量/背景）、冻结执行配置、有效期和任务状态 |
| requests | 每次前审、润色、后审、生图及额度请求的阶段、模型、请求正文、响应、HTTP 状态、上游请求 ID、usage 和时间 |
| assets | 实际图片 bytea、SHA-256、MIME、大小与尺寸，按摘要去重 |
| job_assets | 任务与参考图（可多张，按引用顺序）、生成原图、QQ 发送副本之间的关联 |
| events | 草稿就绪、确认、取消、回图及恢复等审计事件 |

参考图与原图都保存二进制，不只记录本地路径或临时 CDN URL。备份 codex schema 时包含图片数据；
目前不自动删除或清理存档，也不复制群历史。认证头、API Key、Token、Cookie 和数据库连接信息不属于归档内容；
`execution_config` 只冻结后端、模型、端点与推理强度这类非秘密字段，密钥始终按用途从当前配置读取，不进入任务快照。

## 执行配置冻结

任务创建时把 `image_backend`、`image_model`、`image_base_url`、`text_backend`、`text_model`、
`text_base_url`、`text_reasoning_effort` 写入 `jobs.execution_config` 并与任务一起返回。
确认与手动重试都只读这份快照，配置热重载不会把已建任务从 API 计费切回订阅额度，或换掉审核/生图模型。
归档层在每次外发前核对请求的 provider、模型与端点必须与冻结值一致，重放同一来源也不会改写快照。
空快照表示历史任务，继续沿用 Codex 与 DeepSeek 路径，图片默认 Flare；不会批量回填或改写已有记录。
确认前显式 `set --model` 只更新图片模型；模型与质量等参数同一条 SQL 原子保存，
后端、端点和审核配置不变，确认后及 retry 均沿用最终确认的模型。
历史空快照选模型时只记录图片模型覆盖，不套用当前全局后端配置。
大图可能明显增加数据库及备份体积，后续按真实用量决定是否增加保留策略，不提前搭建对象存储。

## 审核和确认的不变量

- direct 只发一次前审；润色模式按前审、润色、后审执行，前后审相互独立。审核请求只提供 pass、nsfw 两个函数工具。
- DeepSeek 文本后端使用 Chat Completions 的 tool_choice=required，并关闭思考模式；OpenAI 文本后端使用
  Responses 的 function 工具（`strict`、`parallel_tool_calls=false`、`reasoning.effort` 取冻结值）。
  两种后端都要求恰好一个合法工具调用：多个调用、缺少调用、参数异常、截断或请求失败均不放行，
  不降级成 auto，也不从 `output_text` 或正文推断通过（Responses 的 function_call 参数是 JSON 字符串）。
- API 模式下本地审核被拒绝的任务仍然只能由宿主管理员手动强制重试，与订阅模式一致；生成本身被上游
  内容审核拒绝时同样只允许管理员强制重试，任何后端都不会自动换模型或换计费模式重发。
- 润色是中间的独立调用，不提供审核工具；实际执行的审核工具调用和归一化 decision 都进入 requests。
- direct 跳过润色与后审，只审核原始描述和当前参考图；通过后展示草稿，仍需手动确认才生图。
- 一条消息最多引用 5 张图片，全部按引用顺序存档并一起送审、一起作为改图参考；摘要列表同样按位置比较，顺序变了就不算同一份草稿。
- 润色模式需两条不同、同属当前任务且阶段分别为 pre_review/post_review 的 pass 记录才能冻结草稿；direct 只需前审 pass，存档层还会核对提示词未变且任务未跑过润色，不伪造后审记录。
- 摘要涵盖原始提示词、润色提示词和参考图片摘要；审核开始后不能替换参考图，冻结后不能改提示词。
- 确认原子校验账号、群、发起者、状态和十分钟有效期；同一平台消息重放仍对应原任务。
- 每次确认或显式重试最多建立一条 generate 请求。普通用户只能重试已确认且生成收到普通 HTTP 4xx/5xx 的失败任务；宿主管理员还可手动强制重试已确认、实际生成过的上游内容审核拒绝。结果未知时保留原记录，不能手动或自动重发。
- 手动重试原子递增 `generation_attempt`，保留原提示词、参数、图片及审核摘要；同一条重试消息只消费一次，即使再次失败后重放也不会生成。
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
/codex image retry <编号>      按原内容重试 HTTP 失败的生图；编号必填，会再次消耗额度
/codex image cancel <编号>     取消草稿或任务
/codex image status [编号]     查看任务状态；不带编号时列出最近 5 条记录
/codex image help              生图帮助
~~~

`/codex image status` 不带编号时先返回当前群唯一的待确认任务；没有待确认任务，
就列出最近 5 条任务的任务号、状态与创建时间，不用先记住编号。

`direct`、`set`、`attach`、`confirm`、`retry`、`cancel`、`status` 都可以省略 `image` 前缀，
`/codex confirm` 与 `/codex image confirm` 等价。

`confirm`、`cancel`、`status`、`set`、`attach` 都可以省略编号，此时使用本人在当前群
唯一待确认的任务；有多个时会回消息列出现有编号，不做猜测。

宿主配置的管理员（`IcaClient.status.admins`，不是 QQ 群管理员身份）可以用 `confirm` / `cancel` / `retry` 处理本群其他人的任务：
机器人账号与来源群仍然强制匹配，只是不再限制发起者，回执会标注“管理员代办”。
同一条 `/codex retry <编号>` 命令对宿主管理员还可强制重试上游审核拒绝的已确认生成任务，回执标注“管理员强制重试”；普通用户没有这个权限。

生成参数可写在描述前后，缺省一律为 auto：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| --size | auto 或 512–2048 的 宽x高 | 例如 1024x1536 |
| --quality | auto/low/medium/high/xhigh/max | 上游渲染质量档位 |
| --model | flare/sunburst | 本次任务的图片模型；省略时沿用配置或任务原值 |
| --max | 无取值 | 固定选择 sunburst + 质量 max，优先于同条命令的 model/quality，与顺序无关 |
| --background | auto/transparent/opaque | 需要透明背景时指定 |

两种后端的图片模型只允许 `gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`
及各自的 `-2026-09-08` 官方快照，均支持上表全部质量档位；其他模型直接拒绝，不保留旧模型兼容分支。
校验使用任务记录的模型，配置重载不会让旧任务换模型；确认前可显式 set 切换 Flare/Sunburst。
API 模式的尺寸宽高仍必须能被 16 整除；创建、调整时直接检查，确认阶段也在消费确认前复核参数。

官方定位：[Flare](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)
侧重快速的日常高质量生图；[Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)
是能力最强的生图与编辑模型，侧重编辑精度。质量缺省仍为 auto，不会自动升为 max。

参数在创建任务时写入 `jobs.image_params`，预览展示的就是确认后发给上游的值。
示例（`direct` 同样支持这些选项）：

```text
/codex image --model flare 一只纸鹤
/codex image --model sunburst 精细修改参考图中的背景
/codex image --max 一只纸鹤
/codex set <编号> --model flare
/codex set <编号> --max
```

确认前用 `/codex image set <编号> --quality high` 或 `--model`、`--max` 调整，只合并给出的键、不接受描述文本；
确认命令本身不带参数，一经确认便不能再改。重放同一来源不会改写已记录参数。

确认前还可以用 `/codex image attach <编号>` 追加参考图：单条消息最多 5 张、总量 16 MB 以内。
参考图参与审核，所以追加会作废已冻结的草稿并把任务退回审核状态；direct 重新前审一次，润色模式重新跑前审、润色和后审，再给新预览；
原任务跑过润色就继续润色，direct 任务保持直出。

预览会展示模式、提示词、参数与 8 位任务编号，10 分钟内有效。确认只生效一次，
且只有原群、原发起者、当前机器人账号三者同时匹配才可操作。引用多张图片或非图片会被拒绝，
不会静默退化成文生图。

## 失败后重试

生成收到 HTTP 错误时，回执按中文原因、HTTP 状态、错误码、错误类型、相关参数、请求 ID、上游原文和建议分行展示，不再直接拼接 JSON。原文最多 1500 字；不会展示认证头、请求体或图片数据。普通 HTTP 400、429、5xx 等失败会附手动重试命令：

~~~text
/codex retry <8 位任务编号>
~~~

这条命令就是对新一次生成的确认，会再次消耗额度，不需要另发 `confirm`。沿用原任务已确认的
提示词、尺寸、质量、背景和参考图顺序，不重新下载参考图、不重新润色或审核，也不更换模型。
已确认的失败任务不受草稿十分钟有效期限制；再次失败后可以发一条新的重试命令。

只允许已确认、生成请求已归档为失败且 HTTP 状态为 4xx/5xx 的任务；审核失败、未确认、
生成中、已成功、已取消，以及超时/断线/成功响应解析失败等结果不确定的任务不能重试，管理员也不例外。上游内容审核拒绝（`moderation_blocked`）仅允许宿主管理员手动强制重试，兼容旧版归档为普通 HTTP 失败的拒绝记录；普通用户仍被拒绝。
强制仅针对已通过本地审核（direct 单次前审、润色模式前后双审）、本人或管理员确认后实际发出过的生成请求，不跳过本地审核，不修改输入，也不关闭上游审核。再次被上游拒绝会再次停止，不自动重试。事件记录 `forced` 与实际操作人 `operator_id`。
HTTP 错误不保证换一次就成功；401/403 应先处理登录或权限，429 应先等待额度或限流恢复，
持续 400 则需检查生成参数或上游限制。程序不自动重试，不把重放旧命令当作新的生成授权。

### 错误映射依据

依据 [OpenAI 错误指南](https://developers.openai.com/api/docs/guides/error-codes)、[图片生成错误处理](https://developers.openai.com/api/docs/guides/image-generation#handling-blocked-requests-and-other-errors) 与 [官方 ResponseError 枚举](https://developers.openai.com/api/reference/resources/responses/methods/retrieve)，优先使用稳定的上游 code，不根据英文句子猜错误类别。仅将返回的错误翻译为说明，不套用其他 API 的自动恢复流程。

| 上游 code / 类型 | 中文原因与建议 |
| --- | --- |
| moderation_blocked | 上游内容审核未通过；普通用户修改输入后重新发起，宿主管理员可手动强制 retry |
| credit_balance_exhausted | 预付余额耗尽；先处理余额 |
| organization_spend_limit_exceeded / project_spend_limit_exceeded | 组织 / 项目支出上限；请账号管理员检查限制 |
| organization_usage_limit_exceeded | 组织用量上限；检查或申请提高限制 |
| slow_down | 请求速率增长过快；降低频率后手动操作 |
| server_is_overloaded | 模型暂时过载；稍后手动操作 |
| insufficient_quota / rate_limit_error | 分别提示余额/用量不足、频率限制；兼容旧版把 insufficient_quota 放在 code 的形式 |
| invalid_api_key / missing_scope / model_not_found / unsupported_country_region_territory | 登录凭据、Key 用途配对、模型/资源和地区访问条件；missing_scope 会提示检查两把 Key 是否用错通道 |
| invalid_prompt、invalid_image / invalid_image_format / invalid_base64_image 等图片错误 | 分别说明描述、格式、编码、地址、尺寸、色彩模式、文件大小/为空、下载或文件缺失等问题 |
| 其他 / 未知错误 | 按 type 或 HTTP 状态提供一般说明，同时保留未知码与上游原文 |

覆盖 HTTP 400、401、403、404、408、409、413、415、422、429、500、502、503、504 的常见说明，其他 5xx 使用服务端错误兜底。公共 API 的余额/组织/项目错误只在上游确实返回时解释，不能把订阅接口的 429 一律认作公共 API 欠费。

API 模式的 401/403/404/429 与订阅模式一样只停止并给出手动操作建议：程序不重发、不换 Key、不换模型、不切回订阅。
API 余额与账单在 OpenAI 控制台核对，插件不查询 API 余额；`/codex check` 仍然查询 Codex 订阅额度，
与生图后端选择无关。

当上游提供 moderation_details 时，显示公开类别 harassment / self-harm / sexual / violence 的中文名称，以及 moderation_stage=input/output/unknown。未知类别保留原名。旧订阅响应只有 message 中的 safety_violations=[...] 时只解析这个明确机器字段；没有字段就不猜。界面标为“上游标记”，不等同于程序确认用户内容有问题。

## 并发与排队

只有收费的生成步骤受限：同时最多 `image_concurrency`（默认 3）个任务在生成，
其余确认或手动重试进入同一队列；等待超过 `image_queue_limit`（默认 5）个时直接拒绝本次操作，
提示稍后再确认，草稿在有效期内仍可确认。

创建任务、审核、润色、调整参数都不占这两个名额，一个慢任务不会挡住别人下一条命令。
排队中的手动重试可以用 `/codex cancel <编号>` 取消，取消后即使取得名额也不再生成；停止或取消不会自动恢复。
插件卸载时排队的确认按取消处理，不再发起请求。

## 模型调用

| 阶段 | 订阅 + DeepSeek（默认） | OpenAI API（独立计费） |
| --- | --- | --- |
| 前置审核 | deepseek-flash，关闭思考，tool_choice=required | gpt-6-luna，reasoning.effort=high，Responses function 工具 |
| 润色 | deepseek-flash，reasoning_effort=high | gpt-6-luna，reasoning.effort=high，max_output_tokens=10000 |
| 后置审核 | deepseek-flash，关闭思考，tool_choice=required | gpt-6-luna，reasoning.effort=high，Responses function 工具 |
| 生图 | gpt-image-2.5-flare，订阅图片接口 | gpt-image-2.5-flare，/images/generations 或 /images/edits |

生图参数（尺寸/质量/背景）取任务冻结值，两种后端共用同一套命令与确认流程。
API 模式的 Responses 回包里只有 `function_call` 与 reasoning 项，不依赖 `output_text`；
文本 Key 不能调用图片接口，图片 Key 也不能调用 Responses，插件始终按用途取 Key，不互相借用。

审核关闭思考是使用 required 的前提；润色使用 high 思考是为了让提示词更完整。
direct 记录一次前审，润色模式分别记录前后审；任何一步失败、超时或裁决异常都停在生图之前。

## 配置

本地 `config/codex.toml` 的 `[main]` 增加：

~~~toml
# 从现有 DSH PostgreSQL 配置一次性复制；运行时不再读取 DSH profile。
pg_connection = "postgresql://...（本地保密，不提交）"
# 只写入本机配置；不要放进仓库、日志或聊天。
deepseek_api_key = "..."
deepseek_base_url = "https://api.deepseek.com"
deepseek_model = "deepseek-flash"

# 生图与文本后端：codex/deepseek 用订阅与 DeepSeek，openai 用官方 API（独立计费）。
image_backend = "openai"
text_backend = "openai"
codex_image_model = "gpt-image-2.5-flare"
openai_base_url = "https://api.openai.com/v1"
openai_text_model = "gpt-6-luna"
openai_image_model = "gpt-image-2.5-flare"
openai_reasoning_effort = "high"
# 两把用途独立的 Key 直接写在本地配置里（config* 已被 gitignore），插件加载时读取。
openai_text_api_key = "sk-...（外审与润色专用，不提交）"
openai_image_api_key = "sk-...（生图专用，不提交）"
# 本机直连 api.openai.com 会超时，按需配置 HTTP 代理；留空则不用代理。
openai_proxy = "http://127.0.0.1:7897"
~~~

Key 只进入请求头，不进入日志、异常文本、PG 存档、回执或测试快照。所选后端缺 Key 时插件加载即失败，
错误信息直接指出配置字段名（`openai_text_api_key` / `openai_image_api_key`），不会退化成运行时模糊报错。
缺少连接时，生图命令会明确提示不可用，其他额度查询功能不受影响。


普通测试不读取真实连接。显式执行 PG 业务验证时，在插件仓库中运行：

~~~powershell
$env:CODEX_TEST_PG_CONFIG = (Resolve-Path config/codex.toml).Path
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt -m unittest discover -s tests -p test_codex_archive.py -v
~~~

测试只读取该本地配置中的连接串，在随机 codex_test schema 使用虚构主体和内存构造的小图片。
覆盖 direct 单审输入绑定与润色双审闸门、跨群隔离、并发一次性确认、不确定结果不重新生成、重启后取回图片及认证字段剔除；
结束前核对 schema 名称，只删除本测试创建的 schema，不触碰正式 codex 或 DSH 的表。
