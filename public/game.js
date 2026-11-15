(() => {
  const W = 1000, H = 650, B = 10;
  const TEAM = [{ name: 'Blue', color: '#2a58d8' }, { name: 'Red', color: '#d82a2a' }];
  const FONT = 'Chicago, Geneva, "Lucida Grande", sans-serif';
  const MAX_POWDER = 256, MIN_POWDER = 10, DEFAULT_AIM = { angle: 60, power: 192 };
  const ICON_H = 46;
  const REPLAY_SPEED = 3;                      // replays run this many times faster than the live slow motion

  const $ = (id) => document.getElementById(id);
  const canvas = $('c'), ctx = canvas.getContext('2d');
  const icons = $('icons'), ictx = icons.getContext('2d');

  let mode = 'online';                         // 'online' | 'computer' | 'local' (two people, one screen)
  const nameOf = (i) => (i === 1 && mode === 'computer' ? 'Computer' : TEAM[i].name);

  let ws = null;
  let you = null, state = null;
  let score = [0, 0], round = 0, target = 4;
  let firedMe = false, oppFired = false;       // who has locked in a shot this volley
  let vol = null;                              // volley animation in progress
  let roundEnded = false, over = false, seriesWinner = null, banner = '';
  let trails = [];                             // paths of recent shots, newest last
  let particles = [], shake = 0;
  let debris = [], rubble = [];                // flying chunks, and chunks that have come to rest this round
  let windVis = 0;                             // smoothed wind used to push clouds, flags and the wind vane
  let windMode = 'constant', optionsLocked = false, closedByServer = false;
  let scale = 1.2;                             // backing-store pixels per world unit
  const aim = [{ ...DEFAULT_AIM }, { ...DEFAULT_AIM }];
  let hist = [[], []];                         // shots each player has fired, oldest first
  let chatMsgs = [];                           // recent chat lines, drawn as plain text in the green under your castle
  let localSeat = 0, maskedSeat = -1;          // local game: whose turn to aim, and whose dials are hidden after locking in
  let roundStart = null, roundVolleys = [];    // recording of the round in progress
  let lastRound = null;                        // the last finished round, for replays
  let replaying = null, replayPressed = false, oppReplay = false;
  const clouds = Array.from({ length: 6 }, (_, i) => ({ x: i * 180, y: 30 + (i * 47) % 110, s: 0.7 + (i % 3) * 0.3 }));

  // Hill shown behind the game window before a game starts.
  const demoTerrain = Array.from({ length: 201 }, (_, i) => {
    const x = i * 5, d = (x - 500) / 350;
    const bump = Math.abs(d) < 1 ? 330 * 0.5 * (1 + Math.cos(Math.PI * d)) : 0;
    return 525 - bump;
  });

  // The seat whose dials the controls drive right now.
  const me = () => (mode === 'local' ? localSeat : you);

  // ---------------------------------------------------------------- sizing

  // The window is CSS-scaled. Keep the --u CSS variable and the canvas resolution matched to its real size.
  let lastMacW = 0;
  function fit() {
    const macW = $('mac').clientWidth;
    if (macW !== lastMacW) { lastMacW = macW; document.documentElement.style.setProperty('--u', (macW / 1000).toFixed(3)); }
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cw = Math.max(500, Math.round(canvas.clientWidth * dpr));
    if (cw !== canvas.width) {
      canvas.width = cw; canvas.height = Math.round(cw * H / W);
      icons.width = cw; icons.height = Math.round(cw * ICON_H / W);
    }
    scale = canvas.width / W;
  }
  window.addEventListener('resize', fit);

  // ---------------------------------------------------------------- network

  function connect(onOpen) {
    $('lobbyMsg').textContent = '';
    closedByServer = false;
    if (ws) { ws.onclose = null; ws.close(); }
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host);
    ws.onopen = onOpen;
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      setChatEnabled(false);
      if (closedByServer) return;
      if (state && !over) { $('left').querySelector('h2').textContent = 'Connection lost'; show('left'); }
      else if (!state) { $('lobbyMsg').textContent = 'Could not reach the server.'; showLobbyMenu(); }
    };
  }

  const send = (obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));
  const show = (id) => $(id).classList.remove('hidden');
  const hide = (id) => $(id).classList.add('hidden');
  function showLobbyMenu() { show('lobbyMenu'); hide('waiting'); lockOptions(false); }

  function onMessage(m) {
    switch (m.type) {
      case 'created':
        hide('lobbyMenu'); show('waiting');
        $('code').textContent = m.code;
        $('windLocked').textContent = `${windMode === 'constant' ? 'Constant' : 'Variable'} wind - best of ${$('bestOf').value}`;
        break;
      case 'start':
        resetReplay();
        you = m.you; state = m.state; score = m.score; round = m.round; target = m.target; mode = m.mode;
        applyWindMode(m.windMode); $('bestOf').value = String(m.target * 2 - 1); lockOptions(true);
        firedMe = oppFired = false; vol = null; roundEnded = false; over = false; seriesWinner = null;
        banner = ''; trails = []; particles = []; debris = []; rubble = [];
        localSeat = 0; maskedSeat = -1; replayPressed = oppReplay = false;
        roundStart = JSON.parse(JSON.stringify(m.state)); roundVolleys = [];
        if (round === 1) { aim[0] = { ...DEFAULT_AIM }; aim[1] = { ...DEFAULT_AIM }; hist = [[], []]; chatMsgs = []; lastRound = null; }
        windVis = state.wind;
        hide('lobby'); hide('gameover'); document.body.classList.remove('menu');
        showChat(mode === 'online');
        syncControls();
        break;
      case 'fired':
        if (mode === 'online' && m.player !== you) oppFired = true;
        break;
      case 'volley':
        startVolley(m);
        break;
      case 'replay_wait':
        oppReplay = true;
        syncControls();
        break;
      case 'replay_go':
        startReplay();
        break;
      case 'chat':
        chatMsgs.push({ player: m.player, text: m.text, born: performance.now() });
        chatMsgs = chatMsgs.slice(-6);
        break;
      case 'rematch_wait':
        if (!$('gameover').classList.contains('hidden')) $('goText').textContent += '\n(Opponent wants a rematch)';
        break;
      case 'opponent_left':
        setChatEnabled(false);
        if (!over) { $('left').querySelector('h2').textContent = 'Opponent left'; show('left'); }
        else { $('rematchBtn').disabled = true; $('goText').textContent += '\n(Opponent left)'; }
        break;
      case 'expired':      // the server closes abandoned games to free resources
        closedByServer = true;
        if (m.waiting || !state) { $('lobbyMsg').textContent = 'Nobody joined in time, so the game was closed.'; showLobbyMenu(); }
        else { $('left').querySelector('h2').textContent = 'Game timed out'; show('left'); }
        break;
      case 'error':
        $('lobbyMsg').textContent = m.msg;
        showLobbyMenu();
        break;
    }
  }

  // ---------------------------------------------------------------- volleys

  const cannonPos = (i) => {
    const c = state.castles[i];
    return { x: c.x0 + c.keepCol * B + B / 2, y: c.baseY - c.grid[c.keepCol].length * B - 4 };
  };

  function startVolley(m, isReplay = false) {
    vol = { m, t: 0, applied: 0, replay: isReplay };
    stopHold();
    if (!isReplay) {
      replayPressed = oppReplay = false;
      roundVolleys.push(m);
      localSeat = 0; maskedSeat = -1;
    }
    m.shots.forEach((s, i) => {
      aim[i] = { angle: s.angle, power: s.power };
      if (!isReplay) hist[i].push({ angle: s.angle, power: s.power });
      const c = cannonPos(i);
      for (let k = 0; k < 12; k++) addParticle(c.x, c.y, (i ? -1 : 1) * (30 + Math.random() * 90), -Math.random() * 70, '#888', 2 + Math.random() * 2, 0.5);
    });
    syncControls();
  }

  // Swap in new castle grids; every block that vanished throws a couple of stone chunks, so a collapse is visible.
  function applyGrids(grids) {
    grids.forEach((g, i) => {
      const c = state.castles[i];
      c.grid.forEach((col, ci) => {
        for (let r = g[ci].length; r < col.length; r++) {
          for (let k = 0; k < 2; k++) {
            debris.push({ x: c.x0 + ci * B + Math.random() * B, y: c.baseY - (r + 0.5) * B, vx: (Math.random() - 0.5) * 170,
              vy: -40 - Math.random() * 160, size: 2 + Math.floor(Math.random() * 3), stone: true });
          }
        }
      });
      c.grid = g;
    });
  }

  function stepVolley(dt) {
    if (!vol) return;
    vol.t += dt;
    const m = vol.m, f = vol.t * m.rate;
    while (vol.applied < m.events.length && m.events[vol.applied].t <= f) {
      const e = m.events[vol.applied++];
      applyGrids(e.castles);
      explosion(e.impact[0], e.impact[1]);
    }
    if (f >= Math.max(...m.shots.map((s) => s.path.length)) + 4) finishVolley();
  }

  function finishVolley() {
    const m = vol.m, isReplay = vol.replay;
    m.castles.forEach((g, i) => { state.castles[i].grid = g; });
    state.wind = m.wind;
    m.shots.forEach((s, i) => trails.push({ path: s.path, player: i }));
    trails = trails.slice(-6);
    vol = null;
    if (isReplay) { replayAdvance(); return; }
    score = m.score;
    firedMe = oppFired = false;
    if (m.roundWinner !== null) {
      roundEnded = true;
      lastRound = { start: roundStart, volleys: roundVolleys, round };
      const w = m.roundWinner;
      banner = w === -1 ? 'Both castles fell - draw! The round restarts...'
        : mode === 'local' ? `${TEAM[w].name} wins the round!`
        : w === you ? 'You win the round!' : `${mode === 'computer' ? 'Computer' : 'Opponent'} wins the round!`;
    }
    if (m.seriesWinner !== null) {
      over = true; seriesWinner = m.seriesWinner;
      setTimeout(showGameOver, 1500);
    }
    syncControls();
  }

  function showGameOver() {
    const local = mode === 'local';
    const headline = local ? `${TEAM[seriesWinner].name} wins the match!` : seriesWinner === you ? 'Victory!' : 'Defeat';
    const [a, b] = local ? [score[0], score[1]] : [score[you], score[1 - you]];
    $('goText').textContent = `${headline}\n${a} - ${b}`;
    $('rematchBtn').disabled = false;
    syncControls();
    show('gameover');
  }

  // ---------------------------------------------------------------- replay

  // Replays rebuild the last round from its recorded start and re-run every volley at REPLAY_SPEED.
  function startReplay() {
    if (!lastRound || vol || replaying) return;
    replayPressed = oppReplay = false;
    hide('gameover');
    replaying = { k: 0, timer: null, saved: { state, trails, rubble, debris, banner, windVis, aim: [{ ...aim[0] }, { ...aim[1] }] } };
    state = JSON.parse(JSON.stringify(lastRound.start));
    trails = []; rubble = []; debris = []; particles = []; banner = ''; windVis = state.wind;
    playReplayVolley();
  }

  function playReplayVolley() {
    const v = lastRound.volleys[replaying.k];
    startVolley({ ...v, rate: v.rate * REPLAY_SPEED }, true);
  }

  function replayAdvance() {
    replaying.k += 1;
    const more = replaying.k < lastRound.volleys.length;
    replaying.timer = setTimeout(more ? playReplayVolley : endReplay, more ? 600 : 1800);
    syncControls();
  }

  function restoreAfterReplay() {
    const s = replaying.saved;
    clearTimeout(replaying.timer);
    replaying = null; vol = null; particles = [];
    state = s.state; trails = s.trails; rubble = s.rubble; debris = s.debris; banner = s.banner; windVis = s.windVis;
    aim[0] = s.aim[0]; aim[1] = s.aim[1];
  }

  function endReplay() {
    restoreAfterReplay();
    if (over) showGameOver();
    syncControls();
  }

  function resetReplay() { if (replaying) restoreAfterReplay(); }

  function requestReplay() {
    if (!canReplay()) return;
    replayPressed = true;
    send({ type: 'replay' });
    syncControls();
  }

  // ---------------------------------------------------------------- effects

  function addParticle(x, y, vx, vy, color, size, life) {
    particles.push({ x, y, vx, vy, color, size, life, max: life });
  }

  const groundAt = (x) => {
    const t = state.terrain, f = Math.min(Math.max(x, 0), W) / 5, i = Math.min(Math.floor(f), t.length - 2);
    return t[i] + (t[i + 1] - t[i]) * (f - i);
  };

  const insideCastle = (x, y) => state.castles.some((c) => {
    const col = Math.floor((x - c.x0) / B), row = Math.floor((c.baseY - y) / B);
    return col >= 0 && col < c.grid.length && row >= 0 && row < c.grid[col].length;
  });

  // Distance from a point to the nearest castle's original footprint.
  const distToCastle = (x, y) => Math.min(...state.castles.map((c) => {
    const dx = Math.max(c.x0 - x, 0, x - (c.x0 + 3 * B)), dy = Math.max(c.baseY - 3 * B - y, 0, y - c.baseY);
    return Math.hypot(dx, dy);
  }));

  function explosion(x, y) {
    shake = 6;
    const colors = ['#000', '#fff', '#ff9a00', '#ffd800', '#888'];
    for (let i = 0; i < 40; i++) {
      const a = Math.random() * Math.PI * 2, s = 30 + Math.random() * 170;
      addParticle(x, y, Math.cos(a) * s, Math.sin(a) * s - 50, colors[i % colors.length], 2 + Math.random() * 3, 0.4 + Math.random() * 0.6);
    }
    // Only a shell landing close to a castle throws stone and dirt that falls back and stays as rubble.
    if (distToCastle(x, y) >= 60) return;
    for (let i = 0; i < 40; i++) {
      const a = -Math.PI * (0.08 + 0.84 * Math.random()), s = 50 + Math.random() * 260;
      debris.push({ x, y, vx: Math.cos(a) * s, vy: Math.sin(a) * s, size: 2 + Math.floor(Math.random() * 3), stone: Math.random() < 0.55 });
    }
  }

  function stepDebris(dt) {
    for (const d of debris) {
      d.vy += 600 * dt; d.x += d.vx * dt; d.y += d.vy * dt;
      if (d.x < 0 || d.x > W || d.y > H) d.dead = true;
      else if (insideCastle(d.x, d.y)) d.dead = true;
      else if (d.y >= groundAt(d.x)) { d.dead = true; rubble.push({ x: d.x, y: groundAt(d.x), size: d.size, stone: d.stone }); }
    }
    debris = debris.filter((d) => !d.dead);
    if (rubble.length > 320) rubble.splice(0, rubble.length - 320);
  }

  function drawChunk(x, y, size, stone) {
    ctx.fillStyle = '#000'; ctx.fillRect(x - size / 2 - 0.7, y - size - 0.7, size + 1.4, size + 1.4);
    ctx.fillStyle = stone ? '#fff' : '#7a5230'; ctx.fillRect(x - size / 2, y - size, size, size);
  }

  // ---------------------------------------------------------------- chat

  const chatForm = $('chatForm'), chatInput = $('chatInput');

  // The input box sits in the green strip under the opponent's castle (the far side from you).
  function showChat(on) {
    chatForm.classList.toggle('hidden', !on);
    chatForm.classList.toggle('left', you === 1);
    chatForm.classList.toggle('right', you !== 1);
    setChatEnabled(on);
  }
  const setChatEnabled = (on) => { chatInput.disabled = !on; };

  chatForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (text) send({ type: 'chat', text });
    chatInput.value = '';
  });
  chatInput.addEventListener('keydown', (e) => { if (e.key === 'Escape') chatInput.blur(); });

  // ---------------------------------------------------------------- controls

  const canAim = () => !!state && !over && !roundEnded && !vol && !replaying && !firedMe;
  const canReplay = () => !!lastRound && !!state && !vol && !replaying && !firedMe && !oppFired && !replayPressed
    && (over || !roundEnded) && maskedSeat < 0;
  const setText = (id, t) => { const el = $(id); if (el.textContent !== String(t)) el.textContent = t; };

  function syncControls() {
    const act = me(), local = mode === 'local';
    for (const i of [0, 1]) {
      const masked = local && maskedSeat === i;     // a locked-in shot stays secret on a shared screen
      setText('ang' + i, masked ? '--' : Math.round(aim[i].angle) + '°');
      setText('pow' + i, masked ? '--' : Math.round(aim[i].power) + 'g');
      setText('sc' + i, score[i]);
      $('side' + i).classList.toggle('theirs', i !== act);
      setText('pname' + i, nameOf(i) + (local ? (state && i === act && canAim() ? ' (aiming)' : '') : (i === you ? ' (you)' : '')));
      // Tiny reminder of an earlier shot. Yours: the shot you last fired. Online, the opponent's main field already
      // shows their last shot, so theirs is the one before it.
      let p = null;
      if (local) { if (i === act && !masked) p = hist[i][hist[i].length - 1]; }
      else p = hist[i][hist[i].length - (i === you ? 1 : 2)];
      setText('pa' + i, p ? `prev ${Math.round(p.angle)}°` : '');
      setText('pp' + i, p ? `prev ${Math.round(p.power)}g` : '');
    }
    const on = canAim();
    document.querySelectorAll('.arr').forEach((b) => { b.disabled = !on; });
    $('fireBtn').disabled = !on;
    let label = firedMe ? 'Locked in' : 'Fire';
    if (local && state && !firedMe) label = localSeat === 0 ? 'Blue: Lock in' : 'Red: Fire';
    setText('fireBtn', label);
    const rp = canReplay();
    $('replayBtn').disabled = !rp;
    setText('replayBtn', replayPressed ? 'Waiting...' : (oppReplay && rp ? 'Join replay' : 'Replay'));
    $('replayBtn2').disabled = !rp;
  }

  function setAim(angle, power) {
    if (!canAim()) { stopHold(); return; }
    aim[me()] = { angle: Math.min(85, Math.max(5, angle)), power: Math.min(MAX_POWDER, Math.max(MIN_POWDER, power)) };
    syncControls();
  }

  function doFire() {
    if (!canAim()) return;
    const seat = me(), a = aim[seat];
    if (mode === 'local') {
      send({ type: 'fire', seat, angle: a.angle, power: a.power });
      if (seat === 0) {
        // Blue is locked in: hide the numbers (and barrel) behind the last public shot until the volley.
        const pub = hist[0][hist[0].length - 1] || DEFAULT_AIM;
        aim[0] = { angle: pub.angle, power: pub.power };
        maskedSeat = 0; localSeat = 1;
      } else {
        firedMe = true;
      }
    } else {
      send({ type: 'fire', angle: a.angle, power: a.power });
      firedMe = true;
    }
    stopHold();
    syncControls();
  }

  const nudge = (kind, d) => {
    const a = aim[me()];
    if (!a) return;
    if (kind === 'angle') setAim(a.angle + d, a.power); else setAim(a.angle, a.power + d);
  };

  let holdDelay = null, holdRepeat = null;
  function stopHold() { clearTimeout(holdDelay); clearInterval(holdRepeat); holdDelay = holdRepeat = null; }
  document.querySelectorAll('.arr').forEach((b) => {
    b.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      const kind = b.dataset.k, d = +b.dataset.d;
      nudge(kind, d);
      holdDelay = setTimeout(() => {
        let ticks = 0;   // powder spans 256 grains, so speed up the longer the button is held
        holdRepeat = setInterval(() => nudge(kind, kind === 'power' && ++ticks > 12 ? d * 5 : d), 70);
      }, 350);
    });
    ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => b.addEventListener(ev, stopHold));
  });

  $('fireBtn').addEventListener('click', doFire);
  $('replayBtn').addEventListener('click', requestReplay);
  $('replayBtn2').addEventListener('click', requestReplay);
  window.addEventListener('keydown', (e) => {
    if (!state || document.activeElement.tagName === 'INPUT') return;
    const k = e.key;
    if (k === 'ArrowLeft') nudge('angle', e.shiftKey ? -5 : -1);
    else if (k === 'ArrowRight') nudge('angle', e.shiftKey ? 5 : 1);
    else if (k === 'ArrowUp') nudge('power', e.shiftKey ? 10 : 1);
    else if (k === 'ArrowDown') nudge('power', e.shiftKey ? -10 : -1);
    else if (k === ' ') doFire();
    else return;
    e.preventDefault();
  });

  // ---------------------------------------------------------------- title screen

  // Wind and best-of are chosen on the title screen and locked once a game exists.
  function applyWindMode(wm) {
    if (wm !== 'variable' && wm !== 'constant') return;
    windMode = wm;
    $('windSelect').value = wm;
  }

  function lockOptions(locked) {
    optionsLocked = locked;
    $('windSelect').disabled = locked;
    $('bestOf').disabled = locked;
  }

  $('windSelect').addEventListener('change', () => applyWindMode($('windSelect').value));
  applyWindMode('constant');

  const startGame = (kind) => {
    lockOptions(true);
    connect(() => send({ type: 'create', mode: kind, windMode, bestOf: +$('bestOf').value }));
  };
  $('onlineBtn').addEventListener('click', () => startGame('online'));
  $('localBtn').addEventListener('click', () => startGame('local'));
  $('botBtn').addEventListener('click', () => startGame('computer'));
  const join = () => {
    const code = $('codeInput').value.trim().toUpperCase();
    if (code.length !== 4) { $('lobbyMsg').textContent = 'Enter the 4-letter game code.'; return; }
    lockOptions(true);
    connect(() => send({ type: 'join', code }));
  };
  $('joinBtn').addEventListener('click', join);
  $('codeInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') join(); });
  $('rematchBtn').addEventListener('click', () => {
    send({ type: 'rematch' });
    $('rematchBtn').disabled = true;
    if (mode === 'online') $('goText').textContent += '\n(Waiting for opponent...)';
  });

  // ---------------------------------------------------------------- drawing

  function drawSky() {
    ctx.fillStyle = '#80dfff'; ctx.fillRect(0, 0, W, H);
    for (const c of clouds) {
      const rects = [[0, 0, 70, 16], [12, -10, 40, 14], [-10, 8, 90, 12]];
      for (const [color, e] of [['#000', 1.5], ['#fff', 0]]) {
        ctx.fillStyle = color;
        for (const [rx, ry, rw, rh] of rects) {
          ctx.fillRect(Math.round(c.x + rx * c.s - e), Math.round(c.y + ry * c.s - e), Math.round(rw * c.s + 2 * e), Math.round(rh * c.s + 2 * e));
        }
      }
    }
  }

  function updateClouds(dt) {
    const wind = state ? state.wind : 0;
    windVis += (wind - windVis) * Math.min(1, dt * 1.5);
    const speed = windVis * 3.5 + (Math.abs(windVis) < 1 ? 4 : 0);
    for (const c of clouds) {
      c.x += speed * c.s * dt;
      if (c.x > W + 120) c.x = -150;
      else if (c.x < -150) c.x = W + 120;
    }
  }

  function drawTerrain(t) {
    const step = W / (t.length - 1);
    ctx.beginPath(); ctx.moveTo(0, H + 4);
    t.forEach((y, i) => ctx.lineTo(i * step, y));
    ctx.lineTo(W, H + 4); ctx.closePath();
    ctx.fillStyle = '#0e7a2c'; ctx.fill();
    ctx.beginPath();
    t.forEach((y, i) => (i ? ctx.lineTo(i * step, y) : ctx.moveTo(0, y)));
    ctx.strokeStyle = '#000'; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
  }

  function drawCastle(i) {
    const c = state.castles[i];
    c.grid.forEach((col, ci) => {
      col.forEach((cell, r) => {
        const x = c.x0 + ci * B, y = c.baseY - (r + 1) * B;
        const door = ci === 1 && r === 0;
        ctx.fillStyle = door ? '#000' : '#fff';
        ctx.fillRect(x, y, B, B);
        ctx.strokeStyle = '#000'; ctx.lineWidth = 1;
        ctx.strokeRect(x + 0.5, y + 0.5, B - 1, B - 1);
        ctx.beginPath();
        if (!door) {
          const vx = r % 2 ? B * 0.3 : B * 0.7;
          ctx.moveTo(x, y + B / 2); ctx.lineTo(x + B, y + B / 2);
          ctx.moveTo(x + vx, y); ctx.lineTo(x + vx, y + B / 2);
          ctx.moveTo(x + B - vx, y + B / 2); ctx.lineTo(x + B - vx, y + B);
        }
        ctx.stroke();
      });
    });

    // flag on the merlon opposite the cannon, streaming downwind
    const fc = c.keepCol === 0 ? 2 : 0, stack = c.grid[fc].length;
    if (stack) {
      const px = c.x0 + fc * B + B / 2, top = c.baseY - stack * B;
      const dir = Math.abs(windVis) < 2 ? (i ? -1 : 1) : Math.sign(windVis);
      ctx.strokeStyle = '#000'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px + 0.5, top); ctx.lineTo(px + 0.5, top - 13); ctx.stroke();
      const fx = dir > 0 ? px + 1 : px - 7;
      ctx.fillStyle = TEAM[i].color; ctx.fillRect(fx, top - 13, 7, 5);
      ctx.strokeRect(fx + 0.5, top - 12.5, 6, 4);
    }
  }

  function drawCannon(i) {
    if (!state.castles[i].grid.some((col) => col.some((cell) => cell.keep))) return;   // the castle has fallen
    const p = cannonPos(i), a = aim[i];
    const dir = i === 0 ? 1 : -1, rad = a.angle * Math.PI / 180;
    ctx.fillStyle = '#000'; ctx.fillRect(p.x - 4, p.y, 8, 4);
    ctx.strokeStyle = '#000'; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p.x + dir * Math.cos(rad) * 12, p.y - Math.sin(rad) * 12); ctx.stroke();
  }

  function drawTrails() {
    ctx.lineWidth = 1;
    trails.forEach((t, k) => {
      const age = Math.floor((trails.length - 1 - k) / 2);   // 0 = most recent volley
      ctx.globalAlpha = [0.9, 0.55, 0.3][age] || 0.25;
      ctx.strokeStyle = TEAM[t.player].color;
      ctx.beginPath();
      t.path.forEach(([x, y], j) => (j ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  }

  function drawBalls() {
    if (!vol) return;
    const f = vol.t * vol.m.rate;
    vol.m.shots.forEach((s, i) => {
      const p = s.path, end = p.length - 1, idx = Math.min(end, Math.floor(f));
      const from = Math.max(0, idx - 25);
      // The path is sampled faster than it is played back, so slide the ball between samples.
      const frac = f < end ? f - idx : 0, nxt = p[Math.min(end, idx + 1)];
      const x = p[idx][0] + (nxt[0] - p[idx][0]) * frac, y = p[idx][1] + (nxt[1] - p[idx][1]) * frac;
      ctx.beginPath();
      for (let k = from; k <= idx; k++) (k === from ? ctx.moveTo(p[k][0], p[k][1]) : ctx.lineTo(p[k][0], p[k][1]));
      ctx.lineTo(x, y);
      ctx.strokeStyle = TEAM[i].color; ctx.lineWidth = 1.5; ctx.stroke();
      if (f < end) {
        ctx.fillStyle = '#000';
        if (x < 0 || x > W || y < 0) {     // out of frame: a triangle on the nearest edge tracks the ball along it
          const mx = Math.min(Math.max(x, 11), W - 11), my = Math.min(Math.max(y, 11), H - 11);
          ctx.save();
          ctx.translate(mx, my); ctx.rotate(Math.atan2(y - my, x - mx));
          ctx.beginPath(); ctx.moveTo(9, 0); ctx.lineTo(-2, -5); ctx.lineTo(-2, 5); ctx.fill();
          ctx.restore();
        } else {
          ctx.beginPath(); ctx.arc(x, y, 3, 0, 7); ctx.fill();
        }
      }
    });
  }

  function drawParticles(dt) {
    for (const p of particles) {
      p.life -= dt; p.vy += 400 * dt; p.x += p.vx * dt; p.y += p.vy * dt;
      ctx.globalAlpha = Math.max(0, p.life / p.max);
      ctx.fillStyle = p.color; ctx.fillRect(p.x, p.y, p.size, p.size);
    }
    ctx.globalAlpha = 1;
    particles = particles.filter((p) => p.life > 0);
    for (const d of debris) drawChunk(d.x, d.y, d.size, d.stone);
  }

  // Chat as plain text on the green ground under your own castle (right-aligned if you are the right castle).
  // Newest line at the bottom, at most four lines; each message fades out after about ten seconds.
  function drawChatOutput(now) {
    chatMsgs = chatMsgs.filter((m) => now - m.born < 13000);
    if (!chatMsgs.length || mode !== 'online') return;
    const right = you === 1, x = right ? W - 12 : 12, maxW = 460, rows = [];
    ctx.font = `14px ${FONT}`; ctx.textAlign = right ? 'right' : 'left';
    for (const m of chatMsgs) {
      const age = (now - m.born) / 1000, alpha = age < 10 ? 1 : 1 - (age - 10) / 3;
      let line = (m.player === you ? 'You' : nameOf(m.player)) + ':';
      for (const word of m.text.split(' ')) {
        if (ctx.measureText(`${line} ${word}`).width > maxW && line.trim() !== '') { rows.push({ text: line, alpha }); line = word; } else line += ` ${word}`;
      }
      rows.push({ text: line, alpha });
    }
    rows.slice(-4).forEach((r, k, shown) => {
      const y = H - 14 - (shown.length - 1 - k) * 17;
      ctx.globalAlpha = Math.max(0, r.alpha);
      ctx.fillStyle = '#063d14'; ctx.fillText(r.text, x + 1, y + 1);
      ctx.fillStyle = '#fff'; ctx.fillText(r.text, x, y);
    });
    ctx.globalAlpha = 1;
  }

  function macBox(text, y, size) {
    ctx.font = `bold ${size}px ${FONT}`;
    const bw = ctx.measureText(text).width + 48, bh = size * 2.9, x = Math.round((W - bw) / 2);
    ctx.fillStyle = '#000'; ctx.fillRect(x + 4, y + 4, bw, bh);
    ctx.fillStyle = '#fff'; ctx.fillRect(x, y, bw, bh);
    ctx.strokeStyle = '#000'; ctx.lineWidth = 2; ctx.strokeRect(x + 1, y + 1, bw - 2, bh - 2);
    ctx.fillStyle = '#000'; ctx.textAlign = 'center'; ctx.fillText(text, W / 2, y + bh / 2 + size * 0.35);
  }

  function drawBanner() {
    if (banner) macBox(banner, 150, 16);
    if (replaying) macBox(`REPLAY - round ${lastRound.round} - ${REPLAY_SPEED}x speed`, 14, 12);
  }

  // A heap of black powder that grows with the charge. Stippled with a fixed hash so it doesn't shimmer.
  function drawPowderPile(g, cx, baseY, frac) {
    const halfW = 4 + 17 * frac, peak = 4 + 28 * frac;
    g.fillStyle = '#000';
    g.fillRect(cx - 21, baseY, 42, 1);
    for (let px = Math.floor(cx - halfW); px < Math.ceil(cx + halfW); px++) {
      const dx = Math.abs(px + 0.5 - cx) / halfW;
      if (dx >= 1) continue;
      const h = Math.max(1, Math.round(peak * (1 - Math.pow(dx, 1.6))));
      for (let k = 0; k < h; k++) {
        const py = baseY - 1 - k;
        const dot = k === h - 1 || ((Math.imul(px, 73856093) ^ Math.imul(py, 19349663)) >>> 0) % 100 < 58;
        if (dot) g.fillRect(px, py, 1, 1);
      }
    }
  }

  // Bottom strip: cannon-angle and powder-pile meters for each side, wind vane in the middle.
  function drawIcons() {
    const g = ictx, IW = W, IH = ICON_H;
    g.setTransform(scale, 0, 0, scale, 0, 0);
    g.fillStyle = '#fff'; g.fillRect(0, 0, IW, IH);
    g.lineWidth = 1; g.strokeStyle = '#000';
    for (const i of [0, 1]) {
      const cx0 = i === 0 ? 0 : IW - 46, px0 = i === 0 ? 46 : IW - 92;
      g.strokeRect(cx0 + 0.5, 0.5, 45, IH - 1); g.strokeRect(px0 + 0.5, 0.5, 45, IH - 1);

      const rad = aim[i].angle * Math.PI / 180, dir = i === 0 ? 1 : -1, bx = cx0 + 23, by = IH - 8;
      g.lineWidth = 5; g.beginPath(); g.moveTo(bx, by); g.lineTo(bx + dir * Math.cos(rad) * 24, by - Math.sin(rad) * 24); g.stroke();
      g.fillStyle = '#000'; g.beginPath(); g.arc(bx, by, 6, 0, 7); g.fill();

      drawPowderPile(g, px0 + 23, IH - 5, aim[i].power / MAX_POWDER);
    }

    const cx = IW / 2, w = windVis;
    g.lineWidth = 2; g.strokeStyle = '#000'; g.beginPath(); g.moveTo(cx, 8); g.lineTo(cx, IH - 3); g.stroke();
    const len = Math.abs(w) < 0.5 ? 6 : 6 + Math.abs(w) * 1.5, fx = w < 0 ? cx - len : cx + 1;
    g.fillStyle = '#e02020'; g.fillRect(fx, 8, len, 8);
    g.lineWidth = 1; g.strokeRect(fx + 0.5, 8.5, len - 1, 7);
    g.fillStyle = '#000'; g.font = `10px ${FONT}`; g.textAlign = 'left';
    g.fillText(state ? `Wind ${Math.abs(state.wind)}${windMode === 'constant' ? ' (constant)' : ''}` : 'Wind', cx + 40, 30);
  }

  // ---------------------------------------------------------------- main loop

  let last = 0;
  function frame(ts) {
    const dt = Math.min(0.05, (ts - last) / 1000 || 0); last = ts;
    fit();
    stepVolley(dt);
    stepDebris(state ? dt : 0);
    updateClouds(dt);

    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.save();
    if (shake > 0.3) { ctx.translate((Math.random() - .5) * shake, (Math.random() - .5) * shake); shake *= 0.9; }
    drawSky();
    drawTerrain(state ? state.terrain : demoTerrain);
    if (state) {
      for (const r of rubble) drawChunk(r.x, r.y, r.size, r.stone);
      drawCastle(0); drawCastle(1);
      drawTrails();
      drawCannon(0); drawCannon(1);
      drawBalls();
      drawParticles(dt);
      drawChatOutput(ts);
    }
    ctx.restore();
    drawBanner();
    drawIcons();

    requestAnimationFrame(frame);
  }
  fit();
  requestAnimationFrame(frame);
  syncControls();
  lockOptions(false);
})();
