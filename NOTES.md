# Notes

## Phone access when laptop is off/asleep (2026-07-22)

**Problem:** The app only exists while `server.py` is running on the laptop.
The phone connects to it over Tailscale (e.g. `http://100.66.235.86:5112/`).
If the laptop is off or asleep, the phone has nothing to connect to — no
code change can fix that; it needs an always-on host somewhere.

**Options considered:**

1. **Raspberry Pi at home** (leaning this way) — cheap (~$40-80), sips
   power, can run 24/7 forever. Run the same `server.py` on it, join it to
   the same Tailscale network, phone hits the Pi's Tailscale address
   instead of the laptop's. One-time setup: flash OS, install
   Python/Flask deps, copy the repo over, install/join Tailscale, set the
   server to start on boot (systemd service). Data stays on hardware you
   own.

2. **Cloud VPS** (~$5-6/mo, e.g. DigitalOcean/Fly.io/Railway) — no
   hardware to buy, reachable from anywhere (not just home network), and
   can be set up remotely without any physical shopping/hardware step.
   Ongoing monthly cost; budget data lives on someone else's server
   (still private, just a different trust model).

3. **Keep laptop always on** — zero new setup/cost, but laptops aren't
   built for 24/7 uptime (OS updates reboot it, lid-closing sleeps it
   unless power settings changed), and it's wasteful to keep a full
   laptop running just to serve a small budget app.

**Status:** Undecided — user is weighing Pi vs. VPS vs. always-on laptop.
Branch is named `raspberry-pi-dev-board`, suggesting a Pi was already in
mind before this conversation.

**Next step when resumed:** ask which option the user wants, then either:
- write Pi setup steps/systemd service file + Tailscale join instructions, or
- provision a VPS and deploy `server.py` there, or
- just adjust Windows power settings on the laptop.
