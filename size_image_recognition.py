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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import cv2
import numpy as np


MIN_OCR_CONFIDENCE = 0.25
_TOKEN_FIELDS = {"text", "confidence", "x", "y", "width", "height"}
_DUPLICATE_MODULE_SIGNATURE = "redefinition of module 'SwiftBridging'"
_SDK_MISMATCH_SIGNATURE = "this SDK is not supported by the compiler"
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


Number = Union[int, float]


@dataclass(frozen=True)
class SizeMeasurements:
    waist: Number
    hip: Number
    length: Number
    foot_opening: Optional[Number] = None


@dataclass(frozen=True)
class SkuRecommendation:
    size: str
    height_min: Number
    height_max: Number
    weight_min: Number
    weight_max: Number
    waist: Number
    hip: Number
    length: Number


@dataclass(frozen=True)
class ClothingSkuRecommendation:
    size: str
    height_min: Number
    height_max: Number
    weight_min: Number
    weight_max: Number
    length: Number
    chest: Number
    shoulder: Number
    sleeve: Number


@dataclass(frozen=True)
class SizeLength:
    size: str
    length: Number


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


def _default_sdk_path() -> Optional[Path]:
    try:
        result = subprocess.run(
            ["xcrun", "--show-sdk-path"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def _compatible_sdk_candidates(default_sdk: Optional[Path]) -> List[Path]:
    try:
        resolved_default = default_sdk.resolve() if default_sdk else None
    except OSError:
        resolved_default = default_sdk
    directories = {
        path
        for directory in (
            Path("/Library/Developer/CommandLineTools/SDKs"),
            Path("/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs"),
        )
        if directory.is_dir()
        for path in directory.glob("MacOSX*.sdk")
    }
    candidates = []
    for path in directories:
        try:
            if resolved_default is not None and path.resolve() == resolved_default:
                continue
        except OSError:
            pass
        if (path / "usr" / "lib" / "swift" / "Swift.swiftmodule").is_dir():
            candidates.append(path)
    return sorted(candidates, key=_numeric_version, reverse=True)


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

    if result.returncode != 0 and _SDK_MISMATCH_SIGNATURE in result.stderr:
        default_sdk = _default_sdk_path()
        for sdk_path in _compatible_sdk_candidates(default_sdk):
            retry_command = base_command + ["-sdk", str(sdk_path), str(script_path), "-o", str(output_path)]
            result = _run_compile(retry_command)
            if result.returncode == 0:
                break

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


_PLAIN_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
_SIZE_NAME = re.compile(r"^(?:XS|S|M|L|X{1,6}L|\d{1,2}XL)$", re.IGNORECASE)
_MEASUREMENT_ALIASES = {
    "waist": ("腰围", "WAISTLINE", "WAIST"),
    "length": ("裤长", "LENGTH", "PANTSLENGTH", "TROUSERLENGTH"),
    "hip": ("臀围", "HIPLINE", "HIPS", "HIP"),
    "foot_opening": ("脚围", "裤脚围", "FOOTOPENING", "LEGOPENING"),
}
_REQUIRED_MEASUREMENTS = ("waist", "length", "hip")
_MEASUREMENT_NAMES = {
    "waist": "腰围",
    "length": "裤长",
    "hip": "臀围",
    "foot_opening": "脚围",
}

_CLOTHING_MEASUREMENT_ALIASES = {
    "length": ("衣长", "LENGTH", "GARMENTLENGTH", "BODYLENGTH", "BACKLENGTH"),
    "chest": ("胸围", "BUST", "CHEST"),
    "shoulder": ("肩宽", "SHOULDERWIDTH", "SHOULDER"),
    "sleeve": ("袖长", "SLEEVELENGTH", "SLEEVE"),
}
_CLOTHING_MEASUREMENT_NAMES = {
    "length": "衣长",
    "chest": "胸围",
    "shoulder": "肩宽",
    "sleeve": "袖长",
}

_TAOBAO_LENGTH_ALIASES = {
    "pants": ("裤长", "LENGTH", "PANTSLENGTH", "TROUSERLENGTH"),
    "clothing": ("衣长", "LENGTH", "GARMENTLENGTH", "BODYLENGTH", "BACKLENGTH"),
}


def _center_x(token: OCRToken) -> float:
    return token.x + token.width / 2.0


def _center_y(token: OCRToken) -> float:
    return token.y + token.height / 2.0


def _normalize_size(text: str) -> Optional[str]:
    normalized = re.sub(r"\s+", "", text).upper()
    return normalized if _SIZE_NAME.fullmatch(normalized) else None


def _expected_size_map(expected_sizes: Sequence[str], source: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for raw_size in expected_sizes:
        if not isinstance(raw_size, str):
            raise RecognitionError(f"{source}：Excel 尺码必须是文本")
        size = _normalize_size(raw_size)
        if size is None:
            raise RecognitionError(f"{source}：Excel 尺码无效：{raw_size!r}")
        if size in result:
            raise RecognitionError(f"{source}：Excel 尺码重复：{size}")
        result[size] = raw_size.strip()
    if not result:
        raise RecognitionError(f"{source}：Excel 尺码为空")
    return result


def parse_size_names(
    tokens: Tuple[OCRToken, ...],
    garment_kind: str,
    *,
    source: str = "尺码信息表",
) -> Tuple[str, ...]:
    """Discover one ordered letter-size header from a garment length table.

    This is only a fallback for workbooks without a ``尺码`` row.  It requires a
    unique pants/clothing length label and at least two same-row size headers;
    ambiguous OCR is rejected instead of inventing a size list.
    """

    aliases = _TAOBAO_LENGTH_ALIASES.get(garment_kind)
    if aliases is None:
        raise RecognitionError(f"不支持从尺码表推导该品类尺码：{garment_kind!r}")
    label_tokens = [
        token for token in tokens if _matches_alias_composition(token.text, aliases)
    ]
    if len(label_tokens) != 1:
        display_name = "裤长" if garment_kind == "pants" else "衣长"
        raise RecognitionError(
            f"{source}：{display_name}行标题匹配数为 {len(label_tokens)}，无法安全推导尺码"
        )
    label = label_tokens[0]
    candidates = [
        (size, token)
        for token in tokens
        for size in [_normalize_size(token.text)]
        if size is not None
        and _center_y(token) < _center_y(label)
        and _center_x(token) > label.x + label.width
    ]
    if len(candidates) < 2:
        raise RecognitionError(f"{source}：尺码表头精确尺码少于 2 个")

    # Keep only the closest horizontal header row above the length label.
    ordered_by_y = sorted(candidates, key=lambda item: _center_y(item[1]))
    rows: List[List[Tuple[str, OCRToken]]] = []
    for item in ordered_by_y:
        if not rows or abs(_center_y(item[1]) - _center_y(rows[-1][0][1])) > 0.04:
            rows.append([item])
        else:
            rows[-1].append(item)
    viable = [row for row in rows if len(row) >= 2]
    if not viable:
        raise RecognitionError(f"{source}：找不到同一行的尺码表头")
    closest_y = max(_center_y(row[0][1]) for row in viable)
    closest = [row for row in viable if abs(_center_y(row[0][1]) - closest_y) <= 0.001]
    if len(closest) != 1:
        raise RecognitionError(f"{source}：尺码表头位置不唯一")

    result: List[str] = []
    for size, _token in sorted(closest[0], key=lambda item: _center_x(item[1])):
        if size in result:
            raise RecognitionError(f"{source}：尺码列标题重复：{size}")
        result.append(size)
    return tuple(result)


def recognize_size_names(
    size_chart_path: Path,
    garment_kind: str,
) -> Tuple[str, ...]:
    size_chart_path = Path(size_chart_path)
    return parse_size_names(
        vision_ocr(size_chart_path), garment_kind, source=str(size_chart_path)
    )


def _require_size_set(actual: Sequence[str], expected: Sequence[str], source: str) -> None:
    actual_set = set(actual)
    expected_set = set(expected)
    if actual_set == expected_set:
        return
    details = []
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    if missing:
        details.append("缺少：" + ", ".join(missing))
    if extra:
        details.append("多出：" + ", ".join(extra))
    raise RecognitionError(f"{source} 尺码集合不一致（{' ；'.join(details)}）")


def _number(text: str) -> Optional[Number]:
    compact = text.strip().replace(",", "")
    if not _PLAIN_NUMBER.fullmatch(compact):
        return None
    value = float(compact)
    if not math.isfinite(value):
        return None
    return int(value) if value.is_integer() else value


def _measurement_kind(text: str) -> Optional[str]:
    compact = re.sub(r"[^A-Z\u4e00-\u9fff]", "", text.upper())
    if not compact or len(compact) > 32:
        return None

    def is_alias_composition(aliases: Sequence[str]) -> bool:
        reachable = {0}
        for start in range(len(compact)):
            if start not in reachable:
                continue
            for alias in aliases:
                if compact.startswith(alias, start):
                    reachable.add(start + len(alias))
        return len(compact) in reachable

    matches = [
        kind
        for kind, aliases in _MEASUREMENT_ALIASES.items()
        if is_alias_composition(aliases)
    ]
    return matches[0] if len(matches) == 1 else None


def _matches_alias_composition(text: str, aliases: Sequence[str]) -> bool:
    compact = re.sub(r"[^A-Z\u4e00-\u9fff]", "", text.upper())
    if not compact or len(compact) > 32:
        return False
    reachable = {0}
    for start in range(len(compact)):
        if start not in reachable:
            continue
        for alias in aliases:
            if compact.startswith(alias, start):
                reachable.add(start + len(alias))
    return len(compact) in reachable


def parse_size_lengths(
    tokens: Tuple[OCRToken, ...],
    expected_sizes: Sequence[str],
    garment_kind: str,
    *,
    source: str = "尺码信息表",
) -> Tuple[SizeLength, ...]:
    """从尺码信息表中只读取淘宝尺码表需要的衣长或裤长一行。"""
    expected_map = _expected_size_map(expected_sizes, source)
    aliases = _TAOBAO_LENGTH_ALIASES.get(garment_kind)
    if aliases is None:
        raise RecognitionError(f"不支持的淘宝尺码类型：{garment_kind!r}")
    display_name = "裤长" if garment_kind == "pants" else "衣长"
    label_tokens = [
        token for token in tokens if _matches_alias_composition(token.text, aliases)
    ]
    if len(label_tokens) != 1:
        raise RecognitionError(
            f"{source}：{display_name}行标题匹配数为 {len(label_tokens)}"
        )
    label = label_tokens[0]
    label_y = _center_y(label)
    label_right = label.x + label.width

    header_candidates = [
        (size, token)
        for token in tokens
        for size in [_normalize_size(token.text)]
        if size in expected_map
        and _center_y(token) < label_y
        and _center_x(token) > label_right
    ]
    headers: Dict[str, OCRToken] = {}
    for size, token in header_candidates:
        if size in headers:
            raise RecognitionError(f"{source}：尺码列标题重复：{size}")
        headers[size] = token
    _require_size_set(tuple(headers), tuple(expected_map), source)

    columns = sorted(headers.items(), key=lambda item: _center_x(item[1]))
    column_bounds = _cell_bounds([_center_x(token) for _size, token in columns])
    first_column_x = min(_center_x(token) for _size, token in columns)
    header_bottom = max(token.y + token.height for _size, token in columns)
    row_labels = sorted(
        (
            token
            for token in tokens
            if _number(token.text) is None
            and _normalize_size(token.text) is None
            and _center_x(token) < first_column_x
            and _center_y(token) > header_bottom
        ),
        key=_center_y,
    )
    label_index = min(
        range(len(row_labels)),
        key=lambda index: abs(_center_y(row_labels[index]) - label_y),
    )
    if row_labels[label_index] is not label:
        raise RecognitionError(f"{source}：无法唯一定位{display_name}数据行")
    previous_y = _center_y(row_labels[label_index - 1]) if label_index else None
    next_y = (
        _center_y(row_labels[label_index + 1])
        if label_index + 1 < len(row_labels)
        else None
    )
    top = (previous_y + label_y) / 2 if previous_y is not None else label_y - 0.08
    bottom = (label_y + next_y) / 2 if next_y is not None else label_y + 0.08

    numeric_tokens = [
        (token, value)
        for token in tokens
        for value in [_number(token.text)]
        if value is not None and top <= _center_y(token) < bottom
    ]
    values: Dict[str, Number] = {}
    for (size, _header), (left, right) in zip(columns, column_bounds):
        matches = [
            value
            for token, value in numeric_tokens
            if left <= _center_x(token) < right
        ]
        if len(matches) != 1:
            raise RecognitionError(
                f"{source}：{size} {display_name}单元格匹配数为 {len(matches)}"
            )
        value = matches[0]
        if value <= 0:
            raise RecognitionError(f"{source}：{size} {display_name}必须是正数")
        values[size] = value
    return tuple(SizeLength(expected_map[size], values[size]) for size in expected_map)


def recognize_size_lengths(
    size_chart_path: Path,
    expected_sizes: Sequence[str],
    garment_kind: str,
) -> Tuple[SizeLength, ...]:
    size_chart_path = Path(size_chart_path)
    return parse_size_lengths(
        vision_ocr(size_chart_path),
        expected_sizes,
        garment_kind,
        source=str(size_chart_path),
    )


def _cell_bounds(centers: Sequence[float]) -> Tuple[Tuple[float, float], ...]:
    if len(centers) < 2:
        raise RecognitionError("表格至少需要两个尺码或两行测量项")
    ordered = tuple(centers)
    if any(right <= left for left, right in zip(ordered, ordered[1:])):
        raise RecognitionError("表格单元格坐标重叠或顺序无效")
    midpoints = [(left + right) / 2.0 for left, right in zip(ordered, ordered[1:])]
    first = ordered[0] - (ordered[1] - ordered[0]) / 2.0
    last = ordered[-1] + (ordered[-1] - ordered[-2]) / 2.0
    edges = [first, *midpoints, last]
    return tuple((edges[index], edges[index + 1]) for index in range(len(ordered)))


def parse_measurement_table(
    tokens: Tuple[OCRToken, ...],
    expected_sizes: Sequence[str],
    *,
    source: str = "尺码信息表",
) -> Dict[str, SizeMeasurements]:
    """Parse a coordinate-based garment measurement table."""
    expected_map = _expected_size_map(expected_sizes, source)

    row_tokens: Dict[str, OCRToken] = {}
    for token in tokens:
        kind = _measurement_kind(token.text)
        if kind is None:
            continue
        if kind in row_tokens:
            raise RecognitionError(f"{source}：{_MEASUREMENT_NAMES[kind]}行标题重复")
        row_tokens[kind] = token
    missing_rows = [
        _MEASUREMENT_NAMES[kind]
        for kind in _REQUIRED_MEASUREMENTS
        if kind not in row_tokens
    ]
    if missing_rows:
        raise RecognitionError(f"{source}：缺少测量行：{', '.join(missing_rows)}")

    first_row_y = min(_center_y(row_tokens[kind]) for kind in _REQUIRED_MEASUREMENTS)
    label_right = max(token.x + token.width for token in row_tokens.values())
    header_candidates = [
        (size, token)
        for token in tokens
        for size in [_normalize_size(token.text)]
        if size is not None
        and _center_y(token) < first_row_y
        and _center_x(token) > label_right
    ]
    seen_headers: Dict[str, OCRToken] = {}
    for size, token in header_candidates:
        if size in seen_headers:
            raise RecognitionError(f"{source}：尺码列标题重复：{size}")
        seen_headers[size] = token
    _require_size_set(tuple(seen_headers), tuple(expected_map), source)

    columns = sorted(seen_headers.items(), key=lambda item: _center_x(item[1]))
    column_centers = [_center_x(token) for _, token in columns]
    column_bounds = _cell_bounds(column_centers)

    ordered_rows = sorted(row_tokens.items(), key=lambda item: _center_y(item[1]))
    row_centers = [_center_y(token) for _, token in ordered_rows]
    row_bounds = _cell_bounds(row_centers)
    numeric_tokens = [(token, _number(token.text)) for token in tokens]
    numeric_tokens = [(token, value) for token, value in numeric_tokens if value is not None]

    cells: Dict[Tuple[str, str], Number] = {}
    for (kind, _row_token), (top, bottom) in zip(ordered_rows, row_bounds):
        for (size, _header_token), (left, right) in zip(columns, column_bounds):
            candidates = [
                value
                for token, value in numeric_tokens
                if left <= _center_x(token) < right
                and top <= _center_y(token) < bottom
            ]
            cell_name = f"{size} {_MEASUREMENT_NAMES[kind]}"
            if not candidates:
                raise RecognitionError(f"{source}：单元格 {cell_name} 缺失")
            if len(candidates) != 1:
                raise RecognitionError(f"{source}：单元格 {cell_name} 重复或有歧义")
            value = candidates[0]
            if isinstance(value, bool) or not math.isfinite(float(value)) or value <= 0:
                raise RecognitionError(f"{source}：单元格 {cell_name} 必须是正数")
            cells[(size, kind)] = value

    result = {}
    for size in expected_map:
        result[size] = SizeMeasurements(
            waist=cells[(size, "waist")],
            hip=cells[(size, "hip")],
            length=cells[(size, "length")],
            foot_opening=cells.get((size, "foot_opening")),
        )
    return result


def parse_clothing_measurement_table(
    tokens: Tuple[OCRToken, ...],
    expected_sizes: Sequence[str],
    *,
    source: str = "尺码信息表",
) -> Dict[str, Mapping[str, Number]]:
    """Parse the four measurements required by Douyin clothing size tables."""
    expected_map = _expected_size_map(expected_sizes, source)

    row_tokens: Dict[str, OCRToken] = {}
    for token in tokens:
        matches = [
            kind
            for kind, aliases in _CLOTHING_MEASUREMENT_ALIASES.items()
            if _matches_alias_composition(token.text, aliases)
        ]
        if len(matches) != 1:
            continue
        kind = matches[0]
        if kind in row_tokens:
            raise RecognitionError(
                f"{source}：{_CLOTHING_MEASUREMENT_NAMES[kind]}行标题重复"
            )
        row_tokens[kind] = token

    missing_rows = [
        display_name
        for kind, display_name in _CLOTHING_MEASUREMENT_NAMES.items()
        if kind not in row_tokens
    ]
    if missing_rows:
        raise RecognitionError(f"{source}：缺少测量行：{', '.join(missing_rows)}")

    first_row_y = min(_center_y(token) for token in row_tokens.values())
    label_right = max(token.x + token.width for token in row_tokens.values())
    header_candidates = [
        (size, token)
        for token in tokens
        for size in [_normalize_size(token.text)]
        if size is not None
        and _center_y(token) < first_row_y
        and _center_x(token) > label_right
    ]
    headers: Dict[str, OCRToken] = {}
    for size, token in header_candidates:
        if size in headers:
            raise RecognitionError(f"{source}：尺码列标题重复：{size}")
        headers[size] = token
    _require_size_set(tuple(headers), tuple(expected_map), source)

    columns = sorted(headers.items(), key=lambda item: _center_x(item[1]))
    column_bounds = _cell_bounds([_center_x(token) for _size, token in columns])
    ordered_rows = sorted(row_tokens.items(), key=lambda item: _center_y(item[1]))
    row_bounds = _cell_bounds([_center_y(token) for _kind, token in ordered_rows])
    numeric_tokens = tuple(
        (token, value)
        for token in tokens
        for value in [_number(token.text)]
        if value is not None
    )

    cells: Dict[Tuple[str, str], Number] = {}
    for (kind, _row_token), (top, bottom) in zip(ordered_rows, row_bounds):
        for (size, _header), (left, right) in zip(columns, column_bounds):
            matches = [
                value
                for token, value in numeric_tokens
                if left <= _center_x(token) < right
                and top <= _center_y(token) < bottom
            ]
            cell_name = f"{size} {_CLOTHING_MEASUREMENT_NAMES[kind]}"
            if len(matches) != 1:
                raise RecognitionError(
                    f"{source}：单元格 {cell_name}匹配数为 {len(matches)}"
                )
            value = matches[0]
            if value <= 0:
                raise RecognitionError(f"{source}：单元格 {cell_name}必须是正数")
            cells[(size, kind)] = value

    return {
        size: {
            kind: cells[(size, kind)]
            for kind in _CLOTHING_MEASUREMENT_NAMES
        }
        for size in expected_map
    }


# A descriptive alias for callers that prefer the source name in the API.
parse_size_measurement_table = parse_measurement_table


def _strictly_increasing(values: Sequence[Number]) -> bool:
    return all(right > left for left, right in zip(values, values[1:]))


def _axis_candidates(
    numeric_tokens: Sequence[Tuple[OCRToken, Number]],
    *,
    horizontal: bool,
) -> Tuple[Tuple[OCRToken, Number], ...]:
    groups: Dict[Tuple[int, ...], Tuple[Tuple[OCRToken, Number], ...]] = {}
    for anchor_index, (anchor, _value) in enumerate(numeric_tokens):
        anchor_coordinate = _center_y(anchor) if horizontal else _center_x(anchor)
        anchor_span = anchor.height if horizontal else anchor.width
        members = []
        for index, item in enumerate(numeric_tokens):
            token = item[0]
            coordinate = _center_y(token) if horizontal else _center_x(token)
            span = token.height if horizontal else token.width
            tolerance = max(0.012, 0.75 * max(anchor_span, span))
            if abs(coordinate - anchor_coordinate) <= tolerance:
                members.append((index, item))
        key = tuple(index for index, _item in members)
        if len(key) < 2:
            continue
        coordinate_key = _center_x if horizontal else _center_y
        ordered = tuple(sorted((item for _index, item in members), key=lambda item: coordinate_key(item[0])))
        coordinates = [coordinate_key(item[0]) for item in ordered]
        values = [item[1] for item in ordered]
        if _strictly_increasing(coordinates) and _strictly_increasing(values):
            groups[key] = ordered
    if not groups:
        orientation = "体重横轴" if horizontal else "身高纵轴"
        raise RecognitionError(f"身高体重推荐表：无法识别{orientation}")
    longest = max(len(group) for group in groups.values())
    best = {tuple(id(item[0]) for item in group): group for group in groups.values() if len(group) == longest}
    if len(best) != 1:
        orientation = "体重横轴" if horizontal else "身高纵轴"
        raise RecognitionError(f"身高体重推荐表：{orientation}候选有歧义")
    return next(iter(best.values()))


def _profile_transitions(
    gray: np.ndarray,
    *,
    horizontal_scan: bool,
    fixed_coordinate: int,
    start: int,
) -> Tuple[int, ...]:
    """Find coherent grayscale transitions, ignoring thin grid/text artifacts."""
    height, width = gray.shape
    fixed_limit = height if horizontal_scan else width
    scan_limit = width if horizontal_scan else height
    half_strip = max(3, int(round(fixed_limit * 0.004)))
    fixed_coordinate = min(max(half_strip, fixed_coordinate), fixed_limit - half_strip - 1)
    if horizontal_scan:
        strip = gray[fixed_coordinate - half_strip : fixed_coordinate + half_strip + 1, :]
        profile = np.median(strip, axis=0)
    else:
        strip = gray[:, fixed_coordinate - half_strip : fixed_coordinate + half_strip + 1]
        profile = np.median(strip, axis=1)
    side = max(4, int(round(scan_limit * 0.006)))
    gap = max(1, side // 3)
    contrasts = np.zeros(scan_limit, dtype=np.float32)
    coherent = np.zeros(scan_limit, dtype=bool)
    for coordinate in range(side, scan_limit - side):
        before = profile[coordinate - side : coordinate - gap]
        after = profile[coordinate + gap : coordinate + side]
        before_median = float(np.median(before))
        after_median = float(np.median(after))
        contrast = abs(after_median - before_median)
        before_spread = float(np.percentile(before, 90) - np.percentile(before, 10))
        after_spread = float(np.percentile(after, 90) - np.percentile(after, 10))
        contrasts[coordinate] = contrast
        stability_limit = max(6.0, contrast * 0.35)
        coherent[coordinate] = (
            contrast >= 7.0
            and before_spread <= stability_limit
            and after_spread <= stability_limit
        )

    points = np.flatnonzero(coherent[max(start, side) : scan_limit - side])
    if not len(points):
        raise RecognitionError("身高体重推荐表：色块边界缺失或对比度不足")
    points = points + max(start, side)
    groups: List[List[int]] = []
    max_gap = max(2, side // 3)
    for point in points:
        coordinate = int(point)
        if not groups or coordinate - groups[-1][-1] > max_gap:
            groups.append([coordinate])
        else:
            groups[-1].append(coordinate)
    return tuple(
        group[int(np.argmax(contrasts[group]))]
        for group in groups
    )


def _profile_transition(
    gray: np.ndarray,
    *,
    horizontal_scan: bool,
    fixed_coordinate: int,
    start: int,
) -> int:
    """Return the nearest sustained transition; primarily useful for diagnostics."""
    return _profile_transitions(
        gray,
        horizontal_scan=horizontal_scan,
        fixed_coordinate=fixed_coordinate,
        start=start,
    )[0]


def _axis_cell_edges(axis: Sequence[Tuple[OCRToken, Number]], *, horizontal: bool) -> Tuple[float, ...]:
    coordinate = _center_x if horizontal else _center_y
    centers = [coordinate(token) for token, _value in axis]
    if len(centers) < 2 or not _strictly_increasing(centers):
        raise RecognitionError("身高体重推荐表：坐标轴刻度坐标无效")
    return tuple(
        [(left + right) / 2.0 for left, right in zip(centers, centers[1:])]
        + [centers[-1] + (centers[-1] - centers[-2]) / 2.0]
    )


def _map_boundary_to_axis(
    boundary: float,
    axis: Sequence[Tuple[OCRToken, Number]],
    *,
    horizontal: bool,
) -> Number:
    edges = _axis_cell_edges(axis, horizontal=horizontal)
    nearest_index = min(range(len(edges)), key=lambda index: abs(edges[index] - boundary))
    coordinate = _center_x if horizontal else _center_y
    centers = [coordinate(token) for token, _value in axis]
    typical_spacing = float(np.median(np.diff(centers)))
    if abs(edges[nearest_index] - boundary) > typical_spacing * 0.28:
        raise RecognitionError("身高体重推荐表：色块边界与坐标轴网格不对齐")
    return axis[nearest_index][1]


def _enclosing_axis_boundary(
    gray: np.ndarray,
    *,
    horizontal_scan: bool,
    fixed_coordinate: int,
    start: int,
    axis: Sequence[Tuple[OCRToken, Number]],
    horizontal_axis: bool,
    image_scale: int,
) -> int:
    candidates = _profile_transitions(
        gray,
        horizontal_scan=horizontal_scan,
        fixed_coordinate=fixed_coordinate,
        start=start,
    )
    normalized_edges = _axis_cell_edges(axis, horizontal=horizontal_axis)
    edges = [edge * image_scale for edge in normalized_edges]
    coordinate = _center_x if horizontal_axis else _center_y
    centers = [coordinate(token) * image_scale for token, _value in axis]
    tolerance = float(np.median(np.diff(centers))) * 0.28

    aligned = []
    for candidate in candidates:
        edge_index = min(
            range(len(edges)),
            key=lambda index: abs(edges[index] - candidate),
        )
        if abs(edges[edge_index] - candidate) <= tolerance:
            aligned.append((candidate, edge_index))
    if not aligned or aligned[0][0] != candidates[0]:
        raise RecognitionError("身高体重推荐表：最近色块边界与坐标轴网格不对齐")
    boundary, edge_index = aligned[0]
    same_edge = [candidate for candidate, index in aligned if index == edge_index]
    if len(same_edge) != 1:
        raise RecognitionError("身高体重推荐表：同一网格存在多个有歧义的色块边界")
    return boundary


def parse_height_weight_chart(
    image_path: Path,
    tokens: Tuple[OCRToken, ...],
    expected_sizes: Sequence[str],
) -> Dict[str, Tuple[Number, Number, Number, Number]]:
    """Return size -> (height_min, height_max, weight_min, weight_max)."""
    source = str(image_path)
    expected_map = _expected_size_map(expected_sizes, source)
    numeric_tokens = tuple(
        (token, value)
        for token in tokens
        for value in [_number(token.text)]
        if value is not None and value > 0
    )
    weight_axis = _axis_candidates(numeric_tokens, horizontal=True)
    height_axis = _axis_candidates(numeric_tokens, horizontal=False)
    weight_y = float(np.median([_center_y(token) for token, _value in weight_axis]))
    height_x = float(np.median([_center_x(token) for token, _value in height_axis]))
    first_weight_x = _center_x(weight_axis[0][0])
    first_height_y = _center_y(height_axis[0][0])
    body_left = (height_x + first_weight_x) / 2.0
    body_top = (weight_y + first_height_y) / 2.0

    labels: Dict[str, OCRToken] = {}
    for token in tokens:
        size = _normalize_size(token.text)
        if size is None or _center_x(token) <= body_left or _center_y(token) <= body_top:
            continue
        if size in labels:
            raise RecognitionError(f"{source}：尺码色块标签重复：{size}")
        labels[size] = token
    _require_size_set(tuple(labels), tuple(expected_map), source)

    image_path = Path(image_path)
    gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.ndim != 2:
        raise RecognitionError(f"{source}：无法读取身高体重推荐图片")
    image_height, image_width = gray.shape
    x_spacing = float(np.median(np.diff([_center_x(token) for token, _value in weight_axis])))
    y_spacing = float(np.median(np.diff([_center_y(token) for token, _value in height_axis])))

    # 这类推荐图的尺码色块是按顺序首尾相接的阶梯：
    # S 从坐标轴最小值开始，后一码的起点是前一码的终点。
    # 原逻辑只识别了每个色块的右/下边界，却把全局最小值
    # 重复用作每个尺码的下限，导致 M 以后都从 155/50 开始。
    next_height_min = min(value for _token, value in height_axis)
    next_weight_min = min(value for _token, value in weight_axis)
    result = {}
    right_boundaries = []
    lower_boundaries = []
    for size in expected_map:
        label = labels[size]
        label_center_x = int(round(_center_x(label) * image_width))
        label_center_y = int(round(_center_y(label) * image_height))
        right_start = int(round((label.x + label.width + x_spacing * 0.12) * image_width))
        lower_start = int(round((label.y + label.height + y_spacing * 0.12) * image_height))
        right_boundary = _enclosing_axis_boundary(
            gray,
            horizontal_scan=True,
            fixed_coordinate=label_center_y,
            start=right_start,
            axis=weight_axis,
            horizontal_axis=True,
            image_scale=image_width,
        )
        lower_boundary = _enclosing_axis_boundary(
            gray,
            horizontal_scan=False,
            fixed_coordinate=label_center_x,
            start=lower_start,
            axis=height_axis,
            horizontal_axis=False,
            image_scale=image_height,
        )
        right_boundaries.append(right_boundary)
        lower_boundaries.append(lower_boundary)
        weight_max = _map_boundary_to_axis(
            right_boundary / image_width,
            weight_axis,
            horizontal=True,
        )
        height_max = _map_boundary_to_axis(
            lower_boundary / image_height,
            height_axis,
            horizontal=False,
        )
        result[size] = (
            next_height_min,
            height_max,
            next_weight_min,
            weight_max,
        )
        next_height_min = height_max
        next_weight_min = weight_max
    if not _strictly_increasing(right_boundaries):
        raise RecognitionError(f"{source}：尺码色块右边界不唯一或未严格递增")
    if not _strictly_increasing(lower_boundaries):
        raise RecognitionError(f"{source}：尺码色块下边界不唯一或未严格递增")
    return result


def _measurement_field(measurement: Any, field: str, source: str, size: str) -> Number:
    if isinstance(measurement, Mapping):
        if field not in measurement:
            raise RecognitionError(f"{source}：{size} 缺少{_MEASUREMENT_NAMES[field]}")
        return measurement[field]
    try:
        return getattr(measurement, field)
    except AttributeError as error:
        raise RecognitionError(f"{source}：{size} 缺少{_MEASUREMENT_NAMES[field]}") from error


def _positive_number(value: Any, source: str, size: str, field: str) -> Number:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecognitionError(f"{source}：{size} {field}必须是数字")
    if not math.isfinite(float(value)) or value <= 0:
        raise RecognitionError(f"{source}：{size} {field}必须是正数")
    return value


def recognize_recommendations(
    size_chart_path: Path,
    height_weight_path: Path,
    expected_sizes: Sequence[str],
) -> Tuple[SkuRecommendation, ...]:
    expected_map = _expected_size_map(expected_sizes, "Excel")
    expected = tuple(expected_map)
    size_chart_path = Path(size_chart_path)
    height_weight_path = Path(height_weight_path)
    size_tokens = vision_ocr(size_chart_path)
    measurements = parse_measurement_table(
        size_tokens,
        expected,
        source=str(size_chart_path),
    )
    _require_size_set(tuple(measurements), expected, str(size_chart_path))
    height_weight_tokens = vision_ocr(height_weight_path)
    ranges = parse_height_weight_chart(height_weight_path, height_weight_tokens, expected)
    _require_size_set(tuple(ranges), expected, str(height_weight_path))

    rows = []
    for size in expected:
        chart_range = ranges[size]
        if not isinstance(chart_range, (tuple, list)) or len(chart_range) != 4:
            raise RecognitionError(f"{height_weight_path}：{size} 身高体重范围格式无效")
        height_min, height_max, weight_min, weight_max = [
            _positive_number(value, str(height_weight_path), size, field)
            for value, field in zip(chart_range, ("身高下限", "身高上限", "体重下限", "体重上限"))
        ]
        if height_min > height_max:
            raise RecognitionError(f"{height_weight_path}：{size} 身高下限大于上限")
        if weight_min > weight_max:
            raise RecognitionError(f"{height_weight_path}：{size} 体重下限大于上限")
        measurement = measurements[size]
        waist = _positive_number(
            _measurement_field(measurement, "waist", str(size_chart_path), size),
            str(size_chart_path),
            size,
            "腰围",
        )
        hip = _positive_number(
            _measurement_field(measurement, "hip", str(size_chart_path), size),
            str(size_chart_path),
            size,
            "臀围",
        )
        length = _positive_number(
            _measurement_field(measurement, "length", str(size_chart_path), size),
            str(size_chart_path),
            size,
            "裤长",
        )
        rows.append(
            SkuRecommendation(
                expected_map[size],
                height_min,
                height_max,
                weight_min,
                weight_max,
                waist,
                hip,
                length,
            )
        )

    monotonic_fields = (
        ("height_max", "身高上限"),
        ("weight_max", "体重上限"),
        ("waist", "腰围"),
        ("hip", "臀围"),
        ("length", "裤长"),
    )
    for previous, current in zip(rows, rows[1:]):
        for field, display_name in monotonic_fields:
            if getattr(current, field) < getattr(previous, field):
                raise RecognitionError(
                    f"尺码顺序 {previous.size} → {current.size} 的{display_name}递减"
                )
    return tuple(rows)


def recognize_clothing_recommendations(
    size_chart_path: Path,
    height_weight_path: Path,
    expected_sizes: Sequence[str],
) -> Tuple[ClothingSkuRecommendation, ...]:
    """Read the measurements shown by Douyin for an upper-body garment."""
    expected_map = _expected_size_map(expected_sizes, "Excel")
    expected = tuple(expected_map)
    size_chart_path = Path(size_chart_path)
    height_weight_path = Path(height_weight_path)
    measurements = parse_clothing_measurement_table(
        vision_ocr(size_chart_path),
        expected,
        source=str(size_chart_path),
    )
    ranges = parse_height_weight_chart(
        height_weight_path,
        vision_ocr(height_weight_path),
        expected,
    )
    _require_size_set(tuple(measurements), expected, str(size_chart_path))
    _require_size_set(tuple(ranges), expected, str(height_weight_path))

    rows = []
    for size in expected:
        height_min, height_max, weight_min, weight_max = ranges[size]
        values = measurements[size]
        rows.append(
            ClothingSkuRecommendation(
                expected_map[size],
                _positive_number(height_min, str(height_weight_path), size, "身高下限"),
                _positive_number(height_max, str(height_weight_path), size, "身高上限"),
                _positive_number(weight_min, str(height_weight_path), size, "体重下限"),
                _positive_number(weight_max, str(height_weight_path), size, "体重上限"),
                _positive_number(values["length"], str(size_chart_path), size, "衣长"),
                _positive_number(values["chest"], str(size_chart_path), size, "胸围"),
                _positive_number(values["shoulder"], str(size_chart_path), size, "肩宽"),
                _positive_number(values["sleeve"], str(size_chart_path), size, "袖长"),
            )
        )

    monotonic_fields = (
        ("height_max", "身高上限"),
        ("weight_max", "体重上限"),
        ("length", "衣长"),
        ("chest", "胸围"),
        ("shoulder", "肩宽"),
        ("sleeve", "袖长"),
    )
    for previous, current in zip(rows, rows[1:]):
        for field, display_name in monotonic_fields:
            if getattr(current, field) < getattr(previous, field):
                raise RecognitionError(
                    f"尺码顺序 {previous.size} → {current.size} 的{display_name}递减"
                )
    return tuple(rows)
