#!/usr/bin/env python3
"""
Monitor de stock REAL - Movistar catálogo
Objetivos monitoreados por defecto:
- Xiaomi 15 5G 512GB Negro Medianoche (Seminuevo)
- Motorola Moto Edge 50 Pro 5G 512GB Blanco (Seminuevo)

Por qué dos etapas: Movistar muestra falsos positivos cada ~1 hora en la ficha
(getDataProductFicha dice que hay stock, pero al comprar dice que no hay).
El stock REAL es el que valida el botón "Comprar" antes de mandar al checkout,
vía GraphQL dataStockProductoBodegaSap. Solo si esa segunda consulta dice
detalle=con-stock y cantidad>0, el equipo se puede agregar al carro.

Etapa 1 (ficha): intercepta getDataProductFicha:
    estado_stock=true / stock_vm05>0 => posible stock (sigue a etapa 2)
    estado_stock=false / stock_vm05==0 => SIN STOCK (termina, sin ruido)
Etapa 2 (verificación real, solo si etapa 1 dice que hay):
    ejecuta dataStockProductoBodegaSap(sku, fullprice, detalle) en la misma
    sesión del navegador (misma validación que hace "Comprar"):
    detalle=con-stock y cantidad>0 => STOCK REAL => avisa fuerte
    detalle=sin-stock o cantidad=0  => FALSO POSITIVO => solo log, sin aviso

Avisa (solo con stock real) con:
  - notify-send (notificación GNOME persistente)
  - sonido insistente (paplay)
  - abre la página automáticamente (xdg-open)
  - log en archivo + opcional Telegram

Uso:
  python3 monitor.py --once                 # una sola revisión de productos configurados
  python3 monitor.py --interval 600         # loop cada 600s (default 600s)
  python3 monitor.py --url <URL>            # monitorear una URL específica
  python3 monitor.py --once --no-open       # sin abrir navegador al avisar
Códigos de salida --once:
  10 = stock real verificado / 11 = falso positivo / 0 = sin stock
  3 = dudoso / 2 = error
"""
import argparse
import json
import subprocess
import sys
import time
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "stock.log"
STATE_FILE = BASE_DIR / "last_state.json"

PRODUCTOS = [
    {
        "nombre": "Xiaomi 15 5G 512GB Negro Medianoche (Seminuevo)",
        "url": "https://catalogo.movistar.cl/tienda/xiaomi-15-5g-512gb-negro-medianoche-seminuevo",
        "sku": "TM5CLXI00015NE12RF",
    },
    {
        "nombre": "Motorola Moto Edge 50 Pro 5G 512GB Blanco (Seminuevo)",
        "url": "https://catalogo.movistar.cl/tienda/motorola-moto-edge-50-pro-5g-512gb-blanco-seminuevo",
        "sku": "TM5CLMO0E50PBL12RF",
    },
]

# Compatibilidad con referencias previas
URL_OBJETIVO = PRODUCTOS[0]["url"]
SKU_OBJETIVO = PRODUCTOS[0]["sku"]
URL_EJEMPLO_CON_STOCK = "https://catalogo.movistar.cl/tienda/xiaomi-15-ultra-5g-512gb-plata-seminuevo"

# Telegram opcional: export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
import os
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Email vía Resend: export RESEND_API_KEY=... ALERT_EMAIL=...
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "thenzo22@gmail.com")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
# Modo cloud (GitHub Actions, etc.): MONITOR_CLOUD=1 desactiva todo lo de
# escritorio (notify-send, sonido, xdg-open) y avisa por email/Telegram
# en --once solo ante transición a stock (usa last_state.json como caché).
CLOUD = os.environ.get("MONITOR_CLOUD", "") == "1"

SOUND = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
SITEKEY_RECAPTCHA = "6Lc7LF4sAAAAAMR8RRtWdOBaLV55EbNiAXSpu5NU"


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify_desktop(titulo: str, cuerpo: str, urgente: bool = False):
    if CLOUD:
        return
    try:
        cmd = ["notify-send"]
        if urgente:
            cmd += ["-u", "critical", "-t", "0"]
        else:
            cmd += ["-t", "30000"]
        cmd += [titulo, cuerpo]
        subprocess.run(cmd, timeout=10)
    except Exception as e:
        log(f"notify-send falló: {e}")


def sonar_alerta(veces: int = 3):
    if CLOUD:
        return
    for _ in range(veces):
        try:
            subprocess.run(["paplay", SOUND], timeout=15)
        except Exception as e:
            log(f"paplay falló: {e}")
            try:
                subprocess.run(["spd-say", "Stock real disponible en Movistar"], timeout=15)
            except Exception:
                pass
            break
        time.sleep(0.5)


def abrir_pagina(url: str):
    if CLOUD:
        return
    try:
        subprocess.Popen(["xdg-open", url])
    except Exception as e:
        log(f"xdg-open falló: {e}")


def enviar_telegram(texto: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": texto}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15).read()
        log("Telegram enviado OK")
    except Exception as e:
        log(f"Telegram falló: {e}")


def enviar_email(asunto: str, texto: str, url: str = ""):
    if not RESEND_API_KEY or not ALERT_EMAIL:
        return
    try:
        import urllib.request
        import html as html_mod
        cuerpo = html_mod.escape(texto).replace("\n", "<br>")
        if url:
            cuerpo += f'<br><br><a href="{html_mod.escape(url)}">Ver producto</a>'
        payload = {"from": RESEND_FROM, "to": ALERT_EMAIL,
                   "subject": asunto, "html": f"<p>{cuerpo}</p>"}
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + RESEND_API_KEY,
                     "Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Safari/537.36",
                     "Accept": "application/json"},
        )
        urllib.request.urlopen(req, timeout=20).read()
        log("Email enviado OK")
    except Exception as e:
        log(f"Email falló: {e}")


def avisar(titulo: str, texto: str, url: str = ""):
    """Aviso remoto: email (si hay RESEND_API_KEY) + Telegram (si configurado)."""
    enviar_email(titulo, texto, url)
    enviar_telegram(texto)


def _verificar_sap(page, sku: str) -> dict:
    """Etapa 2: misma validación que hace el botón Comprar (sin tocar el carro).
    Devuelve dict con detalle/cantidad/codigo/estado."""
    try:
        return page.evaluate("""async (sku) => {
          const emh=((document.querySelector('#du-form-emh')||{}).value)
            || ((document.querySelector('input[name=emh]')||{}).value) || '';
          const m=document.cookie.match(/form_key=([^;]+)/);
          const form_key=m?m[1]:'';
          let captcha='';
          try{
            await new Promise((res,rej)=>{
              let n=0; const iv=setInterval(()=>{
                n++;
                if(window.grecaptcha){ clearInterval(iv); res(1); }
                else if(n>150){ clearInterval(iv); rej('no grecaptcha'); }
              },100);
            });
            captcha=await grecaptcha.execute('SITEKEY',{action:'ajax'});
          }catch(e){ return {error:'captcha:'+e}; }
          const q=`{ dataStockProductoBodegaSap(skuProducto: "${sku}", tipoProducto: "fullprice", origen: "detalle", form_key: "${form_key}", emh: "${emh}", captcha: "${captcha}") { producto { id sku } detalle codigo cantidad estado } }`;
          const r=await fetch('/graphql',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})});
          const j=await r.json();
          const d=(j.data||{}).dataStockProductoBodegaSap||null;
          return {resp: d, raw_len: JSON.stringify(j).length};
        }""".replace("SITEKEY", SITEKEY_RECAPTCHA), sku)
    except Exception as e:
        return {"error": f"evaluate: {e}"}


def chequear_stock(url: str, sku: str = "", timeout_ms: int = 60000,
                   espera_extra_s: int = 12) -> dict:
    """Devuelve dict con veredicto final.
    hay_stock_real: True (verificado) / False (sin stock o falso positivo) / None (dudoso)
    """
    from playwright.sync_api import sync_playwright

    ficha = {}
    texto = ""
    hubo_ficha = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            locale="es-CL",
        )
        page = ctx.new_page()

        def on_response(resp):
            try:
                if "catalogo.movistar.cl/graphql" in resp.url and resp.request.method == "POST":
                    try:
                        body = resp.text()
                    except Exception:
                        return
                    if "getDataProductFicha" in body:
                        try:
                            j = json.loads(body)
                            f = j.get("data", {}).get("getDataProductFicha", {})
                            if f:
                                ficha.update(f)
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(espera_extra_s * 1000)
        # Si la ficha GraphQL no se interceptó (red lenta / bloqueo), un reload
        # antes de fallar a etapa 2 con ficha vacía.
        if not ficha:
            log("Sin ficha interceptada, reintentando con reload...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout((espera_extra_s // 2 + 4) * 1000)
            except Exception as e:
                log(f"Reload falló: {e}")
        try:
            texto = page.inner_text("body")
        except Exception:
            texto = ""

        # --- Etapa 1: ficha ---
        estado_stock = ficha.get("estado_stock") if ficha else None
        estado_producto = ficha.get("estado_producto") if ficha else None
        try:
            stock_vm05 = int(ficha.get("stock_vm05", 0)) if ficha else 0
        except Exception:
            stock_vm05 = 0
        hubo_ficha = bool(ficha)
        ficha_dice_stock = bool(estado_stock) or bool(estado_producto) or (stock_vm05 > 0)

        nombre_pagina = None
        if ficha:
            nombre_pagina = ficha.get("name")
            sku_pagina = ficha.get("sku") or sku
        else:
            try:
                sku_pagina = page.evaluate("() => (document.querySelector('#du-product-sku')||{}).value || ''") or sku
            except Exception:
                sku_pagina = sku

        if not nombre_pagina:
            try:
                import re
                t = page.title()
                if "|" in t:
                    t = t.split("|")[0].strip()
                t = re.sub(r"^\(\d+\)\s*", "", t)
                if t:
                    nombre_pagina = t.strip()
            except Exception:
                pass

        # Sin stock según ficha (+ texto lo respalda) => no hay nada que verificar
        if hubo_ficha and not ficha_dice_stock:
            browser.close()
            return {
                "hay_stock_real": False,
                "etapa": "ficha-sin-stock",
                "nombre": nombre_pagina,
                "ficha": {"estado_stock": estado_stock, "estado_producto": estado_producto,
                          "stock_vm05": stock_vm05, "stock_am47": ficha.get("stock_am47"),
                          "sku": ficha.get("sku") or sku_pagina,
                          "precio": ficha.get("special_price") or ficha.get("price")},
                "verificacion": None,
            }

        # Si la ficha dice stock (o no hubo ficha pero tampoco mensaje de sin stock),
        # pasar a etapa 2. Si hay mensaje explícito de sin stock, no verificar.
        if "Producto temporalmente sin stock" in (texto or "") and not ficha_dice_stock:
            browser.close()
            return {"hay_stock_real": False, "etapa": "dom-sin-stock",
                    "nombre": nombre_pagina,
                    "ficha": ficha if ficha else None, "verificacion": None,
                    "texto_len": len(texto or "")}

        # --- Etapa 2: verificación SAP real ---
        log(f"Etapa 1 dice posible stock (ficha={json.dumps({'estado_stock': estado_stock, 'stock_vm05': stock_vm05}, ensure_ascii=False)}). Verificando en bodega SAP...")
        sap = _verificar_sap(page, sku_pagina)
        browser.close()

        if sap.get("error"):
            return {"hay_stock_real": None, "etapa": "sap-error",
                    "nombre": nombre_pagina,
                    "ficha": {"estado_stock": estado_stock, "estado_producto": estado_producto,
                              "stock_vm05": stock_vm05, "sku": ficha.get("sku") if ficha else sku_pagina,
                              "precio": (ficha.get("special_price") or ficha.get("price")) if ficha else None},
                    "verificacion": sap}

        d = sap.get("resp") or {}
        # Sin respuesta del servicio SAP (resp vacío/nulo): no es verificable,
        # NO clasificar como falso-positivo. Queda dudoso para reintentar.
        if not d:
            return {"hay_stock_real": None, "etapa": "sap-error",
                    "nombre": nombre_pagina,
                    "ficha": {"estado_stock": estado_stock, "estado_producto": estado_producto,
                              "stock_vm05": stock_vm05, "sku": ficha.get("sku") if ficha else sku_pagina,
                              "precio": (ficha.get("special_price") or ficha.get("price")) if ficha else None},
                    "verificacion": {"error": "sap-sin-respuesta", "raw_len": sap.get("raw_len"),
                                     "hubo_ficha": hubo_ficha, "texto_len": len(texto or "")}}
        detalle = (d.get("detalle") or "").strip()
        try:
            cantidad = int(str(d.get("cantidad", "0")))
        except Exception:
            cantidad = 0
        real = (detalle == "con-stock" and cantidad > 0)
        return {
            "hay_stock_real": real,
            "etapa": "verificado-con-stock" if real else "falso-positivo",
            "nombre": nombre_pagina,
            "ficha": {"estado_stock": estado_stock, "estado_producto": estado_producto,
                      "stock_vm05": stock_vm05, "sku": ficha.get("sku") if ficha else sku_pagina,
                      "precio": (ficha.get("special_price") or ficha.get("price")) if ficha else None},
            "verificacion": {"detalle": detalle, "cantidad": cantidad,
                             "codigo": d.get("codigo"), "estado": d.get("estado")},
        }


def leer_estado_previo():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def guardar_estado(d: dict):
    try:
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def obtener_estado_producto(estado_previo: dict, url: str, sku: str = "") -> dict:
    if not isinstance(estado_previo, dict):
        return {}
    if "productos" in estado_previo and isinstance(estado_previo["productos"], dict):
        if url in estado_previo["productos"]:
            return estado_previo["productos"][url]
        if sku and sku in estado_previo["productos"]:
            return estado_previo["productos"][sku]
    if url in estado_previo:
        return estado_previo[url]
    # Compatibilidad con formato mono-producto previo
    if "hay_stock_real" in estado_previo:
        ficha_sku = (estado_previo.get("detalle") or {}).get("ficha", {}).get("sku")
        if (sku and ficha_sku == sku) or (url == URL_OBJETIVO):
            return estado_previo
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, help="URL de un producto específico (si se omite, monitorea lista por defecto)")
    ap.add_argument("--sku", default=None, help="SKU del producto específico")
    ap.add_argument("--interval", type=int, default=600, help="segundos entre revisiones en modo loop")
    ap.add_argument("--once", action="store_true", help="una sola revisión y salir")
    ap.add_argument("--no-open", action="store_true", help="no abrir el navegador al detectar stock")
    args = ap.parse_args()

    if args.url:
        productos = [{"nombre": "Producto objetivo", "url": args.url, "sku": args.sku or ""}]
    else:
        productos = PRODUCTOS

    log(f"Monitor (2 etapas) iniciado. {len(productos)} producto(s) en seguimiento, intervalo={args.interval}s")

    def revisar_producto(prod):
        nombre = prod.get("nombre", "Producto")
        url = prod["url"]
        sku = prod.get("sku", "")
        try:
            r = chequear_stock(url, sku)
        except Exception as e:
            log(f"ERROR al chequear {nombre} ({url}): {e}")
            notify_desktop("Movistar monitor: error", f"No se pudo revisar {nombre}: {e}")
            return None
        nombre_detectado = prod.get("nombre") or r.get("nombre") or "Producto"
        r["nombre"] = nombre_detectado
        log(f"[{nombre_detectado}] Resultado: hay_stock_real={r.get('hay_stock_real')} etapa={r.get('etapa')} "
            f"ficha={json.dumps(r.get('ficha'), ensure_ascii=False)[:300]} "
            f"verificacion={json.dumps(r.get('verificacion'), ensure_ascii=False)[:300]}")
        return r

    if args.once:
        algun_stock = False
        algun_stock_nuevo = False
        todos_sin_stock = True
        hubo_error = False
        estado_completo = leer_estado_previo() if CLOUD else {}

        for i, prod in enumerate(productos):
            if i > 0:
                time.sleep(2)
            r = revisar_producto(prod)
            if r is None:
                hubo_error = True
                continue
            nombre = r.get("nombre") or prod.get("nombre", "Producto")
            url = prod["url"]
            sku = prod.get("sku", "")

            if CLOUD:
                if "productos" not in estado_completo or not isinstance(estado_completo["productos"], dict):
                    estado_completo["productos"] = {}
                prev_hay = obtener_estado_producto(estado_completo, url, sku).get("hay_stock_real")
                estado_completo["productos"][url] = {
                    "nombre": nombre,
                    "hay_stock_real": r.get("hay_stock_real"),
                    "ts": datetime.datetime.now().isoformat(),
                    "detalle": r,
                }

            if r.get("hay_stock_real") is True:
                algun_stock = True
                todos_sin_stock = False
                v = r.get("verificacion", {}) or {}
                es_nuevo = (not CLOUD) or (prev_hay is not True)
                if es_nuevo:
                    algun_stock_nuevo = True
                    msg = (f"STOCK REAL: {nombre}\n"
                           f"Bodega: cantidad={v.get('cantidad')} detalle={v.get('detalle')}\n{url}")
                    log(f">>> ¡STOCK REAL DETECTADO para {nombre}! Avisando...")
                    notify_desktop(f"🟢 ¡STOCK REAL! {nombre}",
                                   f"Verificado en bodega: cantidad={v.get('cantidad')} | {url}", urgente=True)
                    sonar_alerta(2)
                    if not args.no_open:
                        abrir_pagina(url)
                    avisar(f"STOCK REAL: {nombre}", msg, url)
                else:
                    log(f"[{nombre}] Sigue con stock real (ya avisado, sin re-aviso).")
            elif r.get("etapa") == "falso-positivo":
                todos_sin_stock = False
                log(f"[{nombre}] Falso positivo detectado (ficha dice stock, bodega dice sin-stock). Sin aviso.")
            elif r.get("hay_stock_real") is False:
                notify_desktop("Movistar monitor: sin stock", f"{nombre}: Sigue sin stock.")
            else:
                todos_sin_stock = False
                notify_desktop("Movistar monitor: dudoso", f"{nombre}: No se pudo determinar el stock. Revisa el log.")

        if CLOUD:
            estado_completo["ultimo_ciclo"] = datetime.datetime.now().isoformat()
            guardar_estado(estado_completo)

        if algun_stock:
            sys.exit(10)
        if hubo_error:
            sys.exit(2)
        if todos_sin_stock:
            sys.exit(0)
        sys.exit(3)

    # Modo loop infinito: avisa fuerte SOLO con stock real verificado
    estado_completo = leer_estado_previo()
    nombres_prods = "\n• ".join(p.get("nombre", p["url"]) for p in productos)
    notify_desktop("Movistar monitor activado (anti-falsos-positivos)",
                   f"Reviso cada {args.interval}s {len(productos)} productos:\n• {nombres_prods}")
    falsos_seguidos = {p["url"]: 0 for p in productos}

    while True:
        for i, prod in enumerate(productos):
            if i > 0:
                time.sleep(3)
            url = prod["url"]
            nombre = prod.get("nombre", url)
            sku = prod.get("sku", "")
            prev_info = obtener_estado_producto(estado_completo, url, sku)
            prev_hay = prev_info.get("hay_stock_real")

            r = revisar_producto(prod)
            if r is not None:
                hay = r.get("hay_stock_real")
                nombre_real = r.get("nombre") or nombre

                if "productos" not in estado_completo or not isinstance(estado_completo["productos"], dict):
                    estado_completo["productos"] = {}
                estado_completo["productos"][url] = {
                    "nombre": nombre_real,
                    "hay_stock_real": hay,
                    "ts": datetime.datetime.now().isoformat(),
                    "detalle": r,
                }
                estado_completo["ultimo_ciclo"] = datetime.datetime.now().isoformat()
                guardar_estado(estado_completo)

                if hay is True and prev_hay is not True:
                    v = r.get("verificacion", {})
                    msg = (f"🟢 ¡HAY STOCK REAL! {nombre_real}\n"
                           f"Bodega: cantidad={v.get('cantidad')} detalle={v.get('detalle')}\n{url}")
                    log(f">>> ¡STOCK REAL DETECTADO para {nombre_real}! Avisando...")
                    notify_desktop(f"🟢 ¡STOCK REAL! {nombre_real}", msg, urgente=True)
                    for _ in range(3):
                        sonar_alerta(2)
                    if not args.no_open:
                        abrir_pagina(url)
                    avisar(f"STOCK REAL: {nombre_real}", msg, url)
                    falsos_seguidos[url] = 0
                elif hay is True and prev_hay is True:
                    log(f"[{nombre_real}] Sigue con stock real, re-aviso corto")
                    notify_desktop(f"🟢 Sigue con stock real: {nombre_real}", url, urgente=True)
                    sonar_alerta(1)
                elif r.get("etapa") == "falso-positivo":
                    falsos_seguidos[url] = falsos_seguidos.get(url, 0) + 1
                    log(f"[{nombre_real}] Falso positivo #{falsos_seguidos[url]} (ficha con stock, bodega sin stock). Sin aviso sonoro.")
                elif hay is False:
                    falsos_seguidos[url] = 0
                else:
                    log(f"[{nombre_real}] Estado dudoso/error, reintento en próximo ciclo")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
