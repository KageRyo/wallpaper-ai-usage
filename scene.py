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

# Panel geometry in 1080p pixels, relative to the panel center.
LAYOUT = {
    "width": 294, "padding": 36, "row": 48, "heading": 17, "value": 47, "detail": 20,
    "columns": {"label": -103, "five": -11, "week": 84},
}
FULL_HEIGHT = LAYOUT["padding"] + LAYOUT["row"] * len(PROVIDERS)


def color_setting(text, value, **fields):
    return {"type": "color", "text": text, "value": value, **fields}


def slider(text, low, high, value, step=1, **fields):
    fraction = isinstance(step, float)
    return {
        "type": "slider", "text": text, "min": low, "max": high, "value": value, "step": step,
        "fraction": fraction, "precision": 2 if fraction else 0, **fields,
    }


# Shown in the Wallpaper Engine properties panel, in this order.
SETTINGS = {
    "heading": {"type": "text", "text": "<b>AI Usage panel</b>"},
    "position": {
        "type": "combo", "text": "Position", "value": "bottomright", "options": [
            {"label": "Bottom right", "value": "bottomright"},
            {"label": "Bottom left", "value": "bottomleft"},
            {"label": "Top right", "value": "topright"},
            {"label": "Top left", "value": "topleft"},
        ],
    },
    "marginx": slider("Distance from the side", 0, 1600, 25),
    "marginy": slider("Distance from the top or bottom", 0, 900, 35),
    "size": slider("Panel size", 0.5, 2.5, 1.0, 0.05),
    "showcodex": {"type": "bool", "text": "Show Codex", "value": True},
    "showclaude": {"type": "bool", "text": "Show Claude", "value": True},
    "resetmode": {
        "type": "combo", "text": "Reset display", "value": "countdown", "options": [
            {"label": "Time left", "value": "countdown"},
            {"label": "Reset time", "value": "time"},
        ],
    },
    "color": color_setting("Background color", "0.06 0.04 0.10"),
    "opacity": slider("Background opacity", 0.0, 1.0, 0.83, 0.01),
    "textcolor": color_setting("Text color", "1 1 1"),
    "codexcolor": color_setting("Codex name color", "0.88 0.82 1"),
    "claudecolor": color_setting("Claude name color", "1 0.80 0.72"),
    "warn": {"type": "bool", "text": "Color high usage", "value": True},
    "warnat": slider("Warning from (% used)", 1, 100, 70, condition=f"{PREFIX}warn.value"),
    "warncolor": color_setting("Warning color", "1 0.76 0.30", condition=f"{PREFIX}warn.value"),
    "alertat": slider("Alert from (% used)", 1, 100, 90, condition=f"{PREFIX}warn.value"),
    "alertcolor": color_setting("Alert color", "1 0.36 0.34", condition=f"{PREFIX}warn.value"),
}


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
    if (properties.{PREFIX}resetmode === 'time' || !(cutoff > 0)) return rendered = absolute;

    const minutes = Math.ceil((cutoff * 1000 - now) / 60000);
    if (minutes <= 0) return rendered = 'Reset due';
    const hours = Math.floor(minutes / 60);
    if (hours >= 24) return rendered = Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
    if (hours > 0) return rendered = hours + 'h ' + (minutes % 60) + 'm';
    return rendered = minutes + 'm';
}}
"""


LAYOUT_SCRIPT = """'use strict';
const CONFIG = __CONFIG__;

function setting(name) {
    const value = (engine.userProperties || {})[CONFIG.prefix + name];
    return value === undefined || value === null || value === '' ? CONFIG.defaults[name] : value;
}

function number(name) {
    const value = Number(setting(name));
    return isFinite(value) ? value : Number(CONFIG.defaults[name]);
}

function toColor(value) {
    if (value && typeof value === 'object') return new Vec3(Number(value.x), Number(value.y), Number(value.z));
    const parts = String(value).trim().split(/\\s+/).map(Number);
    return parts.length === 3 && parts.every(isFinite) ? new Vec3(parts[0], parts[1], parts[2]) : undefined;
}

function color(name) {
    return toColor(setting(name)) || toColor(CONFIG.defaults[name]);
}

function place(name, x, y, rgb, alpha) {
    const layer = thisScene.getLayer(name);
    if (!layer) return;
    layer.origin = new Vec3(x, y, 0);
    layer.color = rgb;
    layer.alpha = alpha;
}

function layout() {
    const names = CONFIG.names;
    const background = thisScene.getLayer(names.background);
    if (!background) return;
    const canvas = CONFIG.canvas;
    const box = CONFIG.layout;
    const rows = CONFIG.providers.filter((provider) => setting('show' + provider) !== false);
    const scale = Math.max(0.1, number('size')) * canvas.factor;
    const height = box.padding + box.row * rows.length;
    const position = String(setting('position'));
    const halfWidth = box.width / 2 * scale;
    const halfHeight = height / 2 * scale;
    const marginX = Math.max(0, number('marginx')) * canvas.factor;
    const marginY = Math.max(0, number('marginy')) * canvas.factor;
    thisLayer.origin = new Vec3(
        position.endsWith('left') ? marginX + halfWidth : canvas.width - marginX - halfWidth,
        position.startsWith('top') ? canvas.height - marginY - halfHeight : marginY + halfHeight,
        0
    );
    thisLayer.scale = new Vec3(scale, scale, 1);

    const shown = rows.length ? 1 : 0;
    background.scale = new Vec3(1, height / CONFIG.fullHeight, 1);
    background.color = color('color');
    background.alpha = shown * Math.min(1, Math.max(0, number('opacity')));

    const text = color('textcolor');
    const top = height / 2;
    const columns = box.columns;
    for (const window of CONFIG.windows) {
        place(names.headings[window], columns[window], top - box.heading, text, shown * 0.75);
    }

    const highlight = setting('warn') !== false;
    const levels = [[number('alertat'), color('alertcolor')], [number('warnat'), color('warncolor')]];
    const usageColor = (used) => {
        const level = highlight && levels.find(([limit]) => used >= limit);
        return level ? level[1] : text;
    };
    for (const provider of CONFIG.providers) {
        const row = rows.indexOf(provider);
        const visible = row < 0 ? 0 : 1;
        const valueY = top - box.value - box.row * Math.max(row, 0);
        const detailY = valueY - box.detail;
        const group = names.providers[provider];
        place(group.title, columns.label, valueY, color(provider + 'color'), visible);
        place(group.reset, columns.label, detailY, text, visible * 0.7);
        for (const window of CONFIG.windows) {
            const used = parseFloat((engine.userProperties || {})[CONFIG.prefix + provider + window]);
            place(group[window], columns[window], valueY, usageColor(used), visible);
            place(group[window + 'countdown'], columns[window], detailY, text, visible * 0.8);
        }
    }
}

export function init(value) {
    layout();
    return value;
}

export function applyUserProperties() {
    layout();
}
"""


def layer_names():
    names = {
        "root": "AI usage panel", "background": "AI usage background",
        "headings": {"five": "AI usage 5-hour heading", "week": "AI usage 7-day heading"}, "providers": {},
    }
    for provider in PROVIDERS:
        title = provider.title()
        group = {"title": f"AI usage {title}", "reset": f"AI usage {title} reset label"}
        for window in WINDOWS:
            group[window] = f"AI usage {title} {window}"
            group[window + "countdown"] = f"AI usage {title} {window} countdown"
        names["providers"][provider] = group
    return names


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
    for order, (key, setting) in enumerate(SETTINGS.items(), start=500):
        properties[PREFIX + key] = {**setting, "order": order}
    defaults = {key: setting["value"] for key, setting in SETTINGS.items() if "value" in setting}

    layers = scene["objects"]
    next_id = max((layer["id"] for layer in layers), default=0) + 1
    factor = min(width / 1920, height / 1080)
    names = layer_names()
    config = {
        "prefix": PREFIX, "canvas": {"width": width, "height": height, "factor": factor},
        "layout": LAYOUT, "fullHeight": FULL_HEIGHT, "providers": PROVIDERS, "windows": WINDOWS,
        "names": names, "defaults": defaults,
    }

    def add_layer(name, **fields):
        nonlocal next_id
        layers.append({"id": next_id, "name": name, **fields})
        next_id += 1
        return next_id - 1

    # The script repeats this placement when settings change.
    root = add_layer(
        names["root"],
        origin=f"{width - (defaults['marginx'] + LAYOUT['width'] / 2) * factor:g} "
               f"{(defaults['marginy'] + FULL_HEIGHT / 2) * factor:g} 0",
        scale=f"{factor:g} {factor:g} 1",
        visible={"script": LAYOUT_SCRIPT.replace("__CONFIG__", json.dumps(config)), "value": True},
    )
    child = {"parent": root, "depthtest": "disabled", "castshadow": False}
    add_layer(
        names["background"], image=PANEL_MODEL, origin="0 0 0", size=f"{LAYOUT['width']} {FULL_HEIGHT}",
        color=defaults["color"], alpha=defaults["opacity"], **child,
    )

    def add_text(name, column, y, text, color, alpha=1.0, scale=0.30):
        add_layer(
            name, origin=f"{LAYOUT['columns'][column]:g} {y:g} 0", text=text, font=FONT, padding=2,
            pointsize=16, horizontalalign="center", verticalalign="center", anchor="none",
            scale=f"{scale:g} {scale:g} 1", color=color, alpha=alpha, **child,
        )

    top = FULL_HEIGHT / 2
    text_color = defaults["textcolor"]
    for window, label in (("five", "5h used"), ("week", "7d used")):
        add_text(names["headings"][window], window, top - LAYOUT["heading"], label, text_color, 0.75, 0.23)
    for row, provider in enumerate(PROVIDERS):
        title = provider.title()
        group = names["providers"][provider]
        value_y = top - LAYOUT["value"] - LAYOUT["row"] * row
        detail_y = value_y - LAYOUT["detail"]
        add_text(group["title"], "label", value_y, title, defaults[provider + "color"])
        add_text(group["reset"], "label", detail_y, "reset", text_color, 0.7, 0.21)
        for window in WINDOWS:
            key = PREFIX + provider + window
            reset = key + "reset"
            epoch = key + "epoch"
            for suffix, default in (("", "--"), ("reset", "--"), ("epoch", "")):
                properties[key + suffix] = {
                    "type": "textinput", "text": f"{title} {window} {suffix}".strip(),
                    "order": 600, "condition": "false", "value": default,
                }
            add_text(group[window], window, value_y, {"user": key, "value": "--"}, text_color)
            add_text(
                group[window + "countdown"], window, detail_y,
                {"user": reset, "script": countdown_script(reset, epoch), "value": "--"},
                text_color, 0.8, 0.22,
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
