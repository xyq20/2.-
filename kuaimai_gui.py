"""Tkinter launcher for selecting and running Kuaimai product folders."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


PRODUCT_EXCEL_NAME = "产品信息.xlsx"
PROJECT_DIR = Path(__file__).resolve().parent
ERP_SCRIPT = PROJECT_DIR / "kuaimai_erp.py"
GUI_CONFIG_PATH = PROJECT_DIR / "output" / "kuaimai" / "gui-config.json"


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
StartCallback = Callable[[int, int, ProductChoice], None]


def run_batch(
    products: Sequence[ProductChoice],
    runner: Runner,
    stop_event: threading.Event,
    emit: Optional[Callable[[str], None]] = None,
    on_start: Optional[StartCallback] = None,
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
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        emit(line.rstrip())
    process.stdout.close()
    return process.wait()


def load_configured_products_root(
    config_path: Path = GUI_CONFIG_PATH,
) -> Optional[Path]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("products_root") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def save_configured_products_root(
    products_root: Path,
    config_path: Path = GUI_CONFIG_PATH,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {"products_root": str(Path(products_root).expanduser())},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


class KuaimaiGuiApp:
    """Tkinter window for selecting one or more products to publish."""

    def __init__(self, root: Any, products_root: Optional[Path] = None) -> None:
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.ScrolledText = ScrolledText
        self.products_root = products_root or load_configured_products_root()
        self.products: Tuple[ProductChoice, ...] = ()
        self.check_vars: Dict[Path, Any] = {}
        self.status_vars: Dict[Path, Any] = {}
        self.checkbuttons: List[Any] = []
        self.ui_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False

        self.path_var = tk.StringVar(value="正在扫描共享盘……")
        self.selection_var = tk.StringVar(value="已选择 0 个商品")
        self.current_var = tk.StringVar(value="当前没有运行任务")
        self.progress_var = tk.StringVar(value="等待开始")

        self._build_ui()
        self.refresh_products()
        self.root.after(100, self.poll_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        self.root.title("快麦一键铺货")
        self.root.geometry("920x700")
        self.root.minsize(760, 560)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(3, weight=1)

        path_frame = ttk.Frame(self.root, padding=(12, 12, 12, 6))
        path_frame.grid(row=0, column=0, sticky="ew")
        path_frame.columnconfigure(1, weight=1)
        ttk.Label(path_frame, text="共享盘 products：").grid(row=0, column=0, sticky="w")
        self.path_entry = ttk.Entry(
            path_frame,
            textvariable=self.path_var,
            state="readonly",
        )
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self.refresh_button = ttk.Button(
            path_frame,
            text="刷新",
            command=self.refresh_products,
        )
        self.refresh_button.grid(row=0, column=2, padx=(0, 6))
        self.choose_button = ttk.Button(
            path_frame,
            text="选择目录",
            command=self.choose_products_root,
        )
        self.choose_button.grid(row=0, column=3)

        list_frame = ttk.LabelFrame(
            self.root,
            text="选择商品文件夹（可勾选一个或多个）",
            padding=(8, 6, 8, 8),
        )
        list_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.product_canvas = tk.Canvas(list_frame, highlightthickness=0)
        self.product_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.product_canvas.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.product_canvas.configure(yscrollcommand=scrollbar.set)
        self.product_list_frame = ttk.Frame(self.product_canvas)
        self.product_canvas_window = self.product_canvas.create_window(
            (0, 0),
            window=self.product_list_frame,
            anchor="nw",
        )
        self.product_list_frame.bind(
            "<Configure>",
            lambda _event: self.product_canvas.configure(
                scrollregion=self.product_canvas.bbox("all")
            ),
        )
        self.product_canvas.bind(
            "<Configure>",
            lambda event: self.product_canvas.itemconfigure(
                self.product_canvas_window,
                width=event.width,
            ),
        )

        selection_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        selection_frame.grid(row=2, column=0, sticky="ew")
        selection_frame.columnconfigure(3, weight=1)
        self.select_all_button = ttk.Button(
            selection_frame,
            text="全选",
            command=self.select_all,
        )
        self.select_all_button.grid(row=0, column=0, padx=(0, 6))
        self.clear_button = ttk.Button(
            selection_frame,
            text="清空",
            command=self.clear_selection,
        )
        self.clear_button.grid(row=0, column=1, padx=(0, 12))
        ttk.Label(selection_frame, textvariable=self.selection_var).grid(
            row=0,
            column=2,
            sticky="w",
        )

        action_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        action_frame.grid(row=3, column=0, sticky="ew")
        action_frame.columnconfigure(2, weight=1)
        self.single_button = ttk.Button(
            action_frame,
            text="单个执行",
            command=self.single_execute,
        )
        self.single_button.grid(row=0, column=0, padx=(0, 6))
        self.batch_button = ttk.Button(
            action_frame,
            text="批量执行所选",
            command=self.batch_execute,
        )
        self.batch_button.grid(row=0, column=1, padx=(0, 12))
        self.stop_button = ttk.Button(
            action_frame,
            text="停止后续任务",
            command=self.stop_remaining,
            state="disabled",
        )
        self.stop_button.grid(row=0, column=3, sticky="e")

        status_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        status_frame.grid(row=4, column=0, sticky="ew")
        status_frame.columnconfigure(1, weight=1)
        ttk.Label(status_frame, text="当前：").grid(row=0, column=0, sticky="w")
        ttk.Label(status_frame, textvariable=self.current_var).grid(
            row=0,
            column=1,
            sticky="w",
        )
        ttk.Label(status_frame, textvariable=self.progress_var).grid(
            row=1,
            column=1,
            sticky="w",
        )

        log_frame = ttk.LabelFrame(
            self.root,
            text="运行日志",
            padding=(8, 6, 8, 8),
        )
        log_frame.grid(row=5, column=0, sticky="nsew", padx=12, pady=(0, 12))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.root.rowconfigure(5, weight=1)
        self.log_text = self.ScrolledText(
            log_frame,
            height=12,
            state="disabled",
            wrap="word",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.selection_controls = [
            self.refresh_button,
            self.choose_button,
            self.select_all_button,
            self.clear_button,
            self.single_button,
            self.batch_button,
        ]

    def rebuild_product_checkboxes(self) -> None:
        previous_selection = {
            product_path
            for product_path, variable in self.check_vars.items()
            if variable.get()
        }
        for child in self.product_list_frame.winfo_children():
            child.destroy()
        self.check_vars.clear()
        self.status_vars.clear()
        self.checkbuttons.clear()

        if not self.products:
            self.ttk.Label(
                self.product_list_frame,
                text="未找到包含 产品信息.xlsx 的商品文件夹。请先挂载共享盘，或点击“选择目录”。",
            ).grid(row=0, column=0, sticky="w", padx=8, pady=12)
            self.update_selection_label()
            return

        for row, product in enumerate(self.products):
            variable = self.tk.BooleanVar(value=product.directory in previous_selection)
            status_var = self.tk.StringVar(value="待执行")
            self.check_vars[product.directory] = variable
            self.status_vars[product.directory] = status_var
            checkbutton = self.ttk.Checkbutton(
                self.product_list_frame,
                text=product.display_name,
                variable=variable,
                command=self.update_selection_label,
            )
            checkbutton.grid(row=row, column=0, sticky="w", padx=8, pady=4)
            self.ttk.Label(
                self.product_list_frame,
                textvariable=status_var,
                width=12,
                anchor="e",
            ).grid(row=row, column=1, sticky="e", padx=8, pady=4)
            self.ttk.Label(
                self.product_list_frame,
                text=str(product.excel_path),
                foreground="#666666",
            ).grid(row=row, column=2, sticky="w", padx=8, pady=4)
            self.checkbuttons.append(checkbutton)
        self.product_list_frame.columnconfigure(2, weight=1)
        self.update_selection_label()

    def refresh_products(self) -> None:
        if self.running:
            return
        root = discover_products_root(self.products_root)
        self.products_root = root
        self.products = discover_products(root) if root else ()
        self.path_var.set(str(root) if root else "未找到共享盘 products 目录")
        self.rebuild_product_checkboxes()

    def choose_products_root(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择共享盘 products 文件夹",
        )
        if not selected:
            return
        self.products_root = Path(selected).expanduser().resolve(strict=False)
        try:
            save_configured_products_root(self.products_root)
        except OSError as exc:
            self.append_log(f"共享盘路径已使用，但配置保存失败：{exc}")
        self.refresh_products()

    def selected_products(self) -> Tuple[ProductChoice, ...]:
        selected_paths = {
            product_path
            for product_path, variable in self.check_vars.items()
            if variable.get()
        }
        return tuple(
            product for product in self.products if product.directory in selected_paths
        )

    def update_selection_label(self) -> None:
        count = sum(1 for variable in self.check_vars.values() if variable.get())
        self.selection_var.set(f"已选择 {count} 个商品")

    def select_all(self) -> None:
        if self.running:
            return
        for variable in self.check_vars.values():
            variable.set(True)
        self.update_selection_label()

    def clear_selection(self) -> None:
        if self.running:
            return
        for variable in self.check_vars.values():
            variable.set(False)
        self.update_selection_label()

    def single_execute(self) -> None:
        selected = self.selected_products()
        if len(selected) != 1:
            self.show_warning("单个执行需要且只能勾选 1 个商品")
            return
        self.start_execution(selected, "单个执行")

    def batch_execute(self) -> None:
        selected = self.selected_products()
        if not selected:
            self.show_warning("请至少勾选 1 个商品")
            return
        self.start_execution(selected, f"批量执行 {len(selected)} 个商品")

    def start_execution(
        self,
        selected: Sequence[ProductChoice],
        label: str,
    ) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.set_controls_enabled(False)
        for product in selected:
            self.status_vars[product.directory].set("排队中")
        self.current_var.set(label)
        self.progress_var.set(f"已选择 {len(selected)} 个商品，等待启动")
        self.append_log(f"开始{label}；点击执行按钮即表示确认真实保存并铺货。")
        worker = threading.Thread(
            target=self.worker_main,
            args=(tuple(selected),),
            daemon=True,
        )
        worker.start()

    def worker_main(self, selected: Tuple[ProductChoice, ...]) -> None:
        try:
            results = run_batch(
                selected,
                run_product_process,
                self.stop_event,
                emit=lambda line: self.ui_queue.put(("log", line)),
                on_start=lambda index, total, product: self.ui_queue.put(
                    ("started", index, total, product.display_name)
                ),
            )
        except Exception as exc:
            self.ui_queue.put(("worker_error", str(exc)))
            return
        self.ui_queue.put(("complete", results))

    def poll_ui_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.append_log(str(event[1]))
                elif kind == "started":
                    _kind, index, total, display_name = event
                    self.current_var.set(f"正在执行：{display_name}")
                    self.progress_var.set(f"当前第 {index + 1}/{total} 个商品")
                elif kind == "complete":
                    self.finish_execution(event[1])
                elif kind == "worker_error":
                    self.append_log(f"GUI 执行线程异常：{event[1]}")
                    self.finish_execution(())
        except queue.Empty:
            pass
        self.root.after(100, self.poll_ui_queue)

    def finish_execution(self, results: Sequence[BatchItemResult]) -> None:
        completed = failed = not_started = 0
        for result in results:
            status_var = self.status_vars.get(result.product.directory)
            if status_var is not None:
                status_var.set(result.status.value)
            if result.status == BatchStatus.COMPLETED:
                completed += 1
            elif result.status == BatchStatus.FAILED:
                failed += 1
                if result.error:
                    self.append_log(f"{result.product.display_name}：{result.error}")
            else:
                not_started += 1

        self.running = False
        self.set_controls_enabled(True)
        self.current_var.set("任务结束")
        self.progress_var.set(
            f"完成/已提交 {completed} 个，失败 {failed} 个，未启动 {not_started} 个"
        )
        self.append_log(
            f"批量任务结束：完成/已提交 {completed} 个，失败 {failed} 个，未启动 {not_started} 个。"
        )

    def stop_remaining(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.append_log("已请求停止后续任务；当前正在运行的商品会先完成。")

    def set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in self.selection_controls:
            widget.configure(state=state)
        for checkbutton in self.checkbuttons:
            checkbutton.configure(state=state)
        self.stop_button.configure(state="disabled" if enabled else "normal")

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def show_warning(self, message: str) -> None:
        from tkinter import messagebox

        messagebox.showwarning("快麦一键铺货", message, parent=self.root)

    def on_close(self) -> None:
        if self.running:
            self.show_warning("当前有商品正在执行，请先点击“停止后续任务”并等待当前商品结束。")
            return
        self.root.destroy()


def main() -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        print(
            "当前 Python 没有 Tkinter，请安装带 Tk 支持的 Python 3 后重试。",
            file=sys.stderr,
        )
        print(f"详细错误：{exc}", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("快麦一键铺货")
    KuaimaiGuiApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
