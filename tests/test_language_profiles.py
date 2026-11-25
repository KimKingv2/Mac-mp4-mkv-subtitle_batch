import importlib.util
import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def track(track_id, lang, name, default=False, ietf=None):
    props = {"language": lang, "track_name": name, "default_track": default,
             "forced_track": False}
    if ietf is not None:
        props["language_ietf"] = ietf
    return {"id": track_id, "type": "audio", "codec": "AAC", "properties": props}


class LanguageResolutionTests(unittest.TestCase):
    def test_language_aliases(self):
        self.assertEqual(mod.canonical_language_code("en-US"), "eng")
        self.assertEqual(mod.canonical_language_code("eng"), "eng")
        self.assertEqual(mod.canonical_language_code("ja-JP"), "jpn")
        self.assertEqual(mod.canonical_language_code("jpn"), "jpn")
        self.assertEqual(mod.canonical_language_code("ko-KR"), "kor")
        self.assertEqual(mod.canonical_language_code("kor"), "kor")
        self.assertEqual(mod.canonical_language_code("ru-RU"), "rus")
        self.assertEqual(mod.canonical_language_code("rus"), "rus")
        self.assertEqual(mod.canonical_language_code("zh-CN"), "zho")
        self.assertEqual(mod.canonical_language_code("zho"), "zho")
        self.assertEqual(mod.canonical_language_code("fr-FR"), "fra")
        self.assertEqual(mod.canonical_language_code("fra"), "fra")

    def test_resolve_language_ietf_and_legacy_agree(self):
        self.assertEqual(mod.resolve_track_language({"language": "jpn", "language_ietf": "ja-JP"})[0], "jpn")

    def test_conflicting_language_metadata_is_rejected(self):
        with self.assertRaises(RuntimeError):
            mod.resolve_track_language({"language": "rus", "language_ietf": "en-US"})

    def test_clear_track_name_can_fill_und_tag(self):
        self.assertEqual(mod.resolve_track_language({"language": "und", "track_name": "English 5.1"})[0], "eng")
        self.assertEqual(mod.resolve_track_language({"language": "und", "track_name": "Japanese"})[0], "jpn")
        self.assertEqual(mod.resolve_track_language({"language": "und", "track_name": "Russian 5.1"})[0], "rus")
        self.assertEqual(mod.resolve_track_language({"language": "und", "track_name": "中文配音"})[0], "zho")

    def test_language_ietf_and_iso_639_2_variants_are_equivalent(self):
        self.assertEqual(mod.resolve_track_language({"language": "rus", "language_ietf": "ru-RU"})[0], "rus")
        self.assertEqual(mod.resolve_track_language({"language": "zho", "language_ietf": "zh-Hans"})[0], "zho")


class AudioAllowlistTests(unittest.TestCase):
    def test_keep_only_en_ja_ko_and_fallback_default_when_original_default_is_removed(self):
        info = {"tracks": [
            track(0, "rus", "Russian", True),
            track(1, "eng", "English", False),
            track(2, "jpn", "Japanese", False),
            track(3, "kor", "Korean", False),
        ]}
        plan = mod.build_audio_language_plan(info, "eng")
        self.assertEqual(plan["selected_ids"], [1, 2, 3])
        self.assertEqual(plan["default_ids"], [1])
        self.assertEqual([x[1] for x in plan["dropped"]], ["rus"])
        args = mod.audio_language_cli_options(plan)
        self.assertEqual(args[:2], ["--audio-tracks", "1,2,3"])
        self.assertIn("1:yes", args)
        self.assertIn("2:no", args)

    def test_korean_profile_fallback_chooses_korean_audio_default(self):
        info = {"tracks": [track(4, "eng", "English", False), track(5, "kor", "Korean", False)]}
        plan = mod.build_audio_language_plan(info, "kor")
        self.assertEqual(plan["default_ids"], [5])

    def test_russian_audio_is_dropped_when_ietf_and_legacy_use_different_forms(self):
        info = {"tracks": [
            track(0, "rus", "Russian", True, ietf="ru-RU"),
            track(1, "eng", "English", False, ietf="en-US"),
        ]}
        plan = mod.build_audio_language_plan(info, "eng")
        self.assertEqual(plan["selected_ids"], [1])
        self.assertEqual([x[1] for x in plan["dropped"]], ["rus"])

    def test_russian_audio_with_und_tag_and_clear_title_is_safely_dropped(self):
        info = {"tracks": [
            track(0, "und", "Russian Dub", True),
            track(1, "eng", "English Original", False),
        ]}
        plan = mod.build_audio_language_plan(info, "eng")
        self.assertEqual(plan["selected_ids"], [1])
        self.assertEqual([x[1] for x in plan["dropped"]], ["rus"])

    def test_unknown_audio_language_fails_closed(self):
        info = {"tracks": [track(1, "eng", "English"), track(2, "und", "Stereo") ]}
        with self.assertRaisesRegex(RuntimeError, "語言無法可靠識別"):
            mod.build_audio_language_plan(info, "eng")

    def test_no_allowed_audio_fails(self):
        info = {"tracks": [track(1, "rus", "Russian"), track(2, "zho", "Chinese")]}
        with self.assertRaisesRegex(RuntimeError, "沒有可識別"):
            mod.build_audio_language_plan(info, "eng")

    def test_filtered_views_reject_explicit_ffprobe_language_conflict(self):
        mkv = {"tracks": [
            track(0, "rus", "Russian", True, ietf="ru-RU"),
            track(1, "eng", "English", False, ietf="en-US"),
        ], "chapters": [], "attachments": []}
        probe = {"streams": [
            {"index": 0, "codec_type": "audio", "tags": {"language": "eng"}},
            {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
        ], "format": {}}
        plan = mod.build_audio_language_plan(mkv, "eng")
        with self.assertRaisesRegex(RuntimeError, "FFprobe.*語言不一致"):
            mod.filtered_source_views(probe, mkv, plan)

    def test_filtered_views_remove_non_allowed_audio_and_track_indexes_are_retained(self):
        mkv = {"tracks": [
            {"id": 0, "type": "video", "properties": {}},
            track(1, "rus", "Russian", True),
            track(2, "eng", "English", False),
            track(3, "jpn", "Japanese", False),
            {"id": 4, "type": "subtitles", "properties": {}},
        ], "chapters": [], "attachments": []}
        probe = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "disposition": {"default": 1}},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "disposition": {"default": 0}},
            {"index": 3, "codec_type": "audio", "codec_name": "aac", "disposition": {"default": 0}},
            {"index": 4, "codec_type": "subtitle", "codec_name": "subrip"},
        ], "format": {}}
        plan = mod.build_audio_language_plan(mkv, "eng")
        pview, mview = mod.filtered_source_views(probe, mkv, plan)
        self.assertEqual([x["index"] for x in pview["streams"] if x["codec_type"] == "audio"], [2, 3])
        self.assertEqual(pview["_audio_source_stream_indices"], [2, 3])
        self.assertEqual([x["id"] for x in mview["tracks"] if x["type"] == "audio"], [2, 3])
        # Russian was the only original default, so English becomes the fallback default.
        self.assertTrue(mview["tracks"][1]["properties"]["default_track"])
        self.assertEqual(pview["streams"][1]["disposition"]["default"], 1)




class AudioMuxCommandTests(unittest.TestCase):
    def test_dual_subtitle_mux_filters_russian_and_keeps_english_japanese_audio(self):
        source_mkv = {"tracks": [
            {"id": 0, "type": "video", "codec": "AVC", "properties": {"language": "und"}},
            track(1, "rus", "Russian", True, ietf="ru-RU"),
            track(2, "eng", "English", False, ietf="en-US"),
            track(3, "jpn", "Japanese", False, ietf="ja-JP"),
        ], "chapters": [], "attachments": []}
        source_probe = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "rus"}},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
            {"index": 3, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn"}},
        ], "format": {}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "episode.mp4"
            primary = root / "primary.srt"
            secondary = root / "secondary.srt"
            output = root / "output.mkv"
            logfile = root / "run.log"
            for item in (video, primary, secondary):
                item.write_text("test", encoding="utf-8")
            captured = {}

            def fake_run_text(cmd):
                captured["cmd"] = list(cmd)
                output.write_bytes(b"mock-mkv")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with mock.patch.object(mod, "validate_subtitle_source", return_value=({"codec_name": "subrip"}, 3, "")),                  mock.patch.object(mod, "validate_source", return_value=(source_probe, source_mkv, "", "", 0)),                  mock.patch.object(mod, "run_text", side_effect=fake_run_text),                  mock.patch.object(mod, "verify_output", return_value=(True, "validation passed", [])):
                ok, message = mod.mux_two_external_subtitles(
                    "mkvmerge", "mkvextract", "ffmpeg", "ffprobe", video, primary, secondary,
                    output, logfile, primary_language="jpn", primary_track_name="Japanese")

            self.assertTrue(ok, message)
            cmd = captured["cmd"]
            audio_index = cmd.index("--audio-tracks")
            self.assertEqual(cmd[audio_index + 1], "2,3")
            # The source Russian track was the only original default. Since it is
            # dropped, the Japanese-profile track becomes fallback Default Yes.
            self.assertIn(["--default-track-flag", "2:no"], [cmd[i:i+2] for i in range(len(cmd)-1)])
            self.assertIn(["--default-track-flag", "3:yes"], [cmd[i:i+2] for i in range(len(cmd)-1)])
            self.assertIn("--no-subtitles", cmd)


class PrimarySubtitleTests(unittest.TestCase):
    def test_primary_language_track_matching(self):
        self.assertTrue(mod.is_target_language_properties({"language": "jpn", "track_name": "Japanese"}, "jpn"))
        self.assertTrue(mod.is_target_language_properties({"language_ietf": "ko-KR", "track_name": "Korean"}, "kor"))
        self.assertFalse(mod.is_target_language_properties({"language": "eng", "track_name": "English"}, "jpn"))
        self.assertFalse(mod.is_target_language_properties({"language": "eng", "track_name": "Chinese + English"}, "eng"))

    def test_select_japanese_default_embedded_over_external_non_japanese(self):
        source = [{"id": 3, "properties": {"language": "jpn", "track_name": "Japanese", "default_track": True}}]
        source_id, external_default, _ = mod.choose_primary_default_target(source, "eng", "English", "jpn")
        self.assertEqual(source_id, 3)
        self.assertFalse(external_default)

    def test_select_new_korean_subtitle_when_it_is_the_only_candidate(self):
        source_id, external_default, _ = mod.choose_primary_default_target([], "kor", "Korean", "kor")
        self.assertIsNone(source_id)
        self.assertTrue(external_default)

    def test_language_sidecar_suffixes(self):
        self.assertEqual(mod.LANGUAGE_PROFILES["1"]["suffix"], ".en")
        self.assertEqual(mod.LANGUAGE_PROFILES["2"]["suffix"], ".ja")
        self.assertEqual(mod.LANGUAGE_PROFILES["3"]["suffix"], ".ko")

    def test_export_selector_picks_japanese_subtitle_for_japanese_profile(self):
        info = {"tracks": [
            {"id": 4, "type": "subtitles", "properties": {"language": "eng", "track_name": "English", "default_track": False}},
            {"id": 5, "type": "subtitles", "properties": {"language": "jpn", "track_name": "Japanese", "default_track": True}},
        ]}
        selected, _ = mod.choose_language_track_for_export(info, "jpn")
        self.assertEqual(selected["id"], 5)


if __name__ == "__main__":
    unittest.main()
