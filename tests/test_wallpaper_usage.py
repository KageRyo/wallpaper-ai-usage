from datetime import datetime
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from scene import FONT, PREFIX, SETTINGS, add_panel, install_scene, unpack
from wallpaper_usage import active, claude_windows, display_fields, normalize


NODE = shutil.which("node")
# Runs the panel's layout script against stand-ins for Wallpaper Engine's layers.
LAYOUT_HARNESS = r"""
const [source, names, properties] = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Vec3 { constructor(x, y, z) { Object.assign(this, {x, y, z}); } }
const layers = Object.fromEntries(names.map((name) => [name, {}]));
const script = new Function('engine', 'thisScene', 'thisLayer', 'Vec3',
    source.replace(/export function/g, 'function') + '; return applyUserProperties;');
script({userProperties: properties}, {getLayer: (name) => layers[name]}, layers[names[0]], Vec3)({});
process.stdout.write(JSON.stringify(layers));
"""


def run_layout(scene, properties):
    names = [layer["name"] for layer in scene["objects"]]
    script = scene["objects"][0]["visible"]["script"]
    result = subprocess.run(
        [NODE, "-e", LAYOUT_HARNESS], input=json.dumps([script, names, properties]),
        capture_output=True, text=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def vector(value):
    if isinstance(value, dict):
        return [round(value[axis], 4) for axis in "xyz"]
    return [round(float(part), 4) for part in value.split()]


def package_content(name, data):
    version = b"PKGV0022"
    encoded = name.encode()
    return (
        struct.pack("<I", len(version)) + version + struct.pack("<II", 1, len(encoded))
        + encoded + struct.pack("<II", 0, len(data)) + data
    )


class SceneTests(unittest.TestCase):
    def test_keeps_percentage_bound_even_when_countdown_uses_script(self):
        existing = {"id": 20, "name": "Original clock"}
        scene = {
            "general": {"orthogonalprojection": {"width": 1920, "height": 1080}},
            "objects": [existing],
        }
        project = {}
        add_panel(scene, project)
        self.assertIs(scene["objects"][0], existing)
        ids = [layer["id"] for layer in scene["objects"]]
        self.assertEqual(len(ids), len(set(ids)))
        text = {layer["text"]["user"]: layer["text"] for layer in scene["objects"]
                if isinstance(layer.get("text"), dict)}
        props = project["general"]["properties"]
        for provider in ("codex", "claude"):
            for window in ("five", "week"):
                key = PREFIX + provider + window
                self.assertEqual(text[key], {"user": key, "value": "--"})
                self.assertIn("engine.userProperties", text[key + "reset"]["script"])
                self.assertIn(key + "epoch", props)

    def test_install_copies_packed_scene_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = root / "engine"
            (engine / "projects/myprojects").mkdir(parents=True)
            stock_font = engine / "assets" / FONT
            stock_font.parent.mkdir(parents=True)
            stock_font.touch()
            source = root / "workshop"
            source.mkdir()
            metadata = '{"type":"scene","file":"scene.json","title":"Example","workshopid":"123"}'
            (source / "project.json").write_text(metadata)
            scene = {"general": {"orthogonalprojection": {"width": 3440, "height": 1440}}, "objects": []}
            packed = package_content("scene.json", json.dumps(scene).encode())
            (source / "scene.pkg").write_bytes(packed)
            installed = install_scene(source, engine, "quota-copy")
            new_project = json.loads((installed.parent / "project.json").read_text())
            self.assertEqual(new_project["title"], "Example · AI Usage")
            self.assertNotIn("workshopid", new_project)
            self.assertGreater(len(json.loads(installed.read_text())["objects"]), 0)
            self.assertEqual((source / "project.json").read_text(), metadata)
            self.assertEqual((source / "scene.pkg").read_bytes(), packed)
            with self.assertRaisesRegex(ValueError, "already exists"):
                install_scene(source, engine, "quota-copy")

    def test_unpack_rejects_escape_and_truncated_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / "target"
            target.mkdir()
            package = folder / "scene.pkg"
            for bad_path in ("../other.json", r"..\other.json", "C:/other.json"):
                with self.subTest(bad_path=bad_path):
                    package.write_bytes(package_content(bad_path, b"payload"))
                    with self.assertRaisesRegex(ValueError, "Invalid path"):
                        unpack(package, target)
            package.write_bytes(package_content("missing.json", b"payload")[:-1])
            with self.assertRaisesRegex(ValueError, "Truncated"):
                unpack(package, target)
            self.assertEqual(list(target.iterdir()), [])


@unittest.skipUnless(NODE, "Node.js runs the Scene layout script")
class LayoutScriptTests(unittest.TestCase):
    def setUp(self):
        self.scene = {"general": {"orthogonalprojection": {"width": 3840, "height": 2160}}, "objects": []}
        add_panel(self.scene, {})

    def test_default_settings_match_the_saved_layout(self):
        saved = {layer["name"]: layer for layer in self.scene["objects"]}
        for name, layer in run_layout(self.scene, {}).items():
            if "origin" in layer:
                self.assertEqual(vector(layer["origin"]), vector(saved[name]["origin"]), name)

    def test_moves_panel_and_closes_the_hidden_row(self):
        layers = run_layout(self.scene, {
            PREFIX + "position": "topleft", PREFIX + "showcodex": False, PREFIX + "size": 2,
        })
        # A 4K canvas doubles the 1080p layout. One row leaves a panel 294 by 84.
        self.assertEqual(vector(layers["AI usage panel"]["origin"]), [25 * 2 + 147 * 4, 2160 - 35 * 2 - 42 * 4, 0])
        self.assertEqual(layers["AI usage Codex five"]["alpha"], 0)
        self.assertEqual(layers["AI usage Claude five"]["alpha"], 1)
        self.assertEqual(layers["AI usage Claude five"]["origin"]["y"], 42 - 47)

    def test_colors_usage_from_the_warning_and_alert_levels(self):
        usage = {
            PREFIX + "codexfive": "69%", PREFIX + "codexweek": "70%",
            PREFIX + "claudefive": "90%", PREFIX + "claudeweek": "--",
        }
        text, warning, alert = (vector(SETTINGS[key]["value"]) for key in ("textcolor", "warncolor", "alertcolor"))
        layers = run_layout(self.scene, usage)
        colors = [vector(layers[f"AI usage {name}"]["color"])
                  for name in ("Codex five", "Codex week", "Claude five", "Claude week")]
        self.assertEqual(colors, [text, warning, alert, text])
        layers = run_layout(self.scene, {**usage, PREFIX + "warn": False})
        self.assertEqual(vector(layers["AI usage Claude five"]["color"]), text)


class UsageTests(unittest.TestCase):
    def test_uses_zero_quota_and_clears_expired_quota(self):
        now = int(datetime(2026, 10, 25, 13, 10).timestamp())
        end = int(datetime(2026, 10, 25, 14, 20).timestamp())
        snapshot = normalize(claude_windows({
            "rate_limits": {
                "five_hour": {"used_percentage": 0, "resets_at": end},
                "seven_day": {"used_percentage": 82.6, "resets_at": end - 9000},
            },
            "unrelated": {"value": "ignored"},
        }))
        values = display_fields({"claude": snapshot}, now)
        self.assertEqual(values[PREFIX + "claudefive"], "0%")
        self.assertEqual(values[PREFIX + "claudefivereset"], "14:20")
        self.assertEqual(values[PREFIX + "claudeweek"], "--")
        self.assertEqual(values[PREFIX + "claudeweekreset"], "Reset due")
        self.assertEqual(values[PREFIX + "codexfive"], "--")

    def test_matches_windows_path_and_selected_monitor(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"engine": temp, "scene": r"G:\Steam Library\Scenes\scene.json", "monitor": "Monitor1"}
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"Windows user": {
                "general": {"wallpaperconfig": {"selectedwallpapers": {
                    "Monitor0": {"file": "G:/other/scene.json"},
                    "Monitor1": {"file": "g:/steam library/scenes/scene.json"},
                }}}
            }}))
            self.assertTrue(active(config))
            self.assertFalse(active(dict(config, monitor="Monitor0")))


if __name__ == "__main__":
    unittest.main()
