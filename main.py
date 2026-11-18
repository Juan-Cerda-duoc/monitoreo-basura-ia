import os
import cv2
import time
import math
import queue
import base64
import re
import logging
import threading
import serial
import requests
import pandas as pd
from dotenv import load_dotenv
from functools import wraps
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from ultralytics import YOLO
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from supabase import create_client, Client
from flask import Flask, Response, render_template_string, send_file, request, jsonify

# Carga las variables privadas desde el archivo local .env (ignorado por Git)
load_dotenv()

# ============================================================
# 1. CONFIGURACIÓN GENERAL, SEGURIDAD WEB Y CREDENCIALES
# ============================================================

# 🔐 Credenciales para acceder al panel desde cualquier dispositivo
USUARIO_WEB = os.getenv("USUARIO_WEB", "admin")
PASSWORD_WEB = os.getenv("PASSWORD_WEB", "CAMBIAR_PASSWORD_EN_ENV")

CAMARA_INDEX = int(os.getenv("CAMARA_INDEX", 0))
UMBRAL_BASURA = int(os.getenv("UMBRAL_BASURA", 3))
INTERVALO_ALERTA = int(os.getenv("INTERVALO_ALERTA", 15))
CAMARA_ID = os.getenv("CAMARA_ID", "Cámara 03")

# Parámetros de optimización de costos de IA
TIEMPO_PERMANENCIA_SEG = 2.5     # Segundos mínimos cerca de basura para considerar sospechoso
COOLDOWN_POR_PERSONA_SEG = 60    # No re-analizar a la misma persona (ID) durante 60s
MAX_LLAMADAS_GEMINI_HORA = 30    # Tope duro de seguridad para proteger la API Key
UMBRAL_DISTANCIA_PX = 250        # Radio de interacción persona-basura

# 🔑 Llaves de servicios externos (se leen exclusivamente desde .env)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ARDUINO_PORT = os.getenv("ARDUINO_PORT", "COM4")
ARDUINO_BAUD = int(os.getenv("ARDUINO_BAUD", 9600))

SLACK_TOKEN = os.getenv("SLACK_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")

RAW_SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_URL = RAW_SUPABASE_URL.split("/rest/v1")[0].rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

EXCEL_FILE = os.getenv("EXCEL_FILE", "data/registro_activaciones_buzzer.xlsx")
excel_lock = threading.Lock()
arduino_lock = threading.Lock()

CALLE = os.getenv("CALLE", "Avenida Providencia")
NUMERO = os.getenv("NUMERO", "1234")
COMUNA = os.getenv("COMUNA", "Providencia")
UBICACION = f"{CALLE} {NUMERO}, {COMUNA}"

# ============================================================
# 2. INICIALIZACIÓN DE CLIENTES, LOGS Y ARDUINO
# ============================================================

class FiltroLogsEstado(logging.Filter):
    def filter(self, record):
        return "/api/estado" not in record.getMessage()

logging.getLogger("werkzeug").addFilter(FiltroLogsEstado())

slack = WebClient(token=SLACK_TOKEN) if SLACK_TOKEN else None
supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if (SUPABASE_URL and SUPABASE_KEY)
    else None
)
app = Flask(__name__)


def requiere_autenticacion(f):
    """Protege el panel, el video y los endpoints del broker con usuario/contraseña o token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-Broker-Token") == PASSWORD_WEB:
            return f(*args, **kwargs)

        auth = request.authorization
        if not auth or auth.username != USUARIO_WEB or auth.password != PASSWORD_WEB:
            return Response(
                "Acceso restringido. Ingresa usuario y contraseña del sistema de monitoreo.",
                401,
                {"WWW-Authenticate": 'Basic realm="Panel de Monitoreo IA"'}
            )
        return f(*args, **kwargs)
    return decorated


def conectar_arduino():
    try:
        ser = serial.Serial(ARDUINO_PORT, ARDUINO_BAUD, timeout=1)
        time.sleep(2)  # Esperar estabilización tras apertura de puerto
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        print(f"🔌 Arduino conectado correctamente en {ARDUINO_PORT}")
        return ser
    except Exception as e:
        print(f"⚠️ No se pudo conectar al Arduino en {ARDUINO_PORT}: {e}")
        return None


arduino = conectar_arduino()


def _ejecutar_registro_excel(evento="BUZZER_ACTIVADO", detalle="Infracción detectada"):
    with excel_lock:
        carpeta_excel = os.path.dirname(EXCEL_FILE)
        if carpeta_excel:
            os.makedirs(carpeta_excel, exist_ok=True)

        fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df_nuevo = pd.DataFrame([{
            "Fecha y Hora": fecha_hora,
            "Evento": evento,
            "Cámara ID": CAMARA_ID,
            "Ubicación": UBICACION,
            "Detalle / Origen": detalle
        }])

        if os.path.exists(EXCEL_FILE):
            try:
                df_existente = pd.read_excel(EXCEL_FILE)
                df_final = pd.concat([df_existente, df_nuevo], ignore_index=True)
            except Exception:
                df_final = df_nuevo
        else:
            df_final = df_nuevo

        try:
            df_final.to_excel(EXCEL_FILE, index=False)
            print(f"📊 [WORKER-HW] Excel actualizado ({fecha_hora})")
        except Exception as e:
            print(f"❌ [WORKER-HW] Error al guardar Excel: {e}")


def _ejecutar_buzzer_arduino(origen="Infracción confirmada por IA", comando="1"):
    """Envía '1\\n' (encender) o '0\\n' (apagar) por Serial al Arduino y registra en Excel."""
    global arduino
    es_apagado = str(comando).strip() == "0"
    cmd_bytes = b'0\n' if es_apagado else b'1\n'
    cmd_texto = "0\\n" if es_apagado else "1\\n"
    evento_excel = "BUZZER_APAGADO" if es_apagado else "BUZZER_ACTIVADO"

    with arduino_lock:
        if arduino is None or not arduino.is_open:
            arduino = conectar_arduino()

        if arduino and arduino.is_open:
            try:
                arduino.reset_input_buffer()
                arduino.write(cmd_bytes)
                arduino.flush()
                print(f"🔔 [WORKER-HW] Comando '{cmd_texto}' enviado a Arduino ({origen})")

                time.sleep(0.15)
                if arduino.in_waiting > 0:
                    respuesta_ard = arduino.readline().decode('utf-8', errors='ignore').strip()
                    print(f"📟 [ARDUINO RESPONDIÓ]: {respuesta_ard}")
            except Exception as e:
                print(f"❌ [WORKER-HW] Error Serial Arduino: {e}")
                arduino = None
        else:
            print("⚠️ [WORKER-HW] Arduino no conectado (registrando evento igualmente).")

    _ejecutar_registro_excel(evento=evento_excel, detalle=origen)

# ============================================================
# 3. CAPTURA DE CÁMARA DE ALTA VELOCIDAD (SIN BLOQUEOS)
# ============================================================

class CapturadorCamara:
    def __init__(self, src=0):
        self.cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(src)

        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self.ret, self.frame = self.cap.read()
        self.frame_id = 0
        self.running = True
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._actualizar, daemon=True)
        self.thread.start()

    def _actualizar(self):
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.ret = ret
                    self.frame = frame
                    self.frame_id += 1
            else:
                time.sleep(0.005)

    def obtener_frame(self):
        with self.lock:
            if self.ret and self.frame is not None:
                return True, self.frame.copy(), self.frame_id
            return False, None, 0

    def detener(self):
        self.running = False
        if self.cap.isOpened():
            self.cap.release()


print("📷 Iniciando captura de cámara en hilo dedicado...")
camara_global = CapturadorCamara(CAMARA_INDEX)

# ============================================================
# 4. FILTRO INTELIGENTE ANTI-GASTO DE API
# ============================================================

class FiltroInteligenteInfraccion:
    def __init__(self, umbral_distancia=250):
        self.umbral_distancia = umbral_distancia
        self.personas_rastreadas = {}
        self.historial_analizados = {}
        self.llamadas_ultima_hora = deque()
        self.llamadas_evitadas = 0
        self.llamadas_realizadas = 0

    def _centro(self, box):
        x1, y1, x2, y2 = box
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    def _limpiar_registros_antiguos(self, ahora):
        ids_expirados = [
            pid for pid, data in self.personas_rastreadas.items()
            if ahora - data["ultima_vez_visto"] > 8.0
        ]
        for pid in ids_expirados:
            del self.personas_rastreadas[pid]

        while self.llamadas_ultima_hora and ahora - self.llamadas_ultima_hora[0] > 3600:
            self.llamadas_ultima_hora.popleft()

    def evaluar_candidatos(self, personas_con_id, boxes_basura):
        ahora = time.monotonic()
        self._limpiar_registros_antiguos(ahora)
        cant_basura = len(boxes_basura)

        if not personas_con_id or cant_basura == 0:
            return False, "Sin interacción", 0

        personas_cerca = 0

        for pid, box_p in personas_con_id:
            centro_p = self._centro(box_p)
            cerca = any(
                math.hypot(centro_p[0] - self._centro(b_b)[0], centro_p[1] - self._centro(b_b)[1]) <= self.umbral_distancia
                for b_b in boxes_basura
            )

            if not cerca:
                continue

            personas_cerca += 1
            alto_box = max(1.0, box_p[3] - box_p[1])

            if pid not in self.personas_rastreadas:
                self.personas_rastreadas[pid] = {
                    "primer_contacto": ahora,
                    "ultima_vez_visto": ahora,
                    "basura_inicial": cant_basura,
                    "alto_inicial": alto_box
                }
                self.llamadas_evitadas += 1
                continue

            estado = self.personas_rastreadas[pid]
            estado["ultima_vez_visto"] = ahora
            tiempo_cerca = ahora - estado["primer_contacto"]

            if ahora - self.historial_analizados.get(pid, 0) < COOLDOWN_POR_PERSONA_SEG:
                continue

            if len(self.llamadas_ultima_hora) >= MAX_LLAMADAS_GEMINI_HORA:
                return False, "Cuota horaria máxima alcanzada", personas_cerca

            if cant_basura > estado["basura_inicial"] and tiempo_cerca >= 0.8:
                self.historial_analizados[pid] = ahora
                self.llamadas_ultima_hora.append(ahora)
                self.llamadas_realizadas += 1
                return True, f"Aumento de basura junto a ID #{pid}", personas_cerca

            if alto_box < (estado["alto_inicial"] * 0.75) and tiempo_cerca >= 1.0:
                self.historial_analizados[pid] = ahora
                self.llamadas_ultima_hora.append(ahora)
                self.llamadas_realizadas += 1
                return True, f"Cambio de postura (agachado) ID #{pid}", personas_cerca

            if tiempo_cerca >= TIEMPO_PERMANENCIA_SEG:
                self.historial_analizados[pid] = ahora
                self.llamadas_ultima_hora.append(ahora)
                self.llamadas_realizadas += 1
                return True, f"Permanencia sospechosa ({tiempo_cerca:.1f}s) ID #{pid}", personas_cerca

            self.llamadas_evitadas += 1

        return False, "Transeúnte en movimiento (filtrado)", personas_cerca

# ============================================================
# 5. VALIDACIÓN POR IA EN SECUENCIA (GEMINI)
# ============================================================

def obtener_modelos_gemini_activos():
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY no configurada en .env.")
        return ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

    url_list = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        res = requests.get(url_list, timeout=8)
        if res.status_code == 200:
            modelos_validos = [
                m.get("name", "").replace("models/", "")
                for m in res.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and ("flash" in m.get("name", "") or "pro" in m.get("name", ""))
            ]
            if modelos_validos:
                print(f"📋 Modelos Gemini disponibles: {modelos_validos}")
                return modelos_validos
        else:
            print(f"⚠️ GEMINI_API_KEY aún no configurada o inválida (HTTP {res.status_code}).")
    except Exception as e:
        print(f"⚠️ No se pudo conectar con Google Gemini al iniciar: {e}")

    return ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]


MODELOS_GEMINI_CACHE = obtener_modelos_gemini_activos()


def analizar_secuencia_con_gemini(secuencia_frames):
    if not GEMINI_API_KEY:
        return False, "ERROR API GEMINI: Falta configurar GEMINI_API_KEY en .env", "0.0%"

    prompt_instruccion = (
        "Se te presentan 3 fotogramas consecutivos tomados en orden cronológico corto (intervalo de 0.5s).\n"
        "Evalúa el movimiento de la persona entre imágenes para un sistema de fiscalización y monitoreo de aseo.\n"
        "REGLAS OBLIGATORIAS:\n"
        "1. Evalúa la acción en CUALQUIER entorno (interior, oficina, exterior, etc.).\n"
        "2. Responde 'RESPUESTA: SI' si observas CUALQUIERA de estos comportamientos:\n"
        "   - Volcar, dar vuelta, inclinar o vaciar un tambor, contenedor, papelera o bolsa de basura.\n"
        "   - Derramar o vaciar el contenido de un recipiente hacia el suelo.\n"
        "   - Soltar, tirar, dejar o abandonar residuos en el piso.\n"
        "   - Agacharse para depositar basura en el suelo.\n"
        "Responde en la primera línea exactamente: RESPUESTA: SI o RESPUESTA: NO\n"
        "En la segunda línea: CERTEZA: XX%\n"
        "En la tercera línea: Describe brevemente la acción observada."
    )

    parts = [{"text": prompt_instruccion}]
    for f in secuencia_frames:
        f_resized = cv2.resize(f, (640, 360))
        _, buffer = cv2.imencode('.jpg', f_resized, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        b64_img = base64.b64encode(buffer).decode('utf-8')
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64_img}})

    payload = {"contents": [{"parts": parts}]}
    respuesta_texto = None
    ultimo_error = "Sin respuesta de los modelos"

    for modelo in MODELOS_GEMINI_CACHE:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, json=payload, timeout=15)
            if res.status_code == 200:
                candidates = res.json().get("candidates", [])
                if candidates:
                    parts_resp = candidates[0].get("content", {}).get("parts", [])
                    if parts_resp:
                        respuesta_texto = parts_resp[0].get("text", "").strip()
                        print(f"\n🤖 [DICTAMEN GEMINI - {modelo}]:\n{respuesta_texto}\n" + "-" * 45)
                        break
            else:
                try:
                    detalle_err = res.json().get("error", {}).get("message", res.text[:150])
                except Exception:
                    detalle_err = res.text[:150]
                ultimo_error = f"HTTP {res.status_code} ({modelo}): {detalle_err}"
                print(f"❌ [WORKER-IA] {ultimo_error}")
        except Exception as e:
            ultimo_error = f"Excepción ({modelo}): {e}"
            print(f"⚠️ [WORKER-IA] {ultimo_error}")

    if not respuesta_texto:
        return False, f"ERROR API GEMINI: {ultimo_error}", "0.0%"

    respuesta_norm = respuesta_texto.upper().replace("SÍ", "SI")
    primera_linea = respuesta_norm.split("\n")[0]
    es_infractor = "RESPUESTA: SI" in primera_linea or ("SI" in primera_linea and "NO" not in primera_linea)

    match_porcentaje = re.search(r'(\d+)\s*%', respuesta_texto)
    confianza = f"{float(match_porcentaje.group(1)):.1f}%" if match_porcentaje else "85.0%"

    return es_infractor, respuesta_texto, confianza

# ============================================================
# 6. BROKER ASÍNCRONO CON COLAS Y WORKERS DEDICADOS
# ============================================================

class BrokerAsincrono:
    def __init__(self):
        self.lock_estado = threading.Lock()
        self.frame_jpeg_actual = None
        self.historial_eventos = deque(maxlen=50)

        self.detecciones_cache = {
            "boxes_basura": [],
            "personas_con_id": [],
            "estado_txt": "INICIANDO SISTEMA...",
            "color_txt": (0, 255, 0),
            "cant_basura": 0,
            "cant_personas": 0,
            "ia_calls": 0
        }

        self.metricas = {
            "estado": "INICIANDO",
            "basura_detectada": 0,
            "personas_detectadas": 0,
            "personas_cerca": 0,
            "ultimo_filtro": "Ninguno",
            "ultimo_dictamen_ia": "Esperando primer análisis de secuencia...",
            "ultimo_resultado_ia": "PENDIENTE",
            "gemini_llamadas_realizadas": 0,
            "gemini_llamadas_evitadas": 0,
            "tareas_en_cola": 0,
            "arduino_conectado": bool(arduino and arduino.is_open),
            "ultima_actualizacion": None
        }

        self.cola_hardware = queue.Queue(maxsize=100)
        self.cola_nube = queue.Queue(maxsize=100)
        self.cola_gemini = queue.Queue(maxsize=2)

        threading.Thread(target=self._worker_hardware, daemon=True, name="Worker-Hardware").start()
        for i in range(2):
            threading.Thread(target=self._worker_nube, daemon=True, name=f"Worker-Nube-{i+1}").start()
        threading.Thread(target=self._worker_gemini, daemon=True, name="Worker-Gemini").start()

    def solicitar_validacion_gemini(self, frame_anotado, motivo_disparo):
        try:
            self.cola_gemini.put_nowait({
                "frame_anotado": frame_anotado,
                "motivo": motivo_disparo
            })
        except queue.Full:
            pass

    def encolar_buzzer(self, origen="API_HTTP", comando="1"):
        try:
            self.cola_hardware.put_nowait({"origen": origen, "comando": comando})
            return True
        except queue.Full:
            return False

    def actualizar_dictamen_ia(self, dictamen, es_infractor):
        with self.lock_estado:
            hora = datetime.now().strftime("%H:%M:%S")
            self.metricas["ultimo_dictamen_ia"] = f"[{hora}] {dictamen}"
            if dictamen.startswith("ERROR API"):
                self.metricas["ultimo_resultado_ia"] = "ERROR"
            elif es_infractor:
                self.metricas["ultimo_resultado_ia"] = "INFRACCION"
            else:
                self.metricas["ultimo_resultado_ia"] = "DESCARTADO"

    def publicar_evento(self, tipo_alerta, confianza_str, frame_anotado=None, dictamen_ia="", origen="VISION_LOCAL"):
        # Disparo automático del buzzer cuando se confirma infracción
        if tipo_alerta == "INFRACCION":
            self.encolar_buzzer(origen=f"{origen}: {dictamen_ia[:60]}", comando="1")

        tarea_nube = {
            "tipo_alerta": tipo_alerta,
            "confianza_str": confianza_str,
            "frame_anotado": frame_anotado,
            "dictamen_ia": dictamen_ia,
            "origen": origen,
            "fecha": datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        }
        try:
            self.cola_nube.put_nowait(tarea_nube)
        except queue.Full:
            print("⚠️ Cola de nube llena, evento descartado.")

        return {
            "fecha": tarea_nube["fecha"],
            "tipo": tipo_alerta,
            "origen": origen,
            "estado": "Encolado en segundo plano"
        }

    def _worker_hardware(self):
        while True:
            tarea = self.cola_hardware.get()
            try:
                _ejecutar_buzzer_arduino(
                    origen=tarea.get("origen", "Sistema"),
                    comando=tarea.get("comando", "1")
                )
            except Exception as e:
                print(f"❌ Error en Worker-Hardware: {e}")
            finally:
                self.cola_hardware.task_done()

    def _worker_gemini(self):
        while True:
            tarea = self.cola_gemini.get()
            try:
                print(f"🧠 [WORKER-IA] Capturando secuencia de 3 fotos por: {tarea['motivo']}...")
                secuencia_frames = []
                for _ in range(3):
                    ret, f, _ = camara_global.obtener_frame()
                    if ret and f is not None:
                        secuencia_frames.append(f)
                    time.sleep(0.5)

                if len(secuencia_frames) == 3:
                    es_infractor, dictamen, confianza_ia = analizar_secuencia_con_gemini(secuencia_frames)
                    self.actualizar_dictamen_ia(dictamen, es_infractor)

                    if es_infractor:
                        self.publicar_evento(
                            tipo_alerta="INFRACCION",
                            confianza_str=confianza_ia,
                            frame_anotado=tarea["frame_anotado"],
                            dictamen_ia=dictamen,
                            origen=f"IA_GEMINI ({tarea['motivo']})"
                        )
                    else:
                        print(f"ℹ️ [WORKER-IA] Resultado negativo/error ({tarea['motivo']}) -> {dictamen}")
            except Exception as e:
                print(f"❌ Error inesperado en Worker-Gemini: {e}")
            finally:
                self.cola_gemini.task_done()

    def _worker_nube(self):
        while True:
            tarea = self.cola_nube.get()
            try:
                tipo_alerta = tarea["tipo_alerta"]
                confianza_str = tarea["confianza_str"]
                frame_anotado = tarea["frame_anotado"]
                dictamen_ia = tarea["dictamen_ia"]
                origen = tarea["origen"]
                fecha_actual = tarea["fecha"]

                id_alerta = None
                if supabase:
                    try:
                        data = {
                            "tipo": tipo_alerta,
                            "ubicacion": UBICACION,
                            "camara_id": CAMARA_ID,
                            "confianza": confianza_str,
                            "dictamen_ia": dictamen_ia,
                            "estado": "Pendiente"
                        }
                        response = supabase.table("alertas_monitoreo").insert(data).execute()
                        if response.data:
                            id_alerta = response.data[0]["id"]
                    except Exception as e:
                        print(f"❌ [WORKER-NUBE] Error Supabase: {e}")

                id_alerta = id_alerta or int(time.time())

                with self.lock_estado:
                    self.historial_eventos.appendleft({
                        "id": id_alerta,
                        "fecha": fecha_actual,
                        "tipo": tipo_alerta,
                        "origen": origen,
                        "confianza": confianza_str,
                        "dictamen": dictamen_ia
                    })

                if slack and SLACK_CHANNEL:
                    direccion_maps = quote(UBICACION)
                    mapa_url = f"https://www.google.com/maps/search/?api=1&query={direccion_maps}"
                    encabezado = "🚨 *NUEVO REGISTRO DE INFRACCIÓN* 🚨" if tipo_alerta == "INFRACCION" else "⚠️ *ALERTA DE ACUMULACIÓN / EVENTO* ⚠️"

                    mensaje = (
                        f"{encabezado}\n```"
                        f"ID:           {id_alerta}\n"
                        f"Fecha:        {fecha_actual}\n"
                        f"Tipo:         {tipo_alerta}\n"
                        f"Origen:       {origen}\n"
                        f"Ubicación:    {CAMARA_ID} ({UBICACION})\n"
                        f"Confianza IA: {confianza_str}\n"
                        f"```\n"
                        f"🗺️ <{mapa_url}|Ver dirección exacta en Google Maps>"
                    )
                    if dictamen_ia:
                        mensaje += f"\n🤖 *Detalle:* _{dictamen_ia}_"

                    if frame_anotado is not None:
                        nombre_imagen = f"evidencia_{id_alerta}_{threading.get_ident()}.jpg"
                        cv2.imwrite(nombre_imagen, frame_anotado)
                        try:
                            slack.files_upload_v2(
                                channel=SLACK_CHANNEL,
                                file=nombre_imagen,
                                title=f"Evidencia #{id_alerta}",
                                initial_comment=mensaje
                            )
                        finally:
                            if os.path.exists(nombre_imagen):
                                os.remove(nombre_imagen)
                    else:
                        slack.chat_postMessage(channel=SLACK_CHANNEL, text=mensaje)

                print(f"✅ [WORKER-NUBE] Evento ID {id_alerta} ({tipo_alerta}) procesado.")
            except Exception as e:
                print(f"❌ [WORKER-NUBE] Error enviando alerta: {e}")
            finally:
                self.cola_nube.task_done()

    def actualizar_detecciones(self, nuevas_detecciones, nuevas_metricas):
        with self.lock_estado:
            self.detecciones_cache.update(nuevas_detecciones)
            self.metricas.update(nuevas_metricas)
            self.metricas["arduino_conectado"] = bool(arduino and arduino.is_open)
            self.metricas["tareas_en_cola"] = (
                self.cola_hardware.qsize() + self.cola_nube.qsize() + self.cola_gemini.qsize()
            )
            self.metricas["ultima_actualizacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def obtener_detecciones(self):
        with self.lock_estado:
            return dict(self.detecciones_cache)

    def actualizar_frame_jpeg(self, frame_jpeg):
        with self.lock_estado:
            self.frame_jpeg_actual = frame_jpeg

    def obtener_frame_jpeg(self):
        with self.lock_estado:
            return self.frame_jpeg_actual

    def obtener_snapshot_estado(self):
        with self.lock_estado:
            return {
                "camara_id": CAMARA_ID,
                "ubicacion": UBICACION,
                "metricas": dict(self.metricas),
                "ultimos_eventos": list(self.historial_eventos)
            }


broker = BrokerAsincrono()

# ============================================================
# 7. HILOS DE VISIÓN IA Y RENDERIZADO FLUIDO (30 FPS)
# ============================================================

pesos_encontrados = list(Path("runs").glob("**/best.pt"))
if not pesos_encontrados and Path("best.pt").exists():
    pesos_encontrados = [Path("best.pt")]

if not pesos_encontrados:
    print("❌ ERROR: No se encontró 'best.pt'.")
    exit()

modelo_taco_path = max(pesos_encontrados, key=lambda p: p.stat().st_mtime)
print("Cargando modelos YOLO...")
model_taco = YOLO(str(modelo_taco_path))
model_coco = YOLO("yolov8n.pt")
print("✅ Modelos listos.")

filtro_infraccion = FiltroInteligenteInfraccion(umbral_distancia=UMBRAL_DISTANCIA_PX)


def dibujar_hud_sobre_frame(frame, det):
    for (x1, y1, x2, y2) in det["boxes_basura"]:
        cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 215, 255), 2)
        cv2.putText(frame, "Basura", (int(x1), max(15, int(y1) - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 215, 255), 2)

    for (pid, (x1, y1, x2, y2)) in det["personas_con_id"]:
        cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (255, 144, 30), 2)
        cv2.putText(frame, f"Persona #{pid}", (int(x1), max(15, int(y1) - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 144, 30), 2)

    color_txt = det["color_txt"]
    cv2.putText(
        frame,
        f"Basura: {det['cant_basura']} | Personas: {det['cant_personas']} | IA Calls: {det['ia_calls']}",
        (20, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        color_txt,
        2
    )
    cv2.putText(frame, det["estado_txt"], (20, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color_txt, 2)
    return frame


def bucle_renderizado_fluido():
    ultimo_id_procesado = -1
    while True:
        ret, frame, frame_id = camara_global.obtener_frame()
        if not ret or frame is None or frame_id == ultimo_id_procesado:
            time.sleep(0.01)
            continue

        ultimo_id_procesado = frame_id
        det = broker.obtener_detecciones()
        frame_visual = dibujar_hud_sobre_frame(frame, det)

        ret_code, buffer = cv2.imencode('.jpg', frame_visual, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if ret_code:
            broker.actualizar_frame_jpeg(buffer.tobytes())

        time.sleep(0.025)


def bucle_motor_vision():
    ultima_alerta_acumulacion = 0
    ultima_alerta_infraccion = 0
    ultimo_frame_analizado = -1

    while True:
        ret, frame, frame_id = camara_global.obtener_frame()
        if not ret or frame is None or frame_id == ultimo_frame_analizado:
            time.sleep(0.01)
            continue

        ultimo_frame_analizado = frame_id

        results_taco = model_taco(frame, verbose=False, conf=0.25)
        results_coco = model_coco.track(
            frame,
            classes=[0],
            persist=True,
            tracker="bytetrack.yaml",
            verbose=False,
            conf=0.45
        )

        boxes_basura = [b.xyxy[0].tolist() for b in results_taco[0].boxes]
        conf_basura = [b.conf[0].item() for b in results_taco[0].boxes]

        personas_con_id = []
        if results_coco[0].boxes is not None:
            for idx, b in enumerate(results_coco[0].boxes):
                box = b.xyxy[0].tolist()
                track_id = int(b.id[0].item()) if b.id is not None else (10000 + idx)
                personas_con_id.append((track_id, box))

        cant_basura = len(boxes_basura)
        cant_personas = len(personas_con_id)
        promedio_conf = (sum(conf_basura) / len(conf_basura) * 100) if conf_basura else 0.0

        tiempo_actual = time.monotonic()

        debe_llamar_ia, motivo_filtro, personas_cerca = filtro_infraccion.evaluar_candidatos(
            personas_con_id, boxes_basura
        )

        if personas_cerca > 0:
            estado_txt, color_txt = f"MONITOREANDO: {motivo_filtro}", (0, 165, 255)
        elif cant_basura >= UMBRAL_BASURA:
            estado_txt, color_txt = "ESTADO: ACUMULACION DETECTADA", (0, 255, 255)
        else:
            estado_txt, color_txt = "ESTADO: NORMAL", (0, 255, 0)

        nuevas_detecciones = {
            "boxes_basura": boxes_basura,
            "personas_con_id": personas_con_id,
            "estado_txt": estado_txt,
            "color_txt": color_txt,
            "cant_basura": cant_basura,
            "cant_personas": cant_personas,
            "ia_calls": filtro_infraccion.llamadas_realizadas
        }

        broker.actualizar_detecciones(
            nuevas_detecciones,
            {
                "estado": estado_txt,
                "basura_detectada": cant_basura,
                "personas_detectadas": cant_personas,
                "personas_cerca": personas_cerca,
                "ultimo_filtro": motivo_filtro,
                "gemini_llamadas_realizadas": filtro_infraccion.llamadas_realizadas,
                "gemini_llamadas_evitadas": filtro_infraccion.llamadas_evitadas
            }
        )

        if cant_basura >= UMBRAL_BASURA and (tiempo_actual - ultima_alerta_acumulacion >= INTERVALO_ALERTA):
            frame_evidencia = dibujar_hud_sobre_frame(frame.copy(), nuevas_detecciones)
            broker.publicar_evento(
                tipo_alerta="ACUMULACION",
                confianza_str=f"{promedio_conf:.1f}%",
                frame_anotado=frame_evidencia,
                dictamen_ia="Acumulación de residuos detectada",
                origen="YOLO_TACO"
            )
            ultima_alerta_acumulacion = tiempo_actual

        if debe_llamar_ia and (tiempo_actual - ultima_alerta_infraccion >= INTERVALO_ALERTA):
            frame_evidencia = dibujar_hud_sobre_frame(frame.copy(), nuevas_detecciones)
            broker.solicitar_validacion_gemini(frame_evidencia, motivo_filtro)
            ultima_alerta_infraccion = tiempo_actual

# ============================================================
# 8. RUTAS FLASK PROTEGIDAS (BROKER REST + INTERFAZ WEB)
# ============================================================

HTML_INTERFACE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Broker Asíncrono de Monitoreo IA</title>
    <style>
        body { font-family: Arial, sans-serif; background-color: #121212; color: #ffffff; text-align: center; margin: 0; padding: 20px; }
        h1 { color: #00e676; margin-bottom: 15px; }
        .dashboard { display: flex; flex-wrap: wrap; justify-content: center; gap: 20px; max-width: 1250px; margin: auto; }
        .stream-container { background-color: #1e1e1e; padding: 15px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.6); flex: 2; min-width: 320px; }
        .stats-container { background-color: #1e1e1e; padding: 20px; border-radius: 12px; text-align: left; flex: 1; min-width: 300px; }
        img { width: 100%; border-radius: 8px; }
        .stat-box { background: #2a2a2a; padding: 10px 14px; border-radius: 8px; margin-bottom: 10px; }
        .stat-label { font-size: 12px; color: #aaa; margin-bottom: 4px; }
        .stat-val { font-size: 16px; font-weight: bold; color: #00e676; }
        .ia-box { background: #1a2634; border-left: 4px solid #00b0ff; padding: 12px; border-radius: 8px; margin-top: 10px; white-space: pre-wrap; font-size: 13px; color: #e0f7fa; }
        .btn-container { margin-top: 20px; display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; }
        .btn { padding: 12px 20px; font-size: 15px; font-weight: bold; text-decoration: none; border-radius: 8px; border: none; cursor: pointer; color: #fff; transition: opacity 0.2s; }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .btn-download { background-color: #1f9d55; }
        .btn-buzzer { background-color: #d9534f; }
        .btn-buzzer-off { background-color: #4b5563; }
    </style>
</head>
<body>
    <h1>📹 Broker Central de Monitoreo Remoto</h1>
    <div class="dashboard">
        <div class="stream-container">
            <img src="/video_feed" alt="Transmisión en vivo">
        </div>
        <div class="stats-container">
            <h3>📊 Telemetría del Broker</h3>
            <div class="stat-box"><div class="stat-label">Estado Actual</div><div class="stat-val" id="estado">Cargando...</div></div>
            <div class="stat-box"><div class="stat-label">Llamadas Gemini Realizadas / Ahorradas</div><div class="stat-val" id="gemini_stats">0 / 0</div></div>
            <div class="stat-box"><div class="stat-label">Basura / Personas en Escena</div><div class="stat-val" id="conteos">0 / 0</div></div>
            <div class="stat-box"><div class="stat-label">Última Decisión del Filtro</div><div class="stat-val" style="font-size:14px;" id="filtro">-</div></div>
            <div class="ia-box" id="ia_dictamen_box">
                <div class="stat-label" style="color:#80d8ff;">🤖 Última Respuesta de Gemini IA</div>
                <div id="ia_dictamen">Esperando primer análisis...</div>
            </div>
        </div>
    </div>
    <div class="btn-container">
        <a href="/descargar_excel" class="btn btn-download">📊 Descargar Excel</a>
        <button id="btnBuzzer" onclick="activarBuzzerManual()" class="btn btn-buzzer">🔔 Encender Buzzer Manualmente</button>
        <button id="btnBuzzerOff" onclick="apagarBuzzerManual()" class="btn btn-buzzer-off">🔇 Apagar Buzzer Manualmente</button>
    </div>
    <script>
        setInterval(async () => {
            try {
                const res = await fetch('/api/estado', { credentials: 'same-origin' });
                const data = await res.json();
                const m = data.metricas;
                document.getElementById('estado').innerText = m.estado;
                document.getElementById('gemini_stats').innerText = `${m.gemini_llamadas_realizadas} realizadas / ${m.gemini_llamadas_evitadas} ahorradas`;
                document.getElementById('conteos').innerText = `${m.basura_detectada} residuos / ${m.personas_detectadas} personas`;
                document.getElementById('filtro').innerText = m.ultimo_filtro;
                document.getElementById('ia_dictamen').innerText = m.ultimo_dictamen_ia;

                const box = document.getElementById('ia_dictamen_box');
                if (m.ultimo_resultado_ia === 'INFRACCION') {
                    box.style.borderLeftColor = '#ff5252';
                    box.style.background = '#331a1a';
                } else if (m.ultimo_resultado_ia === 'ERROR') {
                    box.style.borderLeftColor = '#ffab40';
                    box.style.background = '#33261a';
                } else {
                    box.style.borderLeftColor = '#00b0ff';
                    box.style.background = '#1a2634';
                }
            } catch (e) {}
        }, 1500);

        async function activarBuzzerManual() {
            const btn = document.getElementById('btnBuzzer');
            const textoOriginal = btn.innerText;
            btn.disabled = true;
            btn.innerText = '⏳ Enviando orden al Arduino...';

            try {
                const res = await fetch('/api/broker/buzzer', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({origen: 'Encendido Manual desde Panel Web'})
                });
                const data = await res.json();
                if (res.ok && data.ok) {
                    btn.innerText = '✅ ¡Buzzer Activado!';
                } else {
                    btn.innerText = '⚠️ Error al encolar orden';
                }
            } catch (e) {
                btn.innerText = '❌ Error de conexión';
            } finally {
                setTimeout(() => {
                    btn.innerText = textoOriginal;
                    btn.disabled = false;
                }, 2000);
            }
        }

        async function apagarBuzzerManual() {
            const btn = document.getElementById('btnBuzzerOff');
            const textoOriginal = btn.innerText;
            btn.disabled = true;
            btn.innerText = '⏳ Apagando buzzer...';

            try {
                const res = await fetch('/api/broker/buzzer/off', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({origen: 'Apagado Manual desde Panel Web'})
                });
                const data = await res.json();
                if (res.ok && data.ok) {
                    btn.innerText = '✅ ¡Buzzer Apagado!';
                } else {
                    btn.innerText = '⚠️ Error al encolar orden';
                }
            } catch (e) {
                btn.innerText = '❌ Error de conexión';
            } finally {
                setTimeout(() => {
                    btn.innerText = textoOriginal;
                    btn.disabled = false;
                }, 2000);
            }
        }
    </script>
</body>
</html>
"""

@app.route('/')
@requiere_autenticacion
def index():
    return render_template_string(HTML_INTERFACE)


@app.route('/video_feed')
@requiere_autenticacion
def video_feed():
    def stream():
        while True:
            frame_bytes = broker.obtener_frame_jpeg()
            if frame_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.03)

    return Response(stream(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/estado', methods=['GET'])
@requiere_autenticacion
def api_estado():
    return jsonify(broker.obtener_snapshot_estado()), 200


@app.route('/api/broker/evento', methods=['POST'])
@requiere_autenticacion
def api_publicar_evento():
    datos = request.get_json(silent=True) or {}
    tipo = datos.get("tipo", "EVENTO_EXTERNO").upper()
    origen = datos.get("origen", "API_EXTERNA")
    confianza = datos.get("confianza", "N/A")
    detalle = datos.get("detalle", "Evento recibido vía Broker HTTP")

    registro = broker.publicar_evento(
        tipo_alerta=tipo,
        confianza_str=confianza,
        frame_anotado=None,
        dictamen_ia=detalle,
        origen=origen
    )
    return jsonify({"ok": True, "evento_encolado": registro}), 202


@app.route('/api/broker/buzzer', methods=['POST'])
@requiere_autenticacion
def api_activar_buzzer():
    datos = request.get_json(silent=True) or {}
    origen = datos.get("origen", "Encendido Manual Web")
    encolado = broker.encolar_buzzer(origen=origen, comando="1")
    return jsonify({"ok": encolado, "estado": "Orden de encendido encolada en segundo plano"}), 202


@app.route('/api/broker/buzzer/off', methods=['POST'])
@requiere_autenticacion
def api_apagar_buzzer():
    datos = request.get_json(silent=True) or {}
    origen = datos.get("origen", "Apagado Manual Web")
    encolado = broker.encolar_buzzer(origen=origen, comando="0")
    return jsonify({"ok": encolado, "estado": "Orden de apagado encolada en segundo plano"}), 202


@app.route('/descargar_excel')
@requiere_autenticacion
def descargar_excel():
    with excel_lock:
        if os.path.exists(EXCEL_FILE):
            return send_file(EXCEL_FILE, as_attachment=True, download_name="registro_activaciones_buzzer.xlsx")
    return "<h2 style='color:red; text-align:center;'>⚠️ Aún no hay registros en Excel.</h2><br><a href='/'>Volver</a>", 404


if __name__ == '__main__':
    threading.Thread(target=bucle_renderizado_fluido, daemon=True, name="Hilo-Stream-30FPS").start()
    threading.Thread(target=bucle_motor_vision, daemon=True, name="Hilo-YOLO-IA").start()

    print("🚀 Broker Asíncrono iniciado en http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)