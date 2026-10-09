"""Offline tests for oled_bridge.py: stop cycling (BOOT button) and serial line reading."""
import os, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    import serial  # noqa: F401
except ImportError:                     # pyserial not installed: a stub is enough for these tests
    stub = types.ModuleType("serial")
    stub.SerialException = type("SerialException", (OSError,), {})
    stub.Serial = None
    sys.modules["serial"] = stub
import oled_bridge as B                 # noqa: E402


class FakeSerial:
    """Minimal pyserial stand-in: data arrives in chunks, read via in_waiting/read."""

    def __init__(self, *chunks):
        self.chunks = [c if isinstance(c, bytes) else c.encode() for c in chunks]

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, n):
        c = self.chunks.pop(0)
        assert len(c) == n
        return c


class StopCyclerTest(unittest.TestCase):
    def test_starts_at_start_stop_and_wraps(self):
        c = B.StopCycler(["6539", "15889", "6712"], start="6539")
        self.assertEqual(c.current, "6539")
        self.assertEqual(c.label(), "6539 1/3")
        self.assertEqual([c.next() for _ in range(4)], ["15889", "6712", "6539", "15889"])
        self.assertEqual(c.label(), "15889 2/3")

    def test_start_not_in_list_is_prepended(self):
        c = B.StopCycler(["15889", "6712"], start="6551")
        self.assertEqual(c.stops, ["6551", "15889", "6712"])
        self.assertEqual(c.current, "6551")

    def test_start_in_middle(self):
        c = B.StopCycler(["6539", "15889", "6712"], start="6712")
        self.assertEqual(c.next(), "6539")

    def test_single_stop(self):
        c = B.StopCycler([], start="6539")
        self.assertEqual(c.next(), "6539")
        self.assertEqual(c.label(), "6539")

    def test_dedup_and_update_keeps_current(self):
        c = B.StopCycler(["6539", "6539", " 6712 "], start="6539")
        self.assertEqual(c.stops, ["6539", "6712"])
        c.next()
        c.update(["6539", "15889", "6712"])
        self.assertEqual(c.current, "6712")
        self.assertEqual(c.label(), "6712 3/3")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            B.StopCycler([])


class SerialInputTest(unittest.TestCase):
    def test_partial_lines_and_presses(self):
        r = B.LineReader()
        ser = FakeSerial("OK\r\nB", "TN\r", "\nI2C device at 0x3C\nBTN\n")
        got = []
        for _ in range(5):              # extra polls with nothing waiting are harmless
            got += r.poll(ser)
        self.assertEqual(got, ["OK", "BTN", "I2C device at 0x3C", "BTN"])
        self.assertEqual(B.count_presses(got), 2)

    def test_nothing_waiting_does_not_block(self):
        self.assertEqual(B.LineReader().poll(FakeSerial()), [])

    def test_garbage_without_newline_is_dropped(self):
        r = B.LineReader(maxlen=10)
        self.assertEqual(r.poll(FakeSerial("x" * 50)), [])
        self.assertEqual(r.poll(FakeSerial("NEXT\n")), ["NEXT"])
        self.assertEqual(B.count_presses(["NEXT", "btn", "OK"]), 2)

    def test_button_press_advances_and_header_shows_stop(self):
        c = B.StopCycler(["6539", "15889", "6712"], start="6539")
        lines = B.LineReader().poll(FakeSerial("OK\nBTN\n"))
        for _ in range(B.count_presses(lines)):
            c.next()
        data = {"buses": [{"line": "71", "seconds": 130, "status": "live"},
                          {"line": "52", "seconds": 600, "status": "scheduled"}]}
        frame = B.build_frame(data, c.current, label=c.label())
        self.assertTrue(frame[0].startswith("H|15889 2/3|"))
        self.assertEqual(frame[1:], ["R|71|G:2", "R|52|P:10", "E"])


if __name__ == "__main__":
    unittest.main()
