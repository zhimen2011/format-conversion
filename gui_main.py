#!/usr/bin/env python3
"""STAS Tool — GUI for .rwy → .stx conversion and intersection data injection.

100% local — no cloud API dependencies.  Built with PySide6 (Qt).
"""

from __future__ import annotations

import os
import sys
import threading
from functools import partial
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from parser import parse_single_airport, parse_rwy_file
from formatter import format_airport_to_file
from updater import extract_intersections_from_chart, update_stx_file
from converter import normalize_airport

# ---------------------------------------------------------------------------
# thread-safe logging
# ---------------------------------------------------------------------------

class LogEmitter(QObject):
    append = Signal(str)


class LogHandler:
    """File-like object that emits Qt signals for thread-safe log updates."""

    def __init__(self, emitter: LogEmitter):
        self._emitter = emitter

    def write(self, text: str) -> None:
        if text.strip():
            self._emitter.append.emit(text)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# worker signals — thread-safe completion notification
# ---------------------------------------------------------------------------

class ExtractionSignals(QObject):
    done = Signal(list)
    error = Signal(str)


class WorkerSignals(QObject):
    done = Signal()
    error = Signal(str)


def _run_extraction(chart_path: str, signals: ExtractionSignals) -> None:
    try:
        data = extract_intersections_from_chart(chart_path)
        signals.done.emit(data)
    except Exception as exc:
        signals.error.emit(str(exc))


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("STAS 工具 — 机场数据转换与交叉口注入")
        self.resize(880, 800)
        self.setMinimumSize(700, 600)

        # state
        self._stx_path: str = ""
        self._full_tora_map: dict[str, float] = {}
        self._extracting = False

        self._setup_ui()
        self._setup_logging()

    # ==================================================================
    # UI construction
    # ==================================================================

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(8)

        # ---- scrollable main area ----
        from PySide6.QtWidgets import QScrollArea

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_content = QWidget()
        scroll.setWidget(scroll_content)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)

        # ===== Area A: basic conversion =====
        area_a = QGroupBox("区域 A — 基础转换 (.rwy → .stx)")
        a_layout = QVBoxLayout(area_a)
        a_layout.setSpacing(6)

        # .rwy file(s)
        rwy_row = QHBoxLayout()
        rwy_row.addWidget(QLabel("输入 .rwy："))
        self._rwy_edit = QLineEdit()
        self._rwy_edit.setPlaceholderText("选择一个或多个 .rwy 文件…")
        rwy_row.addWidget(self._rwy_edit, 1)
        btn_rwy = QPushButton("浏览…")
        btn_rwy.clicked.connect(self._browse_rwy)
        rwy_row.addWidget(btn_rwy)
        a_layout.addLayout(rwy_row)

        # output dir
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("输出目录："))
        self._out_edit = QLineEdit()
        self._out_edit.setPlaceholderText("选择输出目录…")
        out_row.addWidget(self._out_edit, 1)
        btn_out = QPushButton("浏览…")
        btn_out.clicked.connect(self._browse_outdir)
        out_row.addWidget(btn_out)
        a_layout.addLayout(out_row)

        # convert button
        self._btn_convert = QPushButton("一键转换 .rwy → .stx")
        self._btn_convert.clicked.connect(self._do_basic_conversion)
        a_layout.addWidget(self._btn_convert)

        scroll_layout.addWidget(area_a)

        # ===== Area B: intersection extraction & review =====
        area_b = QGroupBox("区域 B — 交叉口识别与人工校核")
        b_layout = QVBoxLayout(area_b)
        b_layout.setSpacing(6)

        # .stx file
        stx_row = QHBoxLayout()
        stx_row.addWidget(QLabel("现有 .stx："))
        self._stx_edit = QLineEdit()
        self._stx_edit.setPlaceholderText("选择已有的 .stx 文件…")
        stx_row.addWidget(self._stx_edit, 1)
        btn_stx = QPushButton("浏览…")
        btn_stx.clicked.connect(self._browse_stx)
        stx_row.addWidget(btn_stx)
        b_layout.addLayout(stx_row)

        # chart file
        chart_row = QHBoxLayout()
        chart_row.addWidget(QLabel("图表文件："))
        self._chart_edit = QLineEdit()
        self._chart_edit.setPlaceholderText("选择机场图表文件 (PDF/PNG/JPEG)…")
        chart_row.addWidget(self._chart_edit, 1)
        btn_chart = QPushButton("浏览…")
        btn_chart.clicked.connect(self._browse_chart)
        chart_row.addWidget(btn_chart)
        b_layout.addLayout(chart_row)

        # extract button + progress
        ext_row = QHBoxLayout()
        self._btn_extract = QPushButton("开始识别")
        self._btn_extract.clicked.connect(self._do_extraction)
        ext_row.addWidget(self._btn_extract)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)          # indeterminate
        self._progress.setVisible(False)
        self._progress.setMaximumWidth(250)
        ext_row.addWidget(self._progress)
        ext_row.addStretch()
        b_layout.addLayout(ext_row)

        # --- review table ---
        b_layout.addWidget(QLabel("数据校核网格（可直接编辑 TORA 数值）："))

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["跑道号", "交叉口名称", "提取 TORA (m)", "偏移量 Offset (m)"]
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._table.setMinimumHeight(160)
        self._table.itemChanged.connect(self._on_cell_changed)
        b_layout.addWidget(self._table, 1)

        # --- add / delete row buttons ---
        edit_row = QHBoxLayout()
        self._btn_add_row = QPushButton("＋ 添加行")
        self._btn_add_row.clicked.connect(self._add_row)
        edit_row.addWidget(self._btn_add_row)
        self._btn_del_row = QPushButton("－ 删除选中行")
        self._btn_del_row.clicked.connect(self._delete_selected_rows)
        self._btn_del_row.setEnabled(False)
        edit_row.addWidget(self._btn_del_row)
        edit_row.addStretch()
        b_layout.addLayout(edit_row)

        # write button
        self._btn_write = QPushButton("确认无误，写入 STX")
        self._btn_write.setEnabled(False)
        self._btn_write.setStyleSheet(
            "QPushButton:enabled { background-color: #2E7D32; color: white; }"
        )
        self._btn_write.clicked.connect(self._do_write_injection)
        b_layout.addWidget(self._btn_write)

        scroll_layout.addWidget(area_b)
        scroll_layout.addStretch()

        root.addWidget(scroll, 1)

        # ===== bottom: log panel =====
        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(160)
        self._log.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self._log)
        root.addWidget(log_group)

    # ==================================================================
    # logging
    # ==================================================================

    def _setup_logging(self) -> None:
        self._log_emitter = LogEmitter()
        self._log_emitter.append.connect(self._log.append)
        sys.stdout = LogHandler(self._log_emitter)

    # ==================================================================
    # file dialogs
    # ==================================================================

    def _browse_rwy(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 .rwy 文件", "",
            "RWY 文件 (*.rwy);;所有文件 (*.*)",
        )
        if files:
            self._rwy_edit.setText("; ".join(files))

    def _browse_outdir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self._out_edit.setText(d)

    def _browse_stx(self) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "选择已有 .stx 文件", "",
            "STX 文件 (*.stx);;所有文件 (*.*)",
        )
        if f:
            self._stx_edit.setText(f)
            self._stx_path = f
            self._load_full_tora_map()

    def _browse_chart(self) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "选择图表文件", "",
            "图表文件 (*.pdf *.png *.jpg *.jpeg *.bmp);;所有文件 (*.*)",
        )
        if f:
            self._chart_edit.setText(f)

    # ==================================================================
    # Area A — basic conversion
    # ==================================================================

    def _do_basic_conversion(self) -> None:
        rwy_files = [f.strip() for f in self._rwy_edit.text().split(";") if f.strip()]
        out_dir = self._out_edit.text().strip()

        if not rwy_files:
            QMessageBox.warning(self, "缺少输入文件", "请至少选择一个 .rwy 文件。")
            return
        if not out_dir:
            QMessageBox.warning(self, "缺少输出目录", "请选择输出目录。")
            return
        if not os.path.isdir(out_dir):
            QMessageBox.critical(self, "目录无效",
                                 f"输出目录不存在：\n{out_dir}")
            return

        self._btn_convert.setEnabled(False)
        self._btn_convert.setText("正在转换…")

        signals = WorkerSignals()
        signals.done.connect(self._on_conversion_done)

        def _run():
            ok = 0
            for path in rwy_files:
                if not os.path.isfile(path):
                    print(f"[跳过] 文件不存在：{path}")
                    continue
                try:
                    airports = parse_rwy_file(path)
                    for ap in airports:
                        base = os.path.splitext(os.path.basename(path))[0]
                        out_path = os.path.join(out_dir, f"{base}.stx")
                        counter = 1
                        while os.path.exists(out_path):
                            out_path = os.path.join(out_dir, f"{base}_{counter}.stx")
                            counter += 1
                        ap = normalize_airport(ap)
                        format_airport_to_file(ap, out_path)
                        print(f"[完成] {ap.icao_code} → {out_path}")
                        ok += 1
                except Exception as exc:
                    print(f"[错误] {path}: {exc}")
            print(f"\n转换完成 — 共写入 {ok} 个机场。\n")
            signals.done.emit()

        threading.Thread(target=_run, daemon=True).start()

    def _on_conversion_done(self) -> None:
        self._btn_convert.setEnabled(True)
        self._btn_convert.setText("一键转换 .rwy → .stx")

    # ==================================================================
    # Area B — extraction
    # ==================================================================

    def _load_full_tora_map(self) -> None:
        if not self._stx_path or not os.path.isfile(self._stx_path):
            return
        try:
            airport = parse_single_airport(self._stx_path)
            self._full_tora_map = {rw.designator: rw.tora for rw in airport.runways}
            print(f"[信息] 已从 .stx 加载 {len(self._full_tora_map)} 条跑道："
                  f"{list(self._full_tora_map.keys())}")
        except Exception as exc:
            print(f"[错误] 解析 .stx 失败：{exc}")
            self._full_tora_map = {}

    def _do_extraction(self) -> None:
        stx_path = self._stx_edit.text().strip()
        chart_path = self._chart_edit.text().strip()

        if not stx_path or not os.path.isfile(stx_path):
            QMessageBox.warning(self, "缺少 .stx 文件", "请选择有效的 .stx 文件。")
            return
        if not chart_path or not os.path.isfile(chart_path):
            QMessageBox.warning(self, "缺少图表文件",
                                "请选择有效的图表文件 (PDF/PNG)。")
            return

        self._stx_path = stx_path
        self._load_full_tora_map()

        if not self._full_tora_map:
            QMessageBox.critical(self, "解析错误",
                                 "无法从 .stx 文件中提取跑道数据。")
            return

        self._btn_extract.setEnabled(False)
        self._btn_extract.setText("正在识别…")
        self._progress.setVisible(True)
        self._clear_table()
        self._btn_write.setEnabled(False)

        self._extract_signals = ExtractionSignals()
        self._extract_signals.done.connect(self._on_extraction_done)
        self._extract_signals.error.connect(self._on_extraction_error)

        threading.Thread(
            target=_run_extraction,
            args=(chart_path, self._extract_signals),
            daemon=True,
        ).start()

    def _on_extraction_done(self, data: list[dict]) -> None:
        self._progress.setVisible(False)
        self._btn_extract.setEnabled(True)
        self._btn_extract.setText("开始识别")

        if not data:
            print("[警告] 未能从图表中提取到交叉口数据。\n")
            return

        print(f"[成功] 提取到 {len(data)} 个交叉口：")
        for e in data:
            print(f"     跑道 {e['runway']:5s}  交叉口 {e['intersection_name']:8s}  TORA={e['tora']:.0f} m")
        print()

        self._populate_table(data)
        self._update_write_button()

    def _on_extraction_error(self, err: str) -> None:
        self._progress.setVisible(False)
        self._btn_extract.setEnabled(True)
        self._btn_extract.setText("开始识别")
        print(f"[错误] 提取失败：{err}\n")

    # ==================================================================
    # review table
    # ==================================================================

    RED = QColor("#D32F2F")
    RED_BG = QColor("#FFCDD2")
    DEFAULT_BG = QColor(255, 255, 255) if QApplication.styleHints().colorScheme() == 0 else QColor(45, 45, 45)

    def _clear_table(self) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        self._table.blockSignals(False)
        self._btn_del_row.setEnabled(False)
        self._btn_write.setEnabled(False)

    def _populate_table(self, entries: list[dict]) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(len(entries))

        for row_idx, entry in enumerate(entries):
            rwy = entry["runway"]
            name = entry["intersection_name"]
            tora_val = entry["tora"]
            full = self._full_tora_map.get(rwy, 0.0)

            # runway (editable for manual correction)
            rwy_item = QTableWidgetItem(rwy)
            self._table.setItem(row_idx, 0, rwy_item)

            # intersection name (editable for manual correction)
            int_item = QTableWidgetItem(name)
            self._table.setItem(row_idx, 1, int_item)

            # TORA (editable) — set data BEFORE adding to table so itemChanged sees UserRole
            tora_item = QTableWidgetItem(str(int(tora_val)))
            tora_item.setData(Qt.ItemDataRole.UserRole, full)
            self._table.setItem(row_idx, 2, tora_item)

            # offset (auto-calculated, read-only)
            self._update_offset_cell(row_idx, tora_val, full)

        self._table.blockSignals(False)

    def _update_offset_cell(self, row: int, tora: float, full: float) -> None:
        off_item = self._table.item(row, 3)
        if off_item is None:
            return

        if full is None or full <= 0:
            text = "未知跑道"
            fg = QColor(128, 128, 128)
            bg = self.DEFAULT_BG
        elif tora > full:
            text = f"错误：TORA > {full:.0f}"
            fg = self.RED
            bg = self.RED_BG
        elif tora <= 0:
            text = "—"
            fg = QColor(128, 128, 128)
            bg = self.DEFAULT_BG
        else:
            offset = full - tora
            text = f"{offset:.0f}"
            fg = QColor(0, 0, 0) if QApplication.styleHints().colorScheme() == 0 else QColor(220, 220, 220)
            bg = self.DEFAULT_BG

        off_item = QTableWidgetItem(text)
        off_item.setFlags(off_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        off_item.setForeground(QBrush(fg))
        off_item.setBackground(QBrush(bg))
        self._table.setItem(row, 3, off_item)

    def _on_cell_changed(self, item: QTableWidgetItem) -> None:
        row = item.row()
        col = item.column()

        if col == 2:  # TORA changed
            full = item.data(Qt.ItemDataRole.UserRole)
            if full is None:
                return
            try:
                tora = float(item.text().strip())
            except ValueError:
                tora = 0.0
            self._update_offset_cell(row, tora, full)

        elif col == 0:  # runway changed — update full TORA reference
            new_rwy = item.text().strip()
            full = self._full_tora_map.get(new_rwy)
            tora_item = self._table.item(row, 2)
            if tora_item is not None:
                if full is not None:
                    tora_item.setData(Qt.ItemDataRole.UserRole, full)
                try:
                    tora = float(tora_item.text().strip())
                except ValueError:
                    tora = 0.0
                self._update_offset_cell(row, tora, full if full is not None else 0.0)

        elif col == 1:  # intersection name changed — just re-validate
            pass

        self._update_write_button()

    # ==================================================================
    # write button management
    # ==================================================================

    def _add_row(self) -> None:
        """Insert a new empty row at the bottom of the table for manual entry."""
        row = self._table.rowCount()
        self._table.insertRow(row)

        # runway (editable)
        rwy_item = QTableWidgetItem("")
        self._table.setItem(row, 0, rwy_item)

        # intersection name (editable)
        int_item = QTableWidgetItem("")
        self._table.setItem(row, 1, int_item)

        # TORA (editable, no initial UserRole for offset lookup)
        tora_item = QTableWidgetItem("")
        self._table.setItem(row, 2, tora_item)

        # offset (read-only, initially blank)
        off_item = QTableWidgetItem("—")
        off_item.setFlags(off_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        fg = QColor(128, 128, 128)
        off_item.setForeground(QBrush(fg))
        self._table.setItem(row, 3, off_item)

        self._table.scrollToBottom()
        self._table.selectRow(row)
        self._update_write_button()

    def _delete_selected_rows(self) -> None:
        """Remove all currently selected rows from the table."""
        selected = set()
        for item in self._table.selectedItems():
            selected.add(item.row())
        # Remove in reverse order to preserve indices
        for row in sorted(selected, reverse=True):
            self._table.removeRow(row)
        self._update_write_button()

    def _has_invalid_rows(self) -> bool:
        for row in range(self._table.rowCount()):
            rwy_item = self._table.item(row, 0)
            name_item = self._table.item(row, 1)
            tora_item = self._table.item(row, 2)
            if rwy_item is None or name_item is None or tora_item is None:
                return True
            rwy = rwy_item.text().strip()
            name = name_item.text().strip()
            if not rwy or not name:
                return True
            full = tora_item.data(Qt.ItemDataRole.UserRole)
            try:
                tora = float(tora_item.text().strip())
            except ValueError:
                return True
            if full is not None and (tora > full or tora <= 0):
                return True
            if full is None and tora <= 0:
                return True
        return False

    def _update_write_button(self) -> None:
        has_rows = self._table.rowCount() > 0
        self._btn_del_row.setEnabled(has_rows)
        if not has_rows:
            self._btn_write.setEnabled(False)
            self._btn_write.setText("确认无误，写入 STX")
        elif self._has_invalid_rows():
            self._btn_write.setEnabled(False)
            self._btn_write.setText("存在错误数据，请修正红色标注行")
        else:
            self._btn_write.setEnabled(True)
            self._btn_write.setText("确认无误，写入 STX")

    def _get_validated_data(self) -> list[dict]:
        result = []
        for row in range(self._table.rowCount()):
            rwy = self._table.item(row, 0).text() if self._table.item(row, 0) else ""
            name = self._table.item(row, 1).text() if self._table.item(row, 1) else ""
            tora = float(self._table.item(row, 2).text().strip()) if self._table.item(row, 2) else 0.0
            result.append({
                "runway": rwy,
                "intersection_name": name,
                "tora": tora,
            })
        return result

    # ==================================================================
    # write injection
    # ==================================================================

    def _do_write_injection(self) -> None:
        if self._has_invalid_rows() or self._table.rowCount() == 0:
            QMessageBox.warning(self, "数据验证失败",
                                "请修正所有 TORA 数值异常的行后再写入。")
            return

        confirm = QMessageBox.question(
            self, "确认注入",
            f"即将把 {self._table.rowCount()} 个交叉口数据写入：\n{self._stx_path}\n\n"
            "此操作将覆盖已有的 .stx 文件，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        final_data = self._get_validated_data()
        self._btn_write.setEnabled(False)
        self._btn_write.setText("正在写入…")

        signals = WorkerSignals()
        signals.done.connect(self._on_write_done)

        def _run():
            try:
                update_stx_file(
                    self._stx_path,
                    extracted_data=final_data,
                    output_path=self._stx_path,
                )
                print("[完成] 交叉口数据写入成功。\n")
            except Exception as exc:
                print(f"[错误] 写入失败：{exc}\n")
            finally:
                signals.done.emit()

        threading.Thread(target=_run, daemon=True).start()

    def _on_write_done(self) -> None:
        self._btn_write.setEnabled(True)
        self._update_write_button()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
