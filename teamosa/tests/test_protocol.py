import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from teamosa import protocol as p


class EncodeBrew(unittest.TestCase):
    def test_verified_frame(self):
        # The frame sent on the real machine on 2026-10-26: it brewed, display showed 140 F.
        self.assertEqual(p.encode_brew(60, 3), b"TEA60,6,25U")
        self.assertEqual(p.encode_brew(60, 3).hex(), "54454136302c362c323555")

    def test_other_app_values(self):
        self.assertEqual(p.encode_brew(95, 3.5), b"TEA95,7,25U")
        self.assertEqual(p.encode_brew(80, 9.5), b"TEA80,19,25U")

    def test_half_minute_steps_only(self):
        with self.assertRaises(p.ProtocolError):
            p.encode_brew(60, 3.25)

    def test_app_limits_enforced(self):
        for temp, minutes in [(55, 3), (100, 3), (62, 3), (60, 2.5), (60, 10)]:
            with self.assertRaises(p.ProtocolError, msg=(temp, minutes)):
                p.encode_brew(temp, minutes)

    def test_extended_lifts_limits_only(self):
        self.assertEqual(p.encode_brew(100, 3.5, extended=True), b"TEA100,7,25U")
        with self.assertRaises(p.ProtocolError):
            p.encode_brew(0, 3, extended=True)

    def test_fractional_temperature_rejected(self):
        with self.assertRaises(p.ProtocolError):
            p.encode_brew(60.5, 3)


class DecodeReply(unittest.TestCase):
    def test_known_frames(self):
        self.assertEqual(p.decode_reply(bytes.fromhex("435550"))[0], "CUP")
        self.assertEqual(p.decode_reply(bytes.fromhex("454155"))[0], "EAU")
        self.assertEqual(p.decode_reply(bytes.fromhex("434f56"))[0], "COV")

    def test_unknown_frame(self):
        self.assertIsNone(p.decode_reply(b"\x01\x02")[0])


if __name__ == "__main__":
    unittest.main()
