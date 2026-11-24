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
from playwright_stealth import stealth_sync

# --- 1. CARGA DE VARIABLES Y SECRETOS DE ENTORNO ---
API_KEY_CAPTCHA = os.environ["API_KEY_CAPTCHA"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_PASS"]
EMAIL_DESTINO = os.environ["EMAIL_DESTINO"]
GOOGLE_CREDS_JSON = json.loads(os.environ["GOOGLE_CREDS_JSON"])
BASE_URL_WEB = os.environ.get("GITHUB_PAGES_URL", "").rstrip("/")

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
                contenedor.innerHTML += `
                    <div class="bg-white p-4 rounded-xl border shadow-sm flex flex-col justify-between">
                        <div>
                            <div class="flex justify-between items-center mb-2">
                                <span class="font-bold text-base text-slate-900">\${item.placa} <span class="text-xs font-normal text-slate-500">(\${item.id_actual})</span></span>
                                <span class="text-xs font-bold px-2.5 py-1 rounded-full \${esActiva ? 'bg-red-100 text-red-700' : 'bg-emerald-100 text-emerald-700'}">\${item.estatus}</span>
                            </div>
                            <p class="text-xs text-slate-600 font-medium">Sucursal: \${item.num_sucursal} - \${item.nombre_sucursal}</p>
                            <p class="text-xs text-slate-500">Analista: \${item.analista} | Conductor: \${item.conductor}</p>
                            <div class="my-3 p-2.5 bg-slate-50 rounded-lg text-xs space-y-1">
                                <p><span class="font-semibold">Folio:</span> \${item.folio} (\${item.entidad})</p>
                                <p><span class="font-semibold">Motivo:</span> \${item.motivo}</p>
                                <p><span class="font-semibold">Monto:</span> <span class="text-red-600 font-bold">\${item.monto.toLocaleString('es-MX', {{style:'currency', currency:'MXN'}})}</span></p>
                                <p><span class="font-semibold">Límite Pago:</span> \${item.limite} (\${item.dias_vencer} días)</p>
                            </div>
                        </div>
                        <div class="grid grid-cols-2 gap-2 pt-2 border-t">
                            <a href="\${item.url_linea}" target="_blank" class="text-center bg-blue-600 hover:bg-blue-700 text-white py-1.5 rounded-lg text-xs font-semibold transition">📄 Línea Captura</a>
                            <a href="\${item.url_boleta}" target="_blank" class="text-center bg-slate-700 hover:bg-slate-800 text-white py-1.5 rounded-lg text-xs font-semibold transition">📸 Ver Boleta</a>
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

    col_placa = encabezados.index("Placa actual")
    col_id = encabezados.index("ID Actual")
    col_num_suc = encabezados.index("Num_Sucursal")
    col_nom_suc = encabezados.index("Nombre_Sucursal")
    col_analista = encabezados.index("Nombre del Analista")
    col_conductor = encabezados.index("Nombre de conductor")
    col_rev = encabezados.index("Ultima_Revision") + 1
    col_est = encabezados.index("Estatus_Conexion") + 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        stealth_sync(page)

        for idx, fila in enumerate(todas_filas[1:]):
            if len(fila) <= col_placa or not fila[col_placa].strip():
                continue
            
            unidad_info = {
                "placa": fila[col_placa].strip().upper(),
                "id_actual": fila[col_id].strip() if len(fila) > col_id else "",
                "num_sucursal": fila[col_num_suc].strip() if len(fila) > col_num_suc else "",
                "nombre_sucursal": fila[col_nom_suc].strip() if len(fila) > col_nom_suc else "",
                "analista": fila[col_analista].strip() if len(fila) > col_analista else "",
                "conductor": fila[col_conductor].strip() if len(fila) > col_conductor else ""
            }
            
            ok_cdmx, m_cdmx = consultar_cdmx(page, unidad_info["placa"])
            st_cdmx = procesar_placa(unidad_info, "CDMX", ok_cdmx, m_cdmx, registros_db)

            ok_edomex, m_edomex = consultar_edomex(page, unidad_info["placa"])
            st_edomex = procesar_placa(unidad_info, "EDOMEX", ok_edomex, m_edomex, registros_db)

            hoja_placas.update_cell(idx + 2, col_rev, hoy.strftime("%Y-%m-%d %H:%M"))
            hoja_placas.update_cell(idx + 2, col_est, f"CDMX:{st_cdmx} | EDOMEX:{st_edomex}")

        browser.close()

    formatear_sheets()
    generar_dashboard_html(registros_db)
    enviar_correo()