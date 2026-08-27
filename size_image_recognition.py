from dataclasses import dataclass
from contextlib import contextmanager
import json
import math
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Dict, Tuple


MIN_OCR_CONFIDENCE = 0.25
_TOKEN_FIELDS = {"text", "confidence", "x", "y", "width", "height"}


class RecognitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRToken:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


@contextmanager
def _swift_command(script_path: Path, image_path: Path):
    """Yield the Swift command, isolating a known stale CLT module-map conflict."""
    command = ["xcrun", "swift"]
    toolchain_usr = Path("/Library/Developer/CommandLineTools/usr")
    swift_include = toolchain_usr / "include" / "swift"
    swift_resource = toolchain_usr / "lib" / "swift"
    clang_roots = sorted((toolchain_usr / "lib" / "clang").glob("*/include"), reverse=True)
    has_conflicting_maps = all(
        (swift_include / name).is_file()
        for name in ("module.modulemap", "bridging.modulemap", "bridging")
    )

    if not has_conflicting_maps or not swift_resource.is_dir() or not clang_roots:
        yield command + [str(script_path), str(image_path)]
        return

    with tempfile.TemporaryDirectory(prefix="vision-ocr-swift-") as temporary_directory:
        toolchain_shadow = Path(temporary_directory)
        (toolchain_shadow / "lib").mkdir()
        shadow_include = toolchain_shadow / "include" / "swift"
        shadow_include.mkdir(parents=True)
        (toolchain_shadow / "lib" / "swift").symlink_to(swift_resource, target_is_directory=True)
        for name in ("bridging.modulemap", "bridging"):
            (shadow_include / name).symlink_to(swift_include / name)
        yield command + [
            "-resource-dir",
            str(toolchain_shadow / "lib" / "swift"),
            "-Xcc",
            "-isystem",
            "-Xcc",
            str(clang_roots[0]),
            str(script_path),
            str(image_path),
        ]


def _finite_number(item: Dict[str, Any], field: str, index: int) -> float:
    value = item[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecognitionError(f"OCR 输出格式无效：第 {index + 1} 项的 {field} 不是数字")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise RecognitionError(
            f"OCR 输出格式无效：第 {index + 1} 项的 {field} 必须是 0 到 1 的有限数"
        )
    return number


def _parse_token(item: Any, index: int) -> OCRToken:
    if not isinstance(item, dict) or set(item) != _TOKEN_FIELDS:
        raise RecognitionError(f"OCR 输出格式无效：第 {index + 1} 项字段不完整")

    text = item["text"]
    if not isinstance(text, str) or not text.strip():
        raise RecognitionError(f"OCR 输出格式无效：第 {index + 1} 项 text 必须是非空文本")

    confidence = _finite_number(item, "confidence", index)
    x = _finite_number(item, "x", index)
    y = _finite_number(item, "y", index)
    width = _finite_number(item, "width", index)
    height = _finite_number(item, "height", index)
    if x + width > 1.000001 or y + height > 1.000001:
        raise RecognitionError(f"OCR 输出格式无效：第 {index + 1} 项坐标框超出图片范围")

    return OCRToken(text.strip(), confidence, x, y, width, height)


def vision_ocr(image_path: Path) -> Tuple[OCRToken, ...]:
    image_path = Path(image_path)
    if not image_path.is_file():
        raise RecognitionError(f"OCR 图片不存在或不是文件：{image_path}")

    script_path = Path(__file__).with_name("scripts") / "vision_ocr.swift"
    try:
        with _swift_command(script_path, image_path) as command:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
    except subprocess.TimeoutExpired as error:
        raise RecognitionError("macOS Vision OCR 执行超时（60 秒）") from error
    except OSError as error:
        raise RecognitionError(f"无法启动本地 macOS Vision OCR：{error}") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or f"退出码 {result.returncode}"
        raise RecognitionError(f"macOS Vision OCR 执行失败：{detail}")

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise RecognitionError("OCR 输出不是有效 JSON") from error
    if not isinstance(payload, list):
        raise RecognitionError("OCR 输出 JSON 顶层必须是数组")

    parsed_tokens = tuple(_parse_token(item, index) for index, item in enumerate(payload))
    tokens = tuple(token for token in parsed_tokens if token.confidence >= MIN_OCR_CONFIDENCE)
    if not tokens:
        raise RecognitionError(f"OCR 未识别到可信文字：{image_path}")
    return tokens
