#!/usr/bin/env python3
"""
File Manager — Mac ↔ Phone (MTP)
Supports basic flip phones and any device that presents an MTP interface over USB.
Requires: pip install PyQt6 pyusb
"""

import sys
import os
import shutil
import logging
import tempfile
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTreeWidget, QTreeWidgetItem,
    QMessageBox, QProgressDialog, QHeaderView, QMenu,
    QSplitter, QFrame, QStatusBar, QInputDialog, QLineEdit,
    QDialog, QListWidget, QListWidgetItem, QStackedWidget, QSlider,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize, QUrl
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QAction, QIcon, QImage, QImageReader,
    QPainter, QPixmap, QDesktopServices,
)

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    MULTIMEDIA_AVAILABLE = True
except ImportError:
    MULTIMEDIA_AVAILABLE = False

from mtp_client import MTPDevice, HANDLE_ROOT, MTPCancelled, is_disconnect_error


# ---------------------------------------------------------------------------
# Global MTP device instance
# ---------------------------------------------------------------------------
mtp = MTPDevice()

# Audio file extensions recognised for playlist creation
AUDIO_EXTENSIONS = {
    '.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg',
    '.wma', '.opus', '.aiff', '.ape', '.3gp', '.amr',
}


def safe_filename(name: str) -> str:
    """Neutralize device-supplied names so they can't escape the destination
    folder (path separators, '..', empty names)."""
    name = (name or '').replace('/', '_').replace('\x00', '')
    if name in ('', '.', '..'):
        return '_'
    return name


# Media extensions recognised by the preview gallery
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp',
    '.heic', '.heif', '.tif', '.tiff',
}
VIDEO_EXTENSIONS = {
    '.mp4', '.m4v', '.mov', '.avi', '.3gp', '.3g2',
    '.mkv', '.webm', '.mpg', '.mpeg', '.wmv', '.mts',
}


def media_kind(name: str):
    """Return 'image', 'video', or None based on the file extension."""
    ext = os.path.splitext(name or '')[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return 'image'
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    return None


_preview_cache_dir = None


def preview_cache_dir() -> str:
    """Session-scoped temp cache for phone files downloaded for previewing.
    Removed when the main window closes."""
    global _preview_cache_dir
    if _preview_cache_dir is None:
        _preview_cache_dir = tempfile.mkdtemp(prefix='mtp_preview_')
    return _preview_cache_dir


# ---------------------------------------------------------------------------
# Drop-aware tree widget
# ---------------------------------------------------------------------------
class DropTree(QTreeWidget):
    """QTreeWidget that accepts file drops from Finder and from within the app."""

    # Emitted when external files are dropped onto this tree.
    # Payload: list of local file paths (str), target item data (dict or None)
    files_dropped = pyqtSignal(list, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        # Drops only. Internal row-dragging is disabled: Qt's default drag
        # handling would visually re-parent rows in the tree without anything
        # actually moving on disk or on the phone.
        self.setDragEnabled(False)
        self.setDragDropMode(QTreeWidget.DragDropMode.DropOnly)
        self.setDropIndicatorShown(True)

    @staticmethod
    def _is_external_file_drag(event) -> bool:
        return (event.source() is None
                and event.mimeData().hasUrls()
                and any(u.isLocalFile() for u in event.mimeData().urls()))

    def dragEnterEvent(self, event):
        if self._is_external_file_drag(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._is_external_file_drag(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if self._is_external_file_drag(event):
            paths = [u.toLocalFile() for u in event.mimeData().urls()
                     if u.isLocalFile()]
            if paths:
                # Find which tree item (if any) the files were dropped onto
                target_item = self.itemAt(event.position().toPoint())
                target_data = (target_item.data(0, Qt.ItemDataRole.UserRole)
                               if target_item else None)
                event.acceptProposedAction()
                self.files_dropped.emit(paths, target_data)
                return
        event.ignore()

def fmt_size(size) -> str:
    try:
        b = int(size)
    except (ValueError, TypeError):
        return "?"
    if b < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def fmt_mtime(ts) -> str:
    """Format a Unix timestamp (Mac side) as a short local date/time."""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def fmt_mtp_date(s: str) -> str:
    """
    Format an MTP date string (e.g. '20231115T143022' or '20231115T143022.0')
    as a short, readable date/time. Returns '' if it can't be parsed.
    """
    if not s:
        return ""
    raw = s.split('.')[0].split('+')[0].split('-')[0]  # drop fractional/timezone tail
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Directory-listing worker (keeps the slow MTP per-file metadata fetch off
# the GUI thread so the window never freezes on large phone folders)
# ---------------------------------------------------------------------------
class ListWorker(QThread):
    done = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, handle: int, storage_id: int):
        super().__init__()
        self.handle = handle
        self.storage_id = storage_id

    def run(self):
        try:
            items = mtp.list_dir(self.handle, self.storage_id)
            self.done.emit(items)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Transfer worker (background push/pull with per-byte progress + cancel)
# ---------------------------------------------------------------------------
class TransferWorker(QThread):
    # (index, total, name)  — index/total are 0 for files inside a pushed/pulled folder
    item_started = pyqtSignal(int, int, str)
    # (bytes_done, bytes_total) for the file currently transferring
    byte_progress = pyqtSignal(int, int)
    finished = pyqtSignal(bool, str)

    def __init__(self, direction: str, items: list, dest,
                 dest_storage, policy: str = 'overwrite',
                 dest_existing: dict = None):
        super().__init__()
        self.direction = direction            # 'push' (Mac→phone) or 'pull' (phone→Mac)
        self.items = items
        self.dest = dest                      # phone handle (push) or Mac path (pull)
        self.dest_storage = dest_storage
        self.policy = policy                  # 'overwrite' or 'skip' for name conflicts
        self.dest_existing = dest_existing or {}  # lower-name -> info (push side)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def _progress(self, done, total):
        self.byte_progress.emit(done, total)

    def run(self):
        errors = []
        transferred = 0
        skipped = 0
        total = len(self.items)
        for idx, info in enumerate(self.items, 1):
            if self._cancelled:
                break
            name = info['name']
            self.item_started.emit(idx, total, name)
            try:
                if self.direction == 'push':
                    was_skipped = self._push_one(info, name)
                else:
                    was_skipped = self._pull_one(info, name)
                if was_skipped:
                    skipped += 1
                else:
                    transferred += 1
            except MTPCancelled:
                break
            except Exception as e:
                errors.append(f"{name}: {e}")

        ok = not errors
        msg = self._summary(transferred, skipped, errors)
        self.finished.emit(ok, msg)

    # -- push helpers ---------------------------------------------------
    def _push_one(self, info, name) -> int:
        """Returns 1 if skipped, else 0."""
        conflict = name.lower() in self.dest_existing
        is_dir = info.get('is_dir') or os.path.isdir(info['path'])
        if conflict and self.policy == 'skip':
            return 1
        if conflict and self.policy == 'overwrite' and not is_dir:
            # Replace: delete the existing object so we don't create a duplicate.
            try:
                mtp.delete_object(self.dest_existing[name.lower()]['handle'])
            except Exception:
                pass
        if is_dir:
            self._push_dir(info['path'], self.dest, self.dest_storage)
        else:
            mtp.send_object(info['path'], parent_handle=self.dest,
                            storage_id=self.dest_storage, progress_cb=self._progress,
                            cancel_cb=self._is_cancelled)
        return 0

    def _push_dir(self, local_dir, parent_handle, dest_storage):
        folder_name = os.path.basename(local_dir.rstrip('/'))
        new_handle = mtp.create_folder(folder_name, parent_handle, storage_id=dest_storage)
        if new_handle == 0:
            items = mtp.list_dir(parent_handle, dest_storage)
            match = next((i for i in items if i['name'] == folder_name and i['is_dir']), None)
            if match:
                new_handle = match['handle']
        for entry in os.scandir(local_dir):
            if self._cancelled:
                break
            if entry.is_dir(follow_symlinks=False):
                self._push_dir(entry.path, new_handle, dest_storage)
            else:
                self.item_started.emit(0, 0, entry.name)
                mtp.send_object(entry.path, parent_handle=new_handle,
                                storage_id=dest_storage, progress_cb=self._progress,
                                cancel_cb=self._is_cancelled)

    # -- pull helpers ---------------------------------------------------
    def _pull_one(self, info, name) -> int:
        """Returns 1 if skipped, else 0."""
        out = os.path.join(self.dest, safe_filename(name))
        if os.path.exists(out) and self.policy == 'skip':
            return 1
        if info['is_dir']:
            self._pull_dir(info['handle'], out)
        else:
            mtp.get_object(info['handle'], out, progress_cb=self._progress,
                           cancel_cb=self._is_cancelled)
        return 0

    def _pull_dir(self, handle, local_dest):
        os.makedirs(local_dest, exist_ok=True)
        for info in mtp.list_dir(handle):
            if self._cancelled:
                break
            out = os.path.join(local_dest, safe_filename(info['name']))
            if info['is_dir']:
                self._pull_dir(info['handle'], out)
            else:
                self.item_started.emit(0, 0, info['name'])
                mtp.get_object(info['handle'], out, progress_cb=self._progress,
                               cancel_cb=self._is_cancelled)

    # -- summary --------------------------------------------------------
    def _summary(self, transferred, skipped, errors) -> str:
        where = "phone" if self.direction == 'push' else "Mac"
        parts = [f"Transferred {transferred} item(s) to {where}"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if self._cancelled:
            parts.append("cancelled")
        msg = " — ".join(parts)
        if errors:
            msg += "\n\nFailed:\n" + "\n".join(errors)
        return msg


# ---------------------------------------------------------------------------
# Gallery: background media loader
# ---------------------------------------------------------------------------
class GalleryLoadWorker(QThread):
    """
    Prepares gallery media off the GUI thread.

    Image rows are queued up front: download from the phone if needed, then
    decode a thumbnail. Video rows are fetched only on demand (prioritize())
    so one big movie never stalls the photo thumbnails.
    """
    THUMB_PX = 320   # decode size; painted at half that, stays crisp on retina

    path_ready = pyqtSignal(int, str)            # row, local file path
    thumb_ready = pyqtSignal(int, QImage)        # row, decoded thumbnail
    row_failed = pyqtSignal(int, str)            # row, error message
    fetch_progress = pyqtSignal(int, int, int)   # row, bytes done, bytes total

    def __init__(self, items: list, is_phone: bool):
        super().__init__()
        self.items = items
        self.is_phone = is_phone
        self._lock = threading.Lock()
        self._queue = deque(i for i, it in enumerate(items)
                            if it['kind'] == 'image')
        self._queued = set(self._queue)
        self._done = set()
        self._stop = False

    def stop(self):
        self._stop = True

    def prioritize(self, row: int):
        """Move a row to the front of the work queue (adding it if absent)."""
        with self._lock:
            if row in self._done:
                return
            if row in self._queued:
                self._queue.remove(row)
            self._queue.appendleft(row)
            self._queued.add(row)

    def _next_row(self):
        with self._lock:
            if self._queue:
                row = self._queue.popleft()
                self._queued.discard(row)
                return row
        return None

    def run(self):
        while not self._stop:
            row = self._next_row()
            if row is None:
                self.msleep(80)   # idle — wait for on-demand video requests
                continue
            try:
                self._process(row)
            except MTPCancelled:
                break
            except Exception as e:
                self.row_failed.emit(row, str(e))
                if is_disconnect_error(e):
                    break   # every further download would fail the same way
            finally:
                with self._lock:
                    self._done.add(row)

    def _process(self, row: int):
        item = self.items[row]
        path = item['path'] if not self.is_phone \
            else self._fetch_from_phone(row, item)
        if self._stop:
            return
        self.path_ready.emit(row, path)
        if item['kind'] == 'image':
            img = self._load_thumb(path)
            if img is not None and not self._stop:
                self.thumb_ready.emit(row, img)

    def _fetch_from_phone(self, row: int, item: dict) -> str:
        name = f"{item['handle']:08x}_{item.get('size', 0)}_{safe_filename(item['name'])}"
        path = os.path.join(preview_cache_dir(), name)
        if not os.path.exists(path):
            mtp.get_object(
                item['handle'], path,
                progress_cb=lambda d, t, r=row: self.fetch_progress.emit(r, d, t),
                cancel_cb=lambda: self._stop)
        return path

    def _load_thumb(self, path: str):
        reader = QImageReader(path)
        reader.setAutoTransform(True)   # honour EXIF rotation
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            reader.setScaledSize(size.scaled(
                self.THUMB_PX, self.THUMB_PX,
                Qt.AspectRatioMode.KeepAspectRatio))
        img = reader.read()
        return None if img.isNull() else img


# ---------------------------------------------------------------------------
# Gallery: thumbnail grid + full-size viewer
# ---------------------------------------------------------------------------
class GalleryDialog(QDialog):
    """Preview gallery for the images/videos of one folder.

    Grid of thumbnails; double-click (or Enter) opens the full-size viewer
    with Prev/Next + arrow-key navigation. Videos play in-app when Qt
    Multimedia is available, otherwise in the default external app.
    """
    THUMB = 160

    def __init__(self, parent, title: str, items: list, is_phone: bool,
                 start_index: int = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(920, 660)
        self.setSizeGripEnabled(True)
        self.items = items
        self.is_phone = is_phone
        # Mac files are already local; phone files get a path once fetched
        self._paths = [None if is_phone else it.get('path') for it in items]
        self._errors = [None] * len(items)
        self._current = -1
        self._full_pixmap = None
        self._player = None
        self._audio = None
        self._video_widget = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        self.pages = QStackedWidget()
        root.addWidget(self.pages)

        # ── Grid page ──
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(QSize(self.THUMB, self.THUMB))
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(10)
        self.grid.setWordWrap(True)
        for it in items:
            glyph = "🎬" if it['kind'] == 'video' else "🖼"
            li = QListWidgetItem(self._glyph_icon(glyph), it['name'])
            li.setSizeHint(QSize(self.THUMB + 24, self.THUMB + 44))
            self.grid.addItem(li)
        self.grid.itemDoubleClicked.connect(self._open_item)
        self.grid.itemActivated.connect(self._open_item)
        self.pages.addWidget(self.grid)

        # ── Viewer page ──
        viewer = QWidget()
        vl = QVBoxLayout(viewer)
        vl.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.addWidget(self._btn("⬅  Grid", self._back_to_grid))
        top.addStretch()
        self.name_lbl = QLabel("")
        self.name_lbl.setStyleSheet("font-weight: bold;")
        top.addWidget(self.name_lbl)
        top.addStretch()
        self.open_ext_btn = self._btn("↗  Open in app", self._open_external)
        top.addWidget(self.open_ext_btn)
        vl.addLayout(top)

        self.center = QStackedWidget()
        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setMinimumSize(1, 1)   # allow shrinking with the window
        self.center.addWidget(self.img_label)
        self.info_label = QLabel("")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setStyleSheet("color: #666; font-size: 14px;")
        self.center.addWidget(self.info_label)
        vl.addWidget(self.center, 1)

        # Video transport controls (hidden while viewing images)
        self.video_bar = QWidget()
        vb = QHBoxLayout(self.video_bar)
        vb.setContentsMargins(0, 0, 0, 0)
        self.play_btn = self._btn("⏸ Pause", self._toggle_play)
        vb.addWidget(self.play_btn)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        vb.addWidget(self.seek, 1)
        self.time_lbl = QLabel("0:00 / 0:00")
        vb.addWidget(self.time_lbl)
        vl.addWidget(self.video_bar)
        self.video_bar.hide()

        nav = QHBoxLayout()
        nav.addStretch()
        nav.addWidget(self._btn("◀  Prev", lambda: self._show_row(self._current - 1)))
        self.pos_lbl = QLabel("")
        self.pos_lbl.setStyleSheet("padding: 0 12px;")
        nav.addWidget(self.pos_lbl)
        nav.addWidget(self._btn("Next  ▶", lambda: self._show_row(self._current + 1)))
        nav.addStretch()
        vl.addLayout(nav)

        self.viewer_page = viewer
        self.pages.addWidget(viewer)

        # ── Background loader ──
        self.worker = GalleryLoadWorker(items, is_phone)
        self.worker.path_ready.connect(self._on_path_ready)
        self.worker.thumb_ready.connect(self._on_thumb_ready)
        self.worker.row_failed.connect(self._on_row_failed)
        self.worker.fetch_progress.connect(self._on_fetch_progress)
        self.worker.start()

        if start_index is not None:
            self._show_row(start_index)

    # -- small helpers --------------------------------------------------
    def _btn(self, text: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setAutoDefault(False)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # keep arrow keys for nav
        b.clicked.connect(slot)
        return b

    def _glyph_icon(self, glyph: str) -> QIcon:
        pm = QPixmap(self.THUMB, self.THUMB)
        pm.fill(QColor(234, 236, 241))
        p = QPainter(pm)
        f = QFont()
        f.setPointSize(46)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        p.end()
        return QIcon(pm)

    def _show_info(self, text: str):
        self.info_label.setText(text)
        self.center.setCurrentWidget(self.info_label)

    # -- navigation -----------------------------------------------------
    def _open_item(self, li: QListWidgetItem):
        self._show_row(self.grid.row(li))

    def _show_row(self, row: int):
        if not (0 <= row < len(self.items)):
            return
        if row == self._current and self.pages.currentWidget() is self.viewer_page:
            return   # itemActivated + itemDoubleClicked can both fire
        self._current = row
        self._stop_playback()
        self.pages.setCurrentWidget(self.viewer_page)
        self.grid.setCurrentRow(row)
        it = self.items[row]
        self.name_lbl.setText(it['name'])
        self.pos_lbl.setText(f"{row + 1} / {len(self.items)}")
        self.video_bar.hide()
        self.open_ext_btn.setEnabled(self._paths[row] is not None)
        if self._errors[row]:
            self._show_info(f"⚠  {self._errors[row]}")
            return
        if self._paths[row] is None:
            self.worker.prioritize(row)
            self._show_info("Downloading from phone…")
            return
        self._display(row)

    def _back_to_grid(self):
        self._stop_playback()
        self.video_bar.hide()
        self.pages.setCurrentWidget(self.grid)

    # -- display --------------------------------------------------------
    def _display(self, row: int):
        path = self._paths[row]
        self.open_ext_btn.setEnabled(True)
        if self.items[row]['kind'] == 'image':
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            img = reader.read()
            if img.isNull():
                self._show_info("Could not decode this image —\ntry '↗ Open in app'")
                return
            self._full_pixmap = QPixmap.fromImage(img)
            self.center.setCurrentWidget(self.img_label)
            self._refit()
        else:
            self._play_video(path)

    def _refit(self):
        if self._full_pixmap is None or self._full_pixmap.isNull():
            return
        if self.center.currentWidget() is not self.img_label:
            return
        self.img_label.setPixmap(self._full_pixmap.scaled(
            self.img_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    # -- video ----------------------------------------------------------
    def _ensure_player(self) -> bool:
        if self._player is not None:
            return True
        if not MULTIMEDIA_AVAILABLE:
            return False
        self._video_widget = QVideoWidget()
        self.center.addWidget(self._video_widget)
        self._audio = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_widget)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self.seek.sliderMoved.connect(self._player.setPosition)
        return True

    def _play_video(self, path: str):
        if not self._ensure_player():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            self._show_info("Qt Multimedia not available —\nopened in the default app instead")
            return
        self.center.setCurrentWidget(self._video_widget)
        self.video_bar.show()
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        self.play_btn.setText("⏸ Pause")

    def _toggle_play(self):
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self.play_btn.setText("▶ Play")
        else:
            self._player.play()
            self.play_btn.setText("⏸ Pause")

    def _stop_playback(self):
        if self._player is not None:
            self._player.stop()

    @staticmethod
    def _fmt_ms(ms: int) -> str:
        s = max(0, ms) // 1000
        return f"{s // 60}:{s % 60:02d}"

    def _on_position(self, pos: int):
        if not self.seek.isSliderDown():
            self.seek.setValue(pos)
        self.time_lbl.setText(
            f"{self._fmt_ms(pos)} / {self._fmt_ms(self._player.duration())}")

    def _on_duration(self, dur: int):
        self.seek.setRange(0, dur)

    def _open_external(self):
        if 0 <= self._current < len(self._paths) and self._paths[self._current]:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._paths[self._current]))

    # -- loader signals -------------------------------------------------
    def _on_path_ready(self, row: int, path: str):
        self._paths[row] = path
        if row == self._current and self.pages.currentWidget() is self.viewer_page:
            self._display(row)

    def _on_thumb_ready(self, row: int, img: QImage):
        li = self.grid.item(row)
        if li is not None:
            li.setIcon(QIcon(QPixmap.fromImage(img)))

    def _on_row_failed(self, row: int, msg: str):
        self._errors[row] = msg
        li = self.grid.item(row)
        if li is not None:
            li.setIcon(self._glyph_icon("⚠"))
        if row == self._current:
            self._show_info(f"⚠  {msg}")

    def _on_fetch_progress(self, row: int, done: int, total: int):
        if row == self._current and total > 0 and self._paths[row] is None:
            self._show_info(f"Downloading from phone…  {min(done * 100 // total, 100)}%")

    # -- events / cleanup -----------------------------------------------
    def keyPressEvent(self, event):
        if self.pages.currentWidget() is self.viewer_page:
            k = event.key()
            if k in (Qt.Key.Key_Left, Qt.Key.Key_Up):
                self._show_row(self._current - 1)
                return
            if k in (Qt.Key.Key_Right, Qt.Key.Key_Down):
                self._show_row(self._current + 1)
                return
            if k == Qt.Key.Key_Space:
                self._toggle_play()
                return
            if k == Qt.Key.Key_Escape:
                self._back_to_grid()
                return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refit()

    def done(self, result: int):
        self._stop_playback()
        if self._player is not None:
            self._player.setSource(QUrl())
        self.worker.stop()
        self.worker.wait(3000)
        super().done(result)


# ---------------------------------------------------------------------------
# File Pane
# ---------------------------------------------------------------------------
class FilePane(QWidget):
    """Dual-purpose file browser: Mac (local fs) or Phone (MTP)."""

    path_changed = pyqtSignal(str)
    # Emitted when files are dropped onto the phone pane:
    # (local_paths: list[str], dest_handle: int, dest_storage_id: int)
    drop_requested = pyqtSignal(list, int, int)

    def __init__(self, title: str, is_phone: bool = False):
        super().__init__()
        self.title = title
        self.is_phone = is_phone

        # Mac side state
        self.current_path: str = ""

        # Phone side (MTP) state
        self.current_handle: int = HANDLE_ROOT
        self.current_storage_id: int = None   # None = virtual multi-storage root
        self._nav_stack: List[tuple] = []   # (handle, display_path, storage_id)
        self._display_path: str = "/"

        # Cut clipboard (phone-side move)
        self._clipboard: dict = {}   # {'items': [...], 'storage_id': int} or empty

        # Background directory-listing state (phone side)
        self._load_token = 0         # bumped on each load to discard stale results
        # Live listing workers. Kept in a set until each finishes: dropping
        # the last Python reference to a running QThread can crash the app.
        self._list_workers = set()

        self._build_ui()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        header = QLabel(self.title)
        header.setFont(QFont("-apple-system", 13, QFont.Weight.Bold))
        header.setStyleSheet("padding: 2px 4px;")
        layout.addWidget(header)

        # Shortcut buttons
        sc_row = QHBoxLayout()
        sc_row.setSpacing(4)
        if not self.is_phone:
            home = Path.home()
            for lbl, path in [
                ("🏠 Home",       str(home)),
                ("🖥 Desktop",    str(home / "Desktop")),
                ("📥 Downloads",  str(home / "Downloads")),
                ("📄 Documents",  str(home / "Documents")),
            ]:
                b = QPushButton(lbl)
                b.setStyleSheet("font-size: 11px; padding: 3px 6px;")
                b.clicked.connect(lambda _, p=path: self.navigate(p))
                sc_row.addWidget(b)
            # Volumes button — shows /Volumes (macOS) or / on other systems
            volumes_root = "/Volumes" if os.path.isdir("/Volumes") else "/"
            vol_b = QPushButton("💽 Volumes")
            vol_b.setStyleSheet("font-size: 11px; padding: 3px 6px;")
            vol_b.setToolTip(f"Browse mounted drives ({volumes_root})")
            vol_b.clicked.connect(lambda _, p=volumes_root: self.navigate(p))
            sc_row.addWidget(vol_b)
            sc_row.addStretch()
            layout.addLayout(sc_row)

            # Second row: dynamically-populated external drive shortcuts
            self._vol_sc_row = QHBoxLayout()
            self._vol_sc_row.setSpacing(4)
            self._vol_sc_row.addStretch()
            layout.addLayout(self._vol_sc_row)
            self._vol_sc_widgets = []   # track buttons so we can clear/refresh
        else:
            root_b = QPushButton("📱 Root")
            root_b.setStyleSheet("font-size: 11px; padding: 3px 6px;")
            root_b.setToolTip("Go to phone root")
            root_b.clicked.connect(self._go_phone_root)
            sc_row.addWidget(root_b)
            for lbl, names in [
                ("📷 DCIM",     ["DCIM"]),
                ("📥 Download", ["Download", "Downloads"]),
                ("🎵 Music",     ["Music"]),
                ("🖼 Pictures",  ["Pictures"]),
                ("🎬 Videos",    ["Videos", "Video"]),
            ]:
                b = QPushButton(lbl)
                b.setStyleSheet("font-size: 11px; padding: 3px 6px;")
                b.clicked.connect(lambda _, n=names: self._go_phone_folder(n))
                sc_row.addWidget(b)
            sc_row.addStretch()
            layout.addLayout(sc_row)

        # Path bar
        path_row = QHBoxLayout()
        self.up_btn = QPushButton("↑")
        self.up_btn.setFixedWidth(32)
        self.up_btn.setToolTip("Go up one level")
        self.up_btn.clicked.connect(self.go_up)
        path_row.addWidget(self.up_btn)

        self.path_lbl = QLabel("")
        self.path_lbl.setStyleSheet("font-size: 11px; color: #333; padding: 2px 4px;"
                                     "background: #f0f0f0; border-radius: 3px;")
        self.path_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_row.addWidget(self.path_lbl, 1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(150)
        self.search.setStyleSheet("font-size: 11px; padding: 2px 4px;")
        self.search.textChanged.connect(self._apply_filter)
        path_row.addWidget(self.search)

        gallery_b = QPushButton("🖼")
        gallery_b.setFixedWidth(32)
        gallery_b.setToolTip("Preview images && videos in this folder")
        gallery_b.clicked.connect(lambda: self._open_gallery())
        path_row.addWidget(gallery_b)

        refresh_b = QPushButton("↺")
        refresh_b.setFixedWidth(32)
        refresh_b.setToolTip("Refresh")
        refresh_b.clicked.connect(self.refresh)
        path_row.addWidget(refresh_b)
        layout.addLayout(path_row)

        # Tree
        self.tree = DropTree()
        self.tree.setHeaderLabels(["Name", "Size", "Type", "Modified"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.files_dropped.connect(self._on_files_dropped)
        layout.addWidget(self.tree)

        self.footer = QLabel("0 items")
        self.footer.setStyleSheet("color: gray; font-size: 11px; padding: 2px 4px;")
        layout.addWidget(self.footer)

    # ------------------------------------------------------------------
    # Navigation — Mac
    # ------------------------------------------------------------------
    def navigate(self, path: str):
        if not path:
            return
        self.current_path = path
        self.path_lbl.setText(path)
        self._load()
        self.path_changed.emit(path)
        if not self.is_phone:
            self._refresh_volume_shortcuts()

    def _refresh_volume_shortcuts(self):
        """Populate the dynamic drive-shortcut row with mounted external volumes."""
        if not hasattr(self, '_vol_sc_row'):
            return
        # Remove old widgets
        for w in self._vol_sc_widgets:
            self._vol_sc_row.removeWidget(w)
            w.deleteLater()
        self._vol_sc_widgets.clear()

        volumes_dir = "/Volumes" if os.path.isdir("/Volumes") else None
        if not volumes_dir:
            return
        try:
            drives = sorted(
                d for d in os.listdir(volumes_dir)
                if os.path.isdir(os.path.join(volumes_dir, d))
                and not d.startswith('.')
                and d != "Macintosh HD"        # skip the internal boot drive
            )
        except Exception:
            return
        # Insert before the trailing stretch (last item in layout)
        insert_pos = self._vol_sc_row.count() - 1  # before the stretch
        for name in drives:
            full = os.path.join(volumes_dir, name)
            # Pick a label icon based on name hints
            icon = "💾"
            low = name.lower()
            if any(x in low for x in ("usb", "flash", "stick", "thumb")):
                icon = "🔌"
            elif any(x in low for x in ("sd", "card")):
                icon = "📇"
            b = QPushButton(f"{icon} {name}")
            b.setStyleSheet(
                "font-size: 11px; padding: 3px 6px;"
                "background-color: #fff8e1; border: 1px solid #ffe082;")
            b.setToolTip(full)
            b.clicked.connect(lambda _, p=full: self.navigate(p))
            self._vol_sc_row.insertWidget(insert_pos, b)
            self._vol_sc_widgets.append(b)
            insert_pos += 1

    def refresh(self):
        self._load()

    def go_up(self):
        if self.is_phone:
            self._go_up_phone()
        else:
            parent = str(Path(self.current_path).parent)
            if parent != self.current_path:
                self.navigate(parent)

    # ------------------------------------------------------------------
    # Navigation — Phone (MTP)
    # ------------------------------------------------------------------
    def _go_phone_root(self):
        """Navigate to the virtual storage-list root."""
        if not mtp.is_connected():
            self.footer.setText("Phone not connected — click 'Connect Phone'")
            return
        try:
            ids = mtp.get_storage_ids()
        except Exception as e:
            self.footer.setText(f"Error: {e}")
            return
        self._nav_stack.clear()
        self._display_path = "/"
        self.current_handle = HANDLE_ROOT
        self.current_storage_id = None
        # If only one storage, skip virtual root and go straight in
        if len(ids) == 1:
            self._enter_storage(ids[0])
        else:
            self.path_lbl.setText("/ (phone)")
            self._load()

    def _enter_storage(self, storage_id: int):
        """Navigate into a specific storage (shows its root folder list)."""
        try:
            info = mtp.get_storage_info(storage_id)
            name = info.get('description', f'Storage {hex(storage_id)}')
        except Exception:
            name = f'Storage {hex(storage_id)}'
        self._nav_stack.append((self.current_handle, self._display_path, None))
        self.current_handle = 0  # parent=0 means root-level objects of the storage
        self.current_storage_id = storage_id
        self._display_path = f"/{name}"
        self.path_lbl.setText(self._display_path)
        self._load()

    def _go_phone_handle(self, handle: int, name: str):
        self._nav_stack.append((self.current_handle, self._display_path, self.current_storage_id))
        self.current_handle = handle
        self._display_path = self._display_path.rstrip("/") + "/" + name
        self.path_lbl.setText(self._display_path)
        self._load()
        self.path_changed.emit(self._display_path)

    def _go_up_phone(self):
        if self._nav_stack:
            prev_handle, prev_path, prev_storage = self._nav_stack.pop()
            self.current_handle = prev_handle
            self._display_path = prev_path
            self.current_storage_id = prev_storage
            self.path_lbl.setText(prev_path)
            self._load()
        else:
            self._go_phone_root()

    def _go_phone_folder(self, names: List[str]):
        """Jump to a named folder, searching within the first (or current) storage."""
        if not mtp.is_connected():
            return
        # Determine which storage to search
        storage_id = self.current_storage_id
        if storage_id is None:
            ids = mtp.cached_storage_ids() or mtp.get_storage_ids()
            storage_id = ids[0] if ids else None
        if storage_id is None:
            self._go_phone_root()
            return
        try:
            items = mtp.list_dir(0, storage_id)   # 0 = root level of this storage
        except Exception:
            self._go_phone_root()
            return
        for try_name in names:
            match = next((i for i in items
                          if i['name'].lower() == try_name.lower() and i['is_dir']), None)
            if match:
                # Reset to storage root then navigate in
                self._nav_stack.clear()
                self._display_path = "/"
                self.current_handle = 0
                self.current_storage_id = None
                self._enter_storage(storage_id)
                self._go_phone_handle(match['handle'], match['name'])
                return
        self._go_phone_root()

    # ------------------------------------------------------------------
    # Load contents
    # ------------------------------------------------------------------
    def _load(self):
        # Any in-flight listing result is now stale.
        self._load_token += 1
        self.tree.clear()

        if self.is_phone:
            if not mtp.is_connected():
                self.footer.setText("No phone connected — click 'Connect Phone'")
                return
            if self.current_storage_id is None:
                self._load_storage_list()
            else:
                # Slow path: fetch the listing on a worker thread so the GUI
                # never freezes on large folders (each file is a USB round-trip).
                self.footer.setText("Loading…")
                token = self._load_token
                worker = ListWorker(self.current_handle, self.current_storage_id)
                worker.done.connect(lambda items, t=token: self._populate_phone(items, t))
                worker.error.connect(lambda msg, t=token: self._on_list_error(msg, t))
                worker.finished.connect(lambda w=worker: self._reap_list_worker(w))
                self._list_workers.add(worker)
                worker.start()
        else:
            self._load_mac()

    def _load_storage_list(self):
        """Virtual root: one row per phone storage (internal, SD card…)."""
        black = QColor(0, 0, 0)
        try:
            storage_ids = mtp.get_storage_ids()
        except Exception as e:
            self.footer.setText(f"Error getting storages: {e}")
            return
        for sid in storage_ids:
            try:
                sinfo = mtp.get_storage_info(sid)
                sname = sinfo.get('description', f'Storage {hex(sid)}')
                free = fmt_size(sinfo.get('free_space', 0))
            except Exception:
                sname = f'Storage {hex(sid)}'
                free = '?'
            item = QTreeWidgetItem()
            item.setText(0, f"💾  {sname}")
            item.setText(1, f"{free} free")
            item.setText(2, "Storage")
            item.setData(0, Qt.ItemDataRole.UserRole, {
                'is_storage': True,
                'storage_id': sid,
                'name': sname,
                'is_dir': True,
                'size': 0,
                'handle': HANDLE_ROOT,
            })
            for col in range(4):
                item.setForeground(col, black)
            self.tree.addTopLevelItem(item)
        self._finish_load()

    def _populate_phone(self, raw: list, token: int):
        if token != self._load_token:
            return  # user navigated away before this listing finished
        black = QColor(0, 0, 0)
        self.tree.clear()
        raw.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        for info in raw:
            item = QTreeWidgetItem()
            icon = "📁" if info['is_dir'] else "📄"
            item.setText(0, f"{icon}  {info['name']}")
            item.setText(1, "—" if info['is_dir'] else fmt_size(info['size']))
            item.setText(2, "Folder" if info['is_dir'] else "File")
            item.setText(3, fmt_mtp_date(info.get('date_modified', '')))
            item.setData(0, Qt.ItemDataRole.UserRole, info)
            for col in range(4):
                item.setForeground(col, black)
            self.tree.addTopLevelItem(item)
        self._finish_load()

    def _on_list_error(self, msg: str, token: int):
        if token != self._load_token:
            return
        self.footer.setText(f"Error listing: {msg}")

    def _reap_list_worker(self, worker):
        self._list_workers.discard(worker)
        worker.deleteLater()

    def _load_mac(self):
        black = QColor(0, 0, 0)
        if not self.current_path:
            return
        try:
            entries = list(os.scandir(self.current_path))
        except PermissionError:
            self.footer.setText("Permission denied")
            return
        except FileNotFoundError:
            self.footer.setText("Folder not found")
            return
        mac_items = []
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                size = 0
                mtime = 0
                try:
                    st = e.stat()
                    size = st.st_size
                    mtime = st.st_mtime
                except OSError:
                    pass
                mac_items.append({
                    "name": e.name,
                    "path": e.path,
                    "is_dir": is_dir,
                    "size": size,
                    "mtime": mtime,
                    "handle": 0,
                })
            except OSError:
                pass
        mac_items.sort(key=lambda x: (not x["is_dir"], x["name"].startswith("."), x["name"].lower()))
        for info in mac_items:
            item = QTreeWidgetItem()
            icon = "📁" if info['is_dir'] else "📄"
            item.setText(0, f"{icon}  {info['name']}")
            item.setText(1, "—" if info['is_dir'] else fmt_size(info['size']))
            item.setText(2, "Folder" if info['is_dir'] else "File")
            item.setText(3, fmt_mtime(info.get('mtime', 0)))
            item.setData(0, Qt.ItemDataRole.UserRole, info)
            for col in range(4):
                item.setForeground(col, black)
            self.tree.addTopLevelItem(item)
        self._finish_load()

    def _finish_load(self):
        count = self.tree.topLevelItemCount()
        if self.is_phone and self.current_storage_id is not None:
            self.footer.setText(f"{count} item{'s' if count != 1 else ''}  •  drag files here to copy to phone")
        else:
            self.footer.setText(f"{count} item{'s' if count != 1 else ''}")
        self._apply_filter(self.search.text())

    def _apply_filter(self, text: str):
        """Hide rows whose name doesn't contain the filter text (case-insensitive)."""
        needle = (text or "").strip().lower()
        shown = 0
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            name = (data.get('name', '') if data else item.text(0)).lower()
            match = needle in name
            item.setHidden(not match)
            if match:
                shown += 1
        if needle:
            self.footer.setText(f"{shown} match{'es' if shown != 1 else ''} for “{text}”")

    # ------------------------------------------------------------------
    # Preview gallery
    # ------------------------------------------------------------------
    def _media_items(self) -> list:
        """Visible (unfiltered-out) images/videos of the current listing."""
        items = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.isHidden():
                continue
            data = it.data(0, Qt.ItemDataRole.UserRole)
            if not data or data.get('is_dir') or data.get('is_storage'):
                continue
            kind = media_kind(data.get('name', ''))
            if kind:
                items.append({**data, 'kind': kind})
        return items

    def _open_gallery(self, start_name: str = None):
        if self.is_phone and not mtp.is_connected():
            self.footer.setText("Phone not connected — click 'Connect Phone'")
            return
        items = self._media_items()
        if not items:
            self.footer.setText("No images or videos in this folder")
            return
        start = None
        if start_name is not None:
            start = next((i for i, it in enumerate(items)
                          if it['name'] == start_name), None)
        where = "Phone" if self.is_phone else "Mac"
        dlg = GalleryDialog(self, f"Gallery — {where} ({len(items)} media files)",
                            items, self.is_phone, start_index=start)
        dlg.exec()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def selected_items(self) -> list:
        result = []
        for sel in self.tree.selectedItems():
            data = sel.data(0, Qt.ItemDataRole.UserRole)
            if data:
                result.append(data)
        return result

    # ------------------------------------------------------------------
    # Drop handler (from Finder or other app)
    # ------------------------------------------------------------------
    def _on_files_dropped(self, paths: list, target_data: dict):
        """
        Called when files are dragged from Finder / Desktop onto this pane.
        - Phone pane: emits drop_requested so MainWindow can run MTP upload.
        - Mac pane:   copies files directly into the displayed folder.
        """
        if self.is_phone:
            if not mtp.is_connected():
                QMessageBox.warning(self, "Drop", "No phone connected.")
                return
            if self.current_storage_id is None:
                QMessageBox.warning(self, "Drop",
                    "Navigate into a storage folder on the phone first,\n"
                    "then drag files here.")
                return
            # Determine destination handle: if dropped onto a sub-folder, use that
            dest_handle = self.current_handle
            if target_data and target_data.get('is_dir') and not target_data.get('is_storage'):
                dest_handle = target_data['handle']
            self.drop_requested.emit(paths, dest_handle, self.current_storage_id)
        else:
            # Mac side: copy from paths into current_path
            if not self.current_path:
                return
            errors = []
            for src in paths:
                try:
                    dst = str(Path(self.current_path) / os.path.basename(src))
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                except Exception as e:
                    errors.append(f"{os.path.basename(src)}: {e}")
            if errors:
                QMessageBox.warning(self, "Copy Error", "\n".join(errors))
            self._load()

    # ------------------------------------------------------------------
    # Double click
    # ------------------------------------------------------------------
    def _on_double_click(self, item, _col):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        if not data['is_dir']:
            # Double-clicking an image/video opens the gallery viewer on it
            if media_kind(data.get('name', '')):
                self._open_gallery(start_name=data['name'])
            return
        if self.is_phone:
            if data.get('is_storage'):
                self._enter_storage(data['storage_id'])
            else:
                self._go_phone_handle(data['handle'], data['name'])
        else:
            self.navigate(data['path'])

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------
    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu()
        if item:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data['is_dir']:
                open_act = QAction("Open", self)
                if self.is_phone:
                    if data.get('is_storage'):
                        open_act.triggered.connect(lambda: self._enter_storage(data['storage_id']))
                    else:
                        open_act.triggered.connect(lambda: self._go_phone_handle(data['handle'], data['name']))
                else:
                    open_act.triggered.connect(lambda: self.navigate(data['path']))
                menu.addAction(open_act)
                # Playlist option for phone folders (not storage-root pseudo-items)
                if self.is_phone and not data.get('is_storage'):
                    playlist_act = QAction("🎵  Create Playlist from folder…", self)
                    _h = data['handle']
                    _n = data['name']
                    _s = data.get('storage_id') or self.current_storage_id
                    playlist_act.triggered.connect(
                        lambda checked=False, h=_h, n=_n, s=_s:
                            self._create_playlist_from_folder(h, n, s))
                    menu.addAction(playlist_act)
                menu.addSeparator()
            rename_act = QAction("✏  Rename…", self)
            rename_act.triggered.connect(self._rename_selected)
            # Can't rename storage-root pseudo-items
            if self.is_phone and data and data.get('is_storage'):
                rename_act.setEnabled(False)
            menu.addAction(rename_act)
            menu.addSeparator()
            if self.is_phone and data and not data.get('is_storage'):
                cut_act = QAction("✂  Cut", self)
                cut_act.triggered.connect(self._cut_selected)
                menu.addAction(cut_act)
                menu.addSeparator()
            del_act = QAction("🗑  Delete", self)
            del_act.triggered.connect(self._delete_selected)
            menu.addAction(del_act)
            menu.addSeparator()
        new_act = QAction("📁  New Folder…", self)
        new_act.triggered.connect(self._new_folder)
        menu.addAction(new_act)
        # Paste — only when clipboard has items and we're inside a real phone folder
        if self.is_phone and self._clipboard.get('items') and self.current_storage_id is not None:
            menu.addSeparator()
            paste_act = QAction(f"📋  Paste {len(self._clipboard['items'])} item(s) here", self)
            paste_act.triggered.connect(self._paste_here)
            menu.addAction(paste_act)
            clear_act = QAction("Clear clipboard", self)
            def _do_clear():
                self._clipboard.clear()
                self.footer.setText("Clipboard cleared")
            clear_act.triggered.connect(_do_clear)
            menu.addAction(clear_act)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------
    def _new_folder(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self.is_phone:
            if not mtp.is_connected():
                QMessageBox.warning(self, "Error", "Phone not connected.")
                return
            try:
                mtp.create_folder(name, self.current_handle,
                                  storage_id=self.current_storage_id)
                self._load()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not create folder:\n{e}")
        else:
            try:
                os.makedirs(str(Path(self.current_path) / name))
                self._load()
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def _rename_selected(self):
        selected = self.selected_items()
        if len(selected) != 1:
            QMessageBox.warning(self, "Rename", "Select exactly one item to rename.")
            return
        info = selected[0]
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=info['name'])
        if not ok or not new_name.strip() or new_name.strip() == info['name']:
            return
        new_name = new_name.strip()
        if self.is_phone:
            if not mtp.is_connected():
                QMessageBox.warning(self, "Rename", "Phone not connected.")
                return
            try:
                mtp.rename_object(info['handle'], new_name)
                self._load()
            except Exception as e:
                QMessageBox.critical(self, "Rename Error", str(e))
        else:
            try:
                os.rename(info['path'], str(Path(self.current_path) / new_name))
                self._load()
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def _cut_selected(self):
        selected = self.selected_items()
        if not selected:
            return
        self._clipboard = {'items': selected, 'storage_id': self.current_storage_id}
        names = ', '.join(i['name'] for i in selected)
        self.footer.setText(
            f"✂  {len(selected)} item(s) cut — navigate to destination, right-click → Paste")

    def _paste_here(self):
        if not self._clipboard.get('items'):
            return
        if not mtp.is_connected():
            QMessageBox.warning(self, "Paste", "Phone not connected.")
            return
        if self.current_storage_id is None:
            QMessageBox.warning(self, "Paste", "Navigate into a storage folder first.")
            return
        errors = []
        moved = []
        for info in self._clipboard['items']:
            try:
                mtp.move_object(info['handle'], self.current_storage_id,
                                self.current_handle)
                moved.append(info['name'])
            except Exception as e:
                errors.append(f"{info['name']}: {e}")
        self._clipboard.clear()
        if errors:
            QMessageBox.warning(self, "Move Errors",
                                f"Moved {len(moved)} item(s).\n\nFailed:\n" + "\n".join(errors))
        elif moved:
            self.footer.setText(f"✓ Moved {len(moved)} item(s) here")
        self._load()

    # ------------------------------------------------------------------
    # Playlist creation
    # ------------------------------------------------------------------
    def _collect_audio_from_folder(self, handle: int, storage_id: int,
                                   rel_prefix: str = '') -> list:
        """
        Recursively collect audio files under `handle`.
        Returns list of dicts: {'rel_path': str, 'display': str}
        rel_path is relative to the folder that will contain the playlist.
        """
        result = []
        try:
            items = mtp.list_dir(handle, storage_id)
        except Exception:
            return result
        for item in sorted(items, key=lambda x: x['name'].lower()):
            rel = f"{rel_prefix}{item['name']}" if rel_prefix else item['name']
            if item['is_dir']:
                result.extend(
                    self._collect_audio_from_folder(
                        item['handle'], storage_id, rel_prefix=rel + '/'))
            else:
                if os.path.splitext(item['name'])[1].lower() in AUDIO_EXTENSIONS:
                    result.append({
                        'rel_path': rel,
                        'display': os.path.splitext(item['name'])[0],
                    })
        return result

    def _create_playlist_from_folder(self, folder_handle: int,
                                     folder_name: str, storage_id: int):
        if not mtp.is_connected():
            QMessageBox.warning(self, "Playlist", "Phone not connected.")
            return

        self.footer.setText(f"🔍 Scanning '{folder_name}' for audio tracks…")
        QApplication.processEvents()

        tracks = self._collect_audio_from_folder(folder_handle, storage_id)

        if not tracks:
            QMessageBox.information(self, "No Audio Found",
                f"No audio files were found in '{folder_name}'.\n\n"
                "Supported formats: " +
                ", ".join(sorted(e.lstrip('.').upper()
                                 for e in AUDIO_EXTENSIONS)))
            self.footer.setText("")
            return

        # Ask for playlist name
        playlist_name, ok = QInputDialog.getText(
            self, "Create Playlist",
            f"Found {len(tracks)} audio track(s) in '{folder_name}'.\n\n"
            "Playlist file name (saved inside the same folder):",
            text=folder_name)
        if not ok or not playlist_name.strip():
            self.footer.setText("")
            return
        playlist_name = playlist_name.strip()
        if not playlist_name.lower().endswith('.m3u'):
            playlist_name += '.m3u'

        # Build M3U content
        lines = ['#EXTM3U', '']
        for t in tracks:
            lines.append(f'#EXTINF:0,{t["display"]}')
            lines.append(t['rel_path'])
        lines.append('')
        m3u_bytes = '\n'.join(lines).encode('utf-8')

        # Write to a temp file whose basename IS the playlist name,
        # then upload via send_object (which uses os.path.basename for the filename)
        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, playlist_name)
        try:
            with open(tmp_path, 'wb') as f:
                f.write(m3u_bytes)
            mtp.send_object(tmp_path, parent_handle=folder_handle,
                            storage_id=storage_id)
            QMessageBox.information(self, "Playlist Created",
                f"✓  '{playlist_name}' created with {len(tracks)} track(s).\n\n"
                "The playlist file was saved inside the folder.\n"
                "Open your phone's Music app and look under Playlists.")
            self._load()
        except Exception as e:
            QMessageBox.critical(self, "Playlist Error",
                f"Could not upload playlist:\n{e}")
        finally:
            try:
                os.unlink(tmp_path)
                os.rmdir(tmp_dir)
            except Exception:
                pass
        self.footer.setText("")

    def _delete_selected(self):
        selected = self.selected_items()
        if not selected:
            return
        names = "\n".join(i['name'] for i in selected)
        reply = QMessageBox.question(self, "Delete",
                                     f"Delete {len(selected)} item(s)?\n\n{names}",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        errors = []
        for info in selected:
            if self.is_phone:
                if not mtp.is_connected():
                    errors.append(f"{info['name']}: not connected")
                    continue
                try:
                    mtp.delete_object(info['handle'])
                except Exception as e:
                    errors.append(f"{info['name']}: {e}")
            else:
                try:
                    if os.path.isdir(info['path']):
                        shutil.rmtree(info['path'])
                    else:
                        os.remove(info['path'])
                except Exception as e:
                    errors.append(f"{info['name']}: {e}")
        if errors:
            QMessageBox.warning(self, "Delete Errors", "\n".join(errors))
        self._load()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("File Manager — Mac ↔ Phone")
        self.setMinimumSize(1050, 680)
        self.worker = None
        self._build_ui()

        # Start Mac pane at Desktop
        desktop = str(Path.home() / "Desktop")
        start = desktop if os.path.isdir(desktop) else str(Path.home())
        self.mac_pane.navigate(start)

        # Try connecting immediately, then check every 5s (silent)
        QTimer.singleShot(600, self._try_connect)
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._auto_detect)
        self._poll_timer.start(5000)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_l = QVBoxLayout(central)
        root_l.setContentsMargins(8, 8, 8, 8)
        root_l.setSpacing(6)

        # Top bar
        top = QHBoxLayout()
        self.phone_status_lbl = QLabel("● No phone")
        self.phone_status_lbl.setStyleSheet("color: #cc0000; font-size: 12px; font-weight: bold;")
        top.addWidget(self.phone_status_lbl)
        self.phone_name_lbl = QLabel("")
        self.phone_name_lbl.setStyleSheet("color: #555; font-size: 11px; margin-left: 6px;")
        top.addWidget(self.phone_name_lbl)
        top.addStretch()

        conn_btn = QPushButton("🔌  Connect Phone")
        conn_btn.setStyleSheet(
            "QPushButton { background-color: #0a84ff; color: #fff; font-weight: bold;"
            " border: 1px solid #006edc; border-radius: 5px; padding: 5px 14px; }"
            "QPushButton:hover { background-color: #006edc; }"
            "QPushButton:pressed { background-color: #0055b3; }")
        conn_btn.setToolTip("Scan USB for an MTP phone and connect")
        conn_btn.clicked.connect(lambda: self._try_connect(show_errors=True))
        top.addWidget(conn_btn)

        disc_btn = QPushButton("✖  Disconnect")
        disc_btn.setStyleSheet("padding: 5px 10px;")
        disc_btn.clicked.connect(self._disconnect)
        top.addWidget(disc_btn)

        help_btn = QPushButton("?  Help")
        help_btn.setStyleSheet("padding: 5px 10px;")
        help_btn.clicked.connect(self._show_help)
        top.addWidget(help_btn)

        root_l.addLayout(top)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color: #d0d0d0;")
        root_l.addWidget(div)

        # Dual panes
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.mac_pane = FilePane("💻  Mac", is_phone=False)
        splitter.addWidget(self.mac_pane)

        # Transfer buttons
        xw = QWidget()
        xw.setFixedWidth(120)
        xl = QVBoxLayout(xw)
        xl.setContentsMargins(4, 0, 4, 0)
        xl.addStretch()
        self.to_phone_btn = QPushButton("→\nPhone")
        self.to_phone_btn.setStyleSheet(
            "QPushButton { padding: 10px 4px; font-weight: bold;"
            " background-color: #e8f5e9; color: #1b5e20; border: 1px solid #a5d6a7; border-radius: 5px; }"
            "QPushButton:hover { background-color: #c8e6c9; }"
            "QPushButton:pressed { background-color: #a5d6a7; }")
        self.to_phone_btn.setToolTip("Copy selected Mac files to phone")
        self.to_phone_btn.clicked.connect(self._transfer_to_phone)
        xl.addWidget(self.to_phone_btn)
        xl.addSpacing(12)
        self.to_mac_btn = QPushButton("←\nMac")
        self.to_mac_btn.setStyleSheet(
            "QPushButton { padding: 10px 4px; font-weight: bold;"
            " background-color: #e3f2fd; color: #0d47a1; border: 1px solid #90caf9; border-radius: 5px; }"
            "QPushButton:hover { background-color: #bbdefb; }"
            "QPushButton:pressed { background-color: #90caf9; }")
        self.to_mac_btn.setToolTip("Copy selected phone files to Mac")
        self.to_mac_btn.clicked.connect(self._transfer_to_mac)
        xl.addWidget(self.to_mac_btn)
        xl.addStretch()
        splitter.addWidget(xw)

        self.phone_pane = FilePane("📱  Phone", is_phone=True)
        self.phone_pane.drop_requested.connect(self._on_drop_to_phone)
        splitter.addWidget(self.phone_pane)
        splitter.setSizes([480, 120, 480])
        root_l.addWidget(splitter)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready — connect your phone via USB and click '🔌 Connect Phone'")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _try_connect(self, show_errors: bool = False):
        if mtp.is_connected():
            return
        # Always wipe stale USB state before trying to connect
        mtp.disconnect()
        devices = mtp.find_mtp_devices()
        if not devices:
            if show_errors:
                QMessageBox.warning(self, "No Phone Found",
                    "No MTP phone detected.\n\n"
                    "• Plug in the USB cable\n"
                    "• On the phone: when asked, choose 'File Transfer' or 'MTP'\n"
                    "• Try a different USB cable or port")
            return
        d = devices[0]
        ok, msg = mtp.connect(vendor_id=d['vendor_id'], product_id=d['product_id'])
        if ok:
            name = d['name'] or 'Phone'
            self.phone_status_lbl.setText("✓ Connected")
            self.phone_status_lbl.setStyleSheet("color: #007a1f; font-size: 12px; font-weight: bold;")
            self.phone_name_lbl.setText(name)
            self.status.showMessage(f"Connected: {name}")
            self.phone_pane._go_phone_root()
        else:
            if show_errors:
                QMessageBox.critical(self, "Connection Failed",
                    f"{msg}\n\nMake sure:\n"
                    "• The phone is in 'File Transfer' / 'MTP' mode (not Charging)\n"
                    "• Try unplugging and replugging, then click Connect again")
            self._set_disconnected()

    def _disconnect(self):
        mtp.disconnect()
        self._set_disconnected()
        self.phone_pane.tree.clear()
        self.phone_pane.footer.setText("Disconnected")
        self.status.showMessage("Disconnected")

    def _handle_disconnection(self):
        """Called when the USB device disappears unexpectedly (e.g. cable pulled)."""
        mtp.disconnect()  # soft cleanup — safe even if USB is gone
        self._set_disconnected()
        self.phone_pane.tree.clear()
        self.phone_pane.footer.setText("Phone disconnected — reconnect USB cable")
        self.status.showMessage(
            "Phone disconnected — reconnect cable, choose 'File Transfer', then click Connect Phone")

    def _set_disconnected(self):
        self.phone_status_lbl.setText("● No phone")
        self.phone_status_lbl.setStyleSheet("color: #cc0000; font-size: 12px; font-weight: bold;")
        self.phone_name_lbl.setText("")

    def _auto_detect(self):
        if mtp.is_connected():
            # Probe that the USB device is still physically present
            if not mtp.ping():
                self._handle_disconnection()
        else:
            devices = mtp.find_mtp_devices()
            if devices:
                self._try_connect(show_errors=False)
            else:
                self._set_disconnected()

    def _show_help(self):
        QMessageBox.information(self, "How to Connect",
            "Connecting your phone:\n\n"
            "1.  Plug the phone into your Mac with a USB data cable\n\n"
            "2.  On the phone — a popup will appear asking for USB mode.\n"
            "     Choose:  'File Transfer'  or  'MTP'\n"
            "     (NOT 'Charging only')\n\n"
            "3.  Click '🔌 Connect Phone' in this app\n\n"
            "──── Troubleshooting ────\n"
            "• Use a data cable, not a charge-only cable\n"
            "• Try a different USB port on your Mac\n"
            "• Unplug, wait 5 seconds, replug\n"
            "• If the phone shows 'Trust This Computer?', tap Yes\n"
        )

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------
    def _on_drop_to_phone(self, paths: list, dest_handle: int, dest_storage: int):
        """Handle files dragged from Finder/Desktop onto the phone pane."""
        items = [{'name': os.path.basename(p), 'path': p, 'is_dir': os.path.isdir(p)}
                 for p in paths]
        self._start_transfer('push', items, dest_handle, dest_storage, self.phone_pane)

    def _transfer_to_phone(self):
        selected = self.mac_pane.selected_items()
        if not selected:
            QMessageBox.information(self, "Transfer", "Select file(s) on the Mac side first.")
            return
        if not mtp.is_connected():
            QMessageBox.warning(self, "Transfer", "No phone connected.")
            return
        dest_handle = self.phone_pane.current_handle
        dest_storage = self.phone_pane.current_storage_id
        if dest_storage is None:
            QMessageBox.warning(self, "Transfer",
                "Navigate into a phone storage folder first before transferring.")
            return
        self._start_transfer('push', selected, dest_handle, dest_storage, self.phone_pane)

    def _transfer_to_mac(self):
        selected = self.phone_pane.selected_items()
        if not selected:
            QMessageBox.information(self, "Transfer", "Select file(s) on the Phone side first.")
            return
        dest_path = self.mac_pane.current_path
        if not dest_path:
            QMessageBox.warning(self, "Transfer", "Navigate to a destination folder on Mac first.")
            return
        self._start_transfer('pull', selected, dest_path, None, self.mac_pane)

    # ------------------------------------------------------------------
    # Transfer orchestration: conflict check → progress dialog → worker
    # ------------------------------------------------------------------
    def _start_transfer(self, direction, items, dest, dest_storage, refresh_pane):
        # Detect name conflicts at the destination so we can ask the user once.
        dest_existing = {}
        conflicts = []
        try:
            if direction == 'push':
                for e in mtp.list_dir(dest, dest_storage):
                    dest_existing[e['name'].lower()] = e
                conflicts = [i for i in items if i['name'].lower() in dest_existing]
            else:
                conflicts = [i for i in items
                             if os.path.exists(os.path.join(dest, i['name']))]
        except Exception:
            pass

        policy = 'overwrite'
        if conflicts:
            policy = self._ask_conflict_policy(conflicts)
            if policy is None:
                return  # user cancelled

        progress = QProgressDialog("Preparing…", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setWindowTitle("Transferring…")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        self.worker = TransferWorker(direction, items, dest, dest_storage,
                                     policy, dest_existing)
        progress.canceled.connect(self.worker.cancel)
        self.worker.item_started.connect(
            lambda i, t, n: self._on_item_started(progress, i, t, n))
        self.worker.byte_progress.connect(
            lambda d, tot: self._on_byte_progress(progress, d, tot))
        self.worker.finished.connect(
            lambda ok, msg: self._transfer_done(ok, msg, progress, refresh_pane))
        progress.show()
        self.worker.start()

    def _ask_conflict_policy(self, conflicts):
        """Ask how to handle name conflicts. Returns 'overwrite', 'skip', or None (cancel)."""
        n = len(conflicts)
        names = ", ".join(c['name'] for c in conflicts[:6])
        if n > 6:
            names += f", … (+{n - 6} more)"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Items Already Exist")
        box.setText(f"{n} item(s) already exist at the destination:\n\n{names}")
        box.setInformativeText("How should the existing items be handled?")
        overwrite_btn = box.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
        skip_btn = box.addButton("Skip", QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(skip_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == overwrite_btn:
            return 'overwrite'
        if clicked == skip_btn:
            return 'skip'
        return None

    def _on_item_started(self, progress, idx, total, name):
        if idx > 0:
            progress.setLabelText(f"Copying {idx}/{total}:\n{name}")
        else:
            progress.setLabelText(f"Copying:\n{name}")
        progress.setValue(0)

    def _on_byte_progress(self, progress, done, total):
        if total > 0:
            progress.setValue(min(int(done * 100 / total), 100))

    def _transfer_done(self, ok: bool, msg: str, progress, refresh_pane):
        progress.close()
        # Detect if the error was caused by the phone being disconnected
        if not ok and is_disconnect_error(msg):
            self._handle_disconnection()
            QMessageBox.warning(self, "Phone Disconnected",
                "The phone was disconnected during transfer.\n\n"
                "Reconnect the cable, choose 'File Transfer', then click 🔌 Connect Phone.")
            return
        if ok:
            self.status.showMessage(f"✓ {msg}")
        else:
            QMessageBox.critical(self, "Transfer Error", msg)
        refresh_pane.refresh()

    def closeEvent(self, event):
        if _preview_cache_dir is not None:
            shutil.rmtree(_preview_cache_dir, ignore_errors=True)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    logging.basicConfig(
        level=(logging.DEBUG if ('--verbose' in sys.argv or '-v' in sys.argv)
               else logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window,          QColor(246, 246, 246))
    palette.setColor(QPalette.ColorRole.Base,            QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(242, 242, 247))
    palette.setColor(QPalette.ColorRole.Text,            QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Button,          QColor(225, 228, 235))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(0, 122, 255))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    # Global button style — light background, dark text, rounded corners
    app.setStyleSheet("""
        QPushButton {
            background-color: #e1e4eb;
            color: #141414;
            border: 1px solid #b8bcc8;
            border-radius: 5px;
            padding: 4px 10px;
        }
        QPushButton:hover  { background-color: #d0d4de; border-color: #9499a8; }
        QPushButton:pressed { background-color: #bbbfc9; }
        QPushButton:disabled { background-color: #ececec; color: #aaa; border-color: #d0d0d0; }
    """)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
