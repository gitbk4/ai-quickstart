"""Tests for scripts/telemetry.py — Wave 1C foundation.

Covers privacy invariants + 12 GAPs:

  1. log_event happy path -> JSONL line under 4096 bytes
  2. activity.jsonl missing -> file is created, then logged
  3. Disk full (mock OSError) -> exception swallowed, never raised
  4. >4096 bytes -> fields truncated, _truncated:true added
  5. opted_in user -> event also queued in .pending-telemetry/
  6. opted_out user -> event in activity.jsonl, never queued
  7. unprompted user -> behaves as opted-out (privacy-first)
  8. flush_aggregated success -> {sent: N, retained: 0}
  9. flush_aggregated endpoint down -> URLError -> {sent: 0, retained: N}
 10. flush_aggregated non-2xx -> retain batch, errors list
 11. get_or_create_anonymous_id stable across calls
 12. get_or_create_anonymous_id regenerates when .id deleted
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Make scripts/ importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import telemetry  # noqa: E402


def _make_home(tmpdir: Path) -> Path:
    """Create a fresh ~/.ai-quickstart/ tree under ``tmpdir``."""
    home = tmpdir / "ai-quickstart"
    (home / "persona").mkdir(parents=True, exist_ok=True)
    return home


def _activity_lines(home: Path):
    p = home / "persona" / "activity.jsonl"
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Anonymous ID
# ---------------------------------------------------------------------------


class AnonymousIdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = _make_home(Path(self._tmp.name))

    def test_id_is_16_hex_chars(self):
        anon = telemetry.get_or_create_anonymous_id(self.home)
        self.assertEqual(len(anon), 16)
        # All hex.
        int(anon, 16)

    def test_stable_across_calls(self):
        # GAP 11: deterministic when the .id file is unchanged.
        a = telemetry.get_or_create_anonymous_id(self.home)
        b = telemetry.get_or_create_anonymous_id(self.home)
        c = telemetry.get_or_create_anonymous_id(self.home)
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_regenerates_when_id_file_deleted(self):
        # GAP 12: deleting .id forces a new identity.
        first = telemetry.get_or_create_anonymous_id(self.home)
        id_path = self.home / "persona" / ".id"
        self.assertTrue(id_path.exists())
        id_path.unlink()
        second = telemetry.get_or_create_anonymous_id(self.home)
        self.assertNotEqual(first, second)

    def test_id_seed_is_random_not_derived(self):
        # Privacy invariant: two fresh installs must yield different ids.
        with TemporaryDirectory() as t1, TemporaryDirectory() as t2:
            h1 = _make_home(Path(t1))
            h2 = _make_home(Path(t2))
            self.assertNotEqual(
                telemetry.get_or_create_anonymous_id(h1),
                telemetry.get_or_create_anonymous_id(h2),
            )

    def test_id_seed_file_is_chmod_0600(self):
        telemetry.get_or_create_anonymous_id(self.home)
        id_path = self.home / "persona" / ".id"
        mode = id_path.stat().st_mode & 0o777
        # On some filesystems chmod is best-effort; we accept 0600 or
        # the umask-default but verify it's not world-readable.
        self.assertEqual(mode & 0o077, 0)


# ---------------------------------------------------------------------------
# Opt-in persistence
# ---------------------------------------------------------------------------


class OptInTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = _make_home(Path(self._tmp.name))

    def test_unprompted_default(self):
        self.assertEqual(telemetry.opt_in_status(self.home), telemetry.UNPROMPTED)

    def test_set_opt_in_true(self):
        telemetry.set_opt_in(self.home, True)
        self.assertEqual(telemetry.opt_in_status(self.home), telemetry.OPT_IN)

    def test_set_opt_in_false(self):
        telemetry.set_opt_in(self.home, False)
        self.assertEqual(telemetry.opt_in_status(self.home), telemetry.OPT_OUT)

    def test_malformed_opt_in_file_treated_as_unprompted(self):
        path = self.home / ".telemetry-opt-in"
        path.write_text("not json{{{")
        self.assertEqual(telemetry.opt_in_status(self.home), telemetry.UNPROMPTED)

    def test_opt_in_prompt_default_is_no(self):
        # GAP: privacy-first default. Empty input -> False.
        with mock.patch("builtins.input", return_value=""):
            with mock.patch("builtins.print"):
                self.assertFalse(telemetry.opt_in_prompt())

    def test_opt_in_prompt_y_means_yes(self):
        with mock.patch("builtins.input", return_value="y"):
            with mock.patch("builtins.print"):
                self.assertTrue(telemetry.opt_in_prompt())

    def test_opt_in_prompt_yes_means_yes(self):
        with mock.patch("builtins.input", return_value="YES"):
            with mock.patch("builtins.print"):
                self.assertTrue(telemetry.opt_in_prompt())

    def test_opt_in_prompt_eof_means_no(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            with mock.patch("builtins.print"):
                self.assertFalse(telemetry.opt_in_prompt())


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------


class LogEventTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = _make_home(Path(self._tmp.name))

    def test_happy_path_appends_jsonl_under_4096(self):
        # GAP 1.
        telemetry.log_event(
            self.home,
            "dashboard.launched",
            fields={"duration_ms": 42},
        )
        lines = _activity_lines(self.home)
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertEqual(rec["event_type"], "dashboard.launched")
        self.assertEqual(rec["fields"], {"duration_ms": 42})
        self.assertEqual(rec["version"], telemetry.TELEMETRY_VERSION)
        self.assertEqual(len(rec["anonymous_id"]), 16)
        self.assertIn("ts", rec)

        # File-level: each line under 4096 bytes.
        with open(self.home / "persona" / "activity.jsonl", "rb") as f:
            for raw in f:
                self.assertLessEqual(len(raw), telemetry.ACTIVITY_LINE_MAX)

    def test_creates_file_when_missing(self):
        # GAP 2.
        act_path = self.home / "persona" / "activity.jsonl"
        self.assertFalse(act_path.exists())
        telemetry.log_event(self.home, "persona.heal.started", fields={"trigger": "manual"})
        self.assertTrue(act_path.exists())
        self.assertEqual(len(_activity_lines(self.home)), 1)

    def test_disk_full_swallowed(self):
        # GAP 3: log_event must NEVER raise.
        with mock.patch("telemetry._append_atomic", side_effect=OSError("disk full")):
            try:
                telemetry.log_event(self.home, "dashboard.launched", fields={"duration_ms": 1})
            except Exception as e:
                self.fail(f"log_event raised: {e}")

    def test_oversize_fields_truncated_with_flag(self):
        # GAP 4: fields > 4096 -> truncated to {} with _truncated:true,
        # line still under cap.
        # Build a fields dict that, when serialized, easily blows past 4096.
        big_fields = {"blob_" + str(i): "x" * 100 for i in range(80)}
        telemetry.log_event(self.home, "dashboard.launched", fields=big_fields)
        lines = _activity_lines(self.home)
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertTrue(rec.get("_truncated"))
        self.assertEqual(rec.get("fields"), {})
        with open(self.home / "persona" / "activity.jsonl", "rb") as f:
            for raw in f:
                self.assertLessEqual(len(raw), telemetry.ACTIVITY_LINE_MAX)

    def test_unknown_event_type_silently_dropped(self):
        # Unknown names must NOT land on disk. Privacy + safety: a typo'd
        # event name shouldn't surface arbitrary fields.
        telemetry.log_event(self.home, "totally.made.up", fields={"x": 1})
        self.assertEqual(_activity_lines(self.home), [])

    def test_opted_in_also_queues_for_aggregation(self):
        # GAP 5.
        telemetry.set_opt_in(self.home, True)
        telemetry.log_event(self.home, "dashboard.launched", fields={"duration_ms": 7})
        # local activity present
        self.assertEqual(len(_activity_lines(self.home)), 1)
        # pending batch present
        pending_dir = self.home / "persona" / ".pending-telemetry"
        self.assertTrue(pending_dir.is_dir())
        batches = list(pending_dir.glob("batch-*.jsonl"))
        self.assertEqual(len(batches), 1)
        with open(batches[0], "r", encoding="utf-8") as f:
            queued = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["event_type"], "dashboard.launched")

    def test_opted_out_does_not_queue(self):
        # GAP 6.
        telemetry.set_opt_in(self.home, False)
        telemetry.log_event(self.home, "dashboard.launched", fields={"duration_ms": 7})
        # local file written
        self.assertEqual(len(_activity_lines(self.home)), 1)
        # NO pending batches
        pending_dir = self.home / "persona" / ".pending-telemetry"
        if pending_dir.exists():
            self.assertEqual(list(pending_dir.glob("batch-*.jsonl")), [])

    def test_unprompted_does_not_queue(self):
        # GAP 7: privacy-first default — unprompted = opted_out for POST.
        self.assertEqual(telemetry.opt_in_status(self.home), telemetry.UNPROMPTED)
        telemetry.log_event(self.home, "dashboard.launched", fields={"duration_ms": 7})
        # local file written
        self.assertEqual(len(_activity_lines(self.home)), 1)
        pending_dir = self.home / "persona" / ".pending-telemetry"
        if pending_dir.exists():
            self.assertEqual(list(pending_dir.glob("batch-*.jsonl")), [])

    def test_record_never_contains_path_or_user_data(self):
        # Privacy invariant: record fields are constrained.
        telemetry.log_event(
            self.home,
            "persona.heal.committed",
            fields={"paragraph_count": 3, "locked_count": 1, "duration_ms": 250},
        )
        rec = _activity_lines(self.home)[0]
        # Never any of these:
        for forbidden in ("path", "cwd", "file", "user", "hostname", "prose"):
            self.assertNotIn(forbidden, rec)
            self.assertNotIn(forbidden, rec.get("fields", {}))


# ---------------------------------------------------------------------------
# flush_aggregated
# ---------------------------------------------------------------------------


class FlushAggregatedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = _make_home(Path(self._tmp.name))
        telemetry.set_opt_in(self.home, True)

    def _seed_batch(self, name: str, count: int = 3) -> Path:
        d = self.home / "persona" / ".pending-telemetry"
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        with open(path, "w", encoding="utf-8") as f:
            for i in range(count):
                f.write(
                    json.dumps(
                        {
                            "ts": "2026-05-02T00:00:00Z",
                            "event_type": "dashboard.launched",
                            "anonymous_id": "abc1234567890def",
                            "version": "v1",
                            "fields": {"duration_ms": i},
                        }
                    )
                    + "\n"
                )
        return path

    def _mock_response(self, status: int = 200):
        m = mock.MagicMock()
        m.__enter__ = mock.MagicMock(return_value=m)
        m.__exit__ = mock.MagicMock(return_value=False)
        m.status = status
        m.getcode = mock.MagicMock(return_value=status)
        return m

    def test_success_path(self):
        # GAP 8: mocked urlopen returns 200 -> file deleted, sent>=1.
        self._seed_batch("batch-2026-05-01.jsonl", count=4)
        with mock.patch(
            "telemetry.urllib.request.urlopen",
            return_value=self._mock_response(200),
        ):
            result = telemetry.flush_aggregated(self.home)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["retained"], 0)
        self.assertEqual(result["errors"], [])
        # File deleted.
        self.assertFalse((self.home / "persona" / ".pending-telemetry" / "batch-2026-05-01.jsonl").exists())

    def test_endpoint_down_urlerror_retained(self):
        # GAP 9.
        self._seed_batch("batch-2026-05-01.jsonl", count=4)
        with mock.patch(
            "telemetry.urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns down"),
        ):
            result = telemetry.flush_aggregated(self.home)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["retained"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("urlerror", result["errors"][0])
        # File still present.
        self.assertTrue(
            (self.home / "persona" / ".pending-telemetry" / "batch-2026-05-01.jsonl").exists()
        )

    def test_non_2xx_retains_batch(self):
        # GAP 10.
        self._seed_batch("batch-2026-05-01.jsonl", count=2)
        with mock.patch(
            "telemetry.urllib.request.urlopen",
            return_value=self._mock_response(500),
        ):
            result = telemetry.flush_aggregated(self.home)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["retained"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("http-500", result["errors"][0])
        self.assertTrue(
            (self.home / "persona" / ".pending-telemetry" / "batch-2026-05-01.jsonl").exists()
        )

    def test_http_error_exception(self):
        # urllib often raises HTTPError for 4xx/5xx; cover that path too.
        self._seed_batch("batch-2026-05-01.jsonl", count=1)
        http_err = urllib.error.HTTPError(
            telemetry.TELEMETRY_ENDPOINT, 503, "Service Unavailable", {}, None
        )
        with mock.patch("telemetry.urllib.request.urlopen", side_effect=http_err):
            result = telemetry.flush_aggregated(self.home)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["retained"], 1)
        self.assertIn("http-503", result["errors"][0])

    def test_does_not_post_current_batch(self):
        # The today-stamped batch is held back so in-progress writes aren't lost.
        today_name = f"batch-{telemetry._utcnow_date_key()}.jsonl"
        self._seed_batch(today_name, count=2)
        with mock.patch("telemetry.urllib.request.urlopen") as m:
            result = telemetry.flush_aggregated(self.home)
        m.assert_not_called()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["retained"], 1)

    def test_opt_out_skips_post(self):
        # If user has revoked, do not POST anything; just return retained.
        self._seed_batch("batch-2026-05-01.jsonl", count=2)
        telemetry.set_opt_in(self.home, False)
        with mock.patch("telemetry.urllib.request.urlopen") as m:
            result = telemetry.flush_aggregated(self.home)
        m.assert_not_called()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["retained"], 1)

    def test_no_pending_batches_is_clean(self):
        result = telemetry.flush_aggregated(self.home)
        self.assertEqual(result, {"sent": 0, "retained": 0, "errors": []})

    def test_multiple_batches_partial_success(self):
        # Two old batches, one succeeds, one fails.
        self._seed_batch("batch-2026-04-30.jsonl", count=2)
        self._seed_batch("batch-2026-05-01.jsonl", count=2)
        responses = [self._mock_response(200), urllib.error.URLError("blip")]

        def side_effect(req, timeout):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("telemetry.urllib.request.urlopen", side_effect=side_effect):
            result = telemetry.flush_aggregated(self.home)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["retained"], 1)
        self.assertEqual(len(result["errors"]), 1)


# ---------------------------------------------------------------------------
# Wire shape (privacy-relevant)
# ---------------------------------------------------------------------------


class WireShapeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = _make_home(Path(self._tmp.name))

    def test_wire_payload_is_only_whitelisted_keys(self):
        # When the POST body is built, every event in the body has the
        # exact whitelist of top-level keys.
        telemetry.set_opt_in(self.home, True)
        telemetry.log_event(
            self.home,
            "dashboard.pane.viewed",
            fields={"pane": "personas", "duration_ms": 1200},
        )
        # Force a "yesterday" filename so flush_aggregated will actually POST.
        pending = self.home / "persona" / ".pending-telemetry"
        today_path = pending / f"batch-{telemetry._utcnow_date_key()}.jsonl"
        renamed = pending / "batch-2026-04-30.jsonl"
        today_path.rename(renamed)

        captured: dict = {}

        def fake_urlopen(req, timeout):
            captured["body"] = req.data
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            m = mock.MagicMock()
            m.__enter__ = mock.MagicMock(return_value=m)
            m.__exit__ = mock.MagicMock(return_value=False)
            m.status = 200
            return m

        with mock.patch("telemetry.urllib.request.urlopen", side_effect=fake_urlopen):
            telemetry.flush_aggregated(self.home)

        self.assertIn("body", captured)
        self.assertEqual(captured["url"], telemetry.TELEMETRY_ENDPOINT)
        body = json.loads(captured["body"])
        self.assertIn("events", body)
        self.assertEqual(len(body["events"]), 1)
        evt = body["events"][0]
        self.assertEqual(
            sorted(evt.keys()),
            ["anonymous_id", "event_type", "fields", "ts", "version"],
        )
        # No PII smuggled into top-level.
        for forbidden in ("user", "host", "ip", "path", "cwd", "file"):
            self.assertNotIn(forbidden, evt)


if __name__ == "__main__":
    unittest.main()
