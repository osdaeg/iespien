#!/usr/bin/env python3
"""
IESPIEN — Generador de calendario deportivo
Consulta Oracle, genera dashboard HTML y lo deposita en nginx.
"""

import yaml
import json
import requests
import logging
import sys
import os
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [IESPIEN] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("iespien")

CONFIG_PATH = os.environ.get("IESPIEN_CONFIG", "/app/config.yaml")
CACHE_DIR   = Path("/app/cache")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_fechas(timezone_str: str) -> list[str]:
    """Devuelve hoy + 2 días siguientes en formato DD/MM/YYYY."""
    tz = ZoneInfo(timezone_str)
    hoy = datetime.now(tz).date()
    return [(hoy + timedelta(days=i)).strftime("%d/%m/%Y") for i in range(3)]


def hoy_str(timezone_str: str) -> str:
    """Fecha de hoy en formato YYYY-MM-DD para nombres de archivo de cache."""
    tz = ZoneInfo(timezone_str)
    return datetime.now(tz).strftime("%Y-%m-%d")


def cache_path(nombre_deporte: str, fecha: str) -> Path:
    """Devuelve el path del archivo de cache para un deporte y fecha."""
    slug = nombre_deporte.lower().replace(" ", "_").replace("/", "-")
    return CACHE_DIR / f"{slug}_{fecha}.json"


def load_cache(nombre_deporte: str, fecha: str) -> dict | None:
    """Carga datos cacheados si existen para hoy."""
    path = cache_path(nombre_deporte, fecha)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            log.info(f"  ✓ Cache hit — usando datos del archivo {path.name}")
            return data
        except Exception as e:
            log.warning(f"  ✗ Cache corrupto ({path.name}): {e} — se reconsultará Oracle")
            return None
    return None


def save_cache(nombre_deporte: str, fecha: str, data: dict):
    """Guarda los datos en cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(nombre_deporte, fecha)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"  ✓ Cache guardado: {path.name}")


def build_schema() -> dict:
    return {
        "fecha_consulta": "string",
        "eventos": [{
            "competicion": "string",
            "contrincantes": "string",
            "fecha": "string",
            "hora": "string",
            "escenario": "string",
            "lugar": "string",
            "canales": ["string"]
        }]
    }


def query_oracle(oracle_url: str, source: str, prompt: str) -> dict | None:
    payload = {
        "prompt": prompt,
        "schema": build_schema(),
        "ai": "gemini",
        "grounding": True,
        "source": source
    }
    try:
        log.info(f"Consultando Oracle: {oracle_url}")
        r = requests.post(oracle_url, json=payload, timeout=None)
        r.raise_for_status()
        resp = r.json()

        if resp.get("status") != "ok":
            log.error(f"Oracle devolvió error: {resp.get('error')}")
            return None

        log.info(f"  ✓ Modelo: {resp.get('model_used')} | Grounding: {resp.get('grounding_used')}")
        return resp.get("data")

    except requests.exceptions.ConnectionError:
        log.error("No se pudo conectar con Oracle. ¿Está corriendo el contenedor?")
        return None
    except Exception as e:
        log.error(f"Error inesperado consultando Oracle: {e}")
        return None


def agrupar_por_fecha(eventos: list[dict]) -> dict:
    """Agrupa eventos por fecha, ordenados cronológicamente."""
    agrupado = {}
    for ev in eventos:
        fecha = ev.get("fecha", "Sin fecha")
        if fecha not in agrupado:
            agrupado[fecha] = []
        agrupado[fecha].append(ev)

    for fecha in agrupado:
        agrupado[fecha].sort(key=lambda e: e.get("hora", "99:99"))

    def parse_fecha(f):
        try:
            return datetime.strptime(f, "%d/%m/%Y")
        except:
            return datetime.max

    return dict(sorted(agrupado.items(), key=lambda x: parse_fecha(x[0])))


def nombre_dia(fecha_str: str, tz_str: str) -> str:
    """Convierte DD/MM/YYYY a 'Hoy', 'Mañana' o nombre del día."""
    try:
        tz = ZoneInfo(tz_str)
        hoy = datetime.now(tz).date()
        fecha = datetime.strptime(fecha_str, "%d/%m/%Y").date()
        delta = (fecha - hoy).days
        if delta == 0:
            return "Hoy"
        elif delta == 1:
            return "Mañana"
        else:
            dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
            return dias[fecha.weekday()]
    except:
        return fecha_str


def prepare_output_dir(cfg: dict):
    """Crea directorio de output y copia íconos si hace falta."""
    output_file = Path(cfg["dashboard"]["output_path"])
    output_dir = output_file.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    icons_src = Path("/app/icons")
    icons_dst = Path(cfg["dashboard"]["icons_path"])
    if icons_src.exists():
        icons_dst.mkdir(parents=True, exist_ok=True)
        for icon in icons_src.iterdir():
            dst = icons_dst / icon.name
            if not dst.exists():
                shutil.copy2(icon, dst)
                log.info(f"  ✓ Ícono copiado: {icon.name}")


def render_dashboard(cfg: dict, resultados: list[dict], fechas: list[str]) -> str:
    tz_str = cfg["dashboard"]["timezone"]
    env = Environment(loader=FileSystemLoader("/app"), autoescape=True)
    template = env.get_template("template.html")

    for res in resultados:
        eventos = res.get("eventos", [])
        res["agrupado"] = agrupar_por_fecha(eventos)
        res["total"] = len(eventos)
        res["grupos"] = [
            {
                "fecha": fecha,
                "label": nombre_dia(fecha, tz_str),
                "eventos": evs
            }
            for fecha, evs in res["agrupado"].items()
        ]

    now = datetime.now(ZoneInfo(tz_str))
    return template.render(
        titulo=cfg["dashboard"]["title"],
        resultados=resultados,
        fechas=fechas,
        generado_en=now.strftime("%d/%m/%Y %H:%M"),
        total_eventos=sum(r["total"] for r in resultados)
    )


def main():
    log.info("═" * 50)
    log.info("IESPIEN arrancando")

    cfg = load_config()
    oracle_url    = cfg["oracle"]["url"]
    oracle_source = cfg["oracle"]["source"]
    tz_str        = cfg["dashboard"]["timezone"]

    fechas    = get_fechas(tz_str)
    fecha_hoy = hoy_str(tz_str)
    log.info(f"Fechas objetivo: {' | '.join(fechas)}")

    deportes_activos = [d for d in cfg["deportes"] if d.get("activo", False)]
    log.info(f"Deportes activos: {[d['nombre'] for d in deportes_activos]}")

    resultados = []

    for deporte in deportes_activos:
        nombre = deporte["nombre"]
        log.info(f"\n▶ Procesando: {nombre}")

        # Intentar cache primero
        data = load_cache(nombre, fecha_hoy)

        if data is None:
            # No hay cache — consultar Oracle
            fechas_str = ", ".join(fechas)
            prompt = deporte["prompt"].replace("{fechas}", fechas_str)
            data = query_oracle(oracle_url, oracle_source, prompt)

            if data is None:
                log.warning(f"  ✗ Sin datos para {nombre}, se omite.")
                continue

            # Guardar en cache
            save_cache(nombre, fecha_hoy, data)

        eventos = data.get("eventos", [])
        log.info(f"  ✓ {len(eventos)} eventos")

        resultados.append({
            "nombre": nombre,
            "icono": deporte.get("icono", ""),
            "eventos": eventos,
        })

    if not resultados:
        log.error("No se obtuvieron datos de ningún deporte. Abortando.")
        sys.exit(1)

    log.info(f"\n▶ Generando dashboard...")
    prepare_output_dir(cfg)

    html = render_dashboard(cfg, resultados, fechas)

    output_path = Path(cfg["dashboard"]["output_path"])
    output_path.write_text(html, encoding="utf-8")
    log.info(f"  ✓ Dashboard guardado en {output_path}")

    total = sum(r["total"] for r in resultados)
    log.info("═" * 50)
    log.info(f"IESPIEN finalizado · {total} eventos · {len(resultados)} deportes")


if __name__ == "__main__":
    main()
