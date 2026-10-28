import ipaddress
import logging

from markupsafe import Markup

from odoo import Command, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..lib import RouterOSError, UnifiError, is_random_mac, normalize_mac
from .access_profile import LEASE_PATH
from .unifi_controller import notify

_logger = logging.getLogger(__name__)

QUEUE_PATH = '/queue/simple'
INCOMPLETE_SUMMARY = 'Dispositivo incompleto en los equipos'
EXPECTED_ERRORS = (UnifiError, RouterOSError, UserError, OSError, ValueError)


class _SafeDict(dict):
    def __missing__(self, key):
        return ''


class WifiAccessDevice(models.Model):
    _name = 'wifi.access.device'
    _description = 'Dispositivo con acceso WiFi'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char('Descripción', required=True, tracking=True,
                       help='Ej.: "Notebook Prof. García". Se usa como comentario de la reserva DHCP.')
    mac = fields.Char('MAC', required=True, tracking=True, index=True)
    profile_id = fields.Many2one('wifi.access.profile', 'Perfil', required=True, tracking=True,
                                 default=lambda self: self.env['wifi.access.profile'].search([], limit=1))
    partner_id = fields.Many2one('res.partner', 'Responsable', tracking=True)
    ref = fields.Char('Referencia', help='Legajo, número de inventario, etc.')
    notes = fields.Text('Notas')
    ip_address = fields.Char('IP fija', tracking=True, copy=False,
                             help='Dejar vacío para asignar automáticamente según el perfil.')
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('active', 'Activo'),
        ('error', 'Error'),
        ('removed', 'Dado de baja'),
    ], string='Estado', default='draft', required=True, tracking=True, copy=False, index=True)
    pending_action = fields.Selection([('provision', 'Alta'), ('deprovision', 'Baja')],
                                      string='Acción pendiente', copy=False, readonly=True)
    unifi_ok = fields.Boolean('En lista blanca UniFi', copy=False, readonly=True)
    mikrotik_ok = fields.Boolean('Reserva DHCP creada', copy=False, readonly=True)
    lease_ref = fields.Char('ID de la reserva', copy=False, readonly=True)
    queue_ref = fields.Char('ID de la cola', copy=False, readonly=True)
    last_error = fields.Text('Último error', copy=False, readonly=True)
    provisioned_date = fields.Datetime('Fecha de alta', copy=False, readonly=True)
    removed_date = fields.Datetime('Fecha de baja', copy=False, readonly=True)
    last_check = fields.Datetime('Última verificación', copy=False, readonly=True)
    imported = fields.Boolean('Importado', copy=False, readonly=True,
                              help='Creado a partir de la configuración existente en los equipos.')
    is_random_mac = fields.Boolean('MAC aleatoria', compute='_compute_is_random_mac', store=True,
                                   help='MAC localmente administrada: el equipo probablemente usa "dirección privada". '
                                        'Puede cambiar y dejar de conectarse.')

    @api.depends('mac')
    def _compute_is_random_mac(self):
        for rec in self:
            rec.is_random_mac = is_random_mac(rec.mac)

    # ------------------------------------------------------------------
    # Validaciones
    # ------------------------------------------------------------------
    @api.model
    def _clean_vals(self, vals):
        if vals.get('mac'):
            mac = normalize_mac(vals['mac'])
            if not mac:
                raise ValidationError('MAC inválida: %s' % vals['mac'])
            vals['mac'] = mac
        if vals.get('ip_address'):
            vals['ip_address'] = vals['ip_address'].strip()
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._clean_vals(dict(v)) for v in vals_list])

    def write(self, vals):
        if {'mac', 'profile_id'} & set(vals) and self.filtered(lambda r: r.state not in ('draft', 'removed')):
            raise UserError('Para cambiar la MAC o el perfil primero dé de baja el dispositivo.')
        return super().write(self._clean_vals(dict(vals)))

    def unlink(self):
        if self.filtered(lambda r: r.state in ('active', 'error')):
            raise UserError('Solo se pueden eliminar dispositivos en borrador o dados de baja.')
        return super().unlink()

    @api.constrains('mac', 'profile_id', 'state')
    def _check_unique_mac(self):
        for rec in self.filtered(lambda r: r.state != 'removed'):
            if self.search_count([('id', '!=', rec.id), ('profile_id', '=', rec.profile_id.id),
                                  ('mac', '=', rec.mac), ('state', '!=', 'removed')]):
                raise ValidationError('La MAC %s ya está cargada en el perfil %s.' % (rec.mac, rec.profile_id.name))

    @api.constrains('ip_address', 'profile_id', 'state')
    def _check_ip(self):
        for rec in self.filtered(lambda r: r.ip_address and r.state != 'removed'):
            try:
                ip = ipaddress.ip_address(rec.ip_address)
            except ValueError as e:
                raise ValidationError('IP inválida: %s' % rec.ip_address) from e
            if ip not in rec.profile_id._net():
                raise ValidationError('La IP %s no pertenece a la red %s.' % (ip, rec.profile_id.network))
            if self.search_count([('id', '!=', rec.id), ('profile_id', '=', rec.profile_id.id),
                                  ('ip_address', '=', rec.ip_address), ('state', '!=', 'removed')]):
                raise ValidationError('La IP %s ya está asignada a otro dispositivo.' % ip)

    # ------------------------------------------------------------------
    # Motor de alta / baja
    # ------------------------------------------------------------------
    def _lease_comment(self):
        template = self.profile_id.comment_template or '{name}'
        return template.format_map(_SafeDict(
            name=self.name or '', mac=self.mac, partner=self.partner_id.name or '', ref=self.ref or '')).strip(' |-·')

    def _run(self, action):
        """Ejecuta alta o baja. Devuelve True si terminó bien; si falla deja el registro en error."""
        self.ensure_one()
        self.profile_id._lock()
        log = []
        try:
            if action == 'provision':
                self._do_provision(log)
            else:
                self._do_deprovision(log)
        except EXPECTED_ERRORS as e:
            _logger.warning('Acceso WiFi: %s de %s falló: %s', action, self.mac, e)
            if isinstance(e, ValidationError):
                self.env.invalidate_all(flush=False)
            self.write({'state': 'error', 'pending_action': action, 'last_error': str(e)})
            self.message_post(body=Markup('<b>%s falló</b><br/>%s%s') % (
                'Alta' if action == 'provision' else 'Baja', str(e),
                Markup('<br/>') + Markup('<br/>').join(log) if log else ''))
            return False
        if log:
            self.message_post(body=Markup('<br/>').join(log))
        return True

    def _do_provision(self, log):
        profile = self.profile_id
        if not self.unifi_ok:
            with profile.controller_id._client() as client:
                wlan = client.get_wlan(profile.wlan_id.unifi_id)
                macs = wlan.get('mac_filter_list') or []
                if self.mac in {normalize_mac(m) for m in macs}:
                    log.append('UniFi: la MAC ya estaba en la lista de %s.' % wlan.get('name'))
                else:
                    macs = macs + [self.mac]
                    client.set_mac_filter_list(wlan, macs)
                    log.append('UniFi: MAC agregada a la lista de %s.' % wlan.get('name'))
                profile.wlan_id.mac_count = len(macs)
            self.unifi_ok = True
            self.env.flush_all()

        if not self.mikrotik_ok:
            with profile.router_id._api() as ros:
                leases = profile._read_leases(ros)
                mine = [l for l in leases if normalize_mac(l.get('mac-address')) == self.mac]
                static = [l for l in mine if profile._lease_is_static(l)]
                net = profile._net()
                if static:
                    lease = static[0]
                    if ipaddress.ip_address(lease['address']) not in net:
                        raise UserError('La MAC ya tiene una reserva fija con %s, fuera de %s. '
                                        'Elimínela o modifíquela en el router.' % (lease['address'], net))
                    if self.ip_address and self.ip_address != lease['address']:
                        raise UserError('La MAC ya tiene la reserva %s, distinta de la IP indicada %s.'
                                        % (lease['address'], self.ip_address))
                    self._check_ip_free_in_odoo(lease['address'])
                    ip, lease_ref = lease['address'], lease['.id']
                    log.append('MikroTik: se reutilizó la reserva existente %s.' % ip)
                else:
                    if self.ip_address:
                        ip = self.ip_address
                        if any(l.get('address') == ip for l in leases):
                            raise UserError('La IP %s ya está en uso en el router.' % ip)
                    else:
                        ip = profile._allocate_ip(leases)
                    for lease in mine:  # préstamos dinámicos de la misma MAC
                        ros.remove(LEASE_PATH, lease['.id'])
                    attrs = {'address': ip, 'mac-address': self.mac.upper(), 'comment': self._lease_comment()}
                    if profile.dhcp_server:
                        attrs['server'] = profile.dhcp_server
                    lease_ref = ros.add(LEASE_PATH, **attrs)
                    log.append('MikroTik: reserva creada %s → %s.' % (self.mac.upper(), ip))
                self._check_ip_free_in_odoo(ip)
                self.write({'ip_address': ip, 'lease_ref': lease_ref})
                self.env.flush_all()

                if profile.create_queue:
                    queue = self._find_queues(ros, ip)
                    if queue:
                        self.queue_ref = queue[0]['.id']
                    else:
                        self.queue_ref = ros.add(QUEUE_PATH, name=ip, target=ip + '/32',
                                                 max_limit=profile.queue_max_limit or '10M/40M')
                        log.append('MikroTik: cola creada %s (%s).' % (ip, profile.queue_max_limit))
            self.mikrotik_ok = True

        self.write({'state': 'active', 'pending_action': False, 'last_error': False,
                    'provisioned_date': fields.Datetime.now(), 'removed_date': False})

    def _do_deprovision(self, log):
        profile = self.profile_id
        with profile.controller_id._client() as client:
            wlan = client.get_wlan(profile.wlan_id.unifi_id)
            macs = wlan.get('mac_filter_list') or []
            kept = [m for m in macs if normalize_mac(m) != self.mac]
            if len(kept) != len(macs):
                client.set_mac_filter_list(wlan, kept)
                log.append('UniFi: MAC quitada de la lista de %s.' % wlan.get('name'))
            profile.wlan_id.mac_count = len(kept)
        self.unifi_ok = False
        self.env.flush_all()

        with profile.router_id._api() as ros:
            net = profile._net()
            for lease in profile._read_leases(ros):
                ours = lease.get('.id') == self.lease_ref or (
                    normalize_mac(lease.get('mac-address')) == self.mac and profile._lease_is_static(lease)
                    and ipaddress.ip_address(lease['address']) in net)
                if ours:
                    ros.remove(LEASE_PATH, lease['.id'])
                    log.append('MikroTik: reserva %s eliminada.' % lease.get('address'))
            if profile.remove_queue and self.ip_address:
                for queue in self._find_queues(ros, self.ip_address):
                    ros.remove(QUEUE_PATH, queue['.id'])
                    log.append('MikroTik: cola %s eliminada.' % queue.get('name'))
        self.write({'mikrotik_ok': False, 'lease_ref': False, 'queue_ref': False, 'state': 'removed',
                    'pending_action': False, 'last_error': False, 'removed_date': fields.Datetime.now()})

    def _check_ip_free_in_odoo(self, ip):
        other = self.search([('id', '!=', self.id), ('profile_id', '=', self.profile_id.id),
                             ('ip_address', '=', ip), ('state', '!=', 'removed')], limit=1)
        if other:
            raise UserError('La IP %s está asignada en Odoo a "%s" (%s).' % (ip, other.name, other.mac))

    @staticmethod
    def _find_queues(ros, ip):
        targets = {ip, ip + '/32'}
        return [q for q in ros.print(QUEUE_PATH, ['.id', 'name', 'target', 'dynamic'])
                if q.get('dynamic') != 'true' and targets & set((q.get('target') or '').split(','))]

    def _notify_result(self, ok, title):
        failed = len(self) - ok
        if len(self) == 1:
            message = 'Operación completada.' if ok else self.last_error
        else:
            message = '%d correctos, %d con error.' % (ok, failed)
        result = notify(title, message, 'success' if not failed else 'danger', sticky=bool(failed))
        result['params']['next'] = {'type': 'ir.actions.client', 'tag': 'soft_reload'}
        return result

    # ------------------------------------------------------------------
    # Botones
    # ------------------------------------------------------------------
    def action_provision(self):
        todo = self.filtered(lambda r: r.state == 'draft' or (r.state == 'error' and r.pending_action == 'provision'))
        if not todo:
            raise UserError('No hay dispositivos en borrador o con alta pendiente.')
        return todo._notify_result(sum(rec._run('provision') for rec in todo), 'Alta de dispositivos')

    def action_deprovision(self):
        todo = self.filtered(lambda r: r.state in ('active', 'error'))
        if not todo:
            raise UserError('No hay dispositivos activos para dar de baja.')
        return todo._notify_result(sum(rec._run('deprovision') for rec in todo), 'Baja de dispositivos')

    def action_retry(self):
        todo = self.filtered(lambda r: r.state == 'error' and r.pending_action)
        return todo._notify_result(sum(rec._run(rec.pending_action) for rec in todo), 'Reintento')

    def action_reset_draft(self):
        self.filtered(lambda r: r.state == 'removed').write(
            {'state': 'draft', 'ip_address': False, 'unifi_ok': False, 'mikrotik_ok': False})

    def action_new_monitor(self):
        """Crea un seguimiento en borrador con los dispositivos seleccionados y lo abre."""
        profile = self[:1].profile_id
        monitor = self.env['wifi.monitor'].create({
            'name': ', '.join(self.mapped('name'))[:120],
            'controller_id': profile.controller_id.id,
            'router_id': profile.router_id.id,
            'target_ids': [Command.create({'device_id': d.id, 'name': d.name, 'mac': d.mac,
                                           'last_ip': d.ip_address}) for d in self],
        })
        return {'type': 'ir.actions.act_window', 'res_model': 'wifi.monitor', 'res_id': monitor.id,
                'view_mode': 'form', 'target': 'current'}

    def action_check(self):
        """Lee los equipos y actualiza los indicadores sin modificar nada."""
        for profile, devices in self.grouped('profile_id').items():
            try:
                with profile.controller_id._client() as client:
                    wlan_macs = {normalize_mac(m) for m in client.get_wlan(profile.wlan_id.unifi_id).get('mac_filter_list') or []}
                with profile.router_id._api() as ros:
                    leases = profile._read_leases(ros)
            except EXPECTED_ERRORS as e:
                raise UserError(str(e)) from e
            net = profile._net()
            static = {}
            for lease in leases:
                mac = normalize_mac(lease.get('mac-address'))
                if mac and profile._lease_is_static(lease) and ipaddress.ip_address(lease['address']) in net:
                    static[mac] = lease
            for rec in devices:
                lease = static.get(rec.mac)
                vals = {'unifi_ok': rec.mac in wlan_macs, 'mikrotik_ok': bool(lease), 'last_check': fields.Datetime.now()}
                if lease:
                    vals.update(ip_address=lease['address'], lease_ref=lease['.id'])
                changes = [label for field, label in (('unifi_ok', 'lista blanca'), ('mikrotik_ok', 'reserva DHCP'),
                                                      ('ip_address', 'IP'))
                           if field in vals and vals[field] != rec[field]]
                rec.write(vals)
                if changes:
                    rec.message_post(body='Verificación: cambió %s respecto de lo registrado.' % ', '.join(changes))
                pending = rec.activity_ids.filtered(lambda a: a.summary == INCOMPLETE_SUMMARY)
                if rec.state == 'active' and not (vals['unifi_ok'] and vals['mikrotik_ok']) and not pending:
                    rec.activity_schedule('mail.mail_activity_data_todo', summary=INCOMPLETE_SUMMARY,
                                          note='Falta %s.' % ' y '.join(
                                              x for x, ok in (('la MAC en UniFi', vals['unifi_ok']),
                                                              ('la reserva en MikroTik', vals['mikrotik_ok'])) if not ok))
        return notify('Verificación terminada', '%d dispositivos verificados.' % len(self), 'info')

    @api.model
    def _cron_retry_failed(self):
        devices = self.search([('state', '=', 'error'), ('pending_action', '!=', False),
                               ('profile_id.auto_retry', '=', True)], limit=50)
        for rec in devices:
            rec._run(rec.pending_action)
