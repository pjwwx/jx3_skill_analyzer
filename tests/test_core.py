from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jx3_analyzer.core import (
    BUFF_COLUMNS,
    SKILL_COLUMNS,
    AnalyzerPaths,
    LuaLoader,
    create_run_output_dir,
    is_server_side_dependency,
)


class CoreOutputTests(unittest.TestCase):
    def test_run_output_directory_uses_seconds_and_avoids_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            moment = datetime(2026, 8, 30, 12, 34, 56)
            first = create_run_output_dir(temporary, moment)
            second = create_run_output_dir(temporary, moment)
            self.assertEqual(first.name, "技能解析_2026-08-30_12-34-56")
            self.assertEqual(second.name, "技能解析_2026-08-30_12-34-56_02")
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_requested_csv_columns_are_removed(self) -> None:
        for column in (
            "脚本相对路径",
            "脚本绝对路径",
            "脚本格式",
            "反编译状态",
            "依赖脚本",
            "解析备注",
        ):
            self.assertNotIn(column, SKILL_COLUMNS)
        self.assertNotIn("调用对象", BUFF_COLUMNS)

    def test_scripts_map_is_classified_as_server_side(self) -> None:
        self.assertTrue(is_server_side_dependency("scripts/Map/副本/Include/数据.lua"))
        self.assertTrue(is_server_side_dependency(r"scripts\Map\副本\Include\数据.lua"))
        self.assertFalse(is_server_side_dependency("scripts/Include/Skill.lh"))

    def test_server_dependency_is_not_reported_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "scripts/skill/npc/sample.lua"
            script.parent.mkdir(parents=True)
            script.write_text('Include("scripts/Map/sample/server.lua")\nfunction Apply() end', encoding="utf-8")
            paths = AnalyzerPaths(
                root=root,
                skills_tab=root / "skills.tab",
                buff_tab=root / "buff.tab",
                skill_ui=root / "skill.txt",
                buff_ui=root / "buff.txt",
                decompiler=root / "unluac.exe",
            )
            loader = LuaLoader(paths, root / "output")
            main, dependencies = loader.load_with_dependencies(script)
            self.assertEqual(dependencies, [])
            self.assertEqual(main.missing_dependencies, [])
            self.assertEqual(main.server_dependencies, ["scripts/Map/sample/server.lua"])


if __name__ == "__main__":
    unittest.main()
