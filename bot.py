import asyncio
import os
import gspread
import img2pdf
import tempfile
import pytz
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
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
SPREADSHEET_ID = "1XHRqlZHvHfxg2g5CqrZZU6_Bnjt3AaNcF4YZkO1wcXk"

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
ESTRATEGIA, SELECCION_CLIENTE, INGRESO_FOLIO, INGRESO_DOCUMENTO = range(10, 14)

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
busqueda_siac = {}  # {user_id: {'estrategia': str, 'resultados': list, 'pagina': int, 'folio_asignado': dict}}

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
        data_worksheet.update_cell(fila_numero, 21, nuevo_estado)
        logger.info(f"✅ Estado actualizado: fila {fila_numero} -> {nuevo_estado}")
        return True
    except Exception as e:
        logger.error(f"❌ Error al actualizar estado de llamada: {e}")
        return False

def registrar_hora_asignacion(data_worksheet, fila_numero):
    try:
        hora_local = obtener_hora_local()
        hora_actual = hora_local.strftime("%Y-%m-%d %H:%M:%S")
        data_worksheet.update_cell(fila_numero, 19, hora_actual)
        logger.info(f"✅ Hora registrada: fila {fila_numero} -> {hora_actual}")
        return hora_actual
    except Exception as e:
        logger.error(f"❌ Error al registrar hora de asignación: {e}")
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
        data_worksheet.update_cell(fila_numero, 21, nuevo_estado)
        if estado_validacion:
            data_worksheet.update_cell(fila_numero, 3, estado_validacion)
        if hora_asignacion:
            hora_local = obtener_hora_local()
            hora_completacion = hora_local.strftime("%Y-%m-%d %H:%M:%S")
            duracion = calcular_duracion(hora_asignacion, hora_completacion)
            data_worksheet.update_cell(fila_numero, 20, duracion)
        if observacion:
            obs_actual = data_worksheet.cell(fila_numero, 17).value or ""
            nueva_obs = f"{obs_actual} | {observacion}" if obs_actual else observacion
            data_worksheet.update_cell(fila_numero, 17, nueva_obs)
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

# ========== FUNCIONES PARA GOOGLE DRIVE ==========
def obtener_drive_service():
    """Obtiene el servicio de Google Drive autenticado"""
    try:
        credentials_json = os.environ.get('GOOGLE_CREDENTIALS')
        if not credentials_json:
            logger.error("❌ GOOGLE_CREDENTIALS no está configurado en variables de entorno")
            return None
        creds_dict = json.loads(credentials_json)
        scopes = ['https://www.googleapis.com/auth/drive.file']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        service = build('drive', 'v3', credentials=creds)
        logger.info("✅ Conexión exitosa a Google Drive")
        return service
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Drive: {e}")
        return None

def obtener_o_crear_carpeta_imagenes(service):
    """Obtiene el ID de la carpeta IMAGENES en Drive, o la crea si no existe"""
    try:
        # Buscar carpeta por nombre
        query = "name='IMAGENES' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        folders = results.get('files', [])
        if folders:
            folder_id = folders[0]['id']
            logger.info(f"✅ Carpeta IMAGENES encontrada con ID: {folder_id}")
            return folder_id
        else:
            # Crear carpeta
            file_metadata = {
                'name': 'IMAGENES',
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = service.files().create(body=file_metadata, fields='id').execute()
            folder_id = folder.get('id')
            logger.info(f"✅ Carpeta IMAGENES creada con ID: {folder_id}")
            return folder_id
    except Exception as e:
        logger.error(f"❌ Error al obtener/crear carpeta IMAGENES: {e}")
        return None

def subir_a_drive(service, folder_id, file_path, nombre_archivo):
    """Sube un archivo a la carpeta especificada y devuelve el enlace público (o ID)"""
    try:
        file_metadata = {
            'name': nombre_archivo,
            'parents': [folder_id]
        }
        media = MediaFileUpload(file_path, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        file_id = file.get('id')
        # Opcional: hacer el archivo público (para obtener enlace directo)
        service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}).execute()
        link = f"https://drive.google.com/file/d/{file_id}/view"
        logger.info(f"✅ Archivo subido a Drive: {nombre_archivo} - ID: {file_id}")
        return link
    except Exception as e:
        logger.error(f"❌ Error al subir archivo a Drive: {e}")
        return None

def desplazar_imagenes(worksheet, fila, nueva_url):
    """Desplaza las columnas IMAGEN1 a IMAGEN15 hacia la derecha y guarda nueva en IMAGEN1"""
    # Las columnas de imágenes comienzan en la columna X (índice 24 en 1-based) hasta la AM (índice 39)
    # X = columna 24, Y=25, ..., AM=39
    # Primero, leer los valores actuales de IMAGEN1 a IMAGEN15
    imagen_cols = list(range(24, 40))  # 24 a 39 inclusive
    valores_actuales = []
    for col in imagen_cols:
        val = worksheet.cell(fila, col).value
        valores_actuales.append(val if val else "")
    # Desplazar: insertar nueva_url en la primera posición y mover los demás a la derecha
    nuevos_valores = [nueva_url] + valores_actuales[:-1]  # El último se pierde (se puede ajustar)
    # Escribir los nuevos valores
    for idx, col in enumerate(imagen_cols):
        worksheet.update_cell(fila, col, nuevos_valores[idx])
    logger.info(f"✅ Imagen desplazada correctamente en fila {fila}")

# ========== FUNCIONES DEL BOT (LAS EXISTENTES SE MANTIENEN) ==========
# ... (aquí van todas las funciones existentes: start, ayuda, registrar, obtener_llamada, etc.)
# Para no duplicar el código, se asume que ya están presentes. Solo se modificará la parte de guardar SIAC.

# ========== COMANDO GUARDAR SIAC (MODIFICADO) ==========
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
        # Guardar el folio en la columna D (índice 4)
        worksheet_data.update_cell(cliente['fila'], 4, folio)
        # Guardar el cliente actualizado en context para el siguiente paso
        context.user_data['siac_cliente_actual'] = cliente
        context.user_data['folio_asignado'] = folio
        await update.message.reply_text(f"✅ *Folio SIAC asignado correctamente*\n\nCliente: {cliente['nombre']}\nFolio: {folio}\n\nAhora envía el *documento* (imagen o PDF) para almacenar en IMAGEN1.\nPuedes enviar una foto o un archivo PDF.", parse_mode='Markdown')
        return INGRESO_DOCUMENTO
    except Exception as e:
        logger.error(f"Error al guardar folio: {e}")
        await update.message.reply_text(f"❌ Error al guardar: {e}")
        return ConversationHandler.END

async def guardar_siac_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el documento (foto o PDF) y lo guarda en Drive y en la hoja"""
    telegram_id = update.effective_user.id
    cliente = context.user_data.get('siac_cliente_actual')
    folio_asignado = context.user_data.get('folio_asignado')
    if not cliente:
        await update.message.reply_text("❌ Sesión expirada. Inicia nuevamente con /guardarSIAC.")
        return ConversationHandler.END

    # Determinar si es foto o documento
    file_id = None
    file_name = None
    if update.message.photo:
        # Es una foto
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        # Crear archivo temporal
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        temp_path = temp_file.name
        temp_file.close()
        await file.download_to_drive(temp_path)
        extension = '.jpg'
        file_name = f"{cliente['nombre']} - {datetime.now().strftime('%Y%m%d_%H%M%S')} - SIAC{extension}"
    elif update.message.document:
        # Es un documento (PDF u otro)
        doc = update.message.document
        # Verificar que sea PDF
        if doc.mime_type != 'application/pdf':
            await update.message.reply_text("❌ Solo se aceptan archivos PDF o imágenes. Por favor, envía un archivo válido.")
            return INGRESO_DOCUMENTO
        file = await context.bot.get_file(doc.file_id)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        temp_path = temp_file.name
        temp_file.close()
        await file.download_to_drive(temp_path)
        extension = '.pdf'
        file_name = f"{cliente['nombre']} - {datetime.now().strftime('%Y%m%d_%H%M%S')} - SIAC{extension}"
    else:
        await update.message.reply_text("❌ Por favor, envía una imagen o un archivo PDF.")
        return INGRESO_DOCUMENTO

    # Subir a Drive
    drive_service = obtener_drive_service()
    if not drive_service:
        await update.message.reply_text("❌ Error al conectar con Google Drive.")
        os.unlink(temp_path)
        return ConversationHandler.END
    folder_id = obtener_o_crear_carpeta_imagenes(drive_service)
    if not folder_id:
        await update.message.reply_text("❌ Error al acceder a la carpeta IMAGENES en Drive.")
        os.unlink(temp_path)
        return ConversationHandler.END
    drive_link = subir_a_drive(drive_service, folder_id, temp_path, file_name)
    os.unlink(temp_path)
    if not drive_link:
        await update.message.reply_text("❌ Error al subir el archivo a Drive.")
        return ConversationHandler.END

    # Actualizar la hoja: desplazar imágenes y guardar el enlace en IMAGEN1
    sheet = conectar_google_sheets()
    if not sheet:
        await update.message.reply_text("❌ Error al conectar con Google Sheets.")
        return ConversationHandler.END
    worksheet_data = obtener_hoja_data(sheet)
    if not worksheet_data:
        await update.message.reply_text("❌ Error al acceder a la hoja Data.")
        return ConversationHandler.END
    try:
        fila = cliente['fila']
        desplazar_imagenes(worksheet_data, fila, drive_link)
        await update.message.reply_text(f"✅ *Documento guardado exitosamente*\n\nCliente: {cliente['nombre']}\nFolio: {folio_asignado}\n\nEl documento se ha guardado en IMAGEN1 de la hoja y en Drive.", parse_mode='Markdown')
        # Limpiar datos temporales
        context.user_data.clear()
        # Eliminar la llamada de la lista de pendientes si aún existe
        if telegram_id in busqueda_siac:
            # Actualizar resultados quitando este cliente
            nuevos_resultados = [r for r in busqueda_siac[telegram_id]['resultados'] if r['fila'] != cliente['fila']]
            if nuevos_resultados:
                busqueda_siac[telegram_id]['resultados'] = nuevos_resultados
                # Volver a mostrar la página actualizada
                await mostrar_pagina_siac(update, context, telegram_id, context.user_data.get('siac_pagina', 0))
                return SELECCION_CLIENTE
            else:
                await update.message.reply_text("📭 No quedan llamadas pendientes para esta estrategia.")
                if telegram_id in busqueda_siac:
                    del busqueda_siac[telegram_id]
                return ConversationHandler.END
        else:
            return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error al guardar documento: {e}")
        await update.message.reply_text(f"❌ Error al guardar el documento: {e}")
        return ConversationHandler.END

async def guardar_siac_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if telegram_id in busqueda_siac:
        del busqueda_siac[telegram_id]
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END

# ========== FUNCIONES RESTANTES (start, ayuda, registrar, etc.) ==========
# ... (se mantienen exactamente igual que en el código anterior)
# Por razones de espacio, no se vuelven a copiar, pero deben estar presentes.

# ========== CONFIGURACIÓN Y EJECUCIÓN ==========
def main():
    logger.info("🤖 Iniciando bot...")
    logger.info(f"📊 Usando Google Sheets ID: {SPREADSHEET_ID}")
    sheet = conectar_google_sheets()
    if not sheet:
        logger.error("❌ No se pudo conectar a Google Sheets. Verifica las credenciales.")
    application = Application.builder().token(TOKEN).build()

    # Comandos básicos (ya existentes)
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
    application.add_handler(CommandHandler("crearpdf", iniciar_creacion_pdf))
    application.add_handler(CommandHandler("generarpdf", generar_pdf))
    application.add_handler(CommandHandler("cancelarpdf", cancelar_pdf))

    # Conversación para guardar SIAC (con nuevo estado INGRESO_DOCUMENTO)
    guardar_siac_conv = ConversationHandler(
        entry_points=[CommandHandler("guardarSIAC", guardar_siac_inicio)],
        states={
            ESTRATEGIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_siac_buscar)],
            SELECCION_CLIENTE: [CallbackQueryHandler(manejar_callback_siac, pattern="^siac_")],
            INGRESO_FOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_siac_folio)],
            INGRESO_DOCUMENTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, guardar_siac_documento)],
        },
        fallbacks=[CommandHandler("cancelar", guardar_siac_cancelar)],
    )
    application.add_handler(guardar_siac_conv)

    # Otros manejadores
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
