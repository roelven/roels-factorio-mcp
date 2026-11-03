# Factorio Game Master — an MCP server for your Factorio server

An MCP server that turns Claude or ChatGPT into the **game master** of a Factorio server:
it relays chat, runs admin commands, applies kid-friendly presets and keeps the server
healthy — from a chat window or by voice ("who's online?", "tell the kids dinner is
ready", "give me 50 iron plates", "save and restart the server").

Built for a family LAN server where one player is on a Nintendo Switch, so it bakes in a
few strong opinions (all removable, see [Customizing](#customizing)):

- **Vanilla only** — mods and Space Age are refused; the mod list is read-only.
- **Version pinned** (2.0.77, matching the Switch); restarts never upgrade the image.
- **Achievements are protected** — Lua permanently disables achievements on a map, so
  every Lua call is gated behind a spoken human confirmation.
- **Restarts are polite** — refused while anyone is online; players get a warning in
  chat first.
- **Kid-friendly** — the system prompt and guide steer the agent's chat to be warm and
  simple.

## How it works

Clients connect over Streamable HTTP (`/mcp`, also served at `/`):

```
claude.ai / Claude app / ChatGPT
        │  MCP over Streamable HTTPS (behind your auth, e.g. Cloudflare Access)
        ▼
factorio-mcp (FastMCP, stateless, JSON responses)
        ├── RCON ─────────► Factorio server container (game console)
        └── Docker API ──► inspect / logs / restart, for that one container only
```

The server is stateless and JSON-only, so it works well with hosted clients (claude.ai
custom connectors, the Claude app, ChatGPT connectors). Voice mode works because every
tool returns short, speakable text.

## Tools

| Tool | What it does |
|---|---|
| `help(topic)` | Overview; a console command's usage; `help('guide')` = the full guide |
| `players_online()` | Who is playing right now |
| `server_status()` | Version, players, map age, container state, uptime, latest save, achievements state |
| `say(message)` | Chat to everyone, shown as `[GM]` |
| `whisper(player, message)` | Private message to one player |
| `recent_log(lines, since, everything)` | Joins, leaves, chat, saves, commands — filtered or raw |
| `run_command(command)` | Any console command (`/kick`, `/config set ...`); Lua is refused here |
| `run_lua(code, confirm_disable_achievements)` | Run `/sc <code>` behind the achievements gate |
| `run_preset(name, ...)` | Ready-made Lua actions (always day, speed boost, give items, ...) |
| `list_presets()` | The presets and their parameters |
| `save_game(name)` | `/server-save`, optionally plus a named copy |
| `list_saves()` | Save files, newest first |
| `read_config(file)` | server-settings, map-gen/map-settings, adminlist, mod-list (secrets masked) |
| `update_config(file, json_patch)` | JSON-merge-patch a config file, with a timestamped backup |
| `restart_server(force)` | Save, restart, wait for RCON; refuses while players are online |

Plus the `game_guide` resource and prompt (the agent's manual: players, house rules,
console commands, a Lua cookbook) and a `GET /healthz` endpoint for container healthchecks.

## Safety rails

- **Achievements gate.** `run_lua`/`run_preset` refuse until called with
  `confirm_disable_achievements=true`. The confirmation is recorded per map seed
  (`state/achievements-disabled.json`), so a new map re-arms the gate. On first confirm
  it sends a no-op `/sc` first to absorb Factorio's "repeat the command to proceed"
  warning, so the real code never runs twice.
- **Refused commands.** `run_command` blocks `/c`, `/sc`, `/mc` (use `run_lua`), plus
  `/quit`, `/cheat`, `/editor` and `/toggle-heavy-mode`.
- **Vanilla pin.** `update_config` refuses `mod-list`; the server never upgrades the image.
- **Docker access is one-way.** `app/docker_api.py` allows exactly three operations
  (inspect, logs, restart) on exactly one hard-coded container name. No list, start,
  stop, exec or pull — even if the agent asks.
- **Restart gate.** Refuses while players are online (warns them in chat first) unless
  `force=true`; saves before restarting; waits for RCON to come back.
- **Config hygiene.** Every `update_config` keeps a `file.bak-YYYYmmdd-HHMMSS` backup;
  secrets are masked in `read_config`; writes are atomic (tmp file + rename) and
  validated before touching the original.
- **Transport hardening.** Stateless JSON-over-HTTP with DNS-rebinding protection; only
  `PUBLIC_HOSTS`, localhost, and the container hostname are accepted. The app has no
  auth of its own — put a tunnel or reverse proxy with real auth (e.g. Cloudflare
  Access) in front of it.
- **Audit trail.** Every tool call (time, tool, args, short result) is appended to
  `logs/tool-calls.log` (rotated at 1 MB × 5).

## Deploying

You need:

- a Factorio server container (e.g.
  [`factoriotools/factorio`](https://hub.docker.com/r/factoriotools/factorio)) with RCON
  enabled and its `config/rconpw` file;
- its Docker network name (default compose project `factorio` → `factorio_default`);
- Docker with the compose plugin.

Steps:

1. Clone and edit `docker-compose.yml`:
   - `PUBLIC_HOSTS`: the public hostname clients will use (or leave empty for
     localhost-only);
   - the two `/srv/factorio` volume paths → your Factorio server's data directory;
   - the Docker socket path (rootless Docker: `/run/user/<uid>/docker.sock`);
   - the external network name.
2. Build and start: `docker compose up -d --build`
3. Check: `curl http://127.0.0.1:8102/healthz` → `ok`
4. Expose it through a tunnel or reverse proxy **with authentication** (e.g. cloudflared +
   Cloudflare Access). Never publish it as-is.
5. Add it to your client as a custom connector at `https://<your-host>/mcp` and just talk
   to it: "who's online?", "what can you do?", "make it always day".

Tested with Factorio 2.0.77 and `mcp` 1.30.0 (FastMCP; the 2.x line renamed it).

## Configuration

All via environment variables (defaults work with the compose file):

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Listen port inside the container |
| `PUBLIC_HOSTS` | *(empty)* | Comma-separated public hostnames for rebinding protection + CORS |
| `RCON_HOST` / `RCON_PORT` | `factorio` / `27015` | Where the game listens for RCON |
| `RCON_PW_FILE` | `/run/secrets/rconpw` | File containing the RCON password |
| `FACTORIO_DATA` | `/factorio` | The server's data dir (saves, config, mods) |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Docker socket for status/logs/restart |
| `STATE_DIR` | `/state` | The achievements-gate marker lives here |
| `LOG_DIR` | `/logs` | The tool-call audit log lives here |

## Customizing

- **`app/game_guide.md`** — the guide the agent reads (`help('guide')`, the `game_guide`
  resource and prompt). Describe your players and house rules here; shipped with example
  players `alex` and `sam`.
- **`app/presets.py`** — the ready-made Lua actions for `run_preset`; add entries to the
  `PRESETS` dict.
- **`app/server.py`** — `INSTRUCTIONS` (the agent's system prompt) and `PINNED_IMAGE`
  (change the version pin if you're not matching a Switch).

## Layout

```
├── Dockerfile              # python:3.13-slim; the app idles at ~45 MB RAM
├── docker-compose.yml      # example deployment next to a factorio server
├── requirements.txt        # mcp==1.30.0 (FastMCP)
└── app/
    ├── server.py           # tools, gates, HTTP transport
    ├── rcon.py             # async Source-RCON client, one persistent connection
    ├── docker_api.py       # restricted Docker Engine API client (one container)
    ├── presets.py          # named Lua snippets
    └── game_guide.md       # the game master guide (resource + prompt)
```

## License

[The Unlicense](LICENSE) — released into the public domain. Use it, fork it, sell it, no
attribution required.