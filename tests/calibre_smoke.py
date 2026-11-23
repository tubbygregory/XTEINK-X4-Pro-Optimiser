"""Exercise the installed plugin with real Calibre APIs and bundled Qt."""

from dataclasses import replace
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from calibre.constants import numeric_version
from calibre.customize.ui import initialized_plugins
from calibre.db.legacy import LibraryDatabase
from calibre.ebooks.metadata.book.base import Metadata
from calibre.gui2 import Application
from calibre.gui2.keyboard import Manager
from PIL import Image
from qt.core import QEventLoop, QMainWindow, QTimer, QT_VERSION_STR, PYQT_VERSION_STR, qVersion

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import CSS, image_bytes, make_epub

from calibre_plugins.xteink_x4_pro import action as ui
from calibre_plugins.xteink_x4_pro.preferences import PREFS
from calibre_plugins.xteink_x4_pro.structure import edit_structure
from calibre_plugins.xteink_x4_pro.xteink.engine import optimise_epub
from calibre_plugins.xteink_x4_pro.xteink.settings import Options
from calibre_plugins.xteink_x4_pro.xteink.validation import find_epubcheck, validate_epub


def require(condition, message):
    # calibre-debug uses Python -OO, so these checks must survive optimisation.
    if not condition:
        raise AssertionError(message)


class LibraryView:
    def __init__(self, ids):
        self.ids = ids

    def get_selected_ids(self):
        return self.ids


def complete(action):
    worker = action.worker
    require(worker is not None, 'worker is not None')
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    worker.finished.connect(loop.quit)
    timer.start(60_000)
    loop.exec()
    require(timer.isActive(), "Timed out waiting for Calibre background job")
    timer.stop()
    require(worker.failure is None, worker.failure)
    require(action.worker is None, 'action.worker is None')
    return worker.summary


def test_structure(root):
    master = make_epub(root / "structural.epub", css=CSS + b"""
@font-face { font-family: Custom; src: url('fonts/test.ttf'); }
.text { font-family: Custom, serif; background: url('used.png'); }
""", font=True, large=True, images={
        "used.png": ("image/png", image_bytes("PNG"), False),
        "unused.png": ("image/png", image_bytes("PNG"), False),
    })
    raw = master.read_bytes()
    result = root / "structural-result.epub"
    options = replace(
        Options(), preserve_styling=False, remove_unused_css=True, remove_fonts=True,
        remove_unused_resources=True, split_xhtml=True, split_kib=32,
    )
    command = find_epubcheck(os.environ.get("XTEINK_TEST_EPUBCHECK", ""))
    if command:
        options = replace(options, validate_epubcheck=True)
    stats = optimise_epub(master, result, options, structure_editor=edit_structure, epubcheck=command)
    require(master.read_bytes() == raw, 'master.read_bytes() == raw')
    require(stats.changed_structure, stats.warnings)
    book = validate_epub(result)
    require(book.references >= {"EPUB/text/notes.xhtml", "EPUB/nav.xhtml"}, 'book.references >= {"EPUB/text/notes.xhtml", "EPUB/nav.xhtml"}')
    with ZipFile(result) as archive:
        names = archive.namelist()
        require("EPUB/fonts/test.ttf" not in names, '"EPUB/fonts/test.ttf" not in names')
        require("EPUB/unused.png" not in names, '"EPUB/unused.png" not in names')
        require("EPUB/used.png" in names, '"EPUB/used.png" in names')
        require(b".unused" not in archive.read("EPUB/theme.css"), 'b".unused" not in archive.read("EPUB/theme.css")')
        require(b"@media" in archive.read("EPUB/theme.css"), 'b"@media" in archive.read("EPUB/theme.css")')
        require(b"test.ttf" not in archive.read("EPUB/theme.css"), 'b"test.ttf" not in archive.read("EPUB/theme.css")')
        chapters = [name for name in names if name.startswith("EPUB/text/chapter") and name.endswith(".xhtml")]
        require(len(chapters) >= 4, chapters)
        notes = archive.read("EPUB/text/notes.xhtml")
        chapter_with_anchor = next(name for name in chapters if b'id="section-38"' in archive.read(name))
        require((Path(chapter_with_anchor).name + "#section-38").encode("utf-8") in notes, notes)
        require(b"ruby" in archive.read("EPUB/text/chapter.xhtml"), 'b"ruby" in archive.read("EPUB/text/chapter.xhtml")')

    scripted = make_epub(root / "scripted.epub", scripted=True, large=True)
    opts = replace(Options(), split_xhtml=True, split_kib=32)
    stats = optimise_epub(scripted, result, opts, structure_editor=edit_structure)
    require(not stats.changed_structure and stats.warnings, 'not stats.changed_structure and stats.warnings')
    require(scripted.read_bytes() == result.read_bytes(), 'scripted.read_bytes() == result.read_bytes()')


def test_action(root):
    plugin = next(plugin for plugin in initialized_plugins() if plugin.name == ui.PLUGIN_NAME)
    require(plugin.version == (1, 0, 0), 'plugin.version == (1, 0, 0)')
    require(plugin.is_customizable(), 'plugin.is_customizable()')
    settings = plugin.config_widget()
    require([settings.tabs.tabText(index) for index in range(settings.tabs.count())] == [
        "General", "Images", "EPUB structure", "Transfer", "Profiles",
    ], '[settings.tabs.tabText(index) for index in range(settings.tabs.count())] == [ "General", "Images", "EPUB structure", "Transfer", "Profiles", ]')
    library = root / "lib"
    output = root / "Optimised EPUBs"
    usb = root / "Simulated XTEINK volume"
    output.mkdir()
    usb.mkdir()
    database = LibraryDatabase(str(library))
    gui = QMainWindow()
    try:
        master = make_epub(root / "import.epub", images={
            "photo.jpg": ("image/jpeg", image_bytes(size=(700, 1000)), True),
        })
        ids, _ = database.new_api.add_books(
            [(Metadata("旅と海", ["Amélie García"]), {"EPUB": str(master)}),
             (Metadata("Second book", ["Unknown"]), {"EPUB": str(make_epub(root / "second.epub"))}),
             (Metadata("No EPUB", ["Unknown"]), {})],
            run_hooks=False,
        )
        baseline = {}
        for book_id in ids[:2]:
            path = Path(database.new_api.format_abspath(book_id, "EPUB"))
            baseline[path] = path.read_bytes()
        gui.current_db = database
        gui.library_view = LibraryView(ids[:1])
        gui.keyboard = Manager(gui)
        action = plugin.load_actual_plugin(gui)
        action.interface_action_base_plugin = plugin
        action.do_genesis()
        toolbar = gui.addToolBar("Test toolbar")
        toolbar.addAction(action.qaction)
        require(action.qaction.text() == ui.ACTION and not action.qaction.icon().isNull(), 'action.qaction.text() == ui.ACTION and not action.qaction.icon().isNull()')
        require(any("Send Optimised" in item.text() for item in action.qaction.menu().actions()), 'any("Send Optimised" in item.text() for item in action.qaction.menu().actions())')

        settings.pref_fields["export_dir"].setText(str(output))
        settings.pref_fields["structure"].setCurrentIndex(settings.pref_fields["structure"].findData("flat"))
        settings.option_fields["jpeg_quality"].setValue(72)
        require(settings.profile_picker.currentText() == "Custom", 'settings.profile_picker.currentText() == "Custom"')
        require(settings._current_options().jpeg_quality == 72, 'settings._current_options().jpeg_quality == 72')
        require(settings.validate(), 'settings.validate()')
        plugin.save_settings(settings)
        reopened = plugin.config_widget()
        require(reopened.option_fields["jpeg_quality"].value() == 72, 'reopened.option_fields["jpeg_quality"].value() == 72')
        require(reopened.pref_fields["export_dir"].text() == str(output), 'reopened.pref_fields["export_dir"].text() == str(output)')
        reopened.close()

        if target := os.environ.get("XTEINK_SCREENSHOTS"):
            settings.profile_picker.setCurrentText("Balanced")
            settings.tabs.setCurrentIndex(1)
            settings.resize(680, 342)
            settings.show()
            Application.instance().processEvents()
            require(settings.grab().save(str(Path(target) / "settings.png")), 'settings.grab().save(str(Path(target) / "settings.png"))')
            settings.hide()

        with patch.object(ui, "info_dialog") as summary, patch.object(ui, "error_dialog") as errors:
            action.qaction.trigger()
            result = complete(action)
            require((result.written, result.changed_images) == (1, 1), result)
            validate_epub(output / "旅と海 - Amélie García.epub")
            require(summary.called and not errors.called, 'summary.called and not errors.called')
            action.qaction.trigger()
            result = complete(action)
            require((result.current, result.cache_hits) == (1, 1), result)
            with patch.object(ui, "question_dialog", return_value=True):
                action._start(all_books=True)
                result = complete(action)
                require((result.written, result.current, result.missing) == (1, 1, 1), result)

            with patch.object(ui, "QFileDialog") as choose, patch.object(ui, "question_dialog") as confirm:
                choose.getExistingDirectory.return_value = str(usb)
                confirm.return_value = False
                action.usb_optimised.trigger()
                require(action.worker is None and not list(usb.iterdir()), 'action.worker is None and not list(usb.iterdir())')
                confirm.return_value = True
                action.usb_optimised.trigger()
                result = complete(action)
                require(result.written == 1, result)
                require((usb / "旅と海 - Amélie García.epub").read_bytes() != next(iter(baseline.values())), '(usb / "旅と海 - Amélie García.epub").read_bytes() != next(iter(baseline.values()))')

                PREFS["automatically_optimise"] = False
                PREFS["overwrite"] = "replace"
                action.usb.trigger()
                result = complete(action)
                require(result.written == 1 and result.changed_images == 0, result)
                require((usb / "旅と海 - Amélie García.epub").read_bytes() == next(iter(baseline.values())), '(usb / "旅と海 - Amélie García.epub").read_bytes() == next(iter(baseline.values()))')
            require(not errors.called, 'not errors.called')
        require(database.new_api.all_book_ids() == frozenset(ids), 'database.new_api.all_book_ids() == frozenset(ids)')
        for path, original in baseline.items():
            require(path.read_bytes() == original, path)
    finally:
        settings.close()
        gui.close()
        database.close()


def main():
    print(
        f"Calibre {'.'.join(map(str, numeric_version))}, Python {sys.version.split()[0]}, "
        f"Qt {qVersion()} (build {QT_VERSION_STR}), PyQt {PYQT_VERSION_STR}, Pillow {Image.__version__}",
        flush=True,
    )
    app = Application([])
    # The Windows Calibre database imposes a library-path limit.
    with TemporaryDirectory(prefix="x4-", dir=os.environ.get("RUNNER_TEMP")) as directory:
        root = Path(directory).resolve()
        test_structure(root)
        test_action(root)
    print("Installed ZIP, native settings, Calibre library, selected and full-library batches, cache, simulated USB, structure edits and master preservation passed.", flush=True)
    app.quit()


main()
