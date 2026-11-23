"""Validated, portable optimisation options and named profiles."""

from dataclasses import asdict, dataclass, fields
import json
import unicodedata


@dataclass(frozen=True)
class Options:
    retain_images: bool = False
    grayscale: bool = True
    resize: bool = True
    max_width: int = 480
    max_height: int = 800
    jpeg_quality: int = 85
    dither: str = "off"
    optimise_cover: bool = True
    preserve_transparency: bool = True
    preserve_svg: bool = True
    preserve_styling: bool = True
    remove_fonts: bool = False
    remove_unused_css: bool = False
    remove_unused_resources: bool = False
    split_xhtml: bool = False
    split_kib: int = 256
    compatibility_fixes: bool = True
    validate_epubcheck: bool = False

    def __post_init__(self):
        for field in fields(self):
            if type(field.default) is bool and type(getattr(self, field.name)) is not bool:
                raise ValueError(f"{field.name} must be true or false.")
        for name, low, high in (
            ("max_width", 1, 8192), ("max_height", 1, 8192),
            ("jpeg_quality", 1, 100), ("split_kib", 32, 4096),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}.")
        if self.dither not in ("off", "floyd-steinberg", "ordered"):
            raise ValueError("Dithering must be off, floyd-steinberg or ordered.")

    @property
    def needs_calibre(self):
        return self.split_xhtml or self.remove_unused_resources or (
            not self.preserve_styling and (self.remove_fonts or self.remove_unused_css)
        )


BALANCED = "Balanced"
CUSTOM = "Custom"
PRESETS = {
    BALANCED: Options(),
    "Maximum Compression": Options(max_width=384, max_height=640, jpeg_quality=65),
    "Maximum Quality": Options(
        retain_images=True, grayscale=False, resize=False, jpeg_quality=100,
        optimise_cover=False, compatibility_fixes=False,
    ),
}

PROFILE_SCHEMA = 1
MAX_PROFILES = 100
MAX_PROFILE_BYTES = 128 * 1024


def profile_name(value):
    if not isinstance(value, str):
        raise ValueError("A preset name must be text.")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > 64 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Enter a preset name of 1–64 printable characters.")
    return value


def options_from_dict(values):
    if not isinstance(values, dict):
        raise ValueError("Optimisation settings must be a JSON object.")
    unknown = values.keys() - {field.name for field in fields(Options)}
    if unknown:
        raise ValueError(f"Unknown optimisation settings: {', '.join(sorted(unknown))}.")
    return Options(**values)


def import_presets(data):
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError("The preset file is too large.")
    document = json.loads(data)
    if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != PROFILE_SCHEMA:
        raise ValueError("This is not a supported XTEINK preset file.")
    values = document.get("presets")
    if not isinstance(values, dict) or not 1 <= len(values) <= MAX_PROFILES:
        raise ValueError(f"A preset file must contain 1–{MAX_PROFILES} presets.")
    reserved = {name.casefold() for name in (*PRESETS, CUSTOM)}
    result = {}
    for raw_name, settings in values.items():
        name = profile_name(raw_name)
        if name.casefold() in reserved:
            raise ValueError(f"Duplicate or reserved preset name: {name}.")
        reserved.add(name.casefold())
        result[name] = options_from_dict(settings)
    return result


def export_presets(values):
    data = {"schema": PROFILE_SCHEMA, "presets": {name: asdict(opts) for name, opts in values.items()}}
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
