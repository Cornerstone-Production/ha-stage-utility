# Stage Utility for Home Assistant

Brings a [Stage Utility](https://github.com/Cornerstone-Production/stage-utility)
appliance into Home Assistant as switches and buttons. Stage Utility runs on the
local network, so nothing here touches the internet and nothing is polled while
the server has something to say.

## What it needs

- Home Assistant 2025.2 or newer.
- A Stage Utility server reachable on the network, version 1.15 or newer (the
  cue manifest and the `cues` event channel).
- A **cue token**, minted in Stage Utility under **Settings → Cues**. The secret
  is shown once, when it is minted, and never again.

## Install

Through [HACS](https://hacs.xyz), as a custom repository:

1. HACS → the three-dot menu → **Custom repositories**.
2. Repository `https://github.com/Cornerstone-Production/ha-stage-utility`,
   type **Integration**. Add.
3. Find **Stage Utility** in HACS, download it, restart Home Assistant.

Or by hand: copy `custom_components/stage_utility` into your `config/custom_components`
directory and restart.

## Set it up

**Settings → Devices & services → Add integration → Stage Utility.**

| Field | What to type |
|---|---|
| Host | `192.168.16.61`, `192.168.16.61:8788` or `http://stage-utility:8788`. A bare address gets plain HTTP on port 8788, which is where every Stage Utility install starts. |
| Cue token | The `su_…` secret from Settings → Cues. |

The flow reads the cue manifest to prove it found a Stage Utility, then posts to
a cue name that cannot exist to prove the token is accepted — a `404` means the
token was fine, a `401` means it was not. Nothing on stage is pressed to find
out.

One entry per server. The entry is keyed by the server's own LAN address, so
adding the same appliance a second time by another name is recognised and
refused rather than doubling every switch.

## What appears

Everything lands on one device named after the server, with a link back to its
web page.

**A switch per cue pair.** A pair is an ON cue and an OFF cue — projectors,
house lights, a plug. `Turn on` calls the ON cue; `Turn off` calls the OFF cue.
The room named in Stage Utility is suggested as the Home Assistant area.

**A button per single cue.** A cue with nothing to turn off — reset a router,
fire a macro. Pressing it calls the cue.

Both go **unavailable** when the Companion button behind the cue is missing from
Companion's export: pressing it would do nothing at all, and being told that is
better than a control that lies.

### How state works

A cue pair only knows what its gear is doing if it has been given a **state
source** in Stage Utility — a Companion custom variable that the operator's own
buttons set. With one, the switch reports what the gear says, and follows it when
somebody turns the projector off at the wall.

Without one, the switch reads **unknown** and turns on `assumed_state`, so the
card shows separate on and off buttons rather than a toggle that would be
guessing. The `reason` attribute says why in a sentence.

Each switch also carries:

| Attribute | What it is |
|---|---|
| `reason` | Why the state is unknown, when it is |
| `state_source` | The Companion variable the state is read from, or `null` |
| `toggle` | Whether the pair is driven by a single toggling button |
| `cue_on` / `cue_off` | The cue names this switch calls |

### How it stays current

Stage Utility carries everything on one Server-Sent Events stream, and it only
reads the gear behind a cue **while somebody is subscribed to the `cues`
channel**. So this integration holds exactly one subscription open for the life
of the config entry and never polls a healthy server. State changes and changes
to the cue list both arrive on that stream.

When the stream drops — the appliance rebooted, the network blinked — it
reconnects with a backoff from 1 s to 60 s, and meanwhile falls back to reading
`/api/cues/states` every 30 seconds, backing that off too if the server stays
away. The moment the stream is back, the polling stops and the manifest is read
again, because nothing is replayed for the time the socket was dead.

Connects and disconnects are logged at INFO, once per change rather than once
per attempt.

## When a cue is refused

Stage Utility refuses a cue for reasons that are about the building, not about
Home Assistant, and it answers with a sentence meant to be read by a person.
That sentence is what surfaces — in the UI, in the automation trace, in the log:

| Reason | Reads as |
|---|---|
| `service-live` | "The Gospel Way is live" |
| `cooldown` | The cue was called again too soon |
| `button-missing` | The Companion button behind the cue is gone; nothing was pressed |
| `condition-not-met` | A condition on the rule said no |
| `disabled` | The rule is switched off |
| `disarmed` | The cue is disarmed |
| `once-per-service` | It has already run this service |
| `planning-center-unknown` | Stage Utility cannot tell whether a service is live, so it will not risk it |

A refusal raises, so an automation sees a failure rather than quietly carrying
on. It changes nothing about the switch's state: nothing happened.

**`skipped` is success.** When Stage Utility can already see the device is in the
state being asked for, it answers `200` with `skipped: true` and presses nothing.
The switch is where it was asked to be, so that is not an error.

**Two-step cues are confirmed automatically.** Some cues answer `202` and ask to
be called again with a token, so a voice assistant has to say "are you sure".
Home Assistant has nobody left to ask — the operator already pressed the switch,
or an automation already decided — so this integration sends the confirmation
straight back. Bear that in mind before exposing a destructive two-step cue to a
dashboard.

## Troubleshooting

**Diagnostics** — Settings → Devices & services → Stage Utility → the three-dot
menu → **Download diagnostics**. It carries the manifest, every cue's last known
state, and whether the event stream is up and what killed it last. The cue token
is redacted.

**Debug logging**:

```yaml
logger:
  logs:
    custom_components.stage_utility: debug
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest -q
```

Branching, the commit convention, and how a push to `beta` or `main` becomes a
release are in [docs/contributing.md](docs/contributing.md).

## Licence

GPL-3.0-or-later, matching Stage Utility. See [LICENSE](LICENSE).
