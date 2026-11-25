import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "frame_mkv_batch.py"
spec = importlib.util.spec_from_file_location("frame_mkv_batch_subtitle_validation", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class SubtitleValidationTests(unittest.TestCase):
    def test_text_subtitle_uses_stdout_pipe_not_dev_null(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "test.log"
            fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch.object(mod.subprocess, "run", return_value=fake) as run:
                ok, message, diagnostic = mod.validate_subtitle_stream_track(
                    "/fake/ffmpeg", "/fake/ffprobe", "/media/output.mkv", 0,
                    {"codec_name": "subrip"}, log, "字幕 1")
            self.assertTrue(ok)
            self.assertIn("解析通過", message)
            self.assertFalse(diagnostic)
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[-3:], ["-f", "ass", "pipe:1"])
            self.assertNotIn("/dev/null", cmd)
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_nonzero_exit_rejects_subtitle(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "test.log"
            fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="decode failed")
            with patch.object(mod.subprocess, "run", return_value=fake):
                ok, message, diagnostic = mod.validate_subtitle_stream_track(
                    "/fake/ffmpeg", "/fake/ffprobe", "/media/output.mkv", 0,
                    {"codec_name": "ass"}, log, "字幕 1")
            self.assertFalse(ok)
            self.assertTrue(diagnostic)
            self.assertIn("退出碼 1", message)

    def test_fatal_stderr_rejects_even_zero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "test.log"
            fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="Error opening output file")
            with patch.object(mod.subprocess, "run", return_value=fake):
                ok, message, diagnostic = mod.validate_subtitle_stream_track(
                    "/fake/ffmpeg", "/fake/ffprobe", "/media/output.mkv", 0,
                    {"codec_name": "subrip"}, log, "字幕 1")
            self.assertFalse(ok)
            self.assertTrue(diagnostic)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_subrip_to_ass_pipe_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            srt = root / "sample.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            ok, message, diagnostic = mod.validate_subtitle_stream_track(
                shutil.which("ffmpeg"), shutil.which("ffprobe"), srt, 0,
                {"codec_name": "subrip"}, root / "test.log", "字幕 1")
            self.assertTrue(ok, message)
            self.assertFalse(diagnostic)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_ass_to_ass_pipe_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ass = root / "sample.ass"
            ass.write_text(
                "[Script Info]\nScriptType: v4.00+\n\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n"
                "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello\n",
                encoding="utf-8")
            ok, message, diagnostic = mod.validate_subtitle_stream_track(
                shutil.which("ffmpeg"), shutil.which("ffprobe"), ass, 0,
                {"codec_name": "ass"}, root / "test.log", "字幕 1")
            self.assertTrue(ok, message)
            self.assertFalse(diagnostic)


if __name__ == "__main__":
    unittest.main()
