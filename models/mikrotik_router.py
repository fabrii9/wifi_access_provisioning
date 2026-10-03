from odoo import api, fields, models
from odoo.exceptions import UserError

from ..lib import RouterOSApi, RouterOSError
from .unifi_controller import notify


class WifiMikrotikRouter(models.Model):
    _name = 'wifi.mikrotik.router'
    _description = 'Router MikroTik'

    name = fields.Char('Nombre', required=True)
    active = fields.Boolean(default=True)
    host = fields.Char('Host / IP', required=True)
    use_ssl = fields.Boolean('Usar api-ssl', default=True,
                             help='api-ssl (8729) cifra la conexión. Requiere un certificado asignado al servicio en el router.')
    port = fields.Integer('Puerto', required=True, default=8729)
    verify_ssl = fields.Boolean('Verificar certificado SSL', default=False)
    username = fields.Char('Usuario', required=True, groups='wifi_access_provisioning.group_wifi_manager')
    password = fields.Char('Contraseña', required=True, groups='wifi_access_provisioning.group_wifi_manager')
    timeout = fields.Integer('Timeout (s)', default=15)
    ros_version = fields.Char('Versión RouterOS', readonly=True)
    board_name = fields.Char('Modelo', readonly=True)
    identity = fields.Char('Identidad', readonly=True)

    def _api(self):
        self.ensure_one()
        rec = self.sudo()
        return RouterOSApi(rec.host, rec.username, rec.password, port=rec.port, use_ssl=rec.use_ssl,
                           verify_ssl=rec.verify_ssl, timeout=rec.timeout)

    @api.onchange('use_ssl')
    def _onchange_use_ssl(self):
        if self.port in (8728, 8729):
            self.port = 8729 if self.use_ssl else 8728

    def action_test_connection(self):
        self.ensure_one()
        try:
            with self._api() as ros:
                res = ros.print('/system/resource', ['version', 'board-name'])[0]
                ident = ros.print('/system/identity', ['name'])[0]
                servers = ros.print('/ip/dhcp-server', ['name'])
        except RouterOSError as e:
            raise UserError(str(e)) from e
        self.write({'ros_version': res.get('version'), 'board_name': res.get('board-name'),
                    'identity': ident.get('name')})
        return notify('Conexión correcta', '%s · RouterOS %s · %d servidores DHCP.' % (
            ident.get('name'), res.get('version'), len(servers)))
