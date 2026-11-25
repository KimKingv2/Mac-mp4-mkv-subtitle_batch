import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch_chinese_sidecar", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ChineseSubtitleSelectionTests(unittest.TestCase):
    def test_chinese_language_aliases_are_identified(self):
        for props in (
            {"language": "zh", "track_name": "Chinese"},
            {"language": "zho", "track_name": "中文"},
            {"language": "chi", "track_name": "Chinese Traditional"},
            {"language_ietf": "zh-Hans", "track_name": "Simplified Chinese"},
        ):
            self.assertTrue(mod.is_chinese_track_properties(props), props)

    def test_explicit_chinese_english_bilingual_title_is_identified_even_when_und(self):
        self.assertTrue(mod.is_chinese_track_properties({
            "language": "und", "track_name": "Chinese + English"
        }))
        self.assertTrue(mod.is_chinese_track_properties({
            "language": "eng", "track_name": "中英雙語"
        }))

    def test_english_only_and_other_language_tracks_are_not_chinese(self):
        self.assertFalse(mod.is_chinese_track_properties({
            "language": "eng", "track_name": "English"
        }))
        self.assertFalse(mod.is_chinese_track_properties({
            "language": "jpn", "track_name": "Japanese"
        }))
        self.assertFalse(mod.is_chinese_track_properties({
            "language": "und", "track_name": "English SDH"
        }))

    def test_chinese_title_with_conflicting_language_tags_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "互相衝突"):
            mod.is_chinese_track_properties({
                "language": "zho", "language_ietf": "en-US", "track_name": "Chinese + English"
            })

    def test_selector_chooses_unique_chinese_bilingual_track(self):
        info = {"tracks": [
            {"id": 1, "type": "subtitles", "properties": {
                "language": "eng", "track_name": "English", "default_track": True}},
            {"id": 2, "type": "subtitles", "properties": {
                "language": "und", "track_name": "Chinese + English", "default_track": False}},
        ]}
        track, reason = mod.choose_chinese_track_for_export(info)
        self.assertEqual(track["id"], 2)
        self.assertIn("唯一", reason)

    def test_selector_prefers_unique_default_when_multiple_chinese_tracks(self):
        info = {"tracks": [
            {"id": 2, "type": "subtitles", "properties": {
                "language": "zho", "track_name": "Chinese Simplified", "default_track": False}},
            {"id": 3, "type": "subtitles", "properties": {
                "language": "zh", "track_name": "Chinese + English", "default_track": True}},
        ]}
        track, _ = mod.choose_chinese_track_for_export(info)
        self.assertEqual(track["id"], 3)

    def test_selector_refuses_multiple_chinese_tracks_without_unique_default(self):
        info = {"tracks": [
            {"id": 2, "type": "subtitles", "properties": {
                "language": "zho", "track_name": "Chinese Simplified", "default_track": False}},
            {"id": 3, "type": "subtitles", "properties": {
                "language": "und", "track_name": "Chinese + English", "default_track": False}},
        ]}
        track, reason = mod.choose_chinese_track_for_export(info)
        self.assertIsNone(track)
        self.assertIn("沒有唯一 Default", reason)

    def test_selector_reports_chinese_candidate_language_conflict(self):
        info = {"tracks": [
            {"id": 4, "type": "subtitles", "properties": {
                "language": "zho", "language_ietf": "en-US", "track_name": "Chinese + English"}},
        ]}
        track, reason = mod.choose_chinese_track_for_export(info)
        self.assertIsNone(track)
        self.assertIn("互相矛盾", reason)


class ChineseSidecarExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "Episode.S01E01.mkv"
        self.output.write_bytes(b"mock mkv")
        self.logfile = self.root / "run.log"
        self.srt_data = (
            "1\n00:00:01,000 --> 00:00:02,000\n中文字幕測試\n"
        ).encode("utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def mkv_info(self, codec_id="S_TEXT/UTF8"):
        return {"tracks": [
            {"id": 2, "type": "subtitles", "codec": "SubRip/SRT", "properties": {
                "track_name": "English", "language": "eng", "codec_id": "S_TEXT/UTF8",
                "default_track": True}},
            {"id": 5, "type": "subtitles", "codec": "SubRip/SRT", "properties": {
                "track_name": "Chinese + English", "language": "und", "codec_id": codec_id,
                "default_track": False}},
        ]}

    def fake_extract(self, command):
        target = command[-1]
        _, destination = target.split(":", 1)
        Path(destination).write_bytes(self.srt_data)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def test_exports_chinese_bilingual_track_as_same_basename_zh_srt(self):
        captured = {}

        def fake_run(command):
            captured["command"] = list(command)
            return self.fake_extract(command)

        with mock.patch.object(mod, "mkvmerge_info", return_value=(self.mkv_info(), 0, "")), \
             mock.patch.object(mod, "run_text", side_effect=fake_run), \
             mock.patch.object(mod, "validate_subtitle_source", return_value=({"codec_name": "subrip"}, 1, "")):
            ok, message, status = mod.export_chinese_sidecar(
                "mkvextract", "mkvmerge", "ffprobe", self.output, self.logfile)

        expected = self.root / "Episode.S01E01.zh.srt"
        self.assertTrue(ok, message)
        self.assertEqual(status, "exported")
        self.assertEqual(Path(message), expected)
        self.assertEqual(expected.read_bytes(), self.srt_data)
        self.assertTrue(captured["command"][-1].startswith("5:"))

    def test_exports_chinese_ass_track_with_ass_extension(self):
        info = self.mkv_info(codec_id="S_TEXT/ASS")
        ass_data = b"[Script Info]\nTitle: Test\n"

        def fake_run(command):
            target = command[-1]
            _, destination = target.split(":", 1)
            Path(destination).write_bytes(ass_data)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.object(mod, "mkvmerge_info", return_value=(info, 0, "")), \
             mock.patch.object(mod, "run_text", side_effect=fake_run), \
             mock.patch.object(mod, "validate_subtitle_source", return_value=({"codec_name": "ass"}, 1, "")):
            ok, message, status = mod.export_chinese_sidecar(
                "mkvextract", "mkvmerge", "ffprobe", self.output, self.logfile)

        expected = self.root / "Episode.S01E01.zh.ass"
        self.assertTrue(ok, message)
        self.assertEqual(status, "exported")
        self.assertEqual(Path(message), expected)
        self.assertEqual(expected.read_bytes(), ass_data)

    def test_existing_zh_sidecar_is_never_overwritten(self):
        existing = self.root / "Episode.S01E01.zh.srt"
        existing.write_text("keep existing", encoding="utf-8")
        with mock.patch.object(mod, "mkvmerge_info", return_value=(self.mkv_info(), 0, "")), \
             mock.patch.object(mod, "run_text") as run_mock:
            ok, message, status = mod.export_chinese_sidecar(
                "mkvextract", "mkvmerge", "ffprobe", self.output, self.logfile)
        self.assertFalse(ok)
        self.assertEqual(status, "exists")
        self.assertIn("避免覆蓋", message)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep existing")
        run_mock.assert_not_called()

    def test_no_chinese_track_is_reported_without_guessing(self):
        info = {"tracks": [
            {"id": 2, "type": "subtitles", "properties": {
                "track_name": "English", "language": "eng", "codec_id": "S_TEXT/UTF8"}},
        ]}
        with mock.patch.object(mod, "mkvmerge_info", return_value=(info, 0, "")), \
             mock.patch.object(mod, "run_text") as run_mock:
            ok, message, status = mod.export_chinese_sidecar(
                "mkvextract", "mkvmerge", "ffprobe", self.output, self.logfile)
        self.assertFalse(ok)
        self.assertEqual(status, "not_exported")
        self.assertIn("沒有可識別", message)
        run_mock.assert_not_called()

    def test_existing_chinese_sidecar_paths_detects_all_supported_extensions(self):
        srt = self.root / "Episode.S01E01.zh.srt"
        ass = self.root / "Episode.S01E01.zh.ass"
        srt.write_text("srt", encoding="utf-8")
        ass.write_text("ass", encoding="utf-8")
        found = mod.existing_chinese_sidecar_paths(self.output)
        self.assertEqual(set(found), {srt, ass})


if __name__ == "__main__":
    unittest.main(verbosity=2)
