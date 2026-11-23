"""Calibre toolbar action and background worker."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from tempfile import gettempdir
from threading import Event
from time import monotonic
import traceback

from calibre.db.errors import NoSuchFormat
from calibre.gui2 import error_dialog, info_dialog, question_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import QFileDialog, QIcon, QLabel, QPixmap, QProgressDialog, QStorageInfo, QThread, Qt, pyqtSignal

from .preferences import PREFS, STATE_DIR, get_profiles, get_transfer_options
from .structure import edit_structure
from .xteink.batch import process_batch
from .xteink.cache import Cache
from .xteink.io_utils import OptimisationError
from .xteink.settings import BALANCED
from .xteink.transfer import Book, Destination
from .xteink.validation import find_epubcheck


PLUGIN_NAME = "XTEINK X4 Pro Optimiser"
ACTION = "Optimise for XTEINK X4 Pro"


def _private_directory(path):
    path = path.resolve()
    if any((parent / "metadata.db").exists() for parent in (path, *path.parents)):
        raise OptimisationError("Calibre’s plugin configuration and working folders must be outside its libraries.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def _volume_hint():
    matches = []
    for volume in QStorageInfo.mountedVolumes():
        label = volume.name() or volume.displayName()
        if volume.isValid() and volume.isReady() and not volume.isReadOnly() and re.search(
            r"\b(?:xteink|x4[\s_-]*pro)\b", label, flags=re.IGNORECASE
        ):
            matches.append(volume.rootPath())
    return matches[0] if len(matches) == 1 else ""


class BatchWorker(QThread):
    progressed = pyqtSignal(int, str)

    def __init__(self, parent, api, ids, destination, transfer, options, prefs):
        super().__init__(parent)
        self.api = api
        self.ids = ids
        self.destination = destination
        self.transfer = transfer
        self.options = options
        self.prefs = prefs
        self.cancel = Event()
        self.summary = None
        self.failure = None
        self._last_progress = 0.0

    def _book(self, book_id):
        mi = self.api.get_metadata(book_id, get_cover=False, get_user_categories=False)
        authors = mi.authors or []
        return Book(book_id, mi.title or f"Book {book_id}", " & ".join(authors),
                    mi.series or "", authors[0] if authors else "")

    def _copy(self, book_id, writer):
        try:
            return self.api.copy_format_to(book_id, "EPUB", writer)
        except NoSuchFormat:
            return False

    def _progress(self, index, text):
        now = monotonic()
        if now - self._last_progress >= 0.14:
            self._last_progress = now
            self.progressed.emit(index, text)

    def run(self):
        handler = None
        try:
            log = None
            if self.prefs["logging"]:
                log_path = _private_directory(STATE_DIR) / "optimiser.log"
                handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                logger = logging.Logger("XTEINK", level=logging.INFO)
                logger.addHandler(handler)
                log = logger.info
            scratch_root = STATE_DIR / "work" if self.prefs["temporary"] == "plugin" else Path(gettempdir())
            scratch = _private_directory(scratch_root)
            cache = Cache(_private_directory(STATE_DIR / "cache")) if self.options and self.prefs["cache"] else None
            command = find_epubcheck(self.prefs["epubcheck_path"]) if self.options and self.options.validate_epubcheck else ()
            if log:
                log(f"Started batch of {len(self.ids)} → {self.destination.path} | options={self.options}")
            self.summary = process_batch(
                self.ids, self._book, self._copy, self.destination, self.transfer, self.options,
                scratch=scratch, cache=cache, epubcheck=command, structure_editor=edit_structure,
                cancel=self.cancel, progress=self._progress, log=log,
            )
            if log:
                result = self.summary
                log(f"Finished batch | written={result.written} current={result.current} skipped={result.skipped_existing} "
                    f"missing={result.missing} failed={result.failed} cancelled={result.cancelled}")
        except Exception:
            self.failure = traceback.format_exc()
        finally:
            if handler is not None:
                handler.close()


class XteinkAction(InterfaceAction):
    name = PLUGIN_NAME
    action_spec = (ACTION, None, "Optimise EPUB copies for the XTEINK X4 Pro", ())
    action_add_menu = True
    dont_add_to = frozenset({"context-menu-device", "context-menu-cover-browser", "context-menu-split"})

    def genesis(self):
        self.worker = None
        self.progress_dialog = None
        self._closing = False
        icon = QPixmap()
        icon.loadFromData(get_resources("images/reader.png"))
        self.qaction.setIcon(QIcon(icon))
        self.menuless_qaction.setIcon(self.qaction.icon())
        self.qaction.triggered.connect(lambda: self._start())
        self.qaction.menu().addAction("Optimise selected books", lambda: self._start())
        self.qaction.menu().addAction("Optimise entire library…", lambda: self._start(all_books=True))
        self.qaction.menu().addSeparator()
        self.usb = self.qaction.menu().addAction("Send to XTEINK (USB)…", lambda: self._start(usb=True))
        self.usb_optimised = self.qaction.menu().addAction(
            "Send Optimised to XTEINK…", lambda: self._start(usb=True, force_optimise=True)
        )
        self.qaction.menu().addSeparator()
        self.qaction.menu().addAction("Clear optimisation cache…", self._clear_cache)
        self.qaction.menu().addAction("Settings…", self._configure)
        self.qaction.menu().aboutToShow.connect(self._refresh_transfer)
        self._refresh_transfer()

    def _refresh_transfer(self):
        self.usb.setEnabled(PREFS["enable_transfer"])
        self.usb_optimised.setEnabled(PREFS["enable_transfer"])

    def _configure(self):
        self.interface_action_base_plugin.do_user_config(self.gui)
        self._refresh_transfer()

    def _select_destination(self, usb, count):
        if usb:
            starting = PREFS["usb_dir"]
            if not starting or not Path(starting).is_dir():
                starting = _volume_hint() if PREFS["detect_device"] else ""
            folder = QFileDialog.getExistingDirectory(self.gui, "Choose the XTEINK USB book folder", starting)
            if not folder:
                return None
            destination = Destination(folder)
            if not question_dialog(
                self.gui, "Confirm XTEINK USB destination",
                f"Send EPUBs from {count} selected book(s) to this folder?\n\n{destination.path}\n\n"
                "Check that this is your XTEINK X4 Pro SD card in USB mode.",
            ):
                return None
            return destination

        folder = PREFS["export_dir"]
        if not folder or not Path(folder).is_dir():
            folder = QFileDialog.getExistingDirectory(self.gui, "Choose an export folder for optimised EPUBs", folder)
        return Destination(folder) if folder else None

    def _start(self, all_books=False, usb=False, force_optimise=False):
        if self.worker is not None:
            info_dialog(self.gui, PLUGIN_NAME, "A batch is already running. Wait for it or cancel it first.", show=True)
            return
        if usb and not PREFS["enable_transfer"]:
            return
        api = self.gui.current_db.new_api
        ids = sorted(api.all_book_ids()) if all_books else list(self.gui.library_view.get_selected_ids())
        if not ids:
            info_dialog(self.gui, PLUGIN_NAME, "Select one or more books in the Calibre library.", show=True)
            return
        try:
            destination = self._select_destination(usb, len(ids))
            if destination is None:
                return
            if all_books and not question_dialog(
                self.gui, "Optimise entire library",
                f"Check all {len(ids)} book(s) in this library and export their EPUB copies to:\n\n{destination.path}",
            ):
                return
            transfer = get_transfer_options()
            options = None
            if not usb or force_optimise or PREFS["automatically_optimise"]:
                profiles = get_profiles()
                options = profiles.get(PREFS["profile"], profiles[BALANCED])
            prefs = {key: PREFS[key] for key in ("cache", "logging", "temporary", "epubcheck_path")}
        except (OSError, ValueError, OptimisationError) as error:
            error_dialog(self.gui, "Check the XTEINK settings or destination", str(error), show=True)
            return

        self._begin(api, ids, destination, transfer, options, prefs)

    def _begin(self, api, ids, destination, transfer, options, prefs):
        worker = self.worker = BatchWorker(self, api, ids, destination, transfer, options, prefs)
        dialog = self.progress_dialog = QProgressDialog("Preparing EPUB copies…", "Cancel", 0, len(ids), self.gui)
        label = QLabel("Preparing EPUB copies…", dialog)
        label.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setLabel(label)
        dialog.setWindowTitle(PLUGIN_NAME)
        dialog.setMinimumWidth(460)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.canceled.connect(worker.cancel.set)
        worker.progressed.connect(self._show_progress)
        worker.finished.connect(self._finished)
        dialog.show()
        worker.start()

    def _show_progress(self, index, message):
        if self.worker and not self.worker.cancel.is_set() and self.progress_dialog:
            self.progress_dialog.setLabelText(message)
            self.progress_dialog.setValue(index)

    def _finished(self):
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        if self.progress_dialog is not None:
            self.progress_dialog.hide()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None
        worker.deleteLater()
        if self._closing:
            return
        if worker.failure:
            error_dialog(self.gui, PLUGIN_NAME, "The batch could not start or finish.", worker.failure, show=True)
            return

        summary = worker.summary
        status = "Batch cancelled." if summary.cancelled else "Batch finished."
        counts = (
            f"{summary.written} exported · {summary.current} already current · "
            f"{summary.skipped_existing} skipped · {summary.missing} without EPUB · {summary.failed} failed"
        )
        details = [f"Destination: {worker.destination.path}"]
        if worker.options is not None:
            details += [
                f"Cache hits: {summary.cache_hits}",
                f"Images changed: {summary.changed_images} of {summary.scanned_images} checked on this run",
                f"EPUBs with structural changes: {summary.changed_structure}",
                f"Processed EPUB sizes: {summary.original_bytes:,} → {summary.output_bytes:,} bytes",
            ]
        details.extend(summary.details)
        if summary.omitted_details:
            details.append(f"{summary.omitted_details} additional details omitted.")
        if worker.prefs["logging"]:
            details.append(f"Log: {STATE_DIR / 'optimiser.log'}")
        info_dialog(self.gui, PLUGIN_NAME, f"{status}\n\n{counts}", "\n".join(details), show=True)

    def _clear_cache(self):
        if self.worker is not None:
            return
        cache = Cache(STATE_DIR / "cache")
        if not cache.directory.exists():
            info_dialog(self.gui, PLUGIN_NAME, "The optimisation cache is empty.", show=True)
            return
        if question_dialog(self.gui, "Clear optimisation cache", "Remove reusable optimised EPUBs from the plugin cache?"):
            try:
                cache = Cache(_private_directory(cache.directory))
                count, size = cache.clear()
                info_dialog(self.gui, PLUGIN_NAME, f"Removed {count} cached copy/copies ({size:,} bytes).", show=True)
            except (OSError, OptimisationError) as error:
                error_dialog(self.gui, "Cache could not be cleared", str(error), show=True)

    def shutting_down(self):
        self._closing = True
        if self.worker is not None:
            self.worker.cancel.set()
            self.worker.wait()
