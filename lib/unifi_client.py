"""Cliente de la API del controlador UniFi Network.

Soporta el controlador autoinstalado (login en /api/login) y las consolas
UniFi OS (login en /api/auth/login y prefijo /proxy/network).
"""
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class UnifiError(Exception):
    """Error de conexión o respuesta rc != ok del controlador."""


class UnifiClient:
    def __init__(self, base_url, username, password, site='default',
                 unifi_os=False, verify_ssl=False, timeout=20):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.site = site or 'default'
        self.unifi_os = unifi_os
        self.timeout = timeout
        self.prefix = '/proxy/network' if unifi_os else ''
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self._logged = False

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()

    def login(self):
        path = '/api/auth/login' if self.unifi_os else '/api/login'
        try:
            r = self.session.post(self.base_url + path, timeout=self.timeout,
                                  json={'username': self.username, 'password': self.password})
        except requests.RequestException as e:
            raise UnifiError("No se pudo conectar al controlador: %s" % e) from e
        if r.status_code != 200:
            raise UnifiError("Login rechazado (HTTP %s). Verifique usuario y contraseña." % r.status_code)
        token = r.headers.get('X-CSRF-Token')
        if token:
            self.session.headers['X-CSRF-Token'] = token
        self._logged = True

    def logout(self):
        if not self._logged:
            return
        path = '/api/auth/logout' if self.unifi_os else '/api/logout'
        try:
            self.session.post(self.base_url + path, timeout=self.timeout)
        except requests.RequestException:
            pass
        self._logged = False

    def _request(self, method, path, payload=None, retry=True):
        url = '%s%s/api/s/%s%s' % (self.base_url, self.prefix, self.site, path)
        try:
            r = self.session.request(method, url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise UnifiError("Error de comunicación con el controlador: %s" % e) from e
        if r.status_code == 401 and retry:
            self.login()
            return self._request(method, path, payload, retry=False)
        try:
            body = r.json()
        except ValueError as e:
            raise UnifiError("Respuesta inválida del controlador (HTTP %s)" % r.status_code) from e
        meta = body.get('meta', {})
        if meta.get('rc') != 'ok':
            raise UnifiError("El controlador devolvió un error: %s" % (meta.get('msg') or r.status_code))
        return body.get('data', [])

    # ------------------------------------------------------------------
    def server_version(self):
        r = self.session.get(self.base_url + self.prefix + '/status', timeout=self.timeout)
        return r.json().get('meta', {}).get('server_version')

    def list_wlans(self):
        return self._request('GET', '/rest/wlanconf')

    def get_wlan(self, wlan_id):
        data = self._request('GET', '/rest/wlanconf/%s' % wlan_id)
        if not data:
            raise UnifiError("La red WiFi %s no existe en el controlador" % wlan_id)
        return data[0]

    def set_mac_filter_list(self, wlan, macs):
        """Reemplaza la lista de MACs conservando la política y el estado del filtro."""
        payload = {
            'mac_filter_enabled': wlan.get('mac_filter_enabled', False),
            'mac_filter_policy': wlan.get('mac_filter_policy', 'allow'),
            'mac_filter_list': macs,
        }
        return self._request('PUT', '/rest/wlanconf/%s' % wlan['_id'], payload)

    def list_known_clients(self):
        return self._request('GET', '/rest/user')

    def list_active_clients(self):
        """Clientes conectados ahora: AP, SSID, canal, señal, velocidades y bytes acumulados."""
        return self._request('GET', '/stat/sta')

    def list_devices_basic(self):
        """Equipos UniFi (APs, switches) con MAC y nombre."""
        return self._request('GET', '/stat/device-basic')
