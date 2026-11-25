import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class P0MetadataTests(unittest.TestCase):
    def test_language_ietf_backfill_is_accepted_only_when_source_missing(self):
        ok, note = mod.compare_track_property({"language": "eng"},
                                              {"language": "eng", "language_ietf": "en"},
                                              "language_ietf")
        self.assertTrue(ok)
        self.assertIn("自動補出", note)

    def test_explicit_language_ietf_change_is_rejected(self):
        ok, note = mod.compare_track_property({"language": "eng", "language_ietf": "en"},
                                              {"language": "eng", "language_ietf": "fr"},
                                              "language_ietf")
        self.assertFalse(ok)

    def test_audio_metadata_change_is_rejected(self):
        source = {"tracks": [{"type": "audio", "codec": "AAC", "properties": {
            "language": "eng", "language_ietf": "en", "track_name": "Main",
            "default_track": True, "forced_track": False, "enabled_track": True,
            "codec_id": "A_AAC", "audio_channels": 6,
            "audio_sampling_frequency": 48000, "audio_bits_per_sample": 16}}]}
        output = {"tracks": [{"type": "audio", "codec": "AAC", "properties": {
            "language": "eng", "language_ietf": "en", "track_name": "Main",
            "default_track": True, "forced_track": False, "enabled_track": True,
            "codec_id": "A_AAC", "audio_channels": 2,
            "audio_sampling_frequency": 48000, "audio_bits_per_sample": 16}}]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("audio_channels", message)

    def test_subtitle_default_can_be_ignored_only_when_requested(self):
        source = {"tracks": [{"type": "subtitles", "codec": "SubRip/SRT", "properties": {
            "language": "eng", "track_name": "English", "default_track": False,
            "forced_track": False, "codec_id": "S_TEXT/UTF8"}}]}
        output = {"tracks": [{"type": "subtitles", "codec": "SubRip/SRT", "properties": {
            "language": "eng", "track_name": "English", "default_track": True,
            "forced_track": False, "codec_id": "S_TEXT/UTF8"}}]}
        strict, _, _ = mod.compare_mkv_track_metadata(source, output, ("subtitles",))
        tolerant, _, _ = mod.compare_mkv_track_metadata(
            source, output, ("subtitles",), ignored_fields={"subtitles": ("default_track",)})
        self.assertFalse(strict)
        self.assertTrue(tolerant)

    def test_ffprobe_audio_channel_layout_change_is_rejected(self):
        source = {"streams": [{"codec_type": "audio", "codec_name": "eac3", "profile": "Dolby Digital Plus",
                               "sample_rate": "48000", "channels": 6, "channel_layout": "5.1",
                               "bits_per_sample": 0, "bits_per_raw_sample": None,
                               "disposition": {"default": 1, "forced": 0}}]}
        output = {"streams": [{"codec_type": "audio", "codec_name": "eac3", "profile": "Dolby Digital Plus",
                               "sample_rate": "48000", "channels": 6, "channel_layout": "5.1(side)",
                               "bits_per_sample": 0, "bits_per_raw_sample": None,
                               "disposition": {"default": 1, "forced": 0}}]}
        ok, message, _ = mod.compare_ffprobe_stream_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("channel_layout", message)

    def test_ffprobe_packet_count_difference_is_rejected(self):
        import json
        class FakeProc:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""
        calls = {"count": 0}
        original_run_text = mod.run_text
        def fake_run_text(cmd):
            calls["count"] += 1
            packets = 100 if calls["count"] == 1 else 98
            return FakeProc(json.dumps({"streams": [{"codec_type": "audio", "codec_name": "aac",
                                                        "nb_read_packets": str(packets)}]}))
        mod.run_text = fake_run_text
        try:
            ok, message = mod.compare_ffprobe_packet_counts("ffprobe", "src.mkv", "out.mkv", ("audio",))
        finally:
            mod.run_text = original_run_text
        self.assertFalse(ok)
        self.assertIn("封包數不同", message)

    def test_chapter_and_attachment_metadata_changes_are_rejected(self):
        source = {"chapters": [{"chapter_time_start": "00:00:00.000000000"}],
                  "attachments": [{"id": 1, "file_name": "font.ttf", "content_type": "font/ttf",
                                   "description": "Subtitle font", "size": 100}]}
        good = {"chapters": [{"chapter_time_start": "00:00:00.000000000"}],
                "attachments": [{"id": 9, "file_name": "font.ttf", "content_type": "font/ttf",
                                 "description": "Subtitle font", "size": 100}]}
        bad = {"chapters": [{"chapter_time_start": "00:00:02.000000000"}],
               "attachments": [{"id": 9, "file_name": "font.ttf", "content_type": "font/ttf",
                                "description": "Subtitle font", "size": 100}]}
        self.assertTrue(mod.compare_chapters_and_attachments(source, good)[0])  # IDs may be renumbered
        self.assertFalse(mod.compare_chapters_and_attachments(source, bad)[0])
        bad_attachment = {"chapters": source["chapters"], "attachments": [dict(good["attachments"][0], size=101)]}
        self.assertFalse(mod.compare_chapters_and_attachments(source, bad_attachment)[0])


class P0PublicationTests(unittest.TestCase):
    def test_atomic_publish_creates_final_without_clobber(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / ".staging" / "episode.mkv"
            final = root / "episode.mkv"
            staged.parent.mkdir()
            staged.write_bytes(b"verified-mkv")
            mod.atomic_publish_no_overwrite(staged, final)
            self.assertEqual(final.read_bytes(), b"verified-mkv")
            self.assertFalse(staged.exists())

    def test_atomic_publish_refuses_existing_final_and_keeps_stage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / ".staging" / "episode.mkv"
            final = root / "episode.mkv"
            staged.parent.mkdir()
            staged.write_bytes(b"new")
            final.write_bytes(b"original-final")
            with self.assertRaises(FileExistsError):
                mod.atomic_publish_no_overwrite(staged, final)
            self.assertEqual(final.read_bytes(), b"original-final")
            self.assertEqual(staged.read_bytes(), b"new")

    def test_failed_validation_is_moved_out_of_final_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staging = root / ".staging"
            staging.mkdir()
            candidate = staging / "episode.mkv"
            final = root / "episode.mkv"
            candidate.write_bytes(b"unverified")
            failed = mod.preserve_failed_staging(candidate, final, None, "test failure")
            self.assertIsNotNone(failed)
            self.assertTrue(failed.exists())
            self.assertFalse(final.exists())
            self.assertFalse(candidate.exists())


class SingleVideoDefaultNormalizationTests(unittest.TestCase):
    @staticmethod
    def info(video_defaults):
        return {"tracks": [
            {"type": "video", "codec": "AVC/H.264", "properties": {
                "language": "und", "track_name": None,
                "default_track": video_defaults, "forced_track": False,
                "enabled_track": True, "codec_id": "V_MPEG4/ISO/AVC",
                "pixel_dimensions": "1920x1080", "display_dimensions": "1920x1080",
                "display_unit": 0, "default_duration": 40000000,
                "stereo_mode": 0, "color_matrix_coefficients": 1,
                "color_primaries": 1, "color_transfer_characteristics": 1,
                "color_range": 1, "chroma_subsampling": "4:2:0",
                "bits_per_channel": 8,
            }}
        ]}

    def test_single_video_missing_flag_to_explicit_default_yes_is_accepted(self):
        ok, message, notes = mod.compare_mkv_track_metadata(
            self.info(None), self.info(True), ("video",), context="最終 MKV ")
        self.assertTrue(ok, message)
        self.assertTrue(any("唯一影片軌明確標記為 Default Yes" in n for n in notes))

    def test_single_video_missing_flag_to_default_no_is_rejected(self):
        ok, message, _ = mod.compare_mkv_track_metadata(
            self.info(None), self.info(False), ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("default_track", message)

    def test_multiple_video_tracks_keep_strict_default_flag_comparison(self):
        source = self.info(None)
        output = self.info(True)
        source["tracks"].append({**source["tracks"][0], "id": 2})
        output["tracks"].append({**output["tracks"][0], "id": 2})
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("default_track", message)

    def test_audio_none_to_true_is_not_relaxed(self):
        source = {"tracks": [{"type": "audio", "codec": "AAC", "properties": {
            "language": "eng", "track_name": None, "default_track": None,
            "forced_track": False, "enabled_track": True, "codec_id": "A_AAC",
            "audio_channels": 2, "audio_sampling_frequency": 48000,
            "audio_bits_per_sample": 16}}]}
        output = {"tracks": [{"type": "audio", "codec": "AAC", "properties": {
            "language": "eng", "track_name": None, "default_track": True,
            "forced_track": False, "enabled_track": True, "codec_id": "A_AAC",
            "audio_channels": 2, "audio_sampling_frequency": 48000,
            "audio_bits_per_sample": 16}}]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("default_track", message)

    def test_helper_detects_only_single_video_default_promotion(self):
        self.assertTrue(mod.has_implicit_single_video_default_promotion(self.info(None), self.info(True)))
        self.assertFalse(mod.has_implicit_single_video_default_promotion(self.info(False), self.info(True)))
        source = self.info(None)
        output = self.info(True)
        source["tracks"].append({**source["tracks"][0], "id": 2})
        output["tracks"].append({**output["tracks"][0], "id": 2})
        self.assertFalse(mod.has_implicit_single_video_default_promotion(source, output))

    def test_ffprobe_video_default_difference_only_allowed_when_explicitly_enabled(self):
        source = {"streams": [{"codec_type": "video", "codec_name": "h264", "profile": "High",
                               "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
                               "field_order": "progressive", "color_range": "tv", "color_space": "bt709",
                               "color_transfer": "bt709", "color_primaries": "bt709",
                               "bits_per_raw_sample": "8", "level": 40,
                               "disposition": {"default": 0, "forced": 0}}]}
        output = {"streams": [{**source["streams"][0], "disposition": {"default": 1, "forced": 0}}]}
        strict, _, _ = mod.compare_ffprobe_stream_metadata(source, output, ("video",))
        tolerant, message, notes = mod.compare_ffprobe_stream_metadata(
            source, output, ("video",), allow_implicit_single_video_default=True)
        self.assertFalse(strict)
        self.assertTrue(tolerant, message)
        self.assertTrue(any("FFprobe disposition" in n for n in notes))

    def test_ffprobe_audio_default_difference_is_never_allowed_by_video_exception(self):
        source = {"streams": [{"codec_type": "audio", "codec_name": "aac", "profile": "LC",
                               "sample_rate": "48000", "channels": 2, "channel_layout": "stereo",
                               "bits_per_sample": 0, "bits_per_raw_sample": None,
                               "disposition": {"default": 0, "forced": 0}}]}
        output = {"streams": [{**source["streams"][0], "disposition": {"default": 1, "forced": 0}}]}
        ok, message, _ = mod.compare_ffprobe_stream_metadata(
            source, output, ("audio",), allow_implicit_single_video_default=True)
        self.assertFalse(ok)
        self.assertIn("disposition.default", message)


class MatroskaCodecIdNormalizationTests(unittest.TestCase):
    @staticmethod
    def info(codec_id, description="HEVC/H.265/MPEG-H"):
        return {"tracks": [{"type": "video", "id": 1, "codec": description, "properties": {
            "language": "und", "track_name": None,
            "default_track": True, "forced_track": False,
            "enabled_track": True, "codec_id": codec_id,
            "pixel_dimensions": "1920x1080", "display_dimensions": "1920x1080",
            "display_unit": 0, "default_duration": 40000000,
            "stereo_mode": 0, "color_matrix_coefficients": 1,
            "color_primaries": 1, "color_transfer_characteristics": 1,
            "color_range": 1, "chroma_subsampling": "4:2:0",
            "bits_per_channel": 10,
        }}]}

    def test_missing_source_codec_id_to_matching_hevc_matroska_id_is_accepted(self):
        source = self.info(None)
        output = self.info("V_MPEGH/ISO/HEVC")
        ok, message, notes = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertTrue(ok, message)
        self.assertTrue(any("來源未提供 Matroska CodecID" in note for note in notes))

    def test_missing_source_codec_id_to_matching_avc_matroska_id_is_accepted(self):
        source = self.info(None, "AVC/H.264/MPEG-4p10")
        output = self.info("V_MPEG4/ISO/AVC", "AVC/H.264/MPEG-4p10")
        ok, message, notes = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertTrue(ok, message)
        self.assertTrue(notes)

    def test_missing_source_codec_id_with_mismatched_output_id_is_rejected(self):
        source = self.info(None, "HEVC/H.265/MPEG-H")
        output = self.info("V_MPEG4/ISO/AVC", "HEVC/H.265/MPEG-H")
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("codec_id", message)

    def test_explicit_source_codec_id_change_is_still_rejected(self):
        source = self.info("V_MPEG4/ISO/AVC", "HEVC/H.265/MPEG-H")
        output = self.info("V_MPEGH/ISO/HEVC", "HEVC/H.265/MPEG-H")
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("codec_id", message)

    @staticmethod
    def audio_track(codec_id, description="AAC", channels=2, sample_rate=48000, bits=16):
        return {"type": "audio", "id": 1, "codec": description, "properties": {
            "language": "eng", "track_name": None, "default_track": True,
            "forced_track": False, "enabled_track": True, "codec_id": codec_id,
            "audio_channels": channels, "audio_sampling_frequency": sample_rate,
            "audio_bits_per_sample": bits}}

    def test_single_aac_audio_missing_source_codec_id_to_a_aac_is_accepted(self):
        source = {"tracks": [self.audio_track(None)]}
        output = {"tracks": [self.audio_track("A_AAC")]}
        ok, message, notes = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertTrue(ok, message)
        self.assertTrue(any("來源未提供 Matroska CodecID" in note and "A_AAC" in note for note in notes))

    def test_single_audio_missing_codec_id_to_non_aac_is_rejected(self):
        source = {"tracks": [self.audio_track(None, "Opus") ]}
        output = {"tracks": [self.audio_track("A_OPUS", "Opus") ]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("codec_id", message)

    def test_a_aac_requires_matching_aac_description(self):
        source = {"tracks": [self.audio_track(None, "AC-3")]}
        output = {"tracks": [self.audio_track("A_AAC", "AC-3")]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("codec_id", message)

    def test_explicit_source_audio_codec_id_change_is_rejected(self):
        source = {"tracks": [self.audio_track("A_AC3", "AAC")]}
        output = {"tracks": [self.audio_track("A_AAC", "AAC")]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("codec_id", message)

    def test_audio_codec_id_normalization_is_not_allowed_for_multiple_tracks(self):
        first_source = self.audio_track(None)
        second_source = self.audio_track("A_AAC")
        first_output = self.audio_track("A_AAC")
        second_output = self.audio_track("A_AAC")
        for track, track_id in ((first_source, 1), (second_source, 2),
                                (first_output, 1), (second_output, 2)):
            track["id"] = track_id
        source = {"tracks": [first_source, second_source]}
        output = {"tracks": [first_output, second_output]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("codec_id", message)

    def test_audio_channel_change_remains_rejected_with_aac_codec_id_normalization(self):
        source = {"tracks": [self.audio_track(None, channels=2)]}
        output = {"tracks": [self.audio_track("A_AAC", channels=6)]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("audio_channels", message)

    def test_single_aac_mp4_sample_size_16_to_missing_matroska_bitdepth_is_accepted(self):
        source = {"tracks": [self.audio_track(None, bits=16)]}
        output = {"tracks": [self.audio_track("A_AAC", bits=None)]}
        ok, message, notes = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertTrue(ok, message)
        self.assertTrue(any("sample-size=16 bits" in note and "BitDepth" in note for note in notes))

    def test_aac_non_16_source_sample_size_to_missing_matroska_bitdepth_is_rejected(self):
        source = {"tracks": [self.audio_track(None, bits=24)]}
        output = {"tracks": [self.audio_track("A_AAC", bits=None)]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("audio_bits_per_sample", message)

    def test_aac_explicit_output_bitdepth_change_is_rejected(self):
        source = {"tracks": [self.audio_track(None, bits=16)]}
        output = {"tracks": [self.audio_track("A_AAC", bits=24)]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("audio_bits_per_sample", message)

    def test_aac_bitdepth_omission_requires_missing_source_codec_id(self):
        source = {"tracks": [self.audio_track("A_AAC", bits=16)]}
        output = {"tracks": [self.audio_track("A_AAC", bits=None)]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("audio_bits_per_sample", message)

    def test_aac_bitdepth_omission_is_not_allowed_for_multiple_audio_tracks(self):
        source_a = self.audio_track("A_AAC", bits=16)
        source_b = self.audio_track("A_AAC", bits=16)
        output_a = self.audio_track("A_AAC", bits=None)
        output_b = self.audio_track("A_AAC", bits=None)
        for index, track in enumerate((source_a, source_b, output_a, output_b), 1):
            track["id"] = index
        source = {"tracks": [source_a, source_b]}
        output = {"tracks": [output_a, output_b]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("audio_bits_per_sample", message)

    def test_aac_bitdepth_omission_requires_matching_aac_codec_description(self):
        source = {"tracks": [self.audio_track(None, description="AAC", bits=16)]}
        output = {"tracks": [self.audio_track("A_AAC", description="AAC LC", bits=None)]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("codec 描述改變", message)

    def test_ffprobe_audio_bit_depth_metadata_remains_strict(self):
        base = {"codec_type": "audio", "codec_name": "aac", "profile": "LC",
                "sample_rate": "48000", "channels": 2, "channel_layout": "stereo",
                "bits_per_sample": 0, "bits_per_raw_sample": None,
                "disposition": {"default": 1, "forced": 0}}
        source = {"streams": [dict(base)]}
        output = {"streams": [dict(base, bits_per_sample=16)]}
        ok, message, _ = mod.compare_ffprobe_stream_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("bits_per_sample", message)


class MissingDisplayDimensionsNormalizationTests(unittest.TestCase):
    @staticmethod
    def info(display_dimensions, pixel_dimensions="1920x1080", video_count=1):
        tracks = []
        for index in range(video_count):
            tracks.append({"type": "video", "id": index + 1, "codec": "HEVC/H.265/MPEG-H", "properties": {
                "language": "und", "track_name": None,
                "default_track": True, "forced_track": False,
                "enabled_track": True, "codec_id": "V_MPEGH/ISO/HEVC",
                "pixel_dimensions": pixel_dimensions, "display_dimensions": display_dimensions,
                "display_unit": 0, "default_duration": 40000000,
                "stereo_mode": 0, "color_matrix_coefficients": 1,
                "color_primaries": 1, "color_transfer_characteristics": 1,
                "color_range": 1, "chroma_subsampling": "4:2:0",
                "bits_per_channel": 10,
            }})
        return {"tracks": tracks}

    def test_missing_source_display_dimensions_to_coded_pixel_size_is_accepted(self):
        source = self.info(None)
        output = self.info("1920x1080")
        ok, message, notes = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertTrue(ok, message)
        self.assertTrue(any("補入與來源／輸出 coded pixel_dimensions" in note for note in notes))

    def test_missing_source_display_unit_to_pixel_default_is_accepted_with_default_display_size(self):
        source = self.info(None)
        output = self.info("1920x1080")
        source["tracks"][0]["properties"]["display_unit"] = None
        output["tracks"][0]["properties"]["display_unit"] = 0
        ok, message, notes = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertTrue(ok, message)
        self.assertTrue(any("DisplayUnit=0" in note for note in notes))

    def test_missing_source_display_unit_to_non_pixel_unit_is_rejected(self):
        source = self.info(None)
        output = self.info("1920x1080")
        source["tracks"][0]["properties"]["display_unit"] = None
        output["tracks"][0]["properties"]["display_unit"] = 1
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("display_unit", message)

    def test_missing_source_display_dimensions_to_anamorphic_size_is_rejected(self):
        source = self.info(None)
        output = self.info("2560x1080")
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("display_dimensions", message)

    def test_coded_pixel_dimension_change_is_rejected(self):
        source = self.info(None, pixel_dimensions="1920x1080")
        output = self.info("1920x1080", pixel_dimensions="1280x720")
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("pixel_dimensions", message)

    def test_explicit_source_display_dimension_change_is_rejected(self):
        source = self.info("1920x1080")
        output = self.info("1280x720")
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("display_dimensions", message)

    def test_multiple_video_tracks_do_not_receive_display_dimension_exception(self):
        source = self.info(None, video_count=2)
        output = self.info("1920x1080", video_count=2)
        ok, message, _ = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("display_dimensions", message)

    def test_ffprobe_sample_aspect_ratio_change_is_rejected(self):
        base = {"codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
                "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "16:9", "pix_fmt": "yuv420p10le",
                "field_order": "progressive", "color_range": "tv",
                "color_space": "bt709", "color_transfer": "bt709",
                "color_primaries": "bt709", "bits_per_raw_sample": "10", "level": 120,
                "disposition": {"default": 1, "forced": 0}}
        source = {"streams": [dict(base)]}
        output = {"streams": [dict(base, sample_aspect_ratio="4:3")]}
        ok, message, _ = mod.compare_ffprobe_stream_metadata(source, output, ("video",))
        self.assertFalse(ok)
        self.assertIn("sample_aspect_ratio", message)

    def test_ffprobe_display_aspect_ratio_change_is_rejected(self):
        base = {"codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
                "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "16:9", "pix_fmt": "yuv420p10le",
                "field_order": "progressive", "color_range": "tv",
                "color_space": "bt709", "color_transfer": "bt709",
                "color_primaries": "bt709", "bits_per_raw_sample": "10", "level": 120,
                "disposition": {"default": 1, "forced": 0}}
        source = {"streams": [dict(base)]}
        output = {"streams": [dict(base, display_aspect_ratio="4:3")]}
        ok, message, _ = mod.compare_ffprobe_stream_metadata(source, output, ("video",))
        self.assertFalse(ok)
        self.assertIn("display_aspect_ratio", message)


class TrackForcedNormalizationTests(unittest.TestCase):
    @staticmethod
    def info(forced_value, video_count=1):
        tracks = []
        for index in range(video_count):
            tracks.append({"type": "video", "id": index + 1, "codec": "AVC/H.264", "properties": {
                "language": "und", "track_name": None,
                "default_track": True, "forced_track": forced_value,
                "enabled_track": True, "codec_id": "V_MPEG4/ISO/AVC",
                "pixel_dimensions": "1920x1080", "display_dimensions": "1920x1080",
                "display_unit": 0, "default_duration": 40000000,
                "stereo_mode": 0, "color_matrix_coefficients": 1,
                "color_primaries": 1, "color_transfer_characteristics": 1,
                "color_range": 1, "chroma_subsampling": "4:2:0",
                "bits_per_channel": 8,
            }})
        return {"tracks": tracks}

    def test_single_video_missing_forced_to_explicit_false_is_accepted(self):
        source, output = self.info(None), self.info(False)
        ok, message, notes = mod.compare_mkv_track_metadata(
            source, output, ("video",), context="最終 MKV ")
        self.assertTrue(ok, message)
        self.assertTrue(any("Forced No" in note for note in notes))

    def test_single_video_missing_forced_to_true_is_rejected(self):
        ok, message, _ = mod.compare_mkv_track_metadata(
            self.info(None), self.info(True), ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("forced_track", message)

    def test_single_video_explicit_false_to_true_is_rejected(self):
        ok, message, _ = mod.compare_mkv_track_metadata(
            self.info(False), self.info(True), ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("forced_track", message)

    def test_multiple_video_tracks_missing_forced_to_false_is_rejected(self):
        ok, message, _ = mod.compare_mkv_track_metadata(
            self.info(None, video_count=2), self.info(False, video_count=2),
            ("video",), context="最終 MKV ")
        self.assertFalse(ok)
        self.assertIn("forced_track", message)

    @staticmethod
    def audio_info(forced_values):
        return {"tracks": [{"type": "audio", "id": index + 1, "codec": "AAC", "properties": {
            "language": "eng", "track_name": None, "default_track": True,
            "forced_track": forced, "enabled_track": True, "codec_id": "A_AAC",
            "audio_channels": 2, "audio_sampling_frequency": 48000,
            "audio_bits_per_sample": 16}} for index, forced in enumerate(forced_values)]}

    def test_single_audio_missing_forced_to_explicit_false_is_accepted(self):
        source, output = self.audio_info([None]), self.audio_info([False])
        ok, message, notes = mod.compare_mkv_track_metadata(
            source, output, ("audio",), context="最終 MKV ")
        self.assertTrue(ok, message)
        self.assertTrue(any("只有一條音訊軌" in note and "Forced No" in note for note in notes))

    def test_single_audio_missing_forced_to_true_is_rejected(self):
        ok, message, _ = mod.compare_mkv_track_metadata(
            self.audio_info([None]), self.audio_info([True]), ("audio",))
        self.assertFalse(ok)
        self.assertIn("forced_track", message)

    def test_single_audio_explicit_false_to_true_is_rejected(self):
        ok, message, _ = mod.compare_mkv_track_metadata(
            self.audio_info([False]), self.audio_info([True]), ("audio",))
        self.assertFalse(ok)
        self.assertIn("forced_track", message)

    def test_multiple_audio_tracks_missing_forced_to_false_is_rejected(self):
        ok, message, _ = mod.compare_mkv_track_metadata(
            self.audio_info([None, None]), self.audio_info([False, False]), ("audio",))
        self.assertFalse(ok)
        self.assertIn("forced_track", message)

    def test_single_audio_forced_normalization_does_not_hide_language_change(self):
        source, output = self.audio_info([None]), self.audio_info([False])
        output["tracks"][0]["properties"]["language"] = "jpn"
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("language", message)

    def test_single_audio_forced_normalization_does_not_hide_channel_change(self):
        source, output = self.audio_info([None]), self.audio_info([False])
        output["tracks"][0]["properties"]["audio_channels"] = 6
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("audio",))
        self.assertFalse(ok)
        self.assertIn("audio_channels", message)

    def test_subtitle_missing_forced_to_false_remains_strict(self):
        source = {"tracks": [{"type": "subtitles", "codec": "SubRip/SRT", "properties": {
            "language": "eng", "track_name": "English", "default_track": True,
            "forced_track": None, "enabled_track": True, "codec_id": "S_TEXT/UTF8"}}]}
        output = {"tracks": [{"type": "subtitles", "codec": "SubRip/SRT", "properties": {
            "language": "eng", "track_name": "English", "default_track": True,
            "forced_track": False, "enabled_track": True, "codec_id": "S_TEXT/UTF8"}}]}
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("subtitles",))
        self.assertFalse(ok)
        self.assertIn("forced_track", message)

    def test_video_default_normalization_remains_available(self):
        source, output = self.info(False), self.info(False)
        source["tracks"][0]["properties"]["default_track"] = None
        output["tracks"][0]["properties"]["default_track"] = True
        ok, message, _ = mod.compare_mkv_track_metadata(source, output, ("video",))
        self.assertTrue(ok, message)


class FFprobePacketSummaryIndexTests(unittest.TestCase):
    def _with_fake_ffprobe(self, payloads, callback):
        import json
        class FakeProc:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = json.dumps(stdout)
                self.stderr = ""
        calls = []
        original = mod.run_text
        def fake_run_text(cmd):
            calls.append(list(cmd))
            return FakeProc(payloads[len(calls) - 1])
        mod.run_text = fake_run_text
        try:
            return callback(calls)
        finally:
            mod.run_text = original

    def test_packet_summary_requests_and_preserves_global_stream_index(self):
        payload = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "nb_read_packets": "82681"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "nb_read_packets": "100"},
        ]}
        def run(calls):
            summary = mod.ffprobe_packet_summary("ffprobe", "source.mp4")
            self.assertIn("stream=index,codec_type,codec_name,nb_read_packets", calls[0])
            self.assertEqual(summary["audio"], [("aac", 100, 1)])
        self._with_fake_ffprobe([payload], run)

    def test_single_source_audio_stream_is_not_filtered_out(self):
        source = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "nb_read_packets": "82681"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "nb_read_packets": "80000"},
        ]}
        output = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "nb_read_packets": "82681"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "nb_read_packets": "80000"},
        ]}
        def run(calls):
            ok, message = mod.compare_ffprobe_packet_counts(
                "ffprobe", "source.mp4", "output.mkv", kinds=("audio",),
                source_audio_stream_indices=[1])
            self.assertTrue(ok, message)
            self.assertIn("封包數比對完成", message)
            self.assertEqual(len(calls), 2)
        self._with_fake_ffprobe([source, output], run)

    def test_retained_audio_global_index_selects_correct_track_after_language_filter(self):
        source = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "nb_read_packets": "82681"},
            {"index": 1, "codec_type": "audio", "codec_name": "ac3", "nb_read_packets": "80000"},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "nb_read_packets": "79000"},
        ]}
        output = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "nb_read_packets": "82681"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "nb_read_packets": "79000"},
        ]}
        def run(calls):
            ok, message = mod.compare_ffprobe_packet_counts(
                "ffprobe", "source.mp4", "output.mkv", kinds=("audio",),
                source_audio_stream_indices=[2])
            self.assertTrue(ok, message)
            self.assertIn("封包數比對完成", message)
        self._with_fake_ffprobe([source, output], run)


if __name__ == "__main__":
    unittest.main(verbosity=2)
