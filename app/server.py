"""Factorio "game master" MCP server (Streamable HTTP).

Talks to the game over RCON and to the `factorio` container (only that one) over the
Docker API. Auth is handled in front of it by a reverse proxy or tunnel with real auth
(e.g. Cloudflare Access); this app has none.
"""
import asyncio
import functools
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

import presets
from docker_api import DockerApi
from rcon import Rcon, RconError

# ---------------------------------------------------------------- config
DATA = Path(os.environ.get("FACTORIO_DATA", "/factorio"))
STATE = Path(os.environ.get("STATE_DIR", "/state"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/logs"))
PUBLIC_HOSTS = [h.strip() for h in os.environ.get("PUBLIC_HOSTS", "").split(",") if h.strip()]
PINNED_IMAGE = "factoriotools/factorio:2.0.77"
GUIDE = Path(__file__).with_name("game_guide.md")
ACH_MARKER = STATE / "achievements-disabled.json"
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,60}$")

rcon = Rcon(os.environ.get("RCON_HOST", "factorio"), int(os.environ.get("RCON_PORT", "27015")),
            os.environ.get("RCON_PW_FILE", "/run/secrets/rconpw"))
docker = DockerApi(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"))

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)
calllog = logging.getLogger("toolcalls")
calllog.setLevel(logging.INFO)
_h = RotatingFileHandler(LOG_DIR / "tool-calls.log", maxBytes=1_000_000, backupCount=5)
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
calllog.addHandler(_h)
calllog.addHandler(logging.StreamHandler())
logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = (
    "Game master for a family Factorio 2.0.77 LAN server (see help('guide') for the players and house "
    "rules). Keep spoken answers short. Keep everything said in game kid-friendly. Never upgrade the "
    "server, enable mods or Space Age (a player is on Nintendo Switch and can't join otherwise). "
    "Lua permanently disables achievements: the first run_lua/run_preset needs the human's spoken OK. "
    "Call help() to see what's possible; help('guide') for the full guide."
)

allowed_hosts = PUBLIC_HOSTS + ["127.0.0.1:*", "localhost:*", "factorio-mcp:*"]
mcp = FastMCP(
    "factorio-gm",
    instructions=INSTRUCTIONS,
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"https://{h}" for h in PUBLIC_HOSTS]
        + ["https://claude.ai", "https://chatgpt.com", "https://chat.openai.com",
           "http://127.0.0.1:*", "http://localhost:*"],
    ),
)


# ---------------------------------------------------------------- helpers
def _short(v, n=300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = s.replace("\n", " | ")
    return s if len(s) <= n else s[:n] + "…"


def tool(fn):
    """Register as an MCP tool and log every call (time, tool, args, short result)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            status = "ok"
        except RconError as e:
            result, status = f"Error: {e}", "error"
        except Exception as e:  # report, don't crash the session
            result, status = f"Error: {type(e).__name__}: {e}", "error"
        calllog.info("%s %s args=%s %.1fs -> %s", fn.__name__, status, _short(kwargs, 500),
                     time.monotonic() - t0, _short(result))
        return result
    return mcp.tool()(wrapper)


async def _online() -> list[str]:
    out = await rcon.command("/players online")
    return [ln.strip().removesuffix(" (online)") for ln in out.splitlines()[1:] if ln.strip()]


def _ach_state() -> dict | None:
    try:
        return json.loads(ACH_MARKER.read_text())
    except (FileNotFoundError, ValueError):
        return None


async def _lua_allowed(confirm: bool) -> str | None:
    """Return None if Lua may run now, else a refusal message. Handles the first-time confirmation."""
    seed = (await rcon.command("/seed")).strip()
    st = _ach_state()
    if st and st.get("seed") == seed:
        return None
    if not confirm:
        return ("Not run. House rule: the first Lua command permanently disables achievements on this map. "
                "Ask the human out loud: 'This will turn off achievements on this map forever. OK?' "
                "If they say yes, call again with confirm_disable_achievements=true.")
    # Factorio refuses the first Lua command with a warning and runs it on repeat.
    # Arm it with a harmless no-op so the real code never runs twice.
    for _ in range(3):
        if "gm-lua-ok" in await rcon.command('/sc rcon.print("gm-lua-ok")'):
            ACH_MARKER.write_text(json.dumps({"seed": seed, "at": datetime.now(timezone.utc).isoformat()}))
            return None
    return "Couldn't enable Lua commands (the server didn't accept /sc). Check recent_log."


async def _lua(code: str) -> str:
    out = await rcon.command("/sc " + code.strip())
    return out or "Done (no output; use rcon.print(...) to return values)."


def _parse_since(since: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", since.lower())
    if m:
        return int(time.time()) - int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]
    return int(datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp())


EVENT_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[")
ENGINE_PREFIX = re.compile(r"^\s*\d+\.\d+ (Info|Warning|Error) [\w.]+:\d+: ")
ENGINE_KEEP = re.compile(r"Saving finished|Auto saving|Loading map|Error|Quitting|Hosting game", re.I)

CONFIG_FILES = {
    "server-settings": ("config/server-settings.json",
                        "Restart needed: server-settings is read at server start. Many options can be changed "
                        "live with run_command('/config set <option> <value>')."),
    "map-gen-settings": ("config/map-gen-settings.json",
                         "Only used when a NEW map is generated; the current map is unchanged."),
    "map-settings": ("config/map-settings.json",
                     "Only used when a NEW map is generated; the current map is unchanged."),
    "server-adminlist": ("config/server-adminlist.json",
                         "Applied at restart. To change admins live use /promote or /demote instead."),
}
READ_ONLY_FILES = {"mod-list": "mods/mod-list.json"}
SECRET_KEYS = {"password", "token", "game_password"}


def _cfg_key(file: str) -> str:
    return file.strip().removeprefix("config/").removeprefix("mods/").removesuffix(".json")


def _merge(base, patch):
    """RFC 7396 JSON merge patch."""
    if not isinstance(patch, dict):
        return patch
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge(out.get(k), v)
    return out


def _mask(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_KEYS and v else _mask(v)) for k, v in obj.items()}
    return obj


def _saves() -> list[Path]:
    return sorted((DATA / "saves").glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)


def _fmt_save(p: Path) -> str:
    ts = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{p.name} ({p.stat().st_size / 1e6:.1f} MB, {ts})"


# ---------------------------------------------------------------- discovery
CONSOLE_COMMANDS = (
    "admins alerts ban banlist bans clear color config delete-blueprint-library demote evolution help ignore "
    "ignores kick large-blueprint-size mute mute-programmable-speaker mutes players promote purge reply "
    "reset-tips save seed server-save shout swap-players time unban unignore unlock-shortcut-bar unlock-tips "
    "unmute version whisper whitelist (Lua: command/c, silent-command/sc, measured-command/mc via run_lua)"
)


@tool
async def help(topic: str = "") -> str:
    """What you can do. No topic: overview of tools, presets and console commands.
    topic='<command>' (e.g. 'whitelist'): that console command's usage, from the server.
    topic='guide': the full game master guide with house rules and a Lua cookbook."""
    t = topic.strip().lstrip("/")
    if t.lower() in ("guide", "game_guide"):
        return GUIDE.read_text()
    if not t:
        return ("Tools: players_online, server_status, say, whisper, recent_log, run_command, run_lua, "
                "run_preset, list_presets, save_game, list_saves, read_config, update_config, restart_server.\n"
                f"Presets: {', '.join(presets.PRESETS)}.\n"
                f"Console commands (2.0.77): {CONSOLE_COMMANDS}. Use help('<command>') for usage.\n"
                "Rules: kid-friendly chat; never upgrade or add mods/Space Age; first Lua needs the human's OK "
                "(disables achievements). help('guide') has the full guide.")
    if not NAME_RE.match(t):
        return "Give a single command name, like help('kick')."
    out = await rcon.command(f"/help {t}")
    lines = out.splitlines()
    # RCON prefixes every /help answer with the 3-line server-console command list.
    if lines and lines[0].startswith("- Server console commands"):
        lines = lines[3:]
    return "\n".join(lines).strip() or out


@mcp.resource("guide://factorio/game_guide", name="game_guide", mime_type="text/markdown",
              description="Factorio game master guide: players, rules, tools, commands, Lua cookbook.")
def game_guide_resource() -> str:
    return GUIDE.read_text()


@mcp.prompt(name="game_guide", description="Load the Factorio game master guide as context.")
def game_guide_prompt() -> str:
    return GUIDE.read_text()


# ---------------------------------------------------------------- chat & console
@tool
async def players_online() -> str:
    """Who is playing right now."""
    names = await _online()
    return f"{len(names)} online: {', '.join(names)}" if names else "Nobody is online."


@tool
async def say(message: str) -> str:
    """Send a chat message to everyone in game, shown as [GM]. Keep it kid-friendly."""
    msg = " ".join(message.split())
    if not msg:
        return "Empty message, nothing sent."
    await rcon.command(f"[GM] {msg}")
    return f"Said: {msg}"


@tool
async def whisper(player: str, message: str) -> str:
    """Send a private message to one player. Keep it kid-friendly."""
    if not NAME_RE.match(player):
        return "Invalid player name."
    msg = " ".join(message.split())
    out = await rcon.command(f"/whisper {player} [GM] {msg}")
    return out or f"Whispered to {player}: {msg}"


LUA_CMDS = {"c", "command", "sc", "silent-command", "mc", "measured-command"}
BLOCKED_CMDS = {
    "quit": "Use restart_server instead; /quit would stop the server.",
    "toggle-heavy-mode": "Refused: heavy mode makes multiplayer unplayable.",
    "cheat": "Needs an in-game player and disables achievements; use run_lua instead (asks first).",
    "editor": "Needs an in-game player and disables achievements; use run_lua instead (asks first).",
}


@tool
async def run_command(command: str) -> str:
    """Run any Factorio console command (e.g. '/players', '/kick name reason', '/config get max-players')
    and return the output. Text without a slash is sent as chat from <server>. Lua goes through run_lua."""
    cmd = command.strip()
    if not cmd:
        return "Empty command."
    if cmd.startswith("/"):
        name = cmd[1:].split(maxsplit=1)[0].lower() if len(cmd) > 1 else ""
        if name in LUA_CMDS:
            return "Lua commands must go through run_lua (it enforces the achievements rule)."
        if name in BLOCKED_CMDS:
            return BLOCKED_CMDS[name]
    out = await rcon.command(cmd)
    return out or "Done (no output)."


@tool
async def run_lua(code: str, confirm_disable_achievements: bool = False) -> str:
    """Run Lua on the server (/sc). Return values with rcon.print(...). 2.0 API.
    The FIRST Lua ever on this map permanently disables achievements: you'll be refused until the human
    says yes out loud, then pass confirm_disable_achievements=true. After that it just runs."""
    if not code.strip():
        return "No code given."
    refusal = await _lua_allowed(confirm_disable_achievements)
    return refusal or await _lua(code)


@tool
async def run_preset(name: str, player: str = "", target: str = "", item: str = "",
                     count: int = 0, value: float = 0, confirm_disable_achievements: bool = False) -> str:
    """Run a ready-made action. Names: always_day_on, always_day_off, speed_boost(player, value=multiplier),
    speed_reset(player), give_items(player, item, count), reveal_map(player, value=radius),
    teleport_to_player(player, target), player_inventory(player), player_info(player), production_stats,
    research_status. Uses Lua, so the achievements rule of run_lua applies."""
    try:
        code = presets.build(name, player or None, target or None, item or None,
                             count or None, value or None)
    except ValueError as e:
        return str(e)
    refusal = await _lua_allowed(confirm_disable_achievements)
    return refusal or await _lua(code)


@tool
async def list_presets() -> str:
    """List ready-made actions for run_preset, with their parameters."""
    return presets.describe()


# ---------------------------------------------------------------- server & container
@tool
async def server_status() -> str:
    """Server health: game version, players online, map age, container state, uptime, latest save."""
    parts = []
    try:
        st = await docker.state()
        parts.append(f"Container {st['status']} (health {st['health']}), up {st['uptime']}, "
                     f"restarts {st['restart_count']}, image {st['image']}")
        if st["image"] != PINNED_IMAGE:
            parts.append(f"WARNING: image is not the pinned {PINNED_IMAGE}!")
    except Exception as e:
        parts.append(f"Container state unavailable: {e}")
    try:
        names = await _online()
        parts.append(f"Factorio {await rcon.command('/version')}, map age {await rcon.command('/time') or 'just started'}")
        parts.append(f"Players online: {', '.join(names) if names else 'none'}")
    except RconError as e:
        parts.append(f"Game not answering: {e}")
    saves = _saves()
    if saves:
        parts.append(f"Latest save: {_fmt_save(saves[0])}")
    try:
        st = _ach_state()
        off = bool(st) and st.get("seed") == (await rcon.command("/seed")).strip()
        parts.append("Achievements: " + ("disabled (Lua has been used)" if off else "still enabled (no Lua used via GM)"))
    except RconError:
        pass
    return "\n".join(parts)


@tool
async def recent_log(lines: int = 40, since: str = "", everything: bool = False) -> str:
    """What happened in game: joins, leaves, chat, commands, saves. since: '30m', '1h', '1d' or ISO time.
    Times are UTC. everything=true includes raw engine lines too."""
    lines = max(1, min(500, lines))
    ts = _parse_since(since) if since else None
    # Filtering drops most raw lines, so look further back (or at everything since `since`).
    raw = await docker.logs(tail=lines if everything and not ts else (100_000 if ts else 5000), since=ts)
    out = []
    for ln in raw:
        ts_part, _, msg = ln.partition(" ")
        if "New RCON connection" in msg or "RCON received an invalid" in msg:
            continue
        if EVENT_RE.match(msg):
            out.append(msg)  # console line, already has a date
        elif everything or ENGINE_KEEP.search(msg):
            msg = ENGINE_PREFIX.sub("", msg)
            out.append(f"{ts_part[:10]} {ts_part[11:19]} {msg}")
    out = out[-lines:]
    return "\n".join(out) if out else "Nothing logged in that period."


@tool
async def save_game(name: str = "") -> str:
    """Save the game now. With a name, also write a named copy saves/<name>.zip."""
    out = await rcon.command("/server-save")
    msg = [out or "Saved."]
    if name:
        if not NAME_RE.match(name):
            return "Saved, but the name may only use letters, digits, - _ and ."
        await asyncio.sleep(5)  # /server-save runs async; a second save meanwhile fails
        out2 = await rcon.command(f"/save {name}")
        msg.append(out2 or f"Named copy saved as {name}.zip")
    await asyncio.sleep(2)
    saves = _saves()
    if saves:
        msg.append(f"Latest file: {_fmt_save(saves[0])}")
    return "\n".join(msg)


@tool
async def list_saves() -> str:
    """List save files, newest first."""
    saves = _saves()
    return "\n".join(_fmt_save(p) for p in saves) if saves else "No saves found."


@tool
async def read_config(file: str) -> str:
    """Read a config file: server-settings, map-gen-settings, map-settings, server-adminlist,
    or mod-list (read-only). Secrets are masked."""
    key = _cfg_key(file)
    rel = CONFIG_FILES[key][0] if key in CONFIG_FILES else READ_ONLY_FILES.get(key)
    if not rel:
        return f"Unknown file. Choose from: {', '.join([*CONFIG_FILES, *READ_ONLY_FILES])}"
    return json.dumps(_mask(json.loads((DATA / rel).read_text())), indent=1)


@tool
async def update_config(file: str, json_patch: str) -> str:
    """Change a config file with a JSON merge patch, e.g. file='server-settings',
    json_patch='{"autosave_interval": 5}'. For server-adminlist give the full list. Keeps a timestamped
    backup and says if a restart is needed. Ask the human before changing anything."""
    key = _cfg_key(file)
    if key in READ_ONLY_FILES:
        return "Refused: mods must stay vanilla (only 'base'); the Switch can't join with mods or Space Age."
    if key not in CONFIG_FILES:
        return f"Unknown file. Choose from: {', '.join(CONFIG_FILES)}"
    try:
        patch = json.loads(json_patch) if isinstance(json_patch, str) else json_patch
    except ValueError as e:
        return f"Invalid JSON patch: {e}"
    path = DATA / CONFIG_FILES[key][0]
    current = json.loads(path.read_text())
    if key == "server-adminlist":
        if not (isinstance(patch, list) and all(isinstance(n, str) and NAME_RE.match(n) for n in patch)):
            return 'server-adminlist needs the full list of player names, e.g. ["your-player-name"].'
        new = patch
    else:
        if not isinstance(patch, dict):
            return "json_patch must be a JSON object."
        new = _merge(current, patch)
    if new == current:
        return "No change."
    text = json.dumps(new, indent=2) + "\n"
    json.loads(text)  # validate round-trip before touching the file
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return f"Updated {path.name} (backup {backup.name}). {CONFIG_FILES[key][1]}"


@tool
async def restart_server(force: bool = False) -> str:
    """Save and restart the Factorio server, then wait until it answers again (about a minute).
    Refuses if anyone is online (and warns them in chat) unless force=true; ask the human first.
    Never upgrades: the 2.0.77 version pin stays."""
    names = await _online()
    if names and not force:
        await rcon.command("[GM] Heads up! The server needs a quick restart soon. Find a safe spot!")
        return (f"Not restarted: {', '.join(names)} online. I warned them in chat. "
                "Ask the human, then call again with force=true.")
    if names:
        await rcon.command("[GM] The server restarts in 15 seconds. Be right back!")
        await asyncio.sleep(15)
    save = await rcon.command("/server-save")
    await asyncio.sleep(3)
    t0 = time.monotonic()
    await rcon.close()
    await docker.restart(stop_timeout=60)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        if await rcon.ping():
            st = await docker.state()
            return (f"Restarted and back after {int(time.monotonic() - t0)}s. "
                    f"Save before restart: {save or 'ok'}. Container {st['status']}, {st['image']}.")
    return "Restart sent, but the game isn't answering RCON after 4 minutes. Check server_status / recent_log."


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _serve_root_too(app):
    """Also serve MCP at `/`: clients may use the bare hostname (Access advertises it as a resource)."""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/":
            scope = dict(scope, path="/mcp", raw_path=b"/mcp")
        await app(scope, receive, send)
    return wrapped


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(_serve_root_too(mcp.streamable_http_app()), host=mcp.settings.host,
                port=mcp.settings.port, log_level="info")
