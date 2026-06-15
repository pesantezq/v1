from __future__ import annotations
import sys, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fmp_client import FMPClient


class TestByteHook(unittest.TestCase):
    def _client(self, tmp):
        return FMPClient(api_key="k", cache_dir=Path(tmp))

    def test_last_response_bytes_recorded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            c = self._client(td)
            body = b'[{"symbol":"AAPL","price":1.0}]'
            fake = MagicMock()
            fake.__enter__.return_value.read.return_value = body
            with patch("urllib.request.urlopen", return_value=fake):
                c._raw_get("quote", {"symbol": "AAPL"})
            self.assertEqual(c.last_response_bytes, len(body))

    def test_last_response_bytes_default_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._client(td).last_response_bytes, 0)


if __name__ == "__main__":
    unittest.main()
