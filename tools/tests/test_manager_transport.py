"""Exercise the actual Manager transport with changing identity and descriptors."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("g++"), "requires a host C++ compiler")
class ManagerTransportTests(unittest.TestCase):
    def test_identity_and_replaced_driver_descriptors_are_not_cached(self):
        with tempfile.TemporaryDirectory(prefix="xkcnsu-transport-") as temporary:
            binary = Path(temporary) / "manager-transport"
            subprocess.run([
                "g++", "-std=c++23", "-pthread",
                str(ROOT / "tools/tests/native_manager_transport.cpp"),
                "-I" + str(ROOT),
                "-I" + str(ROOT / "tools/tests/native-include"),
                "-Wl,--wrap=readlink", "-Wl,--wrap=ioctl",
                "-o", str(binary),
            ], check=True, capture_output=True, text=True, timeout=60)
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
