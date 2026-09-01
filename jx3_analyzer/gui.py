from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QMainWindow,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .core import AnalyzerPaths, RunResult, analyze_ids_file, discover_game_root
from .package_workflow import (
    DungeonInfo,
    PackageCatalog,
    analyze_selected_dungeons,
    load_package_catalog,
    resolve_bin64,
)


APP_STYLE = """
QWidget {
    color: #172033;
    font-family: "Microsoft YaHei UI";
    font-size: 14px;
}
QWidget#appBackground { background: #f3f6fb; }
QScrollArea#pageScroll { background: #f3f6fb; border: none; }
QScrollArea#pageScroll > QWidget > QWidget { background: #f3f6fb; }
QFrame#headerCard {
    background: #172554;
    border: 1px solid #263b7a;
    border-radius: 16px;
}
QLabel#heroTitle { color: white; font-size: 27px; font-weight: 700; }
QLabel#heroSubtitle { color: #c7d2fe; font-size: 13px; }
QLabel#versionBadge {
    color: #dbeafe;
    background: #1e3a8a;
    border: 1px solid #3b82f6;
    border-radius: 13px;
    padding: 6px 11px;
    font-size: 12px;
    font-weight: 600;
}
QFrame#card {
    background: white;
    border: 1px solid #dfe6f2;
    border-radius: 14px;
}
QLabel#sectionNumber {
    color: white;
    background: #2563eb;
    border-radius: 12px;
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
    font-weight: 700;
    qproperty-alignment: AlignCenter;
}
QLabel#sectionTitle { color: #172033; font-size: 17px; font-weight: 700; }
QLabel#sectionHint, QLabel#fieldHint, QLabel#footerText {
    color: #6b7890;
    font-size: 12px;
}
QLabel#fieldLabel { color: #27344d; font-weight: 600; }
QLineEdit {
    color: #172033;
    background: #f8fafc;
    border: 1px solid #ced7e5;
    border-radius: 8px;
    min-height: 24px;
    padding: 9px 11px;
    selection-background-color: #bfdbfe;
}
QLineEdit:hover { border-color: #93a4bd; }
QLineEdit:focus { border: 2px solid #3b82f6; padding: 8px 10px; background: white; }
QPushButton {
    min-height: 38px;
    border-radius: 8px;
    padding: 0 17px;
    font-weight: 600;
}
QPushButton#browseButton, QPushButton#secondaryButton {
    color: #334155;
    background: white;
    border: 1px solid #cbd5e1;
}
QPushButton#browseButton:hover, QPushButton#secondaryButton:hover { background: #f1f5f9; border-color: #94a3b8; }
QPushButton#primaryButton {
    color: white;
    background: #2563eb;
    border: 1px solid #2563eb;
    padding: 0 24px;
}
QPushButton#primaryButton:hover { background: #1d4ed8; border-color: #1d4ed8; }
QPushButton#primaryButton:pressed { background: #1e40af; }
QPushButton:disabled { color: #94a3b8; background: #e8edf4; border-color: #dbe2ec; }
QToolButton#advancedButton {
    color: #34517d;
    background: transparent;
    border: none;
    padding: 6px 2px;
    font-weight: 600;
}
QToolButton#advancedButton:hover { color: #1d4ed8; }
QFrame#advancedPanel { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; }
QTabWidget::pane { border: 1px solid #dce4ef; border-radius: 10px; background: #ffffff; top: -1px; }
QTabBar::tab {
    color: #53627a;
    background: #edf2f8;
    border: 1px solid #dce4ef;
    padding: 10px 22px;
    min-width: 150px;
    font-weight: 600;
}
QTabBar::tab:selected { color: #1d4ed8; background: white; border-bottom-color: white; }
QListWidget {
    color: #26344d;
    background: #f8fafc;
    border: 1px solid #dce4ef;
    border-radius: 9px;
    padding: 5px;
    outline: none;
}
QListWidget::item { min-height: 32px; padding: 3px 8px; border-radius: 6px; }
QListWidget::item:hover { background: #eaf1fb; }
QListWidget::item:selected { color: #173b80; background: #dbeafe; }
QProgressBar {
    min-height: 10px;
    max-height: 10px;
    background: #e6ebf3;
    border: none;
    border-radius: 5px;
    text-align: center;
}
QProgressBar::chunk { background: #2563eb; border-radius: 5px; }
QLabel#statusText { color: #26344d; font-size: 14px; font-weight: 600; }
QLabel#statusMeta { color: #64748b; font-size: 12px; }
QPlainTextEdit {
    color: #31415d;
    background: #f8fafc;
    border: 1px solid #e1e7f0;
    border-radius: 9px;
    padding: 9px;
    font-family: "Microsoft YaHei UI";
    font-size: 12px;
    selection-background-color: #bfdbfe;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px; }
QScrollBar::handle:vertical { background: #c7d1df; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class WorkerSignals(QObject):
    progress = Signal(int, int, str)
    catalog_loaded = Signal(object)
    completed = Signal(object)
    failed = Signal(str)


class AnalyzerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config_path = self._config_path()
        self.config_data = self._load_config()
        self.last_output: Path | None = None
        self.catalog: PackageCatalog | None = None
        self._worker: threading.Thread | None = None
        self.signals = WorkerSignals(self)
        self.signals.progress.connect(self._on_progress)
        self.signals.catalog_loaded.connect(self._on_catalog_loaded)
        self.signals.completed.connect(self._on_done)
        self.signals.failed.connect(self._on_error)

        discovered = discover_game_root()
        self._build_ui()
        guessed_bin64 = ""
        if discovered and (discovered.parent / "Engine_Lua5X64.dll").is_file():
            guessed_bin64 = str(discovered.parent)
        self.bin64_edit.setText(self.config_data.get("bin64") or guessed_bin64)
        self.root_edit.setText(self.config_data.get("root") or str(discovered or ""))
        self.ids_edit.setText(self.config_data.get("ids_file", ""))
        default_output = Path.home() / "Desktop"
        if not default_output.is_dir():
            default_output = Path.home()
        self.output_edit.setText(self.config_data.get("output") or str(default_output))
        self.skills_edit.setText(self.config_data.get("skills_tab", ""))
        self.buff_edit.setText(self.config_data.get("buff_tab", ""))
        self.skill_ui_edit.setText(self.config_data.get("skill_ui", ""))
        self.buff_ui_edit.setText(self.config_data.get("buff_ui", ""))
        self._refresh_automatic_paths(force=False)

    @staticmethod
    def _config_path() -> Path:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "JX3SkillAnalyzer"
        return base / "config.json"

    def _load_config(self) -> dict[str, str]:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _save_config(self) -> None:
        data = {
            "bin64": self.bin64_edit.text().strip(),
            "root": self.root_edit.text().strip(),
            "ids_file": self.ids_edit.text().strip(),
            "output": self.output_edit.text().strip(),
            "skills_tab": self.skills_edit.text().strip(),
            "buff_tab": self.buff_edit.text().strip(),
            "skill_ui": self.skill_ui_edit.text().strip(),
            "buff_ui": self.buff_ui_edit.text().strip(),
        }
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _build_ui(self) -> None:
        self.setWindowTitle("剑网3 技能脚本解析器")
        self.setWindowIcon(_create_app_icon())
        self.setMinimumSize(820, 600)
        self.resize(1120, 800)

        central = QWidget(objectName="appBackground")
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea(objectName="pageScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        central_layout.addWidget(self.scroll_area)

        self.page_widget = QWidget(objectName="appBackground")
        self.page_widget.setMinimumWidth(780)
        self.scroll_area.setWidget(self.page_widget)
        page = QVBoxLayout(self.page_widget)
        page.setContentsMargins(28, 24, 28, 20)
        page.setSpacing(16)

        header = QFrame(objectName="headerCard")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 20, 24, 20)
        hero = QVBoxLayout()
        hero.setSpacing(3)
        title = QLabel("剑网3 技能脚本解析器", objectName="heroTitle")
        subtitle = QLabel("从本地游戏包提取技能、Buff 与界面描述，源码和字节码可混合处理。", objectName="heroSubtitle")
        hero.addWidget(title)
        hero.addWidget(subtitle)
        header_layout.addLayout(hero, 1)
        badge = QLabel(f"本地离线  ·  v{__version__}", objectName="versionBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        page.addWidget(header)

        input_card = QFrame(objectName="card")
        input_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(22, 18, 22, 18)
        input_layout.setSpacing(13)
        input_layout.addLayout(self._section_header("1", "选择分析方式", "推荐直接从游戏包自动读取。"))

        self.mode_tabs = QTabWidget()
        self.mode_tabs.setDocumentMode(True)
        self.mode_tabs.currentChanged.connect(self._on_mode_changed)

        auto_tab = QWidget()
        auto_layout = QVBoxLayout(auto_tab)
        auto_layout.setContentsMargins(18, 17, 18, 16)
        auto_layout.setSpacing(11)
        self.bin64_edit = QLineEdit()
        self.bin64_edit.setPlaceholderText(r"例如 D:\Game\SeasunGame\Game\JX3\bin\zhcn_hd\bin64")
        auto_layout.addLayout(
            self._path_row(
                "剑网3 bin64 目录",
                "只需选择 JX3ClientX64.exe 所在的文件夹。",
                self.bin64_edit,
                self._choose_bin64,
            )
        )

        catalog_actions = QHBoxLayout()
        self.load_button = QPushButton("读取 / 刷新副本列表", objectName="secondaryButton")
        self.load_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_button.clicked.connect(self._load_dungeons)
        catalog_actions.addWidget(self.load_button)
        self.catalog_meta = QLabel("尚未读取游戏包", objectName="fieldHint")
        catalog_actions.addWidget(self.catalog_meta)
        catalog_actions.addStretch(1)
        auto_layout.addLayout(catalog_actions)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("选择副本", objectName="fieldLabel"))
        search_row.addSpacing(8)
        self.dungeon_search = QLineEdit()
        self.dungeon_search.setPlaceholderText("输入副本名筛选")
        self.dungeon_search.setClearButtonEnabled(True)
        self.dungeon_search.textChanged.connect(self._filter_dungeons)
        search_row.addWidget(self.dungeon_search, 1)
        self.selected_meta = QLabel("已选择 0 个", objectName="fieldHint")
        search_row.addWidget(self.selected_meta)
        auto_layout.addLayout(search_row)

        self.dungeon_list = QListWidget()
        self.dungeon_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.dungeon_list.setUniformItemSizes(True)
        self.dungeon_list.setMinimumHeight(220)
        self.dungeon_list.itemSelectionChanged.connect(self._update_selected_count)
        auto_layout.addWidget(self.dungeon_list)

        selection_actions = QHBoxLayout()
        select_visible = QPushButton("选择当前筛选结果", objectName="browseButton")
        select_visible.clicked.connect(self._select_visible_dungeons)
        clear_selection = QPushButton("清空选择", objectName="browseButton")
        clear_selection.clicked.connect(self.dungeon_list.clearSelection)
        selection_actions.addWidget(select_visible)
        selection_actions.addWidget(clear_selection)
        selection_actions.addStretch(1)
        auto_layout.addLayout(selection_actions)
        self.mode_tabs.addTab(auto_tab, "自动读取游戏包")

        self.manual_tab = QWidget()
        manual_layout = QVBoxLayout(self.manual_tab)
        manual_layout.setContentsMargins(18, 17, 18, 16)
        manual_layout.setSpacing(11)
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("包含 settings、scripts 和 ui 文件夹的解包根目录")
        self.root_edit.editingFinished.connect(lambda: self._refresh_automatic_paths(force=True))
        manual_layout.addLayout(
            self._path_row("已解包根目录", "兼容旧流程，程序会自动定位各数据表。", self.root_edit, self._choose_root)
        )
        self.ids_edit = QLineEdit()
        self.ids_edit.setPlaceholderText("选择包含技能 ID 的 txt 文件")
        manual_layout.addLayout(
            self._path_row("技能 ID 文件", "支持每行一个 ID，也支持空格或逗号分隔。", self.ids_edit, self._choose_ids)
        )

        self.advanced_button = QToolButton(objectName="advancedButton")
        self.advanced_button.setText("›  高级路径（通常无需修改）")
        self.advanced_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_button.clicked.connect(self._toggle_advanced)
        manual_layout.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.advanced_panel = QFrame(objectName="advancedPanel")
        advanced_layout = QGridLayout(self.advanced_panel)
        advanced_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        advanced_layout.setContentsMargins(14, 13, 14, 13)
        advanced_layout.setHorizontalSpacing(10)
        advanced_layout.setVerticalSpacing(9)
        self.skills_edit = QLineEdit()
        self.buff_edit = QLineEdit()
        self.skill_ui_edit = QLineEdit()
        self.buff_ui_edit = QLineEdit()
        for row, (label, editor) in enumerate(
            (
                ("skills.tab", self.skills_edit),
                ("buff.tab", self.buff_edit),
                ("UI skill.txt", self.skill_ui_edit),
                ("UI buff.txt", self.buff_ui_edit),
            )
        ):
            advanced_layout.addWidget(QLabel(label, objectName="fieldLabel"), row, 0)
            advanced_layout.addWidget(editor, row, 1)
            button = QPushButton("选择", objectName="browseButton")
            button.setFixedWidth(76)
            button.clicked.connect(lambda _checked=False, target=editor: self._choose_table(target))
            advanced_layout.addWidget(button, row, 2)
        advanced_layout.setColumnStretch(1, 1)
        self.advanced_panel.setMinimumHeight(self.advanced_panel.sizeHint().height())
        self.advanced_panel.hide()
        manual_layout.addWidget(self.advanced_panel)
        manual_layout.addStretch(1)
        self.mode_tabs.addTab(self.manual_tab, "使用已解包文件（兼容）")
        input_layout.addWidget(self.mode_tabs)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择结果存放位置，例如桌面")
        input_layout.addLayout(
            self._path_row(
                "结果存放位置",
                "每次运行会在这里新建一个按时间命名的结果文件夹。",
                self.output_edit,
                self._choose_output,
            )
        )
        page.addWidget(input_card)

        run_card = QFrame(objectName="card")
        run_card.setMinimumHeight(330)
        run_layout = QVBoxLayout(run_card)
        run_layout.setContentsMargins(22, 18, 22, 18)
        run_layout.setSpacing(12)
        run_layout.addLayout(self._section_header("2", "运行解析", "进度和关键结果会显示在这里。"))

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        run_layout.addWidget(self.progress)

        self.status_label = QLabel("准备就绪，请先读取副本列表。", objectName="statusText")
        self.status_label.setWordWrap(True)
        self.status_meta = QLabel("处理完全在本机进行，不会上传游戏文件。", objectName="statusMeta")
        run_layout.addWidget(self.status_label)
        run_layout.addWidget(self.status_meta)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.run_button = QPushButton("开始解析所选副本", objectName="primaryButton")
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.clicked.connect(self._start)
        self.open_button = QPushButton("打开结果目录", objectName="secondaryButton")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_output)
        actions.addWidget(self.run_button)
        actions.addWidget(self.open_button)
        actions.addStretch(1)
        run_layout.addLayout(actions)

        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("处理记录", objectName="fieldLabel"))
        log_header.addStretch(1)
        log_header.addWidget(QLabel("仅显示本次运行的关键步骤", objectName="fieldHint"))
        run_layout.addLayout(log_header)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setMinimumHeight(120)
        self.log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        run_layout.addWidget(self.log, 1)
        page.addWidget(run_card, 1)

        footer = QLabel(
            "说明：scripts/Map 下未随客户端下发的服务器依赖属于正常情况，不计为错误。",
            objectName="footerText",
        )
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page.addWidget(footer)

    @staticmethod
    def _section_header(number: str, title: str, hint: str) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(9)
        layout.addWidget(QLabel(number, objectName="sectionNumber"))
        layout.addWidget(QLabel(title, objectName="sectionTitle"))
        layout.addSpacing(4)
        layout.addWidget(QLabel(hint, objectName="sectionHint"))
        layout.addStretch(1)
        return layout

    @staticmethod
    def _path_row(label: str, hint: str, editor: QLineEdit, callback) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(5)
        label_row = QHBoxLayout()
        label_row.addWidget(QLabel(label, objectName="fieldLabel"))
        label_row.addSpacing(8)
        label_row.addWidget(QLabel(hint, objectName="fieldHint"))
        label_row.addStretch(1)
        field_row = QHBoxLayout()
        field_row.setSpacing(10)
        field_row.addWidget(editor, 1)
        button = QPushButton("浏览…", objectName="browseButton")
        button.setFixedWidth(90)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        field_row.addWidget(button)
        layout.addLayout(label_row)
        layout.addLayout(field_row)
        return layout

    @Slot(int)
    def _on_mode_changed(self, index: int) -> None:
        if not hasattr(self, "run_button"):
            return
        if index == 0:
            self.run_button.setText("开始解析所选副本")
            if self.catalog is None:
                self.status_label.setText("准备就绪，请先读取副本列表。")
        else:
            self.run_button.setText("开始解析技能 ID")
            self.status_label.setText("兼容模式：请选择解包根目录与技能 ID 文件。")

    def _choose_bin64(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择剑网3 bin64 目录", self.bin64_edit.text())
        if path:
            self.bin64_edit.setText(path)
            self.catalog = None
            self.dungeon_list.clear()
            self.catalog_meta.setText("目录已更改，请重新读取副本列表")

    @Slot()
    def _load_dungeons(self) -> None:
        bin64 = self.bin64_edit.text().strip()
        if not bin64:
            QMessageBox.warning(self, "信息不完整", "请先选择剑网3 bin64 目录。")
            return
        self._save_config()
        self.load_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.progress.setRange(0, 3)
        self.progress.setValue(0)
        self.status_label.setText("正在读取游戏包…")
        self.status_meta.setText("首次读取通常只需几秒")
        self.log.clear()
        self._append_log("开始读取技能表、UI 描述与脚本清单")

        def worker() -> None:
            try:
                def report(current: int, total: int, message: str) -> None:
                    self.signals.progress.emit(current, total, message)

                catalog = load_package_catalog(bin64, progress=report)
                self.signals.catalog_loaded.emit(catalog)
            except Exception as exc:
                self.signals.failed.emit(str(exc))

        self._worker = threading.Thread(target=worker, daemon=True, name="jx3-catalog-worker")
        self._worker.start()

    @Slot(object)
    def _on_catalog_loaded(self, catalog: PackageCatalog) -> None:
        self.catalog = catalog
        self.bin64_edit.setText(str(catalog.bin64))
        self.dungeon_list.clear()
        for dungeon in catalog.dungeons:
            item = QListWidgetItem(
                f"{dungeon.name}    {dungeon.skill_count} 个技能  ·  {dungeon.script_count} 个脚本"
            )
            item.setData(Qt.ItemDataRole.UserRole, dungeon.name)
            item.setToolTip(f"{dungeon.name}\n技能 {dungeon.skill_count} 个，相关脚本 {dungeon.script_count} 个")
            self.dungeon_list.addItem(item)
        self._filter_dungeons(self.dungeon_search.text())
        self.load_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.progress.setValue(self.progress.maximum())
        self.catalog_meta.setText(f"已读取 {len(catalog.dungeons)} 个副本")
        self.status_label.setText("副本列表已就绪，请选择一个或多个副本。")
        self.status_meta.setText("可输入名称筛选；按住 Ctrl 可逐项多选")
        self._append_log(f"副本列表读取完成：{len(catalog.dungeons)} 个")
        self._save_config()

    @Slot(str)
    def _filter_dungeons(self, text: str) -> None:
        keyword = text.strip().casefold()
        for index in range(self.dungeon_list.count()):
            item = self.dungeon_list.item(index)
            name = str(item.data(Qt.ItemDataRole.UserRole) or "")
            item.setHidden(bool(keyword and keyword not in name.casefold()))

    def _select_visible_dungeons(self) -> None:
        for index in range(self.dungeon_list.count()):
            item = self.dungeon_list.item(index)
            if not item.isHidden():
                item.setSelected(True)
        self._update_selected_count()

    def _update_selected_count(self) -> None:
        self.selected_meta.setText(f"已选择 {len(self.dungeon_list.selectedItems())} 个")

    def _toggle_advanced(self) -> None:
        visible = not self.advanced_panel.isVisible()
        self.advanced_panel.setVisible(visible)
        self.advanced_button.setText(("⌄" if visible else "›") + "  高级路径（通常无需修改）")
        self.advanced_panel.updateGeometry()
        self.page_widget.updateGeometry()
        if visible:
            QTimer.singleShot(0, lambda: self.scroll_area.ensureWidgetVisible(self.advanced_panel, 0, 20))

    def _choose_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择剑网3本地包解包根目录", self.root_edit.text())
        if path:
            self.root_edit.setText(path)
            self._refresh_automatic_paths(force=True)
            if not self.output_edit.text().strip():
                self.output_edit.setText(str(Path(path) / "技能解析结果"))

    def _choose_ids(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择技能 ID 文件", self.ids_edit.text(), "文本文件 (*.txt);;所有文件 (*.*)")
        if path:
            self.ids_edit.setText(path)
            if not self.output_edit.text().strip() and self.root_edit.text().strip():
                self.output_edit.setText(str(Path(self.root_edit.text().strip()) / "技能解析结果"))

    def _choose_output(self) -> None:
        initial = (
            self.output_edit.text().strip()
            or self.bin64_edit.text().strip()
            or self.root_edit.text().strip()
        )
        path = QFileDialog.getExistingDirectory(self, "选择结果存放位置", initial)
        if path:
            self.output_edit.setText(path)

    def _choose_table(self, editor: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择数据文件", editor.text(), "数据文件 (*.tab *.txt);;所有文件 (*.*)")
        if path:
            editor.setText(path)

    def _refresh_automatic_paths(self, force: bool) -> None:
        root_text = self.root_edit.text().strip()
        if not root_text:
            return
        root = Path(root_text)
        defaults = (
            (self.skills_edit, root / "settings/skill/skills.tab"),
            (self.buff_edit, root / "settings/skill/buff.tab"),
            (self.skill_ui_edit, root / "ui/Scheme/Case/skill.txt"),
            (self.buff_ui_edit, root / "ui/Scheme/Case/buff.txt"),
        )
        for editor, value in defaults:
            if force or not editor.text().strip():
                editor.setText(str(value))

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip())
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    @Slot()
    def _start(self) -> None:
        if self.mode_tabs.currentIndex() == 0:
            self._start_automatic()
        else:
            self._start_manual()

    def _begin_run(self, status: str, meta: str) -> None:
        self.run_button.setEnabled(False)
        self.load_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label.setText(status)
        self.status_meta.setText(meta)
        self.log.clear()

    def _start_automatic(self) -> None:
        output = self.output_edit.text().strip()
        if self.catalog is None:
            QMessageBox.warning(self, "尚未读取副本", "请先点击“读取 / 刷新副本列表”。")
            return
        selected_names = [
            str(item.data(Qt.ItemDataRole.UserRole)) for item in self.dungeon_list.selectedItems()
        ]
        if not selected_names or not output:
            QMessageBox.warning(self, "信息不完整", "请选择至少一个副本，并确认结果存放位置。")
            return
        try:
            current_bin64 = resolve_bin64(self.bin64_edit.text().strip())
        except Exception as exc:
            QMessageBox.warning(self, "游戏目录不可用", str(exc))
            return
        if current_bin64 != self.catalog.bin64:
            QMessageBox.warning(self, "需要刷新", "游戏目录已经改变，请重新读取副本列表。")
            return

        self._save_config()
        self._begin_run("正在准备所选副本…", f"共选择 {len(selected_names)} 个副本")
        self._append_log("开始自动解包并解析：" + "、".join(selected_names))

        def worker() -> None:
            try:
                def report(current: int, total: int, message: str) -> None:
                    self.signals.progress.emit(current, total, message)

                result = analyze_selected_dungeons(
                    self.catalog,
                    selected_names,
                    output,
                    progress=report,
                )
                self.signals.completed.emit(result)
            except Exception as exc:
                self.signals.failed.emit(str(exc))

        self._worker = threading.Thread(target=worker, daemon=True, name="jx3-package-analyzer-worker")
        self._worker.start()

    def _start_manual(self) -> None:
        self._refresh_automatic_paths(force=False)
        root = self.root_edit.text().strip()
        ids_file = self.ids_edit.text().strip()
        output = self.output_edit.text().strip()
        if not root or not ids_file or not output:
            QMessageBox.warning(self, "信息不完整", "请选择本地包根目录、技能 ID 文件和结果存放位置。")
            return

        advanced = {
            "skills_tab": self.skills_edit.text().strip(),
            "buff_tab": self.buff_edit.text().strip(),
            "skill_ui": self.skill_ui_edit.text().strip(),
            "buff_ui": self.buff_ui_edit.text().strip(),
        }
        self._save_config()
        self._begin_run("正在准备数据…", "读取技能表与本地脚本")
        self._append_log("开始解析")

        def worker() -> None:
            try:
                paths = AnalyzerPaths.from_root(root, **advanced)

                def report(current: int, total: int, message: str) -> None:
                    self.signals.progress.emit(current, total, message)

                result = analyze_ids_file(paths, ids_file, output, report)
                self.signals.completed.emit(result)
            except Exception as exc:
                self.signals.failed.emit(str(exc))

        self._worker = threading.Thread(target=worker, daemon=True, name="jx3-analyzer-worker")
        self._worker.start()

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
        self.status_label.setText(message)
        self.status_meta.setText(f"步骤 {current} / {total}")
        self._append_log(message)

    @Slot(object)
    def _on_done(self, result: RunResult) -> None:
        self.last_output = result.output_dir
        self.run_button.setEnabled(True)
        self.load_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self.progress.setValue(self.progress.maximum())
        summary = f"解析完成：成功 {result.successful_skills} 个，失败 {result.failed_skills} 个"
        self.status_label.setText(summary)
        self.status_meta.setText(f"关联 Buff {result.buff_links} 行  ·  已保存到 {result.output_dir.name}")
        self._append_log(summary)
        QMessageBox.information(self, "解析完成", summary + f"\n\n结果目录：\n{result.output_dir}")

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.load_button.setEnabled(True)
        self.status_label.setText("解析失败")
        self.status_meta.setText("请根据下面的错误信息检查输入路径")
        self._append_log("错误：" + message)
        QMessageBox.critical(self, "解析失败", message)

    def _open_output(self) -> None:
        if self.last_output and self.last_output.exists():
            os.startfile(self.last_output)  # type: ignore[attr-defined]

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._save_config()
        super().closeEvent(event)

    def show_initial(self) -> None:
        """用户正常启动时默认最大化；还原窗口后仍可通过滚动区域完整使用。"""
        self.showMaximized()


def _create_app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2563eb"))
    painter.drawRoundedRect(4, 4, 56, 56, 15, 15)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Microsoft YaHei UI", 25, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "剑")
    painter.end()
    return QIcon(pixmap)


def _application() -> tuple[QApplication, bool]:
    existing = QApplication.instance()
    if existing is not None:
        return existing, False
    app = QApplication(sys.argv[:1])
    app.setApplicationName("剑网3 技能脚本解析器")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)
    return app, True


def launch_gui() -> None:
    app, _created = _application()
    window = AnalyzerWindow()
    window.show_initial()
    app.exec()


# 保留旧名称，避免外部脚本导入 AnalyzerApp 时失效。
AnalyzerApp = AnalyzerWindow
