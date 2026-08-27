from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Dict, Iterator, List, Tuple


MIN_OCR_CONFIDENCE = 0.25
_TOKEN_FIELDS = {"text", "confidence", "x", "y", "width", "height"}
_DUPLICATE_MODULE_SIGNATURE = "redefinition of module 'SwiftBridging'"
_TOOL_TIMEOUT_SECONDS = 60


class RecognitionError(RuntimeError):
    pass


class _CacheUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRToken:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class _Toolchain:
    swiftc: Path
    version: str
    target_info: str
    resource_path: Path
    identity: str


def _run_metadata(command: List[str], label: str) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise RecognitionError(f"读取 Swift {label}超时（10 秒）") from error
    except OSError as error:
        raise RecognitionError(f"无法读取 Swift {label}：{error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"退出码 {result.returncode}"
        raise RecognitionError(f"无法读取 Swift {label}：{detail}")
    value = result.stdout.strip()
    if not value:
        raise RecognitionError(f"Swift {label}输出为空")
    return value


def _active_toolchain() -> _Toolchain:
    swiftc_text = _run_metadata(["xcrun", "--find", "swiftc"], "编译器路径")
    version = _run_metadata(["xcrun", "swiftc", "--version"], "编译器版本")
    target_info = _run_metadata(["xcrun", "swiftc", "-print-target-info"], "目标信息")
    try:
        metadata = json.loads(target_info)
        resource_text = metadata["paths"]["runtimeResourcePath"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RecognitionError("Swift 目标信息缺少 runtimeResourcePath") from error
    if not isinstance(resource_text, str) or not resource_text:
        raise RecognitionError("Swift runtimeResourcePath 格式无效")

    swiftc = Path(swiftc_text)
    resource_path = Path(resource_text)
    identity_source = "\0".join((str(swiftc), version, target_info))
    identity = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()
    return _Toolchain(swiftc, version, target_info, resource_path, identity)


def _numeric_version(path: Path) -> Tuple[int, ...]:
    numbers = tuple(int(part) for part in re.findall(r"\d+", path.parent.name))
    return numbers or (0,)


@contextmanager
def _duplicate_module_workaround(toolchain: _Toolchain) -> Iterator[List[str]]:
    resource_path = toolchain.resource_path
    if resource_path.name != "swift" or resource_path.parent.name != "lib":
        raise RecognitionError(
            f"Swift OCR 编译失败：无法从当前工具链解析资源路径 {resource_path}"
        )
    toolchain_usr = resource_path.parents[1]
    swift_include = toolchain_usr / "include" / "swift"
    clang_candidates = [
        path
        for path in (toolchain_usr / "lib" / "clang").glob("*/include")
        if path.is_dir()
    ]
    required = [swift_include / "bridging.modulemap", swift_include / "bridging"]
    if not resource_path.is_dir() or not all(path.is_file() for path in required) or not clang_candidates:
        raise RecognitionError("Swift OCR 编译失败：当前工具链无法应用模块映射兼容处理")

    clang_include = max(clang_candidates, key=_numeric_version)
    try:
        with tempfile.TemporaryDirectory(prefix="vision-ocr-swift-") as temporary_directory:
            shadow = Path(temporary_directory)
            (shadow / "lib").mkdir()
            shadow_include = shadow / "include" / "swift"
            shadow_include.mkdir(parents=True)
            (shadow / "lib" / "swift").symlink_to(resource_path, target_is_directory=True)
            for source in required:
                (shadow_include / source.name).symlink_to(source)
            yield [
                "-resource-dir",
                str(shadow / "lib" / "swift"),
                "-Xcc",
                "-isystem",
                "-Xcc",
                str(clang_include),
            ]
    except OSError as error:
        raise RecognitionError(
            f"Swift OCR 编译失败：无法建立工具链兼容目录：{error}"
        ) from error


def _run_compile(command: List[str]):
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RecognitionError("Swift OCR 编译超时（60 秒）") from error
    except OSError as error:
        raise RecognitionError(f"无法启动本地 Swift OCR 编译器：{error}") from error


def _compile_bridge(script_path: Path, output_path: Path, toolchain: _Toolchain) -> None:
    base_command = ["xcrun", "swiftc"]
    normal_command = base_command + [str(script_path), "-o", str(output_path)]
    result = _run_compile(normal_command)
    if result.returncode != 0 and _DUPLICATE_MODULE_SIGNATURE in result.stderr:
        with _duplicate_module_workaround(toolchain) as compatibility_flags:
            retry_command = base_command + compatibility_flags + [
                str(script_path),
                "-o",
                str(output_path),
            ]
            result = _run_compile(retry_command)

    if result.returncode != 0:
        detail = result.stderr.strip() or f"退出码 {result.returncode}"
        if len(detail) > 2000:
            detail = detail[:2000] + "…"
        raise RecognitionError(f"Swift OCR 编译失败：{detail}")
    if not output_path.is_file():
        raise RecognitionError("Swift OCR 编译失败：编译器未生成可执行文件")


def _cache_directory() -> Path:
    return Path.home() / "Library" / "Caches" / "KuaimaiAutomation" / "vision-ocr"


def _cached_bridge(script_path: Path) -> Path:
    toolchain = _active_toolchain()
    try:
        source = script_path.read_bytes()
    except OSError as error:
        raise RecognitionError(f"无法读取 Swift OCR 脚本：{error}") from error
    cache_key = hashlib.sha256(source + toolchain.identity.encode("ascii")).hexdigest()
    cache_directory = _cache_directory()
    binary_path = cache_directory / f"vision-ocr-{cache_key}"
    lock_path = cache_directory / f"vision-ocr-{cache_key}.lock"

    try:
        cache_directory.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
    except OSError as error:
        raise _CacheUnavailable(str(error)) from error

    temporary_path = None
    with lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if binary_path.is_file() and os.access(binary_path, os.X_OK):
                return binary_path
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{binary_path.name}.",
                dir=str(cache_directory),
            )
            os.close(descriptor)
            temporary_path = Path(temporary_name)
            temporary_path.unlink()
            _compile_bridge(script_path, temporary_path, toolchain)
            temporary_path.chmod(0o700)
            os.replace(temporary_path, binary_path)
            temporary_path = None
            return binary_path
        except RecognitionError:
            raise
        except OSError as error:
            raise _CacheUnavailable(str(error)) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


@contextmanager
def _bridge_command(image_path: Path) -> Iterator[List[str]]:
    script_path = Path(__file__).with_name("scripts") / "vision_ocr.swift"
    try:
        binary_path = _cached_bridge(script_path)
        yield [str(binary_path), str(image_path)]
        return
    except _CacheUnavailable:
        pass

    toolchain = _active_toolchain()
    try:
        with tempfile.TemporaryDirectory(prefix="vision-ocr-binary-") as temporary_directory:
            binary_path = Path(temporary_directory) / "vision_ocr"
            _compile_bridge(script_path, binary_path, toolchain)
            binary_path.chmod(0o700)
            yield [str(binary_path), str(image_path)]
    except OSError as error:
        raise RecognitionError(f"无法建立临时 Swift OCR 可执行文件：{error}") from error


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
    if not isinstance(item, dict):
        raise RecognitionError(f"OCR 输出格式无效：第 {index + 1} 项必须是对象")
    missing = sorted(_TOKEN_FIELDS - set(item))
    unexpected = sorted(set(item) - _TOKEN_FIELDS)
    if missing or unexpected:
        details = []
        if missing:
            details.append("缺少字段：" + ", ".join(missing))
        if unexpected:
            details.append("未知字段：" + ", ".join(unexpected))
        raise RecognitionError(f"OCR 输出格式无效：第 {index + 1} 项" + "；".join(details))

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

    try:
        with _bridge_command(image_path) as command:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=_TOOL_TIMEOUT_SECONDS,
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
