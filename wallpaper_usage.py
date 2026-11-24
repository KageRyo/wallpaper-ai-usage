"""Install and update a Wallpaper Engine usage panel from WSL."""

import argparse
import base64
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from scene import PREFIX, PROVIDERS, WINDOWS, install_scene, read_json, write_json


DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wallpaper-ai-usage/config.json"
INTERVAL_SECONDS = 12


def windows_path(path):
    return subprocess.check_output(["wslpath", "-w", str(path)], text=True, timeout=5).strip()


def active(config):
    settings = read_json(Path(config["engine"]) / "config.json")
    target = PureWindowsPath(config["scene"])
    for profile in settings.values():
        if not isinstance(profile, dict):
            continue
        selections = profile.get("general", {}).get("wallpaperconfig", {}).get("selectedwallpapers", {})
        current = selections.get(config["monitor"], {}).get("file")
        if current and PureWindowsPath(current) == target:
            return True
    return False


def codex_windows(command):
    with subprocess.Popen(
        [command, "app-server", "--listen", "stdio://"], cwd=Path.home(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    ) as server:
        deadline = time.monotonic() + 8
        buffer = b""

        def send(method, **fields):
            server.stdin.write(json.dumps({"method": method, **fields}).encode() + b"\n")

        def receive(request_id):
            nonlocal buffer
            while time.monotonic() < deadline:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    reply = json.loads(line)
                    if reply.get("id") == request_id:
                        if "result" not in reply:
                            raise ValueError("Codex could not read account rate limits")
                        return reply["result"]
                delay = max(0, deadline - time.monotonic())
                if not select.select([server.stdout], [], [], delay)[0]:
                    break
                block = os.read(server.stdout.fileno(), 65536)
                if not block:
                    break
                buffer += block
            raise TimeoutError("Codex did not return account rate limits")

        try:
            client = {"name": "wallpaper-ai-usage", "version": "0.1.0"}
            send("initialize", id=1, params={"clientInfo": client})
            receive(1)
            send("initialized")
            send("account/rateLimits/read", id=2)
            limits = receive(2)["rateLimits"]
        finally:
            server.terminate()

    return {
        name: {"percent": window.get("usedPercent"), "reset": window.get("resetsAt")}
        for window in (limits.get("primary"), limits.get("secondary"))
        if window and (name := {300: "five", 10080: "week"}.get(window.get("windowDurationMins")))
    }


def claude_windows(payload):
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return {}
    return {
        name: {"percent": window.get("used_percentage"), "reset": window.get("resets_at")}
        for name, key in (("five", "five_hour"), ("week", "seven_day"))
        if isinstance(window := limits.get(key), dict)
    }


def normalize(windows):
    clean = {}
    for name in WINDOWS:
        window = windows.get(name)
        if not isinstance(window, dict):
            continue
        percent = window.get("percent")
        reset = window.get("reset")
        if isinstance(percent, bool) or not isinstance(percent, (float, int)):
            continue
        if not math.isfinite(percent) or percent < 0:
            continue
        if isinstance(reset, (int, float)) and math.isfinite(reset) and 0 < reset < 253402300799:
            reset = int(reset)
        else:
            reset = None
        clean[name] = {"percent": round(percent), "reset": reset}
    return clean


def display_fields(state, now):
    values = {}
    current_date = datetime.fromtimestamp(now).date()
    for provider in PROVIDERS:
        snapshot = state.get(provider, {})
        for window in WINDOWS:
            data = snapshot.get(window, {})
            reset = data.get("reset")
            percent = data.get("percent")
            expired = reset is not None and reset <= now
            key = PREFIX + provider + window
            values[key] = f"{percent}%" if percent is not None and not expired else "--"
            label = "--"
            if expired:
                label = "Reset due"
            elif reset:
                when = datetime.fromtimestamp(reset)
                label = when.strftime("%H:%M" if when.date() == current_date else "%m/%d %H:%M")
            values[key + "reset"] = label
            values[key + "epoch"] = str(reset) if reset else ""
    return values


def apply_properties(config, values):
    # Wallpaper Engine reads RAW delimiters from the Windows command line.
    # WSL's direct argv conversion escapes the embedded JSON quotes.
    def quoted(value):
        return "'" + value.replace("'", "''") + "'"

    data = json.dumps(values, separators=(",", ":"))
    arguments = f'-control applyProperties -location {config["monitor"]} -properties RAW~({data})~END'
    code = f"""
$ProgressPreference = 'SilentlyContinue'
$process = Get-Process -Name wallpaper64,wallpaper32 -ErrorAction SilentlyContinue | Select-Object -First 1
if (!$process) {{ exit 1 }}
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = Join-Path {quoted(config["engineWindows"])} ($process.ProcessName + '.exe')
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.Arguments = {quoted(arguments)}
$run = [System.Diagnostics.Process]::Start($start)
$run.WaitForExit()
exit $run.ExitCode
"""
    encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
    subprocess.run(
        [config["powershell"], "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True,
    )


def refresh(config_file, provider, windows=None, force=False):
    config = read_json(config_file)
    if not active(config):
        return "Apply the copied wallpaper on the configured monitor first"

    state_file = config_file.with_suffix(".state.json")
    with state_file.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = read_json(state_file)
        except (FileNotFoundError, ValueError):
            state = {}

        def save():
            with tempfile.NamedTemporaryFile(dir=state_file.parent, delete=False) as handle:
                temporary = Path(handle.name)
            try:
                write_json(temporary, state)
                temporary.replace(state_file)
            finally:
                temporary.unlink(missing_ok=True)

        now = int(time.time())
        if provider == "codex" and (force or now - state.get("codex_attempt", 0) >= INTERVAL_SECONDS):
            state["codex_attempt"] = now
            save()
            if not config["codex"]:
                raise ValueError("Codex was not on PATH when this panel was installed")
            windows = codex_windows(config["codex"])
        if windows := normalize(windows or {}):
            state[provider] = windows
            save()

        # Reapply a cached reading on each eligible event. Wallpaper Engine can
        # reload a Scene while keeping the same active path.
        last_apply = now - state.get("last_apply", 0)
        values = display_fields(state, now)
        if (force or last_apply >= INTERVAL_SECONDS or values != state.get("last_values")) and active(config):
            apply_properties(config, values)
            state["last_apply"] = now
            state["last_values"] = values
            save()
            return "Wallpaper Engine accepted the latest available usage"
        return "Usage is already up to date"


def install(args):
    destination = args.config
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"A configuration already exists at {destination}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", args.name):
        raise ValueError("Use a project name with lowercase letters, digits and hyphens")
    if not re.fullmatch(r"Monitor[0-9]+", args.monitor):
        raise ValueError("Use a Wallpaper Engine monitor location, such as Monitor0")
    engine = args.engine.resolve()
    if not (engine / "wallpaper64.exe").is_file() and not (engine / "wallpaper32.exe").is_file():
        raise ValueError("Select the Wallpaper Engine installation folder")
    if not (engine / "projects/myprojects").is_dir():
        raise ValueError("Open the Wallpaper Engine editor once to create projects/myprojects")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise ValueError("PowerShell was not found on the WSL PATH")
    engine_windows = windows_path(engine)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = install_scene(args.source, engine, args.name)
    config = {
        "engine": str(engine), "engineWindows": engine_windows, "monitor": args.monitor,
        "scene": str(PureWindowsPath(engine_windows) / path.relative_to(engine).as_posix()),
        "powershell": powershell, "codex": shutil.which("codex"),
    }
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Copied the Scene to {path.parent}")
    print(f"Select the copy on {args.monitor} in Wallpaper Engine.")
    print("Run the hooks command for Codex and Claude Code settings.")


def hook_examples(config, claude_command):
    read_json(config)
    path = shlex.join([sys.executable, str(Path(__file__).resolve()), "--config", str(config)])
    hook = {"type": "command", "command": f"{path} codex", "timeout": 15, "async": True}
    print("Codex hooks.json")
    print(json.dumps({"hooks": {event: [{"hooks": [hook]}] for event in ("SessionStart", "PostModelRequest")}}, indent=2))
    print("\nClaude Code settings.json")
    command = f"{path} statusline"
    if claude_command:
        command += f" --and-run {shlex.quote(claude_command)}"
    print(json.dumps({"statusLine": {"type": "command", "command": command}}, indent=2))


def statusline(args):
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    windows = claude_windows(payload or {})
    if windows:
        command = [sys.executable, str(Path(__file__).resolve()), "--config", str(args.config), "claude"]
        # Pass only the two quota windows to the background worker.
        subprocess.Popen(
            [*command, json.dumps(windows)], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if args.and_run:
        return subprocess.run(args.and_run, input=raw, text=True, shell=True).returncode
    parts = []
    reading = normalize(windows)
    for name, title in (("five", "5h"), ("week", "7d")):
        percent = reading.get(name, {}).get("percent")
        if percent is not None:
            parts.append(f"{title} {percent}%")
    if parts:
        print("Claude | " + " | ".join(parts))
    return 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="copy a wallpaper and add the panel")
    setup.add_argument("source", type=Path)
    setup.add_argument("--engine", type=Path, required=True)
    setup.add_argument("--name", default="wallpaper-ai-usage")
    setup.add_argument("--monitor", default="Monitor0")
    hooks = commands.add_parser("hooks", help="print Codex and Claude Code settings")
    hooks.add_argument("--claude-command", help="keep an existing Claude Code status line command")
    codex = commands.add_parser("codex", help="fetch Codex usage and refresh the panel")
    codex.add_argument("--force", action="store_true")
    claude = commands.add_parser("claude", help=argparse.SUPPRESS)
    claude.add_argument("windows")
    status = commands.add_parser("statusline", help="read Claude Code status line input")
    status.add_argument("--and-run", help="forward input to your existing status line command")
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()

    try:
        if args.command == "install":
            install(args)
        elif args.command == "hooks":
            hook_examples(args.config, args.claude_command)
        elif args.command == "statusline":
            return statusline(args)
        elif args.command == "claude":
            refresh(args.config, "claude", json.loads(args.windows))
        else:
            message = refresh(args.config, "codex", force=args.force)
            if args.force:
                print(message)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if args.command != "codex" or args.force:
            print(f"Wallpaper AI Usage: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
