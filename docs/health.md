# Health

A container that restarts, stops or turns unhealthy is something you hear about, not something you find in
`docker ps`. Like the backup, the looking is done by the host and the telling by the router.

Every two minutes `retinue-health.timer` runs `/usr/local/lib/retinue/retinue-health.py` as root. It asks Docker
for each container of the `retinue` project (not `compose run`'s one-offs): its state, its healthcheck's verdict,
how many times Docker restarted it, since when it runs — those fields only, never the container's environment —
and writes them to `/srv/retinue/status/health.json`. The router reads that file through the same read-only mount
as the backup's and sends you one message when something goes bad and one when it is well again:

```
Server: not OK.
- assistant: not responding (healthcheck: unhealthy)
- egress: stopped, code 137, out of memory

Server: OK again — assistant, egress.
```

- Down (exited, restarting, paused, created) or unhealthy is told after **two checks in a row**: a deploy that
  recreates the containers is caught in between for one check at most. Well again — after two good checks in a
  row, so a healthcheck that flaps is not told as recovered at each good one.
- A **restart by Docker** — the restart count grew on the same container — is told at once; when the container is
  running again it is one line, with nothing to recover from. A new container with a count of 0 is a deploy; a
  container gone between the host's `ps` and `inspect` (a deploy again) is skipped.
- **One notice per container an hour** at most: an OOM restart loop or a flapping healthcheck is told once and then
  counted for the morning summary («failures in a day — 10 (egress)»), not sent every few minutes. A bad state that
  went untold this way has its recovery untold too.
- While something stays bad, **nothing is said again**, whatever changes. What was told is kept in the router's
  SQLite, so a restarted router does not tell it twice — and a router that crashed tells of its own restart once
  it is back.
- Stopped with **exit code 0** is your own `docker stop`: told once as «stopped manually», named apart in the
  summary, never counted as a failure.
- **Your quiet hours** (the assistant's, `/initiative quiet 23-8`; [proactive.md](proactive.md)) hold every notice
  but an outage, and the held ones come as one message «Server during quiet hours:» with their times when they end.
  An outage is told at once, night or not: Docker not answering the host or showing no container at all, a stale
  `health.json`, the router or the platform down.
- A `health.json` **older than ten minutes** is told too: the timer has stopped, and nothing else would say so.

The morning summary has a line: `containers: all 17 OK`, or which are not, and how many went bad in the last
day.

## What it cannot tell

**The router down.** It is the router that holds the bot token; the host holds none, by design — a host job with
the token would be a second place to steal it from, outside Dokploy's environment. So the host script sends nothing
itself, and a router that is down cannot be told by anyone. You learn it from the morning summary that does not come
(it always comes, bare if the assistant fails), and a router that comes back tells you of its restart.

**The whole server down**, for the same reason; an outside uptime check is the answer to that, if one is wanted.

## Turn it on

Nothing to turn on: `setup.sh` installs the script and both units and enables the timer on every run, with no
switch — it holds no secret. To see it work:

```bash
systemctl list-timers retinue-health.timer
sudo systemctl start retinue-health && cat /srv/retinue/status/health.json
journalctl -u retinue-health --since today
```
