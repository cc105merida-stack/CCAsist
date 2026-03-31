import asyncio
import os
import gspread
import img2pdf
import tempfile
from google.oauth2.service_account import Credentials
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes, CallbackQueryHandler
import warnings
import json
import logging

# Configurar  logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Ignorar warnings de deprecación
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ========== CONFIGURACIÓN DESDE VARIABLES DE ENTORNO ==========
TOKEN = os.environ.get('TELEGRAM_TOKEN')
SPREADSHEET_ID = "1XHRqlZHvHfxg2g5CqrZZU6_Bnjt3AaNcF4YZkO1wcXk"

# Verificar que las variables requeridas estén presentes
if not TOKEN:
    logger.error("❌ TELEGRAM_TOKEN no está configurado en variables de entorno")
    exit(1)

if not SPREADSHEET_ID:
    logger.error("❌ SPREADSHEET_ID no está configurado en variables de entorno")
    exit(1)

# ========== ESTADOS PARA LA CONVERSACIÓN ============
NOMBRE, CEDULA, TELEFONO, ROL = range(4)

# Estados para las llamadas
class LlamadaEstado:
    def __init__(self):
        self.usuarios_activos = {}
        self.pendientes_notificacion = {}
        self.recontactos_pendientes = {}

llamadas_activas = LlamadaEstado()

# Diccionario para almacenar imágenes para PDF por usuario
imagenes_para_pdf = {}

# Roles disponibles
ROLES = {
    "AGENTE": "Agente",
    "SUPERVISOR": "Supervisor"
}

# ========== FUNCIONES AUXILIARES ============
def calcular_duracion(hora_asignacion, hora_completacion):
    """Calcula la duración entre dos horas en formato HH:MM:SS"""
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
    """Conecta con Google Sheets usando las credenciales desde variable de entorno"""
    try:
        credentials_json = os.environ.get('GOOGLE_CREDENTIALS')
        
        if not credentials_json:
            logger.error("❌ GOOGLE_CREDENTIALS no está configurado en variables de entorno")
            return None
        
        creds_dict = json.loads(credentials_json)
        
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive.file'
        ]
        
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID)
        
        logger.info("✅ Conexión exitosa a Google Sheets")
        return sheet
        
    except json.JSONDecodeError as e:
        logger.error(f"❌ Error al decodificar GOOGLE_CREDENTIALS: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Sheets: {e}")
        return None

def obtener_hoja_registros(sheet):
    """Obtiene la hoja de registros de usuarios"""
    try:
        return sheet.get_worksheet(0)
    except Exception as e:
        logger.error(f"Error al obtener hoja de registros: {e}")
        return None

def obtener_hoja_data(sheet):
    """Obtiene la hoja de datos de llamadas"""
    try:
        return sheet.worksheet("Data")
    except Exception as e:
        logger.error(f"Error al obtener hoja Data: {e}")
        return None

def obtener_rol_usuario(worksheet_registros, telegram_id):
    """Obtiene el rol del usuario por su Telegram ID"""
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
    """Obtiene el nombre completo del usuario por su Telegram ID"""
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
    """Obtiene el Telegram ID del agente por su nombre"""
    try:
        todos_los_registros = worksheet_registros.get_all_values()
        for fila in todos_los_registros[1:]:
            if len(fila) > 1 and fila[1] == nombre_agente:
                if len(fila) > 3:
                    return fila[3]
        return None
    except Exception as e:
        logger.error(f"Error al obtener agente: {e}")
        return None

def obtener_llamadas_pendientes(data_worksheet):
    """Obtiene todas las llamadas con ESTADO2 = NO CONTESTA y que NO tengan agente asignado"""
    try:
        todos_los_registros = data_worksheet.get_all_values()
        pendientes = []
        
        for i, fila in enumerate(todos_los_registros[1:], start=2):
            if len(fila) >= 20:
                estado2 = fila[18] if len(fila) > 18 else ""
                agente_asignado = fila[19] if len(fila) > 19 else ""
                
                if estado2 == "NO CONTESTA" and (agente_asignado == "" or agente_asignado is None):
                    pendientes.append({
                        'fila': i,
                        'folio': fila[3] if len(fila) > 3 else "No disponible",
                        'nombre_cliente': fila[8] if len(fila) > 8 else "No disponible",
                        'celular_cliente': fila[9] if len(fila) > 9 else "No disponible",
                        'validador': fila[1] if len(fila) > 1 else "No asignado",
                        'hora_asignacion': fila[16] if len(fila) > 16 else "No registrada",
                        'datos_completos': fila
                    })
        
        return pendientes
    except Exception as e:
        logger.error(f"Error al obtener llamadas pendientes: {e}")
        return []

def obtener_siguiente_llamada_disponible(data_worksheet):
    """Obtiene la primera llamada con ESTADO2 = DISPONIBLE o vacío"""
    try:
        todos_los_registros = data_worksheet.get_all_values()
        
        for i, fila in enumerate(todos_los_registros[1:], start=2):
            if len(fila) >= 19:
                estado2 = fila[18] if len(fila) > 18 else ""
                if estado2 == "" or estado2 == "DISPONIBLE":
                    return i, fila
        
        return None, None
    except Exception as e:
        logger.error(f"Error al obtener siguiente llamada: {e}")
        return None, None

def verificar_usuario_registrado(worksheet, cedula, telegram_id=None):
    """Verifica si un usuario ya está registrado por cédula o Telegram ID"""
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
    """Guarda los datos en Google Sheets incluyendo el rol"""
    try:
        fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        estado = "ACTIVO"
        nueva_fila = [fecha_hora, nombre, cedula, str(telegram_id), telefono, estado, rol]
        worksheet.append_row(nueva_fila)
        return True
    except Exception as e:
        logger.error(f"Error al guardar: {e}")
        return False

def obtener_registros_por_telegram_id(worksheet, telegram_id):
    """Obtiene los registros de un usuario por su Telegram ID"""
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
    """Actualiza el ESTADO2 de una llamada específica (columna S - índice 19)"""
    try:
        data_worksheet.update_cell(fila_numero, 19, nuevo_estado)
        logger.info(f"✅ Estado actualizado: fila {fila_numero} -> {nuevo_estado}")
        return True
    except Exception as e:
        logger.error(f"❌ Error al actualizar estado de llamada: {e}")
        return False

def registrar_hora_asignacion(data_worksheet, fila_numero):
    """Registra la hora actual en la columna HORA (columna Q - índice 17)"""
    try:
        hora_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data_worksheet.update_cell(fila_numero, 17, hora_actual)
        logger.info(f"✅ Hora registrada: fila {fila_numero} -> {hora_actual}")
        return hora_actual
    except Exception as e:
        logger.error(f"❌ Error al registrar hora de asignación: {e}")
        return None

def registrar_duracion_y_observacion(data_worksheet, fila_numero, resultado, observacion="", hora_asignacion=None, estado_validacion=None):
    """Registra la duración de la llamada y la observación sin timestamp"""
    try:
        if resultado == "completada":
            nuevo_estado = "COMPLETADA"
        elif resultado == "no_contesta":
            nuevo_estado = "NO CONTESTA"
        elif resultado == "rechazada":
            nuevo_estado = "RECHAZADA"
        else:
            nuevo_estado = "FINALIZADA"
        
        data_worksheet.update_cell(fila_numero, 19, nuevo_estado)
        
        if estado_validacion:
            data_worksheet.update_cell(fila_numero, 3, estado_validacion)
        
        if hora_asignacion:
            hora_completacion = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            duracion = calcular_duracion(hora_asignacion, hora_completacion)
            data_worksheet.update_cell(fila_numero, 18, duracion)
        
        if observacion:
            obs_actual = data_worksheet.cell(fila_numero, 16).value or ""
            if obs_actual:
                nueva_obs = f"{obs_actual} | {observacion}"
            else:
                nueva_obs = observacion
            data_worksheet.update_cell(fila_numero, 16, nueva_obs)
        
        return True
    except Exception as e:
        logger.error(f"Error al registrar duración y observación: {e}")
        return False

def formatear_datos_llamada(datos_llamada, es_recontacto=False):
    """Formatea los datos de la llamada para mostrarlos al usuario (sin Markdown)"""
    
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
    paquete = datos_llamada[13] if len(datos_llamada) > 13 and datos_llamada[13] else "No disponible"
    costo = datos_llamada[14] if len(datos_llamada) > 14 and datos_llamada[14] else "No disponible"
    observacion_existente = datos_llamada[15] if len(datos_llamada) > 15 and datos_llamada[15] else None
    
    if es_recontacto:
        titulo = "🔄 RECONTACTO ASIGNADO 🔄"
    else:
        titulo = "📞 LLAMADA ASIGNADA 📞"
    
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
    mensaje += f"📦 PAQUETE: {paquete}\n"
    mensaje += f"💰 COSTO DEL PAQUETE: {costo}\n"
    
    if observacion_existente:
        mensaje += f"\n📝 OBSERVACIÓN PREVIA:\n{observacion_existente}\n"
    
    mensaje += f"\n✅ La llamada ha sido marcada como EN PROCESO.\n"
    mensaje += f"🕐 Hora de asignación: {datetime.now().strftime('%H:%M:%S')}\n\n"
    mensaje += f"Cuando termines, usa /completarllamada para registrar el resultado."
    
    return mensaje

# ========== FUNCIONES DEL BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mensaje de bienvenida según el rol del usuario"""
    user_id = update.effective_user.id

    # Temporal para debuggear
    logger.info("🔍 Verificando credenciales...")
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
                        f"Tu ID de Telegram es: `{user_id}`\n"
                        f"Tu rol es: *{rol}*\n\n"
                        "📚 *Comandos disponibles para Supervisor:*\n\n"
                        "/start - Mostrar este mensaje\n"
                        "/registrar - Iniciar proceso de registro\n"
                        "/miestado - Ver tu estado de registro\n"
                        "/revisarpendientes - Ver llamadas NO CONTESTA\n"
                        "/notificar [número] - Notificar a un agente para recontacto\n"
                        "/cambiarrol [cédula] [rol] - Cambiar rol de un usuario\n"
                        "/cambiarestado [cédula] [estado] - Cambiar estado de un usuario\n"
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
                        f"Tu ID de Telegram es: `{user_id}`\n"
                        f"Tu rol es: *{rol}*\n\n"
                        "📚 *Comandos disponibles para Agente:*\n\n"
                        "/start - Mostrar este mensaje\n"
                        "/registrar - Iniciar proceso de registro\n"
                        "/obtener - Obtener una llamada disponible\n"
                        "/misdatos - Ver tu llamada activa\n"
                        "/completarllamada - Finalizar tu llamada actual\n"
                        "/mispendientes - Ver tus recontactos pendientes\n"
                        "/miestado - Consultar tu estado de registro\n"
                        "/ayuda - Mostrar ayuda detallada\n"
                        "/cancelar - Cancelar el registro en proceso",
                        parse_mode='Markdown'
                    )
                return
    
    # Si el usuario no está registrado o no se pudo obtener el rol
    await update.message.reply_text(
        f"Hola 👋 Soy el bot de gestión de llamadas.\n\n"
        f"Tu ID de Telegram es: `{user_id}`\n\n"
        "⚠️ *No estás registrado en el sistema.*\n\n"
        "Para registrarte, usa el comando:\n"
        "/registrar\n\n"
        "Si ya estás registrado, contacta al administrador.",
        parse_mode='Markdown'
    )
    
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mensaje de ayuda según el rol"""
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
            "*Gestión de Usuarios:*\n"
            "/registrar - Registrar nuevo usuario\n"
            "/miestado - Ver tu estado de registro\n"
            "/cambiarrol [cédula] [rol] - Cambiar rol de un usuario\n"
            "/cambiarestado [cédula] [estado] - Cambiar estado de un usuario\n\n"
            "*Gestión de Llamadas:*\n"
            "/revisarpendientes - Ver llamadas NO CONTESTA\n"
            "/notificar [número] - Notificar a un agente para recontacto\n\n"
            "*Gestión de PDF:*\n"
            "/crearpdf - Iniciar creación de PDF con imágenes\n"
            "/generarpdf - Generar PDF con las imágenes recibidas\n"
            "/cancelarpdf - Cancelar creación de PDF\n\n"
            "*Otros:*\n"
            "/start - Mostrar mensaje de bienvenida\n"
            "/ayuda - Mostrar esta ayuda\n"
            "/cancelar - Cancelar el registro en proceso",
            parse_mode='Markdown'
        )
    elif rol == "Agente":
        await update.message.reply_text(
            "📚 *COMANDOS PARA AGENTE* 📚\n\n"
            "*Gestión de Registro:*\n"
            "/registrar - Iniciar proceso de registro\n"
            "/miestado - Consultar tu estado de registro\n\n"
            "*Gestión de Llamadas:*\n"
            "/obtener - Obtener una llamada disponible\n"
            "/misdatos - Ver tu llamada activa\n"
            "/completarllamada - Finalizar tu llamada actual\n"
            "/mispendientes - Ver tus recontactos pendientes\n\n"
            "*Otros:*\n"
            "/start - Mostrar mensaje de bienvenida\n"
            "/ayuda - Mostrar esta ayuda\n"
            "/cancelar - Cancelar el registro en proceso",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "📚 *COMANDOS DISPONIBLES* 📚\n\n"
            "*Para registrarte:*\n"
            "/registrar - Iniciar proceso de registro\n\n"
            "*Una vez registrado:*\n"
            "Los comandos disponibles dependerán de tu rol.\n\n"
            "Contacta al administrador para más información.",
            parse_mode='Markdown'
        )


async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el proceso de registro"""
    await update.message.reply_text(
        "📝 Vamos a registrarte.\n\n"
        "Por favor, escribe tu nombre completo:"
    )
    return NOMBRE

async def obtener_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obtiene el nombre del usuario"""
    try:
        nombre = update.message.text
        if not nombre or nombre.strip() == "":
            await update.message.reply_text(
                "❌ El nombre no puede estar vacío.\n"
                "Por favor, escribe tu nombre completo:"
            )
            return NOMBRE
        
        context.user_data['nombre'] = nombre.strip()
        
        await update.message.reply_text(
            f"✅ Nombre guardado: {nombre}\n\n"
            "Ahora, escribe tu cédula (solo números):"
        )
        return CEDULA
    except Exception as e:
        logger.error(f"Error en obtener_nombre: {e}")
        await update.message.reply_text(
            "❌ Ocurrió un error. Por favor, intenta nuevamente con /registrar"
        )
        return ConversationHandler.END

async def obtener_cedula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obtiene la cédula y verifica que no esté registrada"""
    cedula = update.message.text.strip()
    telegram_id = update.effective_user.id
    
    if not cedula.isdigit():
        await update.message.reply_text(
            "❌ La cédula debe contener solo números.\n"
            "Por favor, ingresa tu cédula nuevamente:"
        )
        return CEDULA
    
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text(
            "❌ Error al conectar con la base de datos. Por favor, intenta más tarde."
        )
        return ConversationHandler.END
    
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text(
            "❌ Error al acceder a la hoja de registros. Por favor, intenta más tarde."
        )
        return ConversationHandler.END
    
    if verificar_usuario_registrado(worksheet_registros, cedula, telegram_id):
        await update.message.reply_text(
            "❌ ¡Ya estás registrado!\n\n"
            "Tu cédula o tu cuenta de Telegram ya existe en nuestra base de datos. "
            "No puedes registrarte nuevamente.\n\n"
            "Para ver tu estado, usa el comando /miestado"
        )
        return ConversationHandler.END
    
    context.user_data['cedula'] = cedula
    
    keyboard = [
        [
            InlineKeyboardButton("👤 Agente", callback_data="rol_agente"),
            InlineKeyboardButton("👑 Supervisor", callback_data="rol_supervisor"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ Cédula guardada: {cedula}\n\n"
        "Ahora, selecciona tu rol:",
        reply_markup=reply_markup
    )
    return ROL

async def obtener_rol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obtiene el rol seleccionado"""
    query = update.callback_query
    await query.answer()
    
    rol = "Agente" if query.data == "rol_agente" else "Supervisor"
    context.user_data['rol'] = rol
    
    await query.edit_message_text(
        f"✅ Rol seleccionado: {rol}\n\n"
        "Finalmente, escribe tu número de teléfono:"
    )
    return TELEFONO

async def obtener_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obtiene el teléfono y guarda todos los datos"""
    telefono = update.message.text.strip()
    telegram_id = update.effective_user.id
    
    if not telefono.isdigit() or len(telefono) < 7:
        await update.message.reply_text(
            "❌ El teléfono debe contener solo números y tener al menos 7 dígitos.\n"
            "Por favor, ingresa tu teléfono nuevamente:"
        )
        return TELEFONO
    
    context.user_data['telefono'] = telefono
    
    nombre = context.user_data['nombre']
    cedula = context.user_data['cedula']
    rol = context.user_data.get('rol', 'Agente')
    
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text(
            "❌ Error al conectar con la base de datos. Por favor, intenta más tarde."
        )
        return ConversationHandler.END
    
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text(
            "❌ Error al acceder a la hoja de registros. Por favor, intenta más tarde."
        )
        return ConversationHandler.END
    
    if guardar_en_sheets(worksheet_registros, nombre, cedula, telegram_id, telefono, rol):
        await update.message.reply_text(
            f"✅ ¡Registro exitoso! 🎉\n\n"
            f"📋 Datos registrados:\n"
            f"👤 Nombre: {nombre}\n"
            f"🆔 Cédula: {cedula}\n"
            f"🤖 Telegram ID: {telegram_id}\n"
            f"📱 Teléfono: {telefono}\n"
            f"🔘 Estado: ACTIVO\n"
            f"👑 Rol: {rol}\n\n"
            "Ahora puedes usar los comandos según tu rol."
        )
    else:
        await update.message.reply_text(
            "❌ Error al guardar tus datos. Por favor, intenta más tarde."
        )
    
    context.user_data.clear()
    return ConversationHandler.END

async def obtener_llamada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obtiene una llamada disponible para el usuario (solo Agentes)"""
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
        await update.message.reply_text(
            "❌ No tienes permisos para usar este comando.\n"
            "Este comando solo está disponible para Agentes."
        )
        return
    
    registros = obtener_registros_por_telegram_id(worksheet_registros, telegram_id)
    if not registros:
        await update.message.reply_text(
            "❌ No estás registrado en el sistema.\n\n"
            "Por favor, regístrate primero usando el comando /registrar"
        )
        return
    
    nombre_usuario = obtener_nombre_usuario(worksheet_registros, telegram_id)
    if not nombre_usuario:
        nombre_usuario = f"Usuario {telegram_id}"
    
    if telegram_id in llamadas_activas.usuarios_activos:
        folio_activo = llamadas_activas.usuarios_activos[telegram_id].get('folio', 'desconocido')
        await update.message.reply_text(
            f"⚠️ Ya tienes una llamada activa\n\n"
            f"Tienes asignado el folio: {folio_activo}\n\n"
            f"Para obtener una nueva llamada, primero debes completar la actual usando:\n"
            f"/completarllamada\n\n"
            f"También puedes ver tus datos activos con /misdatos"
        )
        return
    
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await update.message.reply_text(
            "❌ Error: No se encontró la hoja 'Data'."
        )
        return
    
    fila_num, datos_llamada = obtener_siguiente_llamada_disponible(worksheet_data)
    
    if not fila_num or not datos_llamada:
        await update.message.reply_text(
            "📭 No hay llamadas disponibles en este momento\n\n"
            "Por favor, intenta más tarde o contacta al administrador."
        )
        return
    
    hora_asignacion = registrar_hora_asignacion(worksheet_data, fila_num)
    if not hora_asignacion:
        await update.message.reply_text(
            "❌ Error al registrar la hora de asignación."
        )
        return
    
    if actualizar_estado_llamada(worksheet_data, fila_num, "EN PROCESO"):
        folio_siac = datos_llamada[3] if len(datos_llamada) > 3 else "Sin folio"
        llamadas_activas.usuarios_activos[telegram_id] = {
            'fila': fila_num,
            'folio': folio_siac,
            'datos': datos_llamada,
            'nombre_usuario': nombre_usuario,
            'hora_asignacion': hora_asignacion,
            'es_recontacto': False
        }
        
        if 'llamadas_activas' not in context.bot_data:
            context.bot_data['llamadas_activas'] = {}
        context.bot_data['llamadas_activas'][str(telegram_id)] = {
            'fila': fila_num,
            'folio': folio_siac,
            'datos': datos_llamada,
            'nombre_usuario': nombre_usuario,
            'hora_asignacion': hora_asignacion,
            'es_recontacto': False
        }
        
        mensaje = formatear_datos_llamada(datos_llamada, es_recontacto=False)
        await update.message.reply_text(mensaje)
        
        try:
            worksheet_data.update_cell(fila_num, 2, nombre_usuario)
            logger.info(f"✅ Validador actualizado: {nombre_usuario} en fila {fila_num}")
        except Exception as e:
            logger.error(f"❌ Error al actualizar validador: {e}")
    else:
        await update.message.reply_text(
            "❌ Error al asignar la llamada."
        )

async def mis_datos_activos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la llamada activa del usuario"""
    telegram_id = update.effective_user.id
    
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
            if rol != "Agente":
                await update.message.reply_text(
                    "❌ Este comando solo está disponible para Agentes."
                )
                return
    
    llamada_activa = None
    if 'llamadas_activas' in context.bot_data and str(telegram_id) in context.bot_data['llamadas_activas']:
        llamada_activa = context.bot_data['llamadas_activas'][str(telegram_id)]
    elif telegram_id in llamadas_activas.usuarios_activos:
        llamada_activa = llamadas_activas.usuarios_activos[telegram_id]
    
    if not llamada_activa:
        await update.message.reply_text(
            "📭 No tienes ninguna llamada activa\n\n"
            "Usa /obtener para solicitar una llamada disponible."
        )
        return
    
    datos = llamada_activa['datos']
    folio = datos[3] if len(datos) > 3 else "No disponible"
    tipo_venta = datos[4] if len(datos) > 4 else "No disponible"
    nombre_cliente = datos[8] if len(datos) > 8 else "No disponible"
    celular_cliente = datos[9] if len(datos) > 9 else "No disponible"
    correo = datos[11] if len(datos) > 11 and datos[11] else "No disponible"
    direccion = datos[12] if len(datos) > 12 and datos[12] else "No disponible"
    hora_asignacion = llamada_activa.get('hora_asignacion', 'No registrada')
    observacion_existente = datos[15] if len(datos) > 15 and datos[15] else None
    
    if hora_asignacion != "No registrada":
        try:
            hora_obj = datetime.strptime(hora_asignacion, "%Y-%m-%d %H:%M:%S")
            hora_mostrar = hora_obj.strftime("%H:%M:%S")
        except:
            hora_mostrar = hora_asignacion
    else:
        hora_mostrar = hora_asignacion
    
    mensaje = (
        f"📞 TU LLAMADA ACTIVA 📞\n\n"
        f"📋 FOLIO SIAC: {folio}\n"
        f"🏷️ TIPO DE VENTA: {tipo_venta}\n"
        f"👤 NOMBRE DEL CLIENTE: {nombre_cliente}\n"
        f"📱 CELULAR CLIENTE: {celular_cliente}\n"
        f"✉️ CORREO: {correo}\n"
        f"📍 DIRECCIÓN: {direccion}\n"
        f"🕐 Hora de asignación: {hora_mostrar}\n"
    )
    
    if observacion_existente:
        mensaje += f"\n📝 OBSERVACIÓN PREVIA:\n{observacion_existente}\n"
    
    mensaje += (
        f"\n✅ Estado: EN PROCESO\n\n"
        f"Cuando termines, usa /completarllamada para finalizar."
    )
    
    await update.message.reply_text(mensaje)

async def completar_llamada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el proceso para completar una llamada (solo Agentes)"""
    telegram_id = update.effective_user.id
    
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
            if rol != "Agente":
                await update.message.reply_text(
                    "❌ Este comando solo está disponible para Agentes."
                )
                return
    
    llamada_activa = None
    if 'llamadas_activas' in context.bot_data and str(telegram_id) in context.bot_data['llamadas_activas']:
        llamada_activa = context.bot_data['llamadas_activas'][str(telegram_id)]
    elif telegram_id in llamadas_activas.usuarios_activos:
        llamada_activa = llamadas_activas.usuarios_activos[telegram_id]
    
    if not llamada_activa:
        await update.message.reply_text(
            "❌ No tienes ninguna llamada activa\n\n"
            "Usa /obtener para solicitar una llamada primero."
        )
        return
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Completada", callback_data="resultado_completada"),
            InlineKeyboardButton("📵 No contesta", callback_data="resultado_no_contesta"),
        ],
        [
            InlineKeyboardButton("❌ Rechazada", callback_data="resultado_rechazada"),
            InlineKeyboardButton("↩️ Cancelar", callback_data="resultado_cancelar"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    context.user_data['llamada_a_completar'] = llamada_activa
    
    await update.message.reply_text(
        "📝 ¿Cómo resultó la llamada?\n\n"
        "Selecciona una opción:",
        reply_markup=reply_markup
    )

async def manejar_resultado_llamada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el resultado seleccionado por el usuario"""
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
            [
                InlineKeyboardButton("✅ Validada", callback_data="validacion_validada"),
                InlineKeyboardButton("📅 Llamada programada", callback_data="validacion_programada"),
            ],
            [
                InlineKeyboardButton("❌ Servicio Cancelado", callback_data="validacion_cancelado"),
                InlineKeyboardButton("⏳ Pendiente por el promotor", callback_data="validacion_pendiente"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📋 Selecciona el estado de validación de la llamada:",
            reply_markup=reply_markup
        )
        return
    
    await query.edit_message_text(
        f"📝 Resultado seleccionado: {resultado.upper()}\n\n"
        f"Por favor, escribe una breve observación (opcional).\n"
        f"Puedes enviar 'skip' para omitir:"
    )

async def manejar_validacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja la selección de validación del usuario"""
    query = update.callback_query
    await query.answer()
    
    validacion = query.data.replace("validacion_", "")
    
    opciones_validacion = {
        "validada": "Validada",
        "programada": "Llamada programada",
        "cancelado": "Servicio Cancelado",
        "pendiente": "Pendiente por el promotor"
    }
    
    estado_validacion = opciones_validacion.get(validacion, validacion)
    context.user_data['validacion_estado'] = estado_validacion
    
    await query.edit_message_text(
        f"📝 Estado de validación seleccionado: {estado_validacion}\n\n"
        f"Por favor, escribe una breve observación (opcional).\n"
        f"Puedes enviar 'skip' para omitir:"
    )

async def recibir_observacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la observación del usuario (para todos los casos)"""
    
    if context.user_data.get('nombre') is not None or context.user_data.get('cedula') is not None:
        return
    
    observacion = update.message.text
    telegram_id = update.effective_user.id
    
    resultado = context.user_data.get('resultado_temp')
    fila_num = context.user_data.get('fila_temp')
    folio = context.user_data.get('folio_temp')
    hora_asignacion = context.user_data.get('hora_asignacion_temp')
    llamada_activa = context.user_data.get('llamada_temp')
    estado_validacion = context.user_data.get('validacion_estado')
    
    if not resultado or not fila_num:
        await update.message.reply_text(
            "❌ Error: No se encontraron los datos de la llamada.\n"
            "Por favor, intenta nuevamente con /completarllamada"
        )
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
        
        context.user_data.pop('resultado_temp', None)
        context.user_data.pop('fila_temp', None)
        context.user_data.pop('folio_temp', None)
        context.user_data.pop('hora_asignacion_temp', None)
        context.user_data.pop('llamada_temp', None)
        context.user_data.pop('validacion_estado', None)
        context.user_data.pop('llamada_a_completar', None)
        
        mensaje_confirmacion = (
            f"✅ Llamada finalizada exitosamente\n\n"
            f"📋 Folio: {folio}\n"
            f"📊 Resultado: {resultado.upper()}\n"
        )
        
        if estado_validacion:
            mensaje_confirmacion += f"🔖 Validación: {estado_validacion}\n"
        
        mensaje_confirmacion += f"📝 Observación: {observacion if observacion else 'Sin observación'}\n\n"
        mensaje_confirmacion += f"Puedes solicitar una nueva llamada usando /obtener"
        
        await update.message.reply_text(mensaje_confirmacion)
    else:
        await update.message.reply_text(
            "❌ Error al registrar el resultado. Por favor, intenta nuevamente."
        )

async def mis_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra las llamadas pendientes de recontacto asignadas al agente"""
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
        await update.message.reply_text(
            "❌ No tienes permisos para usar este comando.\n"
            "Este comando solo está disponible para Agentes."
        )
        return
    
    nombre_agente = obtener_nombre_usuario(worksheet_registros, telegram_id)
    if not nombre_agente:
        await update.message.reply_text(
            "❌ No se encontró tu información de registro."
        )
        return
    
    tiene_activa = telegram_id in llamadas_activas.usuarios_activos
    if tiene_activa:
        folio_activo = llamadas_activas.usuarios_activos[telegram_id].get('folio', 'desconocido')
        await update.message.reply_text(
            f"⚠️ Tienes una llamada activa\n\n"
            f"Tienes asignado el folio: {folio_activo}\n\n"
            f"Para tomar una llamada pendiente, primero debes completar la actual usando:\n"
            f"/completarllamada\n\n"
            f"Puedes ver tus datos activos con /misdatos"
        )
        return
    
    pendientes_agente = []
    
    if 'recontactos_pendientes' in context.bot_data:
        for llamada_id, llamada in context.bot_data['recontactos_pendientes'].items():
            if llamada.get('validador_original') == nombre_agente:
                pendientes_agente.append({
                    'id': llamada_id,
                    'folio': llamada['folio'],
                    'nombre_cliente': llamada['nombre_cliente'],
                    'celular_cliente': llamada['celular_cliente'],
                    'hora_asignacion': llamada.get('hora_asignacion', 'No registrada'),
                    'datos': llamada['datos']
                })
    
    if not pendientes_agente:
        await update.message.reply_text(
            f"📭 No tienes llamadas pendientes de recontacto.\n\n"
            f"Usa /obtener para tomar llamadas disponibles."
        )
        return
    
    mensaje = f"📋 *TUS RECONTACTOS PENDIENTES* 📋\n\n"
    mensaje += f"Tienes {len(pendientes_agente)} llamada(s) pendiente(s) para recontactar.\n\n"
    
    keyboard = []
    
    for idx, pendiente in enumerate(pendientes_agente, 1):
        hora_str = pendiente['hora_asignacion']
        if hora_str != "No registrada":
            try:
                hora_obj = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
                hora_mostrar = hora_obj.strftime("%d/%m %H:%M")
            except:
                hora_mostrar = hora_str
        else:
            hora_mostrar = hora_str
        
        mensaje += (
            f"*{idx}.* 📱 `{pendiente['celular_cliente']}`\n"
            f"   👤 {pendiente['nombre_cliente']}\n"
            f"   📋 Folio: {pendiente['folio']}\n"
            f"   🕐 Asignado: {hora_mostrar}\n\n"
        )
        
        keyboard.append([
            InlineKeyboardButton(
                f"📞 Tomar llamada {idx} - {pendiente['nombre_cliente'][:20]}", 
                callback_data=f"tomar_recontacto_{pendiente['id']}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton("❌ Cerrar", callback_data="cerrar_mispendientes")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        mensaje,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def revisar_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra las llamadas pendientes (solo Supervisores) con botones"""
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
        await update.message.reply_text(
            "❌ No tienes permisos para usar este comando.\n"
            "Este comando solo está disponible para Supervisores."
        )
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
    else:
        claves_actuales = [f"pendiente_{i+1}" for i in range(len(pendientes))]
        claves_anteriores = list(context.bot_data['pendientes_notificacion'].keys())
        
        for clave in claves_anteriores:
            if clave not in claves_actuales:
                del context.bot_data['pendientes_notificacion'][clave]
                logger.info(f"✅ Eliminada clave antigua: {clave}")
    
    mensaje = "📋 *LLAMADAS PENDIENTES (NO CONTESTA)* 📋\n\n"
    keyboard = []
    
    for idx, llamada in enumerate(pendientes, 1):
        clave = f"pendiente_{idx}"
        context.bot_data['pendientes_notificacion'][clave] = llamada
        
        hora_str = llamada['hora_asignacion']
        if hora_str != "No registrada" and hora_str:
            try:
                hora_obj = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
                hora_mostrar = hora_obj.strftime("%d/%m %H:%M")
            except:
                hora_mostrar = hora_str
        else:
            hora_mostrar = "No registrada"
        
        mensaje += (
            f"*{idx}.* 📱 `{llamada['celular_cliente']}`\n"
            f"   👤 {llamada['nombre_cliente']}\n"
            f"   🕐 Llamada: {hora_mostrar}\n"
            f"   👔 Agente: {llamada['validador']}\n"
            f"   📋 Folio: {llamada['folio']}\n\n"
        )
        
        keyboard.append([
            InlineKeyboardButton(
                f"📢 Notificar a {llamada['validador']}", 
                callback_data=f"notificar_{idx}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton("❌ Cerrar", callback_data="cerrar_pendientes")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        mensaje,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def manejar_notificacion_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja la notificación desde el botón"""
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
            await query.edit_message_text(
                "❌ No tienes permisos para realizar esta acción."
            )
            return
        
        if 'pendientes_notificacion' not in context.bot_data or clave not in context.bot_data['pendientes_notificacion']:
            await query.edit_message_text(
                "❌ Esta llamada ya no está disponible.\n"
                "Usa /revisarpendientes para actualizar la lista."
            )
            return
        
        llamada = context.bot_data['pendientes_notificacion'][clave]
        
        agente_telegram_id = obtener_agente_por_nombre(worksheet_registros, llamada['validador'])
        
        if not agente_telegram_id:
            await query.edit_message_text(
                f"❌ No se pudo encontrar al agente {llamada['validador']} en el sistema."
            )
            return
        
        worksheet_data = obtener_hoja_data(sheet)
        if worksheet_data:
            try:
                worksheet_data.update_cell(llamada['fila'], 20, llamada['validador'])
                logger.info(f"✅ Agente asignado guardado: {llamada['validador']} en fila {llamada['fila']}")
            except Exception as e:
                logger.error(f"Error al guardar agente asignado: {e}")
        
        llamada_id = f"recontacto_{llamada['fila']}_{llamada['folio']}"
        
        if 'recontactos_pendientes' not in context.bot_data:
            context.bot_data['recontactos_pendientes'] = {}
        
        context.bot_data['recontactos_pendientes'][llamada_id] = {
            'fila': llamada['fila'],
            'folio': llamada['folio'],
            'datos': llamada['datos_completos'],
            'nombre_cliente': llamada['nombre_cliente'],
            'celular_cliente': llamada['celular_cliente'],
            'validador_original': llamada['validador'],
            'hora_asignacion': llamada['hora_asignacion'],
            'agente_telegram_id': agente_telegram_id
        }
        
        if 'pendientes_notificacion' in context.bot_data and clave in context.bot_data['pendientes_notificacion']:
            del context.bot_data['pendientes_notificacion'][clave]
            logger.info(f"✅ Llamada {clave} eliminada de pendientes_notificacion")
        
        nombre_supervisor = query.from_user.first_name
        if query.from_user.last_name:
            nombre_supervisor += f" {query.from_user.last_name}"
        
        keyboard = [
            [InlineKeyboardButton("📞 Tomar llamada pendiente", callback_data=f"tomar_recontacto_{llamada_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        mensaje_notificacion = (
            f"🔔 NOTIFICACIÓN DE RECONTACTO 🔔\n\n"
            f"El supervisor {nombre_supervisor} te ha solicitado recontactar al siguiente cliente:\n\n"
            f"📱 Teléfono: {llamada['celular_cliente']}\n"
            f"👤 Cliente: {llamada['nombre_cliente']}\n"
            f"📋 Folio SIAC: {llamada['folio']}\n"
            f"🕐 Llamada anterior: {llamada['hora_asignacion']}\n\n"
            f"Haz clic en el botón para tomar esta llamada.\n\n"
            f"También puedes ver todas tus llamadas pendientes con /mispendientes"
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(agente_telegram_id),
                text=mensaje_notificacion,
                reply_markup=reply_markup
            )
            
            mensaje_exito = (
                f"✅ Notificación enviada al agente {llamada['validador']}\n\n"
                f"📱 Cliente: {llamada['celular_cliente']}\n"
                f"👤 Nombre: {llamada['nombre_cliente']}\n"
                f"📋 Folio: {llamada['folio']}\n\n"
                f"La llamada ha sido eliminada de la lista de pendientes.\n"
                f"El agente recibió un botón para tomar la llamada.\n"
                f"También puede usar /mispendientes para ver todas sus llamadas pendientes."
            )
            
            close_keyboard = [[InlineKeyboardButton("❌ Cerrar", callback_data="cerrar_notificacion")]]
            close_markup = InlineKeyboardMarkup(close_keyboard)
            
            await query.edit_message_text(
                mensaje_exito,
                reply_markup=close_markup
            )
            
        except Exception as e:
            await query.edit_message_text(
                f"❌ Error al enviar notificación\n\n"
                f"Detalle: {e}\n\n"
                f"Verifica que el agente {llamada['validador']} tenga el bot iniciado.\n\n"
                f"La llamada permanece en la lista de pendientes."
            )
            
    except ValueError:
        await query.edit_message_text("❌ Error: Índice de llamada no válido.")
    except Exception as e:
        await query.edit_message_text(f"❌ Error al procesar la notificación: {e}")

async def tomar_recontacto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el botón para tomar una llamada de recontacto"""
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
        await query.edit_message_text(
            "❌ No tienes permisos para realizar esta acción.\n"
            "Este botón solo está disponible para Agentes."
        )
        return
    
    registros = obtener_registros_por_telegram_id(worksheet_registros, telegram_id)
    if not registros:
        await query.edit_message_text(
            "❌ No estás registrado en el sistema.\n\n"
            "Por favor, regístrate primero usando el comando /registrar"
        )
        return
    
    if telegram_id in llamadas_activas.usuarios_activos:
        folio_activo = llamadas_activas.usuarios_activos[telegram_id].get('folio', 'desconocido')
        await query.edit_message_text(
            f"⚠️ Ya tienes una llamada activa\n\n"
            f"Tienes asignado el folio: {folio_activo}\n\n"
            f"Para tomar esta nueva llamada, primero debes completar la actual usando:\n"
            f"/completarllamada\n\n"
            f"También puedes ver tus datos activos con /misdatos"
        )
        return
    
    if 'recontactos_pendientes' not in context.bot_data or llamada_id not in context.bot_data['recontactos_pendientes']:
        await query.edit_message_text(
            "❌ Esta llamada ya no está disponible.\n\n"
            "Puede que otro agente la haya tomado o que haya expirado.\n"
            "Usa /mispendientes para actualizar tu lista."
        )
        return
    
    llamada = context.bot_data['recontactos_pendientes'][llamada_id]
    
    nombre_agente = obtener_nombre_usuario(worksheet_registros, telegram_id)
    if llamada['validador_original'] != nombre_agente:
        await query.edit_message_text(
            f"❌ Esta llamada no te fue asignada.\n\n"
            f"La llamada fue asignada originalmente a {llamada['validador_original']}."
        )
        return
    
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await query.edit_message_text(
            "❌ Error: No se encontró la hoja 'Data'."
        )
        return
    
    try:
        fila_num = llamada['fila']
        estado_actual = worksheet_data.cell(fila_num, 19).value
        
        if estado_actual != "NO CONTESTA":
            await query.edit_message_text(
                f"❌ La llamada ya no está disponible.\n"
                f"Estado actual: {estado_actual}\n\n"
                f"Usa /obtener para tomar una llamada nueva o /mispendientes para actualizar tu lista."
            )
            if llamada_id in context.bot_data['recontactos_pendientes']:
                del context.bot_data['recontactos_pendientes'][llamada_id]
            return
        
        datos_llamada = worksheet_data.row_values(fila_num)
        
        hora_asignacion = registrar_hora_asignacion(worksheet_data, fila_num)
        if not hora_asignacion:
            await query.edit_message_text(
                "❌ Error al registrar la hora de asignación."
            )
            return
        
        if actualizar_estado_llamada(worksheet_data, fila_num, "EN PROCESO"):
            try:
                worksheet_data.update_cell(fila_num, 20, "")
                logger.info(f"✅ Agente asignado limpiado en fila {fila_num}")
            except Exception as e:
                logger.error(f"Error al limpiar agente asignado: {e}")
            
            if 'pendientes_notificacion' in context.bot_data:
                claves_a_eliminar = []
                for clave, pend in context.bot_data['pendientes_notificacion'].items():
                    if pend.get('fila') == fila_num or pend.get('folio') == llamada['folio']:
                        claves_a_eliminar.append(clave)
                for clave in claves_a_eliminar:
                    del context.bot_data['pendientes_notificacion'][clave]
                    logger.info(f"✅ Llamada {clave} eliminada de pendientes_notificacion")
            
            llamadas_activas.usuarios_activos[telegram_id] = {
                'fila': fila_num,
                'folio': llamada['folio'],
                'datos': datos_llamada,
                'nombre_usuario': nombre_agente,
                'hora_asignacion': hora_asignacion,
                'es_recontacto': True
            }
            
            if 'llamadas_activas' not in context.bot_data:
                context.bot_data['llamadas_activas'] = {}
            context.bot_data['llamadas_activas'][str(telegram_id)] = {
                'fila': fila_num,
                'folio': llamada['folio'],
                'datos': datos_llamada,
                'nombre_usuario': nombre_agente,
                'hora_asignacion': hora_asignacion,
                'es_recontacto': True
            }
            
            try:
                worksheet_data.update_cell(fila_num, 2, nombre_agente)
                logger.info(f"✅ Validador actualizado: {nombre_agente} en fila {fila_num}")
            except Exception as e:
                logger.error(f"Error al actualizar validador: {e}")
            
            if llamada_id in context.bot_data['recontactos_pendientes']:
                del context.bot_data['recontactos_pendientes'][llamada_id]
                logger.info(f"✅ Llamada {llamada_id} eliminada de recontactos_pendientes")
            
            mensaje = formatear_datos_llamada(datos_llamada, es_recontacto=True)
            
            await query.edit_message_text(
                f"✅ Llamada tomada exitosamente\n\n"
                f"📋 Folio: {llamada['folio']}\n"
                f"👤 Cliente: {llamada['nombre_cliente']}\n\n"
                f"Los detalles de la llamada se han enviado a continuación."
            )
            
            await query.message.reply_text(mensaje)
            
        else:
            await query.edit_message_text(
                "❌ Error al asignar la llamada. Por favor, intenta nuevamente."
            )
            
    except Exception as e:
        await query.edit_message_text(f"❌ Error al procesar el recontacto: {e}")

async def notificar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notifica a un agente para recontactar a un cliente (solo Supervisores)"""
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
        await update.message.reply_text(
            "❌ No tienes permisos para usar este comando.\n"
            "Este comando solo está disponible para Supervisores."
        )
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Por favor, especifica el número de la llamada pendiente.\n"
            "Ejemplo: /notificar 1\n\n"
            "Usa /revisarpendientes para ver la lista."
        )
        return
    
    try:
        idx = int(args[0])
        clave = f"pendiente_{idx}"
        
        if clave not in llamadas_activas.pendientes_notificacion:
            await update.message.reply_text(
                "❌ Número de llamada no válido.\n"
                "Usa /revisarpendientes para ver la lista actualizada."
            )
            return
        
        llamada = llamadas_activas.pendientes_notificacion[clave]
        
        agente_telegram_id = obtener_agente_por_nombre(worksheet_registros, llamada['validador'])
        
        if not agente_telegram_id:
            await update.message.reply_text(
                f"❌ No se pudo encontrar al agente {llamada['validador']} en el sistema."
            )
            return
        
        mensaje_notificacion = (
            f"🔔 **NOTIFICACIÓN DE RECONTACTO** 🔔\n\n"
            f"El supervisor te ha solicitado recontactar al siguiente cliente:\n\n"
            f"📱 **Teléfono:** {llamada['celular_cliente']}\n"
            f"👤 **Cliente:** {llamada['nombre_cliente']}\n"
            f"📋 **Folio SIAC:** {llamada['folio']}\n"
            f"🕐 **Llamada anterior:** {llamada['hora_asignacion']}\n\n"
            f"Por favor, realiza el recontacto lo antes posible."
        )
        
        try:
            await context.bot.send_message(
                chat_id=int(agente_telegram_id),
                text=mensaje_notificacion,
                parse_mode='Markdown'
            )
            
            await update.message.reply_text(
                f"✅ Notificación enviada al agente {llamada['validador']}\n"
                f"📱 Cliente: {llamada['celular_cliente']}\n"
                f"👤 Nombre: {llamada['nombre_cliente']}"
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Error al enviar notificación: {e}\n"
                f"Verifica que el agente tenga el bot iniciado."
            )
            
    except ValueError:
        await update.message.reply_text(
            "❌ El número debe ser un valor numérico.\n"
            "Ejemplo: /notificar 1"
        )

async def miestado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el estado de registro del usuario"""
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
        await update.message.reply_text(
            f"❌ No estás registrado en el sistema.\n\n"
            f"Tu Telegram ID es: {telegram_id}\n\n"
            "Para registrarte, usa el comando /registrar"
        )
        return
    
    ultimo_registro = registros[-1]
    fecha = ultimo_registro[0]
    nombre = ultimo_registro[1]
    cedula = ultimo_registro[2]
    telefono = ultimo_registro[4]
    estado = ultimo_registro[5]
    rol = ultimo_registro[6] if len(ultimo_registro) > 6 else "Agente"
    
    estado_emoji = {
        "ACTIVO": "🟢",
        "INACTIVO": "⚪",
        "INHABILITADO": "🔴"
    }.get(estado, "⚪")
    
    await update.message.reply_text(
        f"📋 Tu información de registro:\n\n"
        f"📅 Fecha registro: {fecha}\n"
        f"👤 Nombre: {nombre}\n"
        f"🆔 Cédula: {cedula}\n"
        f"🤖 Telegram ID: {telegram_id}\n"
        f"📱 Teléfono: {telefono}\n"
        f"{estado_emoji} Estado: {estado}\n"
        f"👑 Rol: {rol}\n\n"
        f"Nota: Si necesitas actualizar tus datos, contacta al administrador."
    )

async def cambiarrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite al supervisor cambiar el rol de un usuario (solo Supervisores)"""
    telegram_id = update.effective_user.id
    
    # Verificar que el usuario sea Supervisor
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    
    rol_solicitante = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol_solicitante != "Supervisor":
        await update.message.reply_text(
            "❌ No tienes permisos para usar este comando.\n"
            "Este comando solo está disponible para Supervisores."
        )
        return
    
    # Obtener los argumentos: cédula y nuevo rol
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "📝 *Formato del comando:*\n"
            "/cambiarrol [cédula] [nuevo_rol]\n\n"
            "*Roles disponibles:*\n"
            "• Agente\n"
            "• Supervisor\n\n"
            "*Ejemplo:*\n"
            "/cambiarrol 12345678 Supervisor",
            parse_mode='Markdown'
        )
        return
    
    cedula = args[0]
    nuevo_rol = args[1].capitalize()  # Capitalizar la primera letra
    
    # Validar que el rol sea válido
    if nuevo_rol not in ["Agente", "Supervisor"]:
        await update.message.reply_text(
            f"❌ Rol '{nuevo_rol}' no válido.\n"
            "Roles disponibles: Agente, Supervisor"
        )
        return
    
    # Buscar al usuario por cédula
    try:
        todos_los_registros = worksheet_registros.get_all_values()
        fila_encontrada = None
        datos_usuario = None
        
        for i, fila in enumerate(todos_los_registros[1:], start=2):  # start=2 para fila real
            if len(fila) > 2 and fila[2] == cedula:  # Columna C es cédula
                fila_encontrada = i
                datos_usuario = fila
                break
        
        if not fila_encontrada:
            await update.message.reply_text(
                f"❌ No se encontró un usuario con la cédula: {cedula}"
            )
            return
        
        # Obtener datos actuales
        nombre_actual = datos_usuario[1] if len(datos_usuario) > 1 else "Desconocido"
        rol_actual = datos_usuario[6] if len(datos_usuario) > 6 else "Agente"
        
        # Actualizar el rol en la columna G (índice 7 en 1-based, columna 7)
        worksheet_registros.update_cell(fila_encontrada, 7, nuevo_rol)
        
        await update.message.reply_text(
            f"✅ *Rol actualizado exitosamente*\n\n"
            f"👤 *Usuario:* {nombre_actual}\n"
            f"🆔 *Cédula:* {cedula}\n"
            f"🔄 *Rol anterior:* {rol_actual}\n"
            f"✨ *Nuevo rol:* {nuevo_rol}",
            parse_mode='Markdown'
        )
        
        logger.info(f"✅ Supervisor {telegram_id} cambió rol de {nombre_actual} ({cedula}) de {rol_actual} a {nuevo_rol}")
        
    except Exception as e:
        logger.error(f"Error al cambiar rol: {e}")
        await update.message.reply_text(
            f"❌ Error al cambiar el rol: {e}"
        )


async def cambiarestado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite al supervisor cambiar el estado de un usuario (solo Supervisores)"""
    telegram_id = update.effective_user.id
    
    # Verificar que el usuario sea Supervisor
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con la base de datos.")
        return
    
    worksheet_registros = obtener_hoja_registros(sheet)
    if not worksheet_registros:
        await update.message.reply_text("❌ Error al acceder a la hoja de registros.")
        return
    
    rol_solicitante = obtener_rol_usuario(worksheet_registros, telegram_id)
    if rol_solicitante != "Supervisor":
        await update.message.reply_text(
            "❌ No tienes permisos para usar este comando.\n"
            "Este comando solo está disponible para Supervisores."
        )
        return
    
    # Obtener los argumentos: cédula y nuevo estado
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "📝 *Formato del comando:*\n"
            "/cambiarestado [cédula] [nuevo_estado]\n\n"
            "*Estados disponibles:*\n"
            "• ACTIVO\n"
            "• INACTIVO\n"
            "• INHABILITADO\n\n"
            "*Ejemplo:*\n"
            "/cambiarestado 12345678 INACTIVO",
            parse_mode='Markdown'
        )
        return
    
    cedula = args[0]
    nuevo_estado = args[1].upper()  # Convertir a mayúsculas
    
    # Validar que el estado sea válido
    if nuevo_estado not in ["ACTIVO", "INACTIVO", "INHABILITADO"]:
        await update.message.reply_text(
            f"❌ Estado '{nuevo_estado}' no válido.\n"
            "Estados disponibles: ACTIVO, INACTIVO, INHABILITADO"
        )
        return
    
    # Buscar al usuario por cédula
    try:
        todos_los_registros = worksheet_registros.get_all_values()
        fila_encontrada = None
        datos_usuario = None
        
        for i, fila in enumerate(todos_los_registros[1:], start=2):  # start=2 para fila real
            if len(fila) > 2 and fila[2] == cedula:  # Columna C es cédula
                fila_encontrada = i
                datos_usuario = fila
                break
        
        if not fila_encontrada:
            await update.message.reply_text(
                f"❌ No se encontró un usuario con la cédula: {cedula}"
            )
            return
        
        # Obtener datos actuales
        nombre_actual = datos_usuario[1] if len(datos_usuario) > 1 else "Desconocido"
        estado_actual = datos_usuario[5] if len(datos_usuario) > 5 else "ACTIVO"
        rol_usuario = datos_usuario[6] if len(datos_usuario) > 6 else "Agente"
        
        # Actualizar el estado en la columna F (índice 6 en 1-based, columna 6)
        worksheet_registros.update_cell(fila_encontrada, 6, nuevo_estado)
        
        # Emoji según el nuevo estado
        estado_emoji = {
            "ACTIVO": "🟢",
            "INACTIVO": "⚪",
            "INHABILITADO": "🔴"
        }.get(nuevo_estado, "⚪")
        
        await update.message.reply_text(
            f"✅ *Estado actualizado exitosamente*\n\n"
            f"👤 *Usuario:* {nombre_actual}\n"
            f"🆔 *Cédula:* {cedula}\n"
            f"👑 *Rol:* {rol_usuario}\n"
            f"🔄 *Estado anterior:* {estado_actual}\n"
            f"✨ *Nuevo estado:* {estado_emoji} {nuevo_estado}",
            parse_mode='Markdown'
        )
        
        logger.info(f"✅ Supervisor {telegram_id} cambió estado de {nombre_actual} ({cedula}) de {estado_actual} a {nuevo_estado}")
        
    except Exception as e:
        logger.error(f"Error al cambiar estado: {e}")
        await update.message.reply_text(
            f"❌ Error al cambiar el estado: {e}"
        )

async def cargar_estructuras_desde_sheets(worksheet_data, worksheet_registros, context):
    """Carga todas las estructuras de datos desde Google Sheets"""
    try:
        # 1. Limpiar estructuras existentes
        llamadas_activas.usuarios_activos = {}
        llamadas_activas.pendientes_notificacion = {}
        llamadas_activas.recontactos_pendientes = {}
        
        if 'llamadas_activas' in context.bot_data:
            context.bot_data['llamadas_activas'] = {}
        if 'pendientes_notificacion' in context.bot_data:
            context.bot_data['pendientes_notificacion'] = {}
        if 'recontactos_pendientes' in context.bot_data:
            context.bot_data['recontactos_pendientes'] = {}
        
        # 2. Cargar llamadas activas (EN PROCESO)
        todas_las_filas = worksheet_data.get_all_values()
        for i, fila in enumerate(todas_las_filas[1:], start=2):
            if len(fila) >= 19:
                estado2 = fila[18] if len(fila) > 18 else ""
                validador = fila[1] if len(fila) > 1 else ""
                
                # Si está EN PROCESO, buscar quién la tomó
                if estado2 == "EN PROCESO" and validador:
                    # Buscar el Telegram ID del agente por su nombre
                    telegram_id = None
                    todos_registros = worksheet_registros.get_all_values()
                    for reg in todos_registros[1:]:
                        if len(reg) > 1 and reg[1] == validador and len(reg) > 3:
                            telegram_id = reg[3]
                            break
                    
                    if telegram_id:
                        llamadas_activas.usuarios_activos[telegram_id] = {
                            'fila': i,
                            'folio': fila[3] if len(fila) > 3 else "Sin folio",
                            'datos': fila,
                            'nombre_usuario': validador,
                            'hora_asignacion': fila[16] if len(fila) > 16 else "No registrada",
                            'es_recontacto': False
                        }
                        if 'llamadas_activas' not in context.bot_data:
                            context.bot_data['llamadas_activas'] = {}
                        context.bot_data['llamadas_activas'][str(telegram_id)] = llamadas_activas.usuarios_activos[telegram_id]
        
        # 3. Cargar pendientes de notificación (NO CONTESTA sin agente asignado)
        pendientes = obtener_llamadas_pendientes(worksheet_data)
        for idx, pendiente in enumerate(pendientes, 1):
            clave = f"pendiente_{idx}"
            llamadas_activas.pendientes_notificacion[clave] = pendiente
            if 'pendientes_notificacion' not in context.bot_data:
                context.bot_data['pendientes_notificacion'] = {}
            context.bot_data['pendientes_notificacion'][clave] = pendiente
        
        # 4. Cargar recontactos pendientes (NO CONTESTA con agente asignado)
        todas_las_filas = worksheet_data.get_all_values()
        for i, fila in enumerate(todas_las_filas[1:], start=2):
            if len(fila) >= 20:
                estado2 = fila[18] if len(fila) > 18 else ""
                agente_asignado = fila[19] if len(fila) > 19 else ""
                
                if estado2 == "NO CONTESTA" and agente_asignado and agente_asignado != "":
                    # Buscar el Telegram ID del agente
                    telegram_id = None
                    todos_registros = worksheet_registros.get_all_values()
                    for reg in todos_registros[1:]:
                        if len(reg) > 1 and reg[1] == agente_asignado and len(reg) > 3:
                            telegram_id = reg[3]
                            break
                    
                    llamada_id = f"recontacto_{i}_{fila[3] if len(fila) > 3 else 'sin_folio'}"
                    llamadas_activas.recontactos_pendientes[llamada_id] = {
                        'fila': i,
                        'folio': fila[3] if len(fila) > 3 else "No disponible",
                        'datos': fila,
                        'nombre_cliente': fila[8] if len(fila) > 8 else "No disponible",
                        'celular_cliente': fila[9] if len(fila) > 9 else "No disponible",
                        'validador_original': agente_asignado,
                        'hora_asignacion': fila[16] if len(fila) > 16 else "No registrada",
                        'agente_telegram_id': telegram_id
                    }
                    if 'recontactos_pendientes' not in context.bot_data:
                        context.bot_data['recontactos_pendientes'] = {}
                    context.bot_data['recontactos_pendientes'][llamada_id] = llamadas_activas.recontactos_pendientes[llamada_id]
        
        return len(llamadas_activas.usuarios_activos), len(llamadas_activas.pendientes_notificacion), len(llamadas_activas.recontactos_pendientes)
        
    except Exception as e:
        logger.error(f"Error al cargar estructuras: {e}")
        return None, None, None


async def recargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recarga todas las estructuras de datos desde Google Sheets (solo Supervisores)"""
    telegram_id = update.effective_user.id
    
    # Verificar que el usuario sea Supervisor
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
        await update.message.reply_text(
            "❌ No tienes permisos para usar este comando.\n"
            "Este comando solo está disponible para Supervisores."
        )
        return
    
    # Mostrar mensaje de inicio
    mensaje_inicio = await update.message.reply_text(
        "🔄 *Recargando estructuras de datos...*\n\n"
        "Por favor espera un momento.",
        parse_mode='Markdown'
    )
    
    # Obtener la hoja Data
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await mensaje_inicio.edit_text("❌ Error al acceder a la hoja Data.")
        return
    
    # Cargar todas las estructuras
    activas, pendientes, recontactos = await cargar_estructuras_desde_sheets(worksheet_data, worksheet_registros, context)
    
    if activas is not None:
        # Mensaje de éxito
        mensaje_exito = (
            f"✅ *Estructuras recargadas exitosamente*\n\n"
            f"📊 *Resumen cargado:*\n"
            f"• Llamadas activas (EN PROCESO): {activas}\n"
            f"• Pendientes de notificar (NO CONTESTA): {pendientes}\n"
            f"• Recontactos pendientes asignados: {recontactos}\n\n"
            f"🔄 Los datos han sido actualizados desde Google Sheets."
        )
        await mensaje_inicio.edit_text(mensaje_exito, parse_mode='Markdown')
        logger.info(f"✅ Supervisor {telegram_id} recargó estructuras: {activas} activas, {pendientes} pendientes, {recontactos} recontactos")
    else:
        await mensaje_inicio.edit_text(
            "❌ *Error al recargar estructuras*\n\n"
            "No se pudieron cargar los datos. Verifica la conexión con Google Sheets.",
            parse_mode='Markdown'
        )


        
async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela el registro"""
    await update.message.reply_text(
        "❌ Registro cancelado.\n\n"
        "Puedes iniciar nuevamente con /registrar"
    )
    context.user_data.clear()
    return ConversationHandler.END

# ========== FUNCIONES PARA PDF ==========
async def iniciar_creacion_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el proceso de creación de PDF (solo Supervisores)"""
    telegram_id = update.effective_user.id
    
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
            if rol != "Supervisor":
                await update.message.reply_text(
                    "❌ No tienes permisos para usar este comando.\n"
                    "Este comando solo está disponible para Supervisores."
                )
                return
    
    imagenes_para_pdf[telegram_id] = []
    
    await update.message.reply_text(
        "📸 *CREACIÓN DE PDF - INSTRUCCIONES* 📸\n\n"
        "Para crear un PDF con imágenes, sigue estos pasos:\n\n"
        "1. Envía las imágenes que deseas incluir (una por una)\n"
        "2. Cuando termines, usa el comando /generarpdf\n"
        "3. Si quieres cancelar, usa /cancelarpdf\n\n"
        "Las imágenes se agregarán en el orden en que las envíes.\n"
        "Puedes enviar hasta 20 imágenes por PDF.\n\n"
        "✅ *Listo para recibir imágenes* - Envía la primera imagen:",
        parse_mode='Markdown'
    )

async def recibir_imagen_para_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe imágenes para el PDF"""
    telegram_id = update.effective_user.id
    
    if telegram_id not in imagenes_para_pdf:
        return
    
    if not update.message.photo:
        await update.message.reply_text(
            "❌ Por favor, envía una imagen (foto).\n"
            "Los archivos en otros formatos no son soportados."
        )
        return
    
    photo = update.message.photo[-1]
    
    if len(imagenes_para_pdf[telegram_id]) >= 20:
        await update.message.reply_text(
            "⚠️ Límite de 20 imágenes alcanzado.\n"
            "Usa /generarpdf para crear el PDF."
        )
        return
    
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        temp_file_path = temp_file.name
        temp_file.close()
        
        file = await context.bot.get_file(photo.file_id)
        await file.download_to_drive(temp_file_path)
        
        imagenes_para_pdf[telegram_id].append(temp_file_path)
        
        count = len(imagenes_para_pdf[telegram_id])
        await update.message.reply_text(
            f"✅ Imagen {count} recibida.\n"
            f"Envía más imágenes o usa /generarpdf para crear el PDF."
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error al descargar la imagen: {e}")

async def generar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Genera el PDF con las imágenes recibidas"""
    telegram_id = update.effective_user.id
    
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
            if rol != "Supervisor":
                await update.message.reply_text(
                    "❌ No tienes permisos para usar este comando.\n"
                    "Este comando solo está disponible para Supervisores."
                )
                return
    
    if telegram_id not in imagenes_para_pdf or not imagenes_para_pdf[telegram_id]:
        await update.message.reply_text(
            "❌ No tienes imágenes guardadas para crear un PDF.\n\n"
            "Usa /crearpdf para comenzar a enviar imágenes."
        )
        return
    
    imagenes = imagenes_para_pdf[telegram_id]
    
    await update.message.reply_text(
        f"📄 Generando PDF con {len(imagenes)} imágenes...\n"
        f"Por favor espera un momento."
    )
    
    try:
        pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        pdf_path.close()
        
        with open(pdf_path.name, "wb") as f:
            f.write(img2pdf.convert(imagenes))
        
        with open(pdf_path.name, 'rb') as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=f"documento_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                caption=f"✅ PDF generado con {len(imagenes)} imágenes.\nFecha: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            )
        
        for img_path in imagenes:
            try:
                os.unlink(img_path)
            except:
                pass
        try:
            os.unlink(pdf_path.name)
        except:
            pass
        
        del imagenes_para_pdf[telegram_id]
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error al generar el PDF: {e}")
        for img_path in imagenes:
            try:
                os.unlink(img_path)
            except:
                pass
        del imagenes_para_pdf[telegram_id]

async def cancelar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela la creación del PDF y limpia las imágenes"""
    telegram_id = update.effective_user.id
    
    sheet = conectar_google_sheets()
    if sheet:
        worksheet_registros = obtener_hoja_registros(sheet)
        if worksheet_registros:
            rol = obtener_rol_usuario(worksheet_registros, telegram_id)
            if rol != "Supervisor":
                await update.message.reply_text(
                    "❌ No tienes permisos para usar este comando.\n"
                    "Este comando solo está disponible para Supervisores."
                )
                return
    
    if telegram_id in imagenes_para_pdf:
        for img_path in imagenes_para_pdf[telegram_id]:
            try:
                os.unlink(img_path)
            except:
                pass
        del imagenes_para_pdf[telegram_id]
        
        await update.message.reply_text(
            "❌ Creación de PDF cancelada.\n"
            "Las imágenes enviadas han sido eliminadas."
        )
    else:
        await update.message.reply_text(
            "❌ No tienes una sesión de creación de PDF activa.\n"
            "Usa /crearpdf para comenzar."
        )

# ========== CONFIGURACIÓN Y 
def main():
    """Función principal que inicia el bot"""
    logger.info("🤖 Iniciando bot...")
    logger.info(f"📊 Usando Google Sheets ID: {SPREADSHEET_ID}")
    
    sheet = conectar_google_sheets()
    if not sheet:
        logger.error("❌ No se pudo conectar a Google Sheets. Verifica las credenciales.")
        logger.error("El bot se ejecutará pero puede tener problemas de funcionamiento.")
    
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
    
    # Comandos administrativos
    application.add_handler(CommandHandler("cambiarrol", cambiarrol))
    application.add_handler(CommandHandler("cambiarestado", cambiarestado))
    
    # Comando oculto para recargar estructuras (solo supervisores, no aparece en ayuda)
    application.add_handler(CommandHandler("recargar", recargar))
    
    # Comandos para PDF
    application.add_handler(CommandHandler("crearpdf", iniciar_creacion_pdf))
    application.add_handler(CommandHandler("generarpdf", generar_pdf))
    application.add_handler(CommandHandler("cancelarpdf", cancelar_pdf))
    
    # Manejadores de mensajes y callbacks
    application.add_handler(MessageHandler(filters.PHOTO, recibir_imagen_para_pdf))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_observacion))
    
    application.add_handler(CallbackQueryHandler(manejar_resultado_llamada, pattern="^resultado_"))
    application.add_handler(CallbackQueryHandler(manejar_validacion, pattern="^validacion_"))
    application.add_handler(CallbackQueryHandler(obtener_rol, pattern="^rol_"))
    application.add_handler(CallbackQueryHandler(manejar_notificacion_boton, pattern="^(notificar_|cerrar_pendientes|cerrar_notificacion)"))
    application.add_handler(CallbackQueryHandler(tomar_recontacto, pattern="^(tomar_recontacto_|cerrar_mispendientes)"))
    
    # Conversación para registro
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
    if 'llamadas_activas' not in application.bot_data:
        application.bot_data['llamadas_activas'] = {}
    if 'pendientes_notificacion' not in application.bot_data:
        application.bot_data['pendientes_notificacion'] = {}
    if 'recontactos_pendientes' not in application.bot_data:
        application.bot_data['recontactos_pendientes'] = {}
    
    # Cargar datos iniciales al iniciar el bot
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
    logger.info(f"📊 Usando Google Sheets ID: {SPREADSHEET_ID}")
    
    application.run_polling()
    
if __name__ == '__main__':
    main()
