"""Smoke tests for romanization, caption formatting, and core API routes."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as app_module


class RomanizationTests(unittest.TestCase):
    def test_basic_phrase(self):
        self.assertEqual(
            app_module.devanagari_to_roman("अभी कैसे हो बढ़िया सब"),
            "abhi kaise ho badhiya sab",
        )

    def test_anusvara_becomes_casual_n(self):
        self.assertEqual(app_module.devanagari_to_roman("करेंगे"), "karenge")
        self.assertEqual(app_module.devanagari_to_roman("नहीं"), "nahin")

    def test_schwa_kept_when_no_vowel_would_remain(self):
        # "ka" -> stripping the 'a' would leave the unpronounceable "k"
        self.assertEqual(app_module._strip_word_final_schwa("ka"), "ka")

    def test_schwa_kept_after_explicit_long_vowel(self):
        # capital A/I/etc mark an explicit matra, not the inherent schwa
        self.assertEqual(app_module._strip_word_final_schwa("yA"), "yA")

    def test_schwa_dropped_after_bare_consonant(self):
        self.assertEqual(app_module._strip_word_final_schwa("saba"), "sab")

    def test_non_devanagari_text_passes_through(self):
        self.assertEqual(app_module.devanagari_to_roman("hello world"), "hello world")


class CaptionFormatTests(unittest.TestCase):
    def setUp(self):
        self.segments = [
            {"start": 0.0, "end": 1.5, "text": "hello"},
            {"start": 1.5, "end": 3.25, "text": "world"},
        ]

    def test_srt_format(self):
        srt = app_module.to_srt(self.segments)
        self.assertTrue(srt.startswith("1\n"))
        self.assertIn("00:00:00,000 --> 00:00:01,500", srt)
        self.assertIn("00:00:01,500 --> 00:00:03,250", srt)
        self.assertIn("hello", srt)
        self.assertIn("world", srt)

    def test_vtt_format(self):
        vtt = app_module.to_vtt(self.segments)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:01.500", vtt)

    def test_empty_segments(self):
        self.assertEqual(app_module.to_srt([]), "")
        self.assertEqual(app_module.to_vtt([]), "WEBVTT\n")


class ApiSmokeTests(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    def test_health_endpoint(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("ffmpeg_found", data)
        self.assertIn("jobs_in_memory", data)

    def test_history_list_shape(self):
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("items", data)
        self.assertIn("total", data)
        self.assertIsInstance(data["items"], list)

    def test_create_job_rejects_empty_body(self):
        res = self.client.post("/api/jobs", json={})
        self.assertEqual(res.status_code, 400)

    def test_create_job_rejects_bad_url(self):
        res = self.client.post("/api/jobs", json={"urls": ["not-a-url"]})
        self.assertEqual(res.status_code, 400)

    def test_unknown_job_is_404(self):
        res = self.client.get("/api/jobs/does-not-exist")
        self.assertEqual(res.status_code, 404)

    def test_unknown_history_is_404(self):
        res = self.client.get("/api/history/does-not-exist")
        self.assertEqual(res.status_code, 404)

    def test_retry_unknown_job_is_404(self):
        res = self.client.post("/api/jobs/does-not-exist/retry")
        self.assertEqual(res.status_code, 404)

    def test_index_page_loads(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
