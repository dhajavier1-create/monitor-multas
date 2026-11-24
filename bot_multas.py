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
API_KEY_CAPTCHA = os.environ.get("API_KEY_CAPTCHA", "")
CDMX_USER = os.environ.get("CDMX_USER", "")
CDMX_PASS = os.environ.get("CDMX_PASS", "")
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_PASS"]
EMAIL_DESTINO = os.environ["EMAIL_DESTINO"]
GOOGLE_CREDS_JSON = json.loads(os.environ["GOOGLE_CREDS_JSON"])
BASE_URL_WEB = (
    os.environ.get("PAGES_URL")
    or os.environ.get("GITHUB_PAGES_URL")
    or ""
).strip().rstrip("/")

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
    ruta_png = f"temp_{nombre_archivo_pdf}.png"
    ruta_pdf = os.path.join("docs/pdf", nombre_archivo_pdf)
    page.screenshot(path=ruta_png, full_page=True)
    imagen = Image.open(ruta_png).convert("RGB")
    imagen.save(ruta_pdf, "PDF", resolution=100.0)
    if os.path.exists(ruta_png):
        os.remove(ruta_png)
    return f"{BASE_URL_WEB}/pdf/{nombre_archivo_pdf}" if BASE_URL_WEB else f"pdf/{nombre_archivo_pdf}"

def resolver_captcha_imagen(ruta_img):
    if not API_KEY_CAPTCHA:
        return "", "ERR_SIN_API_CAPTCHA"
    with open(ruta_img, "rb") as f:
        r = requests.post(
            "http://2captcha.com/in.php",
            files={"file": f},
            data={"key": API_KEY_CAPTCHA, "method": "post"},
            timeout=15
        )
    if "OK|" not in r.text:
        return "", f"ERR_2CAPTCHA({r.text.strip()})"
    captcha_id = r.text.split("|")[1]
    for _ in range(15):
        time.sleep(4)
        res = requests.get(
            f"http://2captcha.com/res.php?key={API_KEY_CAPTCHA}&action=get&id={captcha_id}",
            timeout=15
        )
        if "OK|" in res.text:
            return res.text.split("|")[1], "OK"
        if "CAPCHA_NOT_READY" not in res.text:
            return "", f"ERR_2CAPTCHA({res.text.strip()})"
    return "", "ERR_CAPTCHA_TIMEOUT"

# --- 4. INICIO DE SESIÓN Y SCRAPING EN CDMX Y EDOMEX ---
def iniciar_sesion_cdmx(page):
    """Inicia sesión una sola vez en https://infracciones.cdmx.gob.mx/"""
    if not CDMX_USER or not CDMX_PASS:
        return False, "ERR_FALTAN_CREDENCIALES_CDMX"
    try:
        resp = page.goto("https://infracciones.cdmx.gob.mx/", wait_until="domcontentloaded", timeout=25000)
        if not resp or resp.status >= 400:
            return False, f"ERR_HTTP_{resp.status if resp else 'SIN_RESP'}"

        time.sleep(2)
        input_user = page.locator("input[type='email'], input[name*='user' i], input[name*='correo' i], input[name*='email' i], input[type='text']").first
        input_pass = page.locator("input[type='password']").first

        if input_user.count() == 0 or input_pass.count() == 0:
            return False, "ERR_FORM_LOGIN_NO_ENCONTRADO"

        input_user.fill(CDMX_USER)
        input_pass.fill(CDMX_PASS)

        # Si el login tiene captcha de imagen, lo resuelve
        img_cap = page.locator("img[src*='captcha' i], #captcha_img").first
        if img_cap.count() > 0 and img_cap.is_visible():
            img_cap.screenshot(path="cap_login_cdmx.png")
            txt_cap, est_cap = resolver_captcha_imagen("cap_login_cdmx.png")
            if not txt_cap:
                return False, est_cap
            page.locator("input[name*='captcha' i], input#captcha").first.fill(txt_cap)

        btn_login = page.locator("button[type='submit'], input[type='submit'], button:has-text('Iniciar'), button:has-text('Ingresar'), button:has-text('Entrar')").first
        btn_login.click()
        page.wait_for_load_state("networkidle", timeout=20000)
        time.sleep(2)

        # Verificar que ya no siga en la pantalla de password
        if page.locator("input[type='password']").count() > 0 and page.locator("input[type='password']").first.is_visible():
            return False, "ERR_LOGIN_RECHAZADO"

        print("✅ Sesión iniciada correctamente en CDMX")
        return True, "OK"
    except Exception as e:
        err = type(e).__name__
        print(f"Error login CDMX ({err}): {str(e)[:150]}")
        return False, f"ERR_LOGIN_{err}"

def consultar_cdmx(page, placa, sesion_activa):
    """Consulta la placa dentro de la sesión iniciada en infracciones.cdmx.gob.mx"""
    if not sesion_activa:
        return False, [], "ERR_SIN_SESION"
    try:
        # Si no hay campo de placa visible en la vista actual, recarga la página principal autenticada
        input_placa = page.locator("input[placeholder*='Placa' i], input[name*='placa' i], input#placa, input[type='text']").first
        if input_placa.count() == 0 or not input_placa.is_visible():
            page.goto("https://infracciones.cdmx.gob.mx/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)
            input_placa = page.locator("input[placeholder*='Placa' i], input[name*='placa' i], input#placa, input[type='text']").first

        input_placa.wait_for(state="visible", timeout=8000)
        input_placa.fill("")
        input_placa.fill(placa)

        # Si pide captcha por cada consulta de placa
        img_cap = page.locator("img[src*='captcha' i], #captcha_img").first
        if img_cap.count() > 0 and img_cap.is_visible():
            img_cap.screenshot(path="cap_cdmx.png")
            texto_cap, est_cap = resolver_captcha_imagen("cap_cdmx.png")
            if not texto_cap:
                return False, [], est_cap
            page.locator("input[name*='captcha' i], input#captcha").first.fill(texto_cap)

        btn_buscar = page.locator("button[type='submit'], button:has-text('Buscar'), button:has-text('Consultar')").first
        if btn_buscar.count() > 0:
            btn_buscar.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)

        html = page.content()
        if any(txt in html.lower() for txt in ["no se encontraron", "sin adeudos", "no cuenta con adeudos", "0 adeudos", "sin infracciones"]):
            return True, [], "OK"

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
                try:
                    monto = float(monto_limpio or 0)
                except ValueError:
                    monto = 0.0

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
        return True, multas, "OK"
    except Exception as e:
        err_corto = type(e).__name__
        print(f"[{placa}] Error CDMX ({err_corto}): {str(e)[:150]}")
        return False, [], f"ERR_{err_corto}"

def consultar_edomex(page, placa):
    """Consulta directa en https://infracciones.ssedomex.gob.mx/Search"""
    try:
        resp = page.goto("https://infracciones.ssedomex.gob.mx/Search", wait_until="domcontentloaded", timeout=25000)
        if not resp or resp.status >= 400:
            code = resp.status if resp else "SIN_RESPUESTA"
            print(f"[{placa}] EDOMEX HTTP {code}")
            return False, [], f"ERR_HTTP_{code}"

        time.sleep(2)
        input_placa = page.locator("input[placeholder*='Placa' i], input[name*='placa' i], input[type='text']").first
        input_placa.wait_for(state="visible", timeout=8000)
        input_placa.fill("")
        input_placa.fill(placa)

        btn_buscar = page.locator("button[type='submit'], button:has-text('Buscar'), button:has-text('Consultar')").first
        if btn_buscar.count() > 0:
            btn_buscar.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)

        html = page.content()
        if any(txt in html.lower() for txt in ["no se encontraron", "sin adeudo", "sin infracciones", "no existen registros", "0 registros"]):
            return True, [], "OK"

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
                try:
                    monto = float(monto_limpio or 0)
                except ValueError:
                    monto = 0.0
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
        return True, multas, "OK"
    except Exception as e:
        err_corto = type(e).__name__
        print(f"[{placa}] Error EDOMEX ({err_corto}): {str(e)[:150]}")
        return False, [], f"ERR_{err_corto}"

# --- 5. LÓGICA DE NEGOCIO Y PROCESAMIENTO EN BASE DE DATOS ---
def procesar_placa(unidad_info, entidad, ok, multas_actuales, detalle_estado, registros_db):
    if not ok:
        return detalle_estado

    placa = unidad_info["placa"]
    hoy = datetime.now()
    hoy_str = hoy.strftime("%Y-%m-%d")
    folios_actuales = {m["folio"] for m in multas_actuales}

    for m in multas_actuales:
        if m["folio"] not in registros_db:
            plazo = 30 if entidad == "CDMX" else 15
            fecha_lim_dt = hoy + timedelta(days=plazo)
            limite_str = fecha_lim_dt.strftime("%Y-%m-%d")
            dias_vencer = (fecha_lim_dt - hoy).days

            formula_linea = f'=HIPERVINCULO("{m["url_linea"]}"; "📄 Descargar Línea")'
            formula_boleta = f'=HIPERVINCULO("{m["url_boleta"]}"; "📸 Ver Boleta PDF")'

            nueva_fila = [
                m["folio"], placa, unidad_info["id_actual"], unidad_info["num_sucursal"],
                unidad_info["nombre_sucursal"], unidad_info["analista"], unidad_info["conductor"],
                entidad, m["fecha"], m["motivo"], m["monto"], m["municipio"], limite_str,
                dias_vencer, "ACTIVA", formula_linea, formula_boleta, hoy_str
            ]
            hoja_historial.append_row(nueva_fila, value_input_option="USER_ENTERED")

            fila_mem = list(nueva_fila)
            fila_mem[15] = m["url_linea"]
            fila_mem[16] = m["url_boleta"]
            registros_db[m["folio"]] = {"fila": len(registros_db) + 3, "data": fila_mem}

            alertas_del_dia.append(
                f"🚨 NUEVA INFRACCIÓN ({entidad}) | Placa: {placa} (ID: {unidad_info['id_actual']}) | "
                f"Sucursal: {unidad_info['num_sucursal']} - {unidad_info['nombre_sucursal']} | "
                f"Analista: {unidad_info['analista']} | Folio: {m['folio']} | Motivo: {m['motivo']} | "
                f"Monto: ${m['monto']:,.2f} | Límite: {limite_str}\n"
                f"   👉 Boleta PDF: {m['url_boleta']}\n   👉 Línea Captura: {m['url_linea']}"
            )

    for folio, info in registros_db.items():
        row = info["data"]
        if row[1] == placa and row[7] == entidad and row[14] == "ACTIVA":
            if folio not in folios_actuales:
                hoja_historial.update_cell(info["fila"], 14, 0)
                hoja_historial.update_cell(info["fila"], 15, "PAGADA")
                hoja_historial.update_cell(info["fila"], 18, hoy_str)
                row[13] = 0
                row[14] = "PAGADA"
                alertas_del_dia.append(
                    f"✅ PAGO CONFIRMADO ({entidad}) | Placa: {placa} | Sucursal: {row[3]} - {row[4]} | "
                    f"Analista: {row[5]} | Folio: {folio} (${float(row[10]):,.2f}) ya aparece pagado."
                )
    return "OK"

# --- 6. FORMATO VISUAL AUTOMÁTICO PARA GOOGLE SHEETS ---
def formatear_sheets():
    total_filas = len(hoja_historial.get_all_values())
    if total_filas < 2:
        return
    sheet_id = hoja_historial.id
    requests_formato = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 18},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.06, "green": 0.09, "blue": 0.16},
                        "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 10},
                        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount"
            }
        }
    ]

    if total_filas > 2:
        requests_formato.append({
            "updateBorders": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": total_filas, "startColumnIndex": 0, "endColumnIndex": 18},
                "top": {"style": "SOLID", "color": {"red": 0.75, "green": 0.75, "blue": 0.75}},
                "bottom": {"style": "SOLID", "color": {"red": 0.75, "green": 0.75, "blue": 0.75}},
                "left": {"style": "SOLID", "color": {"red": 0.75, "green": 0.75, "blue": 0.75}},
                "right": {"style": "SOLID", "color": {"red": 0.75, "green": 0.75, "blue": 0.75}},
                "innerHorizontal": {"style": "SOLID", "color": {"red": 0.88, "green": 0.88, "blue": 0.88}},
                "innerVertical": {"style": "SOLID", "color": {"red": 0.88, "green": 0.88, "blue": 0.88}}
            }
        })

        requests_formato.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": total_filas, "startColumnIndex": 10, "endColumnIndex": 11},
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"},
                        "textFormat": {"bold": True}
                    }
                },
                "fields": "userEnteredFormat(numberFormat,textFormat)"
            }
        })

        datos_hist = hoja_historial.get_all_values()[2:]
        for idx, fila in enumerate(datos_hist):
            if len(fila) >= 15:
                estatus = fila[14].strip().upper()
                bg = {"red": 0.99, "green": 0.89, "blue": 0.89} if estatus == "ACTIVA" else {"red": 0.86, "green": 0.96, "blue": 0.90}
                fg = {"red": 0.72, "green": 0.11, "blue": 0.11} if estatus == "ACTIVA" else {"red": 0.06, "green": 0.45, "blue": 0.25}

                requests_formato.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": idx + 2, "endRowIndex": idx + 3, "startColumnIndex": 14, "endColumnIndex": 15},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": bg,
                                "textFormat": {"foregroundColor": fg, "bold": True},
                                "horizontalAlignment": "CENTER"
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
                    }
                })

        requests_formato.append({
            "autoResizeDimensions": {
                "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 18}
            }
        })

    sheet.batch_update({"requests": requests_formato})

# --- 7. GENERACIÓN DEL DASHBOARD HTML AUTOMÁTICO ---
def generar_dashboard_html(registros_db):
    todas = [info["data"] for info in registros_db.values()]
    activas = [r for r in todas if r[14] == "ACTIVA"]
    pagadas = [r for r in todas if r[14] == "PAGADA"]
    monto_activo = sum(float(r[10]) for r in activas)
    monto_pagado = sum(float(r[10]) for r in pagadas)

    datos_json = json.dumps([
        {
            "folio": r[0], "placa": r[1], "id_actual": r[2], "num_sucursal": r[3],
            "nombre_sucursal": r[4], "analista": r[5], "conductor": r[6], "entidad": r[7],
            "fecha": r[8], "motivo": r[9], "monto": float(r[10]), "municipio": r[11],
            "limite": r[12], "dias_vencer": r[13], "estatus": r[14], "url_linea": r[15], "url_boleta": r[16]
        }
        for r in reversed(todas)
    ], ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Flotilla | Control de Infracciones</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-100 text-slate-800 pb-10">
    <header class="bg-slate-900 text-white sticky top-0 z-20 shadow-md">
        <div class="max-w-7xl mx-auto px-4 py-3 flex justify-between items-center">
            <h1 class="text-lg font-bold">🚦 Control de Multas</h1>
            <span class="text-xs text-slate-300">Actualizado: {datetime.now().strftime('%d/%m/%Y %H:%M')}</span>
        </div>
    </header>
    <main class="max-w-7xl mx-auto px-4 pt-5">
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <div class="bg-white p-4 rounded-xl border-l-4 border-red-500 shadow-sm"><p class="text-xs text-slate-500">Activas</p><p class="text-2xl font-bold text-red-600">{len(activas)}</p></div>
            <div class="bg-white p-4 rounded-xl border-l-4 border-amber-500 shadow-sm"><p class="text-xs text-slate-500">Adeudo Total</p><p class="text-2xl font-bold">${monto_activo:,.0f}</p></div>
            <div class="bg-white p-4 rounded-xl border-l-4 border-emerald-500 shadow-sm"><p class="text-xs text-slate-500">Pagadas</p><p class="text-2xl font-bold text-emerald-600">{len(pagadas)}</p></div>
            <div class="bg-white p-4 rounded-xl border-l-4 border-blue-500 shadow-sm"><p class="text-xs text-slate-500">Total Pagado</p><p class="text-2xl font-bold">${monto_pagado:,.0f}</p></div>
        </div>
        <div class="bg-white p-4 rounded-xl shadow-sm mb-5">
            <input type="text" id="buscador" placeholder="🔍 Buscar por placa, sucursal, analista o folio..." class="w-full px-3 py-2 border rounded-lg text-sm">
        </div>
        <div id="vista-cards" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></div>
    </main>
    <script>
        const datos = {datos_json};
        function renderizar() {{
            const texto = document.getElementById('buscador').value.toLowerCase();
            const contenedor = document.getElementById('vista-cards');
            contenedor.innerHTML = '';
            datos.filter(d => d.placa.toLowerCase().includes(texto) || d.analista.toLowerCase().includes(texto) || d.folio.toLowerCase().includes(texto) || d.nombre_sucursal.toLowerCase().includes(texto)).forEach(item => {{
                const esActiva = item.estatus === 'ACTIVA';
                const badgeClass = esActiva ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700';
                contenedor.innerHTML += `
                    <div class="bg-white p-4 rounded-xl border shadow-sm flex flex-col justify-between">
                        <div>
                            <div class="flex justify-between items-center mb-2">
                                <span class="font-bold text-base text-slate-900">\${{item.placa}} <span class="text-xs font-normal text-slate-500">(\${{item.id_actual}})</span></span>
                                <span class="text-xs font-bold px-2.5 py-1 rounded-full \${{badgeClass}}">\${{item.estatus}}</span>
                            </div>
                            <p class="text-xs text-slate-600 font-medium">Sucursal: \${{item.num_sucursal}} - \${{item.nombre_sucursal}}</p>
                            <p class="text-xs text-slate-500">Analista: \${{item.analista}} | Conductor: \${{item.conductor}}</p>
                            <div class="my-3 p-2.5 bg-slate-50 rounded-lg text-xs space-y-1">
                                <p><span class="font-semibold">Folio:</span> \${{item.folio}} (\${{item.entidad}})</p>
                                <p><span class="font-semibold">Motivo:</span> \${{item.motivo}}</p>
                                <p><span class="font-semibold">Monto:</span> <span class="text-red-600 font-bold">\${{item.monto.toLocaleString('es-MX', {{{{style:'currency', currency:'MXN'}}}})}}</span></p>
                                <p><span class="font-semibold">Límite Pago:</span> \${{item.limite}} (\${{item.dias_vencer}} días)</p>
                            </div>
                        </div>
                        <div class="grid grid-cols-2 gap-2 pt-2 border-t">
                            <a href="\${{item.url_linea}}" target="_blank" class="text-center bg-blue-600 hover:bg-blue-700 text-white py-1.5 rounded-lg text-xs font-semibold transition">📄 Línea Captura</a>
                            <a href="\${{item.url_boleta}}" target="_blank" class="text-center bg-slate-700 hover:bg-slate-800 text-white py-1.5 rounded-lg text-xs font-semibold transition">📸 Ver Boleta</a>
                        </div>
                    </div>`;
            }});
        }}
        document.getElementById('buscador').addEventListener('input', renderizar);
        renderizar();
    </script>
</body>
</html>
"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)

# --- 8. ENVÍO AUTOMÁTICO DE ALERTAS POR EMAIL ---
def enviar_correo():
    if not alertas_del_dia:
        return
    cuerpo = "Resumen de alertas del día detectadas por el Bot de Infracciones:\n\n" + "\n\n".join(alertas_del_dia)
    msg = MIMEText(cuerpo)
    msg["Subject"] = f"🚦 Alertas de Infracciones Flotilla ({len(alertas_del_dia)} novedades)"
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_DESTINO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, EMAIL_DESTINO, msg.as_string())

def buscar_col(encabezados_norm, candidatos, default=None):
    for cand in candidatos:
        if cand in encabezados_norm:
            return encabezados_norm.index(cand)
    for idx, h in enumerate(encabezados_norm):
        for cand in candidatos:
            if cand in h:
                return idx
    return default

# --- 9. EJECUCIÓN DEL FLUJO PRINCIPAL ---
if __name__ == "__main__":
    valores_hist = hoja_historial.get_all_values()
    filas_hist = valores_hist[2:] if len(valores_hist) > 2 else []

    registros_db = {}
    for idx, r in enumerate(filas_hist):
        if r and r[0]:
            row_limpia = list(r)
            for col_url in [15, 16]:
                if len(row_limpia) > col_url and "HIPERVINCULO" in row_limpia[col_url]:
                    partes = row_limpia[col_url].split('"')
                    if len(partes) >= 2:
                        row_limpia[col_url] = partes[1]
            registros_db[r[0]] = {"fila": idx + 3, "data": row_limpia}

    hoy = datetime.now()
    for folio, info in registros_db.items():
        r = info["data"]
        if len(r) >= 15 and r[14] == "ACTIVA" and r[12]:
            try:
                dias = (datetime.strptime(r[12], "%Y-%m-%d") - hoy).days
                hoja_historial.update_cell(info["fila"], 14, dias)
                r[13] = dias
            except ValueError:
                pass

    todas_filas = hoja_placas.get_all_values()
    encabezados = [h.strip() for h in todas_filas[0]]
    encabezados_norm = [h.lower() for h in encabezados]

    col_placa = buscar_col(encabezados_norm, ["placa actual", "placa"], 0)
    col_id = buscar_col(encabezados_norm, ["id actual", "id"])
    col_num_suc = buscar_col(encabezados_norm, ["num_sucursal", "num sucursal", "no. sucursal", "numero sucursal", "sucursal"])
    col_nom_suc = buscar_col(encabezados_norm, ["nombre_sucursal", "nombre sucursal"])
    col_analista = buscar_col(encabezados_norm, ["nombre del analista", "analista", "nombre analista"])
    col_conductor = buscar_col(encabezados_norm, ["nombre de conductor", "conductor", "nombre conductor"])
    col_rev_idx = buscar_col(encabezados_norm, ["ultima_revision", "ultima revision", "última revisión"])
    col_est_idx = buscar_col(encabezados_norm, ["estatus_conexion", "estatus conexion", "estatus"])

    col_rev = (col_rev_idx + 1) if col_rev_idx is not None else (len(encabezados) + 1)
    col_est = (col_est_idx + 1) if col_est_idx is not None else (len(encabezados) + 2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="es-MX"
        )
        # Dos pestañas separadas para no perder la sesión de CDMX mientras consulta EdoMéx
        page_cdmx = context.new_page()
        page_edomex = context.new_page()
        for pg in (page_cdmx, page_edomex):
            pg.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        sesion_cdmx_ok, estado_login_cdmx = iniciar_sesion_cdmx(page_cdmx)

        for idx, fila in enumerate(todas_filas[1:]):
            if col_placa is None or len(fila) <= col_placa or not fila[col_placa].strip():
                continue

            unidad_info = {
                "placa": fila[col_placa].strip().upper(),
                "id_actual": fila[col_id].strip() if col_id is not None and len(fila) > col_id else "",
                "num_sucursal": fila[col_num_suc].strip() if col_num_suc is not None and len(fila) > col_num_suc else "",
                "nombre_sucursal": fila[col_nom_suc].strip() if col_nom_suc is not None and len(fila) > col_nom_suc else "",
                "analista": fila[col_analista].strip() if col_analista is not None and len(fila) > col_analista else "",
                "conductor": fila[col_conductor].strip() if col_conductor is not None and len(fila) > col_conductor else ""
            }

            if sesion_cdmx_ok:
                ok_cdmx, m_cdmx, det_cdmx = consultar_cdmx(page_cdmx, unidad_info["placa"], True)
            else:
                ok_cdmx, m_cdmx, det_cdmx = False, [], estado_login_cdmx
            st_cdmx = procesar_placa(unidad_info, "CDMX", ok_cdmx, m_cdmx, det_cdmx, registros_db)

            ok_edomex, m_edomex, det_edomex = consultar_edomex(page_edomex, unidad_info["placa"])
            st_edomex = procesar_placa(unidad_info, "EDOMEX", ok_edomex, m_edomex, det_edomex, registros_db)

            hoja_placas.update_cell(idx + 2, col_rev, hoy.strftime("%Y-%m-%d %H:%M"))
            hoja_placas.update_cell(idx + 2, col_est, f"CDMX:{st_cdmx} | EDOMEX:{st_edomex}")

        browser.close()

    formatear_sheets()
    generar_dashboard_html(registros_db)
    enviar_correo()
