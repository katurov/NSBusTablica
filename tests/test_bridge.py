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


def stop_data(n, base=60):
    """n routes R1..Rn, route k arriving in base*k seconds (nearest first = R1, R2, ...)."""
    return {"buses": [{"line": f"R{k}", "seconds": base * k, "status": "live"} for k in range(1, n + 1)]}


def page_lines(rows):
    return [r.split("|")[1] for r in rows]


class PagingTest(unittest.TestCase):
    HEADER_FONT_W, TIME_W, MAX_HDR = 7, 30, 11    # 7x13B font, "HH:MM" in 6x12, firmware hdrStop[12]

    def cycle(self, n, pushes):
        p, out = B.Pager(4), []
        for _ in range(pushes):
            rows, page, pages = p.next_page(B.route_rows(stop_data(n)))
            out.append((page_lines(rows), page, pages))
        return out

    def test_five_routes_two_pages_wrap(self):
        out = self.cycle(5, 3)
        self.assertEqual(out[0], (["R1", "R2", "R3", "R4"], 0, 2))
        self.assertEqual(out[1], (["R5"], 1, 2))
        self.assertEqual(out[2], (["R1", "R2", "R3", "R4"], 0, 2))

    def test_eight_routes(self):
        out = self.cycle(8, 3)
        self.assertEqual([o[0] for o in out], [["R1", "R2", "R3", "R4"], ["R5", "R6", "R7", "R8"],
                                               ["R1", "R2", "R3", "R4"]])
        self.assertEqual({o[2] for o in out}, {2})

    def test_four_routes_single_page_unchanged(self):
        out = self.cycle(4, 3)
        self.assertEqual(out, [(["R1", "R2", "R3", "R4"], 0, 1)] * 3)
        data = stop_data(4)
        p = B.Pager(4)
        rows, _, _ = p.next_page(B.route_rows(data))
        self.assertEqual(B.frame_from_rows("6539", rows)[1:], B.build_frame(data, "6539")[1:])

    def test_zero_routes(self):
        rows, page, pages = B.Pager(4).next_page(B.route_rows({"buses": []}))
        self.assertEqual((rows, page, pages), ([], 0, 1))
        self.assertEqual(B.frame_from_rows("6539", rows)[1:], ["E"])

    def test_order_frozen_within_cycle(self):
        p = B.Pager(4)
        rows, _, _ = p.next_page(B.route_rows(stop_data(6)))
        self.assertEqual(page_lines(rows), ["R1", "R2", "R3", "R4"])
        # R5 suddenly becomes the nearest bus: page 2 must still show R5, R6 (no repeat/skip)
        d = stop_data(6)
        d["buses"][4]["seconds"] = 5
        rows, page, _ = p.next_page(B.route_rows(d))
        self.assertEqual((page_lines(rows), page), (["R5", "R6"], 1))
        # new cycle: re-sorted nearest-first
        rows, page, _ = p.next_page(B.route_rows(d))
        self.assertEqual((page_lines(rows)[0], page), ("R5", 0))

    def test_routes_vanish_mid_cycle_restarts(self):
        p = B.Pager(4)
        p.next_page(B.route_rows(stop_data(5)))
        rows, page, pages = p.next_page(B.route_rows(stop_data(3)))
        self.assertEqual((page_lines(rows), page, pages), (["R1", "R2", "R3"], 0, 1))

    def test_reset_goes_to_page_one(self):
        p = B.Pager(4)
        p.next_page(B.route_rows(stop_data(9)))
        p.reset()
        _, page, pages = p.next_page(B.route_rows(stop_data(9)))
        self.assertEqual((page, pages), (0, 3))

    def test_header_label(self):
        c = B.StopCycler(["6539", "15889", "6712"], start="6539")
        self.assertEqual(B.header_label(c, 0, 1), "6539 1/3")
        c.next()
        self.assertEqual(B.header_label(c, 1, 4), "15889 p2/4")
        for label in (B.header_label(c, 3, 4), B.header_label(c, 8, 9), "15889 3/3"):
            self.assertLessEqual(len(label), self.MAX_HDR)
            self.assertLessEqual(len(label) * self.HEADER_FONT_W + 4 + self.TIME_W, 128)

    def test_button_resets_page_and_moves_stop(self):
        c, p = B.StopCycler(["6539", "15889"], start="6539"), B.Pager(4)
        p.next_page(B.route_rows(stop_data(6)))           # 6539 page 1, next would be page 2
        for _ in range(B.count_presses(B.LineReader().poll(FakeSerial("BTN\n")))):
            c.next()
        p.reset()
        rows, page, pages = p.next_page(B.route_rows(stop_data(6)))
        self.assertEqual(B.header_label(c, page, pages), "15889 p1/2")
        self.assertEqual(page_lines(rows), ["R1", "R2", "R3", "R4"])


if __name__ == "__main__":
    unittest.main()
