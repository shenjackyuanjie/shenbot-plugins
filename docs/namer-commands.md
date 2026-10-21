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
| `telemetry` | `true` | 预留开关；只有装了 `psycopg` 时才会置位，当前没有上报逻辑 |
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
