import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

"""Exercise real DOM rendering, modes, white activity bars and target-only forms in Chrome."""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from test_server import ROOT
import network_quality as q


class InstallCommandTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_download_does_not_follow_legacy_link_and_cleans_temporary_file(self):
        source = (ROOT / "server/web/app.js").read_text()
        code = "\n".join(
            line
            for line in source.splitlines()
            if line.startswith("  const quote =") or line.startswith("  const install =")
        )
        url = "https://example.com/sync'quoted"
        command = subprocess.check_output(
            [
                "node",
                "-e",
                'const d={installer_url:"https://example.com/install-agent.sh"};const n={desired_mode:"active"};const url='
                + json.dumps(url)
                + ";\n"
                + code
                + '\nprocess.stdout.write(install("test-key"));',
            ],
            text=True,
        )
        for fail in ("0", "1"):
            with self.subTest(download_failure=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bin_dir = root / "bin"
                bin_dir.mkdir()
                tmp = root / "tmp"
                tmp.mkdir()
                installed = root / "installed"
                installed.write_text("installed Agent must remain unchanged")
                (root / "ictl.sh").symlink_to(installed)
                curl = bin_dir / "curl"
                curl.write_text("""#!/bin/bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then output="$2"; shift; fi
  shift
done
printf '%s\\n' "$output" > "$MOCK_ROOT/download-path"
printf '%s\\n' '#!/bin/bash' 'printf "%s\\n" "$@" > "$MOCK_ROOT/arguments"' > "$output"
[ "$FAIL_DOWNLOAD" = 0 ]
""")
                curl.chmod(0o755)
                sudo = bin_dir / "sudo"
                sudo.write_text('#!/bin/bash\nexec "$@"\n')
                sudo.chmod(0o755)
                env = {
                    **os.environ,
                    "PATH": str(bin_dir) + ":" + os.environ["PATH"],
                    "TMPDIR": str(tmp),
                    "MOCK_ROOT": str(root),
                    "FAIL_DOWNLOAD": fail,
                }
                result = subprocess.run(
                    ["bash", "-c", command], cwd=root, env=env, capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0 if fail == "0" else 1, result.stderr)
                self.assertEqual(installed.read_text(), "installed Agent must remain unchanged")
                self.assertTrue((root / "ictl.sh").is_symlink())
                self.assertFalse(Path((root / "download-path").read_text().strip()).exists())
                if fail == "0":
                    self.assertEqual(
                        (root / "arguments").read_text().splitlines(),
                        ["--url", url, "--key", "test-key", "--kind", "active"],
                    )
                else:
                    self.assertFalse((root / "arguments").exists())


class CanvasBrowserTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("PLAYWRIGHT_MODULE"),
        "Set PLAYWRIGHT_MODULE to run Chrome interaction checks",
    )
    def test_edit_save_switch_and_quality(self):
        import asyncio
        from aiohttp import web
        from test_server import Fixture
        import database, settings, transport

        fixture = Fixture()
        fixture.setUp()
        try:
            a, b, c, d, e = [
                fixture.node(x) for x in ["入口 A", "入口 B", "中转 C", "中转 D", "备用 E"]
            ]
            fixture.ready(a, b, c, d, e)
            t, u = fixture.target(), fixture.target("203.0.113.10")
            graph = fixture.graph([a, c], t)
            second = fixture.graph([b, d], u)
            graph["vertices"] += second["vertices"]
            graph["edges"] += second["edges"] + [[f"v{a}", f"v{d}"]]
            ident = fixture.save(graph)
            fixture.settle()
            with fixture.db:
                database.set_setting(fixture.db, "must_change", "0")
                fixture.db.execute("UPDATE nodes SET last_seen=1 WHERE id=?", (b,))

            async def check():
                runner = web.AppRunner(transport.create_app())
                await runner.setup()
                try:
                    site = web.TCPSite(runner, "127.0.0.1", 0)
                    await site.start()
                    port = site._server.sockets[0].getsockname()[1]
                    env = dict(
                        os.environ,
                        TEST_URL=f"http://127.0.0.1:{port}"
                        + database.setting(fixture.db, "panel_path")
                        + "/",
                        TEST_ROUTE=str(ident),
                    )
                    proc = await asyncio.create_subprocess_exec(
                        "node",
                        str(ROOT / "tests/browser_flow.js"),
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    try:
                        out, _ = await asyncio.wait_for(proc.communicate(), 90)
                    except BaseException:
                        proc.kill()
                        await proc.wait()
                        raise
                    self.assertEqual(proc.returncode, 0, out.decode())
                finally:
                    await runner.cleanup()

            asyncio.run(check())
        finally:
            fixture.tearDown()
