# Changelog

## 0.2.1 (docs, 28.10.2026)
- Safety docs: KEY = 1 (key switch) is the recommended default; KEY = 2 only with an approved safety device (SKS/BONATHERM).
- Tested on one installation only (control 18033) – other models untested.
- Liability note: "as far as the law permits"; mandatory statutory liability is not affected.

## 0.2.1
- Safety: retained MQTT commands are ignored and cleared – a stored START can no longer re-start the sauna after a reconnect.
- Safety: start is disabled until you set your own start PIN (at least 6 characters, no trivial sequence); the default 1234 is gone.
- Safety: 5 wrong PINs lock the start for 15 minutes.

## 0.2.0
- Frame header is no longer hard-coded: the 10-byte device header is learned from the module's first frame
  (length, 02/03/04 layout and CRC must match); nothing is sent before it is known. The selftest learns it from the log.
- Start while the module is offline is queued for up to 1 h and sent on the next heartbeat (status "Start waiting
  for Wi-Fi module"); after 3 min the start is checked and resent once, then a message. Stop clears a queued start.
- Heat-up database (`/data/heatup.db`): cabin temperature per minute; forecast "ready at about HH:MM" from minutes
  per kelvin (5-K bands, median of the last 10 runs) – available after the first full heat-up.
- Messages: heating up (+2 K), 50 % heated, ready (with heat-up time), "running 15 min but temperature not rising".
  If the first message comes late (e.g. module was offline), only the highest applicable one is sent.
- While the module is offline the sauna stays "on" if it was running last (status "Wi-Fi module offline (was
  running)") – no false "off" on every reconnect; "ready" needs a connection.
- New sensors: humidity (sanarium only, heartbeat byte 7 – unverified), heated (%), forecast, minutes to ready.
  State JSON adds `raw`, `heated_percent`, `minutes_to_ready`, `forecast`.

## 0.1.0
- First version: local server for the KLAFS Wi-Fi module (login, heartbeat, values, start with device PIN, stop),
  MQTT discovery, start only with start PIN, "now" = now + 1 min (the control needs a start time in the future),
  TCP keepalive after 180 s silence (the module ignores probes), optional frame log (without PIN), optional return route for DNAT setups.
