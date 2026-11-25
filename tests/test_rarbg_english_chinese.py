import importlib.util
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch_rarbg_chinese", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
FIXTURES = Path(__file__).resolve().parent / "fixtures"

class RarbgEnglishChineseTests(unittest.TestCase):
    def test_select_standard_english_and_ignore_sdh(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "Shameless.US.S01E01.1080p.BluRay.x265-RARBG.mp4"
            video.touch()
            subs = root / "Subs"
            episode = subs / video.stem
            episode.mkdir(parents=True)
            (episode / "3_English.srt").write_text((FIXTURES / "3_English.srt").read_text(encoding="utf-8"), encoding="utf-8")
            (episode / "4_English.srt").write_text((FIXTURES / "4_English.srt").read_text(encoding="utf-8"), encoding="utf-8")
            selected, note = mod.select_rarbg_standard_english(video, subs)
            self.assertEqual(selected.name, "3_English.srt")
            self.assertIn("4_English.srt", note)
            self.assertIn("SDH", note)

    def test_reject_multiple_standard_english(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "Show.S01E01.mp4"
            video.touch()
            episode = root / "Subs" / video.stem
            episode.mkdir(parents=True)
            fixture = (FIXTURES / "3_English.srt").read_text(encoding="utf-8")
            (episode / "3_English.srt").write_text(fixture, encoding="utf-8")
            (episode / "5_English.srt").write_text(fixture, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                mod.select_rarbg_standard_english(video, root / "Subs")

    def test_chinese_subtitle_episode_key(self):
        self.assertEqual(mod.episode_key(Path("Shameless.US.S01E01.zh.srt")), "S01E01")

    def test_mux_wrapper_uses_chinese_non_default_and_requires_clean_source(self):
        with patch.object(mod, "mux_two_external_subtitles", return_value=(True, "ok")) as mocked:
            result = mod.mux_english_chinese_only("mkvmerge", "mkvextract", "ffmpeg", "ffprobe",
                                                 "video", "english", "chinese", "output", "log")
        self.assertEqual(result, (True, "ok"))
        self.assertEqual(mocked.call_args.kwargs["second_track_name"], "Chinese")
        self.assertEqual(mocked.call_args.kwargs["second_language"], "zh")
        self.assertTrue(mocked.call_args.kwargs["require_source_without_subtitles"])

if __name__ == "__main__":
    unittest.main()
