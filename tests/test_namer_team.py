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

    def test_input_order_does_not_change_bonus(self):
        forward = run_command(f"/namer-team\n{FIRST}\n{SECOND}").splitlines()
        reverse = run_command(f"/namer-team\n{SECOND}\n{FIRST}").splitlines()
        self.assertEqual(reverse, forward[2:4] + forward[:2] + forward[4:])

    def test_single_player_matches_peek(self):
        for name in (FIRST, SECOND, "solo"):
            player = namerena.name_utils.Player()
            self.assertTrue(player.load(name))
            self.assertEqual(
                run_command(f"/namer-team\n{name}"),
                player.display() + f"\n版本: {namerena.VERSION}",
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

    def test_empty_invalid_and_overlong_names_reply(self):
        for content, expected in (
            ("/namer-team", "请使用"),
            ("/namer-team\n\n", "请输入至少一个名字"),
            ("/namer-team\na@b@c", "名字载入失败"),
            ("/namer-team\n" + "中" * 86, "超过 255 字节"),
        ):
            self.assertIn(expected, run_command(content))

    def test_cqp_uses_tswn_for_single_and_double_groups(self):
        fake_results = {
            1: ["12.345 mario"],
            2: ["67.890 mario+luigi"],
        }
        # 目标列表来自 tswn-openbox 仓库，mock 掉以免依赖本地检出
        target1 = Path("target1.txt")
        target2 = Path("target2.txt")

        def fake_run(player_file, target_file, target_factored=False):
            text = player_file.read_text(encoding="utf-8").strip()
            return fake_results[2 if "mario+luigi" in text else 1]

        with patch.object(namerena, "_run_tswn_cqp_file", side_effect=fake_run), patch.object(
            namerena, "_find_tswn_openbox_assets", return_value=(target1, target2)
        ):
            output = namerena.run_tswn_cqp(["mario", "mario+luigi"])
            self.assertEqual(output.splitlines()[0], "12.345 mario")
            self.assertEqual(output.splitlines()[1].split(None, 1)[1], "mario+luigi")

    def test_pair_rating_command_mapping(self):
        # 5 个命令与上游 settings.toml 的 5 个队友预设一一对应
        self.assertEqual(
            namerena.PAIR_COMMANDS,
            {
                "/namer-cp": ("刺评", "teammate_fz.toml", 4),
                "/namer-fp": ("辅评", "teammate_bc.toml", 4),
                "/namer-wcp": ("无刺评", "teammate_wc.toml", 4),
                "/namer-fsp": ("分身评", "teammate_pj.toml", 4),
                "/namer-pjp": ("配件评", "teammate_fs.toml", 4),
            },
        )

    def test_pair_rating_mapping_has_no_duplicate_targets(self):
        targets = list(namerena.PAIR_COMMANDS.values())
        self.assertEqual(len(targets), len(set(targets)), "队友预设不应重复映射")

    def test_new_commands_use_hyphen_separator(self):
        for command in (
            namerena.CQP_CMD,
            namerena.CONVERT_CMD,
            namerena.CONVERT_RAW_CMD,
            namerena.BASE_CMD,
            namerena.TEAM_CMD,
            namerena.TEAM_RAW_CMD,
        ):
            with self.subTest(command=command):
                self.assertTrue(command.startswith(f"{namerena.CMD_PREFIX}-"))
                self.assertNotIn("_", command[len(namerena.CMD_PREFIX) :])

    def test_openbox_asset_search_shares_candidate_roots(self):
        """openbox 与 backend 的资源定位必须走同一套候选根。"""
        found = {
            "D:/repos/tswn-openbox/crates/tswn_openbox_backend/assets/targets/target1.txt",
            "D:/repos/tswn-openbox/crates/tswn_openbox_backend/assets/targets/target2.txt",
            "D:/repos/tswn-openbox/crates/tswn_openbox_backend/assets/settings.toml",
        }
        runner = (["D:/repos/tswn-openbox/target/release/tswn-cli.exe"], None)

        with patch.object(namerena, "resolve_tswn_runner", return_value=runner), patch.object(
            Path, "is_file", lambda self: str(self).replace("\\", "/") in found
        ), patch.object(Path, "resolve", lambda self: self):
            roots = namerena._tswn_asset_roots()
            self.assertIn(Path("D:/repos/tswn-openbox"), roots)

            bases = namerena._iter_tswn_repo_bases()
            self.assertIn(Path("D:/repos/tswn-openbox").resolve(), bases)
            self.assertIn(Path("D:/repos/tswn-openbox/tswn-new").resolve(), bases)
            self.assertEqual(bases, namerena._iter_tswn_repo_bases(), "候选应可重复且稳定")

            # 两个查找函数吃同一份候选，找到的资产目录一致
            self.assertEqual(
                namerena._find_tswn_openbox_assets(),
                (
                    Path(
                        "D:/repos/tswn-openbox/crates/tswn_openbox_backend/assets/targets/target1.txt"
                    ),
                    Path(
                        "D:/repos/tswn-openbox/crates/tswn_openbox_backend/assets/targets/target2.txt"
                    ),
                ),
            )
            self.assertEqual(
                namerena._find_tswn_backend_assets(),
                Path("D:/repos/tswn-openbox/crates/tswn_openbox_backend/assets"),
            )

    def test_asset_candidates_skip_nonexistent_openbox_crate(self):
        """tswn_openbox 没有 assets/，不应出现在候选里。"""
        with patch.object(namerena, "TSWN_ASSETS_PATH", ""), patch.object(
            namerena, "resolve_tswn_runner", return_value=None
        ), patch.object(Path, "resolve", lambda self: self):
            for path in namerena._iter_tswn_asset_dirs():
                self.assertNotIn("tswn_openbox", path.parts, path)
            dirs = namerena._configured_asset_dirs()
            self.assertEqual(dirs, [])

    def test_asset_search_returns_none_when_nothing_found(self):
        with patch.object(
            namerena, "resolve_tswn_runner", return_value=None
        ), patch.object(
            namerena, "__file__", "D:/nothing/ica-plugin/plugins/namerena.py"
        ), patch.object(
            Path, "is_file", lambda self: False
        ), patch.object(
            Path, "resolve", lambda self: self
        ):
            self.assertIsNone(namerena._find_tswn_openbox_assets())
            self.assertIsNone(namerena._find_tswn_backend_assets())
            self.assertIsNone(namerena._find_tswn_teammate_file("teammate_fz.toml"))

    def test_raw_and_sorted_commands_route_separately(self):
        routed = {}

        def make(name):
            def handler(msg, client, *args, **kwargs):
                routed[name] = kwargs.get("sort_skills", True)
                client.send_message("out")

            return handler

        msg = Mock(content="", is_reply=False, is_from_self=False)
        msg.reply_with.side_effect = lambda text: text
        client = Mock()
        with patch.object(namerena, "convert_name", make("convert_name")), patch.object(
            namerena, "convert_team", make("convert_team")
        ):
            for content, expected in (
                (f"{namerena.CONVERT_CMD}\nname", ("convert_name", True)),
                (f"{namerena.CONVERT_RAW_CMD}\nname", ("convert_name", False)),
                (f"{namerena.TEAM_CMD}\nname", ("convert_team", True)),
                (f"{namerena.TEAM_RAW_CMD}\nname", ("convert_team", False)),
            ):
                with self.subTest(content=content):
                    routed.clear()
                    msg.content = content
                    namerena.dispatch_msg(msg, client)
                    self.assertEqual(
                        routed,
                        {expected[0]: expected[1]},
                        f"{content} 应路由到 {expected[0]}",
                    )

    def test_underscore_typo_is_not_treated_as_eval(self):
        called = []
        msg = Mock(content="", is_reply=False, is_from_self=False)
        msg.reply_with.side_effect = lambda text: text
        client = Mock()
        with patch.object(namerena, "eval_fight", side_effect=lambda *a: called.append(1)):
            for content in ("/namer_peak", "/namer_team", "/namer_peeks", "/namer_teams"):
                with self.subTest(content=content):
                    msg.content = content
                    namerena.dispatch_msg(msg, client)
        self.assertEqual(called, [], "下划线输入不应落入简化求值")

    def test_configured_asset_path_is_searched_first(self):
        configured = Path("D:/custom/openbox-assets")
        expected = {
            "D:/custom/openbox-assets/targets/target1.txt",
            "D:/custom/openbox-assets/targets/target2.txt",
        }

        with patch.object(namerena, "TSWN_ASSETS_PATH", str(configured)), patch.object(
            namerena, "resolve_tswn_runner", return_value=None
        ), patch.object(
            Path, "is_file", lambda self: str(self).replace("\\", "/") in expected
        ), patch.object(
            Path, "resolve", lambda self: self
        ):
            dirs = namerena._iter_tswn_asset_dirs()
            self.assertEqual(dirs[0], configured, "配置项必须排在候选最前")
            self.assertEqual(
                namerena._find_tswn_openbox_assets(),
                (
                    Path("D:/custom/openbox-assets/targets/target1.txt"),
                    Path("D:/custom/openbox-assets/targets/target2.txt"),
                ),
            )

    def test_configured_asset_path_accepts_repo_root(self):
        # 允许填仓库根目录，会自动补 crates/tswn_openbox_backend/assets
        with patch.object(
            namerena, "TSWN_ASSETS_PATH", "D:/repos/tswn-core"
        ), patch.object(namerena, "resolve_tswn_runner", return_value=None), patch.object(
            Path, "resolve", lambda self: self
        ):
            dirs = namerena._iter_tswn_asset_dirs()
            self.assertIn(
                Path("D:/repos/tswn-core/crates/tswn_openbox_backend/assets"), dirs
            )

    def test_blank_asset_path_does_not_inject_candidates(self):
        with patch.object(namerena, "TSWN_ASSETS_PATH", "  "), patch.object(
            namerena, "resolve_tswn_runner", return_value=None
        ):
            self.assertEqual(namerena._configured_asset_dirs(), [])



if __name__ == "__main__":
    unittest.main()
