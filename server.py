#!/usr/bin/env python3
"""Range Lords server: static files + authoritative game logic over WebSockets.

Standard library only. Run:  python3 server.py [port]
"""
import asyncio
import base64
import copy
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import random
import signal
import struct
import sys
import time
from urllib.parse import unquote, urlsplit

# ---------------------------------------------------------------- game logic

W, H = 1000, 700          # playfield size in px
B = 10                    # block size
COLS = 3                  # columns per castle
G = 300.0                 # gravity px/s^2
DT = 1 / 120              # physics step
MAX_V = 760.0             # muzzle speed with a full charge
MAX_POWDER = 256          # grains of powder in a full charge
MIN_POWDER = 10
WIND_MODES = ("variable", "constant")
PLAYBACK_RATE = 15        # path points shown per second; the physics samples 60/s, so shots fly in 4x slow motion
REPLAY_SPEED = 3          # replays play this many times faster than the live slow motion
ROUND_END_PAUSE = 5.0     # seconds the result stays up before the next round starts (time to ask for a replay)
TSTEP = 5                 # terrain sample spacing
MAX_FLIGHT_STEPS = int(20 / DT)
MAX_WIND = 80              # strong enough to blow a high lob right back toward the shooter
TARGET = 4                # rounds needed to win the best-of-7 match
SELF_SAFE_STEPS = 12      # a fresh shell ignores its own castle briefly so it can clear the walls

# Block-stack heights: two tall merlons with a lower wall between. The cannon sits on top of the keep
# (the top block of one merlon, the one nearest the enemy); the flag flies from the other merlon.
HEIGHTS = [3, 2, 3]
KEEP_COL = (2, 0)

# Where the castles may stand: the left castle's x0 and the right castle's outer edge. The gap between them changes every round.
LEFT_X0 = (50, 200)
RIGHT_EDGE = (800, 950)
MIN_SOLUTIONS = 3         # a map is only used if each side has at least this many distinct winning angles


def smoothstep(t):
    t = min(max(t, 0), 1)
    return t * t * (3 - 2 * t)


def pick_levels(rng):
    """Ground height under each castle (bigger y = lower), anywhere from near the top of the screen to near the bottom.
    Mostly a real height difference, up to about 490 px; one map in five is almost level."""
    r = rng.random()
    if r < 0.20:
        diff = rng.uniform(0, 25)        # almost level
    elif r < 0.45:
        diff = rng.uniform(60, 200)
    else:
        diff = rng.uniform(200, 490)
    low = rng.uniform(110 + diff, 600)
    levels = [low - diff, low]
    if rng.random() < 0.5:
        levels.reverse()
    return levels


def make_terrain(rng, levels, x0):
    """Flat shelf under each castle with a lopsided hill between them, sometimes with a second hump."""
    inner_l, inner_r = x0[0] + COLS * B, x0[1]            # the inner edges of the two castles
    gap = inner_r - inner_l
    left = inner_l + gap * rng.uniform(0.05, 0.2)          # where the hill starts and ends
    right = inner_r - gap * rng.uniform(0.05, 0.2)
    span = right - left
    cx = rng.uniform(left + 0.3 * span, right - 0.3 * span)   # peak position
    mid = levels[0] + (levels[1] - levels[0]) * smoothstep((cx - left) / span)
    top = min(levels)
    peak = rng.uniform(max(20, top - 220), max(30, top - 40))   # the hill rises above both castles, even when one is near the top
    amp = mid - peak
    hump = None
    if rng.random() < 0.5:
        hump = (rng.uniform(left + 0.3 * span, right - 0.3 * span), rng.uniform(45, 75), rng.uniform(20, 55))
    pts = []
    for i in range(W // TSTEP + 1):
        x = i * TSTEP
        base = levels[0] + (levels[1] - levels[0]) * smoothstep((x - left) / span)
        half = cx - left if x < cx else right - cx
        d = (x - cx) / half
        bump = amp * 0.5 * (1 + math.cos(math.pi * d)) if abs(d) < 1 else 0
        if hump:
            bump += hump[2] * math.exp(-(((x - hump[0]) / hump[1]) ** 2))
        pts.append(round(base - bump, 1))
    return pts


def ground_y(terrain, x):
    x = min(max(x, 0), W)
    f = x / TSTEP
    i = int(f)
    if i >= len(terrain) - 1:
        return terrain[-1]
    t = f - i
    return terrain[i] * (1 - t) + terrain[i + 1] * t


def new_wind():
    return random.randint(-MAX_WIND, MAX_WIND)


def build_game(rng):
    levels = pick_levels(rng)
    x0 = (round(rng.uniform(*LEFT_X0)), round(rng.uniform(*RIGHT_EDGE)) - COLS * B)
    terrain = make_terrain(rng, levels, x0)
    castles = []
    for i in (0, 1):
        grid = []
        for c, h in enumerate(HEIGHTS):
            col = [{"keep": False} for _ in range(h)]
            if c == KEEP_COL[i]:
                col[-1] = {"keep": True}
            grid.append(col)
        castles.append({"x0": x0[i], "baseY": round(levels[i], 1), "keepCol": KEEP_COL[i], "grid": grid})
    return {"terrain": terrain, "castles": castles, "wind": new_wind()}


def new_game(rng=random):
    """A fresh map. Strong wind, a tall hill and uneven ground can occasionally make a side unwinnable, so reject those."""
    for _ in range(40):
        state = build_game(rng)
        if all(len(solutions(state, i)) >= MIN_SOLUTIONS for i in (0, 1)):
            break
    return state


def cannon_pos(c):
    col = c["keepCol"]
    return c["x0"] + col * B + B / 2, c["baseY"] - len(c["grid"][col]) * B - 4


def hit(state, x, y, skip=None):
    """What a shell at (x, y) is touching: None, -1 for the ground, or the index of the castle it struck."""
    if 0 <= x <= W and y >= ground_y(state["terrain"], x):
        return -1
    for idx, c in enumerate(state["castles"]):
        if idx == skip:
            continue
        col = int((x - c["x0"]) // B)
        if 0 <= col < COLS:
            row = int((c["baseY"] - y) // B)
            if 0 <= row < len(c["grid"][col]):
                return idx
    return None


def keep_alive(castle):
    return any(cell["keep"] for col in castle["grid"] for cell in col)


def volley(state, params, wind_mode="constant"):
    """Both cannons fire at once. params[i] = (angle, grains of powder). Mutates state; returns the result."""
    wind = state["wind"]
    shots = []
    for i in (0, 1):
        angle, power = params[i]
        x, y = cannon_pos(state["castles"][i])
        direction = 1 if i == 0 else -1
        a, v = math.radians(angle), power / MAX_POWDER * MAX_V
        shots.append({
            "x": x, "y": y, "vx": direction * math.cos(a) * v, "vy": -math.sin(a) * v,
            "active": True, "angle": angle, "power": power, "impact": None,
            "path": [[round(x, 1), round(y, 1)]],
        })

    events = []
    for step in range(MAX_FLIGHT_STEPS):
        if not any(s["active"] for s in shots):
            break
        hits = []
        for i, s in enumerate(shots):
            if not s["active"]:
                continue
            s["vx"] += wind * DT
            s["vy"] += G * DT
            s["x"] += s["vx"] * DT
            s["y"] += s["vy"] * DT
            if step % 2 == 1:
                s["path"].append([round(s["x"], 1), round(s["y"], 1)])
            if s["x"] < -60 or s["x"] > W + 60:
                s["active"] = False
            elif s["y"] > 0:
                touched = hit(state, s["x"], s["y"], skip=i if step < SELF_SAFE_STEPS else None)
                if touched is not None:
                    hits.append((i, touched))
        for i, touched in hits:  # collisions are tested before any explosion this step, so simultaneous hits are fair
            s = shots[i]
            s["active"] = False
            s["impact"] = [round(s["x"], 1), round(s["y"], 1)]
            s["path"].append(s["impact"])
            if touched >= 0:    # only a direct hit changes a castle: it comes down whole. A shell landing beside it does nothing.
                state["castles"][touched]["grid"] = [[] for _ in range(COLS)]
            events.append({"t": len(s["path"]) - 1, "player": i, "impact": s["impact"],
                           "castles": copy.deepcopy([c["grid"] for c in state["castles"]])})

    alive = [keep_alive(c) for c in state["castles"]]
    if not alive[0] and not alive[1]:
        round_winner = -1
    elif not alive[0]:
        round_winner = 1
    elif not alive[1]:
        round_winner = 0
    else:
        round_winner = None

    if wind_mode == "variable":
        state["wind"] = new_wind()
    return {
        "type": "volley",
        "rate": PLAYBACK_RATE,
        "shots": [{"angle": s["angle"], "power": s["power"], "path": s["path"], "impact": s["impact"]} for s in shots],
        "events": events,
        "castles": [c["grid"] for c in state["castles"]],
        "wind": state["wind"],
        "roundWinner": round_winner,
    }


# ------------------------------------------------------------ computer player

def trace(state, shooter, angle, power):
    """Where a single shell would land and what it hits (-1 ground, else a castle index), or None if it leaves the map."""
    x, y = cannon_pos(state["castles"][shooter])
    direction = 1 if shooter == 0 else -1
    a, v = math.radians(angle), power / MAX_POWDER * MAX_V
    vx, vy, wind = direction * math.cos(a) * v, -math.sin(a) * v, state["wind"]
    for step in range(MAX_FLIGHT_STEPS):
        vx += wind * DT
        vy += G * DT
        x += vx * DT
        y += vy * DT
        if x < -60 or x > W + 60:
            return None
        if y > 0:
            touched = hit(state, x, y, skip=shooter if step < SELF_SAFE_STEPS else None)
            if touched is not None:
                return x, y, touched
    return None


def solutions(state, shooter):
    """Launch settings (angle, grains) that really do destroy the enemy castle, found by solving the constant-acceleration
    flight equations (gravity plus wind) for each angle and then checking the result against the full simulation, since
    the hill may be in the way."""
    foe = state["castles"][1 - shooter]
    k = foe["keepCol"]
    tx = foe["x0"] + k * B + B / 2
    ty = foe["baseY"] - (len(foe["grid"][k]) - 0.5) * B
    x0, y0 = cannon_pos(state["castles"][shooter])
    d = 1 if shooter == 0 else -1
    dx, dy, wind = tx - x0, ty - y0, state["wind"]

    good = []
    for ang in range(20, 86, 2):
        sin_a, cot_a = math.sin(math.radians(ang)), 1 / math.tan(math.radians(ang))
        accel = 0.5 * (d * cot_a * G + wind)
        if abs(accel) < 1e-9:
            continue
        t2 = (dx + d * cot_a * dy) / accel      # time of flight squared
        if t2 <= 0:
            continue
        t = math.sqrt(t2)
        v = (0.5 * G * t2 - dy) / (sin_a * t)
        power = v / MAX_V * MAX_POWDER
        if v <= 0 or not MIN_POWDER <= power <= MAX_POWDER:
            continue
        land = trace(state, shooter, ang, power)
        if land and land[2] == 1 - shooter:
            good.append((ang, power))
    return good


def bot_shot(state, shooter, volleys):
    """Pick (angle, grains) for the computer. It only looks at the board and the wind, never at the human's aim.

    It takes one of the real solutions, then adds aiming error that shrinks with every volley fired this round,
    like a gunner ranging in."""
    good = solutions(state, shooter)
    ang, power = random.choice(good) if good else (65, 190)

    err = max(0.85, 1.6 * 0.75 ** volleys)
    ang = min(max(ang + random.gauss(0, 3 * err), 5), 85)
    power = min(max(power + random.gauss(0, 14 * err), MIN_POWDER), MAX_POWDER)
    return round(ang), round(power)


class Seat:
    """Seat 1 when nobody is connected there (the computer, or the second person at a shared screen)."""
    closed = False
    idx = 1
    room = None

    def send(self, obj):
        pass


# ------------------------------------------------------------------- limits
# Protection for the public internet. RATE_LIMIT=0 switches the rate limits off (handy for local testing).
# TRUST_PROXY=N says there are N reverse proxies in front of us (Render, Fly, Caddy...), so the real client
# address is the Nth-from-last entry of X-Forwarded-For. Leave it at 0 when the server faces the internet directly.

LIMITS_ON = os.environ.get("RATE_LIMIT", "1") != "0"
TRUST_PROXY = int(os.environ.get("TRUST_PROXY", "0"))

MAX_CONNECTIONS = 1500                 # open sockets in total
MAX_CONNS_PER_IP = 10                  # open websockets per client address
HTTP_BURST, HTTP_PER_SEC = 60, 3.0     # page and asset requests per address
WS_BURST, WS_PER_SEC = 10, 0.5         # new websocket connections per address
MSG_BURST, MSG_PER_SEC = 20, 8.0       # messages per websocket
MSG_STRIKES = 40                       # dropped messages before we hang up on a connection
CREATE_BURST, CREATE_PER_SEC = 6, 0.1  # games created per address
MAX_ROOMS, MAX_ROOMS_PER_IP = 1000, 4
JOIN_BURST, JOIN_PER_SEC = 6, 1 / 20   # room-code guesses per address (a successful join is refunded)
WAITING_ROOM_TTL = 600                 # seconds before a game nobody joined is closed
IDLE_ROOM_TTL = 1800                   # seconds without any message before a game is closed
JANITOR_EVERY = 30
PING_EVERY, READ_TIMEOUT = 40, 120     # websocket keepalive; a silent connection is dropped after READ_TIMEOUT
VOLLEY_SLACK = 0.3                     # seconds a Fire may arrive before the previous volley has finished playing


class Bucket:
    """Token bucket: `burst` tokens, refilled at `rate` per second."""
    __slots__ = ("burst", "rate", "tokens", "stamp")

    def __init__(self, burst, rate):
        self.burst, self.rate, self.tokens, self.stamp = burst, rate, float(burst), time.monotonic()

    def _refill(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def take(self):
        if not LIMITS_ON:
            return True
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def refund(self):
        self.tokens = min(self.burst, self.tokens + 1)

    def full(self):
        self._refill()
        return self.tokens >= self.burst


class IpInfo:
    """Everything we track per client address."""

    def __init__(self):
        self.conns = 0      # open websockets
        self.rooms = 0      # games this address currently owns
        self.http = Bucket(HTTP_BURST, HTTP_PER_SEC)
        self.ws = Bucket(WS_BURST, WS_PER_SEC)
        self.create = Bucket(CREATE_BURST, CREATE_PER_SEC)
        self.join = Bucket(JOIN_BURST, JOIN_PER_SEC)

    def idle(self):
        return self.conns == 0 and self.rooms == 0 and all(b.full() for b in (self.http, self.ws, self.create, self.join))


ips = {}
open_conns = 0
_last_logged = {}


def ip_info(ip):
    return ips.setdefault(ip, IpInfo())


def client_ip(writer, headers):
    peer = (writer.get_extra_info("peername") or ("?",))[0]
    ip = peer
    if TRUST_PROXY:
        hops = [h.strip() for h in headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if len(hops) >= TRUST_PROXY:
            ip = hops[-TRUST_PROXY]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return peer
    if addr.version == 6:   # one subscriber normally owns a whole /64
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return str(addr)


def same_origin(origin, host):
    """A browser tags every websocket handshake with the site the page came from. Only accept our own site, so a page
    on some other website cannot open connections to this server from its visitors' browsers."""
    try:
        return urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


def note_limit(ip, what):
    now = time.monotonic()
    if now - _last_logged.get((ip, what), -1e9) > 30:
        _last_logged[(ip, what)] = now
        print(f"[limit] {what}: {ip}", flush=True)


# ---------------------------------------------------------------- websocket

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MSG = 4 * 1024


def ws_frame(opcode, payload=b""):
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class Client:
    def __init__(self, writer, info, ip):
        self.writer = writer
        self.ip = ip
        self.info = info                          # per-address limits shared by all this address's connections
        self.msgs = Bucket(MSG_BURST, MSG_PER_SEC)
        self.strikes = 0
        self.room = None
        self.idx = None
        self.closed = False
        self.last_chat = 0.0

    def send(self, obj):
        if self.closed:
            return
        try:
            self.writer.write(ws_frame(1, json.dumps(obj, separators=(",", ":")).encode()))
        except Exception:
            self.closed = True


class Room:
    def __init__(self, code):
        self.code = code
        self.clients = [None, None]
        self.state = None
        self.score = [0, 0]
        self.round = 0
        self.wind_mode = "constant"     # "constant": fixed for the round; "variable": new wind after every volley
        self.pending = [None, None]     # locked-in (angle, grains) per player
        self.round_over = False         # a keep fell; waiting for the next round to begin
        self.over = False               # match decided
        self.rematch = [False, False]
        self.timer = None
        self.bot = False                # seat 1 is the computer
        self.local = False              # both people share one screen
        self.target = TARGET            # rounds needed to win the match
        self.replay = [False, False]    # who has asked to watch the last round again
        self.flights = []               # playing time of each volley this round, to size a replay
        self.bot_volleys = 0            # volleys fired this round, drives the computer's aiming error
        self.owner = None               # IpInfo of the address that created the room
        self.created = self.touched = time.monotonic()
        self.busy_until = 0.0           # the previous volley is still playing on the clients until then
        self.counted = False            # this match has been added to the stats

    def start_round(self):
        self.timer = None
        self.round += 1
        self.state = new_game()
        self.pending = [None, None]
        self.round_over = False
        self.bot_volleys = 0
        self.busy_until = 0.0
        self.replay = [False, False]
        self.flights = []
        mode = "computer" if self.bot else "local" if self.local else "online"
        for i, cl in enumerate(self.clients):
            cl.send({"type": "start", "you": i, "state": self.state, "windMode": self.wind_mode,
                     "score": self.score, "round": self.round, "target": self.target, "mode": mode})

    def new_match(self):
        self.score, self.round, self.over = [0, 0], 0, False
        self.rematch = [False, False]
        self.counted = False
        self.start_round()


# --------------------------------------------------------------------- stats

STATS_FILE = os.environ.get("STATS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json"))
STATS_DAYS = 60                  # days of per-day history to keep


class Stats:
    """The three numbers on the title screen. A game counts once its first volley has been fired, so rooms that were
    opened and abandoned do not inflate it. Nothing about individual players is recorded."""

    def __init__(self, path):
        self.path = path
        self.dirty = False
        self.total = 0
        self.days = {}                 # "YYYY-MM-DD" (UTC) -> games started that day
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        totals = data.get("totals", {})
        self.total = int(totals.get("games", 0)) if isinstance(totals, dict) else 0
        for day, v in data.get("days", {}).items():       # older files stored a dict per day; keep only the game count
            self.days[day] = int(v.get("games", 0)) if isinstance(v, dict) else int(v)

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"totals": {"games": self.total}, "days": self.days}, f)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            print(f"[stats] could not save: {e}", flush=True)

    def game_started(self):
        key = time.strftime("%Y-%m-%d", time.gmtime())
        self.days[key] = self.days.get(key, 0) + 1
        for old in sorted(self.days)[:-STATS_DAYS]:
            del self.days[old]
        self.total += 1
        self.dirty = True

    def snapshot(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        playing = sum(isinstance(c, Client) for r in rooms.values() if r.state for c in r.clients)
        return {"playingNow": playing, "gamesToday": self.days.get(today, 0), "totalGames": self.total}


stats = Stats(STATS_FILE)

rooms = {}


def make_code():
    while True:
        code = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4))
        if code not in rooms:
            return code


def num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return min(max(float(v), lo), hi)


def handle_fire(room, client, msg):
    idx = client.idx
    if room.local and type(msg.get("seat")) is int and msg["seat"] in (0, 1):
        idx = msg["seat"]       # two people share this device and take turns locking in
    if not room.state or room.over or room.round_over or room.pending[idx] is not None:
        return
    if LIMITS_ON and time.monotonic() < room.busy_until:   # a real client is locked until the volley finishes playing
        return
    angle, power = num(msg.get("angle"), 0, 90), num(msg.get("power"), MIN_POWDER, MAX_POWDER)
    if angle is None or power is None:
        return
    room.pending[idx] = (angle, power)
    if not room.local:
        room.clients[1 - idx].send({"type": "fired", "player": idx})
    if room.bot and room.pending[1] is None:
        room.pending[1] = bot_shot(room.state, 1, room.bot_volleys)
    if any(p is None for p in room.pending):
        return

    result = volley(room.state, room.pending, room.wind_mode)
    room.pending = [None, None]
    room.replay = [False, False]
    room.bot_volleys += 1
    rw = result["roundWinner"]
    if not room.counted:
        room.counted = True
        stats.game_started()
    if rw in (0, 1):
        room.score[rw] += 1
    result["score"] = list(room.score)
    result["seriesWinner"] = rw if rw in (0, 1) and room.score[rw] >= room.target else None
    room.round_over = rw is not None
    room.over = result["seriesWinner"] is not None
    for cl in room.clients:
        cl.send(result)
    flight = max(len(s["path"]) for s in result["shots"]) / PLAYBACK_RATE
    room.flights.append(flight)
    room.busy_until = time.monotonic() + flight - VOLLEY_SLACK
    if room.round_over and not room.over:
        room.timer = asyncio.get_running_loop().call_later(flight + ROUND_END_PAUSE, room.start_round)


def handle_replay(room, client):
    """Both players must ask before the last round is replayed, like Fire. One press is enough with a computer or a
    shared screen. It also works while the result of a round is still showing: the next round then waits for the replay."""
    refused = (not room.state or any(room.pending)
               or (LIMITS_ON and time.monotonic() < room.busy_until))
    if refused:
        return client.send({"type": "replay_denied"})
    room.replay[client.idx] = True
    if room.bot or room.local:
        room.replay = [True, True]
    if not all(room.replay):
        return room.clients[1 - client.idx].send({"type": "replay_wait"})
    room.replay = [False, False]
    if room.timer:      # a round just ended: hold the next round back until the replay has played out
        room.timer.cancel()
        length = sum(f / REPLAY_SPEED for f in room.flights) + 0.6 * max(0, len(room.flights) - 1) + 2.0
        room.timer = asyncio.get_running_loop().call_later(length + 2.0, room.start_round)
    for cl in room.clients:
        cl.send({"type": "replay_go"})


def handle_message(client, msg):
    kind = msg.get("type") if isinstance(msg, dict) else None
    room = client.room

    if kind == "create":
        if room:
            return client.send({"type": "error", "msg": "Already in a game."})
        info = client.info
        if not info.create.take():
            return client.send({"type": "error", "msg": "You are creating games too fast - wait a moment."})
        if LIMITS_ON and (len(rooms) >= MAX_ROOMS or info.rooms >= MAX_ROOMS_PER_IP):
            return client.send({"type": "error", "msg": "The server is busy - try again in a bit."})
        room = Room(make_code())
        room.owner = info
        info.rooms += 1
        if msg.get("windMode") in WIND_MODES:
            room.wind_mode = msg["windMode"]
        if type(msg.get("bestOf")) is int and msg["bestOf"] in (3, 5, 7, 9):
            room.target = (msg["bestOf"] + 1) // 2
        rooms[room.code] = room
        room.clients[0] = client
        client.room, client.idx = room, 0
        if msg.get("mode") in ("computer", "local"):
            room.bot = msg["mode"] == "computer"
            room.local = msg["mode"] == "local"
            room.clients[1] = Seat()
            room.new_match()
        else:
            client.send({"type": "created", "code": room.code})

    elif kind == "join":
        if room:
            return client.send({"type": "error", "msg": "Already in a game."})
        if not client.info.join.take():     # every guess costs a token up front; a successful join gets it back
            return client.send({"type": "error", "msg": "Too many guesses - wait a minute and try again."})
        target = rooms.get(str(msg.get("code", "")).strip().upper())
        if not target or target.clients[1] is not None:
            return client.send({"type": "error", "msg": "Game not found or already full."})
        client.info.join.refund()
        target.clients[1] = client
        client.room, client.idx = target, 1
        target.new_match()

    elif kind == "fire" and room:
        handle_fire(room, client, msg)

    elif kind == "replay" and room:
        handle_replay(room, client)

    elif kind == "chat" and room and room.clients[1] and not room.bot and not room.local:
        text = "".join(ch for ch in " ".join(str(msg.get("text", "")).split()) if ch.isprintable())[:120]
        now = time.monotonic()
        if text and now - client.last_chat >= 0.4:
            client.last_chat = now
            for cl in room.clients:
                cl.send({"type": "chat", "player": client.idx, "text": text})

    elif kind == "rematch" and room and room.over:
        room.rematch[client.idx] = True
        if room.bot or room.local:
            room.rematch[1] = True
        if all(room.rematch):
            room.new_match()
        else:
            room.clients[1 - client.idx].send({"type": "rematch_wait"})


def disconnect(client):
    client.closed = True
    room = client.room
    if not room:
        return
    if room.timer:
        room.timer.cancel()
        room.timer = None
    if rooms.pop(room.code, None) is room and room.owner:
        room.owner.rooms -= 1
    for cl in room.clients:
        if cl and cl is not client:
            cl.room = None
            cl.send({"type": "opponent_left"})
    client.room = None


async def keepalive(writer):
    """Ping now and then so dead connections show up as read timeouts instead of lingering forever."""
    try:
        while True:
            await asyncio.sleep(PING_EVERY)
            writer.write(ws_frame(9))
    except Exception:
        pass


async def ws_loop(reader, writer, client):
    async def read(n):
        return await asyncio.wait_for(reader.readexactly(n), READ_TIMEOUT)

    msg_buf = b""
    while True:
        b0, b1 = await read(2)
        fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", await read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await read(8))[0]
        if n > MAX_MSG or not masked:
            return
        mask = await read(4)
        data = bytearray(await read(n))
        for i in range(n):
            data[i] ^= mask[i & 3]

        if op == 8:
            return
        if op == 9:
            writer.write(ws_frame(0xA, bytes(data)))
            continue
        if op == 0xA:
            continue
        msg_buf = bytes(data) if op in (1, 2) else msg_buf + bytes(data)
        if len(msg_buf) > MAX_MSG:
            return
        if fin:
            try:
                msg = json.loads(msg_buf.decode())
            except ValueError:
                msg = None
            msg_buf = b""
            if msg is None:
                continue
            if not client.msgs.take():          # too chatty: drop it, and hang up on persistent offenders
                client.strikes += 1
                note_limit(client.ip, "message flood")
                if client.strikes >= MSG_STRIKES:
                    return
                continue
            if client.room:
                client.room.touched = time.monotonic()
            handle_message(client, msg)


# --------------------------------------------------------------------- http

PUBLIC = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public"))


def http_response(status, body=b"", ctype="text/plain; charset=utf-8", extra=""):
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
              403: "Forbidden", 429: "Too Many Requests", 503: "Service Unavailable"}[status]
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n")
    return head.encode() + body


def serve_static(method, target):
    if method not in ("GET", "HEAD"):
        return http_response(405, b"Method not allowed")
    path = unquote(urlsplit(target).path)
    if path == "/stats.json":
        body = json.dumps(stats.snapshot()).encode()
        return http_response(200, b"" if method == "HEAD" else body, "application/json", "Cache-Control: no-store\r\n")
    if path.endswith("/"):
        path += "index.html"
    full = os.path.realpath(os.path.join(PUBLIC, path.lstrip("/")))
    if not full.startswith(PUBLIC + os.sep) or not os.path.isfile(full):
        return http_response(404, b"Not found")
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype.endswith("javascript"):
        ctype += "; charset=utf-8"
    with open(full, "rb") as f:
        body = f.read()
    return http_response(200, b"" if method == "HEAD" else body, ctype, "Cache-Control: no-cache\r\n")


async def handle_connection(reader, writer):
    global open_conns
    open_conns += 1
    client = ping_task = info = None
    counted = False
    try:
        if LIMITS_ON and open_conns > MAX_CONNECTIONS:
            writer.write(http_response(503, b"Server busy", extra="Retry-After: 30\r\n"))
            return
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            writer.write(http_response(400, b"Bad request"))
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        ip = client_ip(writer, headers)
        info = ip_info(ip)

        if headers.get("upgrade", "").lower() == "websocket" and "sec-websocket-key" in headers:
            origin = headers.get("origin")      # absent for non-browser clients, which are not the concern here
            if origin is not None and not same_origin(origin, headers.get("host", "")):
                note_limit(ip, f"foreign origin {origin[:60]}")
                writer.write(http_response(403, b"Origin not allowed"))
                return
            if not info.ws.take() or (LIMITS_ON and info.conns >= MAX_CONNS_PER_IP):
                note_limit(ip, "websocket connections")
                writer.write(http_response(429, b"Too many connections", extra="Retry-After: 10\r\n"))
                return
            info.conns += 1
            counted = True
            accept = base64.b64encode(
                hashlib.sha1(headers["sec-websocket-key"].encode() + WS_GUID).digest()).decode()
            writer.write((
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            client = Client(writer, info, ip)
            ping_task = asyncio.create_task(keepalive(writer))
            await ws_loop(reader, writer, client)
        else:
            if not info.http.take():
                note_limit(ip, "http requests")
                writer.write(http_response(429, b"Too many requests", extra="Retry-After: 10\r\n"))
            else:
                writer.write(serve_static(parts[0], parts[1]))
            await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
            ConnectionError, OSError):
        pass
    finally:
        open_conns -= 1
        if counted:
            info.conns -= 1
        if ping_task:
            ping_task.cancel()
        if client:
            disconnect(client)
        try:
            writer.close()
        except Exception:
            pass


def expire(room):
    waiting = room.clients[1] is None
    for cl in room.clients:
        if isinstance(cl, Client):
            cl.send({"type": "expired", "waiting": waiting})
            cl.writer.close()      # the connection handler notices and tidies the room up


async def janitor():
    while True:
        await asyncio.sleep(JANITOR_EVERY)
        now = time.monotonic()
        for room in list(rooms.values()):
            if (room.clients[1] is None and now - room.created > WAITING_ROOM_TTL) or now - room.touched > IDLE_ROOM_TTL:
                expire(room)
        for ip in [ip for ip, i in ips.items() if i.idle()]:
            del ips[ip]
        for key in [k for k, t in _last_logged.items() if now - t > 300]:
            del _last_logged[key]
        if stats.dirty:
            stats.save()


async def main(port):
    server = await asyncio.start_server(handle_connection, "0.0.0.0", port, limit=16 * 1024)
    sweeper = asyncio.create_task(janitor())
    mode = f"rate limits on, proxy hops: {TRUST_PROXY}" if LIMITS_ON else "RATE LIMITS OFF"
    print(f"Range Lords running at http://localhost:{port}  ({mode}; Ctrl+C to stop)", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (stats.save(), os._exit(0)))     # a normal stop (or a host redeploy) keeps the totals
    try:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))))
    except KeyboardInterrupt:
        stats.save()
