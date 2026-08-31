from __future__ import annotations

import unittest

from jx3_analyzer.lua_analysis import (
    analyze_lua,
    detect_lua_kind,
    safe_numeric,
    select_reachable_source,
)


class LuaAnalysisTests(unittest.TestCase):
    def test_detects_source_and_lua51_bytecode(self) -> None:
        self.assertEqual(detect_lua_kind(b"function Apply() end")[0], "source")
        self.assertEqual(detect_lua_kind(b"\x1bLua\x51\x00")[1], "Lua 5.1")

    def test_safe_numeric_does_not_execute_code(self) -> None:
        self.assertEqual(safe_numeric("32 / 2"), 16)
        self.assertEqual(safe_numeric("2 * LENGTH_BASE"), 2)
        self.assertEqual(safe_numeric("GLOBAL.GAME_FPS * 0.5"), 8)
        self.assertIsNone(safe_numeric("__import__('os').system('echo unsafe')"))

    def test_generic_decompiler_names_are_supported(self) -> None:
        source = r'''
function GetSkillLevelData(p1_0)
    p1_0.nPrepareFrames = 32
    p1_0.nAreaRadius = 8 * LENGTH_BASE
    p1_0.nAngleRange = 256
    SkillPuncture(p1_0)
    p1_0.AddAttribute(ATTRIBUTE_EFFECT_MODE.EFFECT_TO_DEST_NOT_ROLLBACK,
        ATTRIBUTE_TYPE.CALL_PHYSICS_DAMAGE, 0, 0)
    p1_0.AddAttribute(ATTRIBUTE_EFFECT_MODE.EFFECT_TO_DEST_NOT_ROLLBACK,
        ATTRIBUTE_TYPE.CALL_KNOCKED_DOWN, 0, 0)
    p1_0.BindBuff(1, 33739, 1)
    return true
end

function Apply(p2_0, p2_1)
    local r0_2 = GetPlayer(p2_0)
    if r0_2 then
        r0_2.AddBuff(r0_2.dwID, r0_2.nLevel, 28127, 2)
    end
end
'''
        facts = analyze_lua(source)
        self.assertEqual(facts.properties["nPrepareFrames"], ["32"])
        self.assertIn("外功伤害", facts.damage_types)
        self.assertIn("击倒", facts.controls)
        self.assertTrue(facts.penetration)
        self.assertEqual(
            [(item.relation, item.buff_id) for item in facts.buff_references],
            [("AOE绑定Buff", 33739), ("添加给玩家", 28127)],
        )

    def test_only_reachable_include_functions_are_analyzed(self) -> None:
        main = "function Apply(a, b) helper_for_100(a, b) end"
        dependency = """
function helper_for_100(a, b)
    target.AddBuff(target.dwID, 1, 12345, 1)
end
function unrelated(a, b)
    target.AddBuff(target.dwID, 1, 99999, 1)
end
"""
        combined, names = select_reachable_source(main, [dependency])
        self.assertEqual(names, ["helper_for_100"])
        self.assertIn("12345", combined)
        self.assertNotIn("99999", combined)
        facts = analyze_lua(main, [dependency])
        self.assertEqual([item.buff_id for item in facts.buff_references], [12345])


if __name__ == "__main__":
    unittest.main()

