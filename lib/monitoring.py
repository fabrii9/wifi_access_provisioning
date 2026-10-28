"""Funciones puras del seguimiento: tiempos de RouterOS, gráficos SVG y diagnóstico.

No dependen de Odoo para poder probarlas por separado. Las fechas que reciben son
datetimes "naive" en hora local.
"""
import math
import re
import statistics
from datetime import timedelta
from html import escape

# Umbrales del diagnóstico
WEAK_SIGNAL_DBM = -70
HIGH_PING_MS = 50
HIGH_INET_PING_MS = 80
LOSS_PCT = 5
ROAM_COUNT = 3
SATURATION_PCT = 90
HIGH_CPU_PCT = 80

PALETTE = ['#2563eb', '#ea580c', '#16a34a', '#9333ea']

_DURATION = re.compile(r'(\d+(?:\.\d+)?)(ms|us|ns|h|m|s)')
_UNIT_MS = {'h': 3600000, 'm': 60000, 's': 1000, 'ms': 1, 'us': 0.001, 'ns': 0.000001}


def parse_ros_duration(value):
    """'1ms234us' / '12ms' / '345us' / '1s5ms' → milisegundos (float), o None."""
    if not value:
        return None
    parts = _DURATION.findall(value)
    if not parts:
        return None
    return sum(float(n) * _UNIT_MS[u] for n, u in parts)


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Rangos de tiempo
# ----------------------------------------------------------------------
def ranges(points, max_gap):
    """[(ts, bool)] ordenados → [(desde, hasta)] de los tramos consecutivos en True."""
    out, start, last = [], None, None
    for ts, flag in points:
        if flag and start is not None and ts - last > max_gap:
            out.append((start, last))
            start = None
        if flag:
            start = start or ts
            last = ts
        elif start is not None:
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    return out


def format_ranges(spans, limit=6):
    text = ', '.join('%s' % a.strftime('%H:%M') if a == b else '%s–%s' % (a.strftime('%H:%M'), b.strftime('%H:%M'))
                     for a, b in spans[:limit])
    if len(spans) > limit:
        text += ' y %d más' % (len(spans) - limit)
    return text


def percentile(values, pct):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


# ----------------------------------------------------------------------
# Diagnóstico
# ----------------------------------------------------------------------
def target_stats(samples):
    """samples: [{'ts','online','signal','ap','ping_ms','loss','down_mbps','up_mbps','down_bytes','up_bytes'}]"""
    online = [s for s in samples if s['online']]
    signals = [s['signal'] for s in online if s.get('signal') is not None]
    pings = [s['ping_ms'] for s in samples if s.get('ping_ms') is not None]
    losses = [s['loss'] for s in samples if s.get('loss') is not None]
    downs = [s['down_mbps'] for s in online if s.get('down_mbps') is not None]
    ups = [s['up_mbps'] for s in online if s.get('up_mbps') is not None]
    roams, aps, prev = 0, [], None
    for s in online:
        if s.get('ap'):
            if prev and s['ap'] != prev:
                roams += 1
            if s['ap'] not in aps:
                aps.append(s['ap'])
            prev = s['ap']
    return {
        'count': len(samples),
        'online_pct': 100.0 * len(online) / len(samples) if samples else 0,
        'signal_median': statistics.median(signals) if signals else None,
        'signal_min': min(signals) if signals else None,
        'weak_pct': 100.0 * sum(1 for v in signals if v <= WEAK_SIGNAL_DBM) / len(signals) if signals else 0,
        'aps': aps,
        'roams': roams,
        'ping_median': statistics.median(pings) if pings else None,
        'ping_p90': percentile(pings, 90),
        'loss_avg': statistics.mean(losses) if losses else None,
        'down_peak': max(downs) if downs else None,
        'up_peak': max(ups) if ups else None,
        'down_mb': sum(s.get('down_bytes') or 0 for s in online) / 1e6,
        'up_mb': sum(s.get('up_bytes') or 0 for s in online) / 1e6,
    }


def diagnose_target(samples, stats, max_gap, saturated_spans=()):
    """Devuelve [(nivel, texto)] con nivel en ok / warn / bad."""
    if not samples:
        return [('warn', 'No hay muestras de este equipo en el período.')]
    out = []
    offline = ranges([(s['ts'], not s['online']) for s in samples], max_gap)
    if stats['online_pct'] == 0:
        out.append(('bad', 'No se vio conectado al WiFi en ninguna muestra: revisar que esté encendido, '
                           'la MAC (¿dirección privada?) y que esté en la lista blanca.'))
        return out
    if offline:
        level = 'bad' if stats['online_pct'] < 90 else 'warn'
        out.append((level, 'Desconectado del WiFi en %.0f%% de las muestras: %s.'
                    % (100 - stats['online_pct'], format_ranges(offline))))
    if stats['signal_median'] is not None:
        if stats['signal_median'] <= WEAK_SIGNAL_DBM:
            out.append(('bad', 'Señal débil: mediana %d dBm (por debajo de %d dBm es inestable). '
                               'Revisar ubicación del equipo o del AP.' % (stats['signal_median'], WEAK_SIGNAL_DBM)))
        elif stats['weak_pct'] >= 25:
            out.append(('warn', 'Señal débil en %.0f%% del tiempo (mínimo %d dBm).'
                        % (stats['weak_pct'], stats['signal_min'])))
    if stats['roams'] >= ROAM_COUNT:
        out.append(('warn', 'Cambió de AP %d veces (%s). Puede indicar cobertura pareja entre APs '
                            'o un equipo que salta de uno a otro.' % (stats['roams'], ', '.join(stats['aps']))))
    if stats['loss_avg'] is not None and stats['loss_avg'] >= LOSS_PCT:
        out.append(('bad', 'Pérdida de paquetes entre el router y el equipo: %.0f%% promedio.' % stats['loss_avg']))
    if stats['ping_p90'] is not None and stats['ping_p90'] >= HIGH_PING_MS:
        slow = [s for s in samples if (s.get('ping_ms') or 0) >= HIGH_PING_MS]
        text = 'Latencia alta dentro de la red local: %.0f ms en el 10%% peor de las muestras.' % stats['ping_p90']
        if saturated_spans and slow:
            during = sum(1 for s in slow if any(a <= s['ts'] <= b for a, b in saturated_spans))
            if during * 2 >= len(slow):
                text += ' Coincide con la saturación del enlace.'
        out.append(('warn', text))
    if not out:
        out.append(('ok', 'Sin anomalías del lado del WiFi: buena señal, conectado y sin pérdida hacia el router.'))
    return out


def context_stats(samples, cap_down, cap_up):
    downs = [s['wan_down'] for s in samples if s.get('wan_down') is not None]
    ups = [s['wan_up'] for s in samples if s.get('wan_up') is not None]
    cpus = [s['cpu'] for s in samples if s.get('cpu') is not None]
    inet = [s['inet_ms'] for s in samples if s.get('inet_ms') is not None]

    def saturated(s):
        return ((cap_down and (s.get('wan_down') or 0) >= cap_down * SATURATION_PCT / 100)
                or (cap_up and (s.get('wan_up') or 0) >= cap_up * SATURATION_PCT / 100))
    return {
        'count': len(samples),
        'down_peak': max(downs) if downs else None,
        'up_peak': max(ups) if ups else None,
        'down_avg': statistics.mean(downs) if downs else None,
        'up_avg': statistics.mean(ups) if ups else None,
        'cpu_peak': max(cpus) if cpus else None,
        'inet_median': statistics.median(inet) if inet else None,
        'inet_p90': percentile(inet, 90),
        'inet_slow': [(s['ts'], (s.get('inet_ms') or 0) >= HIGH_INET_PING_MS or (s.get('inet_loss') or 0) >= 50)
                      for s in samples],
        'saturated': [(s['ts'], bool(saturated(s))) for s in samples],
    }


def diagnose_context(stats, max_gap, cap_down, cap_up):
    if not stats['count']:
        return [('warn', 'No hay muestras del enlace en el período.')], []
    out = []
    spans = ranges(stats['saturated'], max_gap)
    if spans:
        sat = sum(1 for _ts, flag in stats['saturated'] if flag)
        out.append(('bad', 'Enlace saturado (≥%d%% de %s/%s Mbps) en %.0f%% de las muestras: %s. '
                           'En esos momentos todos los equipos compiten por igual y la navegación se degrada.'
                    % (SATURATION_PCT, cap_down, cap_up, 100.0 * sat / stats['count'], format_ranges(spans))))
    else:
        out.append(('ok', 'El enlace no llegó a saturarse (pico %.0f Mbps de bajada y %.0f de subida).'
                    % (stats['down_peak'] or 0, stats['up_peak'] or 0)))
    slow = ranges(stats['inet_slow'], max_gap)
    if slow:
        out.append(('bad' if spans else 'warn',
                    'Internet lenta desde el router (ping ≥%d ms o con pérdida): %s. Mediana %.0f ms, peor 10%% %.0f ms.%s'
                    % (HIGH_INET_PING_MS, format_ranges(slow), stats['inet_median'] or 0, stats['inet_p90'] or 0,
                       '' if spans else ' Sin saturación propia: puede ser un problema del proveedor.')))
    if stats['cpu_peak'] is not None and stats['cpu_peak'] >= HIGH_CPU_PCT:
        out.append(('warn', 'CPU del router alta: pico de %d%%.' % stats['cpu_peak']))
    return out, spans


# ----------------------------------------------------------------------
# Gráfico de líneas en SVG (se incrusta en el PDF, sin librerías)
# ----------------------------------------------------------------------
def _nice_step(span, ticks=4):
    raw = span / ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def line_chart(series, x_from, x_to, title, unit='', y_min=None, y_max=None, ref_lines=(), bands=(),
               max_gap=None, width=720, height=180):
    """series: [{'label', 'points': [(ts, valor|None)], 'color'?}]
    ref_lines: [(valor, etiqueta, color)] · bands: [(desde, hasta, color, etiqueta)] sombreado de fondo.
    Corta la línea en valores None o saltos mayores a max_gap."""
    left, right, top, bottom = 48, 12, 26, 34
    pw, ph = width - left - right, height - top - bottom
    span_s = max((x_to - x_from).total_seconds(), 60)

    values = [v for s in series for _t, v in s['points'] if v is not None]
    values += [v for v, _l, _c in ref_lines]
    lo = y_min if y_min is not None else (min(values + [0]) if values else 0)
    hi = y_max if y_max is not None else (max(values) if values else 1)
    if hi <= lo:
        hi = lo + 1
    step = _nice_step(hi - lo)
    if y_min is None:
        lo = math.floor(lo / step) * step
    if y_max is None:
        hi = math.ceil(hi / step) * step

    def x(ts):
        return left + pw * min(max((ts - x_from).total_seconds() / span_s, 0), 1)

    def y(v):
        return top + ph * (1 - (min(max(v, lo), hi) - lo) / (hi - lo))

    p = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
         'font-family="Helvetica, Arial, sans-serif" font-size="10">' % (width, height, width, height),
         '<text x="%d" y="14" font-size="12" font-weight="bold" fill="#111827">%s</text>' % (left, escape(title))]
    for a, b, color, _label in bands:
        p.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" opacity="0.18"/>'
                 % (x(a), top, max(x(b) - x(a), 2), ph, color))
    v = lo
    while v <= hi + step / 1000:
        p.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#e5e7eb"/>' % (left, left + pw, y(v), y(v)))
        p.append('<text x="%d" y="%.1f" text-anchor="end" fill="#6b7280">%s</text>'
                 % (left - 4, y(v) + 3, ('%g' % round(v, 2))))
        v += step
    if unit:
        p.append('<text x="4" y="%d" fill="#6b7280">%s</text>' % (top - 6, escape(unit)))

    minutes = span_s / 60
    tick = next((t for t in (5, 10, 15, 30, 60, 120, 240, 480) if minutes / t <= 8), 720)
    t0 = x_from.replace(second=0, microsecond=0)
    t0 += timedelta(minutes=(-t0.minute) % tick if tick < 60 else (60 - t0.minute) % 60)
    t = t0
    while t <= x_to:
        p.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="#f3f4f6"/>' % (x(t), x(t), top, top + ph))
        p.append('<text x="%.1f" y="%d" text-anchor="middle" fill="#6b7280">%s</text>'
                 % (x(t), top + ph + 14, t.strftime('%H:%M')))
        t += timedelta(minutes=tick)

    for value, label, color in ref_lines:
        p.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-dasharray="4 3"/>'
                 % (left, left + pw, y(value), y(value), color))
        p.append('<text x="%d" y="%.1f" text-anchor="end" fill="%s">%s</text>'
                 % (left + pw, y(value) - 3, color, escape(label)))

    if not values or not any(v is not None for s in series for _t, v in s['points']):
        p.append('<text x="%d" y="%d" text-anchor="middle" fill="#9ca3af" font-size="12">Sin datos</text>'
                 % (left + pw / 2, top + ph / 2))

    lx = left
    for i, s in enumerate(series):
        color = s.get('color') or PALETTE[i % len(PALETTE)]
        segments, cur, prev_t = [], [], None
        for ts, val in s['points']:
            if val is None or (max_gap and prev_t and ts - prev_t > max_gap):
                if cur:
                    segments.append(cur)
                cur = []
            if val is not None:
                cur.append((x(ts), y(val)))
            prev_t = ts
        if cur:
            segments.append(cur)
        for seg in segments:
            if len(seg) == 1:
                p.append('<circle cx="%.1f" cy="%.1f" r="2" fill="%s"/>' % (seg[0][0], seg[0][1], color))
            else:
                p.append('<polyline fill="none" stroke="%s" stroke-width="1.6" points="%s"/>'
                         % (color, ' '.join('%.1f,%.1f' % pt for pt in seg)))
        p.append('<rect x="%d" y="%d" width="10" height="3" fill="%s"/>' % (lx, height - 9, color))
        p.append('<text x="%d" y="%d" fill="#374151">%s</text>' % (lx + 14, height - 5, escape(s['label'])))
        lx += 24 + 6 * len(s['label'])
    for color, label in dict.fromkeys((color, label) for _a, _b, color, label in bands):
        p.append('<rect x="%d" y="%d" width="10" height="8" fill="%s" opacity="0.3"/>' % (lx, height - 12, color))
        p.append('<text x="%d" y="%d" fill="#374151">%s</text>' % (lx + 14, height - 5, escape(label)))
        lx += 24 + 6 * len(label)
    p.append('</svg>')
    return '\n'.join(p)
