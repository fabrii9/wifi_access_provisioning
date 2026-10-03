{
    'name': 'Aprovisionamiento WiFi (UniFi + MikroTik)',
    'version': '19.0.1.0.0',
    'category': 'Services',
    'summary': 'Alta y baja de dispositivos: lista blanca de MAC en UniFi y reserva DHCP con IP fija en MikroTik',
    'description': """
Gestiona desde Odoo el acceso de dispositivos a una red WiFi con lista blanca de MAC:

* Agrega / quita la MAC en la lista blanca de una red WiFi del controlador UniFi.
* Crea / elimina una reserva DHCP (IP fija) en un router MikroTik, tomando la
  siguiente IP disponible de la red configurada.
* Opcionalmente crea la cola simple asociada a la IP.
* Importa los dispositivos ya existentes en los equipos.
* Conexiones, redes y reglas configurables desde la interfaz.
""",
    'author': 'Aftermoves',
    'license': 'LGPL-3',
    'depends': ['base', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/unifi_controller_views.xml',
        'views/mikrotik_router_views.xml',
        'views/access_profile_views.xml',
        'views/access_device_views.xml',
        'views/menus.xml',
    ],
    'application': True,
    'installable': True,
}
