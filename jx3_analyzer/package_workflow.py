from __future__ import annotations

import hashlib
import csv
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from .core import (
    AnalyzerPaths,
    LuaLoader,
    RunResult,
    _dependency_strings,
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

OPTIONAL_INDEX_LABELS = {
    "ui/scheme/case/middlemapdungeonboss.tab": "可选小地图 Boss 索引",
}

METADATA_LABELS = {
    "settings/maplist.tab": "地图关联表",
    "settings/npctemplatelist.tab": "NPC 分表目录",
    "settings/aitypelist.tab": "AI 分表目录",
    "settings/doodadtemplate.tab": "交互物品配置",
    "ui/scheme/case/cyclopaedia/dungeoninfo.txt": "官方副本百科",
    "ui/scheme/case/cyclopaedia/dungeonboss.txt": "官方首领百科",
    "ui/scheme/case/cyclopaedia/dungeonnpc.txt": "百科 NPC 索引",
    "ui/scheme/case/cyclopaedia/dungeonskill.txt": "百科技能文案",
}


@dataclass(frozen=True)
class ExtractionSummary:
    total: int
    extracted: int
    skipped: int
    missing: int
    invalid: int
    failed: int
    journal: Path | None = None


@dataclass(frozen=True)
class DungeonInfo:
    name: str
    skill_ids: tuple[str, ...]
    script_paths: tuple[str, ...]
    map_ids: tuple[str, ...] = ()
    map_names: tuple[str, ...] = ()
    difficulty: str = ""
    skill_folder: str = ""

    @property
    def skill_count(self) -> int:
        return len(self.skill_ids)

    @property
    def script_count(self) -> int:
        return len(self.script_paths)

    @property
    def display_name(self) -> str:
        if self.difficulty and (re.match(r"^\d+人", self.name) or self.name.startswith(("英雄", "挑战", "试炼", "帮会", "历战"))):
            return self.name if re.match(r"^\d+人", self.name) else f"{self.name}（{self.difficulty}）"
        return f"{self.name}（{self.difficulty}）" if self.difficulty else self.name

    @property
    def selection_key(self) -> str:
        return f"map:{self.map_ids[0]}" if self.map_ids else f"folder:{self.name}"


@dataclass(frozen=True)
class PackageCatalog:
    bin64: Path
    extracted_root: Path
    dungeons: tuple[DungeonInfo, ...]
    shared_script_paths: tuple[str, ...]
    map_rows: tuple[dict[str, str], ...] = ()
    metadata_journals: tuple[Path, ...] = ()

    def find(self, names: Iterable[str]) -> list[DungeonInfo]:
        by_name = {item.name.casefold(): item for item in self.dungeons}
        by_name.update({item.selection_key.casefold(): item for item in self.dungeons})
        selected: list[DungeonInfo] = []
        missing: list[str] = []
        for name in names:
            item = by_name.get(name.casefold())
            matches = [item] if item else [d for d in self.dungeons if d.skill_folder.casefold() == name.casefold()]
            if not matches:
                missing.append(name)
            for match in matches:
                if match not in selected:
                    selected.append(match)
        if missing:
            raise ValueError("副本列表中找不到：" + "、".join(missing))
        if not selected:
            raise ValueError("请至少选择一个副本。")
        return selected

    def find_map_ids(self, map_ids: Iterable[str]) -> list[DungeonInfo]:
        selected: list[DungeonInfo] = []
        rows = {row.get("ID", ""): row for row in self.map_rows}
        for value in map_ids:
            map_id = str(value).strip()
            if not map_id.isdigit() or int(map_id) <= 0 or map_id not in rows:
                raise ValueError(f"MapList 中找不到地图 ID：{map_id}")
            matches = [d for d in self.dungeons if map_id in d.map_ids]
            if matches:
                selected.extend(d for d in matches if d not in selected)
            else:
                row = rows[map_id]
                item = DungeonInfo(row.get("Name") or f"地图 {map_id}", (), (), (map_id,), (row.get("Name", ""),), _map_difficulty(row))
                if item not in selected:
                    selected.append(item)
        return selected


def bundled_extractor_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / "vendor/JX3PakBridge.exe"
    return Path(__file__).resolve().parents[1] / "vendor/JX3PakBridge.exe"


def finalize_analysis_output(result: RunResult) -> RunResult:
    """Use one layout adapter for automatic, manual CLI and manual GUI runs."""
    if getattr(result, "output_layout_finalized", False):
        return result
    from .output_layout import finalize_output_layout

    layout = finalize_output_layout(result.output_dir, result=result)
    workbook = Path(layout["workbook"])
    result.skill_csv = result.buff_csv = result.issue_csv = workbook
    result.json_file = Path(layout["json_file"])
    setattr(result, "workbook", workbook)
    setattr(result, "output_layout", layout)
    setattr(result, "output_layout_finalized", True)
    return result


def _missing_paths(summary: ExtractionSummary) -> list[str]:
    if not summary.journal:
        return []
    try:
        journal = json.loads(summary.journal.read_text(encoding="utf-8"))
        return [event["path"] for event in journal.get("file_events", []) if event.get("state") == "missing"]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _optional_metadata_notice(summary: ExtractionSummary) -> str:
    missing = _missing_paths(summary)
    references = [path for path in missing if path.casefold() not in OPTIONAL_INDEX_LABELS]
    if references:
        names = [METADATA_LABELS.get(path.casefold(), Path(path).name) for path in references]
        return "参考入口已读取；未读到：" + "、".join(names) + "。具体路径会保留在读取记录中。"
    if missing:
        names = [OPTIONAL_INDEX_LABELS[path.casefold()] for path in missing]
        return "地图、NPC 和官方百科入口已读取；" + "、".join(names) + "未采用。"
    return "地图、NPC 和官方资料入口已读取。"


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
        if ":" in normalized or any(part in {"", ".", ".."} for part in parts):
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
    header_bytes: int | None = None,
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
    request_file.write_bytes(request_bytes)
    if header_bytes is not None and not 512 <= header_bytes <= 65536:
        request_file.unlink(missing_ok=True)
        raise ValueError("索引头读取大小应在 512 至 65536 字节之间。")
    if overwrite:
        # A failed refresh must not leave an old file looking like fresh data.
        for path in paths:
            (destination / path).unlink(missing_ok=True)
    command = [
        str(helper),
        "--bin64",
        str(resolved_bin64),
        "--list",
        str(request_file),
        "--output",
        str(destination),
    ]
    if overwrite:
        command.append("--overwrite")
    if header_bytes is not None:
        command.extend(["--header-bytes", str(header_bytes)])
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

    output = completed.stdout.decode("utf-8", errors="replace").strip()
    log_dir = destination / ".extraction_logs"
    log_dir.mkdir(exist_ok=True)
    journal = log_dir / f"{uuid.uuid4().hex}.json"
    events = [{"state": m[1], "declared_bytes": int(m[2]), "read_bytes": int(m[3]), "path": _normalize_internal_path(m[4])} for line in output.splitlines() if (m := re.fullmatch(r"FILE (\S+) declared=(\d+) read=(\d+) path=(.*)", line))]
    journal.write_text(json.dumps({"source": "local PakV4", "bin64": str(resolved_bin64), "header_bytes": header_bytes, "requested_paths": paths, "exit_code": completed.returncode, "stdout": output, "file_events": events, "files": [{"path": path, "readable": (destination / path).is_file(), "bytes": (destination / path).stat().st_size if (destination / path).is_file() else 0} for path in paths]}, ensure_ascii=False, indent=2), encoding="utf-8")
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
    return ExtractionSummary(*(int(value) for value in match.groups()), journal=journal)


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


def _base_dungeon_name(name: str) -> str:
    value = name.strip()
    while True:
        changed = re.sub(r"^(?:\d+人(?:普通|英雄|挑战)?|普通|英雄|挑战|试炼|帮会|历战[_·]?)", "", value)
        if changed == value:
            break
        value = changed
    return value.casefold()


def _map_difficulty(row: dict[str, str]) -> str:
    name = row.get("Name", "")
    players = row.get("MaxPlayerCount", "").strip()
    mode = next((word for word in ("挑战", "英雄", "普通", "试炼", "帮会", "历战") if word in name), "")
    return (f"{players}人" if players.isdigit() and 0 < int(players) <= 100 else "") + mode


def _matching_map_rows(folder: str, rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    base = _base_dungeon_name(folder)
    explicit_mode = next((word for word in ("挑战", "英雄", "普通", "试炼", "帮会", "历战") if folder.startswith(word) or re.match(r"^\d+人" + word, folder)), "")
    matches = []
    for row in rows:
        name = row.get("Name", "")
        if row.get("Type") != "1" or _base_dungeon_name(name) != base:
            continue
        if explicit_mode and explicit_mode not in name:
            continue
        matches.append(row)
    return matches


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

    map_path = root / "settings/MapList.tab"
    map_rows = tuple(_tab_rows(map_path)) if map_path.is_file() else ()
    dungeons: list[DungeonInfo] = []
    for key, ids in skill_ids.items():
        combined = _deduplicate_paths([*script_paths.get(key, []), *direct_scripts.get(key, [])])
        folder = canonical_names[key]
        matches = _matching_map_rows(folder, map_rows)
        if matches:
            for row in matches:
                name = row.get("Name") or folder
                dungeons.append(DungeonInfo(name, tuple(ids), tuple(combined), (row["ID"],), (name,), _map_difficulty(row), folder))
        else:
            dungeons.append(DungeonInfo(folder, tuple(ids), tuple(combined), skill_folder=folder))
    # Legacy folders may overlap a map already covered by a broader folder.
    unique: dict[tuple[str, tuple[str, ...]], DungeonInfo] = {}
    for dungeon in dungeons:
        identity = (dungeon.name.casefold(), dungeon.map_ids)
        previous = unique.get(identity)
        if previous:
            unique[identity] = DungeonInfo(previous.name, tuple(dict.fromkeys((*previous.skill_ids, *dungeon.skill_ids))), tuple(_deduplicate_paths((*previous.script_paths, *dungeon.script_paths))), previous.map_ids, previous.map_names, previous.difficulty, previous.skill_folder)
        else:
            unique[identity] = dungeon
    dungeons = list(unique.values())
    dungeons.sort(key=lambda item: item.name.casefold())
    return PackageCatalog(
        bin64=resolved_bin64,
        extracted_root=root,
        dungeons=tuple(dungeons),
        shared_script_paths=tuple(_deduplicate_paths(shared_paths)),
        map_rows=map_rows,
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
        progress(1, 3, "正在读取 V4 技能表与脚本清单")
    summary = extract_package_files(resolved_bin64, METADATA_PATHS, root, overwrite=True)
    metadata_journals = [summary.journal] if summary.journal else []
    if summary.missing or summary.invalid or summary.failed:
        raise RuntimeError(
            f"基础数据解包不完整：缺失 {summary.missing}，无效 {summary.invalid}，失败 {summary.failed}。"
        )
    if progress:
        progress(2, 3, "正在读取地图、NPC 和官方资料入口")
    from .dungeon_resources import DUNGEON_METADATA_PATHS

    optional = _deduplicate_paths(path for path in (*DUNGEON_METADATA_PATHS, "settings/MapList.tab", "settings/NpcTemplateList.tab", "settings/AITypeList.tab") if path.casefold() not in {p.casefold() for p in METADATA_PATHS} and path.casefold() != "settings/npctemplate.tab")
    if optional:
        extra = extract_package_files(resolved_bin64, optional, root, overwrite=True)
        if extra.journal:
            metadata_journals.append(extra.journal)
        if progress:
            progress(2, 3, _optional_metadata_notice(extra))
    trunk = resolved_bin64.parents[2] / "PakV4/Trunk.dir"
    stat = trunk.stat()
    (root / "source_snapshot.json").write_text(json.dumps({"source": "local PakV4", "bin64": str(resolved_bin64), "trunk_path": str(trunk), "trunk_size": stat.st_size, "trunk_mtime_ns": stat.st_mtime_ns}, ensure_ascii=False, indent=2), encoding="utf-8")
    catalog = build_package_catalog(resolved_bin64, root)
    if not catalog.dungeons:
        raise RuntimeError("没有从 skills.tab 与 ScriptList.tab 中识别到副本。")
    if progress:
        progress(3, 3, f"已读取 {len(catalog.dungeons)} 个副本")
    return replace(catalog, metadata_journals=tuple(metadata_journals))


def _registry_paths(root: Path, relative: str) -> list[str]:
    table = root / relative
    if not table.is_file():
        return []
    return _deduplicate_paths(row.get("FilePath", "") for row in _tab_rows(table) if row.get("FilePath", "").lower().endswith(".tab"))


def _index_head_rows(path: Path) -> list[dict[str, str]]:
    # Heads live outside root/settings and are never exported as full tables.
    raw = path.read_bytes()
    raw = raw[: raw.rfind(b"\n") + 1]
    if not raw:
        return []
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [{str(k).lstrip("\ufeff"): str(v or "") for k, v in row.items() if k} for row in reader if row and None not in row]


def _reference_head_index(catalog: PackageCatalog, kind: str, progress: PackageProgress | None) -> dict[str, list[str]]:
    registry = "settings/NpcTemplateList.tab" if kind == "npc" else "settings/AITypeList.tab"
    paths = _registry_paths(catalog.extracted_root, registry)
    if not paths:
        return {}
    directory = catalog.extracted_root / ".resource_indexes" / f"{kind}_heads"
    index_file = directory / "index.json"
    snapshot = catalog.extracted_root / "source_snapshot.json"
    stamp = hashlib.sha256((catalog.extracted_root / registry).read_bytes() + (snapshot.read_bytes() if snapshot.is_file() else b"")).hexdigest()
    try:
        payload = json.loads(index_file.read_text(encoding="utf-8"))
        if payload.get("registry_sha256") == stamp:
            return payload["index"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if progress:
        progress(1, 4, f"正在建立{'NPC 地图名称' if kind == 'npc' else 'AI 编号'}索引（每份只读取前 32KB）")
    extraction = extract_package_files(catalog.bin64, paths, directory, header_bytes=32768)
    index: dict[str, list[str]] = {}
    for path in paths:
        head = directory / path
        if not head.is_file():
            continue
        for row in _index_head_rows(head):
            value = row.get("MapName" if kind == "npc" else "AIType", "").strip()
            if value:
                key = value.casefold()
                if path not in index.setdefault(key, []):
                    index[key].append(path)
    index_file.write_text(json.dumps({"source": "local PakV4 header index; not full body", "registry_sha256": stamp, "header_bytes": 32768, "missing_heads": extraction.missing, "journal": str(extraction.journal) if extraction.journal else None, "index": index}, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def _selected_npc_paths(catalog: PackageCatalog, selected: list[DungeonInfo], progress: PackageProgress | None) -> list[str]:
    index = _reference_head_index(catalog, "npc", progress)
    paths = []
    for dungeon in selected:
        for name in dungeon.map_names or (dungeon.name,):
            direct = index.get(name.casefold(), [])
            if direct:
                paths.extend(direct)
            else:
                base = _base_dungeon_name(name)
                paths.extend(path for key, candidates in index.items() if _base_dungeon_name(key) == base for path in candidates)
    return _deduplicate_paths(paths)


def _selected_ai_paths(catalog: PackageCatalog, npc_paths: list[str], progress: PackageProgress | None, *, body_root: Path | None = None) -> list[str]:
    registry = _registry_paths(catalog.extracted_root, "settings/AITypeList.tab")
    by_parent: dict[str, list[str]] = {}
    for path in registry:
        by_parent.setdefault(Path(path).parent.name.casefold(), []).append(path)
    wanted: set[str] = set()
    paths: list[str] = []
    for path in npc_paths:
        table = (body_root or catalog.extracted_root) / path
        if not table.is_file():
            continue
        paths.extend(by_parent.get(Path(path).parent.name.casefold(), []))
        wanted.update(row.get("AIType", "").strip() for row in _tab_rows(table) if row.get("AIType", "").strip().isdigit() and int(row["AIType"]) > 0)
    # Index only headers to locate shared AI IDs; never read every AI body.
    if wanted:
        index = _reference_head_index(catalog, "ai", progress)
        paths.extend(path for value in wanted for path in index.get(value, []))
    return _deduplicate_paths(paths)


def _prepare_script_dependencies(catalog: PackageCatalog, script_paths: Iterable[str], output_dir: Path, progress: PackageProgress | None, issues: list[dict] | None = None) -> list[str]:
    issues = issues if issues is not None else []
    root = catalog.extracted_root.resolve()
    loader = LuaLoader(AnalyzerPaths.from_root(catalog.extracted_root), output_dir)
    pending = _deduplicate_paths(path for path in script_paths if path.lower().endswith((".lua", ".lh", ".li", ".ls")))
    processed: set[str] = set()
    attempts: list[str] = []
    for depth in range(4):
        next_paths: list[str] = []
        for path in pending:
            key = path.casefold()
            if key in processed:
                continue
            processed.add(key)
            local = catalog.extracted_root / path
            if not local.is_file():
                continue
            try:
                artifact = loader.load(local)
                if not getattr(artifact, "decompile_reliable", True):
                    issues.append({"kind": "script_reference", "path": path, "message": "反编译结果不可靠，未用诊断伪代码发现引用。"})
                    continue
                text = artifact.text
            except (OSError, RuntimeError, ValueError):
                continue
            for value in _dependency_strings(text):
                normalized = _normalize_internal_path(value)
                if normalized.lower().startswith("scripts/"):
                    candidate = normalized
                elif normalized.lower().startswith("skill/"):
                    candidate = "scripts/" + normalized
                elif normalized.lower().startswith("npc/"):
                    candidate = "scripts/skill/" + normalized
                else:
                    candidate = str(Path(path).parent / normalized).replace("\\", "/")
                candidate = posixpath.normpath(candidate)
                if ":" in candidate:
                    issues.append({"kind": "invalid_reference", "path": path, "reference": value, "message": "引用包含外部路径，未读取。"})
                    continue
                resolved = (root / candidate).resolve()
                if not resolved.is_relative_to(root):
                    issues.append({"kind": "invalid_reference", "path": path, "reference": value, "message": "相对引用越出解包根目录，未读取。"})
                    continue
                candidate = resolved.relative_to(root).as_posix()
                if candidate.casefold() not in processed:
                    next_paths.append(candidate)
        pending = _deduplicate_paths(next_paths)
        if not pending:
            break
        if len(processed) + len(pending) > 3000:
            raise RuntimeError("所选副本的脚本引用超过 3000 项，请缩小选择范围。")
        if progress:
            progress(depth + 1, 4, f"正在读取第 {depth + 1} 层明确引用的 {len(pending)} 个脚本")
        extract_package_files(catalog.bin64, pending, catalog.extracted_root, overwrite=True)
        attempts.extend(pending)
    return attempts


def analyze_selected_dungeons(
    catalog: PackageCatalog,
    dungeon_names: Iterable[str],
    output_parent: Path | str,
    progress: PackageProgress | None = None,
    *,
    selected_map_ids: Iterable[str] = (),
) -> RunResult:
    from . import dungeon_resources

    names = list(dungeon_names)
    selected = catalog.find(names) if names else []
    for dungeon in catalog.find_map_ids(selected_map_ids):
        if dungeon not in selected:
            selected.append(dungeon)
    if not selected:
        raise ValueError("请至少选择一个副本或地图 ID。")
    map_ids = list(dict.fromkeys(map_id for dungeon in selected for map_id in dungeon.map_ids))
    output_dir = create_run_output_dir(output_parent)
    source_catalog = catalog
    run_root = output_dir / "解包原文件"
    run_root.mkdir(parents=True)
    catalog = replace(catalog, extracted_root=run_root)
    journal_before = set(catalog.extracted_root.glob(".extraction_logs/*.json")) | set(catalog.extracted_root.glob(".resource_indexes/*_heads/.extraction_logs/*.json"))
    workflow_issues: list[dict] = []
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
            *(path for path in dungeon_resources.DUNGEON_METADATA_PATHS if path.casefold() != "settings/npctemplate.tab"),
            *(path for dungeon in selected for path in dungeon.script_paths),
            *dungeon_resources.resource_paths_for_maps(source_catalog.extracted_root, map_ids),
        ]
    )
    if progress:
        progress(1, 4, f"正在读取所选副本的 {len(extract_paths)} 个明确关联文件")
    summary = extract_package_files(catalog.bin64, extract_paths, catalog.extracted_root, overwrite=True)
    if summary.failed or summary.invalid:
        raise RuntimeError(f"副本脚本解包失败 {summary.failed} 个，无效路径 {summary.invalid} 个。")
    missing_required = [path for path in METADATA_PATHS if not (run_root / path).is_file()]
    if missing_required:
        raise RuntimeError("本次基础数据未读到：" + "、".join(missing_required))
    snapshot = source_catalog.extracted_root / "source_snapshot.json"
    if snapshot.is_file():
        (run_root / "source_snapshot.json").write_bytes(snapshot.read_bytes())
    if progress:
        message = f"副本脚本已准备完成，共 {len(skill_ids)} 个技能"
        progress(2, 4, message)
    map_names = {row.get("ID", ""): row.get("DisplayName") or row.get("Name", "") for row in source_catalog.map_rows}
    for path in _missing_paths(summary):
        if path.casefold() in OPTIONAL_INDEX_LABELS:
            workflow_issues.append({"kind": "optional_reference_not_used", "level": "说明", "path": path, "message": "可选小地图 Boss 索引未采用，不计为所选副本的资料缺失。"})
            continue
        match = re.fullmatch(r"ui/Scheme/Case/BossDbm/BossDbmMap(\d+)\.tab", path, flags=re.IGNORECASE)
        if match and match[1] in map_ids:
            map_id = match[1]
            message = f"当前本地 V4 包未读到{map_names.get(map_id) or '所选副本'}（地图 {map_id}）的官方 DBM 表。"
            workflow_issues.append({"kind": "official_dbm_not_read", "level": "资料未读到", "map_id": map_id, "path": path, "message": message})
            if progress:
                progress(2, 4, message)

    npc_paths = _selected_npc_paths(source_catalog, selected, progress)
    if npc_paths:
        if progress:
            progress(2, 4, f"正在完整读取所选副本的 {len(npc_paths)} 份 NPC 分表")
        extract_package_files(catalog.bin64, npc_paths, catalog.extracted_root, overwrite=True)
    ai_paths = _selected_ai_paths(source_catalog, npc_paths, progress, body_root=run_root)
    if ai_paths:
        if progress:
            progress(2, 4, f"正在完整读取明确关联的 {len(ai_paths)} 份 AI 分表")
        extract_package_files(catalog.bin64, ai_paths, catalog.extracted_root, overwrite=True)
    attempted = {path.casefold() for path in (*extract_paths, *npc_paths, *ai_paths)}
    references: list[str] = []
    for depth in range(3):
        discovered = _deduplicate_paths(dungeon_resources.collect_resource_references(catalog.extracted_root, map_ids))
        new = [path for path in discovered if path.casefold() not in attempted and path.casefold() != "settings/npctemplate.tab"]
        if not new:
            break
        if len(new) > 3000:
            raise RuntimeError("资源引用超过 3000 项，请缩小副本选择范围。")
        if progress:
            progress(depth + 1, 3, f"正在读取第 {depth + 1} 层资料引用，共 {len(new)} 项")
        extract_package_files(catalog.bin64, new, catalog.extracted_root, overwrite=True)
        attempted.update(path.casefold() for path in new)
        references.extend(new)

    for value in dungeon_resources.collect_dungeon_skill_ids(catalog.extracted_root, map_ids):
        skill_id = str(value).strip()
        if skill_id.isdigit() and int(skill_id) > 0 and str(int(skill_id)) not in seen_ids:
            normalized = str(int(skill_id))
            seen_ids.add(normalized)
            skill_ids.append(normalized)
    added_scripts = _deduplicate_paths(_skill_script_internal_path(row["ScriptFile"]) for row in _tab_rows(catalog.extracted_root / "settings/skill/skills.tab") if row.get("SkillID", "").strip() in seen_ids and row.get("ScriptFile", "").strip())
    fresh_scripts = [path for path in added_scripts if path.casefold() not in attempted]
    if fresh_scripts:
        extract_package_files(catalog.bin64, fresh_scripts, catalog.extracted_root, overwrite=True)
    _prepare_script_dependencies(catalog, (*added_scripts, *references), output_dir, progress, workflow_issues)

    (output_dir / "所选副本.txt").write_text(
        "\n".join(f"{dungeon.display_name}  地图ID：{','.join(dungeon.map_ids) or '未关联'}  技能目录：{dungeon.skill_folder or '按地图引用'}" for dungeon in selected) + "\n",
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
    resources = dungeon_resources.export_dungeon_resources(catalog.extracted_root, map_ids, output_dir, progress)
    for dungeon in selected:
        if not dungeon.map_ids:
            workflow_issues.append({"kind": "map_association", "dungeon": dungeon.name, "message": "技能目录未能与 MapList 名称明确关联；技能结果保留，地图资料需通过 --map-id 指定。"})
    if map_ids and not npc_paths:
        workflow_issues.append({"kind": "npc_header_index", "map_ids": map_ids, "message": "NPC 表头索引未定位到所选地图分表；只采样前 32KB，不能据此断言地图没有 NPC。"})
    for kind in ("npc", "ai"):
        index_file = source_catalog.extracted_root / ".resource_indexes" / f"{kind}_heads" / "index.json"
        with suppress(OSError, ValueError):
            data = json.loads(index_file.read_text(encoding="utf-8"))
            if data.get("missing_heads"):
                workflow_issues.append({"kind": f"{kind}_header_index", "missing_heads": data["missing_heads"], "message": "部分注册分表在当前包中未读到，详情见解包记录。"})
    resources["workflow_issues"] = workflow_issues
    (output_dir / "资源读取状态.json").write_text(json.dumps({"selected_map_ids": map_ids, "npc_full_tables": npc_paths, "ai_full_tables": ai_paths, "issues": workflow_issues}, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    setattr(result, "resource_counts", resources.get("counts", resources))
    journal_after = set(catalog.extracted_root.glob(".extraction_logs/*.json")) | set(catalog.extracted_root.glob(".resource_indexes/*_heads/.extraction_logs/*.json"))
    journals = (journal_after - journal_before) | set(catalog.metadata_journals)
    for kind in ("npc", "ai"):
        with suppress(OSError, ValueError):
            index_file = source_catalog.extracted_root / ".resource_indexes" / f"{kind}_heads" / "index.json"
            index_data = json.loads(index_file.read_text(encoding="utf-8"))
            if index_data.get("journal"):
                journals.add(Path(index_data["journal"]))
    records = []
    for journal in sorted(journals):
        with suppress(OSError, ValueError):
            records.append(json.loads(journal.read_text(encoding="utf-8")))
    (output_dir / "解包记录.json").write_text(json.dumps({"source": "local PakV4", "selected_map_ids": map_ids, "batches": records}, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    try:
        payload = json.loads(result.json_file.read_text(encoding="utf-8-sig"))
        payload["package_bin64"] = str(catalog.bin64)
        payload["selected_dungeons"] = [dungeon.name for dungeon in selected]
        payload["selected_map_ids"] = map_ids
        payload["package_source"] = "local PakV4"
        payload["dungeon_resources"] = resources
        result.json_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8-sig",
            newline="\n",
        )
    except (OSError, ValueError, TypeError):
        pass
    return finalize_analysis_output(result)
