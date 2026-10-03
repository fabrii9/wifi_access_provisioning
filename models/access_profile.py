import ipaddress

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..lib import RouterOSError, UnifiError, normalize_mac
from .unifi_controller import notify

LEASE_PATH = '/ip/dhcp-server/lease'
LEASE_PROPS = ['.id', 'address', 'mac-address', 'dynamic', 'server', 'comment', 'disabled']


class WifiAccessProfile(models.Model):
    _name = 'wifi.access.profile'
    _description = 'Perfil de acceso WiFi'

    name = fields.Char('Nombre', required=True)
    active = fields.Boolean(default=True)

    # UniFi
    controller_id = fields.Many2one('wifi.unifi.controller', 'Controlador UniFi', required=True)
    wlan_id = fields.Many2one('wifi.unifi.wlan', 'Red WiFi (lista blanca)', required=True,
                              domain="[('controller_id', '=', controller_id)]")
    wlan_policy = fields.Selection(related='wlan_id.mac_filter_policy', string='Política')
    wlan_filter_enabled = fields.Boolean(related='wlan_id.mac_filter_enabled', string='Filtro activo')

    # MikroTik
    router_id = fields.Many2one('wifi.mikrotik.router', 'Router MikroTik', required=True)
    network = fields.Char('Red de IPs fijas', required=True, default='172.17.0.0/16',
                          help='Red en CIDR donde se crean las reservas DHCP.')
    range_start = fields.Char('Primera IP asignable', help='Opcional. Por defecto, la primera IP de la red.')
    range_end = fields.Char('Última IP asignable', help='Opcional. Por defecto, la última IP de la red.')
    excluded_ips = fields.Char('IPs excluidas', default='172.17.0.1',
                               help='Separadas por coma. Ej.: el gateway u otras IPs reservadas.')
    ip_strategy = fields.Selection([
        ('last_created', 'Siguiente a la última reserva creada'),
        ('highest', 'Siguiente a la IP más alta en uso'),
        ('first_free', 'Primer hueco libre'),
    ], string='Asignación de IP', required=True, default='last_created',
        help='Si la IP siguiente ya está usada se avanza a la próxima libre. '
             'Si se llega al final del rango se usa el primer hueco libre.')
    dhcp_server = fields.Char('Servidor DHCP',
                              help='Nombre del servidor DHCP de la reserva. Vacío = todos los servidores '
                                   '(igual que las reservas existentes).')
    comment_template = fields.Char('Comentario de la reserva', default='{name}',
                                   help='Variables: {name}, {mac}, {partner}, {ref}.')

    # Colas
    create_queue = fields.Boolean('Crear cola simple al dar de alta',
                                  help='Desmarcado si el router ya crea las colas con un script programado.')
    queue_max_limit = fields.Char('Límite de la cola (subida/bajada)', default='10M/40M')
    remove_queue = fields.Boolean('Eliminar la cola al dar de baja', default=True)

    auto_retry = fields.Boolean('Reintentar errores automáticamente', default=True,
                                help='Una tarea programada reintenta cada 30 minutos los dispositivos con error.')

    device_ids = fields.One2many('wifi.access.device', 'profile_id', string='Dispositivos')
    device_count = fields.Integer(compute='_compute_device_count')

    @api.depends('device_ids.state')
    def _compute_device_count(self):
        data = self.env['wifi.access.device']._read_group(
            [('profile_id', 'in', self.ids), ('state', '!=', 'removed')], ['profile_id'], ['__count'])
        counts = {profile.id: count for profile, count in data}
        for rec in self:
            rec.device_count = counts.get(rec.id, 0)

    @api.constrains('network', 'range_start', 'range_end', 'excluded_ips')
    def _check_network(self):
        for rec in self:
            try:
                net = ipaddress.ip_network(rec.network.strip(), strict=False)
                start, end, _excl = rec._range()
            except ValueError as e:
                raise ValidationError('Red o IP inválida: %s' % e) from e
            if start not in net or end not in net or start > end:
                raise ValidationError('El rango asignable debe estar dentro de %s y ser creciente.' % net)

    @api.onchange('controller_id')
    def _onchange_controller_id(self):
        if self.wlan_id.controller_id != self.controller_id:
            self.wlan_id = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _net(self):
        return ipaddress.ip_network(self.network.strip(), strict=False)

    def _range(self):
        net = self._net()
        start = ipaddress.ip_address(self.range_start.strip()) if self.range_start else net.network_address + 1
        end = ipaddress.ip_address(self.range_end.strip()) if self.range_end else net.broadcast_address - 1
        excluded = {ipaddress.ip_address(x.strip()) for x in (self.excluded_ips or '').split(',') if x.strip()}
        return start, end, excluded

    def _lock(self):
        """Serializa altas y bajas del mismo perfil (lista de MACs y asignación de IP)."""
        self.env.cr.execute('SELECT id FROM wifi_access_profile WHERE id IN %s FOR UPDATE', (tuple(self.ids),))

    @staticmethod
    def _read_leases(ros):
        return ros.print(LEASE_PATH, LEASE_PROPS)

    @staticmethod
    def _lease_is_static(lease):
        return lease.get('dynamic') != 'true'

    def _allocate_ip(self, leases):
        self.ensure_one()
        net = self._net()
        start, end, excluded = self._range()

        used, static_in_range = set(), []
        for lease in leases:
            try:
                ip = ipaddress.ip_address(lease.get('address', ''))
            except ValueError:
                continue
            used.add(ip)
            if ip in net and start <= ip <= end and self._lease_is_static(lease):
                static_in_range.append((int(lease['.id'].lstrip('*'), 16), ip))
        others = self.env['wifi.access.device'].search(
            [('profile_id', '=', self.id), ('state', '!=', 'removed'), ('ip_address', '!=', False)])
        used |= {ipaddress.ip_address(d.ip_address) for d in others}
        blocked = used | excluded

        def first_free(frm):
            ip = frm
            while ip <= end:
                if ip not in blocked:
                    return ip
                ip += 1
            return None

        candidate = None
        if self.ip_strategy == 'last_created' and static_in_range:
            candidate = first_free(max(static_in_range)[1] + 1)
        elif self.ip_strategy == 'highest' and static_in_range:
            candidate = first_free(max(ip for _id, ip in static_in_range) + 1)
        if candidate is None:
            candidate = first_free(start)
        if candidate is None:
            raise UserError('No quedan IPs libres en el rango %s - %s.' % (start, end))
        return str(candidate)

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def action_preview_next_ip(self):
        self.ensure_one()
        try:
            with self.router_id._api() as ros:
                ip = self._allocate_ip(self._read_leases(ros))
        except RouterOSError as e:
            raise UserError(str(e)) from e
        return notify('Próxima IP', 'El próximo dispositivo recibiría %s.' % ip, 'info')

    def action_view_devices(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('wifi_access_provisioning.action_wifi_access_device')
        action['domain'] = [('profile_id', '=', self.id)]
        action['context'] = {'default_profile_id': self.id}
        return action

    def action_import_existing(self):
        """Crea en Odoo los dispositivos que ya están en la lista blanca, con su reserva si existe."""
        self.ensure_one()
        try:
            with self.controller_id._client() as client:
                wlan = client.get_wlan(self.wlan_id.unifi_id)
                known = {c.get('mac'): c for c in client.list_known_clients()}
            with self.router_id._api() as ros:
                leases = self._read_leases(ros)
        except (UnifiError, RouterOSError) as e:
            raise UserError(str(e)) from e

        net = self._net()
        static = {}
        for lease in leases:
            mac = normalize_mac(lease.get('mac-address'))
            if mac and self._lease_is_static(lease):
                try:
                    in_net = ipaddress.ip_address(lease.get('address', '')) in net
                except ValueError:
                    in_net = False
                if in_net:
                    static[mac] = lease

        Device = self.env['wifi.access.device'].with_context(tracking_disable=True)
        existing = set(Device.search([('profile_id', '=', self.id), ('state', '!=', 'removed')]).mapped('mac'))
        vals_list, skipped = [], 0
        for raw in dict.fromkeys(wlan.get('mac_filter_list') or []):
            mac = normalize_mac(raw)
            if not mac or mac in existing:
                skipped += 1
                continue
            existing.add(mac)
            lease = static.get(mac, {})
            client_info = known.get(mac, {})
            name = lease.get('comment') or client_info.get('name') or client_info.get('hostname') or mac
            vals_list.append({
                'name': name,
                'mac': mac,
                'profile_id': self.id,
                'state': 'active',
                'unifi_ok': True,
                'mikrotik_ok': bool(lease),
                'ip_address': lease.get('address') or False,
                'lease_ref': lease.get('.id') or False,
                'imported': True,
            })
        Device.create(vals_list)
        self.wlan_id.mac_count = len(wlan.get('mac_filter_list') or [])
        without_lease = sum(1 for v in vals_list if not v['mikrotik_ok'])
        return notify('Importación terminada',
                      '%d dispositivos importados (%d sin reserva en %s). %d omitidos por duplicados o ya existentes.'
                      % (len(vals_list), without_lease, net, skipped), sticky=True)
