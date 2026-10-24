import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from process_promos import convert_wav_to_aiff


class PrecisionTests(unittest.TestCase):
    def test_integer_and_float_precision_and_audio_are_preserved(self):
        for codec in ["pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"]:
            with self.subTest(codec=codec), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "source.wav"
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                "sine=frequency=1137:duration=0.2:sample_rate=48000",
                                "-c:a", codec, str(path)], check=True, capture_output=True)
                converted, error = convert_wav_to_aiff(str(path), root)
                self.assertIsNone(error)
                def pcm(file):
                    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(file),
                                           "-f", "f64le", "-c:a", "pcm_f64le", "-"],
                                          check=True, capture_output=True).stdout
                self.assertEqual(pcm(path), pcm(converted))
                metadata = json.loads(subprocess.run([
                    "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                    "stream=codec_name,sample_rate", "-of", "json", converted],
                    check=True, capture_output=True, text=True).stdout)["streams"][0]
                self.assertEqual(metadata["sample_rate"], "48000")
                self.assertEqual(metadata["codec_name"], codec.replace("le", "be"))


if __name__ == "__main__":
    unittest.main()
