import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import probe  # noqa: E402

HOOK = "https://discord.example/api/webhooks/1/secret-token"


class Resp(io.BytesIO):
    def __init__(self, status=200, body=b"{}"):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(codes, sent):
    """codes: url-prefix -> HTTP status, or an exception to raise; Discord posts land in sent."""

    def opener(req, timeout):
        if req.full_url.startswith(HOOK):
            if isinstance(codes.get("discord"), Exception):
                raise codes["discord"]
            sent.append(json.loads(req.data)["content"])
            return Resp(200, b'{"id": "42"}')
        for prefix, code in codes.items():
            if req.full_url.startswith(prefix):
                if isinstance(code, Exception):
                    raise code
                if code >= 400:
                    raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
                return Resp(code)
        raise AssertionError(req.full_url)

    return opener


class TargetsTest(unittest.TestCase):
    def test_targets_file_is_valid_and_covers_every_app(self):
        targets = probe.load_targets(Path(probe.__file__).parent / "targets.json")
        hosts = {t["url"].split("/")[2] for t in targets}
        self.assertEqual(
            hosts,
            {"bigtape.app", "getwelo.app", "mygiven.app", "bigearnings.ai", "api.bigearnings.ai",
             "psar.superbigcompany.com"},
        )
        self.assertEqual(len({t["name"] for t in targets}), len(targets))

    def test_non_https_target_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text('[{"name": "x", "url": "http://x"}]')
            with self.assertRaises(ValueError):
                probe.load_targets(p)


class ProbeTest(unittest.TestCase):
    def test_ts_placeholder_busts_cache(self):
        seen = []

        def opener(req, timeout):
            seen.append(req.full_url)
            return Resp(200)

        self.assertEqual(probe.probe("https://a/robots.txt?uptime={ts}", opener, now=lambda: 123), (True, "HTTP 200"))
        self.assertEqual(seen, ["https://a/robots.txt?uptime=123"])

    def test_http_error_and_no_answer_fail(self):
        ok, detail = probe.probe("https://a", fake_opener({"https://a": 503}, []))
        self.assertEqual((ok, detail), (False, "HTTP 503"))
        ok, detail = probe.probe("https://a", fake_opener({"https://a": urllib.error.URLError(TimeoutError())}, []))
        self.assertFalse(ok)
        self.assertIn("no answer", detail)


class StepTest(unittest.TestCase):
    def test_alert_once_at_second_failure_then_recovery(self):
        down = {"A": (False, "HTTP 500")}
        s, m = probe.step({}, down)
        self.assertEqual((s, m), ({"A": 1}, []))
        s, m = probe.step(s, down)
        self.assertEqual(s, {"A": 2})
        self.assertEqual(len(m), 1)
        self.assertIn("A is DOWN", m[0][1])
        s, m = probe.step(s, down)
        self.assertEqual((s, m), ({"A": 3}, []))
        s, m = probe.step(s, {"A": (True, "HTTP 200")})
        self.assertEqual(s, {"A": 0})
        self.assertIn("back up", m[0][1])

    def test_single_failure_recovers_silently_and_removed_targets_drop(self):
        s, m = probe.step({"A": 1, "gone": 5}, {"A": (True, "HTTP 200")})
        self.assertEqual((s, m), ({"A": 0}, []))


class MainTest(unittest.TestCase):
    def run_main(self, codes, state, env_hook=HOOK, extra_env=None, sleep=None):
        sent = []
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "state.json"
            sp.write_text(json.dumps(state))
            env = {"UPTIME_STATE": str(sp), **(extra_env or {})}
            if env_hook:
                env["DISCORD_WEBHOOK_URL"] = env_hook
            out = io.StringIO()
            err = io.StringIO()
            old = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            try:
                rc = probe.main(opener=fake_opener(codes, sent), environ=env, sleep=sleep or (lambda s: None))
            finally:
                sys.stdout, sys.stderr = old
            return rc, json.loads(sp.read_text()), sent, out.getvalue() + err.getvalue()

    def all_up(self, **over):
        codes = {"https://": 200}
        codes.update(over)
        return dict(sorted(codes.items(), key=lambda kv: -len(kv[0])))

    def test_second_failure_alerts_discord(self):
        rc, state, sent, out = self.run_main(self.all_up(**{"https://bigtape.app": 502}), {"BigTape": 1})
        self.assertEqual(rc, 0)
        self.assertEqual(state["BigTape"], 2)
        self.assertEqual(len(sent), 1)
        self.assertIn("BigTape is DOWN", sent[0])
        self.assertNotIn("secret-token", out)

    def test_missing_webhook_fails_run_and_retries_next_time(self):
        rc, state, sent, _ = self.run_main(self.all_up(**{"https://bigtape.app": 502}), {"BigTape": 1}, env_hook=None)
        self.assertEqual((rc, state["BigTape"], sent), (1, 1, []))

    def test_discord_refusal_never_prints_the_webhook(self):
        codes = self.all_up(**{"https://bigtape.app": 502})
        codes["discord"] = urllib.error.HTTPError(HOOK + "?wait=true", 401, "no", {}, None)
        rc, state, _, out = self.run_main(codes, {"BigTape": 1})
        self.assertEqual((rc, state["BigTape"]), (1, 1))
        self.assertIn("Discord refused", out)
        self.assertNotIn("secret-token", out)
        self.assertNotIn("discord.example", out)

    def test_all_up_sends_nothing(self):
        rc, state, sent, _ = self.run_main(self.all_up(), {})
        self.assertEqual((rc, sent), (0, []))
        self.assertTrue(all(v == 0 for v in state.values()))


    def test_rounds_loop_sleeps_the_remaining_interval_and_alerts_once(self):
        # 3 rounds, site down throughout: fails at round 1 (count 1), alerts at round 2, stays silent at round 3
        slept = []
        rc, state, sent, out = self.run_main(
            self.all_up(**{"https://bigtape.app": 502}), {},
            extra_env={"UPTIME_ROUNDS": "3", "UPTIME_INTERVAL_SEC": "60"}, sleep=slept.append)
        self.assertEqual((rc, state["BigTape"], len(sent)), (0, 3, 1))
        self.assertEqual(len(slept), 2)  # no sleep after the last round
        self.assertIn("round 3/3", out)

    def test_round_sleep_subtracts_probe_time_and_never_goes_negative(self):
        slept = []
        ticks = iter([0, 7, 100, 190, 200])  # round starts/ends: probing took 7 s, then 90 s (longer than the interval)
        with tempfile.TemporaryDirectory() as d:
            env = {"UPTIME_STATE": str(Path(d) / "s.json"), "UPTIME_ROUNDS": "3", "UPTIME_INTERVAL_SEC": "60"}
            old = sys.stdout
            sys.stdout = io.StringIO()
            try:
                rc = probe.main(opener=fake_opener(self.all_up(), []), environ=env, sleep=slept.append,
                                clock=lambda: next(ticks))
            finally:
                sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertEqual(slept, [53.0, 0.0])


class WorkflowTest(unittest.TestCase):
    def test_workflow_is_locked_down(self):
        wf = (Path(probe.__file__).parent / ".github" / "workflows" / "uptime.yml").read_text()
        self.assertIn("cron: '0 * * * *'", wf)
        self.assertIn('UPTIME_ROUNDS: "11"', wf)
        self.assertIn('UPTIME_INTERVAL_SEC: "300"', wf)
        self.assertIn("timeout-minutes: 65", wf)
        self.assertIn("cancel-in-progress: true", wf)
        self.assertIn("workflow_dispatch", wf)
        self.assertNotIn("pull_request", wf)
        self.assertIn("contents: read", wf)
        for line in wf.splitlines():
            if "uses:" in line:
                self.assertRegex(line, r"uses: [\w./-]+@[0-9a-f]{40}\b")


if __name__ == "__main__":
    unittest.main()
