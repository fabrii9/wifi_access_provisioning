"""Cliente mínimo de la API binaria de RouterOS (puertos 8728 / 8729).

Implementa el protocolo de palabras con longitud codificada descrito en
https://help.mikrotik.com/docs/display/ROS/API sin dependencias externas.
"""
import binascii
import hashlib
import socket
import ssl


class RouterOSError(Exception):
    """Error devuelto por el router (!trap / !fatal) o de conexión."""


class RouterOSApi:
    def __init__(self, host, username, password, port=8728, use_ssl=False,
                 verify_ssl=False, timeout=15):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.sock = None

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            if self.use_ssl:
                ctx = ssl.create_default_context()
                if not self.verify_ssl:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except (OSError, ssl.SSLError) as e:
            raise RouterOSError("No se pudo conectar a %s:%s (%s)" % (self.host, self.port, e)) from e
        self._login()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _login(self):
        _replies, done = self._talk(['/login', '=name=' + self.username, '=password=' + self.password])
        challenge = done.get('ret')
        if challenge:  # RouterOS < 6.43: login por desafío MD5
            md5 = hashlib.md5()
            md5.update(b'\x00')
            md5.update(self.password.encode())
            md5.update(binascii.unhexlify(challenge))
            self._talk(['/login', '=name=' + self.username, '=response=00' + md5.hexdigest()])

    # ------------------------------------------------------------------
    # Protocolo
    # ------------------------------------------------------------------
    @staticmethod
    def _encode_length(n):
        if n < 0x80:
            return bytes([n])
        if n < 0x4000:
            return (n | 0x8000).to_bytes(2, 'big')
        if n < 0x200000:
            return (n | 0xC00000).to_bytes(3, 'big')
        if n < 0x10000000:
            return (n | 0xE0000000).to_bytes(4, 'big')
        return b'\xF0' + n.to_bytes(4, 'big')

    def _recv(self, n):
        buf = b''
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except (OSError, ssl.SSLError) as e:
                raise RouterOSError("Error de lectura: %s" % e) from e
            if not chunk:
                raise RouterOSError("El router cerró la conexión")
            buf += chunk
        return buf

    def _read_length(self):
        b = self._recv(1)[0]
        if b & 0x80 == 0x00:
            return b
        if b & 0xC0 == 0x80:
            return ((b & 0x3F) << 8) + self._recv(1)[0]
        if b & 0xE0 == 0xC0:
            return ((b & 0x1F) << 16) + int.from_bytes(self._recv(2), 'big')
        if b & 0xF0 == 0xE0:
            return ((b & 0x0F) << 24) + int.from_bytes(self._recv(3), 'big')
        return int.from_bytes(self._recv(4), 'big')

    def _write_sentence(self, words):
        data = b''
        for w in words:
            raw = w.encode('utf-8')
            data += self._encode_length(len(raw)) + raw
        data += b'\x00'
        try:
            self.sock.sendall(data)
        except (OSError, ssl.SSLError) as e:
            raise RouterOSError("Error de escritura: %s" % e) from e

    def _read_sentence(self):
        words = []
        while True:
            n = self._read_length()
            if n == 0:
                return words
            words.append(self._recv(n).decode('utf-8', errors='replace'))

    def _talk(self, words):
        """Envía un comando y devuelve (lista de !re, atributos de !done)."""
        self._write_sentence(words)
        replies, trap = [], None
        while True:
            sentence = self._read_sentence()
            if not sentence:
                continue
            kind, attrs = sentence[0], {}
            for w in sentence[1:]:
                if w.startswith('='):
                    key, _sep, val = w[1:].partition('=')
                    attrs[key] = val
            if kind == '!re':
                replies.append(attrs)
            elif kind == '!trap':
                trap = attrs.get('message', 'error desconocido')
            elif kind == '!fatal':
                raise RouterOSError("Error fatal: %s" % ' '.join(sentence[1:]))
            elif kind == '!done':
                if trap:
                    raise RouterOSError(trap)
                return replies, attrs
            # !empty (RouterOS >= 7.18) no requiere tratamiento

    # ------------------------------------------------------------------
    # Comandos
    # ------------------------------------------------------------------
    def print(self, path, proplist=None):
        words = [path.rstrip('/') + '/print']
        if proplist:
            words.append('=.proplist=' + ','.join(proplist))
        return self._talk(words)[0]

    def add(self, path, **attrs):
        words = [path.rstrip('/') + '/add'] + ['=%s=%s' % (k.replace('_', '-'), v) for k, v in attrs.items()]
        return self._talk(words)[1].get('ret')

    def remove(self, path, item_id):
        self._talk([path.rstrip('/') + '/remove', '=.id=' + item_id])

    def call(self, command, **attrs):
        """Ejecuta un comando arbitrario (p. ej. /ping, /interface/monitor-traffic) y devuelve los !re."""
        words = [command] + ['=%s=%s' % (k.replace('_', '-'), v) for k, v in attrs.items()]
        return self._talk(words)[0]
