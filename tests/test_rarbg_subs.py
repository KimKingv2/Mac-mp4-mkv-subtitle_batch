import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch_rarbg", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

USER_SAMPLE_ROOT = Path(__file__).resolve().parent / "fixtures"


class RarbgSubtitleClassifierTests(unittest.TestCase):
    def test_user_sample_3_english_is_standard(self):
        result = mod.classify_english_subtitle(USER_SAMPLE_ROOT / "3_English.srt")
        self.assertEqual(result["kind"], "standard")
        self.assertEqual(result["cue_count"], 0)

    def test_user_sample_4_english_is_sdh(self):
        result = mod.classify_english_subtitle(USER_SAMPLE_ROOT / "4_English.srt")
        self.assertEqual(result["kind"], "sdh")
        self.assertGreaterEqual(result["cue_count"], 5)
        self.assertIn("SDH", result["reason"])

    def test_short_english_track_is_treated_as_forced_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "1_English.srt"
            p.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n2\n00:00:03,000 --> 00:00:04,000\nStop!\n", encoding="utf-8")
            self.assertEqual(mod.classify_english_subtitle(p)["kind"], "forced")

    def test_forced_filename_is_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "1_English.Forced.srt"
            p.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            self.assertEqual(mod.classify_english_subtitle(p)["kind"], "forced")


class RarbgFolderMatchingTests(unittest.TestCase):
    def test_exact_release_folder_and_nested_subtitle_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video_root = root / "Videos"
            subs_root = root / "Subs"
            video_root.mkdir(); subs_root.mkdir()
            video = video_root / "Shameless.US.S01E01.1080p.BluRay.x265-RARBG.mp4"
            video.write_bytes(b"placeholder")
            ep_folder = subs_root / video.stem
            nested = ep_folder / "English"
            nested.mkdir(parents=True)
            (nested / "3_English.srt").write_text((USER_SAMPLE_ROOT / "3_English.srt").read_text(encoding="utf-8"), encoding="utf-8")
            (nested / "4_English.srt").write_text((USER_SAMPLE_ROOT / "4_English.srt").read_text(encoding="utf-8"), encoding="utf-8")
            standard, sdh, note = mod.select_rarbg_english_and_sdh(video, subs_root)
            self.assertEqual(standard.name, "3_English.srt")
            self.assertEqual(sdh.name, "4_English.srt")
            self.assertIn("精確匹配", note)

    def test_episode_folder_fallback_is_allowed_only_when_unique(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); subs=root/"Subs"; subs.mkdir()
            video=root/"Show.S01E01.1080p.WEBRip.mp4"
            (subs/"Show.S01E01").mkdir()
            folder, note = mod.find_rarbg_episode_folder(video, subs)
            self.assertEqual(folder.name,"Show.S01E01")
            self.assertIn("唯一 SxxExx",note)

    def test_ambiguous_same_episode_subfolders_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); subs=root/"Subs"; subs.mkdir()
            video=root/"Show.S01E01.1080p.WEBRip.mp4"
            (subs/"Show.S01E01.RARBG").mkdir()
            (subs/"Show.S01E01.OtherRelease").mkdir()
            with self.assertRaises(RuntimeError):
                mod.find_rarbg_episode_folder(video, subs)

    def test_two_standard_english_files_are_rejected_not_guessed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); video=root/"Show.S01E01.RARBG.mp4"
            subs=root/"Subs"; folder=subs/video.stem; folder.mkdir(parents=True)
            standard=(USER_SAMPLE_ROOT/"3_English.srt").read_text(encoding="utf-8")
            (folder/"2_English.srt").write_text(standard, encoding="utf-8")
            (folder/"3_English.srt").write_text(standard, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                mod.select_rarbg_english_and_sdh(video, subs)

    def test_sdh_export_selects_only_track_marked_sdh(self):
        info={"tracks":[
            {"type":"subtitles","id":1,"properties":{"track_name":"English","language":"eng","default_track":True}},
            {"type":"subtitles","id":2,"properties":{"track_name":"English SDH","language":"eng","default_track":False}},
        ]}
        track, reason=mod.choose_english_sdh_track_for_export(info)
        self.assertEqual(track["id"],2)
        normal, _=mod.choose_english_track_for_export(info)
        self.assertEqual(normal["id"],1)

    def test_rarbg_mux_requires_no_embedded_subtitles(self):
        original=mod.mux_two_external_subtitles
        calls={}
        def fake(*args, **kwargs):
            calls.update(kwargs)
            return args[7] if False else kwargs
        mod.mux_two_external_subtitles=fake
        try:
            result=mod.mux_rarbg_english_sdh("mkvmerge","mkvextract","ffmpeg","ffprobe",
                "video.mp4","3_English.srt","4_English.srt","out.mkv",None)
        finally:
            mod.mux_two_external_subtitles=original
        self.assertEqual(result["second_track_name"],"English SDH")
        self.assertEqual(result["second_language"],"eng")
        self.assertTrue(result["require_source_without_subtitles"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
