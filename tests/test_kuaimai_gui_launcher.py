import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class GuiLauncherTests(unittest.TestCase):
    def test_launcher_checks_tk_installs_dependencies_and_starts_gui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher = root / "run-gui.command"
            shutil.copy2(PROJECT_ROOT / "run-gui.command", launcher)
            launcher.chmod(0o755)

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_python3 = fake_bin / "python3"
            fake_python3.write_text(
                "#!/bin/zsh\n"
                "if [[ \"$1\" == \"-c\" ]]; then exit 0; fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_python3.chmod(0o755)

            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text(
                "#!/bin/zsh\n"
                "if [[ \"$1\" == \"-m\" && \"$2\" == \"pip\" ]]; then exit 0; fi\n"
                "print -r -- GUI_ARGS_START\n"
                "for argument in \"$@\"; do print -r -- \"$argument\"; done\n",
                encoding="utf-8",
            )
            venv_python.chmod(0o755)

            completed = subprocess.run(
                ["/bin/zsh", str(launcher)],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )

            output = completed.stdout.splitlines()
            marker = output.index("GUI_ARGS_START")
            self.assertEqual(tuple(output[marker + 1:]), ("kuaimai_gui.py",))


if __name__ == "__main__":
    unittest.main()
