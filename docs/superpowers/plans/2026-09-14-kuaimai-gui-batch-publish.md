# 快麦一键铺货 GUI 与批量执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为同事提供一个可迁移到其他 Mac 的 Tkinter 商品选择窗口，支持勾选单个商品或多个商品，按顺序调用现有快麦全平台真实铺货流程。

**Architecture:** 新增 `kuaimai_gui.py`，将共享盘发现、商品项、命令组装和串行批量队列做成不依赖 Tkinter 的可测试函数；Tkinter 窗口只负责选择、状态、日志和线程调度。新增 `run-gui.command` 负责环境准备和启动 GUI，现有 `kuaimai_erp.py` 及平台逻辑保持不变，每个商品通过独立子进程执行。

**Tech Stack:** Python 3、Tkinter/ttk、`subprocess`、`threading`、`queue`、现有 `.venv` 和 `unittest` 测试体系。

---

### Task 1: 建立可测试的商品发现与批量队列契约

**Files:**
- Create: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/tests/test_kuaimai_gui.py`
- Create: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/kuaimai_gui.py`

- [ ] **Step 1: Write failing tests for valid product discovery and natural ordering**

在 `tests/test_kuaimai_gui.py` 中先写入：

```python
import tempfile
import unittest
from pathlib import Path

from kuaimai_gui import ProductChoice, discover_products


class ProductDiscoveryTests(unittest.TestCase):
    def test_only_directories_with_excel_are_listed_in_natural_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "款10").mkdir()
            (root / "款10" / "产品信息.xlsx").write_bytes(b"xlsx")
            (root / "款2").mkdir()
            (root / "款2" / "产品信息.xlsx").write_bytes(b"xlsx")
            (root / "缺少Excel").mkdir()
            (root / "products.txt").write_text("ignore", encoding="utf-8")

            result = discover_products(root)

            self.assertEqual([item.display_name for item in result], ["款2", "款10"])
            self.assertEqual(result[0].excel_path, root / "款2" / "产品信息.xlsx")
            self.assertIsInstance(result[0], ProductChoice)

    def test_missing_root_returns_empty_tuple(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = discover_products(Path(temp_dir) / "not-found")

            self.assertEqual(result, ())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
python3 -m unittest tests/test_kuaimai_gui.py -v
```

Expected: FAIL because `kuaimai_gui.py` and `discover_products` do not exist yet.

- [ ] **Step 3: Define the pure product model and discovery implementation**

在 `kuaimai_gui.py` 顶部实现以下接口，不在模块导入阶段创建 Tkinter 根窗口：

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Tuple


PRODUCT_EXCEL_NAME = "产品信息.xlsx"


@dataclass(frozen=True)
class ProductChoice:
    directory: Path
    excel_path: Path

    @property
    def display_name(self) -> str:
        return self.directory.name


def natural_sort_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


def discover_products(products_root: Path) -> Tuple[ProductChoice, ...]:
    root = Path(products_root).expanduser()
    if not root.is_dir():
        return ()

    choices = []
    for child in sorted(root.iterdir(), key=lambda item: natural_sort_key(item.name)):
        if not child.is_dir():
            continue
        excel_path = child / PRODUCT_EXCEL_NAME
        if excel_path.is_file():
            choices.append(ProductChoice(child, excel_path))
    return tuple(choices)
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run:

```bash
python3 -m unittest tests/test_kuaimai_gui.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 5: Commit the pure discovery contract**

```bash
git add kuaimai_gui.py tests/test_kuaimai_gui.py
git commit -m "test: define gui product discovery contract"
```

### Task 2: 实现共享盘根目录发现、命令组装和串行批量队列

**Files:**
- Modify: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/kuaimai_gui.py`
- Modify: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/tests/test_kuaimai_gui.py`

- [ ] **Step 1: Write failing tests for root candidates, publish command, and batch status**

在测试文件追加以下测试和导入：

```python
import threading

from kuaimai_gui import (
    BatchItemResult,
    BatchStatus,
    build_product_command,
    discover_products_root,
    run_batch,
)


class BatchExecutionTests(unittest.TestCase):
    def _product(self, root: Path, name: str) -> ProductChoice:
        directory = root / name
        directory.mkdir()
        excel_path = directory / "产品信息.xlsx"
        excel_path.write_bytes(b"xlsx")
        return ProductChoice(directory, excel_path)

    def test_configured_root_is_preferred_when_it_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured = root / "configured-products"
            configured.mkdir()
            volumes = root / "Volumes"
            (volumes / "volume" / "products").mkdir(parents=True)

            result = discover_products_root(configured, volumes)

            self.assertEqual(result, configured)

    def test_command_uses_existing_all_platform_publish_entrypoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            product = self._product(Path(temp_dir), "款1")
            command = build_product_command(
                Path("/tmp/project/.venv/bin/python"),
                Path("/tmp/project/kuaimai_erp.py"),
                product,
            )

            self.assertEqual(
                command,
                [
                    "/tmp/project/.venv/bin/python",
                    "/tmp/project/kuaimai_erp.py",
                    "--excel-url",
                    str(product.excel_path),
                    "--platform",
                    "all",
                    "--save",
                ],
            )

    def test_batch_runs_selected_products_in_order_and_continues_after_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            products = [self._product(root, name) for name in ("一", "二", "三")]
            started = []

            def fake_runner(product, emit):
                started.append(product.display_name)
                emit(f"处理 {product.display_name}")
                return 2 if product.display_name == "二" else 0

            results = run_batch(products, fake_runner, threading.Event())

            self.assertEqual(started, ["一", "二", "三"])
            self.assertEqual(
                [item.status for item in results],
                [BatchStatus.COMPLETED, BatchStatus.FAILED, BatchStatus.COMPLETED],
            )
            self.assertTrue(all(isinstance(item, BatchItemResult) for item in results))

    def test_stop_event_marks_unstarted_products_without_running_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            products = [self._product(root, name) for name in ("一", "二", "三")]
            started = []
            stop_event = threading.Event()

            def fake_runner(product, emit):
                started.append(product.display_name)
                stop_event.set()
                return 0

            results = run_batch(products, fake_runner, stop_event)

            self.assertEqual(started, ["一"])
            self.assertEqual(
                [item.status for item in results],
                [BatchStatus.COMPLETED, BatchStatus.NOT_STARTED, BatchStatus.NOT_STARTED],
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused tests to verify the new tests fail**

Run:

```bash
python3 -m unittest tests/test_kuaimai_gui.py -v
```

Expected: FAIL because the root discovery, command builder, status enum, and batch runner are not defined.

- [ ] **Step 3: Implement root discovery, command assembly, and queue execution**

在 `kuaimai_gui.py` 中加入以下固定接口和实现；候选路径只在 `/Volumes` 的有限深度内检查，不递归扫描整个磁盘：

```python
from enum import Enum
from typing import Callable, List, Optional, Sequence
import threading


class BatchStatus(str, Enum):
    COMPLETED = "已完成/已提交"
    FAILED = "失败"
    NOT_STARTED = "未启动"


@dataclass(frozen=True)
class BatchItemResult:
    product: ProductChoice
    status: BatchStatus
    return_code: Optional[int] = None
    error: str = ""


def discover_products_root(
    configured_root: Optional[Path],
    volumes_root: Path = Path("/Volumes"),
) -> Optional[Path]:
    candidates = []
    if configured_root is not None:
        candidates.append(Path(configured_root).expanduser())
    volumes = Path(volumes_root)
    if volumes.is_dir():
        for volume in sorted(volumes.iterdir(), key=lambda item: natural_sort_key(item.name)):
            if not volume.is_dir():
                continue
            candidates.extend(
                (
                    volume / "共享文件" / "谭" / "products",
                    volume / "谭" / "products",
                    volume / "products",
                )
            )
    seen = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            return resolved
    return None


def build_product_command(
    python_executable: Path,
    script_path: Path,
    product: ProductChoice,
) -> List[str]:
    return [
        str(python_executable),
        str(script_path),
        "--excel-url",
        str(product.excel_path),
        "--platform",
        "all",
        "--save",
    ]


Runner = Callable[[ProductChoice, Callable[[str], None]], int]


def run_batch(
    products: Sequence[ProductChoice],
    runner: Runner,
    stop_event: threading.Event,
) -> List[BatchItemResult]:
    results: List[BatchItemResult] = []
    for index, product in enumerate(products):
        if stop_event.is_set():
            results.extend(
                BatchItemResult(item, BatchStatus.NOT_STARTED)
                for item in products[index:]
            )
            break
        try:
            return_code = runner(product, lambda _line: None)
        except Exception as exc:
            results.append(
                BatchItemResult(product, BatchStatus.FAILED, error=str(exc))
            )
            continue
        status = BatchStatus.COMPLETED if return_code == 0 else BatchStatus.FAILED
        results.append(BatchItemResult(product, status, return_code=return_code))
    return results
```

实现时保留一个可注入的 `emit` 回调，不在纯队列函数里创建线程或 Tkinter 对象；窗口层再把真实输出传入回调，测试可以继续使用内存列表。

- [ ] **Step 4: 修正 runner 输出回调的队列传递并重新运行测试**

将 `run_batch` 的 runner 类型和调用改为让每个商品收到当前 GUI 的输出回调：

```python
Runner = Callable[[ProductChoice, Callable[[str], None]], int]


def run_batch(
    products: Sequence[ProductChoice],
    runner: Runner,
    stop_event: threading.Event,
    emit: Optional[Callable[[str], None]] = None,
    on_start: Optional[Callable[[int, int, ProductChoice], None]] = None,
) -> List[BatchItemResult]:
    output = emit or (lambda _line: None)
    results: List[BatchItemResult] = []
    for index, product in enumerate(products):
        if stop_event.is_set():
            results.extend(
                BatchItemResult(item, BatchStatus.NOT_STARTED)
                for item in products[index:]
            )
            break
        if on_start is not None:
            on_start(index, len(products), product)
        try:
            return_code = runner(product, output)
        except Exception as exc:
            results.append(BatchItemResult(product, BatchStatus.FAILED, error=str(exc)))
            continue
        results.append(
            BatchItemResult(
                product,
                BatchStatus.COMPLETED if return_code == 0 else BatchStatus.FAILED,
                return_code=return_code,
            )
        )
    return results
```

Run:

```bash
python3 -m unittest tests/test_kuaimai_gui.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit the pure batch execution layer**

```bash
git add kuaimai_gui.py tests/test_kuaimai_gui.py
git commit -m "feat: add gui product discovery and batch queue"
```

### Task 3: 接入真实子进程并构建 Tkinter 窗口

**Files:**
- Modify: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/kuaimai_gui.py`

- [ ] **Step 1: Add the real process runner without importing Tkinter**

实现以下函数，保证 GUI 子进程使用当前项目目录和当前虚拟环境，输出合并为一条流，保留现有脚本的交互式登录输入：

```python
import os
import subprocess
import sys


PROJECT_DIR = Path(__file__).resolve().parent
ERP_SCRIPT = PROJECT_DIR / "kuaimai_erp.py"


def run_product_process(
    product: ProductChoice,
    emit: Callable[[str], None],
) -> int:
    command = build_product_command(Path(sys.executable), ERP_SCRIPT, product)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_DIR),
        env=environment,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        emit(line.rstrip())
    return process.wait()
```

- [ ] **Step 2: Add the Tkinter app with single and batch actions**

在 `kuaimai_gui.py` 中实现 `KuaimaiGuiApp`，Tkinter 仅在构造函数内导入。窗口需要包含：共享盘路径、刷新/选择目录、全选/清空、带滚动条的复选框列表、`单个执行`、`批量执行所选`、`停止后续任务`、进度标签和日志框。

核心事件逻辑使用以下结构：

```python
class KuaimaiGuiApp:
    def __init__(self, root, products_root=None):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.products_root = products_root
        self.products = ()
        self.check_vars = {}
        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        # 创建路径行、选择按钮、滚动复选框列表、动作按钮、进度标签和日志框。
        # refresh_products() 首次调用，poll_ui_queue() 用 root.after(100, ...) 持续轮询。

    def single_execute(self):
        selected = self.selected_products()
        if len(selected) != 1:
            self.show_warning("单个执行需要且只能勾选 1 个商品")
            return
        self.start_execution(selected, "单个执行")

    def batch_execute(self):
        selected = self.selected_products()
        if not selected:
            self.show_warning("请至少勾选 1 个商品")
            return
        self.start_execution(selected, f"批量执行 {len(selected)} 个商品")

    def start_execution(self, selected, label):
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.set_controls_enabled(False)
        self.append_log(f"开始{label}；点击按钮即表示确认真实保存并铺货")
        threading.Thread(
            target=self.worker_main,
            args=(tuple(selected),),
            daemon=True,
        ).start()

    def worker_main(self, selected):
        results = run_batch(
            selected,
            run_product_process,
            self.stop_event,
            emit=lambda line: self.ui_queue.put(("log", line)),
            on_start=lambda index, total, product: self.ui_queue.put(
                ("started", index, total, product.display_name)
            ),
        )
        self.ui_queue.put(("complete", results))
```

实际实现必须把所有 Tkinter 控件更新放回主线程：worker 只向 `ui_queue` 写入 `("log", line)`、`("started", ...)`、`("complete", results)` 事件；`poll_ui_queue` 负责刷新日志、当前商品、状态和按钮。

单个执行按钮只接受一个勾选项；批量执行按钮接受一个或多个勾选项并按列表顺序执行。批量失败继续后续商品；停止按钮只设置 `stop_event`，不杀掉当前子进程。

- [ ] **Step 3: Add safe manual root selection and refresh behavior**

为窗口实现配置文件读写、`choose_products_root` 和 `refresh_products`。配置文件只保存共享盘路径，不保存认证信息：

```python
import json


GUI_CONFIG_PATH = PROJECT_DIR / "output" / "kuaimai" / "gui-config.json"


def load_configured_products_root(
    config_path: Path = GUI_CONFIG_PATH,
) -> Optional[Path]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        value = str(data.get("products_root", "")).strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return Path(value).expanduser() if value else None


def save_configured_products_root(
    products_root: Path,
    config_path: Path = GUI_CONFIG_PATH,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"products_root": str(products_root)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def choose_products_root(self):
    from tkinter import filedialog

    selected = filedialog.askdirectory(
        parent=self.root,
        title="选择共享盘 products 文件夹",
    )
    if selected:
        self.products_root = Path(selected)
        save_configured_products_root(self.products_root)
        self.refresh_products()


def refresh_products(self):
    root = discover_products_root(self.products_root)
    if root is None and self.products_root is not None and self.products_root.is_dir():
        root = self.products_root
    self.products_root = root
    self.products = discover_products(root) if root else ()
    self.rebuild_product_checkboxes()
    self.path_var.set(str(root) if root else "未找到共享盘 products 目录")
```

`KuaimaiGuiApp.__init__` 使用 `products_root or load_configured_products_root()` 作为初始配置；配置路径失效时仍由 `discover_products_root` 自动扫描 `/Volumes`。刷新按钮保留当前目录中的勾选状态仅在商品仍存在时恢复，已消失的商品取消勾选。

配置文件只保存 `products_root` 路径，放在 `output/kuaimai/gui-config.json`，不保存任何认证信息；读取损坏或不存在的配置时直接回退到 `/Volumes` 自动发现。选择目录后必须重新发现并只显示包含 `产品信息.xlsx` 的一级文件夹。

- [ ] **Step 4: Add the module entry point and run a headless import check**

实现：

```python
def main() -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        print("当前 Python 没有 Tkinter，请安装带 Tk 支持的 Python 3 后重试。", file=sys.stderr)
        print(f"详细错误：{exc}", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("快麦一键铺货")
    KuaimaiGuiApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run:

```bash
python3 -c 'import kuaimai_gui; print("gui module import ok")'
python3 -m unittest tests/test_kuaimai_gui.py -v
```

Expected: 模块导入成功，纯逻辑测试全部 PASS；有 Tk 支持的 Mac 上手动运行 `python3 kuaimai_gui.py` 会打开窗口。

- [ ] **Step 5: Commit the Tkinter UI and subprocess bridge**

```bash
git add kuaimai_gui.py
git commit -m "feat: add tkinter kuaimai publish gui"
```

### Task 4: 添加换机启动器、迁移说明和启动器测试

**Files:**
- Create: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/run-gui.command`
- Create: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/docs/kuaimai-gui-migration.md`
- Create: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/tests/test_kuaimai_gui_launcher.py`

- [ ] **Step 1: Write the launcher test with fake Python executables**

新建 `tests/test_kuaimai_gui_launcher.py`，通过临时目录复制启动器和 fake `python3`/`.venv/bin/python`，验证它不会进入原有终端菜单：

```python
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
```

- [ ] **Step 2: Run the launcher test to verify it fails**

Run:

```bash
python3 -m unittest tests/test_kuaimai_gui_launcher.py -v
```

Expected: FAIL because `run-gui.command` does not exist yet.

- [ ] **Step 3: Implement `run-gui.command`**

写入并设置可执行权限：

```zsh
#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  print "找不到 python3。请先在这台 Mac 安装 Python 3（需要包含 Tkinter），然后重新双击本文件。"
  exit 1
fi

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  print "当前 Python 没有 Tkinter 支持。请安装带 Tk 支持的 Python 3，然后重新双击本文件。"
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  print "首次运行：正在创建 Python 环境……"
  python3 -m venv .venv
fi

print "正在检查并安装运行依赖，请稍候……"
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
print "环境已就绪，正在打开快麦一键铺货窗口……"
export PYTHONUNBUFFERED=1
exec .venv/bin/python kuaimai_gui.py
```

- [ ] **Step 4: Write migration instructions and make the launcher executable**

`docs/kuaimai-gui-migration.md` 至少包含：

```markdown
# 快麦一键铺货 GUI 换机运行

1. 将整个项目文件夹复制到新 Mac，不要复制旧 Mac 的 `output/kuaimai/auth-state.json` 或 Chrome profile。
2. 在 Finder 中连接共享盘，确认能看到 `共享文件/谭/products`。
3. 双击项目内的 `run-gui.command`；首次启动会创建 `.venv` 并安装依赖。
4. 首次运行按现有快麦登录流程完成本机登录。登录状态会保存在新 Mac 的钥匙串和项目运行目录中。
5. 在窗口中确认商品列表，勾选一个后点“单个执行”，或勾选多个后点“批量执行所选”。
6. 批量任务按列表顺序串行运行；窗口中的报告路径和 `output/kuaimai/runs/` 可用于复核。

点击执行按钮会真实保存并铺货。共享盘未挂载、商品缺少 `产品信息.xlsx`、登录未完成或脚本报错时，先根据窗口日志处理，不要重复点击已经启动的商品。
```

Run:

```bash
chmod +x run-gui.command
python3 -m unittest tests/test_kuaimai_gui_launcher.py -v
```

Expected: 启动器测试 PASS，且 `run-gui.command` 具备执行权限。

- [ ] **Step 5: Commit the migration entrypoint and docs**

```bash
git add run-gui.command docs/kuaimai-gui-migration.md tests/test_kuaimai_gui_launcher.py
git commit -m "feat: add portable kuaimai gui launcher"
```

### Task 5: 全量验证与工作区边界复查

**Files:**
- Verify only: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/kuaimai_gui.py`
- Verify only: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/run-gui.command`
- Verify only: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/tests/test_kuaimai_gui.py`
- Verify only: `/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/tests/test_kuaimai_gui_launcher.py`

- [ ] **Step 1: Run focused GUI tests**

```bash
python3 -m unittest tests/test_kuaimai_gui.py tests/test_kuaimai_gui_launcher.py -v
```

Expected: all focused tests PASS.

- [ ] **Step 2: Run syntax and launcher checks**

```bash
python3 -m py_compile kuaimai_gui.py
zsh -n run-gui.command
git diff --check
```

Expected: all commands exit 0 and produce no syntax or whitespace errors.

- [ ] **Step 3: Run the existing launcher regression tests**

```bash
python3 -m unittest tests/test_launchers.py -v
```

Expected: existing launcher tests remain PASS; `run.command` is unchanged by this feature.

- [ ] **Step 4: Perform a non-publishing GUI smoke check**

在临时 `products` 根目录中创建两个带空 `产品信息.xlsx` 的测试商品目录，调用 `discover_products` 和 `run_batch` 的 fake runner，确认单选队列长度为 1，多选队列顺序与界面列表一致；不得运行 `--save` 的真实商品任务。

- [ ] **Step 5: Review the final diff and report migration limits**

```bash
git status --short
git diff HEAD~4 --stat
```

确认新增提交只包含 GUI 相关文件，未覆盖用户原有未提交修改；最终说明真实 Mac 验收仍需要共享盘挂载、Tkinter 可用、首次登录和至少一个商品的实际窗口验证。
