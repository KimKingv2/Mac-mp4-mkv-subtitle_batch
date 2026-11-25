import copy
import unittest

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DefaultDurationNormalizationTests(unittest.TestCase):
    @staticmethod
    def timing(time_base="1/24000", pts=None, durations=None,
               avg="24000/1001", real="24000/1001", flags=None,
               codec_name="hevc"):
        pts = pts if pts is not None else [0, 2002, 1001, 6006]
        durations = durations if durations is not None else [1001, 1001, 1001, 1001]
        flags = flags if flags is not None else ["K", "", "", ""]
        return {
            "streams": [{
                "codec_name": codec_name,
                "avg_frame_rate": avg,
                "r_frame_rate": real,
                "time_base": time_base,
            }],
            "packets": [
                {"pts": str(p), "duration": str(d), "flags": flag}
                for p, d, flag in zip(pts, durations, flags)
            ],
        }

    @staticmethod
    def mkv_info(default_duration):
        return {"tracks": [{
            "type": "video", "codec": "HEVC/H.265/MPEG-H",
            "properties": {
                "language": "und", "track_name": None,
                "default_track": True, "forced_track": False,
                "enabled_track": True, "codec_id": "V_MPEGH/ISO/HEVC",
                "pixel_dimensions": "1920x1080", "display_dimensions": "1920x1080",
                "display_unit": 0, "default_duration": default_duration,
                "stereo_mode": 0, "color_matrix_coefficients": 1,
                "color_primaries": 1, "color_transfer_characteristics": 1,
                "color_range": 1, "chroma_subsampling": "4:2:0",
                "bits_per_channel": 10,
            },
        }]}

    @staticmethod
    def framemd5_summary(time_base, pts, durations, hashes):
        return {
            "time_base": mod.Fraction(time_base),
            "frames": list(zip(pts, durations, hashes)),
        }

    def test_cfr_23976_mp4_to_mkv_quantization_is_accepted(self):
        source = self.timing()
        output = self.timing(
            time_base="1/1000",
            pts=[21, 104, 63, 271],
            durations=[41, 41, 41, 41],
            flags=["K", "", "", ""],
        )
        ok, message, prefix_drop = mod.compare_implicit_default_duration_timing(
            source, output, 41708333)
        self.assertTrue(ok, message)
        self.assertEqual(prefix_drop, 0)
        self.assertIn("24000/1001", message)
        self.assertIn("逐封包", message)

    def test_default_duration_must_match_exact_frame_rate(self):
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            self.timing(), self.timing("1/1000", [21, 104, 63, 271], [41] * 4), 40000000)
        self.assertFalse(ok)
        self.assertIn("對應約", message)

    def test_vfr_or_ambiguous_rates_are_rejected(self):
        output = self.timing("1/1000", [21, 104, 63, 271], [41] * 4,
                             avg="24000/1001", real="30000/1001")
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            self.timing(), output, 41708333)
        self.assertFalse(ok)
        self.assertIn("固定影格率", message)

    def test_source_variable_packet_durations_are_rejected(self):
        source = self.timing(durations=[1001, 1001, 2002, 1001])
        output = self.timing("1/1000", [21, 104, 63, 271], [41, 41, 83, 41])
        ok, message, _ = mod.compare_implicit_default_duration_timing(source, output, 41708333)
        self.assertFalse(ok)
        self.assertIn("不是固定影格時長", message)

    def test_output_packet_duration_change_is_rejected(self):
        output = self.timing("1/1000", [21, 104, 68, 271], [41, 41, 50, 41])
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            self.timing(), output, 41708333)
        self.assertFalse(ok)
        self.assertIn("封包時長與固定影格率不符", message)

    def test_arbitrary_packet_count_change_is_rejected(self):
        source = self.timing(flags=["K", "", "", ""])
        output = self.timing("1/1000", [21, 104, 63], [41, 41, 41], flags=["K", "", ""])
        ok, message, prefix_drop = mod.compare_implicit_default_duration_timing(
            source, output, 41708333, decoded_frames_identical=True)
        self.assertFalse(ok)
        self.assertEqual(prefix_drop, 0)
        self.assertIn("不是第一個關鍵影格之前的完整前綴", message)

    def test_leading_non_key_packets_require_identical_decoded_frames(self):
        source = self.timing(
            pts=[0, 1001, 2002, 3003, 4004],
            durations=[1001] * 5,
            flags=["", "", "K", "", ""],
        )
        output = self.timing(
            time_base="1/1000", pts=[0, 42, 83], durations=[41, 41, 41],
            flags=["K", "", ""],
        )
        no_proof, message, _ = mod.compare_implicit_default_duration_timing(
            source, output, 41708333, decoded_frames_identical=False)
        self.assertFalse(no_proof)
        self.assertIn("尚未證明解碼影格完全一致", message)

        accepted, message, prefix_drop = mod.compare_implicit_default_duration_timing(
            source, output, 41708333, decoded_frames_identical=True)
        self.assertTrue(accepted, message)
        self.assertEqual(prefix_drop, 2)
        self.assertIn("來源開頭 2 個", message)
        self.assertIn("逐幀相同", message)

    def test_prefix_drop_must_end_exactly_at_first_source_keyframe(self):
        source = self.timing(
            pts=[0, 1001, 2002, 3003, 4004], durations=[1001] * 5,
            flags=["", "K", "", "", ""],
        )
        output = self.timing(
            time_base="1/1000", pts=[0, 42, 83], durations=[41] * 3,
            flags=["K", "", ""],
        )
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            source, output, 41708333, decoded_frames_identical=True)
        self.assertFalse(ok)
        self.assertIn("不是第一個關鍵影格之前的完整前綴", message)

    def test_output_must_begin_with_keyframe(self):
        source = self.timing(
            pts=[0, 1001, 2002, 3003, 4004], durations=[1001] * 5,
            flags=["", "", "K", "", ""],
        )
        output = self.timing(
            time_base="1/1000", pts=[0, 42, 83], durations=[41] * 3,
            flags=["", "", ""],
        )
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            source, output, 41708333, decoded_frames_identical=True)
        self.assertFalse(ok)
        self.assertIn("不是第一個關鍵影格之前的完整前綴", message)

    def test_prefix_drop_exception_is_only_for_matching_h264_or_hevc(self):
        source = self.timing(
            pts=[0, 1001, 2002, 3003, 4004], durations=[1001] * 5,
            flags=["", "", "K", "", ""], codec_name="av1",
        )
        output = self.timing(
            time_base="1/1000", pts=[0, 42, 83], durations=[41] * 3,
            flags=["K", "", ""], codec_name="av1",
        )
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            source, output, 41708333, decoded_frames_identical=True)
        self.assertFalse(ok)
        self.assertIn("只有 H.264／HEVC", message)

    def test_relative_pts_change_over_one_millisecond_is_rejected(self):
        output = self.timing("1/1000", [21, 104, 68, 271], [41] * 4)
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            self.timing(), output, 41708333)
        self.assertFalse(ok)
        self.assertIn("相對 PTS 改變", message)

    def test_missing_packet_timing_is_rejected(self):
        source = self.timing()
        source["packets"][1].pop("pts")
        ok, message, _ = mod.compare_implicit_default_duration_timing(
            source, self.timing("1/1000", [21, 104, 63, 271], [41] * 4), 41708333)
        self.assertFalse(ok)
        self.assertIn("缺少 PTS 或 duration", message)

    def test_framemd5_parser_and_timebase_quantization(self):
        digest1 = "0" * 32
        digest2 = "1" * 32
        source_text = """#format: frame checksums
#version: 2
#hash: MD5
#tb 0: 1001/24000
#stream#, dts, pts, duration, size, hash
0, 0, 0, 1, 86400, {0}
0, 1, 1, 1, 86400, {1}
""".format(digest1, digest2)
        output_text = """#format: frame checksums
#version: 2
#hash: MD5
#tb 0: 1001/24000
#stream#, dts, pts, duration, size, hash
0, 5, 5, 1, 86400, {0}
0, 6, 6, 1, 86400, {1}
""".format(digest1, digest2)
        source = mod.parse_framemd5_output(source_text)
        output = mod.parse_framemd5_output(output_text)
        ok, message = mod.compare_framemd5_summaries(source, output)
        self.assertTrue(ok, message)
        self.assertIn("2 幀的 MD5 逐幀相同", message)

    def test_framemd5_compares_retained_suffix_after_verified_leading_prefix(self):
        source = self.framemd5_summary(
            "1/1000", [0, 40, 80, 120, 160], [40] * 5,
            ["a" * 32, "b" * 32, "0" * 32, "1" * 32, "2" * 32],
        )
        output = self.framemd5_summary(
            "1/1000", [500, 540, 580], [40] * 3,
            ["0" * 32, "1" * 32, "2" * 32],
        )
        ok, message = mod.compare_framemd5_summaries(
            source, output, skip_source_prefix=2)
        self.assertTrue(ok, message)
        self.assertIn("略過來源前導解碼影格 2 幀後", message)
        self.assertIn("3 幀的 MD5 逐幀相同", message)

    def test_framemd5_suffix_still_rejects_any_retained_pixel_difference(self):
        source = self.framemd5_summary(
            "1/1000", [0, 40, 80, 120], [40] * 4,
            ["a" * 32, "b" * 32, "0" * 32, "1" * 32],
        )
        output = self.framemd5_summary(
            "1/1000", [500, 540], [40] * 2,
            ["0" * 32, "f" * 32],
        )
        ok, message = mod.compare_framemd5_summaries(
            source, output, skip_source_prefix=2)
        self.assertFalse(ok)
        self.assertIn("MD5 不一致", message)

    def test_framemd5_suffix_rejects_wrong_prefix_length(self):
        source = self.framemd5_summary(
            "1/1000", [0, 40, 80, 120], [40] * 4,
            ["a" * 32, "b" * 32, "0" * 32, "1" * 32],
        )
        output = self.framemd5_summary(
            "1/1000", [500, 540, 580], [40] * 3,
            ["0" * 32, "1" * 32, "2" * 32],
        )
        ok, message = mod.compare_framemd5_summaries(
            source, output, skip_source_prefix=1)
        self.assertFalse(ok)
        self.assertIn("MD5 不一致", message)

    def test_leading_keyframe_packet_drop_candidate_requires_exact_verified_prefix(self):
        source = self.timing(
            pts=[0, 1001, 2002, 3003, 4004], durations=[1001] * 5,
            flags=["", "", "K", "", ""],
        )
        output = self.timing(
            time_base="1/1000", pts=[0, 42, 83], durations=[41] * 3,
            flags=["K", "", ""],
        )
        count, error = mod.leading_keyframe_packet_drop_candidate(source, output)
        self.assertEqual(count, 2)
        self.assertEqual(error, "")

        bad_output = copy.deepcopy(output)
        bad_output["packets"][0]["flags"] = ""
        count, error = mod.leading_keyframe_packet_drop_candidate(source, bad_output)
        self.assertIsNone(count)
        self.assertIn("未從關鍵影格開始", error)

    def test_framemd5_rejects_pixel_difference(self):
        source = self.framemd5_summary("1/1000", [0, 40], [40, 40], ["0" * 32, "1" * 32])
        output = self.framemd5_summary("1/1000", [0, 40], [40, 40], ["0" * 32, "2" * 32])
        ok, message = mod.compare_framemd5_summaries(source, output)
        self.assertFalse(ok)
        self.assertIn("MD5 不一致", message)

    def test_framemd5_rejects_frame_count_difference(self):
        source = self.framemd5_summary("1/1000", [0, 40], [40, 40], ["0" * 32, "1" * 32])
        output = self.framemd5_summary("1/1000", [0], [40], ["0" * 32])
        ok, message = mod.compare_framemd5_summaries(source, output)
        self.assertFalse(ok)
        self.assertIn("可解碼影格數仍不同", message)

    def test_framemd5_rejects_timestamp_change_over_one_millisecond(self):
        source = self.framemd5_summary("1/1000", [0, 40], [40, 40], ["0" * 32, "1" * 32])
        output = self.framemd5_summary("1/1000", [0, 43], [40, 40], ["0" * 32, "1" * 32])
        ok, message = mod.compare_framemd5_summaries(source, output)
        self.assertFalse(ok)
        self.assertIn("相對 PTS 改變", message)

    def test_framemd5_rejects_duration_change_over_one_millisecond(self):
        source = self.framemd5_summary("1/1000", [0, 40], [40, 40], ["0" * 32, "1" * 32])
        output = self.framemd5_summary("1/1000", [0, 40], [40, 43], ["0" * 32, "1" * 32])
        ok, message = mod.compare_framemd5_summaries(source, output)
        self.assertFalse(ok)
        self.assertIn("duration 改變", message)

    def test_implicit_duration_gate_requires_one_video_and_missing_source_value(self):
        source = self.mkv_info(None)
        output = self.mkv_info(41708333)
        self.assertTrue(mod.needs_implicit_default_duration_check(source, output))
        self.assertFalse(mod.needs_implicit_default_duration_check(self.mkv_info(40000000), output))
        multi = copy.deepcopy(source)
        multi["tracks"].append(copy.deepcopy(multi["tracks"][0]))
        self.assertFalse(mod.needs_implicit_default_duration_check(multi, output))

    def test_mkv_comparison_keeps_duration_strict_without_timing_proof(self):
        source = self.mkv_info(None)
        output = self.mkv_info(41708333)
        strict, message, _ = mod.compare_mkv_track_metadata(source, output, ("video",))
        self.assertFalse(strict)
        self.assertIn("default_duration", message)
        accepted, message, notes = mod.compare_mkv_track_metadata(
            source, output, ("video",), allow_implicit_default_duration=True)
        self.assertTrue(accepted, message)
        self.assertTrue(any("DefaultDuration" in note for note in notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
