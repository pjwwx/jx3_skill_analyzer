"""Static dungeon evidence from one extracted V4 resource snapshot.

This module never opens a package or executes game Lua.  Missing resources remain
missing; authored placements, handbook references and template health are kept
separate from live encounter behavior.
"""
from __future__ import annotations

import configparser
import csv
import hashlib
import io
import json
import math
import re
import struct
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

ProgressCallback = Callable[[int, int, str], None]
DUNGEON_METADATA_PATHS = (
    "settings/MapList.tab", "settings/NpcTemplateList.tab",
    "settings/NpcTemplateDefault.tab", "settings/NpcTemplatePlus.tab",
    "settings/NpcTemplateIdentity.tab", "settings/AITypeList.tab",
    "settings/AITypeDefault.tab", "settings/AITemplate.tab",
    "settings/DoodadTemplate.tab", "settings/DoodadClass.tab",
    "settings/LogicForceList.tab", "settings/RelationCamp.tab",
    "settings/RelationForce.tab", "settings/MiddleMapNpcMark.tab",
    "ui/Scheme/Case/NpcTemplate.tab", "ui/Scheme/Case/DoodadTemplate.tab",
    "ui/Scheme/Case/MiddlemapDungeonBoss.tab",
    "ui/Scheme/Case/cyclopaedia/DungeonInfo.txt",
    "ui/Scheme/Case/cyclopaedia/DungeonBoss.txt",
    "ui/Scheme/Case/cyclopaedia/DungeonNpc.txt",
    "ui/Scheme/Case/cyclopaedia/DungeonSkill.txt",
)
RESOURCE_METADATA_PATHS = DUNGEON_METADATA_PATHS
DBM_SCHEMA = (
    "nID", "nType", "nMapID", "nTargetID", "nTargetLevel", "nTargetType",
    "nActionID", "nAction", "tbSkill", "nProtectTime", "nKey", "nClearKey",
    "nClearType", "nAddTimekey", "nTime", "nStopTimer", "bTimeCeil",
    "bIsBoss", "szComment",
)
DBM_TYPES = {"1": "Buff", "2": "技能", "3": "NPC"}
DBM_ACTIONS = {
    "1": {"1": "获得", "2": "丢失"},
    "2": {"1": "施法开始日志", "2": "技能施放事件"},
    "3": {"1": "开战", "2": "血量百分比下降跨过阈值",
          "3": "蓝量百分比下降跨过阈值", "4": "离场（官方死亡规则）",
          "5": "同模板全离场（官方全死规则）", "6": "出现场景"},
}
LIMITATIONS = [
    "仅关联本次提取目录中的文件，不从其他版本或远端补全。",
    "NPC模板、地图预置、官方百科和DBM提示是不同证据；均不代表完整运行时刷怪。",
    "MaxLife与MaxLifeCount保留原值，不计算确定实战血量；空值默认继承仅作为候选。",
    "交互物品操作、拾取与尸体引用保留配置证据，实际触发条件和掉落未核验。",
    "百科技能文案ID不是运行时技能ID；UI可能复用其他难度的NPC展示模型。",
    "DBM时间为事件触发后的预告秒数，不是从进本开始的固定时间轴。",
]


def _internal(value: str) -> str:
    value = str(value).strip().replace("\\", "/").lstrip("/")
    if not value or any(p in {"", ".", ".."} or ":" in p for p in value.split("/")):
        raise ValueError(f"无效资源路径：{value}")
    return value


def _ids(value: str) -> list[str]:
    return [v for v in re.split(r"[;、,，\s]+", value or "") if v]


def _id(value: object) -> str:
    text = str(value or "").strip()
    return str(int(text)) if re.fullmatch(r"\d+", text) else text


def _number(value: str) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _base_name(name: str) -> str:
    return re.sub(r"^(?:\d+人)?(?:普通|英雄|挑战)", "", re.sub(r"^\d+人", "", name))


class _Snapshot:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data: dict[str, bytes | None] = {}
        self.sources: dict[str, dict] = {}
        self.tables: dict[str, list[dict]] = {}
        self.issues: list[dict] = []
        self._missing: set[str] = set()
        self.allowed: set[str] | None = None
        manifest = self.root / ".full_resource_allowlist.json"
        if manifest.is_file():
            before = manifest.stat(); raw = manifest.read_bytes(); after = manifest.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("本轮完整资源名单在读取时发生变化，请重试")
            try:
                paths = json.loads(raw.decode("utf-8-sig"))["paths"]
                if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                    raise ValueError("paths须为字符串列表")
                self.allowed = {_internal(p).casefold() for p in paths}
            except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
                raise ValueError(f"本轮完整资源名单无效，拒绝混读旧缓存：{exc}") from exc
            rel = ".full_resource_allowlist.json"
            self.allowed.add(rel)
            self.data[rel] = raw
            self.sources[rel] = {"path": rel, "sha256": hashlib.sha256(raw).hexdigest(),
                                 "bytes": len(raw), "mtime_ns": after.st_mtime_ns, "encoding": "utf-8-sig"}

    def allows(self, rel: str) -> bool:
        return self.allowed is None or _internal(rel).casefold() in self.allowed

    def issue(self, path: str, message: str, row: int | str = "", level: str = "提示") -> None:
        self.issues.append({"level": level, "path": path, "row": row, "message": message})

    def path(self, rel: str) -> Path:
        path = (self.root / _internal(rel)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("资源路径超出本次提取目录")
        return path

    def read(self, rel: str, *, required: bool = False) -> bytes | None:
        rel = _internal(rel)
        if not self.allows(rel):
            if required and rel not in self._missing:
                self.issue(rel, "本轮完整资源名单未包含此文件，保持缺失状态")
                self._missing.add(rel)
            return None
        if rel in self.data:
            return self.data[rel]
        path = self.path(rel)
        if not path.is_file():
            self.data[rel] = None
            if required and rel not in self._missing:
                self._missing.add(rel)
                self.issue(rel, "本次提取快照缺少该资料，保留缺失状态")
            return None
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"读取期间文件发生变化，请重试：{rel}")
        self.data[rel] = raw
        self.sources[rel] = {"path": rel, "sha256": hashlib.sha256(raw).hexdigest(),
                             "bytes": len(raw), "mtime_ns": after.st_mtime_ns}
        return raw

    def text(self, rel: str, *, required: bool = False) -> str | None:
        raw = self.read(rel, required=required)
        if raw is None:
            return None
        if raw.startswith(b"\x1bLua") or b"\x00" in raw[:256] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            self.issue(rel, "该文件是字节码或未知二进制，未按文本解释", level="警告")
            return None
        encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
        for encoding in encodings:
            try:
                text = raw.decode(encoding)
                self.sources[_internal(rel)]["encoding"] = encoding
                return text
            except UnicodeDecodeError:
                continue
        self.issue(rel, "文本编码无法无损识别，未用替代字符继续解析", level="警告")
        return None

    def table(self, rel: str, *, required: bool = False) -> list[dict]:
        rel = _internal(rel)
        if rel in self.tables:
            return self.tables[rel]
        text = self.text(rel, required=required)
        if text is None:
            return []
        csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
        reader = csv.reader(io.StringIO(text), delimiter="\t")
        header = next(reader, [])
        header = [v.lstrip("\ufeff") for v in header]
        dbm = bool(re.search(r"BossDbmMap\d+\.tab$", rel, re.I))
        if dbm and (len(header) != 19 or tuple(header[:18]) != DBM_SCHEMA[:18] or header[18] not in {"", "szComment"}):
            self.issue(rel, "官方DBM表头不符合已验证的19列布局，拒绝猜测列含义", level="警告")
            self.tables[rel] = []
            return []
        keys = list(DBM_SCHEMA) if dbm else [key or f"__column_{i + 1}" for i, key in enumerate(header)]
        if len(set(keys)) != len(keys):
            self.issue(rel, "表头含重复字段名，拒绝覆盖原列", level="警告")
            self.tables[rel] = []
            return []
        rows = []
        for cells in reader:
            if not cells or not any(cells):
                continue
            line = reader.line_num
            if len(cells) != len(header):
                self.issue(rel, f"数据列数{len(cells)}与表头{len(header)}不一致，保留原行", line, "警告")
            raw = dict(zip(keys, cells))
            rows.append({"raw": raw, "raw_cells": cells, "raw_header": header,
                         "schema_valid": not dbm or len(cells) == 19,
                         "source": dict(self.sources[rel], row=line)})
        self.tables[rel] = rows
        return rows

    def script(self, value: str) -> dict:
        if not value:
            return {"raw_path": value, "status": "not_configured"}
        try:
            rel = _internal(value)
            raw = self.read(rel)
        except ValueError:
            return {"raw_path": value, "status": "unsafe_path"}
        return {"raw_path": value, "path": rel,
                "status": "missing_in_snapshot" if raw is None else "bytecode_present" if raw.startswith(b"\x1bLua") else "file_present",
                "source": self.sources.get(rel)}

    def finish(self) -> str:
        for rel, source in self.sources.items():
            stat = self.path(rel).stat()
            if (stat.st_size, stat.st_mtime_ns) != (source["bytes"], source["mtime_ns"]):
                raise RuntimeError(f"导出期间源文件发生变化，请重试：{rel}")
        digest = hashlib.sha256()
        for rel, source in sorted(self.sources.items()):
            digest.update(rel.encode("utf-8") + b"\0" + source["sha256"].encode("ascii") + b"\n")
        return digest.hexdigest()


def _available_tables(snapshot: _Snapshot, folder: str, list_path: str) -> list[str]:
    """Only complete files below root; header index folders are never searched."""
    paths = []
    for row in snapshot.table(list_path):
        value = row["raw"].get("FilePath", "")
        if value:
            try:
                rel = _internal(value)
                if snapshot.allows(rel) and snapshot.path(rel).is_file():
                    paths.append(rel)
            except ValueError:
                snapshot.issue(list_path, "登记路径不安全，已跳过", row["source"]["row"], "警告")
    directory = snapshot.path(folder)
    if directory.is_dir():
        paths.extend(p.relative_to(snapshot.root).as_posix() for p in directory.rglob("*.tab")
                     if p.is_file() and snapshot.allows(p.relative_to(snapshot.root).as_posix()))
    return list(dict.fromkeys(paths))


def _maps(snapshot: _Snapshot, ids: Iterable[str]) -> list[dict]:
    selected = {_id(i) for i in ids}
    maps = [r for r in snapshot.table("settings/MapList.tab", required=True) if _id(r["raw"].get("ID")) in selected]
    for missing in selected - {_id(r["raw"].get("ID")) for r in maps}:
        snapshot.issue("settings/MapList.tab", f"本快照未找到选择的地图ID {missing}")
    return maps


def resource_paths_for_maps(extracted_root: Path, selected_map_ids: list[str]) -> list[str]:
    """Explicit MapList-derived paths; caller discovers NPC/AI tables via index heads."""
    snapshot = _Snapshot(extracted_root)
    result = []
    for maprow in _maps(snapshot, selected_map_ids):
        row = maprow["raw"]
        mid = _id(row.get("ID"))
        name = row.get("Name", "")
        result.append(f"ui/Scheme/Case/BossDbm/BossDbmMap{mid}.tab")
        if name:
            result.extend(f"maps/{name}/{name}.{suffix}" for suffix in ("npc", "doodad", "entity", "cfg"))
            result.append(f"data/source/maps/{name}/{name}.Map.Logical")
        resource = row.get("ResourcePath", "").replace("\\", "/")
        if resource:
            resource_path = Path(resource)
            result.append((resource_path.parent / f"{resource_path.stem}.Map.Logical").as_posix())
        if row.get("ScriptFile"):
            result.append(row["ScriptFile"])
    return _safe_unique(result)


def _safe_unique(paths: Iterable[str]) -> list[str]:
    result = []; seen = set()
    for value in paths:
        try:
            path = _internal(value)
        except ValueError:
            continue
        if path.casefold() not in seen:
            seen.add(path.casefold()); result.append(path)
    return result


def decode_map_placements(data: bytes, kind: str) -> list[dict]:
    """Validated version-4 layout only; unknown version/length fails closed."""
    if kind not in {"NPC", "Doodad"}:
        raise ValueError("只能解析NPC或交互物品预置")
    if len(data) < 8:
        raise ValueError("地图预置头部不足8字节")
    version, count = struct.unpack_from("<II", data)
    stride, offsets = (96, (32, 36, 40, 44)) if kind == "NPC" else (86, (32, 37, 41, 45))
    if version != 4 or len(data) != 8 + count * stride:
        raise ValueError(f"未知地图预置布局：版本{version}、数量{count}、长度{len(data)}")
    rows = []
    for index in range(count):
        record = data[8 + index * stride:8 + (index + 1) * stride]
        if b"\0" not in record[:32]:
            raise ValueError("地图预置昵称缺少已验证的零终止符")
        nickname = record[:32].split(b"\0", 1)[0].decode("gb18030")
        template, x, y, z = (struct.unpack_from("<i", record, off)[0] for off in offsets)
        rows.append({"kind": kind, "binary_record_index": index, "template_id": str(template),
                     "nickname": nickname, "nX": x, "nY": y, "nZ": z,
                     "raw_record_hex": record.hex(), "format_version": version, "record_stride": stride,
                     "evidence_type": "map_authored_placement_not_runtime_spawn"})
    return rows


def _placements(snapshot: _Snapshot, maps: list[dict]) -> list[dict]:
    rows = []; seen = set()
    for maprow in maps:
        rawmap = maprow["raw"]; mid = _id(rawmap.get("ID")); name = rawmap.get("Name", "")
        for kind, suffix in (("NPC", "npc"), ("Doodad", "doodad")):
            rel = f"maps/{name}/{name}.{suffix}"
            if not name or rel in seen:
                continue
            seen.add(rel); data = snapshot.read(rel, required=True)
            if data is None:
                continue
            try:
                decoded = decode_map_placements(data, kind)
            except (ValueError, UnicodeDecodeError) as exc:
                snapshot.issue(rel, str(exc), level="警告"); continue
            for record in decoded:
                rows.append(dict(record, map_id=mid, map_name=name, source=dict(snapshot.sources[rel])))
    return rows


def _logical_maps(snapshot: _Snapshot, maps: list[dict]) -> list[dict]:
    """Keep authored INI fields separate from binary placement record indexes."""
    paths = []
    for entry in maps:
        raw = entry["raw"]; name = raw.get("Name", "")
        if name:
            paths.append(f"data/source/maps/{name}/{name}.Map.Logical")
        resource = raw.get("ResourcePath", "").replace("\\", "/")
        if resource:
            path = Path(resource)
            paths.append((path.parent / f"{path.stem}.Map.Logical").as_posix())
    result = []
    for rel in _safe_unique(paths):
        text = snapshot.text(rel)
        if text is None:
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        try:
            parser.read_string(text)
        except configparser.Error as exc:
            snapshot.issue(rel, f"地图Logical不是可验证的INI：{exc}", level="警告")
            continue
        if not parser.has_section("MAIN"):
            snapshot.issue(rel, "地图Logical缺少MAIN段，未猜测归属", level="警告")
            continue
        main = dict(parser["MAIN"])
        sections = {section: dict(parser[section]) for section in parser.sections() if section != "MAIN"}
        for prefix, countfield in (("NPC", "NumNPC"), ("Doodad", "NumDoodad")):
            actual = sum(bool(re.fullmatch(prefix + r"\d+", section)) for section in sections)
            if main.get(countfield, "").isdigit() and actual != int(main[countfield]):
                snapshot.issue(rel, f"{countfield}声明与实际段数不同，保留原始段", level="警告")
        result.append({"logical_scene_id_raw": main.get("LogicalSceneID", ""), "main_raw": main,
                       "sections_raw": sections, "source": dict(snapshot.sources[rel]),
                       "evidence_type": "map_authored_logic_configuration_not_runtime_state"})
    return result


def _encyclopaedia(snapshot: _Snapshot, mapids: set[str]) -> list[dict]:
    base = "ui/Scheme/Case/cyclopaedia/"
    tables = {key: snapshot.table(base + key + ".txt", required=True) for key in ("DungeonInfo", "DungeonBoss", "DungeonNpc", "DungeonSkill")}
    infos = {_id(r["raw"].get("MapID")): r for r in tables["DungeonInfo"]}
    npcs = {_id(r["raw"].get("Index")): r for r in tables["DungeonNpc"]}
    guides = {_id(r["raw"].get("ID")): r for r in tables["DungeonSkill"]}
    result = []
    for boss in tables["DungeonBoss"]:
        mid = _id(boss["raw"].get("MapID"))
        if mid not in mapids:
            continue
        npc_refs = [{"index_raw": index, "record": npcs.get(_id(index)), "status": "resolved" if _id(index) in npcs else "missing_in_snapshot"} for index in _ids(boss["raw"].get("Index", ""))]
        docs = []
        for stage in range(1, 6):
            field = f"Step{stage}Skill" if stage < 5 else "szStep5Skill"
            for guideid in _ids(boss["raw"].get(field, "")):
                docs.append({"stage": stage, "field": field, "guide_document_id": guideid,
                             "record": guides.get(_id(guideid)), "evidence_type": "guide_document_id_not_runtime_skill_id"})
        result.append({"map_id": mid, "map_info": infos.get(mid), "boss": boss,
                       "npc_references": npc_refs, "guide_documents": docs,
                       "evidence_type": "official_ui_encyclopaedia_reference"})
    return result


def _dbm(snapshot: _Snapshot, mapids: set[str]) -> tuple[list[dict], list[dict]]:
    rules = []; prompts = []
    for mid in sorted(mapids):
        rel = f"ui/Scheme/Case/BossDbm/BossDbmMap{mid}.tab"
        for entry in snapshot.table(rel, required=True):
            raw = entry["raw"]; valid = entry.get("schema_valid", True)
            typ = raw.get("nType", "") if valid else ""
            action = raw.get("nActionID", "") if valid else ""
            record = dict(entry, file_map_id=mid, object_type=DBM_TYPES.get(typ, "未设置/未知"),
                          action=DBM_ACTIONS.get(typ, {}).get(action, "未设置/未知"),
                          active_type=typ in DBM_TYPES, countdown_pairs=[],
                          evidence_type="official_dbm_event_rule_not_fixed_timeline")
            if raw.get("nMapID") and _id(raw["nMapID"]) != mid:
                snapshot.issue(rel, "行内nMapID与文件地图ID不同，已同时保留", entry["source"]["row"], "警告")
            record["caster_npc_template_id_raw"] = raw.get("nAction", "") if typ == "2" else ""
            record["active_boss_filter_raw"] = raw.get("nAction", "") if typ == "1" else ""
            record["percent_threshold_raw"] = raw.get("nAction", "") if typ == "3" and action in {"2", "3"} else ""
            sequence = raw.get("tbSkill", "") if valid else ""
            for index, (label, time) in enumerate(re.findall(r"\[(.*?),(.*?)\]", sequence, re.S), 1):
                label = label.strip(); time = time.strip()
                if len(label) >= 2 and label[0] == label[-1] and label[0] in "\"'":
                    label = label[1:-1]
                seconds = _number(time)
                pair = {"index": index, "label": label, "seconds_raw": time, "seconds_after_trigger": seconds,
                        "displayed_by_official_sort": seconds is not None and seconds >= 0}
                record["countdown_pairs"].append(pair)
                prompts.append(dict(pair, file_map_id=mid, rule_id_raw=raw.get("nID", ""),
                                    object_type=record["object_type"], target_id_raw=raw.get("nTargetID", ""),
                                    action=record["action"], source=entry["source"],
                                    evidence_type="official_dbm_prompt_after_event"))
                if seconds is None:
                    snapshot.issue(rel, "DBM预告时间不是数字，保留原文", entry["source"]["row"], "警告")
            if sequence.strip() and not record["countdown_pairs"]:
                snapshot.issue(rel, "tbSkill非空但没有可识别的[文案,秒数]对，原文已保留", entry["source"]["row"], "警告")
            rules.append(record)
    return rules, prompts


def _load(snapshot: _Snapshot, mapids: list[str], *, detail: bool = True) -> dict:
    maps = _maps(snapshot, mapids); ids = {_id(i) for i in mapids}
    names = {r["raw"].get("Name", "") for r in maps}; bases = {_base_name(n) for n in names}
    placements = _placements(snapshot, maps)
    logical_maps = _logical_maps(snapshot, maps) if detail else []
    encyclopaedia = _encyclopaedia(snapshot, ids) if detail else []
    rules, prompts = _dbm(snapshot, ids)
    map_index = {_id(r["raw"].get("ID")): r["raw"] for r in maps}
    for entry in rules + prompts:
        mapraw = map_index.get(entry["file_map_id"], {})
        entry["file_map_name"] = mapraw.get("Name", "")
        entry["MapName"] = mapraw.get("Name", "")
        entry["MaxPlayerCount"] = mapraw.get("MaxPlayerCount", "")
    for entry in encyclopaedia:
        mapraw = map_index.get(entry["map_id"], {})
        entry["map_name"] = mapraw.get("Name", "")
        entry["max_player_count_raw"] = mapraw.get("MaxPlayerCount", "")
    wanted_npc = {r["template_id"] for r in placements if r["kind"] == "NPC"}
    wanted_doodad = {r["template_id"] for r in placements if r["kind"] == "Doodad"}
    wanted_npc.update(_id(r["raw"].get("nTargetID")) for r in rules if r["active_type"] and r["raw"].get("nType") == "3")
    wanted_npc.update(_id(r["raw"].get("nAction")) for r in rules if r["active_type"] and r["raw"].get("nType") == "2" and r["raw"].get("nAction"))
    gameplay_npc = set(wanted_npc)
    for boss in encyclopaedia:
        wanted_npc.update(_id(ref["record"]["raw"].get("NpcID")) for ref in boss["npc_references"] if ref["record"])
    defaults = snapshot.table("settings/NpcTemplateDefault.tab", required=True) if detail else []
    default = defaults[0] if defaults else None
    plus = {r["raw"].get("ID", ""): r for r in snapshot.table("settings/NpcTemplatePlus.tab")} if detail else {}
    ui_npc = {r["raw"].get("ID", r["raw"].get("nID", "")): r for r in snapshot.table("ui/Scheme/Case/NpcTemplate.tab")} if detail else {}
    npc_rows = []
    for rel in _available_tables(snapshot, "settings/NpcTemplate", "settings/NpcTemplateList.tab"):
        if Path(rel).name.casefold() == "npctemplatedefault.tab":
            continue
        for entry in snapshot.table(rel):
            raw = entry["raw"]; template = _id(raw.get("ID")); mapname = raw.get("MapName", "")
            if mapname not in names and template not in wanted_npc:
                continue
            slots = [{"slot": i, "raw": {field + str(i): raw.get(field + str(i), "") for field in ("SkillID", "SkillLevel", "SkillInterval", "SkillFirstInterval", "SkillType", "SkillRate", "SkillAniFrame", "SkillRestFrame")}} for i in range(1, 9)]
            npc_rows.append(dict(entry, template_id=template, skill_slots=slots,
                                 default_record=default, class_record=plus.get(template), ui_record=ui_npc.get(template),
                                 script=snapshot.script(raw.get("ScriptName", "")), ai_records=[],
                                 selection_reason="template_map_name" if mapname in names else "map_or_dbm_template_reference" if template in gameplay_npc else "official_ui_model_reference",
                                 used_for_runtime_skill_discovery=mapname in names or template in gameplay_npc,
                                 evidence_type="npc_template_configuration", live_health_status="not_verified"))
    ai_wanted = {_id(r["raw"].get("AIType")) for r in npc_rows if r["raw"].get("AIType")}
    ai_index = defaultdict(list)
    for rel in _available_tables(snapshot, "settings/AIType", "settings/AITypeList.tab") + ["settings/AITypeDefault.tab"]:
        for entry in snapshot.table(rel):
            key = _id(entry["raw"].get("AIType"))
            if key in ai_wanted:
                ai_index[key].append(dict(entry, script=snapshot.script(entry["raw"].get("ScriptFile", "")), evidence_type="npc_AIType_reference"))
    for entry in npc_rows:
        entry["ai_records"] = ai_index.get(_id(entry["raw"].get("AIType")), [])
        if entry["raw"].get("AIType") and not entry["ai_records"]:
            entry["ai_status"] = "missing_in_snapshot"
        else:
            entry["ai_status"] = "resolved" if entry["ai_records"] else "not_configured"
    doodads = []
    wanted_doodad.update(_id(r["raw"].get("CorpseDoodadID")) for r in npc_rows
                         if _id(r["raw"].get("CorpseDoodadID", "")).isdigit()
                         and int(_id(r["raw"].get("CorpseDoodadID"))) > 0)
    ui_doodad = {_id(r["raw"].get("ID")): r for r in snapshot.table("ui/Scheme/Case/DoodadTemplate.tab")} if detail else {}
    for entry in snapshot.table("settings/DoodadTemplate.tab", required=True):
        raw = entry["raw"]; template = _id(raw.get("ID")); mapname = raw.get("MapName", "")
        if mapname in names or mapname in bases or template in wanted_doodad:
            doodads.append(dict(entry, template_id=template, script=snapshot.script(raw.get("Script", "")),
                                ui_record=ui_doodad.get(template),
                                evidence_type="doodad_template_configuration"))
    npc_index = defaultdict(list); doodad_index = defaultdict(list)
    for r in npc_rows: npc_index[r["template_id"]].append(r)
    for r in doodads: doodad_index[r["template_id"]].append(r)
    map_name_index = {r["raw"].get("Name", ""): r["raw"] for r in maps}
    for entry in npc_rows + doodads:
        name = entry["raw"].get("MapName", "")
        associated = [raw for raw in map_name_index.values()
                      if name == raw.get("Name", "") or entry.get("evidence_type") == "doodad_template_configuration"
                      and name == _base_name(raw.get("Name", ""))]
        entry["map_references"] = [{"map_id": _id(raw.get("ID")), "map_name": raw.get("Name", ""),
                                    "max_player_count_raw": raw.get("MaxPlayerCount", ""),
                                    "evidence_type": "template_map_name" if name == raw.get("Name") else "template_base_map_name_not_runtime_instance"}
                                   for raw in associated]
    def add_map_ref(entry: dict, mid: str, evidence: str) -> None:
        raw = map_index.get(mid)
        if raw is None or any(r["map_id"] == mid and r["evidence_type"] == evidence for r in entry["map_references"]):
            return
        entry["map_references"].append({"map_id": mid, "map_name": raw.get("Name", ""),
                                       "max_player_count_raw": raw.get("MaxPlayerCount", ""), "evidence_type": evidence})
    for placement in placements:
        index = npc_index if placement["kind"] == "NPC" else doodad_index
        for entry in index.get(placement["template_id"], []):
            add_map_ref(entry, placement["map_id"], "map_authored_placement_not_runtime_spawn")
    for rule in rules:
        if not rule["active_type"]:
            continue
        raw = rule["raw"]
        template = _id(raw.get("nTargetID", "")) if raw.get("nType") == "3" else _id(raw.get("nAction", "")) if raw.get("nType") == "2" else ""
        for entry in npc_index.get(template, []):
            add_map_ref(entry, rule["file_map_id"], "official_dbm_event_rule")
    for boss in encyclopaedia:
        for ref in boss["npc_references"]:
            template = _id(ref["record"]["raw"].get("NpcID", "")) if ref.get("record") else ""
            for entry in npc_index.get(template, []):
                add_map_ref(entry, boss["map_id"], "official_ui_encyclopaedia_reference")
    for entry in npc_rows:
        corpse = _id(entry["raw"].get("CorpseDoodadID", ""))
        if not corpse or not corpse.isdigit() or int(corpse) == 0:
            continue
        records = doodad_index.get(corpse, [])
        entry["corpse_doodad_reference"] = {
            "template_id_raw": entry["raw"].get("CorpseDoodadID", ""),
            "status": "resolved" if records else "missing_in_snapshot",
            "records": [{"template_id": r["template_id"], "name": r["raw"].get("Name", ""), "source": r["source"]} for r in records],
            "evidence_type": "explicit_CorpseDoodadID_reference_not_runtime_drop"}
        for record in records:
            record["map_references"].extend(dict(ref, evidence_type="explicit_CorpseDoodadID_reference_not_runtime_drop")
                                             for ref in entry["map_references"]
                                             if ref["map_id"] not in {r["map_id"] for r in record["map_references"]})
    for placement in placements:
        index = npc_index if placement["kind"] == "NPC" else doodad_index
        placement["template_records"] = index.get(placement["template_id"], [])
        placement["template_status"] = "resolved" if placement["template_records"] else "missing_in_snapshot"
        if not placement["template_records"]:
            placement["ui_record"] = ui_npc.get(placement["template_id"]) if placement["kind"] == "NPC" else None
    return {"maps": maps, "npc_templates": npc_rows, "doodad_templates": doodads,
            "placements": placements, "encyclopaedia": encyclopaedia,
            "dbm_rules": rules, "dbm_prompts": prompts, "npc_defaults": defaults,
            "logical_maps": logical_maps}


def _skill_ids(data: dict) -> list[str]:
    values = []
    for entry in data["npc_templates"]:
        if not entry["used_for_runtime_skill_discovery"]:
            continue
        for slot in entry["skill_slots"]:
            value = _id(slot["raw"].get(f"SkillID{slot['slot']}", ""))
            if value.isdigit() and int(value) > 0:
                values.append(value)
    for entry in data["dbm_rules"]:
        if entry["active_type"] and entry["raw"].get("nType") == "2":
            value = _id(entry["raw"].get("nTargetID", ""))
            if value.isdigit() and int(value) > 0:
                values.append(value)
    return list(dict.fromkeys(values))


def collect_dungeon_skill_ids(extracted_root: Path, selected_map_ids: list[str]) -> list[str]:
    """Only explicit NPC SkillID slots and DBM nType=2 targets, never guide IDs."""
    snapshot = _Snapshot(extracted_root)
    return _skill_ids(_load(snapshot, selected_map_ids, detail=False))


def collect_resource_references(extracted_root: Path, selected_map_ids: list[str]) -> list[str]:
    """References from complete extracted resources only, for a later V4 pass."""
    snapshot = _Snapshot(extracted_root); data = _load(snapshot, selected_map_ids, detail=False)
    refs = []
    skillids = set(); buffids = set()
    for entry in data["npc_templates"]:
        if not entry["used_for_runtime_skill_discovery"]:
            continue
        refs.append(entry["raw"].get("ScriptName", ""))
        refs.extend(a["raw"].get("ScriptFile", "") for a in entry["ai_records"])
        skillids.update(_id(slot["raw"].get(f"SkillID{slot['slot']}")) for slot in entry["skill_slots"] if slot["raw"].get(f"SkillID{slot['slot']}"))
    refs.extend(e["raw"].get("Script", "") for e in data["doodad_templates"])
    for e in data["dbm_rules"]:
        if e["active_type"] and e["raw"].get("nType") == "2": skillids.add(_id(e["raw"].get("nTargetID")))
        if e["active_type"] and e["raw"].get("nType") == "1": buffids.add(_id(e["raw"].get("nTargetID")))
    for rel, wanted, key, prefix in (("settings/skill/skills.tab", skillids, "SkillID", "scripts/skill/"), ("settings/skill/Buff.tab", buffids, "ID", "scripts/skill/")):
        for entry in snapshot.table(rel):
            raw = entry["raw"]
            if _id(raw.get(key, raw.get("ID"))) in wanted and raw.get("ScriptFile"):
                script = raw["ScriptFile"].replace("\\", "/").lstrip("/")
                refs.append(script if script.lower().startswith("scripts/") else prefix + script)
    return _safe_unique(refs)


def _save_csv(path: Path, rows: list[dict], *, empty_columns: tuple[str, ...] = ("source_path", "source_row")) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row)) or list(empty_columns)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


_STATUS_LABELS = {
    "resolved": "已关联本快照记录", "missing_in_snapshot": "本快照缺失",
    "not_configured": "字段未配置", "unsafe_path": "路径无效，未读取",
    "file_present": "正文已提取", "bytecode_present": "Lua字节码已提取",
}
_REFERENCE_LABELS = {
    "template_map_name": "模板地图名", "template_base_map_name_not_runtime_instance": "基础副本名（未单独验证难度）",
    "explicit_CorpseDoodadID_reference_not_runtime_drop": "NPC尸体交互物品字段（未核验实际掉落）",
    "map_authored_placement_not_runtime_spawn": "地图预置配置（未核验运行时出现）",
    "official_dbm_event_rule": "官方DBM直接引用", "official_ui_encyclopaedia_reference": "官方百科展示模型引用",
}
_SELECTION_LABELS = {
    "template_map_name": "模板地图名与选择的副本相符",
    "map_or_dbm_template_reference": "地图预置或官方DBM直接引用",
    "official_ui_model_reference": "官方百科展示模型引用；不用于发现运行时技能",
}


def _join(values: Iterable[object], separator: str = "；") -> str:
    return separator.join(dict.fromkeys(str(v) for v in values if v is not None and str(v)))


def _source_columns(entry: dict) -> dict:
    source = entry.get("source", {})
    return {"来源文件": source.get("path", ""), "来源行": source.get("row", "")}


def _associated_columns(entry: dict) -> dict:
    refs = entry.get("map_references", [])
    return {"关联副本ID": _join(r.get("map_id", "") for r in refs),
            "关联副本名称": _join(r.get("map_name", "") for r in refs),
            "人数上限原值": _join(r.get("max_player_count_raw", "") for r in refs),
            "模板所属地图": entry.get("raw", {}).get("MapName", "")}


def _template_name(entry: dict) -> str:
    return entry.get("raw", {}).get("Name", "") or (entry.get("ui_record") or {}).get("raw", {}).get("Name", "")


def _readable_tables(data: dict) -> dict[str, list[dict]]:
    """Flat human-facing summaries; the JSON retains every original column."""
    tables: dict[str, list[dict]] = {key: [] for key in
        ("maps", "npc_templates", "doodad_templates", "placements", "encyclopaedia", "dbm_rules", "dbm_prompts", "relations")}
    for entry in data["maps"]:
        raw = entry["raw"]
        tables["maps"].append({"副本ID": raw.get("ID", ""), "副本名称": raw.get("Name", ""),
                              "显示名称": raw.get("DisplayName", ""), "人数上限原值": raw.get("MaxPlayerCount", ""),
                              "人数下限原值": raw.get("MinPlayerCount", ""), "地图类型原值": raw.get("Type", ""),
                              "地图资源路径": raw.get("ResourcePath", ""), "地图脚本": raw.get("ScriptFile", ""),
                              **_source_columns(entry)})
    for entry in data["npc_templates"]:
        raw = entry["raw"]; slots = []
        for slot in entry["skill_slots"]:
            i = slot["slot"]; value = slot["raw"]
            if value.get(f"SkillID{i}", "") not in {"", "0"}:
                fields = [("技能", value.get(f"SkillID{i}", "")), ("等级", value.get(f"SkillLevel{i}", "")),
                          ("间隔", value.get(f"SkillInterval{i}", "")), ("首次间隔", value.get(f"SkillFirstInterval{i}", ""))]
                slots.append(f"槽{i}：" + "，".join(label + number for label, number in fields if number))
        corpse = entry.get("corpse_doodad_reference", {})
        tables["npc_templates"].append({**_associated_columns(entry), "NPC模板ID": entry["template_id"],
            "NPC名称": _template_name(entry), "称号": raw.get("Title", ""), "等级原值": raw.get("Level", ""),
            "最大生命值原值": raw.get("MaxLife", ""), "生命计数字段原值": raw.get("MaxLifeCount", ""),
            "实战血量": "未核验", "模板分类": (entry.get("class_record") or {}).get("raw", {}).get("Class", ""),
            "技能槽原值": "\n".join(slots), "AI类型ID": raw.get("AIType", ""),
            "AI关联状态": _STATUS_LABELS.get(entry.get("ai_status", ""), entry.get("ai_status", "")),
            "AI脚本": _join(a["raw"].get("ScriptFile", "") + "（" + _STATUS_LABELS.get(a["script"]["status"], a["script"]["status"]) + "）" for a in entry["ai_records"]),
            "NPC脚本": raw.get("ScriptName", ""), "NPC脚本状态": _STATUS_LABELS.get(entry["script"]["status"], entry["script"]["status"]),
            "尸体交互物品模板ID": corpse.get("template_id_raw", ""),
            "尸体交互物品名称": _join(r.get("name", "") for r in corpse.get("records", [])),
            "尸体关联状态": _STATUS_LABELS.get(corpse.get("status", ""), ""),
            "读取依据": _SELECTION_LABELS.get(entry["selection_reason"], entry["selection_reason"]), **_source_columns(entry)})
    for entry in data["doodad_templates"]:
        raw = entry["raw"]
        tables["doodad_templates"].append({**_associated_columns(entry), "交互物品模板ID": entry["template_id"],
            "交互物品名称": _template_name(entry), "名称来源": "模板" if raw.get("Name") else "官方UI" if _template_name(entry) else "未提供",
            "类型原值": raw.get("Kind", ""), "分类ID原值": raw.get("ClassID", ""),
            "可选中原值": raw.get("IsSelectable", ""), "可拾取原值": raw.get("CanPick", ""),
            "逐人操作字段原值": raw.get("CanOperateEach", ""), "准备帧原值": raw.get("OpenPrepareFrame", ""),
            "读条文字": raw.get("BarText", "") or (entry.get("ui_record") or {}).get("raw", {}).get("BarText", ""),
            "交互脚本": raw.get("Script", ""), "脚本状态": _STATUS_LABELS.get(entry["script"]["status"], entry["script"]["status"]),
            "具体交互": "未核验", "关联依据": _join(_REFERENCE_LABELS.get(r["evidence_type"], r["evidence_type"]) for r in entry["map_references"]),
            **_source_columns(entry)})
    for entry in data["placements"]:
        records = entry["template_records"]
        tables["placements"].append({"副本ID": entry["map_id"], "副本名称": entry["map_name"],
            "预置类别": "NPC" if entry["kind"] == "NPC" else "交互物品", "模板ID": entry["template_id"],
            "模板名称": _join(_template_name(r) for r in records) or (entry.get("ui_record") or {}).get("raw", {}).get("Name", ""),
            "预置昵称": entry["nickname"], "X原值": entry["nX"], "Y原值": entry["nY"], "Z原值": entry["nZ"],
            "模板关联状态": _STATUS_LABELS.get(entry["template_status"], entry["template_status"]),
            "证据说明": "地图预置配置，未核验运行时出现", "来源记录序号": entry["binary_record_index"], **_source_columns(entry)})
    for entry in data["encyclopaedia"]:
        raw = entry["boss"]["raw"]
        common = {"副本ID": entry["map_id"], "副本名称": entry["map_name"], "人数上限原值": entry["max_player_count_raw"],
                  "百科BOSS名称": raw.get("BOSS", ""), "百科BOSS索引": raw.get("BossIndex", ""),
                  "展示NPC模板ID": _join(ref["record"]["raw"].get("NpcID", "") if ref.get("record") else "未关联索引" + ref["index_raw"] for ref in entry["npc_references"]),
                  "展示NPC名称": _join(ref["record"]["raw"].get("Name", "") for ref in entry["npc_references"] if ref.get("record")),
                  "百科地图索引状态": "已关联" if entry.get("map_info") else "本快照缺失，副本名取自MapList",
                  "百科关系说明": "展示模型可能复用其他难度；文案ID不等于运行时技能ID", **_source_columns(entry["boss"])}
        for guide in entry["guide_documents"] or [{}]:
            guide_raw = (guide.get("record") or {}).get("raw", {})
            tables["encyclopaedia"].append({**common, "阶段": guide.get("stage", ""),
                "技能文案ID": guide.get("guide_document_id", ""), "技能文案名称": guide_raw.get("SkillName", ""),
                "技能文案说明": guide_raw.get("Desc", ""), "文案关联状态": "已关联" if guide.get("record") else "本快照缺失" if guide else "未配置文案",
                "文案来源文件": (guide.get("record") or {}).get("source", {}).get("path", ""),
                "文案来源行": (guide.get("record") or {}).get("source", {}).get("row", "")})
    for entry in data["dbm_rules"]:
        raw = entry["raw"]
        controls = (("保护参数", "nProtectTime"), ("时间键", "nKey"), ("清除键", "nClearKey"), ("清除类型", "nClearType"),
                    ("增加时间键", "nAddTimekey"), ("时间参数", "nTime"), ("暂停计时器", "nStopTimer"),
                    ("时间取整", "bTimeCeil"), ("BOSS标记", "bIsBoss"))
        tables["dbm_rules"].append({"副本ID": entry["file_map_id"], "副本名称": entry["file_map_name"], "人数上限原值": entry["MaxPlayerCount"],
            "规则ID": raw.get("nID", ""), "行内地图ID原值": raw.get("nMapID", ""), "监测对象类型": entry["object_type"],
            "目标ID原值": raw.get("nTargetID", ""), "目标等级原值": raw.get("nTargetLevel", ""), "目标类型原值": raw.get("nTargetType", ""),
            "触发条件": entry["action"], "施法NPC过滤ID原值": entry["caster_npc_template_id_raw"],
            "活动BOSS过滤原值": entry["active_boss_filter_raw"], "百分比阈值原值": entry["percent_threshold_raw"],
            "预告提示（触发后秒数）": _join(p["label"] + " @ " + p["seconds_raw"] + "秒" for p in entry["countdown_pairs"]),
            "控制参数原值": _join(label + "=" + raw[key] for label, key in controls if raw.get(key, "")),
            "规则备注": raw.get("szComment", ""),
            "读取状态": "列数不符，仅保留原文" if not entry.get("schema_valid", True) else "有效监测类型" if entry["active_type"] else "默认或未识别类型；原行保留",
            **_source_columns(entry)})
    for entry in data["dbm_prompts"]:
        tables["dbm_prompts"].append({"副本ID": entry["file_map_id"], "副本名称": entry["file_map_name"], "人数上限原值": entry["MaxPlayerCount"],
            "规则ID": entry["rule_id_raw"], "监测对象类型": entry["object_type"], "目标ID原值": entry["target_id_raw"],
            "触发条件": entry["action"], "预告序号": entry["index"], "提示文字": entry["label"],
            "触发后秒数原值": entry["seconds_raw"], "按官方排序显示": "是" if entry["displayed_by_official_sort"] else "否",
            "时间说明": "从该事件触发时计时", **_source_columns(entry)})
    relation_names = {"npc_skill_slot": "NPC技能槽", "npc_ai_script": "NPC关联AI脚本", "npc_corpse_doodad": "NPC尸体交互物品配置",
                      "doodad_script": "交互物品脚本引用", "dbm_observes": "官方DBM监测"}
    type_names = {"npc_template": "NPC模板", "doodad_template": "交互物品模板", "skill": "技能", "script_path": "脚本", "dbm_rule": "DBM规则"}
    evidence_names = {"explicit_configuration": "模板明确配置", "explicit_AIType_reference": "通过AIType字段明确关联",
                      "explicit_configuration_not_resolved_interaction": "有脚本引用；具体交互未核验", "official_dbm_event_rule": "官方DBM事件规则",
                      "explicit_CorpseDoodadID_reference_not_runtime_drop": "尸体交互物品字段明确配置；未核验运行时掉落"}
    named_templates = {(kind, entry["template_id"]): _template_name(entry)
                       for kind, entries in (("npc_template", data["npc_templates"]), ("doodad_template", data["doodad_templates"]))
                       for entry in entries}
    for entry in data["relations"]:
        tables["relations"].append({"副本ID": entry.get("map_id", ""), "副本名称": entry.get("map_name", ""),
            "关系": relation_names.get(entry["relation"], entry["relation"]), "起点类型": type_names.get(entry["from_type"], entry["from_type"]),
            "起点ID": entry["from_id"], "起点名称": named_templates.get((entry["from_type"], _id(entry["from_id"])), ""),
            "目标类型": type_names.get(entry["to_type"], entry["to_type"]), "目标ID或路径原值": entry["to_id_raw"],
            "目标名称": named_templates.get((entry["to_type"], _id(entry["to_id_raw"])), ""),
            "等级原值": entry.get("level_raw", ""), "关联状态": _STATUS_LABELS.get(entry.get("status", ""), ""),
            "证据说明": evidence_names.get(entry["evidence_type"], entry["evidence_type"]), **_source_columns(entry)})
    return tables


def export_dungeon_resources(extracted_root: Path, selected_map_ids: list[str], output_dir: Path,
                             progress: ProgressCallback | None = None) -> dict:
    snapshot = _Snapshot(extracted_root); output = Path(output_dir)
    if progress: progress(0, 3, "读取同一快照的副本资料")
    data = _load(snapshot, selected_map_ids)
    relations = []
    for entry in data["npc_templates"]:
        for slot in entry["skill_slots"]:
            i = slot["slot"]; skill = slot["raw"].get(f"SkillID{i}", "")
            if skill and skill != "0":
                relations.append({"relation": "npc_skill_slot", "from_type": "npc_template", "from_id": entry["template_id"],
                                  "map_name": entry["raw"].get("MapName", ""),
                                  "to_type": "skill", "to_id_raw": skill, "level_raw": slot["raw"].get(f"SkillLevel{i}", ""),
                                  "source": entry["source"], "evidence_type": "explicit_configuration"})
        for ai in entry["ai_records"]:
            relations.append({"relation": "npc_ai_script", "from_type": "npc_template", "from_id": entry["template_id"],
                              "map_name": entry["raw"].get("MapName", ""),
                              "to_type": "script_path", "to_id_raw": ai["raw"].get("ScriptFile", ""),
                              "status": ai["script"]["status"], "source": ai["source"], "evidence_type": "explicit_AIType_reference"})
        if entry.get("corpse_doodad_reference"):
            corpse = entry["corpse_doodad_reference"]
            relations.append({"relation": "npc_corpse_doodad", "from_type": "npc_template", "from_id": entry["template_id"],
                              "map_name": entry["raw"].get("MapName", ""), "to_type": "doodad_template",
                              "to_id_raw": corpse["template_id_raw"], "status": corpse["status"],
                              "source": entry["source"], "evidence_type": corpse["evidence_type"]})
    for entry in data["doodad_templates"]:
        if entry["raw"].get("Script"):
            relations.append({"relation": "doodad_script", "from_type": "doodad_template", "from_id": entry["template_id"],
                              "map_name": entry["raw"].get("MapName", ""),
                              "to_type": "script_path", "to_id_raw": entry["raw"]["Script"], "status": entry["script"]["status"],
                              "source": entry["source"], "evidence_type": "explicit_configuration_not_resolved_interaction"})
    for entry in data["dbm_rules"]:
        typ = entry["raw"].get("nType")
        if entry["active_type"] and typ in DBM_TYPES:
            relations.append({"relation": "dbm_observes", "from_type": "dbm_rule", "from_id": entry["raw"].get("nID", ""),
                              "map_id": entry["file_map_id"], "to_type": DBM_TYPES[typ], "to_id_raw": entry["raw"].get("nTargetID", ""),
                              "map_name": entry["file_map_name"], "max_player_count_raw": entry["MaxPlayerCount"],
                              "level_raw": entry["raw"].get("nTargetLevel", ""), "source": entry["source"], "evidence_type": "official_dbm_event_rule"})
    data["relations"] = relations
    if progress: progress(1, 3, "整理原始字段和官方引用")
    snapshot_id = snapshot.finish()
    counts = {key: len(value) for key, value in data.items() if isinstance(value, list)}
    counts["active_dbm_rules"] = sum(r["active_type"] for r in data["dbm_rules"])
    counts["missing_placement_templates"] = sum(r["template_status"] == "missing_in_snapshot" for r in data["placements"])
    counts["issues"] = len(snapshot.issues)
    document = {"format": "jx3_dungeon_static_resources_v1", "snapshot_id": snapshot_id,
                "extracted_root": str(snapshot.root), "selected_map_ids": list(selected_map_ids),
                "sources": list(snapshot.sources.values()), "counts": counts, "limitations": LIMITATIONS,
                "issues": snapshot.issues, **data}
    output.mkdir(parents=True, exist_ok=True)
    files = {}; readable = _readable_tables(data)
    for key, name in (("maps", "地图.csv"), ("npc_templates", "NPC.csv"), ("doodad_templates", "交互物品.csv"),
                      ("placements", "地图预置.csv"), ("encyclopaedia", "官方百科.csv"),
                      ("dbm_rules", "官方DBM.csv"), ("dbm_prompts", "DBM提示.csv"), ("relations", "关系证据.csv")):
        path = output / name; rows = readable[key]
        for row in rows: row["快照ID"] = snapshot_id
        _save_csv(path, rows, empty_columns=("副本ID", "副本名称", "来源文件", "来源行", "快照ID")); files[key] = str(path.resolve())
    issue_path = output / "资料问题.csv"
    _save_csv(issue_path, [{"级别": row["level"], "来源文件": row["path"], "来源行": row["row"], "说明": row["message"], "快照ID": snapshot_id}
                          for row in snapshot.issues], empty_columns=("级别", "来源文件", "来源行", "说明", "快照ID"))
    files["issues"] = str(issue_path.resolve())
    json_path = output / "副本资料.json"
    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    files["json"] = str(json_path.resolve())
    if progress: progress(3, 3, "副本资料导出完成")
    return {"counts": counts, "files": files, "snapshot_id": snapshot_id, "issues": snapshot.issues,
            "csv_row_counts": {key: len(rows) for key, rows in readable.items()}}
