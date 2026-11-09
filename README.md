# uptime

External uptime checks for public sites. A GitHub Actions job starts every hour and runs `probe.py`, which GETs each URL in `targets.json` every 5 minutes for ~55 minutes (11 rounds; GitHub fires `*/5` schedules only sporadically on a low-activity repo). Overlapping runs cancel the older one; state is saved after every round. A URL fails when it doesn't answer 2xx within 20 s.

After 2 failures in a row it posts one alert to Discord, and it posts one note when the site recovers. The webhook is the Actions secret `DISCORD_WEBHOOK_URL`.

Tests: `python3 -m unittest discover -s tests`

A failed run (red X in Actions) means the probe itself is broken (bad targets file, cache step, unsent alert), not a site: the sites' state is in the run log and in #alert-bt. A heartbeat on runner-01 alerts #alert-bt when no run is in progress (started < 65 min ago) and none has succeeded for 30 min, or the workflow is disabled.

Nov 8 2026: no scheduled run fired in the first 35 min after the repo was created, though the workflow was active on
main and Actions was enabled (0 runs with event=schedule). Committing a change to the workflow file makes GitHub
register the schedule again. If the schedule stops firing, the heartbeat says so, and the same commit is the first fix.
