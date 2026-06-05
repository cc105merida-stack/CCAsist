import asyncio
import os
import gspread
import img2pdf
import tempfile
import pytz
from google.oauth2.service_account import Credentials
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes, CallbackQueryHandler
import warnings
import json
import logging

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Ignorar warnings de deprecación
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ========== CONFIGURACIÓN DESDE VARIABLES DE ENTORNO ==========
TOKEN = os.environ.get('TELEGRAM_TOKEN')
SPREADSHEET_ID = "1YUTaWS-_2GPghdv-bl_XU3vHY2GghtD-k4FN2UZWQHs"

if not TOKEN:
    logger.error("❌ TELEGRAM_TOKEN no está configurado en variables de entorno")
    exit(1)

if not SPREADSHEET_ID:
    logger.error("❌ SPREADSHEET_ID no está configurado en variables de entorno")
    exit(1)

# Configurar zona horaria
LOCAL_TZ = pytz.timezone('America/Caracas')  # GMT-4

# ========== ESTADOS PARA LAS CONVERSACIONES ============
NOMBRE, CEDULA, TELEFONO, ROL = range(4)
ESTRATEGIA, SELECCION_CLIENTE, INGRESO_FOLIO = range(10, 13)

# Estados para las llamadas
class LlamadaEstado:
    def __init__(self):
        self.usuarios_activos = {}
        self.pendientes_notificacion = {}
        self.recontactos_pendientes = {}

llamadas_activas = LlamadaEstado()

# Diccionario para almacenar imágenes para PDF por usuario
imagenes_para_pdf = {}

# Almacenamiento temporal para la búsqueda de SIAC
busqueda_siac = {}  # {user_id: {'estrategia': str, 'resultados': list, 'pagina': int}}

# Roles disponibles
ROLES = {
    "AGENTE": "Agente",
    "SUPERVISOR": "Supervisor"
}

# ========== FUNCIONES AUXILIARES ============
def obtener_hora_local():
    utc_now = datetime.now(pytz.UTC)
    hora_local = utc_now.astimezone(LOCAL_TZ)
    return hora_local

def calcular_duracion(hora_asignacion, hora_completacion):
    try:
        asignacion = datetime.strptime(hora_asignacion, "%Y-%m-%d %H:%M:%S")
        completacion = datetime.strptime(hora_completacion, "%Y-%m-%d %H:%M:%S")
        diferencia = completacion - asignacion
        segundos_totales = int(diferencia.total_seconds())
        horas = segundos_totales // 3600
        minutos = (segundos_totales % 3600) // 60
        segundos = segundos_totales % 60
        return f"{horas:02d}:{minutos:02d}:{segundos:02d}"
    except Exception as e:
        logger.error(f"Error al calcular duración: {e}")
        return "00:00:00"

def conectar_google_sheets():
    try:
        credentials_json = os.environ.get('GOOGLE_CREDENTIALS')
        if not credentials_json:
            logger.error("❌ GOOGLE_CREDENTIALS no está configurado en variables de entorno")
            return None
        creds_dict = json.loads(credentials_json)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID)
        logger.info("✅ Conexión exitosa a Google Sheets")
        return sheet
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Sheets: {e}")
        return None

def obtener_hoja_registros(sheet):
    try:
        return sheet.get_worksheet(0)
    except Exception as e:
        logger.error(f"Error al obtener hoja de registros: {e}")
        return None

def obtener_hoja_data(sheet):
    try:
        return sheet.worksheet("Data")
    except Exception as e:
        logger.error(f"Error al obtener hoja Data: {e}")
        return None

def obtener_rol_usuario(worksheet_registros, telegram_id):
    try:
        todos_los_registros = worksheet_registros.get_all_values()
        for fila in todos_los_registros[1:]:
            if len(fila) > 3 and fila[3] == str(telegram_id):
                if len(fila) > 6:
                    return fila[6]
                return "Agente"
        return None
    except Exception as e:
        logger.error(f"Error al obtener rol de usuario: {e}")
        return None

def obtener_nombre_usuario(worksheet_registros, telegram_id):
    try:
        todos_los_registros = worksheet_registros.get_all_values()
        for fila in todos_los_registros[1:]:
            if len(fila) > 3 and fila[3] == str(telegram_id):
                return fila[1]
        return None
    except Exception as e:
        logger.error(f"Error al obtener nombre de usuario: {e}")
        return None

def obtener_agente_por_nombre(worksheet_registros, nombre_agente):
    try:
        todos_los_registros = worksheet_registros.get_all_values()
        for fila in todos_los_registros[1:]:
            if len(fila) > 1 and fila[1] == nombre_agente and len(fila) > 3:
                return fila[3]
        return None
    except Exception as e:
        logger.error(f"Error al obtener agente: {e}")
        return None

def obtener_llamadas_pendientes(data_worksheet):
    try:
        todos_los_registros = data_worksheet.get_all_values()
        pendientes = []
        for i, fila in enumerate(todos_los_registros[1:], start=2):
            if len(fila) >= 22:
                estado2 = fila[20] if len(fila) > 20 else ""
                agente_asignado = fila[21] if len(fila) > 21 else ""
                if estado2 == "NO CONTESTA" and (agente_asignado == "" or agente_asignado is None):
                    pendientes.append({
                        'fila': i,
                        'folio': fila[3] if len(fila) > 3 else "No disponible",
                        'nombre_cliente': fila[8] if len(fila) > 8 else "No disponible",
                        'celular_cliente': fila[9] if len(fila) > 9 else "No disponible",
                        'validador': fila[1] if len(fila) > 1 else "No asignado",
                        'hora_asignacion': fila[18] if len(fila) > 18 else "No registrada",
                        'datos_completos': fila
                    })
        return pendientes
    except Exception as e:
        logger.error(f"Error al obtener llamadas pendientes: {e}")
        return []

def obtener_siguiente_llamada_disponible(data_worksheet):
    try:
        todos_los_registros = data_worksheet.get_all_values()
        for i, fila in enumerate(todos_los_registros[1:], start=2):
            if len(fila) >= 21:
                estado2 = fila[20] if len(fila) > 20 else ""
                if estado2 == "" or estado2 == "DISPONIBLE":
                    return i, fila
        return None, None
    except Exception as e:
        logger.error(f"Error al obtener siguiente llamada: {e}")
        return None, None

def verificar_usuario_registrado(worksheet, cedula, telegram_id=None):
    try:
        todos_los_registros = worksheet.get_all_values()
        for fila in todos_los_registros[1:]:
            if len(fila) > 2 and fila[2] == cedula:
                return True
            if telegram_id and len(fila) > 3 and fila[3] == str(telegram_id):
                return True
        return False
    except Exception as e:
        logger.error(f"Error al verificar usuario: {e}")
        return False

def guardar_en_sheets(worksheet, nombre, cedula, telegram_id, telefono, rol):
    try:
        hora_local = obtener_hora_local()
        fecha_hora = hora_local.strftime("%Y-%m-%d %H:%M:%S")
        estado = "ACTIVO"
        nueva_fila = [fecha_hora, nombre, cedula, str(telegram_id), telefono, estado, rol]
        worksheet.append_row(nueva_fila)
        return True
    except Exception as e:
        logger.error(f"Error al guardar: {e}")
        return False

def obtener_registros_por_telegram_id(worksheet, telegram_id):
    try:
        todos_los_registros = worksheet.get_all_values()
        registros_usuario = []
        for fila in todos_los_registros[1:]:
            if len(fila) > 3 and fila[3] == str(telegram_id):
                registros_usuario.append(fila)
        return registros_usuario
    except Exception as e:
        logger.error(f"Error al obtener registros: {e}")
        return []

def actualizar_estado_llamada(data_worksheet, fila_numero, nuevo_estado):
    try:
        data_worksheet.update_cell(fila_numero, 21, nuevo_estado)  # Columna U
        logger.info(f"✅ Estado actualizado: fila {fila_numero} -> {nuevo_estado}")
        return True
    except Exception as e:
        logger.error(f"❌ Error al actualizar estado de llamada: {e}")
        return False

def registrar_hora_asignacion(data_worksheet, fila_numero):
    try:
        hora_local = obtener_hora_local()
        hora_actual = hora_local.strftime("%Y-%m-%d %H:%M:%S")
        data_worksheet.update_cell(fila_numero, 19, hora_actual)  # Columna S
        logger.info(f"✅ Hora registrada: fila {fila_numero} -> {hora_actual}")
        return hora_actual
    except Exception as e:
        logger.error(f"❌ Error al registrar hora de asignación: {e}")
        return None

def incrementar_intentos(data_worksheet, fila_numero):
    """Incrementa el contador de intentos (columna AN, índice 40)"""
    try:
        # Leer valor actual (columna AN = índice 40 en 1-based)
        valor_actual = data_worksheet.cell(fila_numero, 40).value
        if valor_actual is None or valor_actual == "":
            nuevo_valor = 1
        else:
            try:
                nuevo_valor = int(valor_actual) + 1
            except ValueError:
                nuevo_valor = 1
        data_worksheet.update_cell(fila_numero, 40, nuevo_valor)
        logger.info(f"✅ Intentos incrementados: fila {fila_numero} -> {nuevo_valor}")
        return nuevo_valor
    except Exception as e:
        logger.error(f"❌ Error al incrementar intentos: {e}")
        return None

def registrar_duracion_y_observacion(data_worksheet, fila_numero, resultado, observacion="", hora_asignacion=None, estado_validacion=None):
    try:
        if resultado == "completada":
            nuevo_estado = "COMPLETADA"
        elif resultado == "no_contesta":
            nuevo_estado = "NO CONTESTA"
        elif resultado == "rechazada":
            nuevo_estado = "RECHAZADA"
        else:
            nuevo_estado = "FINALIZADA"
        data_worksheet.update_cell(fila_numero, 21, nuevo_estado)  # Columna U
        if estado_validacion:
            data_worksheet.update_cell(fila_numero, 3, estado_validacion)  # Columna C
        if hora_asignacion:
            hora_local = obtener_hora_local()
            hora_completacion = hora_local.strftime("%Y-%m-%d %H:%M:%S")
            duracion = calcular_duracion(hora_asignacion, hora_completacion)
            data_worksheet.update_cell(fila_numero, 20, duracion)  # Columna T
        if observacion:
            # Leer observación actual desde la columna R (índice 18)
            obs_actual = data_worksheet.cell(fila_numero, 18).value or ""
            nueva_obs = f"{obs_actual} | {observacion}" if obs_actual else observacion
            # Guardar en la columna R (índice 18)
            data_worksheet.update_cell(fila_numero, 18, nueva_obs)
        return True
    except Exception as e:
        logger.error(f"Error al registrar duración y observación: {e}")
        return False

def formatear_datos_llamada(datos_llamada, es_recontacto=False):
    folio = datos_llamada[3] if len(datos_llamada) > 3 and datos_llamada[3] else "No disponible"
    tipo_venta = datos_llamada[4] if len(datos_llamada) > 4 and datos_llamada[4] else "No disponible"
    estrategia = datos_llamada[5] if len(datos_llamada) > 5 and datos_llamada[5] else "No disponible"
    gerentes = datos_llamada[6] if len(datos_llamada) > 6 and datos_llamada[6] else "No disponible"
    clave_promotor = datos_llamada[7] if len(datos_llamada) > 7 and datos_llamada[7] else "No disponible"
    nombre_cliente = datos_llamada[8] if len(datos_llamada) > 8 and datos_llamada[8] else "No disponible"
    celular_cliente = datos_llamada[9] if len(datos_llamada) > 9 and datos_llamada[9] else "No disponible"
    celular_referencia = datos_llamada[10] if len(datos_llamada) > 10 and datos_llamada[10] else "No disponible"
    correo = datos_llamada[11] if len(datos_llamada) > 11 and datos_llamada[11] else "No disponible"
    direccion = datos_llamada[12] if len(datos_llamada) > 12 and datos_llamada[12] else "No disponible"
    precio_paquete = datos_llamada[13] if len(datos_llamada) > 13 and datos_llamada[13] else "No disponible"
    velocidad = datos_llamada[14] if len(datos_llamada) > 14 and datos_llamada[14] else "No disponible"
    gastos_instalacion = datos_llamada[15] if len(datos_llamada) > 15 and datos_llamada[15] else "No disponible"
    beneficios = datos_llamada[16] if len(datos_llamada) > 16 and datos_llamada[16] else "No disponible"
    observacion_existente = datos_llamada[17] if len(datos_llamada) > 17 and datos_llamada[17] else None

    titulo = "🔄 RECONTACTO ASIGNADO 🔄" if es_recontacto else "📞 LLAMADA ASIGNADA 📞"
    hora_local = obtener_hora_local()
    hora_mostrar = hora_local.strftime('%H:%M:%S')

    mensaje = f"{titulo}\n\n"
    mensaje += f"📋 FOLIO SIAC: {folio}\n"
    mensaje += f"🏷️ TIPO DE VENTA: {tipo_venta}\n"
    mensaje += f"🎯 ESTRATEGIA: {estrategia}\n"
    mensaje += f"👔 GERENTES: {gerentes}\n"
    mensaje += f"🔑 CLAVE DE PROMOTOR: {clave_promotor}\n"
    mensaje += f"👤 NOMBRE DEL CLIENTE: {nombre_cliente}\n"
    mensaje += f"📱 CELULAR CLIENTE: {celular_cliente}\n"
    mensaje += f"📞 CELULAR REFERENCIA: {celular_referencia}\n"
    mensaje += f"✉️ CORREO: {correo}\n"
    mensaje += f"📍 DIRECCIÓN: {direccion}\n"
    mensaje += f"💰 PRECIO DEL PAQUETE: {precio_paquete}\n"
    mensaje += f"⚡ VELOCIDAD: {velocidad}\n"
    mensaje += f"🔧 GASTOS DE INSTALACIÓN: {gastos_instalacion}\n"
    mensaje += f"🎁 BENEFICIOS: {beneficios}\n"
    if observacion_existente:
        mensaje += f"\n📝 OBSERVACIÓN PREVIA:\n{observacion_existente}\n"
    mensaje += f"\n✅ La llamada ha sido marcada como EN PROCESO.\n"
    mensaje += f"🕐 Hora de asignación: {hora_mostrar}\n\n"
    mensaje += "Cuando termines, usa /completarllamada para registrar el resultado."
    return mensaje

# ========== FUNCIONES DEL BOT (REGISTRO, LLAMADAS, ETC.) ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"🔍 Verificando credenciales...")
    credentials_json = os.environ.get('GOOGLE_CREDENTIALS')
    if credentials_json:
        logger.info("✅ GOOGLE_CREDENTIALS encontrada")
        try:
            creds_dict = json.loads(credentials_json)
            logger.info(f"✅ Cuenta de servicio: {creds_dict.get('client_email', 'No encontrado')}")
        except:
            logger.error("❌ Error al parsear GOOGLE_CREDENTIALS")
    else:
        logger.error("❌ GOOGLE_CREDENTIALS no encontrada")
    logger.info(f"🔍 SPREADSHEET_ID: {SPREADSHEET_ID}")
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, user_id)
            if rol:
                if rol == "Supervisor":
                    await update.message.reply_text(
                        f"Hola 👋 Soy el bot de gestión de llamadas.\n\n"
                        f"Tu ID de Telegram es: `{user_id}`\nTu rol es: *{rol}*\n\n"
                        "📚 *Comandos disponibles para Supervisor:*\n\n"
                        "/start - Mostrar este mensaje\n"
                        "/registrar - Iniciar proceso de registro\n"
                        "/miestado - Ver tu estado de registro\n"
                        "/revisarpendientes - Ver llamadas NO CONTESTA\n"
                        "/notificar [número] - Notificar a un agente para recontacto\n"
                        "/cambiarrol [cédula] [rol] - Cambiar rol de un usuario\n"
                        "/cambiarestado [cédula] [estado] - Cambiar estado de un usuario\n"
                        "/guardarSIAC - Asignar folios SIAC a llamadas sin folio\n"
                        "/crearpdf - Iniciar creación de PDF con imágenes\n"
                        "/generarpdf - Generar PDF con las imágenes recibidas\n"
                        "/cancelarpdf - Cancelar creación de PDF\n"
                        "/ayuda - Mostrar ayuda detallada\n"
                        "/cancelar - Cancelar el registro en proceso",
                        parse_mode='Markdown'
                    )
                else:
                    await update.message.reply_text(
                        f"Hola 👋 Soy el bot de gestión de llamadas.\n\n"
                        f"Tu ID de Telegram es: `{user_id}`\nTu rol es: *{rol}*\n\n"
                        "📚 *Comandos disponibles para Agente:*\n\n"
                        "/start - Mostrar este mensaje\n"
                        "/registrar - Iniciar proceso de registro\n"
                        "/obtener - Obtener una llamada disponible\n"
                        "/misdatos - Ver tu llamada activa\n"
                        "/completarllamada - Finalizar tu llamada actual\n"
                        "/mispendientes - Ver tus recontactos pendientes\n"
                        "/miestado - Consultar tu estado de registro\n"
                        "/crearpdf - Iniciar creación de PDF con imágenes\n"
                        "/generarpdf - Generar PDF con las imágenes recibidas\n"
                        "/cancelarpdf - Cancelar creación de PDF\n"
                        "/ayuda - Mostrar ayuda detallada\n"
                        "/cancelar - Cancelar el registro en proceso",
                        parse_mode='Markdown'
                    )
                return
    await update.message.reply_text(
        f"Hola 👋 Soy el bot de gestión de llamadas.\n\nTu ID de Telegram es: `{user_id}`\n\n"
        "⚠️ *No estás registrado en el sistema.*\n\nPara registrarte, usa /registrar\n\n"
        "Si ya estás registrado, contacta al administrador.",
        parse_mode='Markdown'
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    rol = None
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol == "Supervisor":
        await update.message.reply_text(
            "📚 *COMANDOS PARA SUPERVISOR* 📚\n\n"
            "*Gestión de Usuarios:*\n/registrar - Registrar nuevo usuario\n/miestado - Ver tu estado de registro\n"
            "/cambiarrol [cédula] [rol] - Cambiar rol de un usuario\n/cambiarestado [cédula] [estado] - Cambiar estado de un usuario\n\n"
            "*Gestión de Llamadas:*\n/revisarpendientes - Ver llamadas NO CONTESTA\n/notificar [número] - Notificar a un agente\n"
            "/guardarSIAC - Asignar folios SIAC a llamadas sin folio\n\n"
            "*Gestión de PDF:*\n/crearpdf - Iniciar creación de PDF\n/generarpdf - Generar PDF\n/cancelarpdf - Cancelar creación de PDF\n\n"
            "*Otros:*\n/start - Bienvenida\n/ayuda - Esta ayuda\n/cancelar - Cancelar registro",
            parse_mode='Markdown'
        )
    elif rol == "Agente":
        await update.message.reply_text(
            "📚 *COMANDOS PARA AGENTE* 📚\n\n"
            "*Gestión de Registro:*\n/registrar - Registrarse\n/miestado - Ver estado de registro\n\n"
            "*Gestión de Llamadas:*\n/obtener - Tomar llamada disponible\n/misdatos - Ver llamada activa\n"
            "/completarllamada - Finalizar llamada actual\n/mispendientes - Ver recontactos pendientes\n\n"
            "*Gestión de PDF:*\n/crearpdf - Iniciar creación de PDF\n/generarpdf - Generar PDF\n/cancelarpdf - Cancelar creación de PDF\n\n"
            "*Otros:*\n/start - Bienvenida\n/ayuda - Esta ayuda\n/cancelar - Cancelar registro",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "📚 *COMANDOS DISPONIBLES* 📚\n\n*Para registrarte:*\n/registrar\n\n"
            "*Una vez registrado:*\nLos comandos dependerán de tu rol.\n\nContacta al administrador.",
            parse_mode='Markdown'
        )

async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Vamos a registrarte.\n\nPor favor, escribe tu nombre completo:")
    return NOMBRE

async def obtener_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nombre = update.message.text
        if not nombre or nombre.strip() == "":
            await update.message.reply_text("❌ El nombre no puede estar vacío.\nPor favor, escribe tu nombre completo:")
            return NOMBRE
        context.user_data['nombre'] = nombre.strip()
        await update.message.reply_text(f"✅ Nombre guardado: {nombre}\n\nAhora, escribe tu cédula (solo números):")
        return CEDULA
    except Exception as e:
        logger.error(f"Error en obtener_nombre: {e}")
        await update.message.reply_text("❌ Ocurrió un error. Intenta nuevamente con /registrar")
        return ConversationHandler.END

async def obtener_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cedula = update.message.text.strip()
    telegram_id = update.effective_user.id
    if not cedula.isdigit():
        await update.message.reply_text("❌ La cédula debe contener solo números.\nPor favor, ingresa tu cédula nuevamente:")
        return CEDULA
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return ConversationHandler.END
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return ConversationHandler.END
    if verificar_usuario_registrado(worksheet_registros, cedula, telegram_id):
        await update.message.reply_text("❌ ¡Ya estás registrado!\n\nPara ver tu estado, usa /miestado")
        return ConversationHandler.END
    context.user_data['cedula'] = cedula
    keyboard = [[InlineKeyboardButton("👤 Agente", callback_data="rol_agente"), InlineKeyboardButton("👑 Supervisor", callback_data="rol_supervisor")]]
    await update.message.reply_text(f"✅ Cédula guardada: {cedula}\n\nAhora, selecciona tu rol:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ROL

async def obtener_rol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rol = "Agente" if query.data == "rol_agente" else "Supervisor"
    context.user_data['rol'] = rol
    await query.edit_message_text(f"✅ Rol seleccionado: {rol}\n\nFinalmente, escribe tu número de teléfono:")
    return TELEFONO

async def obtener_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telefono = update.message.text.strip()
    telegram_id = update.effective_user.id
    if not telefono.isdigit() or len(telefono) < 7:
        await update.message.reply_text("❌ El teléfono debe contener solo números y tener al menos 7 dígitos.\nPor favor, ingresa tu teléfono nuevamente:")
        return TELEFONO
    context.user_data['telefono'] = telefono
    nombre = context.user_data['nombre']
    cedula = context.user_data['cedula']
    rol = context.user_data.get('rol', 'Agente')
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return ConversationHandler.END
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return ConversationHandler.END
    if guardar_en_sheets(worksheet_registros, nombre, cedula, telegram_id, telefono, rol):
        await update.message.reply_text(f"✅ ¡Registro exitoso! 🎉\n\n📋 Datos registrados:\n👤 Nombre: {nombre}\n🆔 Cédula: {cedula}\n🤖 Telegram ID: {telegram_id}\n📱 Teléfono: {telefono}\n🔘 Estado: ACTIVO\n👑 Rol: {rol}\n\nAhora puedes usar los comandos según tu rol.")
    else:
        await update.message.reply_text("❌ Error al guardar tus datos.")
    context.user_data.clear()
    return ConversationHandler.END

async def obtener_llamada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    rol = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol != "Agente":
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return
    registros = obtener_registros_por_telegram_id(worksheet_registros, telegram_id)
    if not registros:
        await update.message.reply_text("❌ No estás registrado en el sistema. Usa /registrar")
        return
    nombre_usuario = obtener_nombre_usuario(worksheet_registros, telegram_id) or f"Usuario {telegram_id}"
    if telegram_id in llamadas_activas.usuarios_activos:
        folio_activo = llamadas_activas.usuarios_activos[telegram_id].get('folio', 'desconocido')
        await update.message.reply_text(f"⚠️ Ya tienes una llamada activa (folio: {folio_activo}).\nCompleta con /completarllamada")
        return
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await update.message.reply_text("❌ Error: No se encontró la hoja 'Data'.")
        return
    fila_num, datos_llamada = obtener_siguiente_llamada_disponible(worksheet_data)
    if not fila_num or not datos_llamada:
        await update.message.reply_text("📭 No hay llamadas disponibles en este momento.")
        return
    hora_asignacion = registrar_hora_asignacion(worksheet_data, fila_num)
    if not hora_asignacion:
        await update.message.reply_text("❌ Error al registrar la hora de asignación.")
        return
    
    # Incrementar contador de intentos
    incrementar_intentos(worksheet_data, fila_num)
    
    if actualizar_estado_llamada(worksheet_data, fila_num, "EN PROCESO"):
        folio_siac = datos_llamada[3] if len(datos_llamada) > 3 else "Sin folio"
        llamadas_activas.usuarios_activos[telegram_id] = {
            'fila': fila_num, 'folio': folio_siac, 'datos': datos_llamada,
            'nombre_usuario': nombre_usuario, 'hora_asignacion': hora_asignacion, 'es_recontacto': False
        }
        if 'llamadas_activas' not in context.bot_data:
            context.bot_data['llamadas_activas'] = {}
        context.bot_data['llamadas_activas'][str(telegram_id)] = llamadas_activas.usuarios_activos[telegram_id]
        mensaje = formatear_datos_llamada(datos_llamada, es_recontacto=False)
        await update.message.reply_text(mensaje)
        try:
            worksheet_data.update_cell(fila_num, 2, nombre_usuario)
            logger.info(f"✅ Validador actualizado: {nombre_usuario} en fila {fila_num}")
        except Exception as e:
            logger.error(f"❌ Error al actualizar validador: {e}")
    else:
        await update.message.reply_text("❌ Error al asignar la llamada.")

async def mis_datos_activos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
            if rol != "Agente":
                await update.message.reply_text("❌ Este comando solo está disponible para Agentes.")
                return
    llamada_activa = None
    if 'llamadas_activas' in context.bot_data and str(telegram_id) in context.bot_data['llamadas_activas']:
        llamada_activa = context.bot_data['llamadas_activas'][str(telegram_id)]
    elif telegram_id in llamadas_activas.usuarios_activos:
        llamada_activa = llamadas_activas.usuarios_activos[telegram_id]
    if not llamada_activa:
        await update.message.reply_text("📭 No tienes ninguna llamada activa.\nUsa /obtener para solicitar una.")
        return
    datos = llamada_activa['datos']
    folio = datos[3] if len(datos) > 3 else "No disponible"
    tipo_venta = datos[4] if len(datos) > 4 else "No disponible"
    nombre_cliente = datos[8] if len(datos) > 8 else "No disponible"
    celular_cliente = datos[9] if len(datos) > 9 else "No disponible"
    correo = datos[11] if len(datos) > 11 and datos[11] else "No disponible"
    direccion = datos[12] if len(datos) > 12 and datos[12] else "No disponible"
    precio_paquete = datos[13] if len(datos) > 13 and datos[13] else "No disponible"
    velocidad = datos[14] if len(datos) > 14 and datos[14] else "No disponible"
    gastos_instalacion = datos[15] if len(datos) > 15 and datos[15] else "No disponible"
    beneficios = datos[16] if len(datos) > 16 and datos[16] else "No disponible"
    hora_asignacion = llamada_activa.get('hora_asignacion', 'No registrada')
    observacion_existente = datos[17] if len(datos) > 17 and datos[17] else None
    try:
        hora_mostrar = datetime.strptime(hora_asignacion, "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S") if hora_asignacion != "No registrada" else hora_asignacion
    except:
        hora_mostrar = hora_asignacion
    mensaje = (f"📞 TU LLAMADA ACTIVA 📞\n\n📋 FOLIO SIAC: {folio}\n🏷️ TIPO DE VENTA: {tipo_venta}\n"
               f"👤 NOMBRE DEL CLIENTE: {nombre_cliente}\n📱 CELULAR CLIENTE: {celular_cliente}\n✉️ CORREO: {correo}\n"
               f"📍 DIRECCIÓN: {direccion}\n💰 PRECIO DEL PAQUETE: {precio_paquete}\n⚡ VELOCIDAD: {velocidad}\n"
               f"🔧 GASTOS DE INSTALACIÓN: {gastos_instalacion}\n🎁 BENEFICIOS: {beneficios}\n🕐 Hora de asignación: {hora_mostrar}\n")
    if observacion_existente:
        mensaje += f"\n📝 OBSERVACIÓN PREVIA:\n{observacion_existente}\n"
    mensaje += "\n✅ Estado: EN PROCESO\n\nCuando termines, usa /completarllamada para finalizar."
    await update.message.reply_text(mensaje)

async def completar_llamada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
            if rol != "Agente":
                await update.message.reply_text("❌ Este comando solo está disponible para Agentes.")
                return
    llamada_activa = None
    if 'llamadas_activas' in context.bot_data and str(telegram_id) in context.bot_data['llamadas_activas']:
        llamada_activa = context.bot_data['llamadas_activas'][str(telegram_id)]
    elif telegram_id in llamadas_activas.usuarios_activos:
        llamada_activa = llamadas_activas.usuarios_activos[telegram_id]
    if not llamada_activa:
        await update.message.reply_text("❌ No tienes ninguna llamada activa.\nUsa /obtener primero.")
        return
    keyboard = [
        [InlineKeyboardButton("✅ Completada", callback_data="resultado_completada"), InlineKeyboardButton("📵 No contesta", callback_data="resultado_no_contesta")],
        [InlineKeyboardButton("❌ Rechazada", callback_data="resultado_rechazada"), InlineKeyboardButton("↩️ Cancelar", callback_data="resultado_cancelar")]
    ]
    context.user_data['llamada_a_completar'] = llamada_activa
    await update.message.reply_text("📝 ¿Cómo resultó la llamada?\n\nSelecciona una opción:", reply_markup=InlineKeyboardMarkup(keyboard))

async def manejar_resultado_llamada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    resultado = query.data.replace("resultado_", "")
    if resultado == "cancelar":
        await query.edit_message_text("✅ Operación cancelada. La llamada sigue activa.")
        return
    llamada_activa = context.user_data.get('llamada_a_completar')
    if not llamada_activa:
        await query.edit_message_text("❌ Error: No se encontró la llamada activa.")
        return
    fila_num = llamada_activa['fila']
    folio = llamada_activa['folio']
    hora_asignacion = llamada_activa.get('hora_asignacion')
    context.user_data['resultado_temp'] = resultado
    context.user_data['fila_temp'] = fila_num
    context.user_data['folio_temp'] = folio
    context.user_data['hora_asignacion_temp'] = hora_asignacion
    context.user_data['llamada_temp'] = llamada_activa
    if resultado == "completada":
        keyboard = [
            [InlineKeyboardButton("✅ Validada", callback_data="validacion_validada"), InlineKeyboardButton("📅 Llamada programada", callback_data="validacion_programada")],
            [InlineKeyboardButton("❌ Servicio Cancelado", callback_data="validacion_cancelado"), InlineKeyboardButton("⏳ Pendiente por el promotor", callback_data="validacion_pendiente")]
        ]
        await query.edit_message_text("📋 Selecciona el estado de validación de la llamada:", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    await query.edit_message_text(f"📝 Resultado seleccionado: {resultado.upper()}\n\nPor favor, escribe una breve observación (opcional).\nPuedes enviar 'skip' para omitir:")

async def manejar_validacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    validacion = query.data.replace("validacion_", "")
    opciones_validacion = {"validada": "Validada", "programada": "Llamada programada", "cancelado": "Servicio Cancelado", "pendiente": "Pendiente por el promotor"}
    estado_validacion = opciones_validacion.get(validacion, validacion)
    context.user_data['validacion_estado'] = estado_validacion
    await query.edit_message_text(f"📝 Estado de validación seleccionado: {estado_validacion}\n\nPor favor, escribe una breve observación (opcional).\nPuedes enviar 'skip' para omitir:")

async def recibir_observacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('nombre') is not None or context.user_data.get('cedula') is not None:
        return
    observacion = update.message.text
    telegram_id = update.effective_user.id
    resultado = context.user_data.get('resultado_temp')
    fila_num = context.user_data.get('fila_temp')
    folio = context.user_data.get('folio_temp')
    hora_asignacion = context.user_data.get('hora_asignacion_temp')
    estado_validacion = context.user_data.get('validacion_estado')
    if not resultado or not fila_num:
        await update.message.reply_text("❌ Error: No se encontraron los datos de la llamada.\nIntenta con /completarllamada")
        return
    if observacion.lower() == 'skip':
        observacion = ""
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await update.message.reply_text("❌ Error al acceder a la hoja Data.")
        return
    if registrar_duracion_y_observacion(worksheet_data, fila_num, resultado, observacion, hora_asignacion, estado_validacion):
        if 'llamadas_activas' in context.bot_data and str(telegram_id) in context.bot_data['llamadas_activas']:
            del context.bot_data['llamadas_activas'][str(telegram_id)]
        if telegram_id in llamadas_activas.usuarios_activos:
            del llamadas_activas.usuarios_activos[telegram_id]
        for key in ['resultado_temp', 'fila_temp', 'folio_temp', 'hora_asignacion_temp', 'llamada_temp', 'validacion_estado', 'llamada_a_completar']:
            context.user_data.pop(key, None)
        msg = f"✅ Llamada finalizada exitosamente\n\n📋 Folio: {folio}\n📊 Resultado: {resultado.upper()}\n"
        if estado_validacion:
            msg += f"🔖 Validación: {estado_validacion}\n"
        msg += f"📝 Observación: {observacion if observacion else 'Sin observación'}\n\nPuedes solicitar una nueva llamada con /obtener"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("❌ Error al registrar el resultado.")

async def mis_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    rol = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol != "Agente":
        await update.message.reply_text("❌ Este comando solo está disponible para Agentes.")
        return
    nombre_agente = obtener_nombre_usuario(worksheet_registros, telegram_id)
    if not nombre_agente:
        await update.message.reply_text("❌ No se encontró tu información de registro.")
        return
    if telegram_id in llamadas_activas.usuarios_activos:
        folio_activo = llamadas_activas.usuarios_activos[telegram_id].get('folio', 'desconocido')
        await update.message.reply_text(f"⚠️ Tienes una llamada activa (folio: {folio_activo}).\nCompleta con /completarllamada")
        return
    pendientes_agente = []
    if 'recontactos_pendientes' in context.bot_data:
        for llamada_id, llamada in context.bot_data['recontactos_pendientes'].items():
            if llamada.get('validador_original') == nombre_agente:
                pendientes_agente.append({'id': llamada_id, 'folio': llamada['folio'], 'nombre_cliente': llamada['nombre_cliente'], 'celular_cliente': llamada['celular_cliente'], 'hora_asignacion': llamada.get('hora_asignacion', 'No registrada'), 'datos': llamada['datos']})
    if not pendientes_agente:
        await update.message.reply_text("📭 No tienes llamadas pendientes de recontacto.\nUsa /obtener para tomar llamadas disponibles.")
        return
    mensaje = f"📋 *TUS RECONTACTOS PENDIENTES* 📋\n\nTienes {len(pendientes_agente)} llamada(s) pendiente(s) para recontactar.\n\n"
    keyboard = []
    for idx, pendiente in enumerate(pendientes_agente, 1):
        try:
            hora_mostrar = datetime.strptime(pendiente['hora_asignacion'], "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M")
        except:
            hora_mostrar = pendiente['hora_asignacion']
        mensaje += f"*{idx}.* 📱 `{pendiente['celular_cliente']}`\n   👤 {pendiente['nombre_cliente']}\n   📋 Folio: {pendiente['folio']}\n   🕐 Asignado: {hora_mostrar}\n\n"
        keyboard.append([InlineKeyboardButton(f"📞 Tomar llamada {idx} - {pendiente['nombre_cliente'][:20]}", callback_data=f"tomar_recontacto_{pendiente['id']}")])
    keyboard.append([InlineKeyboardButton("❌ Cerrar", callback_data="cerrar_mispendientes")])
    await update.message.reply_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def revisar_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    rol = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol != "Supervisor":
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await update.message.reply_text("❌ Error al acceder a la hoja Data.")
        return
    pendientes = obtener_llamadas_pendientes(worksheet_data)
    if not pendientes:
        await update.message.reply_text("📭 No hay llamadas pendientes (NO CONTESTA) en este momento.")
        return
    if 'pendientes_notificacion' not in context.bot_data:
        context.bot_data['pendientes_notificacion'] = {}
    mensaje = "📋 *LLAMADAS PENDIENTES (NO CONTESTA)* 📋\n\n"
    keyboard = []
    for idx, llamada in enumerate(pendientes, 1):
        clave = f"pendiente_{idx}"
        context.bot_data['pendientes_notificacion'][clave] = llamada
        try:
            hora_mostrar = datetime.strptime(llamada['hora_asignacion'], "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M")
        except:
            hora_mostrar = "No registrada"
        mensaje += f"*{idx}.* 📱 `{llamada['celular_cliente']}`\n   👤 {llamada['nombre_cliente']}\n   🕐 Llamada: {hora_mostrar}\n   👔 Agente: {llamada['validador']}\n   📋 Folio: {llamada['folio']}\n\n"
        keyboard.append([InlineKeyboardButton(f"📢 Notificar a {llamada['validador']}", callback_data=f"notificar_{idx}")])
    keyboard.append([InlineKeyboardButton("❌ Cerrar", callback_data="cerrar_pendientes")])
    await update.message.reply_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def manejar_notificacion_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    callback_data = query.data
    if callback_data == "cerrar_pendientes":
        await query.edit_message_text("✅ Listado de pendientes cerrado.")
        return
    if callback_data == "cerrar_notificacion":
        await query.delete_message()
        return
    if not callback_data.startswith("notificar_"):
        return
    try:
        idx = int(callback_data.replace("notificar_", ""))
        clave = f"pendiente_{idx}"
        sheet = conectar_google_sheets()
        if not sheet:
            await query.edit_message_text("❌ Error al conectar con la base de datos.")
            return
        worksheet_registros = obtener_hoja_registros(sheet)
        if not worksheet_registros:
            await query.edit_message_text("❌ Error al acceder a la hoja de registros.")
            return
        rol = obtener_rol_usuario(worksheet_registros, telegram_id)
        if rol != "Supervisor":
            await query.edit_message_text("❌ No tienes permisos para realizar esta acción.")
            return
        if 'pendientes_notificacion' not in context.bot_data or clave not in context.bot_data['pendientes_notificacion']:
            await query.edit_message_text("❌ Esta llamada ya no está disponible.\nUsa /revisarpendientes para actualizar.")
            return
        llamada = context.bot_data['pendientes_notificacion'][clave]
        agente_telegram_id = obtener_agente_por_nombre(worksheet_registros, llamada['validador'])
        if not agente_telegram_id:
            await query.edit_message_text(f"❌ No se pudo encontrar al agente {llamada['validador']} en el sistema.")
            return
        worksheet_data = obtener_hoja_data(sheet)
        if worksheet_data:
            try:
                worksheet_data.update_cell(llamada['fila'], 22, llamada['validador'])  # Columna V
                logger.info(f"✅ Agente asignado guardado: {llamada['validador']} en fila {llamada['fila']}")
            except Exception as e:
                logger.error(f"Error al guardar agente asignado: {e}")
        llamada_id = f"recontacto_{llamada['fila']}_{llamada['folio']}"
        if 'recontactos_pendientes' not in context.bot_data:
            context.bot_data['recontactos_pendientes'] = {}
        context.bot_data['recontactos_pendientes'][llamada_id] = {
            'fila': llamada['fila'], 'folio': llamada['folio'], 'datos': llamada['datos_completos'],
            'nombre_cliente': llamada['nombre_cliente'], 'celular_cliente': llamada['celular_cliente'],
            'validador_original': llamada['validador'], 'hora_asignacion': llamada['hora_asignacion'],
            'agente_telegram_id': agente_telegram_id
        }
        if 'pendientes_notificacion' in context.bot_data and clave in context.bot_data['pendientes_notificacion']:
            del context.bot_data['pendientes_notificacion'][clave]
        nombre_supervisor = query.from_user.first_name
        if query.from_user.last_name:
            nombre_supervisor += f" {query.from_user.last_name}"
        keyboard = [[InlineKeyboardButton("📞 Tomar llamada pendiente", callback_data=f"tomar_recontacto_{llamada_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        mensaje_notificacion = (f"🔔 NOTIFICACIÓN DE RECONTACTO 🔔\n\nEl supervisor {nombre_supervisor} te ha solicitado recontactar al siguiente cliente:\n\n"
                                f"📱 Teléfono: {llamada['celular_cliente']}\n👤 Cliente: {llamada['nombre_cliente']}\n📋 Folio SIAC: {llamada['folio']}\n"
                                f"🕐 Llamada anterior: {llamada['hora_asignacion']}\n\nHaz clic en el botón para tomar esta llamada.\n\nTambién puedes ver todas tus llamadas pendientes con /mispendientes")
        try:
            await context.bot.send_message(chat_id=int(agente_telegram_id), text=mensaje_notificacion, reply_markup=reply_markup)
            mensaje_exito = (f"✅ Notificación enviada al agente {llamada['validador']}\n\n📱 Cliente: {llamada['celular_cliente']}\n👤 Nombre: {llamada['nombre_cliente']}\n"
                             f"📋 Folio: {llamada['folio']}\n\nLa llamada ha sido eliminada da la lista de pendientes.\nEl agente recibió un botón para tomar la llamada.\n"
                             f"También puede usar /mispendientes para ver todas sus llamadas pendientes.")
            close_keyboard = [[InlineKeyboardButton("❌ Cerrar", callback_data="cerrar_notificacion")]]
            await query.edit_message_text(mensaje_exito, reply_markup=InlineKeyboardMarkup(close_keyboard))
        except Exception as e:
            await query.edit_message_text(f"❌ Error al enviar notificación\n\nDetalle: {e}\n\nVerifica que el agente {llamada['validador']} tenga el bot iniciado.\n\nLa llamada permanece en la lista de pendientes.")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al procesar la notificación: {e}")

async def tomar_recontacto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    callback_data = query.data
    if callback_data == "cerrar_mispendientes":
        await query.delete_message()
        return
    if not callback_data.startswith("tomar_recontacto_"):
        return
    llamada_id = callback_data.replace("tomar_recontacto_", "")
    sheet = conectar_google_sheets()
    if not sheet:
        await query.edit_message_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await query.edit_message_text("❌ Error al acceder a la hoja de registros.")
        return
    rol = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol != "Agente":
        await query.edit_message_text("❌ No tienes permisos para realizar esta acción.")
        return
    registros = obtener_registros_por_telegram_id(worksheet_registros, telegram_id)
    if not registros:
        await query.edit_message_text("❌ No estás registrado en el sistema.\nPor favor, regístrate con /registrar")
        return
    if telegram_id in llamadas_activas.usuarios_activos:
        folio_activo = llamadas_activas.usuarios_activos[telegram_id].get('folio', 'desconocido')
        await query.edit_message_text(f"⚠️ Ya tienes una llamada activa (folio: {folio_activo}).\nCompleta con /completarllamada")
        return
    if 'recontactos_pendientes' not in context.bot_data or llamada_id not in context.bot_data['recontactos_pendientes']:
        await query.edit_message_text("❌ Esta llamada ya no está disponible.\nUsa /mispendientes para actualizar tu lista.")
        return
    llamada = context.bot_data['recontactos_pendientes'][llamada_id]
    nombre_agente = obtener_nombre_usuario(worksheet_registros, telegram_id)
    if llamada['validador_original'] != nombre_agente:
        await query.edit_message_text(f"❌ Esta llamada no te fue asignada.\nFue asignada originalmente a {llamada['validador_original']}.")
        return
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await query.edit_message_text("❌ Error: No se encontró la hoja 'Data'.")
        return
    try:
        fila_num = llamada['fila']
        estado_actual = worksheet_data.cell(fila_num, 21).value  # Columna U
        if estado_actual != "NO CONTESTA":
            await query.edit_message_text(f"❌ La llamada ya no está disponible. Estado actual: {estado_actual}\nUsa /obtener para una nueva.")
            if llamada_id in context.bot_data['recontactos_pendientes']:
                del context.bot_data['recontactos_pendientes'][llamada_id]
            return
        datos_llamada = worksheet_data.row_values(fila_num)
        hora_asignacion = registrar_hora_asignacion(worksheet_data, fila_num)
        if not hora_asignacion:
            await query.edit_message_text("❌ Error al registrar la hora de asignación.")
            return
        
        # Incrementar contador de intentos
        incrementar_intentos(worksheet_data, fila_num)
        
        if actualizar_estado_llamada(worksheet_data, fila_num, "EN PROCESO"):
            try:
                worksheet_data.update_cell(fila_num, 22, "")  # Limpiar AGENTE_ASIGNADO
                logger.info(f"✅ Agente asignado limpiado en fila {fila_num}")
            except Exception as e:
                logger.error(f"Error al limpiar agente asignado: {e}")
            if 'pendientes_notificacion' in context.bot_data:
                claves_a_eliminar = [clave for clave, pend in context.bot_data['pendientes_notificacion'].items() if pend.get('fila') == fila_num or pend.get('folio') == llamada['folio']]
                for clave in claves_a_eliminar:
                    del context.bot_data['pendientes_notificacion'][clave]
            llamadas_activas.usuarios_activos[telegram_id] = {
                'fila': fila_num, 'folio': llamada['folio'], 'datos': datos_llamada,
                'nombre_usuario': nombre_agente, 'hora_asignacion': hora_asignacion, 'es_recontacto': True
            }
            if 'llamadas_activas' not in context.bot_data:
                context.bot_data['llamadas_activas'] = {}
            context.bot_data['llamadas_activas'][str(telegram_id)] = llamadas_activas.usuarios_activos[telegram_id]
            try:
                worksheet_data.update_cell(fila_num, 2, nombre_agente)
                logger.info(f"✅ Validador actualizado: {nombre_agente} en fila {fila_num}")
            except Exception as e:
                logger.error(f"Error al actualizar validador: {e}")
            if llamada_id in context.bot_data['recontactos_pendientes']:
                del context.bot_data['recontactos_pendientes'][llamada_id]
            mensaje = formatear_datos_llamada(datos_llamada, es_recontacto=True)
            await query.edit_message_text(f"✅ Llamada tomada exitosamente\n\n📋 Folio: {llamada['folio']}\n👤 Cliente: {llamada['nombre_cliente']}\n\nLos detalles se han enviado a continuación.")
            await query.message.reply_text(mensaje)
        else:
            await query.edit_message_text("❌ Error al asignar la llamada.")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al procesar el recontacto: {e}")

async def notificar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    rol = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol != "Supervisor":
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ Ejemplo: /notificar 1\nUsa /revisarpendientes para ver la lista.")
        return
    try:
        idx = int(args[0])
        clave = f"pendiente_{idx}"
        if clave not in llamadas_activas.pendientes_notificacion:
            await update.message.reply_text("❌ Número de llamada no válido.\nUsa /revisarpendientes para ver la lista actualizada.")
            return
        llamada = llamadas_activas.pendientes_notificacion[clave]
        agente_telegram_id = obtener_agente_por_nombre(worksheet_registros, llamada['validador'])
        if not agente_telegram_id:
            await update.message.reply_text(f"❌ No se pudo encontrar al agente {llamada['validador']} en el sistema.")
            return
        mensaje = (f"🔔 **NOTIFICACIÓN DE RECONTACTO** 🔔\n\nEl supervisor te ha solicitado recontactar al siguiente cliente:\n\n"
                   f"📱 **Teléfono:** {llamada['celular_cliente']}\n👤 **Cliente:** {llamada['nombre_cliente']}\n"
                   f"📋 **Folio SIAC:** {llamada['folio']}\n🕐 **Llamada anterior:** {llamada['hora_asignacion']}\n\n"
                   f"Por favor, realiza el recontacto lo antes posible.")
        try:
            await context.bot.send_message(chat_id=int(agente_telegram_id), text=mensaje, parse_mode='Markdown')
            await update.message.reply_text(f"✅ Notificación enviada al agente {llamada['validador']}\n📱 Cliente: {llamada['celular_cliente']}\n👤 Nombre: {llamada['nombre_cliente']}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error al enviar notificación: {e}\nVerifica que el agente tenga el bot iniciado.")
    except ValueError:
        await update.message.reply_text("❌ El número debe ser un valor numérico.\nEjemplo: /notificar 1")

async def miestado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    registros = obtener_registros_por_telegram_id(worksheet_registros, telegram_id)
    if not registros:
        await update.message.reply_text(f"❌ No estás registrado en el sistema.\nTu Telegram ID es: {telegram_id}\nUsa /registrar")
        return
    ultimo = registros[-1]
    fecha, nombre, cedula, telefono, estado, rol = ultimo[0], ultimo[1], ultimo[2], ultimo[4], ultimo[5], ultimo[6] if len(ultimo) > 6 else "Agente"
    estado_emoji = {"ACTIVO": "🟢", "INACTIVO": "⚪", "INHABILITADO": "🔴"}.get(estado, "⚪")
    await update.message.reply_text(f"📋 Tu información de registro:\n\n📅 Fecha registro: {fecha}\n👤 Nombre: {nombre}\n🆔 Cédula: {cedula}\n🤖 Telegram ID: {telegram_id}\n📱 Teléfono: {telefono}\n{estado_emoji} Estado: {estado}\n👑 Rol: {rol}")

async def cambiarrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    if obtener_rol_usuario(worksheet_registros, telegram_id) != "Supervisor":
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("📝 Formato: /cambiarrol [cédula] [nuevo_rol]\nRoles: Agente, Supervisor\nEjemplo: /cambiarrol 12345678 Supervisor", parse_mode='Markdown')
        return
    cedula, nuevo_rol = args[0], args[1].capitalize()
    if nuevo_rol not in ["Agente", "Supervisor"]:
        await update.message.reply_text(f"❌ Rol '{nuevo_rol}' no válido.")
        return
    try:
        todos = worksheet_registros.get_all_values()
        fila = None
        datos = None
        for i, fil in enumerate(todos[1:], start=2):
            if len(fil) > 2 and fil[2] == cedula:
                fila, datos = i, fil
                break
        if not fila:
            await update.message.reply_text(f"❌ No se encontró un usuario con la cédula: {cedula}")
            return
        nombre_actual = datos[1] if len(datos) > 1 else "Desconocido"
        rol_actual = datos[6] if len(datos) > 6 else "Agente"
        worksheet_registros.update_cell(fila, 7, nuevo_rol)
        await update.message.reply_text(f"✅ *Rol actualizado*\n👤 Usuario: {nombre_actual}\n🆔 Cédula: {cedula}\n🔄 Rol anterior: {rol_actual}\n✨ Nuevo rol: {nuevo_rol}", parse_mode='Markdown')
        logger.info(f"✅ Supervisor {telegram_id} cambió rol de {nombre_actual} ({cedula}) de {rol_actual} a {nuevo_rol}")
    except Exception as e:
        logger.error(f"Error al cambiar rol: {e}")
        await update.message.reply_text(f"❌ Error al cambiar el rol: {e}")

async def cambiarestado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    if obtener_rol_usuario(worksheet_registros, telegram_id) != "Supervisor":
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("📝 Formato: /cambiarestado [cédula] [nuevo_estado]\nEstados: ACTIVO, INACTIVO, INHABILITADO\nEjemplo: /cambiarestado 12345678 INACTIVO", parse_mode='Markdown')
        return
    cedula, nuevo_estado = args[0], args[1].upper()
    if nuevo_estado not in ["ACTIVO", "INACTIVO", "INHABILITADO"]:
        await update.message.reply_text(f"❌ Estado '{nuevo_estado}' no válido.")
        return
    try:
        todos = worksheet_registros.get_all_values()
        fila = None
        datos = None
        for i, fil in enumerate(todos[1:], start=2):
            if len(fil) > 2 and fil[2] == cedula:
                fila, datos = i, fil
                break
        if not fila:
            await update.message.reply_text(f"❌ No se encontró un usuario con la cédula: {cedula}")
            return
        nombre_actual = datos[1] if len(datos) > 1 else "Desconocido"
        estado_actual = datos[5] if len(datos) > 5 else "ACTIVO"
        rol_usuario = datos[6] if len(datos) > 6 else "Agente"
        worksheet_registros.update_cell(fila, 6, nuevo_estado)
        emoji = {"ACTIVO": "🟢", "INACTIVO": "⚪", "INHABILITADO": "🔴"}.get(nuevo_estado, "⚪")
        await update.message.reply_text(f"✅ *Estado actualizado*\n👤 Usuario: {nombre_actual}\n🆔 Cédula: {cedula}\n👑 Rol: {rol_usuario}\n🔄 Estado anterior: {estado_actual}\n✨ Nuevo estado: {emoji} {nuevo_estado}", parse_mode='Markdown')
        logger.info(f"✅ Supervisor {telegram_id} cambió estado de {nombre_actual} ({cedula}) de {estado_actual} a {nuevo_estado}")
    except Exception as e:
        logger.error(f"Error al cambiar estado: {e}")
        await update.message.reply_text(f"❌ Error al cambiar el estado: {e}")

# ========== COMANDO GUARDAR SIAC ==========
async def guardar_siac_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return ConversationHandler.END
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return ConversationHandler.END
    if obtener_rol_usuario(worksheet_registros, telegram_id) != "Supervisor":
        await update.message.reply_text("❌ No tienes permisos para usar este comando.\nSolo Supervisores.")
        return ConversationHandler.END
    await update.message.reply_text("📝 *ASIGNAR FOLIOS SIAC* 📝\n\nIngresa el código de *estrategia* para buscar llamadas sin folio:\nEjemplo: `10001009`", parse_mode='Markdown')
    return ESTRATEGIA

async def guardar_siac_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    estrategia = update.message.text.strip()
    telegram_id = update.effective_user.id
    if not estrategia.replace('.', '').isdigit():
        await update.message.reply_text("❌ El código debe ser numérico. Intenta de nuevo:")
        return ESTRATEGIA
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return ConversationHandler.END
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await update.message.reply_text("❌ Error al acceder a la hoja Data.")
        return ConversationHandler.END
    todas = worksheet_data.get_all_values()
    resultados = []
    for i, fila in enumerate(todas[1:], start=2):
        if len(fila) > 5:
            est_fila = fila[5] if len(fila) > 5 else ""
            folio_fila = fila[3] if len(fila) > 3 else ""
            if est_fila == estrategia and (folio_fila == "" or folio_fila == "No disponible"):
                resultados.append({
                    'fila': i,
                    'nombre': fila[8] if len(fila) > 8 else "Sin nombre",
                    'telefono': fila[9] if len(fila) > 9 else "Sin teléfono",
                    'gerente': fila[6] if len(fila) > 6 else "Sin gerente",
                    'datos_completos': fila
                })
    if not resultados:
        await update.message.reply_text(f"📭 No se encontraron llamadas sin folio para la estrategia `{estrategia}`.", parse_mode='Markdown')
        return ConversationHandler.END
    busqueda_siac[telegram_id] = {'estrategia': estrategia, 'resultados': resultados, 'pagina': 0}
    await mostrar_pagina_siac(update, context, telegram_id)
    return SELECCION_CLIENTE

async def mostrar_pagina_siac(update: Update, context: ContextTypes.DEFAULT_TYPE, telegram_id: int, pagina: int = 0):
    data = busqueda_siac.get(telegram_id)
    if not data:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ Sesión expirada. Inicia nuevamente con /guardarSIAC.")
        else:
            await update.message.reply_text("❌ Sesión expirada. Inicia nuevamente con /guardarSIAC.")
        return ConversationHandler.END
    resultados = data['resultados']
    total = len(resultados)
    items_por_pagina = 10
    inicio = pagina * items_por_pagina
    fin = min(inicio + items_por_pagina, total)
    total_paginas = (total + items_por_pagina - 1) // items_por_pagina
    gerente = resultados[0]['gerente'] if resultados else "N/A"
    mensaje = f"📋 *LLAMADAS SIN FOLIO - ESTRATEGIA {data['estrategia']}*\n\nGerente: *{gerente}*\n\nMostrando {inicio+1} a {fin} de {total} (Pág. {pagina+1}/{total_paginas})\n\n"
    keyboard = []
    for idx in range(inicio, fin):
        res = resultados[idx]
        texto = f"{res['nombre'][:25]} - {res['telefono']}"
        keyboard.append([InlineKeyboardButton(texto, callback_data=f"siac_seleccionar_{idx}")])
    nav = []
    if pagina > 0:
        nav.append(InlineKeyboardButton("◀ Anterior", callback_data="siac_pagina_anterior"))
    if pagina + 1 < total_paginas:
        nav.append(InlineKeyboardButton("Siguiente ▶", callback_data="siac_pagina_siguiente"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="siac_cancelar")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query is not None:
        await update.callback_query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        await update.message.reply_text(mensaje, parse_mode='Markdown', reply_markup=reply_markup)

async def manejar_callback_siac(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    data = query.data
    if data == "siac_cancelar":
        await query.edit_message_text("✅ Operación cancelada.")
        if telegram_id in busqueda_siac:
            del busqueda_siac[telegram_id]
        return ConversationHandler.END
    if data.startswith("siac_pagina_"):
        direccion = data.replace("siac_pagina_", "")
        info = busqueda_siac.get(telegram_id)
        if not info:
            await query.edit_message_text("❌ Sesión expirada.")
            return ConversationHandler.END
        nueva = info['pagina']
        if direccion == "anterior" and nueva > 0:
            nueva -= 1
        elif direccion == "siguiente":
            nueva += 1
        else:
            return
        info['pagina'] = nueva
        await mostrar_pagina_siac(query, context, telegram_id, nueva)
        return
    if data.startswith("siac_seleccionar_"):
        idx = int(data.replace("siac_seleccionar_", ""))
        info = busqueda_siac.get(telegram_id)
        if not info or idx >= len(info['resultados']):
            await query.edit_message_text("❌ El cliente seleccionado ya no está disponible.")
            return ConversationHandler.END
        cliente = info['resultados'][idx]
        context.user_data['siac_cliente'] = cliente
        context.user_data['siac_resultados'] = info['resultados']
        context.user_data['siac_pagina'] = info['pagina']
        await query.edit_message_text(f"✏️ *Asignar Folio SIAC*\n\nCliente: {cliente['nombre']}\nTeléfono: {cliente['telefono']}\nGerente: {cliente['gerente']}\n\nIngresa el número de *folio SIAC* (solo números):", parse_mode='Markdown')
        return INGRESO_FOLIO

async def guardar_siac_folio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folio = update.message.text.strip()
    telegram_id = update.effective_user.id
    cliente = context.user_data.get('siac_cliente')
    resultados = context.user_data.get('siac_resultados')
    pagina = context.user_data.get('siac_pagina', 0)
    if not cliente:
        await update.message.reply_text("❌ Sesión expirada. Inicia nuevamente con /guardarSIAC.")
        return ConversationHandler.END
    if not folio.isdigit():
        await update.message.reply_text("❌ El folio debe contener solo números. Intenta de nuevo:")
        return INGRESO_FOLIO
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return ConversationHandler.END
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await update.message.reply_text("❌ Error al acceder a la hoja Data.")
        return ConversationHandler.END
    try:
        worksheet_data.update_cell(cliente['fila'], 4, folio)
        await update.message.reply_text(f"✅ *Folio SIAC asignado correctamente*\n\nCliente: {cliente['nombre']}\nFolio: {folio}\n\nLa llamada ha sido actualizada.")
        # Limpiar datos temporales
        context.user_data.clear()
        # Eliminar este cliente de la lista de resultados
        if telegram_id in busqueda_siac:
            nuevos_resultados = [r for r in busqueda_siac[telegram_id]['resultados'] if r['fila'] != cliente['fila']]
            if nuevos_resultados:
                busqueda_siac[telegram_id]['resultados'] = nuevos_resultados
                await mostrar_pagina_siac(update, context, telegram_id, pagina)
                return SELECCION_CLIENTE
            else:
                await update.message.reply_text("📭 No quedan llamadas pendientes para esta estrategia.")
                if telegram_id in busqueda_siac:
                    del busqueda_siac[telegram_id]
                return ConversationHandler.END
        else:
            return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error al guardar folio: {e}")
        await update.message.reply_text(f"❌ Error al guardar: {e}")
        return ConversationHandler.END

async def guardar_siac_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if telegram_id in busqueda_siac:
        del busqueda_siac[telegram_id]
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END

# ========== FUNCIONES PARA PDF ==========
async def iniciar_creacion_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            if not obtener_registros_por_telegram_id(worksheet_registros, telegram_id):
                await update.message.reply_text("❌ No estás registrado. Usa /registrar")
                return
    imagenes_para_pdf[telegram_id] = []
    await update.message.reply_text("📸 *CREACIÓN DE PDF - INSTRUCCIONES* 📸\n\n1. Envía las imágenes (una por una)\n2. Cuando termines, usa /generarpdf\n3. Para cancelar, usa /cancelarpdf\n\nPuedes enviar hasta 20 imágenes.\n\n✅ *Listo para recibir imágenes* - Envía la primera imagen:", parse_mode='Markdown')

async def recibir_imagen_para_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if telegram_id not in imagenes_para_pdf:
        return
    if not update.message.photo:
        await update.message.reply_text("❌ Por favor, envía una imagen (foto).")
        return
    photo = update.message.photo[-1]
    if len(imagenes_para_pdf[telegram_id]) >= 20:
        await update.message.reply_text("⚠️ Límite de 20 imágenes alcanzado.\nUsa /generarpdf para crear el PDF.")
        return
    try:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        path = temp.name
        temp.close()
        file = await context.bot.get_file(photo.file_id)
        await file.download_to_drive(path)
        imagenes_para_pdf[telegram_id].append(path)
        count = len(imagenes_para_pdf[telegram_id])
        await update.message.reply_text(f"✅ Imagen {count} recibida.\nEnvía más o usa /generarpdf.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def generar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            if not obtener_registros_por_telegram_id(worksheet_registros, telegram_id):
                await update.message.reply_text("❌ No estás registrado. Usa /registrar")
                return
    if telegram_id not in imagenes_para_pdf or not imagenes_para_pdf[telegram_id]:
        await update.message.reply_text("❌ No tienes imágenes guardadas.\nUsa /crearpdf para comenzar.")
        return
    imagenes = imagenes_para_pdf[telegram_id]
    await update.message.reply_text(f"📄 Generando PDF con {len(imagenes)} imágenes...\nPor favor espera.")
    try:
        pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        pdf_path.close()
        with open(pdf_path.name, "wb") as f:
            f.write(img2pdf.convert(imagenes))
        with open(pdf_path.name, 'rb') as f:
            await update.message.reply_document(document=f, filename=f"documento_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                                                caption=f"✅ PDF generado con {len(imagenes)} imágenes.\nFecha: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        for img in imagenes:
            try: os.unlink(img)
            except: pass
        os.unlink(pdf_path.name)
        del imagenes_para_pdf[telegram_id]
    except Exception as e:
        await update.message.reply_text(f"❌ Error al generar el PDF: {e}")
        for img in imagenes:
            try: os.unlink(img)
            except: pass
        del imagenes_para_pdf[telegram_id]

async def cancelar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            if not obtener_registros_por_telegram_id(worksheet_registros, telegram_id):
                await update.message.reply_text("❌ No estás registrado. Usa /registrar")
                return
    if telegram_id in imagenes_para_pdf:
        for img in imagenes_para_pdf[telegram_id]:
            try: os.unlink(img)
            except: pass
        del imagenes_para_pdf[telegram_id]
        await update.message.reply_text("❌ Creación de PDF cancelada.\nLas imágenes han sido eliminadas.")
    else:
        await update.message.reply_text("❌ No tienes una sesión de creación de PDF activa.\nUsa /crearpdf para comenzar.")

async def cargar_estructuras_desde_sheets(worksheet_data, worksheet_registros, context):
    try:
        llamadas_activas.usuarios_activos = {}
        llamadas_activas.pendientes_notificacion = {}
        llamadas_activas.recontactos_pendientes = {}
        for d in [context.bot_data.get(k, {}) for k in ['llamadas_activas', 'pendientes_notificacion', 'recontactos_pendientes']]:
            d.clear()
        todas = worksheet_data.get_all_values()
        for i, fila in enumerate(todas[1:], start=2):
            if len(fila) >= 21:
                estado2 = fila[20] if len(fila) > 20 else ""
                validador = fila[1] if len(fila) > 1 else ""
                if estado2 == "EN PROCESO" and validador:
                    telegram_id = None
                    for reg in worksheet_registros.get_all_values()[1:]:
                        if len(reg) > 1 and reg[1] == validador and len(reg) > 3:
                            telegram_id = reg[3]
                            break
                    if telegram_id:
                        llamadas_activas.usuarios_activos[telegram_id] = {
                            'fila': i, 'folio': fila[3] if len(fila) > 3 else "Sin folio", 'datos': fila,
                            'nombre_usuario': validador, 'hora_asignacion': fila[18] if len(fila) > 18 else "No registrada",
                            'es_recontacto': False
                        }
                        context.bot_data['llamadas_activas'][str(telegram_id)] = llamadas_activas.usuarios_activos[telegram_id]
        pend = obtener_llamadas_pendientes(worksheet_data)
        for idx, p in enumerate(pend, 1):
            clave = f"pendiente_{idx}"
            llamadas_activas.pendientes_notificacion[clave] = p
            context.bot_data['pendientes_notificacion'][clave] = p
        for i, fila in enumerate(todas[1:], start=2):
            if len(fila) >= 22:
                estado2 = fila[20] if len(fila) > 20 else ""
                agente_asignado = fila[21] if len(fila) > 21 else ""
                if estado2 == "NO CONTESTA" and agente_asignado and agente_asignado != "":
                    telegram_id = None
                    for reg in worksheet_registros.get_all_values()[1:]:
                        if len(reg) > 1 and reg[1] == agente_asignado and len(reg) > 3:
                            telegram_id = reg[3]
                            break
                    lid = f"recontacto_{i}_{fila[3] if len(fila) > 3 else 'sin_folio'}"
                    llamadas_activas.recontactos_pendientes[lid] = {
                        'fila': i, 'folio': fila[3] if len(fila) > 3 else "No disponible", 'datos': fila,
                        'nombre_cliente': fila[8] if len(fila) > 8 else "No disponible",
                        'celular_cliente': fila[9] if len(fila) > 9 else "No disponible",
                        'validador_original': agente_asignado, 'hora_asignacion': fila[18] if len(fila) > 18 else "No registrada",
                        'agente_telegram_id': telegram_id
                    }
                    context.bot_data['recontactos_pendientes'][lid] = llamadas_activas.recontactos_pendientes[lid]
        return len(llamadas_activas.usuarios_activos), len(llamadas_activas.pendientes_notificacion), len(llamadas_activas.recontactos_pendientes)
    except Exception as e:
        logger.error(f"Error al cargar estructuras: {e}")
        return None, None, None

async def recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    if obtener_rol_usuario(worksheet_registros, telegram_id) != "Supervisor":
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return
    msg = await update.message.reply_text("🔄 *Recargando estructuras de datos...*\nPor favor espera.", parse_mode='Markdown')
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await msg.edit_text("❌ Error al acceder a la hoja Data.")
        return
    activas, pendientes, recontactos = await cargar_estructuras_desde_sheets(worksheet_data, worksheet_registros, context)
    if activas is not None:
        await msg.edit_text(f"✅ *Estructuras recargadas*\n\n📊 Resumen:\n• Activas (EN PROCESO): {activas}\n• Pendientes de notificar: {pendientes}\n• Recontactos asignados: {recontactos}", parse_mode='Markdown')
        logger.info(f"✅ Supervisor {telegram_id} recargó estructuras: {activas} activas, {pendientes} pendientes, {recontactos} recontactos")
    else:
        await msg.edit_text("❌ *Error al recargar estructuras*", parse_mode='Markdown')

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Registro cancelado.\nPuedes iniciar nuevamente con /registrar")
    context.user_data.clear()
    return ConversationHandler.END

# ========== CONFIGURACIÓN Y EJECUCIÓN ==========
def main():
    logger.info("🤖 Iniciando bot...")
    logger.info(f"📊 Usando Google Sheets ID: {SPREADSHEET_ID}")
    sheet = conectar_google_sheets()
    if not sheet:
        logger.error("❌ No se pudo conectar a Google Sheets. Verifica las credenciales.")
    application = Application.builder().token(TOKEN).build()
    # Comandos básicos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ayuda", ayuda))
    application.add_handler(CommandHandler("miestado", miestado))
    application.add_handler(CommandHandler("obtener", obtener_llamada))
    application.add_handler(CommandHandler("misdatos", mis_datos_activos))
    application.add_handler(CommandHandler("completarllamada", completar_llamada))
    application.add_handler(CommandHandler("revisarpendientes", revisar_pendientes))
    application.add_handler(CommandHandler("mispendientes", mis_pendientes))
    application.add_handler(CommandHandler("notificar", notificar))
    application.add_handler(CommandHandler("cambiarrol", cambiarrol))
    application.add_handler(CommandHandler("cambiarestado", cambiarestado))
    application.add_handler(CommandHandler("recargar", recargar))
    # Comandos PDF
    application.add_handler(CommandHandler("crearpdf", iniciar_creacion_pdf))
    application.add_handler(CommandHandler("generarpdf", generar_pdf))
    application.add_handler(CommandHandler("cancelarpdf", cancelar_pdf))
    # Comando guardar SIAC
    guardar_siac_conv = ConversationHandler(
        entry_points=[CommandHandler("guardarSIAC", guardar_siac_inicio)],
        states={
            ESTRATEGIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_siac_buscar)],
            SELECCION_CLIENTE: [CallbackQueryHandler(manejar_callback_siac, pattern="^siac_")],
            INGRESO_FOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_siac_folio)],
        },
        fallbacks=[CommandHandler("cancelar", guardar_siac_cancelar)],
    )
    application.add_handler(guardar_siac_conv)
    # Manejadores de mensajes y callbacks
    application.add_handler(MessageHandler(filters.PHOTO, recibir_imagen_para_pdf))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_observacion))
    application.add_handler(CallbackQueryHandler(manejar_resultado_llamada, pattern="^resultado_"))
    application.add_handler(CallbackQueryHandler(manejar_validacion, pattern="^validacion_"))
    application.add_handler(CallbackQueryHandler(obtener_rol, pattern="^rol_"))
    application.add_handler(CallbackQueryHandler(manejar_notificacion_boton, pattern="^(notificar_|cerrar_pendientes|cerrar_notificacion)"))
    application.add_handler(CallbackQueryHandler(tomar_recontacto, pattern="^(tomar_recontacto_|cerrar_mispendientes)"))
    # Conversación registro
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("registrar", registrar)],
        states={
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, obtener_nombre)],
            CEDULA: [MessageHandler(filters.TEXT & ~filters.COMMAND, obtener_cedula)],
            ROL: [CallbackQueryHandler(obtener_rol, pattern="^rol_")],
            TELEFONO: [MessageHandler(filters.TEXT & ~filters.COMMAND, obtener_telefono)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )
    application.add_handler(conv_handler)
    # Inicializar almacenamiento
    for key in ['llamadas_activas', 'pendientes_notificacion', 'recontactos_pendientes']:
        if key not in application.bot_data:
            application.bot_data[key] = {}
    # Cargar datos iniciales
    logger.info("📂 Cargando datos iniciales desde Google Sheets...")
    if sheet:
        worksheet_data = obtener_hoja_data(sheet)
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_data and worksheet_registros:
            loop = asyncio.get_event_loop()
            activas, pendientes, recontactos = loop.run_until_complete(
                cargar_estructuras_desde_sheets(worksheet_data, worksheet_registros, application)
            )
            logger.info(f"📊 Datos iniciales cargados: {activas} activas, {pendientes} pendientes, {recontactos} recontactos")
    logger.info("✅ Bot iniciado correctamente")
    application.run_polling()

if __name__ == '__main__':
    main()
