"""状态页现场查询的离线回归：隔离其他目标、透传失败，并保持周期状态行契约。"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins import ds_monitor


BASELINE = """status: 首次检查，已建立状态基线（事件 2 条，其中未恢复 0 条）
=== 本轮检查结果 changes=0 notices=0 errors=0 chat=skipped official=skipped harness=skipped platform=skipped docs=skipped fingerprint=skipped status=baseline ===
"""


class StatusProbeTests(unittest.TestCase):
    """只模拟子进程，不加载本地配置，也不启动实际检查或通知。"""

    def test_probe_command_runs_only_status_and_preserves_success_report(self) -> None:
        targets = {
            "chat",
            "official",
            "harness",
            "platform",
            "docs",
            "fingerprint",
            "status",
            "harness-release",
            "harness-tag",
            "harness-commit",
            "harness-npm",
            "github-profile",
        }
        # 有相邻 Rust 工作区时，直接只读 Check 声明确认 CLI 契约，避免重复清单遗漏新目标。
        cli = Path(__file__).resolve().parents[3] / "deepseek/web_craw/src/cli.rs"
        if cli.is_file():
            check_fields = (
                cli.read_text(encoding="utf-8")
                .split("    Check {", 1)[1]
                .split("    Watch {", 1)[0]
            )
            targets = {
                name.replace("_", "-")
                for name in re.findall(r"no_(\w+): bool", check_fields)
                if name != "analyze"
            }

        def run(args, **kwargs):
            self.assertEqual(
                args[:4], ["mock-monitor", "--config", "mock-config", "check"]
            )
            disabled = {
                arg.removeprefix("--no-") for arg in args if arg.startswith("--no-")
            }
            self.assertEqual(targets - disabled, {"status"})
            self.assertIn("--no-analyze", args)
            self.assertIn("--noticer=", args)
            self.assertGreater(kwargs["timeout"], 0)
            return subprocess.CompletedProcess(args, 0, BASELINE, "")

        with (
            patch.object(ds_monitor, "_binary_path", "mock-monitor"),
            patch.object(ds_monitor, "_config_path", "mock-config"),
            patch.object(ds_monitor, "validate_binary", return_value=True),
            patch.object(ds_monitor, "work_dir", return_value="mock-workdir"),
            patch.object(
                ds_monitor,
                "load_rust_config",
                side_effect=AssertionError("不能读取私有配置"),
            ),
            patch.object(ds_monitor.subprocess, "run", side_effect=run) as process,
        ):
            reply = ds_monitor.status_page_report(ds_monitor.run_status_probe())
        process.assert_called_once()
        self.assertIn("当前没有未恢复事件", reply)
        self.assertNotIn("❌", reply)

    def test_external_config_and_invalid_startup_never_launch_default_monitoring(
        self,
    ) -> None:
        """外置配置决定运行相对路径，真实 Cargo 仓库决定构建；坏配置必须在启动前拒绝。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source"
            binary = repo / "target/release/ds-monitor-copy.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"mock executable")
            manifest = repo / "Cargo.toml"
            manifest.write_text(
                '[package]\nname = "ds-monitor"\nversion = "0.5.0"\n', encoding="utf-8"
            )
            external = root / "external-config"
            external.mkdir()
            config = external / "monitor.toml"
            config.write_text("[status]\nenabled = true\n", encoding="utf-8")
            with (
                patch.object(ds_monitor, "_binary_path", str(binary)),
                patch.object(ds_monitor, "_config_path", str(config)),
                patch.object(ds_monitor, "log") as log,
                patch.object(ds_monitor, "notify_error") as notify,
                patch.object(ds_monitor.subprocess, "Popen") as popen,
                patch.object(
                    ds_monitor.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, BASELINE, ""),
                ) as run,
            ):
                self.assertEqual(Path(ds_monitor.work_dir()), external)
                self.assertEqual(Path(ds_monitor.repo_root()), repo)
                reply = ds_monitor.status_page_report(ds_monitor.run_status_probe())
                self.assertIn("当前没有未恢复事件", reply)
                self.assertEqual(Path(run.call_args.kwargs["cwd"]), external)
                run.reset_mock()
                config.unlink()
                reply = ds_monitor.status_page_report(ds_monitor.run_status_probe())
                self.assertIn("校验失败", reply)
                self.assertFalse(config.exists(), "不能让 Rust 自动创建默认配置")
                run.assert_not_called()
                self.assertIn("配置文件", str(log.call_args))
                secret = "仅用于测试的敏感配置值"
                config.write_text('[status]\nurl = "' + secret, encoding="utf-8")
                log.reset_mock()
                notify.reset_mock()
                self.assertFalse(ds_monitor.validate_binary())
                self.assertEqual(ds_monitor.load_rust_config(), {})
                diagnostic = str(log.call_args_list) + str(notify.call_args_list)
                self.assertIn("TOMLDecodeError", diagnostic)
                self.assertNotIn(secret, diagnostic)
                run.assert_not_called()
                popen.assert_not_called()
                # 配置旁的坏 manifest 不能劫持构建，合法 Cargo 目录则按契约优先。
                config.write_text("[status]\nenabled = true\n", encoding="utf-8")
                external_manifest = external / "Cargo.toml"
                external_manifest.write_text("not valid TOML", encoding="utf-8")
                self.assertEqual(Path(ds_monitor.repo_root()), repo)
                external_manifest.write_text(
                    "[workspace]\nmembers = []\n", encoding="utf-8"
                )
                self.assertEqual(Path(ds_monitor.repo_root()), external)
                self.assertEqual(Path(ds_monitor.work_dir()), external)
                external_manifest.unlink()
                manifest.unlink()
                with self.assertRaises(FileNotFoundError):
                    ds_monitor.repo_root()


if __name__ == "__main__":
    unittest.main()
