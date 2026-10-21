import json
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from plugins import namerena


def run_command(content):
    msg = Mock(content=content, is_reply=False, is_from_self=False)
    msg.reply_with.side_effect = lambda text: text
    client = Mock()
    namerena.dispatch_msg(msg, client)
    client.send_message.assert_called_once()
    return client.send_message.call_args.args[0]


def split_overlay(export):
    """把 `名字+ol:{...}` 拆成前缀、overlay 对象与结束位置。"""
    marker = export.index("+ol:")
    overlay, end = json.JSONDecoder().raw_decode(export, marker + len("+ol:"))
    return export[:marker], overlay, end


def fake_to_ol(name, skills=None):
    """给 `+ol` 导出造一个形状正确的假结果，避免测试依赖外部 tswn-cli。"""
    skills = {"sklfire": 5} if skills is None else skills
    return (
        f'{name}+ol:{{"attrs":[66,69,45,65,69,46,89,297],'
        f'"skills":{json.dumps(skills, ensure_ascii=False)},'
        '"name_factor_enabled":true}'
    )


def tswn_group_line(*exports):
    """tswn-cli 的组内分隔符是单 `+`，测试侧按它的真实形状造数据。"""
    return "+".join(exports)


def decode_level(value):
    """把 `5` / `"2*7"` / `"17+5"` 这类技能等级写法还原成数字。"""
    if isinstance(value, int):
        return value
    if value.startswith("2*"):
        return 2 * int(value[2:])
    base, boost = value.split("+")
    return int(base) + int(boost)


def skills_of(export):
    _, overlay, _ = split_overlay(export)
    return {key: decode_level(value) for key, value in overlay["skills"].items()}


def attrs_of(export):
    _, overlay, _ = split_overlay(export)
    return overlay["attrs"]


def convert_echo(input_file, output_file):
    names = [line for line in input_file.read_text(encoding="utf-8").splitlines() if line]
    output_file.write_text("\n".join(fake_to_ol(name) for name in names) + "\n", encoding="utf-8")


class NamerToDiyTests(unittest.TestCase):
    def setUp(self):
        # 离线用例不关心本机是否装了 tswn-cli：runner 解析固定成假结果
        patcher = patch.object(
            namerena, "resolve_tswn_runner", return_value=(["tswn-cli"], None)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_usage_hint_without_newline(self):
        output = run_command(namerena.TODIY_CMD)
        self.assertIn("请使用", output)
        self.assertIn(namerena.TODIY_CMD, output)
        self.assertIn("++", output)

    def test_empty_input_asks_for_names(self):
        for content in (
            f"{namerena.TODIY_CMD}\n",
            f"{namerena.TODIY_CMD}\n\n  \n",
        ):
            with self.subTest(content=content):
                self.assertIn("请输入至少一个名字", run_command(content))

    def test_single_name_is_converted_to_overlay(self):
        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=convert_echo):
            output = run_command(f"{namerena.TODIY_CMD}\nmario@team")
        prefix, overlay, end = split_overlay(output)
        self.assertEqual(prefix, "mario@team")
        self.assertEqual(end, len(output))
        self.assertEqual(overlay["attrs"], [66, 69, 45, 65, 69, 46, 89, 297])
        self.assertIs(overlay["name_factor_enabled"], True)
        self.assertNotIn("++", output)

    def test_multiple_lines_keep_input_order_and_skip_blanks(self):
        names = ["mario@team", "", "luigi", "  ", "地狱之轮 #mW88BamWo@Shabby_fish"]
        expected = [fake_to_ol(name) for name in names if name.strip()]
        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=convert_echo):
            output = run_command(f"{namerena.TODIY_CMD}\n" + "\n".join(names))
        self.assertEqual(output.splitlines(), expected)

    def test_names_are_written_one_per_line(self):
        seen = {}

        def capture(input_file, output_file):
            seen["text"] = input_file.read_text(encoding="utf-8")
            output_file.write_text(fake_to_ol("a") + "\n" + fake_to_ol("b") + "\n", encoding="utf-8")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=capture):
            run_command(f"{namerena.TODIY_CMD}\na\nb")
        self.assertEqual(seen["text"], "a\nb\n")

    def test_group_players_are_joined_with_double_plus(self):
        group = tswn_group_line(fake_to_ol("mario@a"), fake_to_ol("luigi@a"))

        def convert(input_file, output_file):
            output_file.write_text(group + "\n", encoding="utf-8")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=convert):
            output = run_command(f"{namerena.TODIY_CMD}\nmario@a+luigi@a")
        self.assertEqual(output, fake_to_ol("mario@a") + "++" + fake_to_ol("luigi@a"))
        self.assertEqual(output.count("++"), 1)

    def test_group_without_team_is_still_split(self):
        """没有 @队伍 的双号靠单 + 输入就会被当成一个号 + 武器，必须走 ++。"""
        group = tswn_group_line(fake_to_ol("mario"), fake_to_ol("luigi"))

        def convert(input_file, output_file):
            output_file.write_text(group + "\n", encoding="utf-8")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=convert):
            output = run_command(f"{namerena.TODIY_CMD}\nmario++luigi")
        self.assertEqual(namerena._split_ol_group(output), [fake_to_ol("mario"), fake_to_ol("luigi")])

    def test_boost_plus_inside_overlay_is_not_a_separator(self):
        left = fake_to_ol("a@t", {"sklmerge": "17+5"})
        right = fake_to_ol("b@t", {"sklmerge": "14+6"})

        def convert(input_file, output_file):
            output_file.write_text(tswn_group_line(left, right) + "\n", encoding="utf-8")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=convert):
            output = run_command(f"{namerena.TODIY_CMD}\na@t+b@t")
        self.assertEqual(output, left + "++" + right)
        players = namerena._split_ol_group(output)
        self.assertEqual(len(players), 2)
        self.assertEqual(skills_of(players[0]), {"sklmerge": 22})
        self.assertEqual(skills_of(players[1]), {"sklmerge": 20})

    def test_split_ol_group_handles_both_separators(self):
        """靶子 DIY 走同一套扫描：单 + 与 ++ 都必须切出同样的两个号。"""
        left = fake_to_ol("a@t", {"sklmerge": "17+5"})
        right = fake_to_ol("b@t", {"sklfire": 3})
        expected = [left, right]
        self.assertEqual(namerena._split_ol_group(tswn_group_line(left, right)), expected)
        self.assertEqual(namerena._split_ol_group("++".join(expected)), expected)
        self.assertEqual(namerena._split_ol_group(left), [left])

    def test_line_count_mismatch_is_reported(self):
        def drop_one(input_file, output_file):
            output_file.write_text(fake_to_ol("only") + "\n", encoding="utf-8")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=drop_one):
            output = run_command(f"{namerena.TODIY_CMD}\na\nb")
        self.assertIn("行数不一致", output)

    def test_tswn_failure_is_reported(self):
        def boom(input_file, output_file):
            raise RuntimeError("tswn DIY 转换失败（exit 1）")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=boom):
            output = run_command(f"{namerena.TODIY_CMD}\nmario")
        self.assertIn("DIY 转换失败", output)
        self.assertIn("exit 1", output)

    def test_missing_tswn_cli_points_at_config(self):
        with patch.object(namerena, "resolve_tswn_runner", return_value=None):
            output = run_command(f"{namerena.TODIY_CMD}\nmario")
        self.assertIn("未找到 tswn-cli", output)
        self.assertIn("tswn_cli_path", output)

    def test_command_routes_to_to_diy_handler(self):
        msg = Mock(content=f"{namerena.TODIY_CMD}\nmario", is_reply=False, is_from_self=False)
        msg.reply_with.side_effect = lambda text: text
        client = Mock()
        with patch.object(namerena, "cmd_to_diy", side_effect=lambda m, c: c.send_message("out")), patch.object(
            namerena, "eval_fight", side_effect=lambda *a: self.fail("不应落入简化求值")
        ):
            namerena.dispatch_msg(msg, client)
        self.assertEqual(client.send_message.call_args.args[0], "out")

    def test_command_uses_hyphen_separator(self):
        self.assertTrue(namerena.TODIY_CMD.startswith(f"{namerena.CMD_PREFIX}-"))
        self.assertNotIn("_", namerena.TODIY_CMD[len(namerena.CMD_PREFIX) :])


@unittest.skipUnless(
    namerena.resolve_tswn_runner() is not None, "未找到 tswn-cli，跳过真实转换校验"
)
class NamerToDiyLiveTests(unittest.TestCase):
    def test_live_conversion_matches_tswn_cli(self):
        output = run_command(f"{namerena.TODIY_CMD}\nmario@team\nsolo")
        lines = output.splitlines()
        self.assertEqual(len(lines), 2)
        for line, name in zip(lines, ("mario@team", "solo")):
            prefix, overlay, end = split_overlay(line)
            self.assertEqual(prefix, name)
            self.assertEqual(end, len(line), "`+ol` 后不应再有内容")
            self.assertEqual(len(overlay["attrs"]), 8)
            self.assertIn("skills", overlay)
            self.assertIn("name_factor_enabled", overlay)

    def test_live_conversion_drops_weapon_suffix(self):
        output = run_command(f"{namerena.TODIY_CMD}\nmario+fire")
        prefix, overlay, _ = split_overlay(output)
        self.assertEqual(prefix, "mario")
        self.assertTrue(overlay["attrs"])

    def test_live_group_uses_double_plus_and_round_trips(self):
        """无队伍双号：导出用 ++，把导出原样喂回去必须还是那两个号。"""
        output = run_command(f"{namerena.TODIY_CMD}\nmario++luigi")
        self.assertIn("++", output)
        players = namerena._split_ol_group(output)
        self.assertEqual(len(players), 2)

        again = run_command(f"{namerena.TODIY_CMD}\n{output}")
        self.assertEqual(len(again.splitlines()), 1)
        self.assertEqual(namerena._split_ol_group(again), players)

    def test_live_group_bakes_in_same_team_upgrade(self):
        """同一行的两个同队号要吃组队加成，且加成写进 +ol。"""
        grouped = namerena._split_ol_group(
            run_command(f"{namerena.TODIY_CMD}\nn1a@t+n2a@t")
        )
        # 分成两行就不再是同一组，不会互相加成
        solo = run_command(f"{namerena.TODIY_CMD}\nn1a@t\nn2a@t").splitlines()
        self.assertEqual(len(grouped), 2)
        self.assertEqual(len(solo), 2)
        self.assertNotEqual(
            [skills_of(player) for player in grouped],
            [skills_of(player) for player in solo],
            "同一行成组时同队加成应写进 +ol",
        )
        for grouped_player, solo_player in zip(grouped, solo):
            self.assertEqual(attrs_of(grouped_player), attrs_of(solo_player))
            grouped_skills = skills_of(grouped_player)
            solo_skills = skills_of(solo_player)
            for key, level in grouped_skills.items():
                self.assertGreaterEqual(
                    level, solo_skills.get(key, 0), "组队加成不应降低技能等级"
                )

    def test_live_group_matches_namer_team_port(self):
        """组内同队加成要与 /namer-team 那份端口算得完全一致。"""
        grouped = namerena._split_ol_group(
            run_command(f"{namerena.TODIY_CMD}\n1@a+2@a")
        )
        team_attrs = {}
        for line in run_command(f"/namer-team\n1@a\n2@a").splitlines():
            if "|HP:" not in line:
                continue
            name, *fields = line.split("|")
            values = {
                field.split(":")[0]: int(field.split(":")[1].split("(")[0])
                for field in fields
            }
            team_attrs[name] = [
                values[label] for label in ("攻", "防", "速", "敏", "魔", "抗", "智", "HP")
            ]
        self.assertEqual(len(grouped), 2)
        for export in grouped:
            name = split_overlay(export)[0]
            self.assertEqual(attrs_of(export), team_attrs[name], f"{name} 的属性与 /namer-team 不一致")


if __name__ == "__main__":
    unittest.main()
