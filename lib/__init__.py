import re

from .routeros_api import RouterOSApi, RouterOSError
from .unifi_client import UnifiClient, UnifiError

_HEX = re.compile(r'[^0-9a-fA-F]')


def normalize_mac(value):
    """Devuelve la MAC como aa:bb:cc:dd:ee:ff o None si no es válida."""
    if not value:
        return None
    digits = _HEX.sub('', value).lower()
    if len(digits) != 12:
        return None
    return ':'.join(digits[i:i + 2] for i in range(0, 12, 2))


def is_random_mac(mac):
    """True si la MAC es localmente administrada (típica de MAC aleatoria en celulares)."""
    return bool(mac) and bool(int(mac[:2], 16) & 0x02)
