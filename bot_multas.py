import os
import json
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from PIL import Image
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from playwright.sync_api import sync_playwright

# --- 1. CARGA DE VARIABLES Y SECRETOS DE ENTORNO ---
API_KEY_CAPTCHA = os.environ["API_KEY_CAPTCHA"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_PASS"]
EMAIL_DESTINO = os.environ["EMAIL_DESTINO"]
GOOGLE_CREDS_JSON = json.loads(os.environ["GOOGLE_CREDS_JSON"])
BASE_URL_WEB = (
    os.environ.get("PAGES_URL")
    or os.environ.get("GITHUB_PAGES_URL")
    or ""
).strip().rstrip("/")

# Carpeta para almacenar PDFs que generará el bot para la Web App
os.makedirs("docs/pdf", exist_ok=True)

# --- 2. CONEXIÓN A GOOGLE SHEETS ---
NOMBRE_HOJA = "Gestión_Vehicular-Control_Flotilla"
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(GOOGLE_CREDS_JSON, scope)
client = gspread.authorize(creds)
sheet = client.open(NOMBRE_HOJA)
hoja_placas = sheet.worksheet("Placas")
hoja_historial = sheet.worksheet("Historial")

alertas_del_dia = []

# --- 3. FUNCIONES AUXILIARES DE CONVERSIÓN Y CAPTCHA ---
def captura_a_pdf(page, nombre_archivo_pdf):
    """Toma una captura completa de la pantalla del portal y la convierte a documento PDF."""
    ruta_png = f"temp_{nombre_archivo_pdf}.png"
    ruta_pdf = os.path.join("docs/pdf", nombre_archivo_pdf)
    page.screenshot(path=ruta_png, full_page=True)
    imagen = Image.open(ruta_png).convert("RGB")
    imagen.save(ruta_pdf, "PDF", resolution=100.0)
    if os.path.exists(ruta_png):
        os.remove(ruta_png)
    return f"{BASE_URL_WEB}/pdf/{nombre_archivo_pdf}" if BASE_URL_WEB else f"pdf/{nombre_archivo_pdf}"

def resolver_captcha_imagen(ruta_img):
    """Envía la imagen del captcha a 2Captcha y regresa la respuesta resuelta."""
    with open(ruta_img, "rb") as f:
        r = requests.post("http://2captcha.com/in.php", files={"file": f}, data={"key": API_KEY_CAPTCHA, "method": "post"})
    if "OK|" not in r.text:
        return ""
    captcha_id = r.text.split("|")[1]
    for _ in range(15):
        time.sleep(4)
        res = requests.get(f"http://2captcha.com/res.php?key={API_KEY_CAPTCHA}&action=get&id={captcha_id}")
        if "OK|" in res.text:
            return res.text.split("|")[1]
    return ""

# --- 4. EXTRACCIÓN Y SCRAPING EN CDMX Y EDOMEX ---
def consultar_cdmx(page, placa):
    """Consulta portal CDMX con evasión de bloqueos y resolución de captcha."""
    try:
        page.goto("https://data.finanzas.cdmx.gob.mx/sma/consulta_sedema", timeout=35000)
        page.fill("input[name='placa']", placa)
        page.locator("#captcha_img").screenshot(path="cap_cdmx.png")
        texto_cap = resolver_captcha_imagen("cap_cdmx.png")
        if not texto_cap:
            return False, []
        page.fill("input[name='captcha']", texto_cap)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle", timeout=20000)

        html = page.content()
        if "No se encontraron" in html or "Sin adeudos" in html:
            return True, []

        fecha_hoy = datetime.now().strftime("%Y%m%d")
        multas = []
        filas = page.locator("table tbody tr").all()

        for idx, f in enumerate(filas):
            cols = f.locator("td").all_inner_texts()
            if len(cols) >= 4:
                folio = cols[0].strip()
                fecha_inf = cols[1].strip()
                motivo = cols[2].strip()
                monto_limpio = cols[3].replace("$", "").replace(",", "").strip()
                monto = float(monto_limpio or 0)

                nombre_boleta = f"BOLETA_{placa}_{folio}_{fecha_hoy}.pdf"
                url_boleta = captura_a_pdf(page, nombre_boleta)

                nombre_linea = f"LINEA_{placa}_{folio}_{fecha_hoy}.pdf"
                ruta_linea = os.path.join("docs/pdf", nombre_linea)
                url_linea = f"{BASE_URL_WEB}/pdf/{nombre_linea}"

                btn_pago = f.locator("a, button").filter(has_text="Formato").or_(f.locator("a, button").filter(has_text="Pagar")).or_(f.locator("a, button").filter(has_text="Línea"))
                if btn_pago.count() > 0:
                    try:
                        with page.expect_download(timeout=10000) as download_info:
                            btn_pago.first.click()
                        download = download_info.value
                        download.save_as(ruta_linea)
                    except Exception:
                        time.sleep(2)
                        url_linea = captura_a_pdf(page, nombre_linea)
                else:
                    url_linea = captura_a_pdf(page, nombre_linea)

                multas.append({
                    "folio": folio, "fecha": fecha_inf, "motivo": motivo,
                    "monto": monto, "municipio": "CDMX", "url_boleta": url_boleta, "url_linea": url_linea
                })
        return (len(multas) > 0), multas
    except Exception:
        return False, []

def consultar_edomex(page, placa):
    """Consulta portal del Estado de México."""
    try:
        page.goto("https://infracciones.ssedomex.gob.mx/", timeout=35000)
        time.sleep(4)
        page.fill("input[placeholder*='Placa'], input[name*='placa']", placa)
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle", timeout=25000)

        html = page.content()
        if "No se encontraron infracciones" in html or "sin adeudo" in html.lower():
            return True, []

        fecha_hoy = datetime.now().strftime("%Y%m%d")
        multas = []
        filas = page.locator("table tbody tr").all()

        for f in filas:
            cols = f.locator("td").all_inner_texts()
            if len(cols) >= 4:
                folio = cols[0].strip()
                fecha_inf = cols[1].strip()
                motivo = cols[2].strip()
                monto_limpio = cols[3].replace("$", "").replace(",", "").strip()
                monto = float(monto_limpio or 0)
                municipio = cols[4].strip() if len(cols) >= 5 else "Edomex"

                nombre_boleta = f"BOLETA_{placa}_{folio}_{fecha_hoy}.pdf"
                btn_detalle = f.locator("a, button").filter(has_text="Detalle").or_(f.locator("a, button").filter(has_text="Ver")).or_(f.locator("td").first)
                try:
                    btn_detalle.first.click()
                    time.sleep(2)
                except Exception:
                    pass
                url_boleta = captura_a_pdf(page, nombre_boleta)

                nombre_linea = f"LINEA_{placa}_{folio}_{fecha_hoy}.pdf"
                ruta_linea = os.path.join("docs/pdf", nombre_linea)
                url_linea = f"{BASE_URL_WEB}/pdf/{nombre_linea}"

                btn_formato = page.locator("a, button").filter(has_text="Formato").or_(page.locator("a, button").filter(has_text="Línea")).or_(page.locator("a, button").filter(has_text="Pago"))
                if btn_formato.count() > 0:
                    try:
                        with page.expect_download(timeout=10000) as download_info:
                            btn_formato.first.click()
                        download = download_info.value
                        download.save_as(ruta_linea)
                    except Exception:
                        time.sleep(2)
                        url_linea = captura_a_pdf(page, nombre_linea)
                else:
                    url_linea = captura_a_pdf(page, nombre_linea)

                multas.append({
                    "folio": folio, "fecha": fecha_inf, "motivo": motivo,
                    "monto": monto, "municipio": municipio, "url_boleta": url_boleta, "url_linea": url_linea
                })
        return (len(multas) > 0), multas
    except Exception:
        return False, []

# --- 5. LÓGICA DE NEGOCIO Y PROCESAMIENTO EN BASE DE DATOS ---
def procesar_placa(unidad_info, entidad, ok, multas_actuales, registros_db):
    if not ok:
        return "ERROR_PORTAL"

    placa = unidad_info["placa"]
    hoy = datetime.now()
    hoy_str = hoy.strftime("%Y-%m-%d")
    folios_actuales = {m["folio"] for m in multas_actuales}

    for m in multas_actuales:
        if m["folio"] not
