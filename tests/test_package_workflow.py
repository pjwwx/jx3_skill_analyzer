from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jx3_analyzer.package_workflow import (
    build_package_catalog,
    extract_package_files,
    resolve_bin64,
)


class PackageWorkflowTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        game_root = root / "JX3"
        bin64 = game_root / "bin/zhcn_hd/bin64"
        bin64.mkdir(parents=True)
        (bin64 / "Engine_Lua5X64.dll").write_bytes(b"fixture")
        pak = game_root / "PakV4"
        pak.mkdir()
        (pak / "Trunk.dir").write_bytes(b"fixture")

        extracted = root / "cache"
        skills = extracted / "settings/skill/skills.tab"
        skills.parent.mkdir(parents=True)
        skills.write_text(
            "SkillID\tScriptFile\tSkillName\n"
            "100\tnpc/副本BOSS/测试甲/a.lua\t技能甲\n"
            "101\tnpc/副本BOSS/测试甲/b.lua\t技能乙\n"
            "101\tnpc/副本BOSS/测试甲/b.lua\t技能乙重复等级\n"
            "200\tnpc/副本BOSS/测试乙/c.lua\t技能丙\n",
            encoding="utf-8",
        )
        scripts = extracted / "scripts/ScriptList.tab"
        scripts.parent.mkdir(parents=True)
        scripts.write_text(
            "FilePath\n"
            "scripts\\Include\\Skill.lh\n"
            "scripts\\skill\\npc\\副本BOSS\\测试甲\\a.lua\n"
            "scripts\\skill\\npc\\副本BOSS\\测试甲\\b.lua\n"
            "scripts\\skill\\npc\\副本BOSS\\测试甲\\extra.lua\n"
            "scripts\\skill\\npc\\副本BOSS\\测试乙\\c.lua\n",
            encoding="utf-8",
        )
        return bin64, extracted

    def test_resolve_bin64_accepts_bin64_and_game_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bin64, _ = self._fixture(Path(temporary))
            self.assertEqual(resolve_bin64(bin64), bin64.resolve())
            self.assertEqual(resolve_bin64(bin64.parents[2]), bin64.resolve())

    def test_catalog_discovers_dungeons_ids_and_all_folder_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bin64, extracted = self._fixture(Path(temporary))
            catalog = build_package_catalog(bin64, extracted)
            self.assertEqual({item.name for item in catalog.dungeons}, {"测试甲", "测试乙"})
            first = next(item for item in catalog.dungeons if item.name == "测试甲")
            self.assertEqual(first.skill_ids, ("100", "101"))
            self.assertEqual(first.script_count, 3)
            self.assertEqual(catalog.shared_script_paths, ("scripts/Include/Skill.lh",))
            self.assertEqual(catalog.find(["测试甲"]), [first])

    def test_extractor_is_staged_in_bin64_and_removed_after_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin64, _ = self._fixture(root)
            helper = root / "JX3PakBridge.exe"
            helper.write_bytes(b"static helper fixture")
            output = root / "output"
            captured: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                staged = Path(command[0])
                self.assertEqual(staged.parent, bin64.resolve())
                self.assertTrue(staged.is_file())
                self.assertEqual(Path(str(kwargs["cwd"])), bin64.resolve())
                captured["staged"] = staged
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        b"SUMMARY total=1 extracted=1 skipped=0 "
                        b"missing=0 invalid=0 failed=0\n"
                    ),
                )

            with (
                patch("jx3_analyzer.package_workflow.bundled_extractor_path", return_value=helper),
                patch("jx3_analyzer.package_workflow.subprocess.run", side_effect=fake_run),
            ):
                summary = extract_package_files(bin64, ["settings/skill/skills.tab"], output)

            self.assertEqual(summary.extracted, 1)
            self.assertFalse(Path(str(captured["staged"])).exists())
            self.assertEqual(list(output.glob(".jx3_extract_*.txt")), [])


if __name__ == "__main__":
    unittest.main()
