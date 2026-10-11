"""Builds and runs the host test of the firmware frame parser/pager (firmware/include/frame.h) with c++.
Skipped when no C++ compiler is installed."""
import os, shutil, subprocess, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")


@unittest.skipUnless(CXX, "no C++ compiler")
class FirmwareFrameTest(unittest.TestCase):
    def test_frame_parser_and_pager(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "test_frame")
            subprocess.run([CXX, "-std=c++11", "-Wall", "-I", os.path.join(ROOT, "firmware", "include"),
                            os.path.join(ROOT, "tests", "firmware", "test_frame.cpp"), "-o", exe], check=True)
            r = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
