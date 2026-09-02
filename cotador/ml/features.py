from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

RAIO_TERRA_KM = 6371.0088
ARQUIVOS_OLIST = {
    "olist_customers_dataset.csv", "olist_geolocation_dataset.csv", "olist_order_items_dataset.csv",
    "olist_orders_dataset.csv", "olist_products_dataset.csv", "olist_sellers_dataset.csv",
}
NUMERICAS = ["distance_km", "weight_total_kg", "quantity_items", "declared_value", "month"]
CATEGORICAS = ["origin_state", "dest_state", "route", "same_state", "geo_profile"]
FEATURES = NUMERICAS + CATEGORICAS
LOG_NUMERICAS = ["distance_km", "weight_total_kg", "quantity_items", "declared_value"]

CAPITAIS = {
    "aracaju", "belem", "belo horizonte", "boa vista", "brasilia", "campo grande",
    "cuiaba", "curitiba", "florianopolis", "fortaleza", "goiania", "joao pessoa",
    "macapa", "maceio", "manaus", "natal", "palmas", "porto alegre", "porto velho",
    "recife", "rio branco", "rio de janeiro", "salvador", "sao luis", "sao paulo",
    "teresina", "vitoria",
}


def texto_normalizado(valor: object) -> str:
    texto = str(valor or "").strip().lower()
    texto = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto)


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * RAIO_TERRA_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def perfil_geografico(cidade_origem: str, cidade_destino: str) -> str:
    origem = "capital" if texto_normalizado(cidade_origem) in CAPITAIS else "interior"
    destino = "capital" if texto_normalizado(cidade_destino) in CAPITAIS else "interior"
    return f"{origem}_{destino}"


def preparar_features(dados: pd.DataFrame) -> pd.DataFrame:
    x = dados[FEATURES].copy()
    for coluna in NUMERICAS:
        x[coluna] = pd.to_numeric(x[coluna], errors="coerce").fillna(0).clip(lower=0)
    for coluna in CATEGORICAS:
        x[coluna] = x[coluna].fillna("desconhecido").astype(str)
    for coluna in LOG_NUMERICAS:
        x[coluna] = np.log1p(x[coluna])
    return x


def mapa_geografico(geo: pd.DataFrame) -> pd.DataFrame:
    coordenadas = geo.groupby("geolocation_zip_code_prefix", as_index=False)[["geolocation_lat", "geolocation_lng"]].median()
    rotulos = geo.groupby("geolocation_zip_code_prefix")[["geolocation_city", "geolocation_state"]].agg(
        lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]
    ).reset_index()
    return coordenadas.merge(rotulos, on="geolocation_zip_code_prefix").rename(columns={
        "geolocation_zip_code_prefix": "zip_prefix", "geolocation_lat": "lat", "geolocation_lng": "lng",
        "geolocation_city": "city", "geolocation_state": "state",
    })
