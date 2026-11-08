# uptime

External uptime checks for public sites. Every 5 minutes a GitHub Actions job runs `probe.py`, which GETs each URL in `targets.json`. A URL fails when it doesn't answer 2xx within 20 s.

After 2 failures in a row it posts one alert to Discord, and it posts one note when the site recovers. The webhook is the Actions secret `DISCORD_WEBHOOK_URL`.

Tests: `python3 -m unittest discover -s tests`

A failed run (red X in Actions) means the probe itself is broken (bad targets file, cache step, unsent alert), not a site: the sites' state is in the run log and in #alert-bt. A heartbeat on runner-01 alerts #alert-bt when no run has succeeded for 30 min or the workflow is disabled.
