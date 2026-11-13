"""The built panel is a fixed set of files under the secret path; everything else is a 404."""
import gzip
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import api
import settings
import static_files


class StaticFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "web"
        (self.root / "assets" / "deep").mkdir(parents=True)
        (self.root / "index.html").write_text("<!doctype html><title>ictl</title>")
        (self.root / "theme.js").write_text("// theme")
        (self.root / "assets" / "index-AbC_12-x.js").write_text("export const big = '" + "x" * 4000 + "';")
        (self.root / "assets" / "index-AbC.css").write_text("a{}")
        (self.root / "assets" / "index-AbC.js.map").write_text("{}")
        (self.root / "assets" / ".hidden.js").write_text("secret")
        (self.root / "assets" / "deep" / "nested.js").write_text("nested")
        (Path(self.temp.name) / "outside.js").write_text("outside")
        os.symlink(Path(self.temp.name) / "outside.js", self.root / "assets" / "link.js")
        self.patch = patch.object(settings, "WEB_DIR", str(self.root))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def get(self, path, **headers):
        request = api.Request("GET", "/panel" + path, headers.items(), b"", "203.0.113.9", panel_path="/panel", sync_path="/sync")
        request.route("GET")
        code, data, response_headers = request.response
        return code, data, dict(response_headers)

    def test_entry_document_is_revalidated_and_locked_down(self):
        code, data, headers = self.get("/")
        self.assertEqual((code, data), (200, b"<!doctype html><title>ictl</title>"))
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.get("/index.html")[0], 200)

    def test_hashed_assets_are_immutable_and_compressed_on_request(self):
        code, data, headers = self.get("/assets/index-AbC_12-x.js")
        self.assertEqual(code, 200)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        packed_code, packed, packed_headers = self.get("/assets/index-AbC_12-x.js", **{"Accept-Encoding": "br, gzip;q=0.8"})
        self.assertEqual((packed_code, packed_headers["Content-Encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(packed), data)
        self.assertLess(len(packed), len(data))
        # Too small to be worth compressing.
        self.assertNotIn("Content-Encoding", self.get("/assets/index-AbC.css", **{"Accept-Encoding": "gzip"})[2])

    def test_only_known_build_files_exist(self):
        for path in (
            "/assets/index-AbC.js.map", "/assets/.hidden.js", "/assets/deep/nested.js", "/assets/link.js",
            "/assets/", "/assets", "/missing.js", "/../outside.js", "/assets/../../outside.js",
            "/assets/%2e%2e/%2e%2e/outside.js", "/assets//index-AbC.css", "/nodes", "/index.html/",
        ):
            with self.subTest(path=path):
                code, data, headers = self.get(path)
                self.assertEqual((code, data), (404, settings.NOT_FOUND.encode()))
        request = api.Request("GET", "/other/index.html", (), b"", "203.0.113.9", panel_path="/panel", sync_path="/sync")
        request.route("GET")
        self.assertEqual(request.response[0], 404)

    def test_missing_build_is_reported_before_serving(self):
        import cli
        (self.root / "index.html").unlink()
        static_files._loaded.clear()
        with patch("shutil.which", return_value="/usr/bin/openssl"), self.assertRaises(Exception) as caught:
            cli.cmd_serve(None)
        self.assertIn("缺少面板文件", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
