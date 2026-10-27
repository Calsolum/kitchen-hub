"""CLI tests. They never reach the real brewer: every run uses a bogus address and a
temporary state directory, and only paths that return before any Bluetooth use."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from teamosa import cli

BOGUS = ["--address", "00:00:00:00:00:00", "--attempts", "1"]

try:
    import fcntl  # noqa: F401
    HAVE_FCNTL = True
except ImportError:
    HAVE_FCNTL = False


class DryRun(unittest.TestCase):
    def test_prints_frame(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["brew", "--temp", "60", "--time", "3", "--dry-run"] + BOGUS)
        self.assertEqual(code, 0)
        self.assertIn("TEA60,6,25U", out.getvalue())
        self.assertIn("54454136302c362c323555", out.getvalue())

    def test_out_of_range_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["brew", "--temp", "100", "--time", "3", "--dry-run"] + BOGUS)
        self.assertEqual(cm.exception.code, 2)

    def test_missing_settings_is_a_usage_error(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TEAMOSA_TEMP", "TEAMOSA_TIME")}
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["brew", "--dry-run"] + BOGUS)
        self.assertEqual(cm.exception.code, 2)


@unittest.skipUnless(HAVE_FCNTL, "locking needs Linux")
class Guards(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = ["--state-dir", self.dir.name]

    def tearDown(self):
        self.dir.cleanup()

    def _write_runs(self, *records):
        with open(os.path.join(self.dir.name, "runs.jsonl"), "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_too_soon_after_a_brew(self):
        self._write_runs({"ts": time.time() - 60, "outcome": "brewed"})
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["brew", "--temp", "60", "--time", "3"] + BOGUS + self.state)
        self.assertEqual(code, cli.EXIT_TOO_SOON)

    def test_uncertain_write_also_counts(self):
        self._write_runs({"ts": time.time() - 60, "outcome": "write_unknown"})
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["brew", "--temp", "60", "--time", "3"] + BOGUS + self.state)
        self.assertEqual(code, cli.EXIT_TOO_SOON)

    def test_skipped_runs_do_not_count(self):
        self._write_runs({"ts": time.time() - 60, "outcome": "not_ready"})
        self.assertIsNone(cli._last_brew_ts(cli.Path(self.dir.name)))

    def test_lock_held(self):
        held = cli._lock(cli.Path(self.dir.name))
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["check"] + BOGUS + self.state)
            self.assertEqual(code, cli.EXIT_LOCKED)
        finally:
            held.close()


if __name__ == "__main__":
    unittest.main()
