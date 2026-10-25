import argparse
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import transcribe

TEST_ROOT = Path(__file__).resolve().parent / "data"
TEST_ROOT.mkdir(exist_ok=True)

class OutputTests(unittest.TestCase):
    def test_cyrillic_transcript(self):
        model = SimpleNamespace(transcribe=lambda *a, **kw: (
            iter([SimpleNamespace(text=" Привет, мир! ", end=2)]), SimpleNamespace(language="ru")))
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            output = Path(folder) / "текст.txt"
            transcribe.transcribe(model, argparse.Namespace(audio="x.mp3", language="auto", overwrite=False), output)
            self.assertEqual(output.read_text(encoding="utf-8-sig"), "Привет, мир!\n")
            self.assertEqual(list(Path(folder).glob("*.partial")), [])

    def test_late_failure_preserves_existing_output(self):
        def segments():
            yield SimpleNamespace(text="Incomplete", end=1)
            raise RuntimeError("CUDA out of memory")
        model = SimpleNamespace(transcribe=lambda *a, **kw: (segments(), SimpleNamespace(language="ru")))
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            output = Path(folder) / "text.txt"
            output.write_text("original", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "CUDA"):
                transcribe.transcribe(model, argparse.Namespace(audio="x", language="auto", overwrite=True), output)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(Path(folder).glob("*.partial")), [])

    def test_existing_output_rejected_before_model_load(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            audio = Path(folder) / "audio.wav"
            audio.touch()
            audio.with_suffix(".txt").touch()
            with patch.object(transcribe, "register_nvidia_dlls") as register:
                self.assertEqual(transcribe.main([str(audio)]), 1)
                register.assert_not_called()

    def test_gpu_failure_during_iteration_retries_on_cpu(self):
        devices = []
        def load(*a, **kw):
            devices.append(kw["device"])
            return object()
        fake_ct = SimpleNamespace(get_cuda_device_count=lambda: 1)
        fake_fw = SimpleNamespace(WhisperModel=load, download_model=lambda *a, **kw: "unused")
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            audio = Path(folder) / "audio.wav"
            audio.touch()
            with patch.dict("sys.modules", ctranslate2=fake_ct, faster_whisper=fake_fw), \
                    patch.object(transcribe, "register_nvidia_dlls"), \
                    patch.object(transcribe, "transcribe", side_effect=[RuntimeError("CUDA out of memory"), None]) as work:
                self.assertEqual(transcribe.main([str(audio), "--offline"]), 0)
                self.assertEqual(devices, ["cuda", "cpu"])
                self.assertEqual(work.call_count, 2)


if __name__ == "__main__":
    unittest.main()
