import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from plugins import namerena


FIRST = "大家好啊，我是媒莺旷籍@otto"
SECOND = "大家好啊，我是婪蛤渤泪@otto"


def run_command(content):
    msg = Mock(content=content, is_reply=False, is_from_self=False)
    msg.reply_with.side_effect = lambda text: text
    client = Mock()
    namerena.dispatch_msg(msg, client)
    client.send_message.assert_called_once()
    return client.send_message.call_args.args[0]


class NamerTeamTests(unittest.TestCase):
    def test_example_matches_attributes_and_final_skills(self):
        self.assertEqual(
            run_command(f"/namer-team\n{FIRST}\n{SECOND}"),
            f"{FIRST}|HP:305|攻:60|防:92(+10)|速:94(+5)|敏:59|魔:92|抗:95|智:91|八围:685\n"
            "潜行:98|召唤亡灵:44|防御:26|吞噬:13|伤害反弹:10|魅惑:4\n"
            f"{SECOND}|HP:330|攻:72|防:89|速:98|敏:94|魔:82|抗:96|智:94|八围:735\n"
            "守护:82|铁壁:68|垂死抗争:21|伤害反弹:15|苏生术:10|吸血攻击:8|魅惑:6|聚气:5\n"
            f"版本: {namerena.VERSION}",
        )

    def test_raw_commands_keep_skill_slot_order(self):
        player = namerena.name_utils.Player()
        self.assertTrue(player.load(FIRST))
        self.assertEqual(
            run_command(f"/namer-peeks\n{FIRST}"),
            player.display(sort_skills=False) + f"\n版本:{namerena.VERSION}",
        )

        team_output = run_command(f"/namer-teams\n{FIRST}\n{SECOND}")
        self.assertLess(team_output.index("魅惑:4"), team_output.index("伤害反弹:10"))

    def test_different_teams_receive_no_bonus(self):
        other_team = SECOND.replace("@otto", "@other")
        expected = []
        for name in (FIRST, other_team):
            player = namerena.name_utils.Player()
            self.assertTrue(player.load(name))
            expected.append(player.display())
        self.assertEqual(
            run_command(f"/namer-team\n{FIRST}\n{other_team}"),
            "\n".join(expected) + f"\n版本: {namerena.VERSION}",
        )

    def test_cqp_uses_tswn_for_single_and_double_groups(self):
        fake_results = {
            1: ["12.345 mario"],
            2: ["67.890 mario+luigi"],
        }
        # 只验证单/双人分组路由；资源定位、DIY 转换和计算均 mock，不执行外部 tswn。
        target1 = Path("target1.txt")
        target2 = Path("target2.txt")

        def fake_convert(input_file, output_file):
            output_file.write_text(
                input_file.read_text(encoding="utf-8"), encoding="utf-8"
            )

        def fake_run(player_file, target_file, target_factored=False):
            text = player_file.read_text(encoding="utf-8").strip()
            count = 2 if "mario+luigi" in text else 1
            self.assertEqual(target_file, target2 if count == 2 else target1)
            self.assertFalse(target_factored)
            return fake_results[count]

        with (
            patch.object(namerena, "_run_tswn_cqp_file", side_effect=fake_run),
            patch.object(
                namerena, "_find_tswn_openbox_assets", return_value=(target1, target2)
            ),
            patch.object(namerena, "_run_tswn_to_diy_file", side_effect=fake_convert),
        ):
            output = namerena.run_tswn_cqp(["mario", "mario+luigi"])
            self.assertEqual(output.splitlines()[0], "12.345 mario")
            self.assertEqual(output.splitlines()[1].split(None, 1)[1], "mario+luigi")

    def test_bench_compare_uses_bench_auto(self):
        calls = []

        def fake_run(input_text, *args):
            calls.append((input_text, args))
            return "胜率: 50.00%  (5000/10000)", 0.01

        with patch.object(namerena, "run_tswn_cli", side_effect=fake_run):
            result = namerena.run_tswn_bench_compare("!test!\n!a\n!b")

        # 顶层 raw 已移除，必须走 bench auto
        self.assertEqual(
            calls[0][1], ("bench", "auto", "-n", str(namerena.TSWN_COMPARE_ROUNDS))
        )
        self.assertEqual(calls[0][0], "!a\n!b")
        self.assertEqual(result[0], "胜率: 50.00%")


if __name__ == "__main__":
    unittest.main()
