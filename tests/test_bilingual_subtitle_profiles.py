import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch_bilingual_profiles", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class BilingualSubtitleProfileTests(unittest.TestCase):
    def test_zh_tw_en_maps_to_traditional_chinese_and_english(self):
        result = mod.identify_bilingual_subtitle_profile(
            "/subs/Shameless.ZH-TW_EN.ass/S01E01.ass", "eng")
        self.assertEqual(result["language_ietf"], "zh-TW")
        self.assertEqual(result["track_name"], "繁體中文 + English")
        self.assertEqual(result["sidecar_suffix"], ".zh-tw-en")
        self.assertEqual(result["original_language"], "eng")

    def test_zh_cn_ja_maps_to_simplified_chinese_and_japanese(self):
        result = mod.identify_bilingual_subtitle_profile(
            "/subs/Show.ZH-CN_JA/S01E01.ass", "jpn")
        self.assertEqual(result["language_ietf"], "zh-CN")
        self.assertEqual(result["track_name"], "簡體中文 + Japanese")
        self.assertEqual(result["sidecar_suffix"], ".zh-cn-ja")

    def test_zh_hant_ko_and_zh_hans_en_aliases(self):
        traditional = mod.identify_bilingual_subtitle_profile("/subs/zh-Hant_KO/a.ass", "kor")
        simplified = mod.identify_bilingual_subtitle_profile("/subs/zh-Hans_EN/a.ass", "eng")
        self.assertEqual(traditional["language_ietf"], "zh-TW")
        self.assertEqual(traditional["track_name"], "繁體中文 + Korean")
        self.assertEqual(simplified["language_ietf"], "zh-CN")
        self.assertEqual(simplified["track_name"], "簡體中文 + English")

    def test_localized_words_are_supported(self):
        traditional = mod.identify_bilingual_subtitle_profile(
            "/subs/Traditional Chinese English/a.ass", "eng")
        simplified = mod.identify_bilingual_subtitle_profile(
            "/subs/簡體中文 Japanese/a.ass", "jpn")
        self.assertEqual(traditional["language_ietf"], "zh-TW")
        self.assertEqual(simplified["language_ietf"], "zh-CN")

    def test_unmarked_variant_is_rejected_instead_of_guessing(self):
        with self.assertRaisesRegex(RuntimeError, "未標明簡體或繁體"):
            mod.identify_bilingual_subtitle_profile("/subs/Bilingual/a.ass", "eng")

    def test_conflicting_simplified_and_traditional_markers_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "同時包含簡體與繁體"):
            mod.identify_bilingual_subtitle_profile("/subs/ZH-TW_ZH-CN_EN/a.ass", "eng")

    def test_explicit_original_language_must_match_primary_profile(self):
        with self.assertRaisesRegex(RuntimeError, "拒絕錯配"):
            mod.identify_bilingual_subtitle_profile("/subs/ZH-TW_EN/a.ass", "jpn")

    def test_unknown_original_language_suffix_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "不支援的原語言"):
            mod.identify_bilingual_subtitle_profile("/subs/ZH-TW_RU/a.ass", "eng")

    def test_bilingual_chinese_tracks_are_recognized_from_zh_ietf(self):
        self.assertTrue(mod.is_chinese_track_properties({
            "language": "chi",
            "language_ietf": "zh-TW",
            "track_name": "繁體中文 + English",
        }))
        self.assertTrue(mod.is_chinese_track_properties({
            "language": "chi",
            "language_ietf": "zh-CN",
            "track_name": "簡體中文 + Japanese",
        }))

    def test_bilingual_sidecar_suffix_uses_locale_and_original_language(self):
        traditional_english = {"properties": {
            "language": "chi", "language_ietf": "zh-TW",
            "track_name": "繁體中文 + English",
        }}
        simplified_japanese = {"properties": {
            "language": "chi", "language_ietf": "zh-CN",
            "track_name": "簡體中文 + Japanese",
        }}
        traditional_korean = {"properties": {
            "language": "chi", "language_ietf": "zh-TW",
            "track_name": "繁體中文 + Korean",
        }}
        self.assertEqual(mod.chinese_sidecar_suffix(traditional_english), ".zh-tw-en")
        self.assertEqual(mod.chinese_sidecar_suffix(simplified_japanese), ".zh-cn-ja")
        self.assertEqual(mod.chinese_sidecar_suffix(traditional_korean), ".zh-tw-ko")

    def test_generic_legacy_chinese_sidecar_keeps_zh_suffix(self):
        legacy = {"properties": {
            "language": "und", "track_name": "Chinese + English",
        }}
        self.assertEqual(mod.chinese_sidecar_suffix(legacy), ".zh")


class BilingualMuxCommandTests(unittest.TestCase):
    def test_mode_one_writes_chinese_bcp47_and_locale_specific_track_name(self):
        source_mkv = {"tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": {
                "language": "und", "default_track": True, "forced_track": False}},
            {"id": 1, "type": "audio", "codec": "AAC", "properties": {
                "language": "eng", "track_name": "English", "default_track": True,
                "forced_track": False}},
        ], "chapters": [], "attachments": []}
        source_probe = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac",
             "tags": {"language": "eng"}, "disposition": {"default": 1}},
        ], "format": {}}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mp4"
            primary = root / "English.srt"
            bilingual_root = root / "ZH-TW_EN"
            bilingual_root.mkdir()
            bilingual = bilingual_root / "episode.ass"
            output = root / "output.mkv"
            logfile = root / "run.log"
            for item in (video, primary, bilingual):
                item.write_text("test", encoding="utf-8")
            captured = {}

            def fake_run_text(command):
                captured["command"] = list(command)
                output.write_bytes(b"mock-mkv")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(mod, "validate_subtitle_source",
                                   return_value=({"codec_name": "subrip"}, 3, "")), \\
                 mock.patch.object(mod, "validate_source",
                                   return_value=(source_probe, source_mkv, "", "", 0)), \\
                 mock.patch.object(mod, "run_text", side_effect=fake_run_text), \\
                 mock.patch.object(mod, "verify_output", return_value=(True, "validation passed", [])) as verify:
                ok, message = mod.mux_one(
                    "mkvmerge", "mkvextract", "ffmpeg", "ffprobe",
                    video, primary, bilingual, output, logfile,
                    primary_language="eng", primary_track_name="English")

            self.assertTrue(ok, message)
            command = captured["command"]
            language_values = [command[i + 1] for i, value in enumerate(command[:-1]) if value == "--language"]
            track_names = [command[i + 1] for i, value in enumerate(command[:-1]) if value == "--track-name"]
            self.assertEqual(language_values, ["0:eng", "0:zh-TW"])
            self.assertEqual(track_names, ["0:English", "0:繁體中文 + English"])
            self.assertEqual(verify.call_args.kwargs["second_language"], "zh-TW")
            self.assertEqual(verify.call_args.kwargs["second_track_name"], "繁體中文 + English")


if __name__ == "__main__":
    unittest.main(verbosity=2)
