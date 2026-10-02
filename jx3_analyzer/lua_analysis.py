from __future__ import annotations

import ast
import math
import re
import struct
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Iterable, Iterator


LUA_MAGIC = b"\x1bLua"


def isolate_lua51_global_functions(data: bytes, stats: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Copy proven CLOSURE/SETGLOBAL pairs into self-contained strict inputs.

    No instructions, constants or nested prototypes of the copied function are
    rewritten. Captured outer variables are deliberately unsupported: their
    initializers may belong to the part that failed to decompile.
    """
    if len(data) < 12 or data[:6] != b"\x1bLua\x51\x00":
        raise ValueError("只支持标准 Lua 5.1 字节码")
    endian, int_size, string_size, instruction_size, number_size, integral = data[6:12]
    if endian not in (0, 1) or int_size != 4 or string_size not in (4, 8) or instruction_size != 4 or number_size != 8 or integral != 0:
        raise ValueError("Lua 5.1 字节码布局未经过验证")
    order = "little" if endian else "big"
    cursor = 12

    def read(count: int) -> bytes:
        nonlocal cursor
        if count < 0 or count > len(data) - cursor:
            raise ValueError("Lua 字节码截断或计数越界")
        value = data[cursor:cursor + count]
        cursor += count
        return value

    def uint(size: int = 4) -> int:
        return int.from_bytes(read(size), order)

    def string() -> bytes:
        count = uint(string_size)
        value = read(count)
        if value and value[-1] != 0:
            raise ValueError("Lua 字符串缺少终止字节")
        return value[:-1] if value else b""

    def proto(depth: int = 0) -> dict:
        if depth > 100:
            raise ValueError("Lua 原型嵌套过深")
        start = cursor
        string()
        first, last = uint(), uint()
        upvalues, params, vararg, stack = read(4)
        code_bytes = read(uint() * 4)
        code = [int.from_bytes(code_bytes[i:i + 4], order) for i in range(0, len(code_bytes), 4)]
        constants = []
        for _ in range(uint()):
            kind = read(1)[0]
            if kind == 0:
                constants.append(None)
            elif kind == 1:
                constants.append(bool(read(1)[0]))
            elif kind == 3:
                constants.append(struct.unpack("<d" if endian else ">d", read(8))[0])
            elif kind == 4:
                constants.append(string())
            else:
                raise ValueError("未知 Lua 常量类型")
        children_count = uint()
        if children_count > len(data) // 32:
            raise ValueError("Lua 子原型计数异常")
        children = [proto(depth + 1) for _ in range(children_count)]
        read(uint() * 4)  # line positions
        for _ in range(uint()):
            string(); read(8)  # local name and lifetime
        for _ in range(uint()):
            string()  # upvalue debug names
        return dict(start=start, end=cursor, upvalues=upvalues, params=params,
                    vararg=vararg, stack=stack, code=code, constants=constants,
                    children=children, first_line=first, last_line=last)

    root = proto()
    if cursor != len(data):
        raise ValueError("Lua 字节码尾部存在未知数据")
    candidates: dict[str, list[dict]] = {}
    global_writes: dict[str, list[int]] = {}

    def global_name(instruction: int) -> str | None:
        constant = instruction >> 14
        if constant >= len(root["constants"]):
            raise ValueError("Lua SETGLOBAL 常量索引异常")
        raw_name = root["constants"][constant]
        if not isinstance(raw_name, bytes):
            raise ValueError("Lua SETGLOBAL 名称不是字符串")
        return raw_name.decode("ascii") if re.fullmatch(rb"[A-Za-z_]\w*", raw_name) else None

    pc = 0
    while pc < len(root["code"]):
        instruction = root["code"][pc]
        opcode = instruction & 63
        if opcode > 37:
            raise ValueError("未知 Lua 5.1 指令")
        if opcode == 7:  # Count every SETGLOBAL, including non-function writes.
            name = global_name(instruction)
            if name:
                global_writes.setdefault(name, []).append(pc)
        if opcode == 34 and ((instruction >> 14) & 511) == 0:  # SETLIST extra data word
            if pc + 1 >= len(root["code"]) or root["code"][pc + 1] == 0:
                raise ValueError("Lua SETLIST 扩展计数字缺失或无效")
            pc += 2
            continue
        if opcode != 36:  # Lua 5.1 CLOSURE
            pc += 1
            continue
        register = (instruction >> 6) & 255
        index = instruction >> 14
        if index >= len(root["children"]):
            raise ValueError("Lua CLOSURE 子原型索引异常")
        child = root["children"][index]
        following = pc + 1 + child["upvalues"]
        if following > len(root["code"]):
            raise ValueError("Lua CLOSURE 捕获描述截断")
        for binding in root["code"][pc + 1:following]:
            binding_opcode, source_register = binding & 63, (binding >> 23) & 511
            if binding_opcode not in (0, 4) or source_register >= (root["stack"] if binding_opcode == 0 else root["upvalues"]):
                raise ValueError("Lua CLOSURE 捕获描述无效")
        if following < len(root["code"]):
            assignment = root["code"][following]
            if assignment & 63 == 7 and (assignment >> 6) & 255 == register:
                name = global_name(assignment)
                if name:
                    candidates.setdefault(name, []).append(dict(name=name, child=child, child_index=index,
                                                                closure_pc=pc, assignment_pc=following))
        pc = following  # Binding pseudo-instructions cannot name another closure.

    def encoded(value: int, size: int = 4) -> bytes:
        return value.to_bytes(size, order)

    recovered, rejected = [], []
    if stats is not None:
        stats.update(root_child_count=len(root["children"]),
                     mapped_global_function_count=len(candidates),
                     unmapped_root_children=len(root["children"]) - len({c["child_index"] for matches in candidates.values() for c in matches}))
    for name, matches in candidates.items():
        candidate = matches[0]; child = candidate["child"]
        info = {key: candidate[key] for key in ("name", "child_index", "closure_pc", "assignment_pc")}
        info.update(first_line=child["first_line"], last_line=child["last_line"],
                    original_offset=child["start"], original_bytes=child["end"] - child["start"],
                    upvalues=child["upvalues"])
        if len(matches) != 1 or len(global_writes.get(name, [])) != 1 or child["upvalues"]:
            info["reason"] = "重复全局赋值，无法确认唯一函数" if len(matches) != 1 or len(global_writes.get(name, [])) != 1 else "捕获外层变量，初始化未恢复"
            rejected.append(info)
            continue
        # A new three-instruction root only installs the untouched child under
        # its bytecode-proven global name; it never needs external upvalues.
        raw_function = data[child["start"]:child["end"]]
        instructions = [36, 7, 30 | (1 << 23)]  # CLOSURE; SETGLOBAL; RETURN
        wrapper = encoded(0, string_size) + encoded(0) + encoded(0) + bytes((0, 0, 2, 2))
        wrapper += encoded(3) + b"".join(encoded(i) for i in instructions)
        name_bytes = name.encode("ascii") + b"\0"
        wrapper += encoded(1) + bytes((4,)) + encoded(len(name_bytes), string_size) + name_bytes
        wrapper += encoded(1) + raw_function + encoded(0) * 3
        info["chunk"] = data[:12] + wrapper
        recovered.append(info)
    return recovered, rejected


@dataclass(frozen=True)
class LuaCall:
    callee: str
    arguments: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class BuffReference:
    relation: str
    buff_id_expression: str
    buff_id: int | None
    level_expression: str = ""
    receiver: str = ""
    call_name: str = ""


@dataclass
class LuaFacts:
    properties: dict[str, list[str]] = field(default_factory=dict)
    damage_types: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    penetration: bool = False
    puncture: bool = False
    buff_references: list[BuffReference] = field(default_factory=list)
    called_skill_ids: list[int] = field(default_factory=list)
    attribute_types: list[str] = field(default_factory=list)
    damage_base: list[str] = field(default_factory=list)
    damage_rand: list[str] = field(default_factory=list)
    damage_base_raw: list[str] = field(default_factory=list)
    damage_rand_raw: list[str] = field(default_factory=list)
    damage_base_expressions: list[str] = field(default_factory=list)
    damage_rand_expressions: list[str] = field(default_factory=list)
    reachable_helpers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = asdict(self)
        return result


DAMAGE_ATTRIBUTE_MAP = {
    "PHYSICS": "外功伤害",
    "SOLAR": "阳性内功伤害",
    "LUNAR": "阴性内功伤害",
    "NEUTRAL": "混元内功伤害",
    "POISON": "毒性内功伤害",
}

CONTROL_ATTRIBUTE_MAP = {
    "CALL_REPULSED": "击退",
    "CALL_KNOCKED_BACK_EXHALE": "击退",
    "CALL_KNOCKED_OFF_PARABOLA": "击飞",
    "CALL_KNOCKED_DOWN": "击倒",
    "PULL": "拉拽",
    "STOP_XYZ": "定身/停止移动",
}

_CALL_RE = re.compile(
    r"(?<![\w])(?P<callee>[A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)*)\s*\(",
    re.MULTILINE,
)
_FUNCTION_RE = re.compile(
    r"(?m)^\s*(?:local\s+)?function\s+"
    r"(?P<name>[A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)*)\s*\((?P<params>[^)]*)\)"
)
_BLOCK_TOKEN_RE = re.compile(r"\b(function|if|for|while|do|repeat|end|until)\b")
_LONG_STRING_OPEN_RE = re.compile(r"\[(=*)\[")


def detect_lua_kind(data: bytes) -> tuple[str, str]:
    """返回 (类型, 版本说明)。"""
    if data.startswith(LUA_MAGIC):
        version = data[4] if len(data) > 4 else 0
        version_name = {
            0x50: "Lua 5.0",
            0x51: "Lua 5.1",
            0x52: "Lua 5.2",
            0x53: "Lua 5.3",
            0x54: "Lua 5.4",
        }.get(version, f"未知 Lua 版本(0x{version:02X})")
        return "bytecode", version_name
    if b"\x00" in data[:4096]:
        return "binary", "非标准二进制"
    return "source", "Lua 源码"


@lru_cache(maxsize=256)
def strip_lua_comments(text: str) -> str:
    """删除注释但保持字符串、换行和字符位置基本不变。"""
    chars = list(text)
    i = 0
    quote: str | None = None
    while i < len(chars):
        ch = chars[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "-" and i + 1 < len(chars) and chars[i + 1] == "-":
            long_match = re.match(r"--\[(=*)\[", text[i:])
            if long_match:
                equals = long_match.group(1)
                close_marker = "]" + equals + "]"
                close_at = text.find(close_marker, i + long_match.end())
                end = len(chars) if close_at < 0 else close_at + len(close_marker)
            else:
                newline = text.find("\n", i + 2)
                end = len(chars) if newline < 0 else newline
            for j in range(i, end):
                if chars[j] not in "\r\n":
                    chars[j] = " "
            i = end
            continue
        i += 1
    return "".join(chars)


def _matching_parenthesis(text: str, open_pos: int) -> int | None:
    depth = 0
    quote: str | None = None
    long_close: str | None = None
    i = open_pos
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if long_close:
            if text.startswith(long_close, i):
                i += len(long_close)
                long_close = None
            else:
                i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "[":
            m = re.match(r"\[(=*)\[", text[i:])
            if m:
                long_close = "]" + m.group(1) + "]"
                i += m.end()
                continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def split_lua_arguments(arguments: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    levels = {"(": 0, "[": 0, "{": 0}
    matching = {")": "(", "]": "[", "}": "{"}
    quote: str | None = None
    long_close: str | None = None
    i = 0
    while i < len(arguments):
        ch = arguments[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if long_close:
            if arguments.startswith(long_close, i):
                i += len(long_close)
                long_close = None
            else:
                i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "[":
            m = re.match(r"\[(=*)\[", arguments[i:])
            if m:
                long_close = "]" + m.group(1) + "]"
                i += m.end()
                continue
            levels["["] += 1
        elif ch in "({":
            levels[ch] += 1
        elif ch in matching:
            levels[matching[ch]] = max(0, levels[matching[ch]] - 1)
        elif ch == "," and not any(levels.values()):
            parts.append(arguments[start:i].strip())
            start = i + 1
        i += 1
    tail = arguments[start:].strip()
    if tail or parts:
        parts.append(tail)
    return tuple(parts)


def iter_lua_calls(text: str) -> Iterator[LuaCall]:
    cleaned = strip_lua_comments(text)
    code_mask = _mask_lua_strings(cleaned)
    for match in _CALL_RE.finditer(code_mask):
        callee = match.group("callee")
        if callee in {"function", "if", "for", "while", "return"}:
            continue
        # Function headers and quoted examples are not executed calls.
        if re.search(r"\bfunction\s*$", code_mask[max(0, match.start() - 256) : match.start()]):
            continue
        open_pos = cleaned.find("(", match.start("callee") + len(callee))
        close_pos = _matching_parenthesis(cleaned, open_pos)
        if close_pos is None:
            continue
        args = split_lua_arguments(cleaned[open_pos + 1 : close_pos])
        yield LuaCall(callee, args, match.start(), close_pos + 1)


@lru_cache(maxsize=256)
def _mask_lua_strings(text: str) -> str:
    """Mask short/long strings while preserving offsets for call arguments."""
    chars = list(text)
    i = 0
    while i < len(text):
        start = i
        if text[i] in ("'", '"'):
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
        else:
            long_match = _LONG_STRING_OPEN_RE.match(text, i) if text[i] == "[" else None
            if not long_match:
                i += 1
                continue
            close = "]" + long_match.group(1) + "]"
            end = text.find(close, long_match.end())
            i = len(text) if end < 0 else end + len(close)
        for j in range(start, min(i, len(chars))):
            if chars[j] not in "\r\n":
                chars[j] = " "
    return "".join(chars)


@lru_cache(maxsize=256)
def extract_function_blocks(text: str) -> dict[str, list[str]]:
    cleaned = strip_lua_comments(text)
    code_mask = _mask_lua_strings(cleaned)
    result: dict[str, list[str]] = {}
    for match in _FUNCTION_RE.finditer(code_mask):
        depth = 0
        pending_do = 0
        end_pos: int | None = None
        for token_match in _BLOCK_TOKEN_RE.finditer(code_mask, match.start()):
            token = token_match.group(1)
            if token == "function":
                depth += 1
            elif token == "if":
                depth += 1
            elif token in {"for", "while"}:
                depth += 1
                pending_do += 1
            elif token == "repeat":
                depth += 1
            elif token == "do":
                if pending_do:
                    pending_do -= 1
                else:
                    depth += 1
            elif token in {"end", "until"}:
                depth -= 1
                if depth == 0:
                    end_pos = token_match.end()
                    break
        if end_pos:
            result.setdefault(match.group("name"), []).append(cleaned[match.start() : end_pos])
    return result


def select_reachable_source(
    main_text: str, dependency_texts: Iterable[str]
) -> tuple[str, list[str]]:
    """仅追加主脚本确实调用到的依赖函数，避免整个共享 include 污染结果。"""
    definitions: dict[str, list[str]] = {}
    for dependency in dependency_texts:
        for name, blocks in extract_function_blocks(dependency).items():
            definitions.setdefault(name, []).extend(blocks)

    selected_blocks: list[str] = []
    selected_names: list[str] = []
    seen: set[str] = set()
    queue = [call.callee for call in iter_lua_calls(main_text)]
    while queue:
        name = queue.pop(0)
        if name in seen or name not in definitions:
            continue
        seen.add(name)
        selected_names.append(name)
        for block in definitions[name]:
            selected_blocks.append(block)
            queue.extend(call.callee for call in iter_lua_calls(block))
    return "\n\n".join([main_text, *selected_blocks]), selected_names


def _unique(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


_ALLOWED_BINARY_OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_ALLOWED_UNARY_OPERATORS = {
    ast.UAdd: lambda a: a,
    ast.USub: lambda a: -a,
}
_KNOWN_CONSTANTS = {
    "LENGTH_BASE": 1,
    "PERCENT_BASE": 1,
    "GAME_FPS": 16,
}


def safe_numeric(expression: str) -> float | int | None:
    expression = expression.strip().rstrip(";")
    expression = expression.replace("GLOBAL.GAME_FPS", "GAME_FPS")
    expression = re.sub(r"(?<=\d)[fF]\b", "", expression)
    if not expression:
        return None
    try:
        node = ast.parse(expression, mode="eval").body
    except (SyntaxError, ValueError):
        return None

    def evaluate(current: ast.AST) -> float | int:
        if isinstance(current, ast.Constant) and isinstance(current.value, (int, float)):
            return current.value
        if isinstance(current, ast.Name) and current.id in _KNOWN_CONSTANTS:
            return _KNOWN_CONSTANTS[current.id]
        if isinstance(current, ast.BinOp) and type(current.op) in _ALLOWED_BINARY_OPERATORS:
            return _ALLOWED_BINARY_OPERATORS[type(current.op)](
                evaluate(current.left), evaluate(current.right)
            )
        if isinstance(current, ast.UnaryOp) and type(current.op) in _ALLOWED_UNARY_OPERATORS:
            return _ALLOWED_UNARY_OPERATORS[type(current.op)](evaluate(current.operand))
        raise ValueError("unsupported expression")

    try:
        value = evaluate(node)
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    except (ArithmeticError, ValueError, OverflowError):
        return None


def numeric_id(expression: str) -> int | None:
    value = safe_numeric(expression)
    if value is None or int(value) != value or value < 0:
        return None
    return int(value)


def normalize_dimension_expression(expression: str) -> str:
    expression = re.sub(r"\s*\*\s*LENGTH_BASE\b", "", expression)
    expression = re.sub(r"\bLENGTH_BASE\s*\*\s*", "", expression)
    expression = re.sub(r"\s*\*\s*PERCENT_BASE\b", "", expression)
    expression = re.sub(r"\bPERCENT_BASE\s*\*\s*", "", expression)
    return expression.strip()


def _receiver_relation(receiver: str, hints: dict[str, str]) -> str:
    lowered = receiver.lower()
    if receiver in hints:
        return hints[receiver]
    if "player" in lowered:
        return "添加给玩家"
    if "npctarget" in lowered or "targetnpc" in lowered:
        return "添加给NPC目标"
    if lowered.endswith("npct") or "npc_t" in lowered:
        return "添加给小怪/侠客/召唤物"
    if "npc" in lowered:
        return "添加给NPC"
    if "target" in lowered:
        return "添加给目标"
    return "添加给对象"


def _assignment_hints(text: str) -> dict[str, str]:
    hints: dict[str, str] = {}
    patterns = {
        "添加给玩家": r"(?:local\s+)?([A-Za-z_]\w*)\s*=\s*GetPlayer\s*\(",
        "添加给NPC": r"(?:local\s+)?([A-Za-z_]\w*)\s*=\s*GetNpc\s*\(",
        "添加给角色": r"(?:local\s+)?([A-Za-z_]\w*)\s*=\s*GetCharacter\s*\(",
    }
    for relation, pattern in patterns.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            hints[match.group(1)] = relation
    return hints


def _extract_properties(text: str) -> dict[str, list[str]]:
    blocks = extract_function_blocks(text)
    candidates: list[tuple[str, str]] = []
    for name, function_blocks in blocks.items():
        if name.split(".")[-1].split(":")[-1] != "GetSkillLevelData":
            continue
        for block in function_blocks:
            header = _FUNCTION_RE.search(block)
            params = [] if not header else [p.strip() for p in header.group("params").split(",")]
            if params and re.fullmatch(r"[A-Za-z_]\w*", params[0]):
                candidates.append((params[0], block))

    if not candidates:
        candidates = [("skill", text)]

    properties: dict[str, list[str]] = {}
    for variable, block in candidates:
        pattern = re.compile(
            rf"\b{re.escape(variable)}\.(?P<key>[nb][A-Za-z0-9_]*)\s*=\s*"
            r"(?P<value>[^;\r\n]+)"
        )
        for match in pattern.finditer(strip_lua_comments(block)):
            key = match.group("key")
            value = match.group("value").strip()
            properties.setdefault(key, [])
            if value not in properties[key]:
                properties[key].append(value)
    return properties


def analyze_lua(text: str, dependency_texts: Iterable[str] = ()) -> LuaFacts:
    relevant_text, helpers = select_reachable_source(text, dependency_texts)
    cleaned = strip_lua_comments(relevant_text)
    facts = LuaFacts(reachable_helpers=helpers)
    facts.properties = _extract_properties(relevant_text)
    attribute_names = re.findall(r"ATTRIBUTE_TYPE\.([A-Z0-9_]+)", _mask_lua_strings(cleaned))
    facts.attribute_types = _unique(attribute_names)
    for attribute in facts.attribute_types:
        # Defensive, reflection and shield attributes mention damage schools too.
        damage_match = re.fullmatch(
            r"(?:SKILL_(PHYSICS|SOLAR|LUNAR|NEUTRAL|POISON)_DAMAGE(?:_RAND)?|"
            r"CALL_(PHYSICS|SOLAR|LUNAR|NEUTRAL|POISON)_DAMAGE)",
            attribute,
        )
        if damage_match:
            marker = damage_match.group(1) or damage_match.group(2)
            label = DAMAGE_ATTRIBUTE_MAP[marker]
            if label not in facts.damage_types:
                facts.damage_types.append(label)
        if attribute in CONTROL_ATTRIBUTE_MAP:
            label = CONTROL_ATTRIBUTE_MAP[attribute]
            if label not in facts.controls:
                facts.controls.append(label)

    hints = _assignment_hints(cleaned)
    references: list[BuffReference] = []
    called_skills: list[int] = []
    for call in iter_lua_calls(relevant_text):
        call_name = re.split(r"[.:]", call.callee)[-1]
        receiver = re.split(r"[.:]", call.callee)[0] if "." in call.callee or ":" in call.callee else ""
        args = call.arguments

        if call_name == "SkillPenetration":
            facts.penetration = True
        elif call_name == "SkillPuncture":
            facts.puncture = True
        elif call_name == "GetSkillDamageBaseByVersion":
            facts.damage_base_expressions.append(cleaned[call.start : call.end])
        elif call_name == "GetSkillDamageRandByVersion":
            facts.damage_rand_expressions.append(cleaned[call.start : call.end])

        if call_name == "AddAttribute" and len(args) >= 3:
            attr_match = re.search(r"ATTRIBUTE_TYPE\.([A-Z0-9_]+)", args[1])
            if attr_match and attr_match.group(1) == "CALL_BUFF":
                level_expression = args[3] if len(args) > 3 else ""
                references.append(
                    BuffReference(
                        "属性调用Buff",
                        args[2],
                        numeric_id(args[2]),
                        level_expression,
                        receiver,
                        call_name,
                    )
                )
        elif call_name == "BindBuff" and len(args) >= 2:
            references.append(
                BuffReference(
                    "AOE绑定Buff",
                    args[1],
                    numeric_id(args[1]),
                    args[2] if len(args) > 2 else "",
                    receiver,
                    call_name,
                )
            )
        elif call_name == "AddBuff" and len(args) >= 3:
            references.append(
                BuffReference(
                    _receiver_relation(receiver, hints),
                    args[2],
                    numeric_id(args[2]),
                    args[3] if len(args) > 3 else "",
                    receiver,
                    call_name,
                )
            )
        elif call_name in {"DelBuff", "RemoveBuff"} and args:
            references.append(
                BuffReference(
                    "移除Buff",
                    args[0],
                    numeric_id(args[0]),
                    "",
                    receiver,
                    call_name,
                )
            )
        elif call_name in {"CastSkill", "CastSkillLevel", "BossCastSkill"} and args:
            # Embedded object APIs use dot calls with skillID first; colon calls
            # likewise start their explicit arguments with skillID. Global boss
            # wrappers take the character first and skillID second.
            index = 0 if receiver or call_name == "CastSkill" else 1
            if "." in call.callee and args[0].strip() == receiver:
                index = 1  # Explicit self in a conventional Lua dot call.
            if index >= len(args):
                continue
            candidate = args[index]
            skill_id = numeric_id(candidate)
            if skill_id is not None and skill_id not in called_skills:
                called_skills.append(skill_id)

    deduped_references: list[BuffReference] = []
    seen_refs: set[tuple] = set()
    for reference in references:
        key = (
            reference.relation,
            reference.buff_id_expression,
            reference.level_expression,
            reference.receiver,
        )
        if key not in seen_refs:
            seen_refs.add(key)
            deduped_references.append(reference)
    facts.buff_references = deduped_references
    facts.called_skill_ids = called_skills

    # 伤害表按等级排列；重复值也有等级含义，不能像普通属性那样去重。
    facts.damage_base_raw = [
        value.strip()
        for value in re.findall(r"\bnDamageBase\s*=\s*([^,}\r\n]+)", cleaned)
        if value.strip()
    ]
    facts.damage_rand_raw = [
        value.strip()
        for value in re.findall(r"\bnDamageRand\s*=\s*([^,}\r\n]+)", cleaned)
        if value.strip()
    ]
    facts.damage_base_expressions = _unique(facts.damage_base_expressions)
    facts.damage_rand_expressions = _unique(facts.damage_rand_expressions)
    facts.damage_base = facts.damage_base_raw[:] if not facts.damage_base_expressions else []
    facts.damage_rand = facts.damage_rand_raw[:] if not facts.damage_rand_expressions else []
    if facts.damage_base_expressions or facts.damage_rand_expressions:
        facts.warnings.append(
            "伤害由版本函数动态计算；未计算实际数值，原始配置表（包括99999999占位）仅保留在JSON中"
        )
    return facts


def property_values(facts: LuaFacts, *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        values.extend(facts.properties.get(name, []))
    return _unique(values)


def numeric_property_values(facts: LuaFacts, *names: str) -> list[float | int]:
    values: list[float | int] = []
    for expression in property_values(facts, *names):
        value = safe_numeric(normalize_dimension_expression(expression))
        if value is not None and value not in values:
            values.append(value)
    return values
