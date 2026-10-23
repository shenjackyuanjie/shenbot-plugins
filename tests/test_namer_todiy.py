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


class NamerToDiyTests(unittest.TestCase):
    def setUp(self):
        # 离线用例不关心本机是否装了 tswn-cli：runner 解析固定成假结果
        patcher = patch.object(
            namerena, "resolve_tswn_runner", return_value=(["tswn-cli"], None)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_group_without_team_is_still_split(self):
        """没有 @队伍 的双号靠单 + 输入就会被当成一个号 + 武器，必须走 ++。"""
        group = tswn_group_line(fake_to_ol("mario"), fake_to_ol("luigi"))

        def convert(input_file, output_file):
            output_file.write_text(group + "\n", encoding="utf-8")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=convert):
            output = run_command(f"{namerena.TODIY_CMD}\nmario++luigi")
        self.assertEqual(
            namerena._split_ol_group(output), [fake_to_ol("mario"), fake_to_ol("luigi")]
        )

    def test_boost_plus_inside_overlay_is_not_a_separator(self):
        left = fake_to_ol("a@t", {"sklmerge": "17+5"})
        right = fake_to_ol("b@t", {"sklmerge": "14+6"})

        def convert(input_file, output_file):
            output_file.write_text(
                tswn_group_line(left, right) + "\n", encoding="utf-8"
            )

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=convert):
            output = run_command(f"{namerena.TODIY_CMD}\na@t+b@t")
        self.assertEqual(output, left + "++" + right)
        players = namerena._split_ol_group(output)
        self.assertEqual(len(players), 2)
        self.assertEqual(skills_of(players[0]), {"sklmerge": 22})
        self.assertEqual(skills_of(players[1]), {"sklmerge": 20})

    def test_line_count_mismatch_is_reported(self):
        def drop_one(input_file, output_file):
            output_file.write_text(fake_to_ol("only") + "\n", encoding="utf-8")

        with patch.object(namerena, "_run_tswn_to_diy_file", side_effect=drop_one):
            output = run_command(f"{namerena.TODIY_CMD}\na\nb")
        self.assertIn("行数不一致", output)


@unittest.skipUnless(
    namerena.resolve_tswn_runner() is not None, "未找到 tswn-cli，跳过真实转换校验"
)
class NamerToDiyLiveTests(unittest.TestCase):
    def test_live_group_matches_namer_team_port(self):
        """组内同队加成要与 /namer-team 那份端口算得完全一致。"""
        grouped = namerena._split_ol_group(
            run_command(f"{namerena.TODIY_CMD}\n1@a+2@a")
        )
        team_attrs = {}
        for line in run_command("/namer-team\n1@a\n2@a").splitlines():
            if "|HP:" not in line:
                continue
            name, *fields = line.split("|")
            values = {
                field.split(":")[0]: int(field.split(":")[1].split("(")[0])
                for field in fields
            }
            team_attrs[name] = [
                values[label]
                for label in ("攻", "防", "速", "敏", "魔", "抗", "智", "HP")
            ]
        self.assertEqual(len(grouped), 2)
        for export in grouped:
            name = split_overlay(export)[0]
            self.assertEqual(
                attrs_of(export),
                team_attrs[name],
                f"{name} 的属性与 /namer-team 不一致",
            )


if __name__ == "__main__":
    unittest.main()
