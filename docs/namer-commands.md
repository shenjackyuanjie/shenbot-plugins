# 名竞小工具（namer）命令说明

[namerena.py](../plugins/namerena.py)（`plugin_id = namer`）把 namerena 的名字评估、评分和
Openbox 的几套命令行工具包成 Bot 命令。本文说明命令、依赖与 `/namer-todiy` 的导出格式。

## 命令前缀与输入约定

- 命令统一用 `/namer` 前缀，子命令用连字符连接（`/namer-todiy`）。下划线写法（`/namer_team`）
  不匹配任何命令，也不会落进简化求值的 `/namer`，方便发现笔误。
- 除 `/namer-help` 以外，命令都要求「命令占一行，之后每个输入各占一行」。回复本插件自己发的消息、
  或自己发送的消息会被忽略。
- `/namer` 是 `/namerena` 的简化输入，只做最后兜底匹配。

```text
/namer-team
名字A@队伍
名字B@队伍
```

## 命令表

| 命令 | 作用 | 依赖 |
|---|---|---|
| `/namer-help` | 命令列表 | — |
| `/namerena`、`/namer` | 运行名字竞技场，输入格式与网页版相同 | JS 运行时 |
| `/namer-pp` `/namer-pd` `/namer-qp` `/namer-qd` | 四档评分 | JS 运行时 |
| `/namer-pf` | 一次性全评：四个分数 + 总分 | JS 运行时；`use_tswn_compare` 打开时额外调 `tswn-cli` 对照 |
| `/namer-fight` | 1v1 战斗，`AAA+BBB+seed:123@!` 指定种子 | JS 运行时；可选 `tswn-cli` 对照 |
| `/namer-peek`、`/namer-peeks` | 查看单个名字的八围与技能，`-peeks` 按原始槽位顺序 | — |
| `/namer-team`、`/namer-teams` | 同队加成后的八围与熟练度，`-teams` 按原始槽位顺序 | — |
| `/namer-base` | base 工具，一次只接受一个名字，避免刷屏 | — |
| `/namer-cqp` | 100% CQP，一行一组，双人组用 `+` 连接 | `tswn-cli` + Openbox 靶子资产 |
| `/namer-cp` `/namer-fp` `/namer-wcp` `/namer-fsp` `/namer-pjp` | Openbox 队友评测（刺评/辅评/无刺评/分身评/配件评） | `tswn-cli` + backend 资产 |
| `/namer-todiy` | 名字导出为 `+ol` 覆盖格式，对齐 Openbox 的 `to-diy` | `tswn-cli` |

「JS 运行时」指 `node` 或 `bun`：评分与战斗跑 `plugins/md5/md5-api.js`，用 `use_bun` 决定优先哪一个。
`-peek` / `-team` / `-base` 用插件内置的本地端口，不需要外部运行时。

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `use_bun` | `false` | 优先使用 bun，找不到时回落到 node |
| `telemetry` | `true` | 配置 PG 后启用结构化存档；关闭则不连接数据库 |
| `pg_connection` | `""` | 本地 PostgreSQL 连接串，目标数据库建议 `namer`；不要提交或粘贴到日志 |
| `telemetry_tailchat_account` | `""` | 严格存档的 Tailchat 来源归属，填写实际机器人 user ID；ICA 自动取当前连接的 self_id |
| `use_tswn_compare` | `true` | 评分/战斗时附带 `tswn-cli` 的对照结果 |
| `tswn_cli_path` | `""` | `tswn-cli` 的 exe、仓库根目录或 `crates/tswn_core` |
| `tswn_assets_path` | `""` | Openbox 资产目录（含 `targets/`、`teammates/` 的那一层），留空则自动查找 |

`tswn_cli_path` 留空时会依次尝试：插件目录下的 `name_utils/tswn-cli`、`PATH`、以及仓库相对位置的
`target/{debug,release}/tswn-cli`。找不到时 `/namer-cqp`、队友评和 `/namer-todiy` 会报错，
其余命令不受影响。

## /namer-todiy

把名字导出成 namerena 的 `+ol:{...}` 覆盖格式，与 Openbox 的 `to-diy` 面板同一套实现：

```text
/namer-todiy
1@a+2@a
```

```text
1@a+ol:{"attrs":[85,79,67,68,68,71,73,192],"skills":{"sklthunder":5,"skldefend":7,"sklheal":12,"sklhide":6,"sklassassinate":"2*24"},"name_factor_enabled":true}++2@a+ol:{"attrs":[75,90,69,72,74,59,55,298],"skills":{"sklprotect":9,"sklcurse":12,"sklsummon":"2*11","sklzombie":5,"sklreflect":5},"name_factor_enabled":true}
```

输出一行对应输入一行，每行是 `名字@队伍+ol:{"attrs":…,"skills":…,"name_factor_enabled":…}`。

### 组内分隔符是 `++`

同一行的多个号之间用 `++` 连接，不是单 `+`。两个原因：

- `+ol:{...}` 的技能强化值本身就带 `+`（`"17+5"`、`"2*24"`），单 `+` 无法区分「分隔符」和「技能值」。
- 没有 `@队伍` 的两个号用单 `+` 拼起来会被 tswn 当成「一个号 + 武器」：`mario+ol:{...}+luigi+ol:{...}`
  回读后只剩一个号，`++` 才能原样回到两个号。

同理，**输入**侧也遵循这条规则：两边都有 `@队伍` 时 `+` 可以正常分组，
`mario+luigi` 这种无队伍的组必须写成 `mario++luigi`，否则只导出一个号。

### 组队加成已经烘进导出

一行就是一个组。组内 `@队伍` 相同的号会先走 namerena 的同队升级，再把结果冻结进 `+ol`，
所以导出的八围/技能是这组号的构建结果（含组内加成），数字与 `/namer-team` 的端口计算逐项一致：

| 名字 | 单独导出 | 与 `2@a` 同行导出 |
|---|---|---|
| `1@a` 攻击 | 71 | **85** |
| `2@a` 伤害反弹 | 0（技能未出现） | **5** |

同一对号如果分成两行输入，就不再是一组，不会互相加成。

注意 `+ol` 里的八围是**名字系数生效前**的构建值，与战斗里显示的属性差一个 `name_factor` 缩放；
导出的 `name_factor_enabled` 记录该号是否参与这一步缩放。

### 回读与错误

- 导出结果可以直接粘回输入再跑一次，得到逐字节相同的两个号（`++` 保住了组结构）。
- 转换走 `tswn-cli to-diy -f <输入> -o <输出>` 的文件模式：`to-diy` 的标准输出带 CLI banner，
  只有文件内容是纯转换结果。
- 常见报错：未找到 `tswn-cli`（提示配置 `tswn_cli_path`）、转换失败（附 tswn 的 stderr 尾部）、
  结果行数与输入行数不一致。

## 验证

```powershell
uv run --no-project --python 3.12 -m unittest discover -s tests -v
ruff check .
```

- [test_namer_team.py](../tests/test_namer_team.py)：`-peek` / `-team` 端口与 CQP 分组路由，
  外部 `tswn` 全部在测试侧 mock。
- [test_namer_todiy.py](../tests/test_namer_todiy.py)：`/namer-todiy` 的离线用例；
  装了 `tswn-cli` 时额外校验真实导出、`++` 往返与「组队加成烘进导出」。

## 结构化请求存档（0.12.0）

### 开启与兼容策略

- 管理员在本地插件配置中填写指向 `namer` 库的 `pg_connection`，并保持 `telemetry=true`。
  安装可用的 `psycopg` 驱动；插件首次加载自动在目标库初始化独立 `namerena` schema。
  不读取 Codex 配置、不操作其它 schema、不做历史消息回填。
- 默认连接串为空：功能沿用无存档模式，加载时会明确警告“当前不保存请求”。
  `telemetry=false` 时也保持旧行为，且不要求驱动或数据库可用。
- 开启且填写 PG 后为严格模式：初始化/请求登记失败不开始计算。实际调用前提交 started，完成后更新终态；
  结果提交失败保留未完成记录，不能保证崩溃前尚未提交的结果可恢复，也不会自动重放计算。
- 来源唯一键为平台、实际机器人账号、会话、消息 ID；重复投递不重复计算。
  Tailchat 宿主当前没有公开自身 user ID，因此严格模式必须显式填写真实账号，缺失只拒绝 Tailchat 请求，
  不能用宿主 client_id 或发言者代替。

### 存档内容与结构

- `jobs`：来源元数据、插件版本、带版本的 `input/result JSONB`、状态、时间及发送受理事件。
- `calls`：每次实际业务引擎调用的结构化输入/输出、引擎版本或资产摘要、退出码、耗时、错误分类。
  本地 Python 属性计算同样记录；运行时版本查询不当作业务计算。
- 输入按原命令保留组/成员/输入顺序、身份片段、武器片段、解析后的 DIY 覆盖、控制号及实际执行参数。
  本地 peek/team/base 使用各自真实的名字与空白规则；引擎 DSL 保存 `identity_parts` 等结构化词法，
  有效身份/隐式继承队伍/有效种子无法可靠解析时明确为 null、语义标为 engine_defined，
  不声称已经完整解析不同引擎的有效身份。裸 `seed:ABC` 仍是名字，`seed:123@!` 保留为控制号。
- 输出保存评分、胜场及实际轮数、赢家、属性与技能、DIY 数据。md5 用原生 JSON；
  tswn 战斗用 JSONL 的最终 result（丢弃逐帧轨迹），CQP/队友评分使用 JSONL 文件；
  PF/bench 的集中适配器读取引擎数值输出并标注 engine_reported 精度，解析失败明确记录 parse_failed。
  不从百分比倒推胜场，不把请求轮数假装成实际轮数；md5 的 winner 只承诺“赢家之一”。
- 请求结果按 input_index 和引擎对齐，保留 PF 各项分数、可比较项的差值、call_id 引用及部分失败。
  靶子转换等辅助调用保留在 calls，不冒充用户最终评分；同名输入也按序号独立保存。
- 不存命令原文、拼接 CLI 输入、完整 stdout/stderr、逐轮轨迹或最终回复文字。
  错误只存 code 与结构参数；发送事件只说明 accepted/not_accepted/unknown，受理不等于最终送达。

后台 CQP/队友评在线程真正结束后收尾，插件卸载不向旧连接回包；热重载重新导入辅助模块，
旧请求保留旧 Session，不与新请求串用。数据库与代码均不自动重试业务计算。

## 历史原始输入离线重算与覆盖

入口为 [namer_history.py](../tools/namer_history.py)，通过必填 --group 从只读历史源读取本次明确授权的单个群的全部候选原命令，
不读取旧回复、不以聊天 client 执行、不发送消息、不启动插件 on_load/watch。默认只盘点，
必须显式添加 `--execute` 才计算并写目标；目标数据库参数仅允许 `namer`，连接后还会核实真实 dbname。
连接从本地配置读取，不出现在命令行、日志或提交中。

在仓库目录执行（以下 12345 仅为合成群号，真实执行必须换成明确授权群号；保留默认评分 10000 轮、pair 1000 轮）：

```powershell
# 只盘点输入，不写目标
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt tools/namer_history.py --group 12345
# 小批试算：普通属性20条、PF两条
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt tools/namer_history.py --group 12345 --execute --commands /namer-peek --limit 20
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt tools/namer_history.py --group 12345 --execute --commands /namer-pf --limit 2
# 全部原命令，按来源覆盖当前单份结果（无limit）；默认单worker
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt tools/namer_history.py --group 12345 --execute --mode overwrite
# 断点继续，跳过相同计算指纹的成功来源；limit只计真正尝试，不计已跳过条目
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt tools/namer_history.py --group 12345 --execute --mode resume --limit 100
# 只重试已有失败/部分成功/中断条目，不新建尚未执行的来源
uv run --no-project --no-cache --python 3.12 --with-requirements requirements.txt tools/namer_history.py --group 12345 --execute --mode retry-failed
```

可用 `--source-config` 指定只读源配置；`--target-config` 默认读取本地 Codex 配置的连接后显式改库名；
`--plugin-config` 读取当前 namer 的运行时、tswn 与资产路径。
`--commands` 接受逗号分隔规范命令，重型 CQP/队友评可单独分批；
`--rounds`、`--pair-rounds` 可显式降低小批成本，不会暗中降低全量默认轮数。
每个子进程默认最多600秒、每条请求默认最多3600秒，可分别用 `--timeout`、`--request-timeout` 调整。
超时记录明确失败，后续可重试；不使用 shell 执行聊天内容，不擅自改变种子。

### 单结果覆盖与断点契约

- schema v2为历史 job 增加 history_key、requested_at、短期 writer_token；历史 account=NULL，
  origin=history_recompute，不猜测原执行机器人、不修改消息 ID。原请求发生时间与当前重算时间分开保存。
- history_key由稳定源别名、平台、会话、消息 ID构成；更换源别名视为另一来源，应保持默认稳定别名。
  live 与 history 是明确分开的路径，历史覆盖绝不删除实时 job/calls。
- 同来源计算期间持有会话级数据库锁；领取用短事务删除该历史 job 的旧 calls、清空旧 result、置 started。
  不是保留旧版本的 runs/attempts；新 calls 仍在每次真正执行前登记，完成后才发布当前业务结果。
- 失败留下 failed/partial，崩溃留下 started；旧成功不会继续冒充当前成功。断点重启只在取得独占锁后回收。
  writer_token只是瞬时所有权栅栏，贯穿调用/结果/发送元数据；旧worker失锁或被回收后不能写回新结果。
- resume 跳过相同指纹的成功样本，失败或中断可以重算；retry-failed只处理已有非成功样本。
  指纹包含输入、规范命令、轮数/超时设置、执行代码与引擎/资产摘要；发现批次内文件改变时整批停止。
- 旧别名按已知源码语义处理：_peeks/_teams 规范为对应连字符命令，rhp 对应当前 fsp；
  未知命令或无法解析输入明确记录 unsupported/invalid，不从旧回复推测结果。
  每行原始输入保留独立词法节点，非法overlay仅该行标 unparsed，不能丢弃其它行或名字空白。
- 全量暂不启用计算缓存/并发；每条来源独立落库，打印 scanned、executed、分类计数和 elapsed_seconds，
  不打印聊天正文、源消息标识或凭据。
