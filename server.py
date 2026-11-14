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
import struct
import sys
import time
from urllib.parse import unquote, urlsplit

# ---------------------------------------------------------------- game logic

W, H = 1000, 650          # playfield size in px
B = 10                    # block size
COLS = 3                  # columns per castle
G = 300.0                 # gravity px/s^2
DT = 1 / 120              # physics step
MAX_V = 760.0             # muzzle speed with a full charge
MAX_POWDER = 256          # grains of powder in a full charge
MIN_POWDER = 10
WIND_MODES = ("variable", "constant")
PLAYBACK_RATE = 15        # path points shown per second; the physics samples 60/s, so shots fly in 4x slow motion
TSTEP = 5                 # terrain sample spacing
BLAST = 18                # explosion radius
MAX_FLIGHT_STEPS = int(20 / DT)
MAX_WIND = 20
TARGET = 4                # rounds needed to win the best-of-7 match
SELF_SAFE_STEPS = 12      # a fresh shell ignores its own castle briefly so it can clear the walls

# Block-stack heights: two tall merlons with a lower wall between. The cannon sits on top of the keep
# (the top block of one merlon, the one nearest the enemy); the flag flies from the other merlon.
HEIGHTS = [3, 2, 3]
KEEP_COL = (2, 0)

X0 = (60, W - 60 - COLS * B)


def smoothstep(t):
    t = min(max(t, 0), 1)
    return t * t * (3 - 2 * t)


def pick_levels(rng):
    """Ground height under each castle (bigger y = lower). Often similar, often one side far higher."""
    if rng.random() < 0.35:
        return [rng.uniform(480, 555), rng.uniform(480, 555)]
    high, low = rng.uniform(390, 460), rng.uniform(510, 560)
    return [high, low] if rng.random() < 0.5 else [low, high]


def make_terrain(rng, levels):
    """Flat shelf under each castle with a lopsided hill between them, sometimes with a second hump."""
    left, right = rng.uniform(110, 330), rng.uniform(670, 890)   # where the hill starts and ends
    cx = rng.uniform(max(left + 120, 400), min(right - 120, 600))  # peak position
    span = right - left
    mid = levels[0] + (levels[1] - levels[0]) * smoothstep((cx - left) / span)
    peak = rng.uniform(110, min(levels) - 90)
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


def new_game(rng=random):
    levels = pick_levels(rng)
    terrain = make_terrain(rng, levels)
    castles = []
    for i in (0, 1):
        grid = []
        for c, h in enumerate(HEIGHTS):
            col = [{"hp": 2, "keep": False} for _ in range(h)]
            if c == KEEP_COL[i]:
                col[-1] = {"hp": 1, "keep": True}  # one hit and the keep is gone
            grid.append(col)
        castles.append({"x0": X0[i], "baseY": round(levels[i], 1), "keepCol": KEEP_COL[i], "grid": grid})
    return {"terrain": terrain, "castles": castles, "wind": new_wind()}


def cannon_pos(c):
    col = c["keepCol"]
    return c["x0"] + col * B + B / 2, c["baseY"] - len(c["grid"][col]) * B - 4


def hit(state, x, y, skip=None):
    if 0 <= x <= W and y >= ground_y(state["terrain"], x):
        return True
    for idx, c in enumerate(state["castles"]):
        if idx == skip:
            continue
        col = int((x - c["x0"]) // B)
        if 0 <= col < COLS:
            row = int((c["baseY"] - y) // B)
            if 0 <= row < len(c["grid"][col]):
                return True
    return False


def explode(state, ix, iy):
    for c in state["castles"]:
        for ci, col in enumerate(c["grid"]):
            for r, cell in enumerate(col):
                cx = c["x0"] + ci * B + B / 2
                cy = c["baseY"] - (r + 0.5) * B
                d = math.hypot(cx - ix, cy - iy)
                dmg = 2 if d < BLAST * 0.5 else (1 if d < BLAST else 0)
                if dmg:
                    cell["hp"] -= dmg
                    if cell["hp"] <= 0:
                        col[r] = None
            c["grid"][ci] = [cell for cell in col if cell]  # survivors fall down


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
            elif s["y"] > 0 and hit(state, s["x"], s["y"], skip=i if step < SELF_SAFE_STEPS else None):
                hits.append(i)
        for i in hits:  # collisions are tested before any explosion this step, so simultaneous hits are fair
            s = shots[i]
            s["active"] = False
            s["impact"] = [round(s["x"], 1), round(s["y"], 1)]
            s["path"].append(s["impact"])
            explode(state, *s["impact"])
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
    """Where a single shell would land (no explosion, no second shell), or None if it leaves the map."""
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
        if y > 0 and hit(state, x, y, skip=shooter if step < SELF_SAFE_STEPS else None):
            return x, y
    return None


def bot_shot(state, shooter, volleys):
    """Pick (angle, grains) for the computer. It only looks at the board and the wind, never at the human's aim.

    For each launch angle, solve the constant-acceleration flight equations (gravity plus wind) for the powder
    that passes through the enemy keep, then check that shot against the real simulation (the hill may be in the
    way). Finally add aiming error that shrinks with every volley fired this round, like a gunner ranging in."""
    foe = state["castles"][1 - shooter]
    k = foe["keepCol"]
    tx = foe["x0"] + k * B + B / 2
    ty = foe["baseY"] - (len(foe["grid"][k]) - 0.5) * B
    x0, y0 = cannon_pos(state["castles"][shooter])
    d = 1 if shooter == 0 else -1
    dx, dy, wind = tx - x0, ty - y0, state["wind"]

    good = []
    for ang in range(46, 86, 2):
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
        if land and math.hypot(land[0] - tx, land[1] - ty) < BLAST * 0.8:
            good.append((ang, power))
    ang, power = random.choice(good) if good else (65, 190)

    err = max(0.5, 1.5 * 0.75 ** volleys)
    ang = min(max(ang + random.gauss(0, 3 * err), 5), 85)
    power = min(max(power + random.gauss(0, 14 * err), MIN_POWDER), MAX_POWDER)
    return round(ang), round(power)


class Bot:
    """The computer opponent occupies seat 1 and never sends or receives anything."""
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
        self.bot_volleys = 0            # volleys fired this round, drives the computer's aiming error
        self.owner = None               # IpInfo of the address that created the room
        self.created = self.touched = time.monotonic()
        self.busy_until = 0.0           # the previous volley is still playing on the clients until then

    def start_round(self):
        self.timer = None
        self.round += 1
        self.state = new_game()
        self.pending = [None, None]
        self.round_over = False
        self.bot_volleys = 0
        self.busy_until = 0.0
        for i, cl in enumerate(self.clients):
            cl.send({"type": "start", "you": i, "state": self.state, "windMode": self.wind_mode,
                     "score": self.score, "round": self.round, "target": TARGET, "vsComputer": self.bot})

    def new_match(self):
        self.score, self.round, self.over = [0, 0], 0, False
        self.rematch = [False, False]
        self.start_round()


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
    if not room.state or room.over or room.round_over or room.pending[client.idx] is not None:
        return
    if LIMITS_ON and time.monotonic() < room.busy_until:   # a real client is locked until the volley finishes playing
        return
    angle, power = num(msg.get("angle"), 0, 90), num(msg.get("power"), MIN_POWDER, MAX_POWDER)
    if angle is None or power is None:
        return
    room.pending[client.idx] = (angle, power)
    room.clients[1 - client.idx].send({"type": "fired", "player": client.idx})
    if room.bot and room.pending[1] is None:
        room.pending[1] = bot_shot(room.state, 1, room.bot_volleys)
    if any(p is None for p in room.pending):
        return

    result = volley(room.state, room.pending, room.wind_mode)
    room.pending = [None, None]
    room.bot_volleys += 1
    rw = result["roundWinner"]
    if rw in (0, 1):
        room.score[rw] += 1
    result["score"] = list(room.score)
    result["seriesWinner"] = rw if rw in (0, 1) and room.score[rw] >= TARGET else None
    room.round_over = rw is not None
    room.over = result["seriesWinner"] is not None
    for cl in room.clients:
        cl.send(result)
    flight = max(len(s["path"]) for s in result["shots"]) / PLAYBACK_RATE
    room.busy_until = time.monotonic() + flight - VOLLEY_SLACK
    if room.round_over and not room.over:
        room.timer = asyncio.get_running_loop().call_later(flight + 3.5, room.start_round)


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
        rooms[room.code] = room
        room.clients[0] = client
        client.room, client.idx = room, 0
        if msg.get("vsComputer") is True:
            room.bot = True
            room.clients[1] = Bot()
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

    elif kind == "chat" and room and room.clients[1] and not room.bot:
        text = "".join(ch for ch in " ".join(str(msg.get("text", "")).split()) if ch.isprintable())[:120]
        now = time.monotonic()
        if text and now - client.last_chat >= 0.4:
            client.last_chat = now
            for cl in room.clients:
                cl.send({"type": "chat", "player": client.idx, "text": text})

    elif kind == "rematch" and room and room.over:
        room.rematch[client.idx] = True
        if room.bot:
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
              429: "Too Many Requests", 503: "Service Unavailable"}[status]
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n")
    return head.encode() + body


def serve_static(method, target):
    if method not in ("GET", "HEAD"):
        return http_response(405, b"Method not allowed")
    path = unquote(urlsplit(target).path)
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


async def main(port):
    server = await asyncio.start_server(handle_connection, "0.0.0.0", port, limit=16 * 1024)
    sweeper = asyncio.create_task(janitor())
    mode = f"rate limits on, proxy hops: {TRUST_PROXY}" if LIMITS_ON else "RATE LIMITS OFF"
    print(f"Range Lords running at http://localhost:{port}  ({mode}; Ctrl+C to stop)", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))))
    except KeyboardInterrupt:
        pass
