# Aprovisionamiento WiFi (UniFi + MikroTik) — Odoo 19

Alta y baja de dispositivos desde Odoo:

1. **UniFi:** agrega o quita la MAC en la lista blanca de una red WiFi.
2. **MikroTik:** crea o elimina la reserva DHCP con IP fija, usando la IP siguiente
   a la última reserva creada en la red configurada (por defecto 172.17.0.0/16).
3. Opcional: crea y elimina la cola simple de la IP.

## Instalación

Copiar `wifi_access_provisioning` a la carpeta de addons, actualizar la lista de
aplicaciones e instalar **Acceso WiFi**. Solo depende de `base` y `mail`, y no
usa librerías externas: el cliente de la API de RouterOS está incluido.

## Configuración (Acceso WiFi → Configuración)

1. **Controladores UniFi:** URL (`https://host:8443`), sitio (`default`), usuario
   admin local sin MFA. Marcar "Consola UniFi OS" solo para UDM o Cloud Key Gen2+.
   → *Probar conexión* → *Sincronizar redes WiFi*.
2. **Routers MikroTik:** host, puerto 8729 con api-ssl (recomendado) u 8728,
   usuario con permisos `read, write, api`. → *Probar conexión*.
3. **Perfiles de acceso:** red WiFi + router + red de IPs + estrategia de asignación.
   → *Ver próxima IP* → *Importar dispositivos existentes* (solo lee los equipos).

## Uso (Acceso WiFi → Dispositivos)

Cargar la descripción y la MAC (se acepta cualquier formato) → **Dar de alta**.
La IP se asigna sola, o se puede indicar a mano en borrador.

- **Verificar:** lee los equipos y marca diferencias, sin modificar nada.
- **Dar de baja:** quita la MAC de la lista y elimina la reserva y la cola.
- Si un paso falla, el dispositivo queda en *Error* con el detalle. *Reintentar*
  (o la tarea programada cada 30 min) retoma solo los pasos pendientes.
- Las altas y bajas del mismo perfil se ejecutan de a una, para que dos
  operaciones simultáneas no se pisen la lista de MACs ni la IP.

## Permisos

- **Operador:** da de alta, baja y verifica dispositivos.
- **Administrador:** además configura y ve las credenciales.

## Notas

- La lista de MACs de UniFi se reescribe completa en cada cambio (lectura → modificación → escritura).
- Las MACs aleatorias ("dirección privada" en celulares) se marcan con una advertencia.
- Si la MAC ya tiene una reserva fija dentro de la red, se reutiliza en vez de duplicarla.
  Si la MAC tenía un préstamo dinámico, se borra y se reemplaza por la reserva fija.
