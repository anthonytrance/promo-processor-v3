import struct
import unittest

from audio_length import length_from_header


def wav(seconds, rate=44100, channels=2, bits=24):
    per_second = rate * channels * bits // 8
    data = per_second * seconds
    fmt = struct.pack("<HHIIHH", 1, channels, rate, per_second, channels * bits // 8, bits)
    head = b"RIFF" + struct.pack("<I", 36 + data) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    return head + b"data" + struct.pack("<I", data), len(head) + 8 + data


def aiff(seconds, rate=44100):
    exponent, value = 16383 + 15, rate  # 44100 < 2**16, normalised mantissa
    mantissa = value << (63 - 15)
    comm = struct.pack(">hIh", 2, rate * seconds, 24) + struct.pack(">HQ", exponent, mantissa)
    return b"FORM" + struct.pack(">I", 0) + b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm


def mp3_cbr(seconds, kbps=320):
    frame = bytes([0xFF, 0xFB, 0xE0, 0x00])  # MPEG1 layer 3, 320 kbps, 44.1 kHz, stereo
    tag_body = b"\x00" * 100
    tag = b"ID3\x03\x00\x00" + bytes([0, 0, 0, len(tag_body)]) + tag_body
    return tag + frame + b"\x00" * 200, len(tag) + kbps * 1000 // 8 * seconds


class LengthTests(unittest.TestCase):
    def test_wav(self):
        head, total = wav(397)
        self.assertAlmostEqual(length_from_header(head, total), 397)

    def test_aiff(self):
        self.assertAlmostEqual(length_from_header(aiff(377), 10 ** 8), 377)

    def test_mp3_constant_bitrate_after_id3(self):
        head, total = mp3_cbr(214)
        self.assertAlmostEqual(length_from_header(head, total), 214, delta=1)

    def test_large_id3_asks_for_second_read(self):
        head = b"ID3\x04\x00\x00" + bytes([0, 8, 0, 0]) + b"\x00" * 100  # 131072-byte tag
        self.assertEqual(length_from_header(head, 10 ** 7), ("need", 131082))

    def test_unknown_format(self):
        self.assertIsNone(length_from_header(b"PK\x03\x04" + b"\x00" * 60, 1000))


class DropboxProbeTests(unittest.TestCase):
    def probe(self, data, name="A - Song (Radio Edit).mp3"):
        from unittest import mock
        import dropbox_promos
        adapter = dropbox_promos.DropboxPromos.__new__(dropbox_promos.DropboxPromos)
        adapter.store, adapter.lengths = mock.Mock(), {}
        response = mock.Mock()
        response.iter_content.return_value = (data[i:i + 16384] for i in range(0, len(data), 16384))
        adapter.client = mock.Mock(**{"sharing_get_shared_link_file.return_value": (None, response)})
        entry = {"id": "id:1", "name": name, "path": "/" + name, "size": len(data)}
        return adapter.length("src", entry), response

    def test_cover_art_tag_read_through_stream(self):
        frame = bytes([0xFF, 0xFB, 0xE0, 0x00]) + b"\x00" * 1040
        tag = b"ID3\x00\x00" + bytes([0, 8, 0, 0]) + b"\x00" * 131072
        data = tag + frame * 600
        seconds, response = self.probe(data)
        self.assertAlmostEqual(seconds, (len(data) - len(tag)) * 8 / 320000, places=3)
        response.close.assert_called_once()

    def test_unreadable_gives_none(self):
        self.assertIsNone(self.probe(b"<html>" + b"x" * 70000)[0])


if __name__ == "__main__":
    unittest.main()
