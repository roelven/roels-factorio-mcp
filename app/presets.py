"""Named Lua snippets ("presets") for easy voice requests.

To add one: add an entry to PRESETS. Each entry has:
  desc   - one short line shown to the agent
  params - which arguments it needs (from: player, target, item, count, value)
  lua    - Lua code using {player}, {target}, {item} (already quoted Lua strings)
           and {count}, {value} (numbers). Use rcon.print() for output.
           `P` and `T` are bound to the player / target when those params are used.
All presets run through run_lua, so the achievements rule applies.
"""
import json
import re

_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,60}$")

# Prelude added when the preset uses `player` / `target`.
_GET_P = ('local P = game.get_player({player}) '
          'if not (P and P.valid) then rcon.print("No player named " .. {player}) return end ')
_GET_T = ('local T = game.get_player({target}) '
          'if not (T and T.valid) then rcon.print("No player named " .. {target}) return end ')
_NEED_CHAR = 'if not P.character then rcon.print(P.name .. " has no character right now (offline or dead?)") return end '

PRESETS: dict[str, dict] = {
    "always_day_on": {
        "desc": "Make it always day on the main map.",
        "params": [],
        "lua": 'game.surfaces["nauvis"].always_day = true rcon.print("Always day is ON")',
    },
    "always_day_off": {
        "desc": "Turn always-day off (normal day/night cycle).",
        "params": [],
        "lua": 'game.surfaces["nauvis"].always_day = false rcon.print("Always day is OFF")',
    },
    "speed_boost": {
        "desc": "Faster running and hand-crafting for a player. value = multiplier (default 2, max 5).",
        "params": ["player", "value"],
        "lua": _NEED_CHAR + 'P.character_running_speed_modifier = {value} - 1 '
               'P.character_crafting_speed_modifier = {value} - 1 '
               'rcon.print(P.name .. " now runs and crafts at x" .. {value})',
    },
    "speed_reset": {
        "desc": "Reset a player's running and crafting speed to normal.",
        "params": ["player"],
        "lua": _NEED_CHAR + 'P.character_running_speed_modifier = 0 P.character_crafting_speed_modifier = 0 '
               'rcon.print(P.name .. " is back to normal speed")',
    },
    "give_items": {
        "desc": "Give a player items, e.g. item='iron-plate' count=50. Item names use dashes.",
        "params": ["player", "item", "count"],
        "lua": 'if not prototypes.item[{item}] then rcon.print("Unknown item " .. {item} .. ". Use internal names like iron-plate, transport-belt.") return end '
               'local n = P.insert{{name = {item}, count = {count}}} '
               'rcon.print("Gave " .. P.name .. " " .. n .. " x " .. {item} .. (n < {count} and " (inventory full)" or ""))',
    },
    "reveal_map": {
        "desc": "Reveal (chart) the map around a player. value = radius in tiles (default 200, max 2000).",
        "params": ["player", "value"],
        "lua": 'local p, r = P.position, {value} '
               'P.force.chart(P.surface, {{{{p.x - r, p.y - r}}, {{p.x + r, p.y + r}}}}) '
               'rcon.print("Revealed " .. r .. " tiles around " .. P.name)',
    },
    "teleport_to_player": {
        "desc": "Teleport player to target player.",
        "params": ["player", "target"],
        "lua": _NEED_CHAR + 'local pos = T.surface.find_non_colliding_position("character", T.position, 20, 0.5) or T.position '
               'P.teleport(pos, T.surface) rcon.print("Teleported " .. P.name .. " to " .. T.name)',
    },
    "player_inventory": {
        "desc": "List what's in a player's main inventory (top 25).",
        "params": ["player"],
        "lua": 'local inv = P.get_main_inventory() if not inv then rcon.print(P.name .. " has no inventory right now") return end '
               'local c = inv.get_contents() table.sort(c, function(a, b) return a.count > b.count end) '
               'local out = {{}} for i = 1, math.min(25, #c) do out[#out + 1] = c[i].name .. " " .. c[i].count end '
               'rcon.print(#c == 0 and (P.name .. " has an empty inventory") or (P.name .. ": " .. table.concat(out, ", ")))',
    },
    "player_info": {
        "desc": "Where a player is, whether online, health, and play time.",
        "params": ["player"],
        "lua": 'local hp = P.character and math.floor(P.character.health) or "n/a" '
               'rcon.print(string.format("%s: %s, at x=%d y=%d on %s, health %s, played %d min", P.name, '
               'P.connected and "online" or "offline", P.position.x, P.position.y, P.surface.name, hp, P.online_time / 3600))',
    },
    "production_stats": {
        "desc": "Top items produced and consumed so far, plus current research.",
        "params": [],
        "lua": 'local f = game.forces.player local s = f.get_item_production_statistics("nauvis") '
               'local function top(t) local a = {{}} for k, v in pairs(t) do a[#a + 1] = {{k, v}} end '
               'table.sort(a, function(x, y) return x[2] > y[2] end) local o = {{}} '
               'for i = 1, math.min(10, #a) do o[#o + 1] = a[i][1] .. " " .. math.floor(a[i][2]) end return table.concat(o, ", ") end '
               'local r = f.current_research '
               'rcon.print("Produced: " .. top(s.input_counts) .. "\\nConsumed: " .. top(s.output_counts) .. '
               '"\\nResearching: " .. (r and (r.name .. " " .. math.floor(f.research_progress * 100) .. "%") or "nothing"))',
    },
    "research_status": {
        "desc": "Current research, progress and how many technologies are done.",
        "params": [],
        "lua": 'local f = game.forces.player local done, total = 0, 0 '
               'for _, t in pairs(f.technologies) do total = total + 1 if t.researched then done = done + 1 end end '
               'local r = f.current_research '
               'rcon.print("Researching: " .. (r and (r.name .. " " .. math.floor(f.research_progress * 100) .. "%") or "nothing") .. '
               '". Done " .. done .. " of " .. total .. " technologies.")',
    },
}

_DEFAULT_VALUE = {"speed_boost": 2, "reveal_map": 200}
_MAX_VALUE = {"speed_boost": 5, "reveal_map": 2000}


def _lua_str(s: str) -> str:
    return json.dumps(s)  # safe Lua string literal for the restricted charset below


def build(name: str, player: str | None = None, target: str | None = None,
          item: str | None = None, count: int | None = None, value: float | None = None) -> str:
    """Return the Lua code for a preset, or raise ValueError with a short reason."""
    if name not in PRESETS:
        raise ValueError(f"Unknown preset '{name}'. Available: {', '.join(PRESETS)}")
    p = PRESETS[name]
    args = {"player": player, "target": target, "item": item}
    for key in ("player", "target", "item"):
        if key in p["params"]:
            v = args[key]
            if not v or not _NAME.match(v):
                raise ValueError(f"Preset '{name}' needs a valid '{key}'.")
            args[key] = _lua_str(v)
    if "count" in p["params"]:
        args["count"] = int(max(1, min(10000, count or 1)))
    if "value" in p["params"]:
        v = value if value is not None else _DEFAULT_VALUE.get(name, 1)
        args["value"] = max(0.1, min(_MAX_VALUE.get(name, 2000), float(v)))
        if float(args["value"]).is_integer():
            args["value"] = int(args["value"])
    prelude = (_GET_P if "player" in p["params"] else "") + (_GET_T if "target" in p["params"] else "")
    return (prelude + p["lua"]).format(**args)


def describe() -> str:
    lines = []
    for n, p in PRESETS.items():
        ps = f" ({', '.join(p['params'])})" if p["params"] else ""
        lines.append(f"- {n}{ps}: {p['desc']}")
    return "\n".join(lines)
