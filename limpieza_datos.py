"""
Limpieza de datos abiertos del ICE (Costa Rica) para el dashboard de la red de transmisión.

Fuente: Portal de Datos Abiertos del Sector Electricidad, Grupo ICE
        https://datos-ice-se.opendata.arcgis.com

Entradas (data/raw): Subestaciones.csv, LineasDeTransmision.csv,
                     LineasDeTransmision.geojson, Agencias.csv
Salidas  (data/clean): dim_subestaciones.csv, fact_lineas.csv,
                       lineas_transmision.geojson, dim_agencias.csv, log_limpieza.txt

Uso:  python limpieza_datos.py [--raw data/raw] [--out data/clean]
Requiere: pandas, pyproj
"""
import argparse
import json
import math
import re
import unicodedata
from pathlib import Path

import pandas as pd
from pyproj import Transformer

# ---------------------------------------------------------------- utilidades
def norm(texto) -> str:
    """Minúsculas, sin tildes y sin espacios sobrantes (para comparar nombres)."""
    t = unicodedata.normalize("NFD", str(texto).strip())
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def haversine_m(a, b) -> float:
    """Distancia en metros entre dos puntos (lon, lat) sobre la esfera terrestre."""
    r = 6371008.8
    lo1, la1, lo2, la2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# Nombres oficiales con tilde (el dato fuente mezcla ambas formas)
CANTONES = {
    "aserri": "Aserrí", "belen": "Belén", "canas": "Cañas", "escazu": "Escazú",
    "jimenez": "Jiménez", "la union": "La Unión", "limon": "Limón",
    "paraiso": "Paraíso", "perez zeledon": "Pérez Zeledón", "poas": "Poás",
    "pococi": "Pococí", "san jose": "San José", "san ramon": "San Ramón",
    "sarapiqui": "Sarapiquí", "tibas": "Tibás", "tilaran": "Tilarán",
    "vazquez de coronado": "Vázquez de Coronado",
}
DISTRITOS = {
    "angeles": "Ángeles", "canas": "Cañas", "cobano": "Cóbano", "limon": "Limón",
    "penas blancas": "Peñas Blancas", "rio blanco": "Río Blanco",
    "rio cuarto": "Río Cuarto", "sarapiqui": "Sarapiquí", "san jose": "San José",
    "ribera": "La Ribera",  # mismo código de distrito (40702) que "La Ribera"
}
PROVINCIAS = {"san jose": "San José", "limon": "Limón"}
COD_PROVINCIA = {1: "San José", 2: "Alajuela", 3: "Cartago", 4: "Heredia",
                 5: "Guanacaste", 6: "Puntarenas", 7: "Limón"}


def titulo(valor, tabla):
    return tabla.get(norm(valor), str(valor).strip())


def main(raw: Path, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    log = []

    # ------------------------------------------------------------ SUBESTACIONES
    s = pd.read_csv(raw / "Subestaciones.csv", encoding="utf-8-sig")
    n0 = {c: s[c].nunique() for c in ("Provincia", "Canton", "Distrito")}

    s["provincia"] = s["Provincia"].map(lambda v: titulo(v, PROVINCIAS))
    s["canton"] = s["Canton"].map(lambda v: titulo(v, CANTONES))
    s["distrito"] = s["Distrito"].map(lambda v: titulo(v, DISTRITOS))

    # Validación cruzada: el 1er dígito del código de distrito = provincia
    prov_codigo = (s["CodDistrit"] // 10000).map(COD_PROVINCIA)
    incoherentes = s[prov_codigo != s["provincia"]]
    log.append(f"Provincia vs código de distrito: {len(incoherentes)} filas incoherentes")

    # Coordenadas: X/Y vienen en Web Mercator (EPSG:3857) -> lat/lon (EPSG:4326)
    t = Transformer.from_crs(3857, 4326, always_xy=True)
    s["longitud"], s["latitud"] = t.transform(s["X"].values, s["Y"].values)

    # Control cruzado contra las coordenadas CRTM05 originales (EPSG:5367)
    t05 = Transformer.from_crs(5367, 4326, always_xy=True)
    lo2, la2 = t05.transform(s["PuntoX"].values, s["PuntoY"].values)
    desv = [haversine_m((a, b), (c, d)) for a, b, c, d in zip(s["longitud"], s["latitud"], lo2, la2)]
    log.append(f"Subestaciones: diferencia máxima entre Web Mercator y CRTM05 = {max(desv):.0f} m "
               f"(mediana {sorted(desv)[len(desv)//2]:.0f} m)")

    dim_sub = pd.DataFrame({
        "id_subestacion": range(1, len(s) + 1),
        "subestacion": s["Subestacio"].str.strip(),
        "codigo": s["Observacio"].str.strip(),
        "provincia": s["provincia"],
        "canton": s["canton"],
        "distrito": s["distrito"],
        "cod_distrito": s["CodDistrit"],
        "latitud": s["latitud"].round(6),
        "longitud": s["longitud"].round(6),
    }).sort_values("subestacion").reset_index(drop=True)
    dim_sub["id_subestacion"] = range(1, len(dim_sub) + 1)

    log.append(f"Provincias: {n0['Provincia']} variantes -> {dim_sub.provincia.nunique()}")
    log.append(f"Cantones:   {n0['Canton']} variantes -> {dim_sub.canton.nunique()}")
    log.append(f"Distritos (por código oficial): {dim_sub.cod_distrito.nunique()} "
               f"(por nombre habría {dim_sub.distrito.nunique()}: los nombres se repiten entre cantones)")
    dim_sub.to_csv(out / "dim_subestaciones.csv", index=False, encoding="utf-8-sig")

    # ------------------------------------------------------------------- LÍNEAS
    gj = json.load(open(raw / "LineasDeTransmision.geojson", encoding="utf-8"))
    # Alias de nombres de subestación usados en el nombre del circuito
    alias = {"garita": "la garita"}
    ids = {norm(n): i for n, i in zip(dim_sub.subestacion, dim_sub.id_subestacion)}
    nombres = dict(zip(dim_sub.id_subestacion, dim_sub.subestacion))

    def resolver(extremo):
        base = re.sub(r"\(.*?\)", "", extremo)               # "Jacó (siepac)" -> "Jacó"
        base = re.sub(r"\d+$", "", base.strip()).strip()      # "Lindora1" -> "Lindora"
        clave = alias.get(norm(base), norm(base))
        return base, ids.get(clave)

    filas = []
    for f in gj["features"]:
        p, g = f["properties"], f["geometry"]
        partes = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        metros = sum(haversine_m(seg[i], seg[i + 1]) for seg in partes for i in range(len(seg) - 1))

        circuito = p["Circuito"].strip()
        trozos = [x for x in re.split(r"\s*-\s*", circuito)]
        if len(trozos) == 2:
            (o_txt, o_id), (d_txt, d_id) = resolver(trozos[0]), resolver(trozos[1])
            num = re.search(r"(\d+)$", trozos[1]) or re.search(r"(\d+)$", trozos[0])
        else:
            o_txt = d_txt = None
            o_id = d_id = None
            num = None
        regional = "SIEPAC" in circuito.upper()
        filas.append({
            "id_linea": p["FID"],
            "circuito": circuito,
            "voltaje_kv": p["Voltaje"],
            "origen": nombres.get(o_id, o_txt),
            "id_origen": o_id,
            "destino": nombres.get(d_id, d_txt),
            "id_destino": d_id,
            "n_circuito": int(num.group(1)) if num else None,
            "tipo": "Regional (SIEPAC)" if regional else "Nacional",
            "extremos_enlazados": int(o_id is not None) + int(d_id is not None),
            "longitud_km": round(metros / 1000, 3),
            "n_tramos": len(partes),
            "longitud_km_fuente_STLe": round(p["SHAPE_STLe"] / 1000, 3),
        })
        f["properties"] = {k: filas[-1][k] for k in
                           ("id_linea", "circuito", "voltaje_kv", "origen", "destino", "tipo", "longitud_km")}

    fact = pd.DataFrame(filas)
    for c in ("id_origen", "id_destino", "n_circuito"):
        fact[c] = fact[c].astype("Int64")
    fact.to_csv(out / "fact_lineas.csv", index=False, encoding="utf-8-sig")
    with open(out / "lineas_transmision.geojson", "w", encoding="utf-8") as fh:
        json.dump(gj, fh, ensure_ascii=False)

    log.append(f"Líneas: {len(fact)} | km totales (geometría) = {fact.longitud_km.sum():,.1f} "
               f"| con SHAPE_STLe de la fuente = {fact.longitud_km_fuente_STLe.sum():,.1f}")
    log.append("Longitud: Shape__Length de la fuente = longitud real x ~1.016 (inflada por Web Mercator); "
               "SHAPE_STLe no coincide con la geometría. Se usa la longitud calculada sobre el GeoJSON.")
    log.append("Extremos enlazados con subestaciones: "
               + ", ".join(f"{k}->{v}" for k, v in fact.extremos_enlazados.value_counts().sort_index().items()))
    sin = fact[(fact.extremos_enlazados < 2)]
    log.append("Líneas con algún extremo sin subestación (cliente, frontera u otra red): "
               + "; ".join(sin.circuito))

    # ------------------------------------------------------------------ AGENCIAS
    a = pd.read_csv(raw / "Agencias.csv", encoding="utf-8-sig", dtype={"Télefono": str, "Fax": str})
    lo, la = t05.transform(a["x"].values, a["y"].values)
    ag = pd.DataFrame({
        "id_agencia": a["OBJECTID_1"],
        "direccion": a["Direccion"].str.strip(),
        "telefono": a["Télefono"].astype(str).str.strip(),
        "fax": a["Fax"].astype(str).str.strip(),
        "horario": a["Horario"].fillna("").str.strip().replace("", "Sin dato"),
        "latitud": [round(v, 6) for v in la],
        "longitud": [round(v, 6) for v in lo],
    })
    ag["sabado"] = ag["horario"].str.contains("Sábado", na=False)
    ag.to_csv(out / "dim_agencias.csv", index=False, encoding="utf-8-sig")
    log.append(f"Agencias: {len(ag)} | columnas vacías en origen descartadas: Nombre, Imagen | "
               f"direcciones repetidas: {ag.direccion.duplicated().sum()} | "
               f"lat {ag.latitud.min():.2f}..{ag.latitud.max():.2f}, lon {ag.longitud.min():.2f}..{ag.longitud.max():.2f}")

    (out / "log_limpieza.txt").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw", type=Path)
    ap.add_argument("--out", default="data/clean", type=Path)
    args = ap.parse_args()
    main(args.raw, args.out)
