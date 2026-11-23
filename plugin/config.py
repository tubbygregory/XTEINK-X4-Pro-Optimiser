"""Native Calibre preferences with five tabs and portable JSON presets."""

from dataclasses import asdict
from pathlib import Path

from calibre.gui2 import error_dialog, question_dialog
from qt.core import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QPushButton, QSignalBlocker, QSlider, QSpinBox,
    QTabWidget, Qt, QVBoxLayout, QWidget,
)

from .preferences import PREFS, get_profiles
from .xteink.io_utils import atomic_output
from .xteink.settings import (
    BALANCED, CUSTOM, MAX_PROFILE_BYTES, MAX_PROFILES, PRESETS, Options,
    export_presets, import_presets, profile_name,
)
from .xteink.transfer import TransferOptions


def _checkbox(form, label, tooltip=""):
    widget = QCheckBox(label)
    widget.setToolTip(tooltip)
    form.addRow(widget)
    return widget


def _combo(form, label, choices):
    widget = QComboBox()
    for key, display in choices:
        widget.addItem(display, key)
    form.addRow(label, widget)
    return widget


def _spinbox(form, label, minimum, maximum, suffix=""):
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setSuffix(suffix)
    form.addRow(label, widget)
    return widget


def _description(form, text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setContentsMargins(0, 6, 0, 4)
    form.addRow(label)


def _path_row(owner, form, label, file=False):
    field = QLineEdit()
    browse = QPushButton("Browse…")
    row = QHBoxLayout()
    row.addWidget(field, 1)
    row.addWidget(browse)

    def browse_path():
        if file:
            path, _ = QFileDialog.getOpenFileName(
                owner, label, field.text(), "EPUBCheck (*.jar *.exe);;All files (*)"
            )
        else:
            path = QFileDialog.getExistingDirectory(owner, label, field.text())
        if path:
            field.setText(path)

    browse.clicked.connect(browse_path)
    form.addRow(label, row)
    return field


class ConfigWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.option_fields = {}
        self.pref_fields = {}
        self.profiles = get_profiles()
        self._edited_preset = None
        self.current_profile = PREFS["profile"] if PREFS["profile"] in self.profiles else BALANCED
        self.tabs = QTabWidget(self)
        QVBoxLayout(self).addWidget(self.tabs)

        self._general()
        self._images()
        self._epub()
        self._transfer()
        self._profiles()

        for key, widget in self.pref_fields.items():
            self._set(widget, PREFS[key])
        self._show_options(self.profiles[self.current_profile])
        for widget in self.option_fields.values():
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self._option_changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._option_changed)
            else:
                widget.valueChanged.connect(self._option_changed)
        self._update_enabled()

    def _tab(self, title):
        page = QWidget(self.tabs)
        layout = QFormLayout(page)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.tabs.addTab(page, title)
        return layout

    def _general(self):
        form = self._tab("General")
        self.pref_fields["automatically_optimise"] = _checkbox(
            form, "Automatically optimise EPUBs when sending to XTEINK",
            "Applies to the plugin’s Send to XTEINK (USB) action.",
        )
        self.pref_fields["enable_transfer"] = _checkbox(form, "Enable XTEINK USB transfer actions")
        self.pref_fields["export_dir"] = _path_row(self, form, "Export folder")
        self.pref_fields["temporary"] = _combo(form, "Working files", (
            ("system", "System temporary folder"), ("plugin", "Plugin working folder"),
        ))
        self.pref_fields["cache"] = _checkbox(form, "Reuse optimised copies from the disk cache")
        self.pref_fields["logging"] = _checkbox(form, "Write a diagnostic log")
        _description(form, "Working files are removed after each book. Use Clear optimisation cache in the toolbar menu to remove reusable copies.")

    def _images(self):
        form = self._tab("Images")
        fields = self.option_fields
        fields["retain_images"] = _checkbox(form, "Retain all original images")
        fields["grayscale"] = _checkbox(form, "Convert colour images to grayscale")
        fields["resize"] = _checkbox(form, "Resize oversized images without upscaling")
        fields["max_width"] = _spinbox(form, "Maximum width", 1, 8192, " px")
        fields["max_height"] = _spinbox(form, "Maximum height", 1, 8192, " px")
        slider = fields["jpeg_quality"] = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(1, 100)
        quality = QLabel()
        quality.setMinimumWidth(32)
        slider.valueChanged.connect(lambda value: quality.setText(str(value)))
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(quality)
        form.addRow("JPEG quality", row)
        fields["dither"] = _combo(form, "Dithering", (
            ("off", "Off"), ("floyd-steinberg", "Floyd–Steinberg"), ("ordered", "Ordered"),
        ))
        fields["optimise_cover"] = _checkbox(form, "Optimise identified cover images")
        fields["preserve_transparency"] = _checkbox(
            form, "Preserve transparency", "Turning this off composites eligible transparent images on white."
        )
        fields["preserve_svg"] = _checkbox(
            form, "Preserve original SVG images",
            "Turning this off permits removing XML comments from eligible SVGs. SVGs stay vector images.",
        )

    def _epub(self):
        form = self._tab("EPUB structure")
        fields = self.option_fields
        fields["preserve_styling"] = _checkbox(form, "Preserve original styling and embedded fonts")
        fields["remove_fonts"] = _checkbox(form, "Remove embedded fonts", "Check books that depend on custom glyphs before using this option.")
        fields["remove_unused_css"] = _checkbox(form, "Remove unused CSS with Calibre")
        fields["remove_unused_resources"] = _checkbox(form, "Remove demonstrably unreferenced raster images")
        fields["split_xhtml"] = _checkbox(form, "Split large XHTML chapters at block boundaries")
        fields["split_kib"] = _spinbox(form, "Target chapter size", 32, 4096, " KiB")
        fields["compatibility_fixes"] = _checkbox(
            form, "Enable compatibility fixes", "Normalise the EPUB ZIP and encode progressive JPEGs as baseline."
        )
        fields["validate_epubcheck"] = _checkbox(form, "Run EPUBCheck when available")
        self.pref_fields["epubcheck_path"] = _path_row(self, form, "EPUBCheck path", file=True)
        self.pref_fields["epubcheck_path"].setPlaceholderText("Search PATH if empty")
        _description(form, "Every output receives structural checks. Optional edits are skipped when the publication relies on features those edits could disturb.")

    def _transfer(self):
        form = self._tab("Transfer")
        self.pref_fields["usb_dir"] = _path_row(self, form, "USB book folder")
        self.pref_fields["detect_device"] = _checkbox(form, "Suggest mounted volumes labelled XTEINK or X4 Pro")
        self.pref_fields["structure"] = _combo(form, "Subfolders", (
            ("author", "By first author"), ("series", "By series"), ("flat", "No subfolders"),
        ))
        self.pref_fields["template"] = QLineEdit()
        form.addRow("Filename template", self.pref_fields["template"])
        self.pref_fields["overwrite"] = _combo(form, "If a different file exists", (
            ("keep_both", "Keep both"), ("skip", "Skip existing file"), ("replace", "Replace existing file"),
        ))
        _description(form, "Template fields are {title}, {authors}, {series} and {id}. Confirm the SD card folder before each USB batch.")

    def _profiles(self):
        form = self._tab("Profiles")
        self.profile_picker = QComboBox()
        form.addRow("Active profile", self.profile_picker)
        row = QHBoxLayout()
        for label, action in (
            ("Save as…", self._save_as), ("Rename…", self._rename),
            ("Delete", self._delete),
        ):
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        form.addRow(row)
        row = QHBoxLayout()
        for label, action in (("Import…", self._import), ("Export…", self._export)):
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        form.addRow(row)
        _description(form, "Changing an image or EPUB option creates Custom settings. Save them under a name to reuse or share them. Built-in presets are available at any time.")
        self._fill_picker()
        self.profile_picker.currentTextChanged.connect(self._select_profile)

    @staticmethod
    def _set(widget, value):
        if isinstance(widget, QCheckBox):
            widget.setChecked(value)
        elif isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
        elif isinstance(widget, QLineEdit):
            widget.setText(value)
        else:
            widget.setValue(value)

    @staticmethod
    def _get(widget):
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        return widget.value()

    def _current_options(self):
        return Options(**{key: self._get(widget) for key, widget in self.option_fields.items()})

    def _show_options(self, options):
        for key, value in asdict(options).items():
            widget = self.option_fields[key]
            # The quality label uses the slider's signal. Block just our
            # profile-change handler while loading the new settings.
            self._set(widget, value)

    def _fill_picker(self):
        with QSignalBlocker(self.profile_picker):
            self.profile_picker.clear()
            self.profile_picker.addItems(self.profiles)
            self.profile_picker.setCurrentText(self.current_profile)

    def _select_profile(self, name):
        self.current_profile = name
        self._edited_preset = None
        self._loading = True
        try:
            self._show_options(self.profiles[name])
        finally:
            self._loading = False
        self._update_enabled()

    def _option_changed(self, *_):
        if getattr(self, "_loading", False):
            return
        self.profiles[CUSTOM] = self._current_options()
        if self.current_profile != CUSTOM:
            if self.current_profile not in PRESETS:
                self._edited_preset = self.current_profile
            self.current_profile = CUSTOM
            with QSignalBlocker(self.profile_picker):
                self.profile_picker.setCurrentText(CUSTOM)
        self._update_enabled()

    def _update_enabled(self):
        fields = self.option_fields
        process_images = not fields["retain_images"].isChecked()
        for key in ("grayscale", "resize", "jpeg_quality", "dither", "optimise_cover", "preserve_transparency", "preserve_svg"):
            fields[key].setEnabled(process_images)
        for key in ("max_width", "max_height"):
            fields[key].setEnabled(process_images and fields["resize"].isChecked())
        for key in ("remove_fonts", "remove_unused_css"):
            fields[key].setEnabled(not fields["preserve_styling"].isChecked())
        fields["split_kib"].setEnabled(fields["split_xhtml"].isChecked())
        self.pref_fields["epubcheck_path"].setEnabled(fields["validate_epubcheck"].isChecked())

    def _new_name(self, title, initial=""):
        name, accepted = QInputDialog.getText(self, title, "Preset name", text=initial)
        if not accepted:
            return None
        name = profile_name(name)
        if name.casefold() in {reserved.casefold() for reserved in (*PRESETS, CUSTOM)}:
            raise ValueError("Choose a name different from the built-in presets.")
        return name

    def _save_as(self):
        try:
            initial = self.current_profile if self.current_profile not in (*PRESETS, CUSTOM) else self._edited_preset
            name = self._new_name("Save XTEINK preset", initial or "")
            if name is None:
                return
            existing = next((key for key in self.profiles if key.casefold() == name.casefold()), None)
            if existing is not None and not question_dialog(self, "Replace preset", f"Replace {existing!r} with these settings?"):
                return
            if existing is not None:
                del self.profiles[existing]
            elif len(self.profiles) >= len(PRESETS) + 1 + MAX_PROFILES:
                raise ValueError(f"At most {MAX_PROFILES} named presets can be saved.")
            self.profiles[name] = self._current_options()
            self._edited_preset = None
            self.current_profile = name
            self._fill_picker()
        except ValueError as error:
            error_dialog(self, "Preset could not be saved", str(error), show=True)

    def _rename(self):
        old = self.current_profile
        if old in (*PRESETS, CUSTOM):
            return
        try:
            name = self._new_name("Rename XTEINK preset", old)
            if name is None or name == old:
                return
            if any(other != old and other.casefold() == name.casefold() for other in self.profiles):
                raise ValueError("A preset with that name already exists.")
            self.profiles[name] = self.profiles.pop(old)
            self.current_profile = name
            self._fill_picker()
        except ValueError as error:
            error_dialog(self, "Preset could not be renamed", str(error), show=True)

    def _delete(self):
        name = self.current_profile
        if name in (*PRESETS, CUSTOM):
            return
        if question_dialog(self, "Delete preset", f"Delete {name!r}?"):
            del self.profiles[name]
            self._select_profile(BALANCED)
            self._fill_picker()

    def _import(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Import XTEINK presets", "", "JSON files (*.json)")
        if not filename:
            return
        try:
            with open(filename, "rb") as file:
                incoming = import_presets(file.read(MAX_PROFILE_BYTES + 1))
            existing = {name.casefold(): name for name in self.profiles}
            conflicts = [existing[name.casefold()] for name in incoming if name.casefold() in existing]
            if conflicts and not question_dialog(
                self, "Replace matching presets", f"Replace these saved presets?\n\n{', '.join(conflicts)}"
            ):
                return
            if len(self.profiles) - len(conflicts) + len(incoming) > len(PRESETS) + 1 + MAX_PROFILES:
                raise ValueError(f"At most {MAX_PROFILES} named presets can be saved.")
            for name in conflicts:
                del self.profiles[name]
            self.profiles.update(incoming)
            self._select_profile(next(iter(incoming)))
            self._fill_picker()
        except (OSError, ValueError) as error:
            error_dialog(self, "Preset import failed", str(error), show=True)

    def _export(self):
        name = self.current_profile
        if name in (*PRESETS, CUSTOM):
            name = self._new_export_name(name)
            if name is None:
                return
        filename, _ = QFileDialog.getSaveFileName(self, "Export active XTEINK preset", "", "JSON files (*.json)")
        if not filename:
            return
        path = Path(filename)
        if path.suffix.lower() != ".json":
            path = path.with_name(path.name + ".json")
        try:
            with atomic_output(path) as staging:
                staging.write_bytes(export_presets({name: self._current_options()}))
        except OSError as error:
            error_dialog(self, "Preset export failed", str(error), show=True)

    def _new_export_name(self, name):
        try:
            return self._new_name("Name the exported preset", f"{name} copy")
        except ValueError as error:
            error_dialog(self, "Preset export failed", str(error), show=True)
            return None

    def validate(self):
        try:
            TransferOptions(*(self._get(self.pref_fields[key]) for key in ("template", "structure", "overwrite")))
        except ValueError as error:
            error_dialog(self, "Check the transfer settings", str(error), show=True)
            self.tabs.setCurrentIndex(3)
            return False
        return True

    def save_settings(self):
        with PREFS:
            PREFS["profile"] = self.current_profile
            PREFS["custom"] = asdict(self.profiles[CUSTOM])
            PREFS["presets"] = {name: asdict(opts) for name, opts in self.profiles.items() if name not in (*PRESETS, CUSTOM)}
            for key, widget in self.pref_fields.items():
                PREFS[key] = self._get(widget)
