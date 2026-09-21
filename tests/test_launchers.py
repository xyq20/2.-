import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LauncherArgumentTests(unittest.TestCase):
    def test_new_product_menu_saves_without_publishing(self):
        arguments, _ = self._run_launcher(input_text="10\n2\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "base", "--create-product", "--save-only"))

    def test_new_product_menu_rejects_publish_mode(self):
        arguments, output = self._run_launcher(input_text="10\n3\n1\n")
        self.assertIn("请选择 1 或 2", output)
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "base", "--create-product", "--no-save"))

    def test_double_click_menu_allows_jd_no_save_preview(self):
        arguments, output = self._run_launcher(input_text="9\n1\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "jd", "--no-save"))
        self.assertIn("京东（填写、保存、铺货）", output)

    def test_double_click_menu_allows_jd_save_only(self):
        arguments, _output = self._run_launcher(input_text="9\n2\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "jd", "--save-only"))

    def test_double_click_menu_allows_jd_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="9\n3\nPUBLISH\n")
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "jd", "--save"))
        self.assertIn("的指定店铺提交铺货", output)

    def test_create_cli_defaults_to_base_and_preserves_equals_platform(self):
        arguments, _ = self._run_launcher(("--create-product", "--no-save"))
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "base", "--create-product", "--no-save"))
        arguments, _ = self._run_launcher(("--platform=base", "--create-product", "--no-save"))
        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform=base", "--create-product", "--no-save"))

    def _run_launcher(self, user_arguments=(), input_text="", extra_env=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run.command"
            shutil.copy2(PROJECT_ROOT / "run.command", launcher)
            launcher.chmod(0o755)
            fake_python = root / ".venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/bin/zsh\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"pip\" ]]; then exit 0; fi\n"
                "print -r -- RUN_ARGS_START\n"
                "for argument in \"$@\"; do print -r -- \"$argument\"; done\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            completed = subprocess.run(
                ["/bin/zsh", str(launcher), *user_arguments],
                input=input_text,
                check=True,
                capture_output=True,
                text=True,
                env={**__import__("os").environ,
                     "KUAIMAI_SKIP_PRODUCT_SELECTION": "1", **(extra_env or {})},
            )
        output = completed.stdout.splitlines()
        marker = output.index("RUN_ARGS_START")
        return tuple(output[marker + 1 :]), completed.stdout

    def test_double_click_menu_selects_pdd_no_save_preview(self):
        arguments, output = self._run_launcher(input_text="5\n1\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "pdd", "--no-save"),
        )
        self.assertIn("请选择需要运行的平台", output)
        self.assertIn("不会改动基础资料", output)

    def test_double_click_menu_identifies_pdd_as_part_of_all(self):
        _arguments, output = self._run_launcher(input_text="0\n1\n")

        self.assertIn("基础资料 + 抖音 + 淘宝 + 天猫 + 拼多多 + 微信小店 + 小红书", output)

    def test_double_click_menu_allows_wxsph_save_without_publish(self):
        arguments, output = self._run_launcher(input_text="6\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "wxsph", "--save-only"),
        )
        self.assertIn("微信小店（视频号，填写、保存、铺货）", output)

    def test_double_click_menu_allows_wxsph_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="6\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "wxsph", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_selects_xhs_form_preview_without_platform_save(self):
        arguments, output = self._run_launcher(input_text="7\n1\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--no-save"),
        )
        self.assertIn("小红书（填写、保存、铺货）", output)

    def test_double_click_menu_allows_xhs_save_without_publish(self):
        arguments, output = self._run_launcher(input_text="7\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--save-only"),
        )
        self.assertNotIn("只允许不保存预览", output)

    def test_double_click_menu_allows_xhs_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="7\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_selects_youzan_no_save_preview(self):
        arguments, output = self._run_launcher(input_text="8\n1\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "youzan", "--no-save"),
        )
        self.assertIn("有赞（填写、保存、铺货）", output)

    def test_double_click_menu_allows_youzan_save_only(self):
        arguments, _output = self._run_launcher(input_text="8\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "youzan", "--save-only"),
        )

    def test_double_click_menu_allows_youzan_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="8\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "youzan", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_allows_pdd_publish_after_confirmation(self):
        arguments, output = self._run_launcher(input_text="5\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "pdd", "--save"),
        )
        self.assertIn("的指定店铺提交铺货", output)

    def test_double_click_menu_allows_tmall_save_only_without_once_flag(self):
        arguments, output = self._run_launcher(input_text="4\n2\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "tmall", "--save-only"),
        )
        self.assertNotIn("只允许不保存预览", output)

    def test_double_click_menu_runs_tmall_formal_publish_without_once_flag(self):
        arguments, output = self._run_launcher(input_text="4\n3\nPUBLISH\n")

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "tmall", "--save"),
        )
        self.assertNotIn("allow-tmall-publish-once", arguments)

    def test_double_click_menu_can_select_taobao_save_without_publish(self):
        arguments, _output = self._run_launcher(input_text="3\n2\n")

        self.assertEqual(
            arguments,
            (
                "kuaimai_erp.py",
                "--platform",
                "taobao",
                "--save-only",
                "--allow-taobao-save-once",
            ),
        )

    def test_publish_requires_exact_confirmation_and_cancel_falls_back_to_no_save(self):
        arguments, output = self._run_launcher(input_text="3\n3\nno\n")

        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "taobao", "--no-save"))
        self.assertIn("未确认，已改为平台仅填写/检查", output)
        self.assertNotIn("--allow-taobao-publish-once", arguments)

    def test_command_line_arguments_bypass_menu_and_are_forwarded(self):
        arguments, output = self._run_launcher(
            ("--platform", "xhs", "--inspect-only", "--no-save")
        )

        self.assertEqual(
            arguments,
            ("kuaimai_erp.py", "--platform", "xhs", "--inspect-only", "--no-save"),
        )
        self.assertNotIn("请选择需要运行的平台", output)

    def test_command_line_defaults_platform_to_all_when_omitted(self):
        arguments, _output = self._run_launcher(("--no-save",))

        self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "all", "--no-save"))

    def test_learning_helper_environment_adds_flag_without_changing_menu_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run.command"
            shutil.copy2(PROJECT_ROOT / "run.command", launcher)
            launcher.chmod(0o755)
            fake_python = root / ".venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/bin/zsh\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"pip\" ]]; then exit 0; fi\n"
                "print -r -- RUN_ARGS_START\n"
                "for argument in \"$@\"; do print -r -- \"$argument\"; done\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            completed = subprocess.run(
                ["/bin/zsh", str(launcher)],
                input="9\n1\n",
                check=True,
                capture_output=True,
                text=True,
                env={**__import__("os").environ, "KUAIMAI_LEARNING_AUTO_ENABLE": "1",
                     "KUAIMAI_SKIP_PRODUCT_SELECTION": "1"},
            )
        output = completed.stdout.splitlines()
        marker = output.index("RUN_ARGS_START")
        self.assertEqual(
            tuple(output[marker + 1 :]),
            ("kuaimai_erp.py", "--platform", "jd", "--no-save", "--learning-enabled"),
        )

    def test_product_menu_scans_and_remembers_last_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products = root / "products"
            first = products / "A款" / "产品信息.xlsx"
            second = products / "B 款" / "产品信息.xlsx"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.touch()
            second.touch()
            last_file = root / "state" / "last-product-path"
            env = {"KUAIMAI_SKIP_PRODUCT_SELECTION": "0",
                   "KUAIMAI_PRODUCTS_ROOT": str(products),
                   "KUAIMAI_LAST_PRODUCT_FILE": str(last_file)}
            arguments, output = self._run_launcher(input_text="2\n9\n1\n", extra_env=env)
            self.assertIn("B 款", output)
            self.assertEqual(arguments,
                ("kuaimai_erp.py", "--excel-url", str(second), "--platform", "jd", "--no-save"))
            arguments, output = self._run_launcher(input_text="\n9\n1\n", extra_env=env)
            self.assertIn("[上次选择]", output)
            self.assertEqual(arguments,
                ("kuaimai_erp.py", "--excel-url", str(second), "--platform", "jd", "--no-save"))


if __name__ == "__main__":
    unittest.main()
