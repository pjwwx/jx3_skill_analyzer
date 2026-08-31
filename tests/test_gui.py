from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from jx3_analyzer.gui import AnalyzerWindow, smoke_test_gui  # noqa: E402


class GuiStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_gui_can_initialize_and_populate_automatic_paths(self) -> None:
        window = AnalyzerWindow()
        try:
            window.show_initial()
            self.app.processEvents()
            self.assertTrue(window.isMaximized())
            self.assertEqual(window.mode_tabs.currentIndex(), 0)
            self.assertEqual(window.run_button.text(), "开始解析所选副本")

            window.showNormal()
            window.resize(840, 600)
            window.mode_tabs.setCurrentIndex(1)
            window.root_edit.setText(r"D:\Game\Package")
            window._refresh_automatic_paths(force=True)
            self.app.processEvents()
            self.assertEqual(window.windowTitle(), "剑网3 技能脚本解析器")
            self.assertTrue(window.skills_edit.text().endswith("settings\\skill\\skills.tab"))
            self.assertTrue(window.buff_edit.text().endswith("settings\\skill\\buff.tab"))
            self.assertFalse(window.advanced_panel.isVisible())

            window._toggle_advanced()
            self.app.processEvents()
            self.assertTrue(window.advanced_panel.isVisible())
            self.assertGreaterEqual(window.advanced_panel.height(), window.advanced_panel.minimumSizeHint().height())
            self.assertGreater(window.scroll_area.verticalScrollBar().maximum(), 0)
        finally:
            window.close()

    def test_gui_smoke_can_render_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preview = Path(temporary) / "preview.png"
            smoke_test_gui(preview)
            self.assertTrue(preview.is_file())
            self.assertGreater(preview.stat().st_size, 5_000)


if __name__ == "__main__":
    unittest.main()
