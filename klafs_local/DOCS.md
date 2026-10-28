# KLAFS Sauna local – documentation

> Independent hobby project – **not** affiliated with or endorsed by KLAFS. Use at your own risk, no liability.
> Only switch the sauna on when someone is at home, check the cabin before every start, never bypass the safety devices
> of the control. See the safety section in the [README](../README.md).

## How it works

The KLAFS Wi-Fi module keeps a TCP connection to the KLAFS cloud on port 28888 and sends a heartbeat every minute.
This app listens on the same port, answers like the cloud and publishes the sauna to Home Assistant via MQTT.

### Connect the module to the app

- **Module setting** (easiest): enter the IP of Home Assistant as server address, port 28888.
- **DNAT** (if the module ignores the setting and connects to the cloud address): on the router redirect TCP
  `<cloud IP>:28888` from the module to `<Home Assistant IP>:28888`. Because the answer must go back via the router,
  set *Module IP* and *Router IP* in the app options – the app then adds the return route.

### Sauna control settings

- Service menu **20 KEY**: safe default **1** = a key switch must confirm every start (key symbol in the display, also
  for starts on the panel). **2** ("remote", no key) **only** with an approved safety device (SKS over the heater or
  BONATHERM) and if the manual of your control/heater allows remote start. 0 = remote start is only "requested"
  (state 1).
- The control takes the time sent with the values as **start time**. A start time in the past makes it wait until the
  next day – the app always sends now + 1 minute (or the preset time you choose).
- Remote switch-on is only allowed with a safety device (SKS, BONATHERM or key switch) – see README.

## Features

- **Start with PIN** (lock "Start (PIN)" or the keypad card), optional preset time; **stop** with the switch or by
  locking the lock. Stopping at the control panel always works.
- **Pending start**: if you start while the Wi-Fi module is offline, the start is queued for up to **1 h** (status
  "Start waiting for Wi-Fi module") and sent with the next heartbeat. Without a heartbeat within 1 h you get the
  message "not reachable for 1 h – please try again". A stop clears a queued start.
- **Start check**: 3 min after a start the app checks that the sauna runs (or shows the preset). If not, the start is
  sent once more; if it still does not run, a message asks you to check the control (key switch / KEY setting).
- **Heat-up forecast**: every heat-up is stored per minute in `/data/heatup.db`. From the last 10 runs the app learns
  the minutes per kelvin in 5-K bands (median) and shows *Forecast* ("ready at about HH:MM (X min left)"),
  *Minutes to ready* and *Heated* (%). Before the first complete heat-up to "ready" the forecast says
  "forecast after first full heat-up". "Ready" = set temperature − 3 K.
- **Messages** (sensor *Last message*, e.g. for push notifications): heating up (+2 K), 50 % heated, ready (with
  heat-up time), "running 15 min but temperature not rising". If the first message would come late (e.g. the module
  was offline and the cabin is already half heated), only the highest applicable message is sent.
- **Humidity** sensor: heartbeat byte 7, only in sanarium mode – a candidate, not yet verified.

### Offline behaviour

The module's Wi-Fi drops now and then and reconnects by itself. **After a remote start the module often drops the
connection for about 10–15 minutes** and then reconnects – the sauna keeps heating. The app reports *offline* only
after 10 min without heartbeat; while offline the sauna stays "on" if it was running last (status "Wi-Fi module
offline (was running)"), so you do not get a false "off". "Ready" and the cabin temperature need a connection.
Commands other than a queued start fail while offline – stopping at the control panel always works.

## Protocol

```
frame  HEADER (10 B) | LEN (2 B LE) | 02 | CMD (2 B LE) DATA… | 03 | CRC (2 B LE) | 04
HEADER = 10-byte device header (learned from the module, not hard-coded)
LEN = length of CMD+DATA · CRC-16/MCRF4XX (poly 0x1021 reflected, init 0xFFFF, no xor) over header..03
```

The header may be specific to the device, so the app learns it from the module's first frame (a position where
LEN, `02`, `03`, `04` and the CRC of a complete frame match) and uses it for all frames it sends. Nothing is sent
before the header is known – the module always speaks first (login or heartbeat).

| CMD | direction | data |
|---|---|---|
| 0x1001 login | module → server, answer server | answer `00 a0 02` + Unix time (4 B LE) + 4×00 (sets the clock), then 0x1020 |
| 0x1002 heartbeat | module → server, every minute | 15 B: [1] mode 1 sauna / 2 sanarium · [2] state 0 off / 1 requested / 2 program active or preset · [3] set °C · [6] cabin °C · [7] humidity (candidate, sanarium) · [12..13] start time (00:00 = none); answer `00` |
| 0x1020 status | server → module | answer 63 B (not decoded) |
| 0x1021 values | server → module | mode, set °C, 00, humidity level (0–10), start hour, start minute (local time) |
| 0x1022 start | server → module | device PIN as ASCII |
| 0x1023 stop | server → module | – |

The module's Wi-Fi sometimes drops and reconnects; the app notices a dead connection after ~4–5 min (TCP keepalive after 180 s silence – the module does not answer keepalive probes, a shorter time drops every connection) and
counts reconnects per day (`connects_today` in the state JSON).

## MQTT

State `klafs/sauna/zustand` (JSON), availability `klafs/sauna/verfuegbar`, messages `klafs/sauna/hinweis`,
commands `klafs/sauna/set/{power,start,modus,temp_sauna,temp_sanarium,feuchte}`.
`set/start` takes `{"aktion": "START", "code": "<start PIN>", "zeit": "HH:MM"}` (zeit optional = preset),
`{"aktion": "STOP"}` stops. The state JSON also carries `heated_percent`, `minutes_to_ready`, `forecast` and `raw`
(last heartbeat data).

## Options

| option | meaning |
|---|---|
| language | en / de for entity names and messages |
| device_pin | PIN of the control (factory 1234), sent with each start |
| start_pin | PIN for Home Assistant (lock "Start (PIN)"). **Empty by default – starting is disabled until you set your own PIN (at least 6 characters, no trivial sequence).** 5 wrong PINs lock the start for 15 minutes. |
| port | 28888 |
| module_ip, router_ip | only for DNAT: return route |
| log_frames | write all frames (PIN masked) to /share/klafs_local |

## Changes 0.2.1
- Retained MQTT commands are ignored and cleared (a stored start command can no longer re-start the sauna after a reconnect).
- Start is disabled until an own start PIN (≥ 6 characters) is configured; the former default 1234 is no longer accepted.
- 5 wrong PINs lock the start for 15 minutes.
