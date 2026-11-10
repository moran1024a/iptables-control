import asyncio
import os
import unittest
from test_server import Fixture, ROOT
from test_imports import ss_uri
import database


@unittest.skipUnless(os.environ.get('PLAYWRIGHT_MODULE'), 'Set PLAYWRIGHT_MODULE for browser checks')
class ExternalBrowserTests(Fixture, unittest.TestCase):
    def test_import_group_sort_and_mixed_subscription(self):
        from aiohttp import web
        import transport
        node=self.node();self.ready(node)
        target=self.call('api_target_save',dict(uri=ss_uri()))['id']
        self.save(self.graph([node],target));self.settle()
        with self.db:
            database.set_setting(self.db,'must_change','0')

        async def check():
            runner=web.AppRunner(transport.create_app())
            await runner.setup()
            try:
                site=web.TCPSite(runner,'127.0.0.1',0)
                await site.start()
                port=site._server.sockets[0].getsockname()[1]
                env=dict(os.environ,TEST_URL=f'http://127.0.0.1:{port}'+database.setting(self.db,'panel_path')+'/')
                proc=await asyncio.create_subprocess_exec('node',str(ROOT/'tests/browser_external.js'),env=env,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
                try:
                    output,_=await asyncio.wait_for(proc.communicate(),75)
                except BaseException:
                    proc.kill();await proc.wait();raise
                self.assertEqual(proc.returncode,0,output.decode())
            finally:
                await runner.cleanup()
        asyncio.run(check())
