#!/usr/bin/env python3
"""Local server for the KLAFS sauna Wi-Fi module – replaces the KLAFS cloud, talks to Home Assistant via MQTT.

The module connects by TCP to port 28888 (normally the KLAFS cloud). Point it to this server (module setting, or a
DNAT rule on the router for the cloud address) and it talks to this server as if it were the cloud. The KLAFS app does
not work anymore while the module is connected here.

Protocol (reverse-engineered from a recording, see DOCS.md):
  frame   HEADER (10 B) | LEN (2 B LE) | 02 | CMD (2 B LE) DATA… | 03 | CRC (2 B LE) | 04
          HEADER = 10-byte device header, learned from the module's first frame (not hard-coded; nothing is sent
          before it is known). LEN = length of CMD+DATA; CRC-16/MCRF4XX (poly 0x1021 reflected, init 0xFFFF)
          over header..03.
  0x1001 login · 0x1002 heartbeat (every minute, ack 00) · 0x1020 status · 0x1021 values
  (mode, set temperature, 00, humidity level, start time hour, minute – local time) · 0x1022 start with device PIN
  (ASCII) · 0x1023 stop.
  heartbeat data: [1] mode (1 sauna, 2 sanarium) · [2] state (0 off, 1 requested, 2 program active / preset)
  · [3] set temperature · [6] cabin temperature · [7] humidity candidate (sanarium only, unverified)
  · [12:14] start time (00:00 = none).
Findings: the control needs service menu 20 KEY = 2 ("remote"), otherwise a remote start is only "requested".
The start time must be in the future, otherwise the control waits until the next day → "now" = now + 1 minute.

Start while the module is offline: queued for up to 1 h and sent on the next heartbeat. After 3 min the start is
checked and resent once. Every heat-up is stored per minute in <data_dir>/heatup.db; from it a forecast "ready at
about HH:MM" (minutes per kelvin in 5-K bands, only after the first full heat-up) and messages heating up / 50 % /
ready / temperature not rising.

Home Assistant (MQTT discovery, device "KLAFS Sauna"): switch (off = stop; on is refused without PIN), lock
"Start (PIN)" (unlock with the start PIN = start), mode, set temperatures, humidity level, cabin temperature, humidity,
status, connected, ready, heated %, forecast, minutes to ready, last message. Options in /data/options.json (add-on)
or a JSON file given with --options (key data_dir overrides /data for local runs).
"""
import argparse, asyncio, datetime, json, logging, os, socket, sqlite3, threading, time, zoneinfo

LOGIN, BEAT, START_PIN, VALUES, STOP, STATUS = 0x1001, 0x1002, 0x1022, 0x1021, 0x1023, 0x1020
OFFLINE_AFTER = 600   # seconds without heartbeat until 'offline'
CONNECTED_BEAT = 150  # seconds: heartbeat younger than this + open connection = commands can be sent now
WAIT_MAX = 60 * 60    # seconds a start is queued while the module is offline
CHECK_AFTER = 180     # seconds after a start: running? otherwise resend once
HEAD_LEN = 10
DATA_DIR = '/data'
NAMES = {LOGIN: 'login', BEAT: 'heartbeat', VALUES: 'values', START_PIN: 'start/PIN', STOP: 'stop', STATUS: 'status'}
PRE = 'klafs/sauna'
log = logging.getLogger('klafs_local')

TEXT = {
    'en': {'sauna': 'Sauna', 'start': 'Start (PIN)', 'mode': 'Mode', 'temp_sauna': 'Set temperature sauna',
           'temp_sanarium': 'Set temperature sanarium', 'humidity_level': 'Humidity level (sanarium)', 'temperature': 'Cabin temperature',
           'humidity': 'Humidity', 'remaining': 'Remaining time', 'status': 'Status', 'connected': 'Wi-Fi module connected',
           'ready': 'Ready (temperature reached)', 'hint': 'Last message', 'off': 'off', 'heating': 'heating', 'ready_s': 'ready',
           'heated': 'Heated', 'forecast': 'Forecast', 'minutes_to_ready': 'Minutes to ready',
           'offline': 'Wi-Fi module offline', 'offline_run': 'Wi-Fi module offline (was running)',
           'waiting': 'Start waiting for Wi-Fi module', 'preset': 'preset {t}', 'requested': 'requested (check KEY / start time)',
           'no_pin': 'Not started: start only with PIN ("Start (PIN)").', 'wrong_pin': 'Not started: wrong PIN.',
           'sent': 'Start sent ({m}, start {t}).', 'failed': 'Command {a} failed: {e}', 'not_connected': 'Wi-Fi module not connected',
           'past': 'Start time {t} is not in the future',
           'queued': 'Sauna not reachable right now – the start is sent as soon as it is online (until {t}).',
           'queued_lost': 'Connection to the sauna lost – the start is sent as soon as it is online.',
           'queued_after': 'Sauna not reachable after the start – the start is sent again as soon as it is online.',
           'resumed': 'Sauna reachable again – start sent ({m}, start {t}).',
           'resent': 'Sauna did not react – start sent again.',
           'not_started': 'Not started ({e}) – please try again.',
           'timeout': 'Not started: sauna not reachable for 1 h – please try again.',
           'check_panel': 'Not started: the control reports "{s}" – please check at the device (KEY = 2?).',
           'fc_at': 'ready at about {t} ({m} min left)', 'fc_none': 'forecast after first full heat-up',
           'h_ready': 'Sauna ready: {temp} °C reached (heat-up {m} min).',
           'h_up': 'Sauna heating up: {temp} °C → {set} °C, {f}.',
           'h_50': 'Sauna 50 % heated ({temp} °C), {f}.',
           'h_cold': 'Sauna running for 15 min but the temperature is not rising ({temp} °C) – please check.'},
    'de': {'sauna': 'Sauna', 'start': 'Start (PIN)', 'mode': 'Modus', 'temp_sauna': 'Soll-Temperatur Sauna',
           'temp_sanarium': 'Soll-Temperatur Sanarium', 'humidity_level': 'Feuchte-Stufe (Sanarium)', 'temperature': 'Kabinentemperatur',
           'humidity': 'Feuchte', 'remaining': 'Restzeit', 'status': 'Status', 'connected': 'WLAN-Modul verbunden',
           'ready': 'Bereit (Temperatur erreicht)', 'hint': 'Letzte Meldung', 'off': 'aus', 'heating': 'heizt', 'ready_s': 'bereit',
           'heated': 'Aufgeheizt', 'forecast': 'Prognose', 'minutes_to_ready': 'Minuten bis bereit',
           'offline': 'WLAN-Modul offline', 'offline_run': 'WLAN-Modul offline (lief zuletzt)',
           'waiting': 'Start wartet auf WLAN-Modul', 'preset': 'Vorwahl {t}', 'requested': 'angefordert (KEY/Startzeit prüfen)',
           'no_pin': 'Nicht gestartet: Start nur mit PIN („Start (PIN)“).', 'wrong_pin': 'Nicht gestartet: PIN falsch.',
           'sent': 'Start gesendet ({m}, Start {t}).', 'failed': 'Befehl {a} fehlgeschlagen: {e}', 'not_connected': 'WLAN-Modul nicht verbunden',
           'past': 'Startzeit {t} liegt nicht in der Zukunft',
           'queued': 'Sauna gerade nicht erreichbar – Start wird nachgeholt, sobald sie online ist (bis {t}).',
           'queued_lost': 'Sauna-Verbindung abgebrochen – Start wird nachgeholt, sobald sie online ist.',
           'queued_after': 'Sauna nach dem Start nicht mehr erreichbar – Start wird nachgeholt, sobald sie online ist.',
           'resumed': 'Sauna wieder erreichbar – Start nachgeholt ({m}, Start {t}).',
           'resent': 'Sauna hat nicht reagiert – Start erneut gesendet.',
           'not_started': 'Sauna nicht gestartet ({e}) – bitte erneut versuchen.',
           'timeout': 'Sauna nicht gestartet: 1 h nicht erreichbar – bitte erneut versuchen.',
           'check_panel': 'Sauna nicht gestartet: Steuerung meldet „{s}“ – bitte am Gerät prüfen (KEY = 2?).',
           'fc_at': 'fertig ca. {t} (noch {m} min)', 'fc_none': 'Prognose nach erstem Hochheizen',
           'h_ready': 'Sauna bereit: {temp} °C erreicht (Aufheizen {m} min).',
           'h_up': 'Sauna heizt hoch: {temp} °C → {set} °C, {f}.',
           'h_50': 'Sauna zu 50 % aufgeheizt ({temp} °C), {f}.',
           'h_cold': 'Sauna läuft seit 15 min, aber die Temperatur steigt nicht ({temp} °C) – bitte prüfen.'},
}


def crc16(b):
    c = 0xFFFF
    for x in b:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c


def frame(cmd, data, head):
    body = cmd.to_bytes(2, 'little') + data
    r = head + len(body).to_bytes(2, 'little') + b'\x02' + body + b'\x03'
    return r + crc16(r).to_bytes(2, 'little') + b'\x04'


def _valid(r):
    return r[HEAD_LEN + 2] == 2 and r[-4] == 3 and r[-1] == 4 and crc16(r[:-3]) == int.from_bytes(r[-3:-1], 'little')


def learn_head(buf):
    """Find the 10-byte device header: a position where LEN, 02, 03, 04 and the CRC of a complete frame match."""
    for i in range(0, len(buf) - HEAD_LEN - 2):
        n = int.from_bytes(buf[i + HEAD_LEN:i + HEAD_LEN + 2], 'little')
        end = i + HEAD_LEN + 2 + 1 + n + 1 + 2 + 1
        if n < 2 or end > len(buf) or buf[i + HEAD_LEN + 2] != 2:
            continue
        if _valid(buf[i:end]):
            return bytes(buf[i:i + HEAD_LEN])
    return None


def split(buf, head):
    """Returns (list of (cmd, data), rest). Several frames per TCP packet happen."""
    out = []
    while True:
        i = buf.find(head)
        if i < 0 or len(buf) < i + 13:
            return out, buf[i:] if i >= 0 else b''
        n = int.from_bytes(buf[i + 10:i + 12], 'little')
        end = i + 12 + 1 + n + 1 + 2 + 1
        if len(buf) < end:
            return out, buf[i:]
        r = buf[i:end]
        buf = buf[end:]
        if not _valid(r):
            log.warning('frame dropped (form/CRC): %s', r.hex())
            continue
        out.append((int.from_bytes(r[13:15], 'little'), r[15:-4]))


def parse_beat(d):
    if len(d) < 15:
        return {'raw': d.hex()}
    return {'mode': d[1], 'state': d[2], 'set': d[3], 'temp': d[6], 'humidity_candidate': d[7],
            'start': f'{d[12]:02d}:{d[13]:02d}', 'raw': d.hex()}


def _locked(f):
    """DB access from several threads (heartbeat, watch loop, MQTT) one after the other."""
    def w(self, *a, **k):
        with self.lock:
            return f(self, *a, **k)
    return w


class HeatUp:
    """Heat-up runs (cabin temperature per minute) and forecast from minutes per kelvin (5-K bands, median of the last 10 runs)."""
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, start REAL, mode INT, set_temp INT,
                                     temp_start INT, ready REAL, temp_ready INT, finished REAL);
                                 CREATE TABLE IF NOT EXISTS points (run INT, ts REAL, temp INT, humidity INT);''')
        # close runs left open by a restart
        self.db.execute('UPDATE runs SET finished = start WHERE finished IS NULL'); self.db.commit()

    @_locked
    def new(self, mode, set_temp, temp):
        c = self.db.execute('INSERT INTO runs (start, mode, set_temp, temp_start) VALUES (?,?,?,?)', (time.time(), mode, set_temp, temp))
        self.db.commit(); return c.lastrowid

    @_locked
    def point(self, run, temp, humidity=None):
        self.db.execute('INSERT INTO points VALUES (?,?,?,?)', (run, time.time(), temp, humidity)); self.db.commit()

    @_locked
    def ready(self, run, temp):
        self.db.execute('UPDATE runs SET ready = ?, temp_ready = ? WHERE id = ?', (time.time(), temp, run)); self.db.commit()

    @_locked
    def finish(self, run):
        self.db.execute('UPDATE runs SET finished = ? WHERE id = ?', (time.time(), run)); self.db.commit()

    @_locked
    def calibrated(self):
        return self.db.execute('SELECT COUNT(*) FROM runs WHERE ready IS NOT NULL').fetchone()[0] > 0

    @_locked
    def bands(self):
        """{band (T//5): minutes per K} from the first time each whole temperature was reached per run."""
        ids = [r[0] for r in self.db.execute('SELECT id FROM runs ORDER BY id DESC LIMIT 10')]
        vals = {}
        for i in ids:
            first = {}
            for ts, t in self.db.execute('SELECT ts, temp FROM points WHERE run = ? ORDER BY ts', (i,)):
                if t is not None and t not in first and (not first or t > max(first)):
                    first[t] = ts
            tt = sorted(first)
            for a, b in zip(tt, tt[1:]):
                vals.setdefault(a // 5, []).append((first[b] - first[a]) / 60 / (b - a))
        return {k: sorted(v)[len(v) // 2] for k, v in vals.items()}

    @_locked
    def minutes(self, cur, target):
        """Minutes from cur to target, None until one run has heated up to 'ready'."""
        if not self.calibrated() or cur is None:
            return None
        b = self.bands()
        if not b:
            return None

        def rate(k):   # missing band: next known below, else above
            below = [x for x in b if x <= k]
            return b[max(below)] if below else b[min(b)]
        return round(sum(rate(t // 5) for t in range(int(cur), int(target))))


class Sauna:
    def __init__(self, opt, mq=None):
        self.o = opt
        self.t = TEXT.get(opt.get('language', 'en'), TEXT['en'])
        self.tz = zoneinfo.ZoneInfo(opt.get('timezone') or 'Europe/Berlin')
        self.mq = mq
        self.w = None
        self.head = None         # device header, learned from the module's first frame
        self.rid = 0
        self.beat = {}
        self.t_beat = 0.0
        self.want = {'mode': 1, 'temp_sauna': 80, 'temp_sanarium': 60, 'humidity': 8}
        self.loop = None
        self.day, self.connects = None, 0
        self.pending = None      # start queued while the module is offline {'at', 'until'}
        self.check_start = None  # start check {'t', 'attempt', 'at'}
        self.heat = None         # HeatUp database (set in main)
        self.run = None          # current heat-up run {'id', 'start', 'temp_start', 'sent': set}

    def record(self, rid, direction, data):
        d = self.o.get('log_dir')
        if not d:
            return
        t = datetime.datetime.now()
        try:
            os.makedirs(d, exist_ok=True)
            with open(f'{d}/{t:%Y%m%d}.log', 'a') as f:
                f.write(f'{t:%H:%M:%S.%f} #{rid} {direction} {len(data)}B {data.hex()}\n')
        except OSError as e:
            log.warning('log: %s', e)

    # ── module ──
    async def send(self, cmd, data=b''):
        if not self.w or not self.head:
            raise RuntimeError(self.t['not_connected'])
        r = frame(cmd, data, self.head)
        self.record(self.rid, 'K>M', r if cmd != START_PIN else frame(cmd, b'*' * len(data), self.head))  # PIN never logged
        log.info('→ module %s %s', NAMES.get(cmd, hex(cmd)), '(PIN)' if cmd == START_PIN else data.hex())
        self.w.write(r)
        await self.w.drain()

    async def values(self, start=None):
        z = start or datetime.datetime.now(self.tz)
        m = self.want['mode']
        temp = self.want['temp_sanarium'] if m == 2 else self.want['temp_sauna']
        hum = self.want['humidity'] if m == 2 else 0
        await self.send(VALUES, bytes([m, int(temp), 0, int(hum), z.hour, z.minute]))

    async def start(self, at=None):
        now = datetime.datetime.now(self.tz)
        if at:
            h, mi = (int(x) for x in at.split(':'))
            st = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if st <= now:
                raise RuntimeError(self.t['past'].format(t=at))
        else:
            st = now + datetime.timedelta(minutes=1)
        await self.values(st)
        await asyncio.sleep(0.3)
        await self.send(START_PIN, str(self.o.get('device_pin', '1234')).encode())
        return st.strftime('%H:%M')

    async def handle(self, cmd, d):
        if cmd == LOGIN:
            log.info('module login')
            await self.send(LOGIN, b'\x00\xa0\x02' + int(time.time()).to_bytes(4, 'little') + bytes(4))  # sets the clock
            await self.send(STATUS)
        elif cmd == BEAT:
            self.beat = parse_beat(d)
            self.t_beat = time.time()
            await self.send(BEAT, b'\x00')
            if self.beat.get('mode') in (1, 2):
                self.want['mode'] = self.beat['mode']
                if self.beat.get('set'):
                    self.want['temp_sanarium' if self.beat['mode'] == 2 else 'temp_sauna'] = self.beat['set']
            if self.pending:
                p, self.pending = self.pending, None
                try:
                    await self.run_start(p['at'], resumed=True)
                except (RuntimeError, OSError) as e:
                    self.hint(self.t['not_started'].format(e=e))
            try:
                self.track_run()
            except Exception as e:   # the database must never break the connection
                log.warning('heat-up: %s', e)
            self.publish()
        else:
            log.info('← module %s %s', NAMES.get(cmd, hex(cmd)), d.hex())

    async def connection(self, r, w):
        self.rid += 1
        rid, peer = self.rid, w.get_extra_info('peername')
        self.record(rid, 'OPEN', str(peer).encode())
        log.info('module connected from %s (#%s)', peer, rid)
        sock = w.get_extra_info('socket')
        if sock is not None:   # only after 180 s silence: the module does NOT answer keepalive probes (heartbeat every 60 s)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 180)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except OSError as e:
                log.warning('keepalive: %s', e)
        today = datetime.date.today().isoformat()
        if self.day != today:
            self.day, self.connects = today, 0
        self.connects += 1
        old, self.w, buf = self.w, w, b''
        if old and old is not w:
            try:
                old.close()
            except Exception:
                pass
        try:
            while d := await r.read(4096):
                self.record(rid, 'M>K', d)
                buf += d
                if not self.head:
                    self.head = learn_head(buf)
                    if not self.head:
                        buf = buf[-4096:]
                        continue
                    log.info('device header learned from the module')
                frames, buf = split(buf, self.head)
                for cmd, data in frames:
                    await self.handle(cmd, data)
        except Exception as e:
            self.record(rid, 'M>K!', str(e).encode())
        finally:
            self.record(rid, 'CLOSE', b'')
            log.info('module disconnected (#%s)', rid)
            if self.w is w:
                self.w = None
            self.publish()

    def connected(self):
        """Commands can be sent now: connection open and a recent heartbeat."""
        return self.w is not None and bool(self.t_beat) and time.time() - self.t_beat < CONNECTED_BEAT

    def mode_name(self):
        return 'Sanarium' if self.want['mode'] == 2 else 'Sauna'

    async def run_start(self, at, attempt=1, resumed=False):
        st = await self.start(at)
        self.check_start = {'t': time.time(), 'attempt': attempt, 'at': at}
        t = self.t
        self.hint(t['resent'] if attempt > 1 else t['resumed' if resumed else 'sent'].format(m=self.mode_name(), t=st))

    def queue_start(self, at, key):
        self.pending = {'at': at, 'until': time.time() + WAIT_MAX}
        until = datetime.datetime.now(self.tz) + datetime.timedelta(seconds=WAIT_MAX)
        self.hint(self.t[key].format(t=f'{until:%H:%M}'))

    async def request_start(self, at):
        if not self.connected():
            self.queue_start(at, 'queued')
            return
        try:
            await self.run_start(at)
        except (RuntimeError, OSError) as e:
            log.warning('start failed (%s) – queued', e)
            self.queue_start(at, 'queued_lost')

    async def check(self):
        """Every 30 s: queued start expired? Start accepted by the control?"""
        if self.pending and time.time() > self.pending['until']:
            self.pending = None
            self.hint(self.t['timeout'])
        c = self.check_start
        if c and time.time() - c['t'] > CHECK_AFTER:
            s = self._status()
            if s['running'] or s['preset']:
                self.check_start = None
            elif c['attempt'] == 1 and self.connected():
                try:
                    await self.run_start(c['at'], 2)
                except (RuntimeError, OSError) as e:
                    self.check_start = None
                    self.hint(self.t['not_started'].format(e=e))
            elif c['attempt'] == 1:
                self.check_start = None
                self.queue_start(c['at'], 'queued_after')
            else:
                self.check_start = None
                self.hint(self.t['check_panel'].format(s=self.state()['statusMessage'] or self.t['off']))

    # ── heat-up / forecast ──
    def target(self):
        setp = self.beat.get('set') or (self.want['temp_sanarium'] if self.want['mode'] == 2 else self.want['temp_sauna'])
        return setp - 3   # 'ready' like isReadyForUse

    def forecast(self):
        """(percent, minutes to ready or None, text)"""
        r, cur = self.run, self.beat.get('temp')
        if not r or cur is None:
            return None, None, None
        tgt = self.target()
        if cur >= tgt:
            return 100, 0, self.t['ready_s']
        pct = max(0, round(100 * (cur - r['temp_start']) / max(1, tgt - r['temp_start'])))
        m = self.heat.minutes(cur, tgt) if self.heat else None
        if m is None:
            return pct, None, self.t['fc_none']
        at = datetime.datetime.now(self.tz) + datetime.timedelta(minutes=m)
        return pct, m, self.t['fc_at'].format(t=f'{at:%H:%M}', m=m)

    def track_run(self):
        """After each heartbeat: create/extend the heat-up run, messages heating up / 50 % / ready / not rising.
        Only the highest applicable message is sent (ready > 50 % > heating up); lower ones are marked as sent, so a
        late first message (module was offline) does not come as a burst."""
        if not self.heat:
            return
        cur = self.beat.get('temp')
        if not self._status()['running']:
            if self.run:
                self.heat.finish(self.run['id']); self.run = None
            return
        if cur is None:
            return
        hum = self.beat.get('humidity_candidate') if self.want['mode'] == 2 else None
        tgt = self.target()
        if not self.run:
            self.run = {'id': self.heat.new(self.want['mode'], self.beat.get('set'), cur), 'start': time.time(),
                        'temp_start': cur, 'sent': set()}
            if cur >= tgt:   # server restart with an already hot cabin: no messages
                self.run['sent'].update({'ready', 'up', '50'})
        r = self.run
        self.heat.point(r['id'], cur, hum)
        pct, m, text = self.forecast()
        g, t = r['sent'], self.t
        rising = cur >= r['temp_start'] + 2
        if cur >= tgt and 'ready' not in g:
            g.update({'ready', 'up', '50'})
            self.heat.ready(r['id'], cur)
            self.hint(t['h_ready'].format(temp=cur, m=round((time.time() - r['start']) / 60)))
        elif pct is not None and pct >= 50 and (rising or 'up' in g) and '50' not in g:
            g.update({'up', '50'})
            self.hint(t['h_50'].format(temp=cur, f=text))
        elif rising and 'up' not in g:
            g.add('up')
            self.hint(t['h_up'].format(temp=cur, set=tgt + 3, f=text))
        elif 'up' not in g and 'cold' not in g and time.time() - r['start'] > 15 * 60:
            g.add('cold')
            self.hint(t['h_cold'].format(temp=cur))

    # ── Home Assistant ──
    def _status(self):
        conn = bool(self.t_beat) and time.time() - self.t_beat < OFFLINE_AFTER
        z, st = self.beat.get('state', 0), self.beat.get('start', '00:00')
        now = datetime.datetime.now(self.tz).strftime('%H:%M')
        mins = lambda x: int(x[:2]) * 60 + int(x[3:])
        preset = z == 2 and st != '00:00' and st > now and mins(st) - mins(now) < 18 * 60
        # offline stays 'on' if it was running last (otherwise a false 'sauna off' on every reconnect)
        running = bool(self.t_beat) and z == 2 and not preset
        return {'conn': conn, 'z': z, 'start': st, 'preset': preset, 'running': running}

    def state(self):
        t, b, s = self.t, self.beat, self._status()
        conn, z, st = s['conn'], s['z'], s['start']
        temp, setp = b.get('temp'), b.get('set')
        ready = conn and s['running'] and temp is not None and bool(setp) and temp >= setp - 3
        if self.pending:
            msg = t['waiting']
        elif not conn:
            msg = t['offline_run'] if s['running'] else t['offline']
        elif s['preset']:
            msg = t['preset'].format(t=st)
        elif z == 1:
            msg = t['requested']
        else:
            msg = None
        return {'isConnected': conn, 'isPoweredOn': s['running'], 'isReadyForUse': bool(ready), 'sanariumSelected': self.want['mode'] == 2,
                'selectedSaunaTemperature': self.want['temp_sauna'], 'selectedSanariumTemperature': self.want['temp_sanarium'],
                'selectedHumLevel': self.want['humidity'], 'currentTemperature': temp if conn else None,
                'currentHumidity': b.get('humidity_candidate') if conn and self.want['mode'] == 2 else None,
                'remainingBathingHours': 0, 'remainingBathingMinutes': 0, 'statusMessage': msg, 'state_raw': z, 'start_time': st,
                'heartbeat': datetime.datetime.fromtimestamp(self.t_beat, self.tz).isoformat(timespec='seconds') if self.t_beat else None,
                'connects_today': self.connects, 'raw': b.get('raw')}

    def state_full(self):
        d = self.state()
        pct, m, text = self.forecast() if d['isPoweredOn'] else (None, None, None)
        if not d['isPoweredOn'] and self.heat:
            text = None if self.heat.calibrated() else self.t['fc_none']
        d.update({'heated_percent': pct, 'minutes_to_ready': m, 'forecast': text})
        return d

    def publish(self):
        if self.mq:
            self.mq.publish(PRE + '/zustand', json.dumps(self.state_full()), retain=True)
            self.mq.publish(PRE + '/verfuegbar', 'online', retain=True)

    def hint(self, text):
        log.warning('message: %s', text)
        if self.mq:   # time appended so the same message triggers HA again
            self.mq.publish(PRE + '/hinweis', f'{text} ({datetime.datetime.now(self.tz):%H:%M:%S})')

    async def command(self, kind, value):
        t = self.t
        if kind == 'power':
            if value == 'ON':
                self.hint(t['no_pin'])
            else:
                self.pending = self.check_start = None
                await self.send(STOP)
        elif kind == 'start':
            try:
                b = json.loads(value)
            except ValueError:
                b = {'aktion': 'START', 'code': value}
            if b.get('aktion') == 'STOP':
                self.pending = self.check_start = None
                await self.send(STOP)
            elif str(b.get('code', '')).strip() != str(self.o.get('start_pin', '')):
                self.hint(t['wrong_pin'])
            else:
                await self.request_start((b.get('zeit') or '').strip() or None)
        elif kind == 'modus':
            self.want['mode'] = 2 if value == 'Sanarium' else 1
            await self.values()
        elif kind in ('temp_sauna', 'temp_sanarium'):
            self.want[kind] = max(10, min(100, int(float(value))))
            await self.values()
        elif kind == 'feuchte':
            self.want['humidity'] = max(1, min(10, int(float(value))))
            await self.values()
        self.publish()

    def discovery(self):
        st, t = PRE + '/zustand', self.t
        dev = {'identifiers': ['klafs_sauna'], 'name': 'KLAFS Sauna', 'manufacturer': 'KLAFS', 'model': 'Wi-Fi module (local)'}

        def disc(comp, oid, cfg):
            cfg = {'unique_id': 'klafs_' + oid, 'default_entity_id': f'{comp}.klafs_sauna_{oid}', 'device': dev,
                   'availability_topic': PRE + '/verfuegbar', **cfg}
            self.mq.publish(f'homeassistant/{comp}/klafs_sauna/{oid}/config', json.dumps(cfg), retain=True)
        disc('switch', 'sauna', {'name': t['sauna'], 'icon': 'mdi:sauna', 'command_topic': PRE + '/set/power', 'state_topic': st,
                                 'value_template': "{{ 'ON' if value_json.isPoweredOn else 'OFF' }}"})
        disc('lock', 'start_pin', {'name': t['start'], 'icon': 'mdi:sauna', 'command_topic': PRE + '/set/start',
                                   'code_format': '^\\d{4,8}$', 'payload_unlock': 'START', 'payload_lock': 'STOP',
                                   'command_template': '{"aktion": "{{ value }}", "code": "{{ code }}"}', 'state_topic': st,
                                   'value_template': "{{ 'UNLOCKED' if value_json.isPoweredOn or (value_json.statusMessage and value_json.start_time != '00:00' and value_json.state_raw == 2) else 'LOCKED' }}",
                                   'state_locked': 'LOCKED', 'state_unlocked': 'UNLOCKED'})
        disc('select', 'modus', {'name': t['mode'], 'icon': 'mdi:sauna', 'options': ['Sauna', 'Sanarium'], 'command_topic': PRE + '/set/modus',
                                 'state_topic': st, 'value_template': "{{ 'Sanarium' if value_json.sanariumSelected else 'Sauna' }}"})
        for oid, key, top in (('temp_sauna', 'temp_sauna', 'selectedSaunaTemperature'), ('temp_sanarium', 'temp_sanarium', 'selectedSanariumTemperature')):
            disc('number', oid, {'name': t[key], 'min': 10, 'max': 100, 'step': 1, 'unit_of_measurement': '°C', 'mode': 'box',
                                 'command_topic': f'{PRE}/set/{oid}', 'state_topic': st, 'value_template': f'{{{{ value_json.{top} }}}}'})
        disc('number', 'feuchte_stufe', {'name': t['humidity_level'], 'min': 1, 'max': 10, 'step': 1, 'mode': 'slider',
                                         'command_topic': PRE + '/set/feuchte', 'state_topic': st, 'value_template': '{{ value_json.selectedHumLevel }}'})
        disc('sensor', 'temperatur', {'name': t['temperature'], 'device_class': 'temperature', 'unit_of_measurement': '°C', 'state_class': 'measurement',
                                      'state_topic': st, 'value_template': '{{ value_json.currentTemperature if value_json.isConnected else none }}'})
        disc('sensor', 'feuchte', {'name': t['humidity'], 'device_class': 'humidity', 'unit_of_measurement': '%', 'state_class': 'measurement',
                                   'state_topic': st, 'value_template': '{{ value_json.currentHumidity }}'})
        disc('sensor', 'status', {'name': t['status'], 'icon': 'mdi:information-outline', 'state_topic': st,
                                  'value_template': "{{ value_json.statusMessage or ('%s' if value_json.isReadyForUse else ('%s' if value_json.isPoweredOn else '%s')) }}" % (t['ready_s'], t['heating'], t['off'])})
        disc('binary_sensor', 'verbunden', {'name': t['connected'], 'device_class': 'connectivity', 'state_topic': st,
                                            'value_template': "{{ 'ON' if value_json.isConnected else 'OFF' }}"})
        disc('binary_sensor', 'bereit', {'name': t['ready'], 'icon': 'mdi:check-circle', 'state_topic': st,
                                         'value_template': "{{ 'ON' if value_json.isReadyForUse else 'OFF' }}"})
        disc('sensor', 'hinweis', {'name': t['hint'], 'icon': 'mdi:message-alert-outline', 'state_topic': PRE + '/hinweis'})
        disc('sensor', 'aufgeheizt', {'name': t['heated'], 'icon': 'mdi:percent', 'unit_of_measurement': '%', 'state_topic': st,
                                      'value_template': '{{ value_json.heated_percent }}'})
        disc('sensor', 'prognose', {'name': t['forecast'], 'icon': 'mdi:timer-outline', 'state_topic': st,
                                    'value_template': "{{ value_json.forecast or '–' }}"})
        disc('sensor', 'minuten_bis_bereit', {'name': t['minutes_to_ready'], 'icon': 'mdi:timer-sand', 'unit_of_measurement': 'min',
                                              'state_topic': st, 'value_template': '{{ value_json.minutes_to_ready }}'})


def selftest(path):
    """Learn the device header from the log, then check that every frame is valid and rebuilt 1:1."""
    raws = []
    for z in open(path, errors='ignore'):
        p = z.split(' ')
        if len(p) < 5 or p[2] not in ('M>K', 'K>M'):
            continue
        try:
            raws.append((p[0], bytes.fromhex(p[4])))
        except ValueError:
            continue
    head = next((h for h in (learn_head(r) for _, r in raws) if h), None)
    assert head, 'no valid frame found – header not learned'
    good = 0
    for ts, raw in raws:
        frames, _ = split(raw, head)
        for cmd, d in frames:
            assert frame(cmd, d, head) in raw, f'rebuild differs: {ts}'
            good += 1
    print(f'header learned; {good} frames valid and rebuilt 1:1')


async def main(a):
    import paho.mqtt.client as mqtt
    opt = json.load(open(a.options))
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='klafs_local')
    if opt.get('mqtt_user'):
        cl.username_pw_set(opt['mqtt_user'], opt.get('mqtt_password', ''))
    cl.will_set(PRE + '/verfuegbar', 'offline', retain=True)
    s = Sauna(opt, cl)
    s.loop = asyncio.get_running_loop()
    try:
        s.heat = HeatUp(os.path.join(opt.get('data_dir') or DATA_DIR, 'heatup.db'))
    except (OSError, sqlite3.Error) as e:
        log.warning('heat-up database not available (%s) – no forecast', e)

    def on_connect(c, u, flags, rc, props=None):
        s.discovery()
        c.subscribe(PRE + '/set/#')
        s.publish()
        log.info('MQTT connected (%s)', rc)

    def on_message(c, u, msg):
        kind = msg.topic.rsplit('/', 1)[1]
        fut = asyncio.run_coroutine_threadsafe(s.command(kind, msg.payload.decode()), s.loop)
        fut.add_done_callback(lambda f: f.exception() and s.hint(s.t['failed'].format(a=kind, e=f.exception())))

    cl.on_connect, cl.on_message = on_connect, on_message
    cl.connect(opt.get('mqtt_host', 'core-mosquitto'), int(opt.get('mqtt_port', 1883)), 60)
    cl.loop_start()
    server = await asyncio.start_server(s.connection, '0.0.0.0', int(opt.get('port', 28888)))
    log.info('listening on port %s', opt.get('port', 28888))

    async def watch():   # offline detection, preset → running, queued start / start check
        while True:
            await asyncio.sleep(30)
            try:
                await s.check()
            except Exception as e:
                log.warning('check: %s', e)
            s.publish()
    async with server:
        await asyncio.gather(server.serve_forever(), watch())


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--selftest', metavar='LOG')
    p.add_argument('--options', default='/data/options.json')
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if a.selftest:
        selftest(a.selftest)
    else:
        asyncio.run(main(a))
