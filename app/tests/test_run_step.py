import unittest

from gi.repository import GLib

from framewise import export


class RunStepTests(unittest.TestCase):
    def _run(self, cmd):
        loop, result = GLib.MainLoop(), {}

        def then(ok, message):
            result.update(ok=ok, message=message)
            loop.quit()
        export._run_step(export.SaveJob(None),
                         cmd, 0, None, then)
        GLib.timeout_add_seconds(20, loop.quit)
        loop.run()
        return result

    def test_lots_of_error_output_does_not_block(self):
        # Some ffmpeg versions print a line per frame: more than the pipe holds.
        r = self._run(["sh", "-c", "yes 'warning: something' | head -c 300000 >&2; echo last >&2"])
        self.assertEqual(r.get("ok"), True, r)
        self.assertTrue(r["message"].endswith("last"), r["message"][-80:])

    def test_failure_message(self):
        r = self._run(["sh", "-c", "echo 'it broke' >&2; exit 1"])
        self.assertEqual(r, {"ok": False, "message": "it broke"})


if __name__ == "__main__":
    unittest.main()
