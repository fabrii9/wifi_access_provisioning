# Aprovisionamiento WiFi (UniFi + MikroTik) — Odoo 19

Alta y baja de dispositivos desde Odoo:

1. **UniFi:** agrega o quita la MAC en la lista blanca de una red WiFi.
2. **MikroTik:** crea o elimina la reserva DHCP con IP fija, usando la IP siguiente
   a la última reserva creada en la red configurada (por defecto 172.17.0.0/16).
3. Opcional: crea y elimina la cola simple de la IP.
4. **Seguimientos:** ante un reclamo, mide en un horario programado la señal, el AP,
   la latencia y el consumo de cada equipo, junto con la ocupación del enlace y la
   latencia a internet, y arma un informe PDF con gráficos y diagnóstico.

## Instalación

Copiar `wifi_access_provisioning` a la carpeta de addons, actualizar la lista de
aplicaciones e instalar **Acceso WiFi**. Solo depende de `base` y `mail`, y no
usa librerías externas: el cliente de la API de RouterOS está incluido.

## Configuración (Acceso WiFi → Configuración)

1. **Controladores UniFi:** URL (`https://host:8443`), sitio (`default`), usuario
   admin local sin MFA. Marcar "Consola UniFi OS" solo para UDM o Cloud Key Gen2+.
   → *Probar conexión* → *Sincronizar redes WiFi*.
2. **Routers MikroTik:** host, puerto 8729 con api-ssl (recomendado) u 8728,
   usuario con permisos `read, write, api` (más `test` para el ping de los seguimientos).
   → *Probar conexión*.
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

## Seguimientos (Acceso WiFi → Seguimientos)

Para investigar un reclamo del tipo "estas PCs se cortan entre las 11 y las 13":

1. En **Dispositivos**, seleccionar los equipos → *Acción* → **Hacer seguimiento**
   (o crear el seguimiento a mano y cargar las MACs, aunque no estén dadas de alta).
2. Ajustar días, horario (conviene dejar margen antes y después), intervalo y a quién
   enviar el informe. En *Fuentes*, la interfaz WAN y el ancho de banda contratado.
3. **Iniciar.** Una tarea programada toma muestras cada minuto que corresponda, solo
   dentro de la ventana. *Tomar muestra ahora* prueba la lectura en el momento.

En cada muestra se lee, con una sola conexión a cada equipo:

- **UniFi:** si el equipo está conectado, AP, SSID, canal, señal (dBm), velocidad de
  enlace y bytes (para calcular el consumo entre muestras).
- **MikroTik:** ping a cada equipo (latencia de la red local y del WiFi), ping a
  internet (8.8.8.8 por defecto), tráfico de la interfaz WAN y CPU.

El **informe** (PDF) tiene, por día, el estado del enlace y, por equipo, un diagnóstico
automático (desconexiones, señal débil, cambios de AP, pérdida, latencia, coincidencia
con saturación) más los gráficos de señal, latencia y consumo con las franjas de
desconexión y de enlace saturado. Se genera cada día al cerrar la ventana, al
finalizar o a mano; queda adjunto en el seguimiento y se envía a los destinatarios.
Las muestras también se pueden explorar como lista, gráfico o tabla dinámica.

## Permisos

- **Operador:** da de alta, baja y verifica dispositivos.
- **Administrador:** además configura y ve las credenciales.

## Notas

- La lista de MACs de UniFi se reescribe completa en cada cambio (lectura → modificación → escritura).
- Las MACs aleatorias ("dirección privada" en celulares) se marcan con una advertencia.
- Si la MAC ya tiene una reserva fija dentro de la red, se reutiliza en vez de duplicarla.
  Si la MAC tenía un préstamo dinámico, se borra y se reemplaza por la reserva fija.
