from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .lua_analysis import (
    BuffReference,
    LuaFacts,
    analyze_lua,
    detect_lua_kind,
    iter_lua_calls,
    normalize_dimension_expression,
    numeric_id,
    numeric_property_values,
    property_values,
    safe_numeric,
)


ProgressCallback = Callable[[int, int, str], None]

CAST_MODE_NAMES = {
    "CasterArea": "释放者周边范围",
    "CasterAreaOfAttention": "释放者面向范围",
    "CasterAreaOfDepth": "释放者纵深范围",
    "CasterConvexHullArea": "释放者凸包范围",
    "CasterSingle": "释放者自身",
    "CasterSpreadCircle": "释放者扩散圆",
    "Item": "道具",
    "PartyArea": "小队范围",
    "Point": "指定点",
    "PointArea": "指定区域",
    "PointAreaFindFirst": "首次生效区域",
    "PointAreaOfCasterTeam": "释放者小队区域",
    "PointRectangle": "指定矩形区域",
    "Rectangle": "矩形",
    "RectangleOfDepth": "纵深矩形",
    "Sector": "扇形",
    "SectorOfAttention": "面向扇形",
    "SectorOfDepth": "纵深扇形",
    "TargetAngleRectangle": "目标溅射矩形",
    "TargetAngleSector": "目标溅射扇形",
    "TargetArea": "目标周边范围",
    "TargetChain": "与目标间连线",
    "TargetHoodle": "目标弹射",
    "TargetLeader": "阵眼",
    "TargetRay": "目标射线",
    "TargetSingle": "目标单体",
    "TargetTeamArea": "目标小队范围",
}

KIND_DAMAGE_NAMES = {
    "Physics": "外功伤害",
    "SolarMagic": "阳性内功伤害",
    "LunarMagic": "阴性内功伤害",
    "NeutralMagic": "混元内功伤害",
    "Poison": "毒性内功伤害",
    "PoisonMagic": "毒性内功伤害",
}

SKILL_COLUMNS = [
    "技能ID",
    "技能名称(配置)",
    "技能名称(UI)",
    "UI描述",
    "UI短描述",
    "UI简述",
    "伤害类型",
    "是否穿透",
    "技能释放方式",
    "技能类型",
    "技能读条帧",
    "技能读条时间(秒)",
    "技能引导帧",
    "技能引导时间(秒)",
    "技能形状",
    "技能角度",
    "技能宽度",
    "技能高度",
    "技能作用半径",
    "技能保护半径",
    "最小释放距离",
    "最大释放距离",
    "目标数量上限",
    "技能控制效果",
    "技能是否可打断",
    "各等级基础伤害",
    "各等级伤害浮动",
    "添加给玩家的buff_id",
    "添加给NPC目标的buff_id",
    "可能添加给小怪/侠客/召唤物的buff_id",
    "添加给NPC的buff_id",
    "添加给其他对象的buff_id",
    "AOE绑定的buff_id",
    "属性调用的buff_id",
    "移除的buff_id",
    "触发的技能ID",
    "属性类型(原始)",
    "解析到的依赖函数",
]

BUFF_COLUMNS = [
    "来源技能ID",
    "来源技能名称",
    "关联方式",
    "BuffID表达式",
    "BuffID",
    "引用等级表达式",
    "配置等级",
    "Buff名称(配置)",
    "Buff名称(UI)",
    "UI描述",
    "Buff类型",
    "功能类型",
    "计数(Count)",
    "间隔帧(Interval)",
    "间隔时间(秒)",
    "估算持续时间(秒)",
    "最大层数",
    "是否可叠加",
    "Buff脚本",
    "属性概览",
]

ISSUE_COLUMNS = ["级别", "项目", "技能ID", "路径", "信息"]
OUTPUT_FOLDER_PREFIX = "技能解析"


@dataclass
class AnalyzerPaths:
    root: Path
    skills_tab: Path
    buff_tab: Path
    skill_ui: Path
    buff_ui: Path
    decompiler: Path

    @classmethod
    def from_root(
        cls,
        root: Path | str,
        *,
        skills_tab: Path | str | None = None,
        buff_tab: Path | str | None = None,
        skill_ui: Path | str | None = None,
        buff_ui: Path | str | None = None,
        decompiler: Path | str | None = None,
    ) -> "AnalyzerPaths":
        root_path = normalize_game_root(Path(root))
        return cls(
            root=root_path,
            skills_tab=Path(skills_tab) if skills_tab else root_path / "settings/skill/skills.tab",
            buff_tab=Path(buff_tab) if buff_tab else root_path / "settings/skill/buff.tab",
            skill_ui=Path(skill_ui) if skill_ui else root_path / "ui/Scheme/Case/skill.txt",
            buff_ui=Path(buff_ui) if buff_ui else root_path / "ui/Scheme/Case/buff.txt",
            decompiler=Path(decompiler) if decompiler else bundled_decompiler_path(),
        )

    def validate(self) -> None:
        required = {
            "游戏本地包根目录": self.root,
            "skills.tab": self.skills_tab,
            "buff.tab": self.buff_tab,
            "UI skill.txt": self.skill_ui,
            "UI buff.txt": self.buff_ui,
            "字节码反编译器": self.decompiler,
        }
        missing = [f"{name}: {path}" for name, path in required.items() if not path.exists()]
        if missing:
            raise FileNotFoundError("缺少必要文件：\n" + "\n".join(missing))


@dataclass
class SourceArtifact:
    path: Path
    kind: str
    version: str
    text: str
    decompile_status: str
    decompiled_output: Path | None = None
    dependencies: list[Path] = field(default_factory=list)
    server_dependencies: list[str] = field(default_factory=list)
    missing_dependencies: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ScriptResult:
    main: SourceArtifact
    dependencies: list[SourceArtifact]
    facts: LuaFacts


@dataclass
class RunResult:
    requested_ids: int
    successful_skills: int
    failed_skills: int
    compiled_scripts: int
    source_scripts: int
    buff_links: int
    output_dir: Path
    skill_csv: Path
    buff_csv: Path
    issue_csv: Path
    json_file: Path


def bundled_decompiler_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / "vendor/unluac.exe"
    return Path(__file__).resolve().parents[1] / "vendor/unluac.exe"


def create_run_output_dir(
    base_dir: Path | str,
    run_time: datetime | None = None,
) -> Path:
    """在用户选择的目录内创建按秒命名、不会覆盖旧结果的运行目录。"""
    base_path = Path(base_dir).expanduser().resolve()
    base_path.mkdir(parents=True, exist_ok=True)
    timestamp = (run_time or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    stem = f"{OUTPUT_FOLDER_PREFIX}_{timestamp}"
    for sequence in range(1, 10_000):
        name = stem if sequence == 1 else f"{stem}_{sequence:02d}"
        candidate = base_path / name
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"同一秒内创建的结果目录过多：{base_path}")


def normalize_game_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    candidates = [path, *path.parents]
    for candidate in candidates:
        if (candidate / "settings/skill/skills.tab").is_file() and (
            candidate / "scripts/skill"
        ).is_dir():
            return candidate
    return path


def discover_game_root(starts: Iterable[Path] = ()) -> Path | None:
    candidates = [*starts, Path.cwd(), Path(sys.executable).resolve().parent, Path(__file__).resolve()]
    checked: set[Path] = set()
    for start in candidates:
        try:
            current = start if start.is_dir() else start.parent
            for candidate in [current, *current.parents]:
                if candidate in checked:
                    continue
                checked.add(candidate)
                if (candidate / "settings/skill/skills.tab").is_file() and (
                    candidate / "scripts/skill"
                ).is_dir():
                    return candidate
        except OSError:
            continue
    return None


def read_text_auto(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("gb18030", errors="replace"), "gb18030-lossy"


def parse_skill_ids(path: Path | str) -> list[str]:
    text, _ = read_text_auto(Path(path))
    result: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = re.split(r"#|--|//", raw_line, maxsplit=1)[0]
        for value in re.findall(r"(?<!\d)\d+(?!\d)", line):
            normalized = str(int(value))
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    if not result:
        raise ValueError(f"没有从技能ID文件中读到有效数字：{path}")
    return result


def _tab_rows(path: Path) -> Iterator[dict[str, str]]:
    csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    text, _ = read_text_auto(path)
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    for row in reader:
        if row:
            yield {str(key).lstrip("\ufeff"): (value or "") for key, value in row.items() if key}


def load_selected_rows(path: Path, key: str, wanted: set[str]) -> dict[str, list[dict[str, str]]]:
    result = {item: [] for item in wanted}
    for row in _tab_rows(path):
        value = row.get(key, "").strip()
        if value in wanted:
            result.setdefault(value, []).append(row)
    return result


def _unquote_lua(value: str) -> str | None:
    value = value.strip()
    if len(value) < 2 or value[0] not in "'\"" or value[-1] != value[0]:
        return None
    body = value[1:-1]
    body = body.replace(r"\\", "\\").replace(r"\"", '"').replace(r"\'", "'")
    return body


@lru_cache(maxsize=256)
def _dependency_strings(text: str) -> tuple[str, ...]:
    result: list[str] = []
    for call in iter_lua_calls(text):
        call_name = re.split(r"[.:]", call.callee)[-1]
        if call_name == "Include" and call.arguments:
            candidate = _unquote_lua(call.arguments[0])
            if candidate:
                result.append(candidate)
        elif call_name in {"AddAttribute", "SetTimer", "ExecuteScript"}:
            for argument in call.arguments:
                candidate = _unquote_lua(argument)
                if candidate and candidate.lower().endswith(".lua"):
                    result.append(candidate)
    return tuple(dict.fromkeys(result))


def is_server_side_dependency(script_path: str) -> bool:
    """客户端本地包不会下发 scripts/Map 下的服务器逻辑。"""
    normalized = script_path.strip().replace("\\", "/").lstrip("/").lower()
    return normalized.startswith("scripts/map/")


class LuaLoader:
    def __init__(self, paths: AnalyzerPaths, output_dir: Path):
        self.paths = paths
        self.output_dir = output_dir
        self.cache: dict[Path, SourceArtifact] = {}
        self._lock = threading.Lock()

    def resolve_script(self, script_path: str, *, parent: Path | None = None) -> Path | None:
        normalized = script_path.strip().replace("\\", "/").lstrip("/")
        candidates: list[Path] = []
        if parent and not normalized.lower().startswith(("scripts/", "skill/", "npc/")):
            candidates.append(parent / normalized)
        if normalized.lower().startswith("scripts/"):
            candidates.append(self.paths.root / normalized)
        elif normalized.lower().startswith("skill/"):
            candidates.append(self.paths.root / "scripts" / normalized)
        else:
            candidates.append(self.paths.root / "scripts/skill" / normalized)
            candidates.append(self.paths.root / "scripts" / normalized)
            candidates.append(self.paths.root / normalized)
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _decompiled_destination(self, path: Path) -> Path:
        try:
            relative = path.resolve().relative_to(self.paths.root.resolve())
        except ValueError:
            relative = Path("external") / path.name
        destination = self.output_dir / "反编译脚本" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    def load(self, path: Path) -> SourceArtifact:
        resolved = path.resolve()
        with self._lock:
            cached = self.cache.get(resolved)
        if cached:
            return cached

        data = resolved.read_bytes()
        kind, version = detect_lua_kind(data)
        warnings: list[str] = []
        output_path: Path | None = None
        if kind == "source":
            text, encoding = read_text_auto(resolved)
            status = f"无需反编译({encoding})"
        elif kind == "bytecode":
            command = [
                str(self.paths.decompiler),
                "-i",
                str(resolved),
                "-e",
                "gbk",
                "-m",
                "lossy",
            ]
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags,
                check=False,
            )
            if completed.returncode != 0:
                error = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"反编译失败（退出码 {completed.returncode}）：{error}")
            text = completed.stdout.decode("utf-8", errors="replace")
            output_path = self._decompiled_destination(resolved)
            output_path.write_text(text, encoding="utf-8-sig", newline="\n")
            status = "反编译成功"
        else:
            raise RuntimeError(f"不是可识别的 Lua 源码或标准 Lua 字节码：{version}")

        artifact = SourceArtifact(
            path=resolved,
            kind=kind,
            version=version,
            text=text,
            decompile_status=status,
            decompiled_output=output_path,
            warnings=warnings,
        )
        with self._lock:
            self.cache[resolved] = artifact
        return artifact

    def load_with_dependencies(self, main_path: Path, max_dependencies: int = 40) -> tuple[SourceArtifact, list[SourceArtifact]]:
        main = self.load(main_path)
        dependencies: list[SourceArtifact] = []
        seen = {main.path}
        queue: list[tuple[str, Path]] = [
            (dependency, main.path.parent) for dependency in _dependency_strings(main.text)
        ]
        while queue and len(dependencies) < max_dependencies:
            raw_path, parent = queue.pop(0)
            resolved = self.resolve_script(raw_path, parent=parent)
            if resolved is None:
                if is_server_side_dependency(raw_path):
                    if raw_path not in main.server_dependencies:
                        main.server_dependencies.append(raw_path)
                elif raw_path.lower().endswith(".lua") and raw_path not in main.missing_dependencies:
                    main.missing_dependencies.append(raw_path)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                artifact = self.load(resolved)
            except Exception as exc:  # 单个依赖失败不应导致主技能完全失败
                main.warnings.append(f"依赖脚本处理失败 {raw_path}: {exc}")
                continue
            dependencies.append(artifact)
            main.dependencies.append(resolved)
            queue.extend((value, resolved.parent) for value in _dependency_strings(artifact.text))
        if queue:
            main.warnings.append(f"依赖脚本超过 {max_dependencies} 个，已停止继续展开")
        return main, dependencies


def _joined(values: Iterable[object], separator: str = ";") -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return separator.join(result)


def _level_sequence(values: Iterable[object]) -> str:
    return ";".join(f"Lv{index}={str(value).strip()}" for index, value in enumerate(values, 1))


def _ui_value(rows: list[dict[str, str]], field_name: str) -> str:
    if not rows:
        return ""
    ordered = sorted(rows, key=lambda row: (row.get("Level", "") not in {"", "0"}, row.get("Level", "")))
    return _joined(row.get(field_name, "") for row in ordered)


def _property_text(facts: LuaFacts, *names: str, dimension: bool = False) -> str:
    values = property_values(facts, *names)
    if dimension:
        values = [normalize_dimension_expression(value) for value in values]
    return _joined(values)


def _seconds_from_frames(facts: LuaFacts, *names: str) -> str:
    values: list[str] = []
    for expression in property_values(facts, *names):
        frame_value = safe_numeric(expression)
        if frame_value is not None:
            seconds = frame_value / 16
            values.append(f"{seconds:g}")
        else:
            values.append(f"无法计算({expression})")
    return _joined(values)


def _shape(facts: LuaFacts, cast_mode: str) -> str:
    if property_values(facts, "nRectWidth") or "Rectangle" in cast_mode:
        return "矩形"
    angle_values = numeric_property_values(facts, "nAngleRange")
    if angle_values and any(value == 256 for value in angle_values):
        return "圆形/全角"
    if angle_values or "Sector" in cast_mode:
        return "扇形/面向"
    if property_values(facts, "nAreaRadius") or "Area" in cast_mode:
        return "圆形范围"
    return ""


def _angle(facts: LuaFacts) -> str:
    values: list[str] = []
    for raw in property_values(facts, "nAngleRange"):
        number = safe_numeric(raw)
        values.append(f"{number * 360 / 256:g}°" if number is not None else raw)
    return _joined(values)


def _interruptible(facts: LuaFacts) -> str:
    values = numeric_property_values(facts, "nBrokenRate")
    if not values:
        return ""
    return "可打断" if any(value > 0.5 for value in values) else "不可打断"


def _skill_type(facts: LuaFacts) -> str:
    if property_values(facts, "nChannelFrame", "nChannelFrames"):
        return "引导技能"
    if property_values(facts, "nPrepareFrames", "nPrepareFrame"):
        return "读条技能"
    return "瞬发技能"


def _buff_ids_by_relation(references: list[BuffReference], relation: str) -> str:
    values = []
    for reference in references:
        if reference.relation == relation:
            values.append(str(reference.buff_id) if reference.buff_id is not None else reference.buff_id_expression)
    return _joined(values)


def _other_buff_ids(references: list[BuffReference]) -> str:
    known = {
        "添加给玩家",
        "添加给NPC目标",
        "添加给小怪/侠客/召唤物",
        "添加给NPC",
        "AOE绑定Buff",
        "属性调用Buff",
        "移除Buff",
    }
    return _joined(
        str(reference.buff_id) if reference.buff_id is not None else reference.buff_id_expression
        for reference in references
        if reference.relation not in known
    )


def _issue(issues: list[dict[str, str]], level: str, project: str, skill_id: str, path: object, message: str) -> None:
    issues.append(
        {
            "级别": level,
            "项目": project,
            "技能ID": skill_id,
            "路径": str(path or ""),
            "信息": message,
        }
    )


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _buff_attribute_summary(row: dict[str, str]) -> str:
    summaries: list[str] = []
    prefixes = (("Begin", 15), ("Active", 2), ("EndTime", 2))
    for prefix, count in prefixes:
        for index in range(1, count + 1):
            attribute = row.get(f"{prefix}Attrib{index}", "").strip()
            if not attribute:
                continue
            value_a = row.get(f"{prefix}Value{index}A", "").strip()
            value_b = row.get(f"{prefix}Value{index}B", "").strip()
            values = ",".join(value for value in (value_a, value_b) if value)
            summaries.append(f"{prefix}:{attribute}" + (f"({values})" if values else ""))
    return _joined(summaries)


def _frame_seconds(value: str) -> str:
    number = safe_numeric(value)
    return f"{number / 16:g}" if number is not None else ""


def _estimated_buff_seconds(count: str, interval: str) -> str:
    count_value = safe_numeric(count)
    interval_value = safe_numeric(interval)
    if count_value is None or interval_value is None or interval_value <= 0:
        return ""
    return f"{count_value * interval_value / 16:g}"


def _select_buff_rows(rows: list[dict[str, str]], level_expression: str) -> list[dict[str, str]]:
    requested_level = numeric_id(level_expression)
    if requested_level is None:
        return rows
    exact = [row for row in rows if numeric_id(row.get("Level", "")) == requested_level]
    if exact:
        return exact
    level_zero = [row for row in rows if row.get("Level", "").strip() in {"", "0"}]
    return level_zero or rows


def _build_buff_rows(
    skill_rows: list[dict[str, object]],
    skill_scripts: dict[str, str],
    script_results: dict[str, ScriptResult],
    buff_config: dict[str, list[dict[str, str]]],
    buff_ui: dict[str, list[dict[str, str]]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for skill in skill_rows:
        skill_id = str(skill["技能ID"])
        script_key = skill_scripts.get(skill_id, "")
        result = script_results.get(script_key)
        if not result:
            continue
        for reference in result.facts.buff_references:
            if reference.buff_id is None:
                output.append(
                    {
                        "来源技能ID": skill_id,
                        "来源技能名称": skill.get("技能名称(UI)") or skill.get("技能名称(配置)"),
                        "关联方式": reference.relation,
                        "BuffID表达式": reference.buff_id_expression,
                        "BuffID": "",
                        "引用等级表达式": reference.level_expression,
                    }
                )
                continue
            buff_id = str(reference.buff_id)
            config_rows = _select_buff_rows(buff_config.get(buff_id, []), reference.level_expression)
            ui_rows = _select_buff_rows(buff_ui.get(buff_id, []), reference.level_expression)
            levels = _joined(
                [row.get("Level", "") for row in config_rows + ui_rows]
            ).split(";")
            levels = [level for level in levels if level] or [""]
            for level in levels:
                configs = [row for row in config_rows if row.get("Level", "") == level] or ([config_rows[0]] if config_rows else [{}])
                matching_ui = [row for row in ui_rows if row.get("Level", "") == level]
                ui_row = (matching_ui or ui_rows or [{}])[0]
                for config in configs:
                    count = config.get("Count", "")
                    interval = config.get("Interval", "")
                    output.append(
                        {
                            "来源技能ID": skill_id,
                            "来源技能名称": skill.get("技能名称(UI)") or skill.get("技能名称(配置)"),
                            "关联方式": reference.relation,
                            "BuffID表达式": reference.buff_id_expression,
                            "BuffID": buff_id,
                            "引用等级表达式": reference.level_expression,
                            "配置等级": level,
                            "Buff名称(配置)": config.get("Name", ""),
                            "Buff名称(UI)": ui_row.get("Name", ""),
                            "UI描述": ui_row.get("Desc", ""),
                            "Buff类型": config.get("BuffType", ""),
                            "功能类型": config.get("FunctionType", ""),
                            "计数(Count)": count,
                            "间隔帧(Interval)": interval,
                            "间隔时间(秒)": _frame_seconds(interval),
                            "估算持续时间(秒)": _estimated_buff_seconds(count, interval),
                            "最大层数": config.get("MaxStackNum", ""),
                            "是否可叠加": config.get("IsStackable", ""),
                            "Buff脚本": config.get("ScriptFile", ""),
                            "属性概览": _buff_attribute_summary(config),
                        }
                    )
    return output


def analyze_skill_ids(
    paths: AnalyzerPaths,
    skill_ids: list[str],
    output_dir: Path | str,
    progress: ProgressCallback | None = None,
) -> RunResult:
    paths.validate()
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    total_steps = max(1, len(skill_ids) + 5)
    current_step = 0

    def emit(message: str) -> None:
        nonlocal current_step
        current_step += 1
        if progress:
            progress(min(current_step, total_steps), total_steps, message)

    wanted = set(skill_ids)
    issues: list[dict[str, str]] = []
    emit("正在读取 skills.tab")
    skill_config = load_selected_rows(paths.skills_tab, "SkillID", wanted)
    emit("正在读取技能 UI 描述")
    skill_ui = load_selected_rows(paths.skill_ui, "SkillID", wanted)

    loader = LuaLoader(paths, output_path)
    script_results: dict[str, ScriptResult] = {}
    skill_scripts: dict[str, str] = {}
    output_skills: list[dict[str, object]] = []
    compiled_count = 0
    source_count = 0

    for skill_id in skill_ids:
        rows = skill_config.get(skill_id, [])
        if not rows:
            _issue(issues, "错误", "技能表", skill_id, paths.skills_tab, "skills.tab 中没有这个 SkillID")
            emit(f"技能 {skill_id}：未在技能表中找到")
            continue
        if len(rows) > 1:
            _issue(issues, "提示", "技能表", skill_id, paths.skills_tab, f"找到 {len(rows)} 行，使用第一行")
        config = rows[0]
        relative_script = config.get("ScriptFile", "").strip()
        resolved_script = loader.resolve_script(relative_script)
        if not relative_script or resolved_script is None:
            _issue(issues, "错误", "技能脚本", skill_id, relative_script, "技能脚本不存在或路径无法解析")
            emit(f"技能 {skill_id}：脚本不存在")
            continue
        script_key = str(resolved_script)
        try:
            if script_key not in script_results:
                main, dependencies = loader.load_with_dependencies(resolved_script)
                facts = analyze_lua(main.text, [item.text for item in dependencies])
                facts.warnings.extend(main.warnings)
                if main.missing_dependencies:
                    facts.warnings.append("本地依赖未找到: " + ";".join(main.missing_dependencies))
                script_results[script_key] = ScriptResult(main, dependencies, facts)
                if main.kind == "bytecode":
                    compiled_count += 1
                else:
                    source_count += 1
            result = script_results[script_key]
        except Exception as exc:
            _issue(issues, "错误", "技能脚本", skill_id, resolved_script, str(exc))
            emit(f"技能 {skill_id}：处理失败")
            continue

        facts = result.facts
        skill_scripts[skill_id] = script_key
        ui_rows = skill_ui.get(skill_id, [])
        damage_types = facts.damage_types[:]
        fallback_damage = KIND_DAMAGE_NAMES.get(config.get("KindType", ""), "")
        if not damage_types and fallback_damage:
            damage_types.append(fallback_damage + "(来自skills.tab)")
        cast_mode = config.get("CastMode", "")
        relation_notes = list(facts.warnings)
        for warning in relation_notes:
            _issue(issues, "提示", "脚本依赖", skill_id, resolved_script, warning)

        output_skills.append(
            {
                "技能ID": skill_id,
                "技能名称(配置)": config.get("SkillName", ""),
                "技能名称(UI)": _ui_value(ui_rows, "Name"),
                "UI描述": _ui_value(ui_rows, "Desc"),
                "UI短描述": _ui_value(ui_rows, "ShortDesc"),
                "UI简述": _ui_value(ui_rows, "SimpleDesc"),
                "伤害类型": _joined(damage_types),
                "是否穿透": "可穿透" if facts.penetration else "",
                "技能释放方式": CAST_MODE_NAMES.get(cast_mode, cast_mode),
                "技能类型": _skill_type(facts),
                "技能读条帧": _property_text(facts, "nPrepareFrames", "nPrepareFrame"),
                "技能读条时间(秒)": _seconds_from_frames(facts, "nPrepareFrames", "nPrepareFrame"),
                "技能引导帧": _property_text(facts, "nChannelFrame", "nChannelFrames"),
                "技能引导时间(秒)": _seconds_from_frames(facts, "nChannelFrame", "nChannelFrames"),
                "技能形状": _shape(facts, cast_mode),
                "技能角度": _angle(facts),
                "技能宽度": _property_text(facts, "nRectWidth", dimension=True),
                "技能高度": _property_text(facts, "nHeight", dimension=True),
                "技能作用半径": _property_text(facts, "nAreaRadius", dimension=True),
                "技能保护半径": _property_text(facts, "nProtectRadius", dimension=True),
                "最小释放距离": _property_text(facts, "nMinRadius", dimension=True),
                "最大释放距离": _property_text(facts, "nMaxRadius", dimension=True),
                "目标数量上限": _property_text(facts, "nTargetCountLimit"),
                "技能控制效果": _joined(facts.controls),
                "技能是否可打断": _interruptible(facts),
                "各等级基础伤害": _level_sequence(facts.damage_base),
                "各等级伤害浮动": _level_sequence(facts.damage_rand),
                "添加给玩家的buff_id": _buff_ids_by_relation(facts.buff_references, "添加给玩家"),
                "添加给NPC目标的buff_id": _buff_ids_by_relation(facts.buff_references, "添加给NPC目标"),
                "可能添加给小怪/侠客/召唤物的buff_id": _buff_ids_by_relation(facts.buff_references, "添加给小怪/侠客/召唤物"),
                "添加给NPC的buff_id": _buff_ids_by_relation(facts.buff_references, "添加给NPC"),
                "添加给其他对象的buff_id": _other_buff_ids(facts.buff_references),
                "AOE绑定的buff_id": _buff_ids_by_relation(facts.buff_references, "AOE绑定Buff"),
                "属性调用的buff_id": _buff_ids_by_relation(facts.buff_references, "属性调用Buff"),
                "移除的buff_id": _buff_ids_by_relation(facts.buff_references, "移除Buff"),
                "触发的技能ID": _joined(facts.called_skill_ids),
                "属性类型(原始)": _joined(facts.attribute_types),
                "解析到的依赖函数": _joined(facts.reachable_helpers),
            }
        )
        emit(f"技能 {skill_id}：解析完成")

    numeric_buff_ids = {
        str(reference.buff_id)
        for result in script_results.values()
        for reference in result.facts.buff_references
        if reference.buff_id is not None
    }
    emit("正在读取关联 Buff 配置")
    buff_config = load_selected_rows(paths.buff_tab, "ID", numeric_buff_ids) if numeric_buff_ids else {}
    emit("正在读取关联 Buff UI 描述")
    buff_ui = load_selected_rows(paths.buff_ui, "BuffID", numeric_buff_ids) if numeric_buff_ids else {}
    buff_rows = _build_buff_rows(output_skills, skill_scripts, script_results, buff_config, buff_ui)

    skill_csv = output_path / "技能解析.csv"
    buff_csv = output_path / "关联Buff.csv"
    issue_csv = output_path / "错误与警告.csv"
    json_file = output_path / "解析结果.json"
    _write_csv(skill_csv, SKILL_COLUMNS, output_skills)
    _write_csv(buff_csv, BUFF_COLUMNS, buff_rows)
    _write_csv(issue_csv, ISSUE_COLUMNS, issues)

    json_payload = {
        "version": "3.0.2",
        "game_root": str(paths.root),
        "requested_skill_ids": skill_ids,
        "skills": output_skills,
        "buffs": buff_rows,
        "issues": issues,
        "scripts": {
            key: {
                "main": {
                    "path": str(value.main.path),
                    "kind": value.main.kind,
                    "version": value.main.version,
                    "decompile_status": value.main.decompile_status,
                    "decompiled_output": str(value.main.decompiled_output or ""),
                    "server_side_dependencies": value.main.server_dependencies,
                    "missing_dependencies": value.main.missing_dependencies,
                },
                "dependencies": [str(item.path) for item in value.dependencies],
                "facts": value.facts.to_dict(),
            }
            for key, value in script_results.items()
        },
    }
    json_file.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8-sig",
        newline="\n",
    )
    emit("结果文件已生成")

    return RunResult(
        requested_ids=len(skill_ids),
        successful_skills=len(output_skills),
        failed_skills=len(skill_ids) - len(output_skills),
        compiled_scripts=compiled_count,
        source_scripts=source_count,
        buff_links=len(buff_rows),
        output_dir=output_path,
        skill_csv=skill_csv,
        buff_csv=buff_csv,
        issue_csv=issue_csv,
        json_file=json_file,
    )


def analyze_ids_file(
    paths: AnalyzerPaths,
    ids_file: Path | str,
    output_dir: Path | str,
    progress: ProgressCallback | None = None,
) -> RunResult:
    skill_ids = parse_skill_ids(ids_file)
    paths.validate()
    run_output_dir = create_run_output_dir(output_dir)
    return analyze_skill_ids(paths, skill_ids, run_output_dir, progress)
