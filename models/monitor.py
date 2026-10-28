import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.addons.base.models.res_partner import _tz_get

from ..lib import RouterOSError, UnifiError, normalize_mac
from ..lib import monitoring as mon_lib
from .unifi_controller import notify

_logger = logging.getLogger(__name__)

WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
OFFLINE_COLOR = '#dc2626'
SATURATED_COLOR = '#f59e0b'


class WifiMonitor(models.Model):
    _name = 'wifi.monitor'
    _description = 'Seguimiento de dispositivos'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char('Motivo', required=True, tracking=True,
                       help='Ej.: "Cortes entre las 11 y las 13 en las PCs de secretaría".')
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('running', 'En curso'),
        ('done', 'Finalizado'),
    ], string='Estado', default='draft', required=True, tracking=True, copy=False, index=True)
    partner_id = fields.Many2one('res.partner', 'Reclamó', tracking=True)
    recipient_ids = fields.Many2many('res.partner', string='Enviar informe a',
                                     help='Reciben el PDF por correo. Siempre queda adjunto en el seguimiento.')
    notes = fields.Text('Notas')
    target_ids = fields.One2many('wifi.monitor.target', 'monitor_id', string='Equipos', copy=True)

    # Fuentes
    controller_id = fields.Many2one('wifi.unifi.controller', 'Controlador UniFi', required=True,
                                    default=lambda self: self.env['wifi.unifi.controller'].search([], limit=1))
    router_id = fields.Many2one('wifi.mikrotik.router', 'Router MikroTik', required=True,
                                default=lambda self: self.env['wifi.mikrotik.router'].search([], limit=1))
    wan_interface = fields.Char('Interfaz WAN', required=True, default='ether8-Transdatos',
                                help='Nombre exacto de la interfaz en el router, para medir la ocupación del enlace.')
    wan_down_mbps = fields.Integer('Bajada contratada (Mbps)', default=140)
    wan_up_mbps = fields.Integer('Subida contratada (Mbps)', default=40)
    inet_ping_host = fields.Char('Medir latencia a internet contra', default='8.8.8.8',
                                 help='IP que el router pinguea en cada muestra. Vacío para no medir.')

    # Programación
    tz = fields.Selection(_tz_get, string='Zona horaria', required=True,
                          default=lambda self: self.env.user.tz or 'America/Argentina/Buenos_Aires')
    date_start = fields.Date('Desde el día', required=True, default=fields.Date.context_today)
    date_end = fields.Date('Hasta el día', required=True,
                           default=lambda self: fields.Date.context_today(self) + timedelta(days=6))
    hour_from = fields.Float('Desde la hora', required=True, default=10.5)
    hour_to = fields.Float('Hasta la hora', required=True, default=13.5)
    mon = fields.Boolean('Lun', default=True)
    tue = fields.Boolean('Mar', default=True)
    wed = fields.Boolean('Mié', default=True)
    thu = fields.Boolean('Jue', default=True)
    fri = fields.Boolean('Vie', default=True)
    sat = fields.Boolean('Sáb')
    sun = fields.Boolean('Dom')
    interval = fields.Integer('Muestra cada (minutos)', required=True, default=2)

    report_mode = fields.Selection([
        ('daily', 'Cada día, al cerrar la ventana'),
        ('end', 'Al finalizar el seguimiento'),
        ('manual', 'Solo a mano'),
    ], string='Informe', required=True, default='daily')

    last_sample_at = fields.Datetime('Última muestra', readonly=True, copy=False)
    last_report_date = fields.Date('Último informe diario', readonly=True, copy=False)
    last_error = fields.Text('Último error de lectura', readonly=True, copy=False)
    sample_count = fields.Integer(compute='_compute_sample_count')

    def _compute_sample_count(self):
        data = self.env['wifi.monitor.sample']._read_group(
            [('monitor_id', 'in', self.ids)], ['monitor_id'], ['__count'])
        counts = {monitor.id: count for monitor, count in data}
        for rec in self:
            rec.sample_count = counts.get(rec.id, 0)

    @api.constrains('date_start', 'date_end', 'hour_from', 'hour_to', 'interval', *WEEKDAYS)
    def _check_schedule(self):
        for rec in self:
            if rec.date_end < rec.date_start:
                raise ValidationError('La fecha de fin no puede ser anterior a la de inicio.')
            if not 0 <= rec.hour_from < rec.hour_to <= 24:
                raise ValidationError('El horario debe ser creciente y estar entre 00:00 y 24:00.')
            if rec.interval < 1:
                raise ValidationError('El intervalo mínimo es de 1 minuto.')
            if not any(rec[d] for d in WEEKDAYS):
                raise ValidationError('Marque al menos un día de la semana.')

    # ------------------------------------------------------------------
    # Ventana horaria (todo en hora local del seguimiento; Odoo guarda en UTC)
    # ------------------------------------------------------------------
    def _zone(self):
        return ZoneInfo(self.tz or 'UTC')

    def _local_now(self):
        return datetime.now(self._zone()).replace(tzinfo=None)

    def _to_local(self, utc_dt):
        return utc_dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(self._zone()).replace(tzinfo=None)

    def _to_utc(self, local_dt):
        return local_dt.replace(tzinfo=self._zone()).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)

    @staticmethod
    def _float_time(value):
        minutes = round(value * 60)
        return time(23, 59, 59) if minutes >= 24 * 60 else time(minutes // 60, minutes % 60)

    def _window(self, day):
        return (datetime.combine(day, self._float_time(self.hour_from)),
                datetime.combine(day, self._float_time(self.hour_to)))

    def _is_active_day(self, day):
        return self.date_start <= day <= self.date_end and self[WEEKDAYS[day.weekday()]]

    def _in_window(self, local_dt):
        start, end = self._window(local_dt.date())
        return self._is_active_day(local_dt.date()) and start <= local_dt <= end

    def _max_gap(self):
        return timedelta(minutes=self.interval * 2.5)

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def action_start(self):
        if self.filtered(lambda r: not r.target_ids):
            raise UserError('Agregue al menos un equipo para seguir.')
        self.write({'state': 'running', 'last_error': False})

    def action_stop(self):
        for rec in self.filtered(lambda r: r.state == 'running'):
            rec.state = 'done'
            if rec.report_mode == 'end' and rec.sample_count:
                rec._send_report()

    def action_reset_draft(self):
        self.write({'state': 'draft'})

    def action_collect_now(self):
        self.ensure_one()
        if not self.target_ids:
            raise UserError('Agregue al menos un equipo para seguir.')
        self._collect()
        if self.last_error:
            return notify('Muestra tomada con errores', self.last_error, 'warning', sticky=True)
        return notify('Muestra tomada', '%d de %d equipos conectados al WiFi.'
                      % (len(self.target_ids.filtered('online')), len(self.target_ids)))

    def action_view_samples(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('wifi_access_provisioning.action_wifi_monitor_sample')
        action['domain'] = [('monitor_id', '=', self.id)]
        action['context'] = {'search_default_group_target': 1}
        return action

    def action_print_report(self):
        if not self.sample_count:
            raise UserError('Todavía no hay muestras para informar.')
        return self.env.ref('wifi_access_provisioning.action_report_wifi_monitor').report_action(self)

    def action_send_report(self):
        self.ensure_one()
        if not self.sample_count:
            raise UserError('Todavía no hay muestras para informar.')
        self._send_report()
        sent = ' y se envió a %s' % ', '.join(self.recipient_ids.mapped('name')) if self.recipient_ids else ''
        return notify('Informe generado', 'Quedó adjunto en el seguimiento%s.' % sent)

    # ------------------------------------------------------------------
    # Lectura de UniFi y MikroTik
    # ------------------------------------------------------------------
    def _collect(self):
        """Toma una muestra de todos los equipos y del enlace, con una sola conexión a cada equipo."""
        self.ensure_one()
        now = fields.Datetime.now()
        errors, clients, ap_names = [], {}, {}
        unifi_failed = False
        try:
            with self.controller_id._client() as client:
                clients = {normalize_mac(c.get('mac')): c for c in client.list_active_clients()}
                try:
                    ap_names = {normalize_mac(d.get('mac')): d.get('name') for d in client.list_devices_basic()}
                except UnifiError:
                    pass  # controladores viejos: se muestra la MAC del AP
        except UnifiError as e:
            unifi_failed = True
            errors.append('UniFi: %s' % e)

        context_vals, pings = {}, {}
        try:
            with self.router_id._api() as ros:
                try:
                    traffic = ros.call('/interface/monitor-traffic', interface=self.wan_interface, once='')
                    if traffic:
                        context_vals['wan_down_mbps'] = (mon_lib.to_float(traffic[0].get('rx-bits-per-second')) or 0) / 1e6
                        context_vals['wan_up_mbps'] = (mon_lib.to_float(traffic[0].get('tx-bits-per-second')) or 0) / 1e6
                except RouterOSError as e:
                    errors.append('MikroTik, interfaz %s: %s' % (self.wan_interface, e))
                if self.inet_ping_host:
                    try:
                        inet_ms, inet_loss = self._ping(ros, self.inet_ping_host)
                        context_vals.update(inet_ping_done=True, inet_ping_ms=inet_ms, inet_ping_loss=inet_loss)
                    except RouterOSError as e:
                        errors.append('Ping a %s: %s' % (self.inet_ping_host, e))
                resource = ros.print('/system/resource', ['cpu-load'])
                if resource:
                    context_vals['cpu_load'] = mon_lib.to_float(resource[0].get('cpu-load'))
                for target in self.target_ids:
                    ip = (clients.get(target.mac) or {}).get('ip') or target.last_ip or target.device_id.ip_address
                    if ip:
                        try:
                            pings[target.id] = self._ping(ros, ip)
                        except RouterOSError as e:
                            errors.append('Ping a %s: %s' % (ip, e))
        except RouterOSError as e:
            errors.append('MikroTik: %s' % e)

        if context_vals:
            self.env['wifi.monitor.context'].sudo().create(dict(context_vals, monitor_id=self.id, timestamp=now))

        sample_vals = []
        for target in self.target_ids:
            sta = clients.get(target.mac)
            vals = {'monitor_id': self.id, 'target_id': target.id, 'timestamp': now,
                    'online': bool(sta), 'unifi_error': unifi_failed}
            if target.id in pings:
                ping_ms, loss = pings[target.id]
                vals.update(ping_done=True, ping_ms=ping_ms, ping_loss=loss)
            target_vals = {'last_sample_at': now}
            if sta:
                # Contadores desde el punto de vista del AP: tx = hacia el equipo (bajada), rx = desde el equipo (subida)
                down_total = mon_lib.to_float(sta.get('tx_bytes'))
                up_total = mon_lib.to_float(sta.get('rx_bytes'))
                if target.counter_at and down_total is not None and up_total is not None \
                        and down_total >= target.last_down_total and up_total >= target.last_up_total:
                    seconds = max((now - target.counter_at).total_seconds(), 1)
                    down_bytes = down_total - target.last_down_total
                    up_bytes = up_total - target.last_up_total
                    vals.update(has_rate=True, down_bytes=down_bytes, up_bytes=up_bytes,
                                down_mbps=down_bytes * 8 / seconds / 1e6, up_mbps=up_bytes * 8 / seconds / 1e6)
                ap_mac = normalize_mac(sta.get('ap_mac'))
                vals.update({
                    'ip': sta.get('ip'),
                    'ap_mac': ap_mac,
                    'ap_name': ap_names.get(ap_mac) or ap_mac,
                    'essid': sta.get('essid'),
                    'channel': sta.get('channel'),
                    'signal': sta.get('signal'),
                    'satisfaction': sta.get('satisfaction'),
                    'link_down_mbps': (mon_lib.to_float(sta.get('tx_rate')) or 0) / 1000,
                    'link_up_mbps': (mon_lib.to_float(sta.get('rx_rate')) or 0) / 1000,
                })
                target_vals.update({
                    'online': True, 'last_ip': sta.get('ip') or target.last_ip, 'last_ap': vals['ap_name'],
                    'last_signal': sta.get('signal'), 'last_seen_at': now,
                    'counter_at': now if down_total is not None else False,
                    'last_down_total': down_total or 0, 'last_up_total': up_total or 0,
                })
            elif not unifi_failed:
                # se desconectó: los contadores de UniFi vuelven a cero al reconectar
                target_vals.update({'online': False, 'counter_at': False})
            target.sudo().write(target_vals)
            sample_vals.append(vals)
        self.env['wifi.monitor.sample'].sudo().create(sample_vals)
        self.sudo().write({'last_sample_at': now, 'last_error': '\n'.join(errors) or False})

    @staticmethod
    def _ping(ros, ip):
        """3 pings desde el router. Devuelve (rtt promedio en ms o None, % de pérdida)."""
        replies = ros.call('/ping', address=ip, count=3, interval='200ms')
        last = next((r for r in reversed(replies) if 'sent' in r), None)
        if last:
            return mon_lib.parse_ros_duration(last.get('avg-rtt')), mon_lib.to_float(last.get('packet-loss')) or 0.0
        ok = [t for t in (mon_lib.parse_ros_duration(r.get('time')) for r in replies) if t is not None]
        return (sum(ok) / len(ok) if ok else None), 100.0 * (3 - len(ok)) / 3

    # ------------------------------------------------------------------
    # Tarea programada (cada minuto)
    # ------------------------------------------------------------------
    @api.model
    def _cron_run(self):
        for rec in self.search([('state', '=', 'running')]):
            try:
                rec._cron_step()
                self.env.cr.commit()  # cada seguimiento en su propia transacción
            except Exception:
                self.env.cr.rollback()
                _logger.exception('Seguimiento %s: falló la tarea programada', rec.id)

    def _cron_step(self):
        local_now = self._local_now()
        today = local_now.date()
        if self._in_window(local_now):
            if not self.last_sample_at or (
                    fields.Datetime.now() - self.last_sample_at >= timedelta(minutes=self.interval, seconds=-20)):
                self._collect()
            return
        after_window = local_now > self._window(today)[1]
        if self.report_mode == 'daily' and after_window and self._is_active_day(today) \
                and self.last_report_date != today and self._has_samples(today):
            self._send_report(day=today)
            self.last_report_date = today
        if today > self.date_end or (today == self.date_end and after_window):
            self.action_stop()

    def _has_samples(self, day):
        start, end = self._window(day)
        return bool(self.env['wifi.monitor.sample'].search_count([
            ('monitor_id', '=', self.id), ('timestamp', '>=', self._to_utc(start)),
            ('timestamp', '<=', self._to_utc(end))], limit=1))

    # ------------------------------------------------------------------
    # Informe
    # ------------------------------------------------------------------
    def _report_days(self, day=None):
        if day:
            return [day]
        samples = self.env['wifi.monitor.sample'].search_read([('monitor_id', '=', self.id)], ['timestamp'])
        return sorted({self._to_local(s['timestamp']).date() for s in samples})

    def _report_payload(self, day=None):
        """Estadísticas, gráficos y diagnóstico por día, listos para la plantilla."""
        self.ensure_one()
        max_gap = self._max_gap()
        step = timedelta(minutes=self.interval)
        days = []
        for d in self._report_days(day):
            start, end = self._window(d)
            utc_range = [('timestamp', '>=', self._to_utc(start)), ('timestamp', '<=', self._to_utc(end))]

            ctx = [{'ts': self._to_local(c.timestamp), 'wan_down': c.wan_down_mbps, 'wan_up': c.wan_up_mbps,
                    'cpu': c.cpu_load,
                    'inet_ms': c.inet_ping_ms if c.inet_ping_done and c.inet_ping_loss < 100 else None,
                    'inet_loss': c.inet_ping_loss if c.inet_ping_done else None}
                   for c in self.env['wifi.monitor.context'].search([('monitor_id', '=', self.id)] + utc_range,
                                                                    order='timestamp')]
            ctx_stats = mon_lib.context_stats(ctx, self.wan_down_mbps, self.wan_up_mbps)
            ctx_diag, saturated = mon_lib.diagnose_context(ctx_stats, max_gap, self.wan_down_mbps, self.wan_up_mbps)
            sat_bands = [(a, b + step, SATURATED_COLOR, 'Enlace saturado') for a, b in saturated]
            wan_chart = mon_lib.line_chart(
                [{'label': 'Bajada', 'points': [(c['ts'], c['wan_down']) for c in ctx]},
                 {'label': 'Subida', 'points': [(c['ts'], c['wan_up']) for c in ctx]}],
                start, end, 'Ocupación del enlace (%s)' % self.wan_interface, 'Mbps', y_min=0,
                ref_lines=[(v, '%s contratada' % lbl, '#9ca3af')
                           for v, lbl in ((self.wan_down_mbps, 'bajada'), (self.wan_up_mbps, 'subida')) if v],
                bands=sat_bands, max_gap=max_gap)
            inet_chart = mon_lib.line_chart(
                [{'label': 'Ping a %s' % self.inet_ping_host, 'points': [(c['ts'], c['inet_ms']) for c in ctx]}],
                start, end, 'Latencia a internet desde el router', 'ms', y_min=0,
                ref_lines=[(mon_lib.HIGH_INET_PING_MS, 'lenta', '#dc2626')], bands=sat_bands, max_gap=max_gap,
            ) if self.inet_ping_host else None

            targets = []
            for target in self.target_ids:
                recs = self.env['wifi.monitor.sample'].search(
                    [('target_id', '=', target.id), ('unifi_error', '=', False)] + utc_range, order='timestamp')
                samples = [{
                    'ts': self._to_local(s.timestamp),
                    'online': s.online,
                    'signal': s.signal if s.online and s.signal else None,
                    'ap': s.ap_name,
                    'ping_ms': s.ping_ms if s.ping_done and s.ping_loss < 100 else None,
                    'loss': s.ping_loss if s.ping_done else None,
                    'down_mbps': s.down_mbps if s.has_rate else None,
                    'up_mbps': s.up_mbps if s.has_rate else None,
                    'down_bytes': s.down_bytes,
                    'up_bytes': s.up_bytes,
                } for s in recs]
                stats = mon_lib.target_stats(samples)
                offline = [(a, b + step, OFFLINE_COLOR, 'Desconectado')
                           for a, b in mon_lib.ranges([(s['ts'], not s['online']) for s in samples], max_gap)]
                charts = [
                    mon_lib.line_chart([{'label': 'Señal', 'points': [(s['ts'], s['signal']) for s in samples]}],
                                       start, end, 'Señal WiFi', 'dBm', y_min=-90, y_max=-30,
                                       ref_lines=[(mon_lib.WEAK_SIGNAL_DBM, 'débil', '#dc2626')],
                                       bands=offline, max_gap=max_gap),
                    mon_lib.line_chart([{'label': 'Ping desde el router', 'points': [(s['ts'], s['ping_ms']) for s in samples]}],
                                       start, end, 'Latencia en la red local', 'ms', y_min=0,
                                       ref_lines=[(mon_lib.HIGH_PING_MS, 'alta', '#dc2626')],
                                       bands=offline, max_gap=max_gap),
                    mon_lib.line_chart([{'label': 'Bajada', 'points': [(s['ts'], s['down_mbps']) for s in samples]},
                                        {'label': 'Subida', 'points': [(s['ts'], s['up_mbps']) for s in samples]}],
                                       start, end, 'Consumo del equipo', 'Mbps',
                                       y_min=0, bands=offline + sat_bands, max_gap=max_gap),
                ]
                targets.append({
                    'target': target,
                    'stats': stats,
                    'diagnosis': mon_lib.diagnose_target(samples, stats, max_gap, saturated),
                    'charts': [Markup(c) for c in charts],
                })
            days.append({
                'day': d,
                'label': '%s · %s a %s' % (d.strftime('%d/%m/%Y'), start.strftime('%H:%M'), end.strftime('%H:%M')),
                'context_stats': ctx_stats,
                'context_diagnosis': ctx_diag,
                'wan_chart': Markup(wan_chart),
                'inet_chart': Markup(inet_chart) if inet_chart else None,
                'targets': targets,
            })
        return {'days': days}

    def _send_report(self, day=None):
        """Genera el PDF, lo adjunta al seguimiento y lo envía a los destinatarios."""
        self.ensure_one()
        data = {'day': fields.Date.to_string(day)} if day else None
        pdf, _fmt = self.env['ir.actions.report']._render_qweb_pdf(
            'wifi_access_provisioning.action_report_wifi_monitor', self.ids, data=data)
        suffix = day.strftime('%Y-%m-%d') if day else 'completo'
        attachment = self.env['ir.attachment'].create({
            'name': 'Seguimiento %s - %s.pdf' % (self.name, suffix),
            'raw': pdf,
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/pdf',
        })
        parts = []
        for d in self._report_payload(day)['days']:
            items = [Markup('<li>Enlace: %s</li>') % text for _lvl, text in d['context_diagnosis']]
            items += [Markup('<li>%s: %s</li>') % (t['target'].name, ' '.join(x for _lvl, x in t['diagnosis']))
                      for t in d['targets']]
            parts.append(Markup('<p><b>%s</b></p><ul>%s</ul>') % (d['label'], Markup('').join(items)))
        body = Markup('<p>Informe de seguimiento <b>%s</b>. El detalle con gráficos está en el PDF adjunto.</p>%s') % (
            self.name, Markup('').join(parts))
        self.message_post(body=body, subject='Seguimiento: %s (%s)' % (self.name, suffix),
                          partner_ids=self.recipient_ids.ids, attachment_ids=[attachment.id],
                          message_type='comment', subtype_xmlid='mail.mt_comment')
        return attachment


class WifiMonitorTarget(models.Model):
    _name = 'wifi.monitor.target'
    _description = 'Equipo en seguimiento'
    _order = 'monitor_id, id'

    monitor_id = fields.Many2one('wifi.monitor', 'Seguimiento', required=True, ondelete='cascade', index=True)
    device_id = fields.Many2one('wifi.access.device', 'Dispositivo',
                                help='Opcional: un dispositivo ya dado de alta. Si no, alcanza con la MAC.')
    name = fields.Char('Descripción', required=True)
    mac = fields.Char('MAC', required=True)
    online = fields.Boolean('Conectado', readonly=True)
    last_ip = fields.Char('IP')
    last_ap = fields.Char('AP', readonly=True)
    last_signal = fields.Integer('Señal (dBm)', readonly=True)
    last_seen_at = fields.Datetime('Visto por última vez', readonly=True)
    last_sample_at = fields.Datetime(readonly=True)
    # base para calcular el consumo entre dos muestras
    counter_at = fields.Datetime(readonly=True)
    last_down_total = fields.Float(readonly=True, digits=(20, 0))
    last_up_total = fields.Float(readonly=True, digits=(20, 0))

    _mac_uniq = models.Constraint('UNIQUE(monitor_id, mac)', 'La MAC ya está en este seguimiento.')

    @api.onchange('device_id')
    def _onchange_device_id(self):
        if self.device_id:
            self.mac = self.device_id.mac
            self.name = self.device_id.name
            self.last_ip = self.device_id.ip_address

    @api.model
    def _clean_vals(self, vals):
        if vals.get('mac'):
            mac = normalize_mac(vals['mac'])
            if not mac:
                raise ValidationError('MAC inválida: %s' % vals['mac'])
            vals['mac'] = mac
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._clean_vals(dict(v)) for v in vals_list])

    def write(self, vals):
        return super().write(self._clean_vals(dict(vals)))


class WifiMonitorSample(models.Model):
    _name = 'wifi.monitor.sample'
    _description = 'Muestra de seguimiento'
    _order = 'timestamp desc, id desc'
    _rec_name = 'timestamp'

    monitor_id = fields.Many2one('wifi.monitor', 'Seguimiento', required=True, ondelete='cascade', index=True)
    target_id = fields.Many2one('wifi.monitor.target', 'Equipo', required=True, ondelete='cascade', index=True)
    timestamp = fields.Datetime('Momento', required=True, index=True)
    online = fields.Boolean('Conectado')
    unifi_error = fields.Boolean('Sin datos de UniFi', help='No se pudo leer el controlador en esta muestra.')
    ip = fields.Char('IP')
    ap_mac = fields.Char('MAC del AP')
    ap_name = fields.Char('AP')
    essid = fields.Char('SSID')
    channel = fields.Integer('Canal', aggregator='max')
    signal = fields.Integer('Señal (dBm)', aggregator='avg')
    satisfaction = fields.Integer('Satisfacción UniFi (%)', aggregator='avg')
    link_down_mbps = fields.Float('Velocidad de enlace bajada (Mbps)', aggregator='avg', digits=(10, 1))
    link_up_mbps = fields.Float('Velocidad de enlace subida (Mbps)', aggregator='avg', digits=(10, 1))
    has_rate = fields.Boolean('Consumo medido')
    down_mbps = fields.Float('Consumo bajada (Mbps)', aggregator='avg', digits=(10, 2))
    up_mbps = fields.Float('Consumo subida (Mbps)', aggregator='avg', digits=(10, 2))
    down_bytes = fields.Float('Bajada (bytes)', digits=(20, 0))
    up_bytes = fields.Float('Subida (bytes)', digits=(20, 0))
    ping_done = fields.Boolean('Ping medido')
    ping_ms = fields.Float('Ping (ms)', aggregator='avg', digits=(10, 1))
    ping_loss = fields.Float('Pérdida (%)', aggregator='avg', digits=(5, 0))


class WifiMonitorContext(models.Model):
    _name = 'wifi.monitor.context'
    _description = 'Muestra del enlace y del router'
    _order = 'timestamp desc, id desc'
    _rec_name = 'timestamp'

    monitor_id = fields.Many2one('wifi.monitor', 'Seguimiento', required=True, ondelete='cascade', index=True)
    timestamp = fields.Datetime('Momento', required=True, index=True)
    wan_down_mbps = fields.Float('Enlace bajada (Mbps)', aggregator='avg', digits=(10, 1))
    wan_up_mbps = fields.Float('Enlace subida (Mbps)', aggregator='avg', digits=(10, 1))
    cpu_load = fields.Float('CPU (%)', aggregator='avg', digits=(5, 0))
    inet_ping_done = fields.Boolean('Ping a internet medido')
    inet_ping_ms = fields.Float('Ping a internet (ms)', aggregator='avg', digits=(10, 1))
    inet_ping_loss = fields.Float('Pérdida a internet (%)', aggregator='avg', digits=(5, 0))


class ReportWifiMonitor(models.AbstractModel):
    _name = 'report.wifi_access_provisioning.report_wifi_monitor'
    _description = 'Informe de seguimiento'

    @api.model
    def _get_report_values(self, docids, data=None):
        docs = self.env['wifi.monitor'].browse(docids)
        day = fields.Date.to_date((data or {}).get('day'))
        return {
            'doc_ids': docids,
            'doc_model': 'wifi.monitor',
            'docs': docs,
            'payloads': {doc.id: doc._report_payload(day) for doc in docs},
        }
