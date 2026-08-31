from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .core import (
    AnalyzerPaths,
    RunResult,
    _tab_rows,
    analyze_skill_ids,
    create_run_output_dir,
    read_text_auto,
)


PackageProgress = Callable[[int, int, str], None]

METADATA_PATHS = (
    "settings/skill/skills.tab",
    "settings/skill/Buff.tab",
    "ui/Scheme/Case/buff.txt",
    "ui/Scheme/Case/skill.txt",
    "scripts/ScriptList.tab",
)


@dataclass(frozen=True)
class ExtractionSummary:
    total: int
    extracted: int
    skipped: int
    missing: int
    invalid: int
    failed: int


@dataclass(frozen=True)
class DungeonInfo:
    name: str
    skill_ids: tuple[str, ...]
    script_paths: tuple[str, ...]

    @property
    def skill_count(self) -> int:
        return len(self.skill_ids)

    @property
    def script_count(self) -> int:
        return len(self.script_paths)


@dataclass(frozen=True)
class PackageCatalog:
    bin64: Path
    extracted_root: Path
    dungeons: tuple[DungeonInfo, ...]
    shared_script_paths: tuple[str, ...]

    def find(self, names: Iterable[str]) -> list[DungeonInfo]:
        by_name = {item.name.casefold(): item for item in self.dungeons}
        selected: list[DungeonInfo] = []
        missing: list[str] = []
        for name in names:
            item = by_name.get(name.casefold())
            if item is None:
                missing.append(name)
            elif item not in selected:
                selected.append(item)
        if missing:
            raise ValueError("副本列表中找不到：" + "、".join(missing))
        if not selected:
            raise ValueError("请至少选择一个副本。")
        return selected


def bundled_extractor_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / "vendor/JX3PakBridge.exe"
    return Path(__file__).resolve().parents[1] / "vendor/JX3PakBridge.exe"


def resolve_bin64(path: Path | str) -> Path:
    selected = Path(path).expanduser().resolve()
    candidates = [
        selected,
        selected / "bin64",
        selected / "bin/zhcn_hd/bin64",
        selected / "bin/zhcn/bin64",
    ]
    if selected.is_dir():
        try:
            candidates.extend(child / "bin64" for child in (selected / "bin").iterdir())
        except OSError:
            pass

    checked: set[Path] = set()
    for candidate in candidates:
        if candidate in checked:
            continue
        checked.add(candidate)
        if (candidate / "Engine_Lua5X64.dll").is_file():
            try:
                game_root = candidate.parents[2]
            except IndexError:
                continue
            if (game_root / "PakV4/Trunk.dir").is_file():
                return candidate
    raise FileNotFoundError(
        "所选位置不是可用的剑网3 bin64：需要找到 Engine_Lua5X64.dll，"
        "并能从该目录定位到游戏根目录下的 PakV4/Trunk.dir。"
    )


def default_package_cache_root(bin64: Path | str) -> Path:
    resolved = resolve_bin64(bin64)
    trunk = resolved.parents[2] / "PakV4/Trunk.dir"
    stat = trunk.stat()
    identity = f"{str(resolved).casefold()}|{stat.st_size}|{stat.st_mtime_ns}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return local_app_data / "JX3SkillAnalyzer/package_cache" / digest


def _normalize_internal_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/").lstrip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _deduplicate_paths(paths: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in paths:
        normalized = _normalize_internal_path(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"资源路径不安全：{value}")
        seen.add(key)
        result.append(normalized)
    return result


def extract_package_files(
    bin64: Path | str,
    internal_paths: Iterable[str],
    output_root: Path | str,
    *,
    overwrite: bool = True,
) -> ExtractionSummary:
    resolved_bin64 = resolve_bin64(bin64)
    helper = bundled_extractor_path()
    if not helper.is_file():
        raise FileNotFoundError(f"程序内置解包组件缺失：{helper}")

    paths = _deduplicate_paths(internal_paths)
    if not paths:
        raise ValueError("没有需要解包的资源路径。")
    try:
        request_bytes = ("\r\n".join(paths) + "\r\n").encode("gbk")
    except UnicodeEncodeError as exc:
        raise ValueError(f"资源路径无法转换为游戏使用的 GBK 编码：{exc}") from exc

    destination = Path(output_root).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    request_file = destination / f".jx3_extract_{uuid.uuid4().hex}.txt"
    staged_helper = resolved_bin64 / f".jx3_skill_analyzer_{uuid.uuid4().hex}.exe"
    request_file.write_bytes(request_bytes)
    try:
        shutil.copyfile(helper, staged_helper)
    except OSError as exc:
        with suppress(OSError):
            request_file.unlink(missing_ok=True)
        raise PermissionError(
            "无法在所选 bin64 中临时启动内置解包组件。"
            "请确认游戏目录可写，或以管理员身份运行本程序。"
        ) from exc

    command = [
        str(staged_helper),
        "--bin64",
        str(resolved_bin64),
        "--list",
        str(request_file),
        "--output",
        str(destination),
    ]
    if overwrite:
        command.append("--overwrite")
    command.append("--verbose")
    try:
        completed = subprocess.run(
            command,
            cwd=resolved_bin64,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    finally:
        with suppress(OSError):
            request_file.unlink(missing_ok=True)
        with suppress(OSError):
            staged_helper.unlink(missing_ok=True)

    output = completed.stdout.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        windows_code = completed.returncode & 0xFFFFFFFF
        trace_lines = output.splitlines()[-12:]
        detail = "\n".join(trace_lines) if trace_lines else "原生组件没有返回阶段信息"
        raise RuntimeError(
            "读取游戏包失败："
            f"Windows 退出码 {windows_code}（0x{windows_code:08X}）\n"
            f"最后处理阶段：\n{detail}"
        )
    match = re.search(
        r"SUMMARY total=(\d+) extracted=(\d+) skipped=(\d+) "
        r"missing=(\d+) invalid=(\d+) failed=(\d+)",
        output,
    )
    if not match:
        raise RuntimeError(f"解包组件没有返回有效结果：{output or '无输出'}")
    return ExtractionSummary(*(int(value) for value in match.groups()))


def _dungeon_from_path(path: str, *, script_list: bool) -> str | None:
    normalized = _normalize_internal_path(path)
    parts = normalized.split("/")
    folded = [part.casefold() for part in parts]
    expected = ["scripts", "skill", "npc", "副本boss"] if script_list else ["npc", "副本boss"]
    # Legacy tables also contain scripts placed directly under 副本BOSS.  Their
    # filename is not a dungeon name, so only accept paths with a folder and at
    # least one child beneath it.
    if folded[: len(expected)] != expected or len(parts) <= len(expected) + 1:
        return None
    name = parts[len(expected)].strip()
    return name or None


def _skill_script_internal_path(path: str) -> str:
    normalized = _normalize_internal_path(path)
    folded = normalized.casefold()
    if folded.startswith("scripts/"):
        return normalized
    if folded.startswith("skill/"):
        return "scripts/" + normalized
    return "scripts/skill/" + normalized


def build_package_catalog(bin64: Path | str, extracted_root: Path | str) -> PackageCatalog:
    resolved_bin64 = resolve_bin64(bin64)
    root = Path(extracted_root).expanduser().resolve()
    skills_path = root / "settings/skill/skills.tab"
    script_list_path = root / "scripts/ScriptList.tab"
    if not skills_path.is_file() or not script_list_path.is_file():
        raise FileNotFoundError("读取副本列表所需的 skills.tab 或 scripts/ScriptList.tab 不完整。")

    skill_ids: dict[str, list[str]] = {}
    direct_scripts: dict[str, list[str]] = {}
    canonical_names: dict[str, str] = {}
    for row in _tab_rows(skills_path):
        script_file = row.get("ScriptFile", "").strip()
        dungeon = _dungeon_from_path(script_file, script_list=False)
        skill_id = row.get("SkillID", "").strip()
        if dungeon is None or not skill_id.isdigit() or int(skill_id) <= 0:
            continue
        key = dungeon.casefold()
        canonical_names.setdefault(key, dungeon)
        normalized_id = str(int(skill_id))
        ids = skill_ids.setdefault(key, [])
        if normalized_id not in ids:
            ids.append(normalized_id)
        scripts = direct_scripts.setdefault(key, [])
        internal_script = _skill_script_internal_path(script_file)
        if internal_script.casefold() not in {item.casefold() for item in scripts}:
            scripts.append(internal_script)

    script_paths: dict[str, list[str]] = {}
    shared_paths: list[str] = []
    script_text, _ = read_text_auto(script_list_path)
    for raw_line in script_text.splitlines():
        internal = _normalize_internal_path(raw_line.lstrip("\ufeff"))
        if not internal or internal.casefold() == "filepath":
            continue
        folded = internal.casefold()
        if folded.startswith("scripts/include/") or folded.startswith("scripts/skill/include/"):
            shared_paths.append(internal)
        dungeon = _dungeon_from_path(internal, script_list=True)
        if dungeon is not None:
            script_paths.setdefault(dungeon.casefold(), []).append(internal)

    dungeons: list[DungeonInfo] = []
    for key, ids in skill_ids.items():
        combined = _deduplicate_paths([*script_paths.get(key, []), *direct_scripts.get(key, [])])
        dungeons.append(
            DungeonInfo(
                name=canonical_names[key],
                skill_ids=tuple(ids),
                script_paths=tuple(combined),
            )
        )
    dungeons.sort(key=lambda item: item.name.casefold())
    return PackageCatalog(
        bin64=resolved_bin64,
        extracted_root=root,
        dungeons=tuple(dungeons),
        shared_script_paths=tuple(_deduplicate_paths(shared_paths)),
    )


def load_package_catalog(
    bin64: Path | str,
    *,
    cache_root: Path | str | None = None,
    progress: PackageProgress | None = None,
) -> PackageCatalog:
    resolved_bin64 = resolve_bin64(bin64)
    root = Path(cache_root).expanduser().resolve() if cache_root else default_package_cache_root(resolved_bin64)
    if progress:
        progress(1, 3, "正在读取游戏包中的技能表与脚本清单")
    summary = extract_package_files(resolved_bin64, METADATA_PATHS, root, overwrite=True)
    if summary.missing or summary.invalid or summary.failed:
        raise RuntimeError(
            f"基础数据解包不完整：缺失 {summary.missing}，无效 {summary.invalid}，失败 {summary.failed}。"
        )
    if progress:
        progress(2, 3, "正在整理可选副本")
    catalog = build_package_catalog(resolved_bin64, root)
    if not catalog.dungeons:
        raise RuntimeError("没有从 skills.tab 与 ScriptList.tab 中识别到副本。")
    if progress:
        progress(3, 3, f"已读取 {len(catalog.dungeons)} 个副本")
    return catalog


def analyze_selected_dungeons(
    catalog: PackageCatalog,
    dungeon_names: Iterable[str],
    output_parent: Path | str,
    progress: PackageProgress | None = None,
) -> RunResult:
    selected = catalog.find(dungeon_names)
    skill_ids: list[str] = []
    seen_ids: set[str] = set()
    for dungeon in selected:
        for skill_id in dungeon.skill_ids:
            if skill_id not in seen_ids:
                seen_ids.add(skill_id)
                skill_ids.append(skill_id)

    extract_paths = _deduplicate_paths(
        [
            *METADATA_PATHS,
            *catalog.shared_script_paths,
            *(path for dungeon in selected for path in dungeon.script_paths),
        ]
    )
    if progress:
        progress(1, 2, f"正在解包 {len(selected)} 个副本的 {len(extract_paths)} 个相关文件")
    summary = extract_package_files(catalog.bin64, extract_paths, catalog.extracted_root, overwrite=True)
    if summary.failed or summary.invalid:
        raise RuntimeError(f"副本脚本解包失败 {summary.failed} 个，无效路径 {summary.invalid} 个。")
    if progress:
        message = f"副本脚本已准备完成，共 {len(skill_ids)} 个技能"
        if summary.missing:
            message += f"（包内未找到 {summary.missing} 个清单项）"
        progress(2, 2, message)

    output_dir = create_run_output_dir(output_parent)
    (output_dir / "所选副本.txt").write_text(
        "\n".join(dungeon.name for dungeon in selected) + "\n",
        encoding="utf-8-sig",
        newline="\n",
    )
    (output_dir / "技能ID.txt").write_text(
        "\n".join(skill_ids) + "\n",
        encoding="utf-8-sig",
        newline="\n",
    )

    paths = AnalyzerPaths.from_root(catalog.extracted_root)
    result = analyze_skill_ids(paths, skill_ids, output_dir, progress)
    try:
        payload = json.loads(result.json_file.read_text(encoding="utf-8-sig"))
        payload["package_bin64"] = str(catalog.bin64)
        payload["selected_dungeons"] = [dungeon.name for dungeon in selected]
        result.json_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8-sig",
            newline="\n",
        )
    except (OSError, ValueError, TypeError):
        pass
    return result
