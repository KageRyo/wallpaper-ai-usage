"""Copy a Wallpaper Engine Scene and add an original usage panel."""

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import tempfile


PREFIX = "aiusage"
FONT = "fonts/NotoSans-Regular.ttf"
PANEL_MODEL = "models/ai-usage-panel.json"
PROVIDERS = ("codex", "claude")
WINDOWS = ("five", "week")
RESERVED_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.IGNORECASE)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def unpack(package, destination):
    """Extract Wallpaper Engine's PKGV package table into a new directory."""
    with package.open("rb") as source:
        size = package.stat().st_size

        def integer():
            raw = source.read(4)
            if len(raw) != 4:
                raise ValueError("Truncated Scene package")
            return struct.unpack("<I", raw)[0]

        def string():
            length = integer()
            if length > 4096:
                raise ValueError("Invalid Scene package string")
            raw = source.read(length)
            if len(raw) != length:
                raise ValueError("Truncated Scene package string")
            return raw.decode("utf-8")

        version = string()
        if len(version) != 8 or not version.startswith("PKGV") or not version[4:].isdigit():
            raise ValueError("Unsupported Scene package format")
        count = integer()
        if not 0 < count <= min(100_000, size // 13):
            raise ValueError("Invalid Scene package entry count")
        entries = {}
        used_paths = set()
        for _ in range(count):
            name = string()
            target = PurePosixPath(name.replace("\\", "/"))
            if (not target.parts or target.is_absolute()
                    or any(part.endswith((".", " ")) or RESERVED_DEVICE.match(part)
                           or re.search(r'[<>:"|?*\x00-\x1f]', part) for part in target.parts)):
                raise ValueError(f"Invalid path in Scene package: {name}")
            key = target.as_posix().casefold()
            if key in used_paths:
                raise ValueError(f"Duplicate path in Scene package: {name}")
            used_paths.add(key)
            entries[target] = (integer(), integer())

        start = source.tell()
        for target, (offset, length) in entries.items():
            if offset + length > size - start:
                raise ValueError(f"Truncated Scene package entry: {target}")
            path = destination.joinpath(*target.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            source.seek(start + offset)
            with path.open("xb") as output:
                while length:
                    block = source.read(min(length, 1024 * 1024))
                    if not block:
                        raise ValueError(f"Truncated Scene package entry: {target}")
                    output.write(block)
                    length -= len(block)


def countdown_script(key, epoch_key):
    return f"""'use strict';
let nextUpdate = 0;
let rendered;

export function update(value) {{
    const now = Date.now();
    if (now < nextUpdate && rendered !== undefined) return rendered;
    nextUpdate = now + 1000;

    const properties = engine.userProperties || {{}};
    const absolute = properties.{key} || value || '--';
    const cutoff = Number(properties.{epoch_key});
    if (!(cutoff > 0)) return rendered = absolute;

    const minutes = Math.ceil((cutoff * 1000 - now) / 60000);
    if (minutes <= 0) return rendered = 'Reset due';
    const hours = Math.floor(minutes / 60);
    if (hours >= 24) return rendered = Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
    if (hours > 0) return rendered = hours + 'h ' + (minutes % 60) + 'm';
    return rendered = minutes + 'm';
}}
"""


def add_panel(scene, project):
    projection = scene.get("general", {}).get("orthogonalprojection")
    if not projection:
        raise ValueError("Expected a 2D Scene with an orthogonal projection")
    width, height = (float(projection[axis]) for axis in ("width", "height"))
    if not 0 < width <= 16384 or not 0 < height <= 16384:
        raise ValueError("Invalid Scene dimensions")
    if scene["general"].get("perspective"):
        raise ValueError("Expected a 2D Scene, found a perspective scene")

    properties = project.setdefault("general", {}).setdefault("properties", {})
    if any(key.startswith(PREFIX) for key in properties):
        raise ValueError("This Scene already has a usage panel")
    properties[f"{PREFIX}opacity"] = {
        "type": "slider", "text": "Usage panel background opacity",
        "min": 0.0, "max": 1.0, "value": 0.83, "fraction": True, "precision": 2,
        "step": 0.01, "order": 500,
    }
    layers = scene["objects"]
    next_id = max((layer["id"] for layer in layers), default=0) + 1
    factor = min(width / 1920, height / 1080)

    def add_layer(name, right, bottom, **fields):
        nonlocal next_id
        layers.append({
            "id": next_id, "name": name, "origin": f"{width - right * factor:g} {bottom * factor:g} 0",
            "depthtest": "disabled", "castshadow": False, **fields,
        })
        next_id += 1

    add_layer(
        "AI usage panel", 172, 101, image=PANEL_MODEL, size=f"{294 * factor:g} {132 * factor:g}",
        color="0.06 0.04 0.10", alpha={"user": f"{PREFIX}opacity", "value": 0.83},
    )
    muted = "0.83 0.78 0.88"

    def add_text(name, right, bottom, text, color=muted, scale=0.30):
        add_layer(
            name, right, bottom, text=text, font=FONT, padding=2, pointsize=16,
            horizontalalign="center", verticalalign="center", anchor="none",
            scale=f"{scale * factor:g} {scale * factor:g} 1", color=color,
        )

    add_text("AI usage 5-hour heading", 183, 150, "5h used", scale=0.23)
    add_text("AI usage 7-day heading", 88, 150, "7d used", scale=0.23)
    for provider, bottom, label_color in (
        ("codex", 120, "0.88 0.82 1.0"),
        ("claude", 72, "1.0 0.80 0.72"),
    ):
        title = provider.title()
        add_text(f"AI usage {title}", 275, bottom - 8, title, label_color)
        add_text(f"AI usage {title} reset label", 275, bottom - 20, "reset", scale=0.21)
        for window, right in (("five", 183), ("week", 88)):
            key = PREFIX + provider + window
            reset = key + "reset"
            epoch = key + "epoch"
            for suffix, default in (("", "--"), ("reset", "--"), ("epoch", "")):
                properties[key + suffix] = {
                    "type": "textinput", "text": f"{title} {window} {suffix}".strip(),
                    "order": 510, "condition": "false", "value": default,
                }
            add_text(
                f"AI usage {title} {window}", right, bottom,
                {"user": key, "value": "--"}, "1 1 1",
            )
            add_text(
                f"AI usage {title} {window} countdown", right, bottom - 20,
                {"user": reset, "script": countdown_script(reset, epoch), "value": "--"},
                scale=0.22,
            )


def install_scene(source, engine, name):
    source = source.resolve()
    metadata = read_json(source / "project.json")
    if metadata.get("type") != "scene":
        raise ValueError("Select a Scene wallpaper folder")
    entry = PurePosixPath(metadata.get("file", "scene.json"))
    if len(entry.parts) != 1 or not re.fullmatch(r"[\w .-]+\.json", entry.name, re.IGNORECASE):
        raise ValueError("The Scene file must be a JSON file in the project root")
    if entry.name.casefold() == "project.json" or RESERVED_DEVICE.match(entry.name):
        raise ValueError("Invalid Scene file name")
    if not (engine / "assets" / FONT).is_file():
        raise ValueError(f"Wallpaper Engine's built-in font was not found: {FONT}")

    projects = engine / "projects" / "myprojects"
    destination = projects / name
    if source == destination or destination.is_symlink() or destination.exists():
        raise ValueError(f"Project already exists: {destination}")
    # A source containing the destination would cause recursive copying.
    if destination.is_relative_to(source):
        raise ValueError("Select a single wallpaper folder, not an ancestor of myprojects")

    def ignore_generated(folder, names):
        if Path(folder) == source:
            return {item for item in names if item in {".git", "scene.pkg", "project.json", ".remember"}
                    or (Path(folder) / item).is_symlink()}
        return {item for item in names if (Path(folder) / item).is_symlink()}

    with tempfile.TemporaryDirectory(prefix=".ai-usage-", dir=projects) as temp:
        staging = Path(temp) / "project"
        shutil.copytree(source, staging, ignore=ignore_generated)
        if not (staging / entry.name).is_file():
            unpack(source / "scene.pkg", staging)
        scene = read_json(staging / entry.name)
        add_panel(scene, metadata)
        for key in list(metadata):
            if key.casefold() == "workshopid":
                del metadata[key]
        metadata["title"] = f'{metadata.get("title", name)} · AI Usage'
        model = staging / PANEL_MODEL
        if model.exists():
            raise ValueError(f"The Scene already uses the reserved path {PANEL_MODEL}")
        model.parent.mkdir(parents=True, exist_ok=True)
        write_json(model, {
            "instanced": True, "material": "materials/util/solidlayer_instance.json", "solidlayer": True,
        })
        write_json(staging / entry.name, scene)
        write_json(staging / "project.json", metadata)
        staging.rename(destination)
    return destination / entry.name
