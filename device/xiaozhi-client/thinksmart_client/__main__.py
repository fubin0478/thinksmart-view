"""python -m thinksmart_client: self-owned ThinkSmart View voice client."""
import asyncio
import logging

from aiohttp import web

from . import app as app_mod, audio, bridge, config, mcp
from .app import Client
from .web import make_app


async def main():
    bridge.install_native(audio, mcp, app_mod)
    cfg = config.load()
    client = Client(cfg)
    client.loop = asyncio.get_running_loop()
    client._send_q = asyncio.Queue(maxsize=200)
    # Bind :8090 before audio/KWS/Bluetooth startup so kiosk reaches Home immediately on boot
    runner = web.AppRunner(make_app(client), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', cfg['port']).start()
    logging.getLogger(__name__).info('page on http://127.0.0.1:%d/', cfg['port'])
    await client.start()
    await asyncio.Event().wait()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    asyncio.run(main())
