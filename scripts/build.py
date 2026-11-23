"""Build a deterministic, directly installable Calibre plugin ZIP."""

import argparse
import ast
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


def build(directory):
    source = ast.parse((PLUGIN / "xteink" / "__init__.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                   and any(isinstance(name, ast.Name) and name.id == "VERSION" for name in node.targets))
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("XTEINK-X4-Pro-Optimiser-" + ".".join(map(str, version)) + ".zip")
    files = {
        path.relative_to(PLUGIN).as_posix(): path
        for path in PLUGIN.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".png", ".txt")
    }
    files["LICENSE"] = ROOT / "LICENSE"
    with ZipFile(target, "w") as archive:
        for name, path in sorted(files.items()):
            entry = ZipInfo(name)
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    with ZipFile(target) as archive:
        if archive.testzip() is not None or "__init__.py" not in archive.namelist():
            raise RuntimeError("The built plugin ZIP did not pass its archive check.")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    print(build(parser.parse_args().output).resolve())
