"""Software updates: permanently locked to local self-managed mode."""
import asyncio
from pathlib import Path

STATE = Path('/var/lib/thinksmart-update')
STATUS = STATE / 'status.json'
REQUESTS = STATE / 'requests'
SELF_MANAGED_NOTE = '自主维护版 · 已停用第三方在线更新'


def read():
    """Static self-managed status; never polls or offers external releases."""
    return {
        'enabled': False,
        'self_managed': True,
        'note': SELF_MANAGED_NOTE,
    }


def request(action, version='', system=''):
    """Refuse all third-party online check/install requests."""
    return False


async def watch(publish):
    """Publish static self-managed state once and sleep forever (zero CPU wakeups)."""
    publish('update', read())
    await asyncio.Event().wait()
