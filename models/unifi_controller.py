from odoo import api, fields, models
from odoo.exceptions import UserError

from ..lib import UnifiClient, UnifiError


def notify(title, message, kind='success', sticky=False):
    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {'title': title, 'message': message, 'type': kind, 'sticky': sticky},
    }


class WifiUnifiController(models.Model):
    _name = 'wifi.unifi.controller'
    _description = 'Controlador UniFi'

    name = fields.Char('Nombre', required=True)
    active = fields.Boolean(default=True)
    url = fields.Char('URL', required=True, help='Ej.: https://controlador.ejemplo.com:8443')
    unifi_os = fields.Boolean(
        'Consola UniFi OS',
        help='Marcar para UDM / Cloud Key Gen2+ / UniFi OS Server (usa /api/auth/login y /proxy/network). '
             'Desmarcar para el controlador autoinstalado clásico.')
    site = fields.Char('Sitio', required=True, default='default',
                       help='Identificador interno del sitio (no la descripción). Normalmente "default".')
    username = fields.Char('Usuario', required=True, groups='wifi_access_provisioning.group_wifi_manager')
    password = fields.Char('Contraseña', required=True, groups='wifi_access_provisioning.group_wifi_manager')
    verify_ssl = fields.Boolean('Verificar certificado SSL', default=False)
    timeout = fields.Integer('Timeout (s)', default=20)
    server_version = fields.Char('Versión', readonly=True)
    wlan_ids = fields.One2many('wifi.unifi.wlan', 'controller_id', string='Redes WiFi')
    last_sync = fields.Datetime('Última sincronización', readonly=True)

    def _client(self):
        self.ensure_one()
        rec = self.sudo()
        return UnifiClient(rec.url, rec.username, rec.password, site=rec.site,
                           unifi_os=rec.unifi_os, verify_ssl=rec.verify_ssl, timeout=rec.timeout)

    def action_test_connection(self):
        self.ensure_one()
        try:
            with self._client() as client:
                version = client.server_version()
                wlans = client.list_wlans()
        except UnifiError as e:
            raise UserError(str(e)) from e
        self.server_version = version
        return notify('Conexión correcta',
                      'UniFi Network %s, sitio "%s": %d redes WiFi.' % (version or '?', self.site, len(wlans)))

    def action_sync_wlans(self):
        for rec in self:
            try:
                with rec._client() as client:
                    wlans = client.list_wlans()
            except UnifiError as e:
                raise UserError(str(e)) from e
            existing = {w.unifi_id: w for w in rec.with_context(active_test=False).wlan_ids}
            seen = set()
            for data in wlans:
                vals = self.env['wifi.unifi.wlan']._vals_from_api(data)
                seen.add(data['_id'])
                if data['_id'] in existing:
                    existing[data['_id']].write(dict(vals, active=True))
                else:
                    self.env['wifi.unifi.wlan'].create(dict(vals, controller_id=rec.id, unifi_id=data['_id']))
            for unifi_id, wlan in existing.items():
                if unifi_id not in seen:
                    wlan.active = False
            rec.last_sync = fields.Datetime.now()
        return notify('Redes WiFi sincronizadas', '%d redes leídas del controlador.' % len(wlans))


class WifiUnifiWlan(models.Model):
    _name = 'wifi.unifi.wlan'
    _description = 'Red WiFi (UniFi)'
    _order = 'controller_id, name'

    name = fields.Char('SSID', required=True)
    active = fields.Boolean(default=True)
    controller_id = fields.Many2one('wifi.unifi.controller', 'Controlador', required=True, ondelete='cascade')
    unifi_id = fields.Char('ID UniFi', required=True, readonly=True)
    enabled = fields.Boolean('Habilitada', readonly=True)
    security = fields.Char('Seguridad', readonly=True)
    mac_filter_enabled = fields.Boolean('Filtro MAC activo', readonly=True)
    mac_filter_policy = fields.Selection(
        [('allow', 'Lista blanca (permitir)'), ('deny', 'Lista negra (bloquear)')],
        string='Política del filtro', readonly=True)
    mac_count = fields.Integer('MACs en la lista', readonly=True)

    _unifi_id_uniq = models.Constraint('UNIQUE(controller_id, unifi_id)',
                                       'La red WiFi ya existe para este controlador.')

    @api.model
    def _vals_from_api(self, data):
        return {
            'name': data.get('name'),
            'enabled': data.get('enabled', False),
            'security': data.get('security'),
            'mac_filter_enabled': data.get('mac_filter_enabled', False),
            'mac_filter_policy': data.get('mac_filter_policy') if data.get('mac_filter_policy') in ('allow', 'deny') else False,
            'mac_count': len(data.get('mac_filter_list') or []),
        }

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s (%s)' % (rec.name, rec.controller_id.name) if rec.controller_id else rec.name
