from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import IsolationForest
from sklearn.metrics import mean_absolute_error, median_absolute_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .features import CATEGORICAS, FEATURES, NUMERICAS, haversine_km, perfil_geografico, preparar_features

ARQUIVOS_OLIST = {
    "olist_customers_dataset.csv", "olist_geolocation_dataset.csv", "olist_order_items_dataset.csv",
    "olist_orders_dataset.csv", "olist_products_dataset.csv", "olist_sellers_dataset.csv",
}


def localizar_zip_olist(pasta: Path) -> Path:
    candidatos = [pasta] if pasta.is_file() else sorted(pasta.glob("*.zip"))
    for caminho in candidatos:
        try:
            with zipfile.ZipFile(caminho) as arquivo:
                nomes = {Path(n).name for n in arquivo.namelist()}
                if ARQUIVOS_OLIST.issubset(nomes):
                    return caminho
        except zipfile.BadZipFile:
            continue
    raise FileNotFoundError(f"nenhum ZIP Olist válido encontrado em {pasta}")


def _ler(arquivo: zipfile.ZipFile, nome: str) -> pd.DataFrame:
    membro = next(n for n in arquivo.namelist() if Path(n).name == nome)
    with arquivo.open(membro) as stream:
        return pd.read_csv(stream, low_memory=False)


def mapa_geografico(geo: pd.DataFrame) -> pd.DataFrame:
    coordenadas = geo.groupby("geolocation_zip_code_prefix", as_index=False)[["geolocation_lat", "geolocation_lng"]].median()
    rotulos = geo.groupby("geolocation_zip_code_prefix")[["geolocation_city", "geolocation_state"]].agg(
        lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]
    ).reset_index()
    return coordenadas.merge(rotulos, on="geolocation_zip_code_prefix").rename(columns={
        "geolocation_zip_code_prefix": "zip_prefix", "geolocation_lat": "lat", "geolocation_lng": "lng",
        "geolocation_city": "city", "geolocation_state": "state",
    })


def carregar_embarques(caminho_zip: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    with zipfile.ZipFile(caminho_zip) as arquivo:
        clientes = _ler(arquivo, "olist_customers_dataset.csv")
        geo = mapa_geografico(_ler(arquivo, "olist_geolocation_dataset.csv"))
        itens = _ler(arquivo, "olist_order_items_dataset.csv")
        pedidos = _ler(arquivo, "olist_orders_dataset.csv")
        produtos = _ler(arquivo, "olist_products_dataset.csv")
        vendedores = _ler(arquivo, "olist_sellers_dataset.csv")

    pedidos = pedidos.loc[pedidos["order_status"].eq("delivered"), ["order_id", "customer_id", "order_purchase_timestamp"]]
    base = itens.merge(pedidos, on="order_id").merge(
        produtos[["product_id", "product_weight_g"]], on="product_id", how="left"
    )
    embarques = base.groupby(["order_id", "seller_id", "customer_id", "order_purchase_timestamp"], as_index=False).agg(
        quantity_items=("order_item_id", "count"),
        declared_value=("price", "sum"),
        freight_value=("freight_value", "sum"),
        weight_total_g=("product_weight_g", "sum"),
    )
    embarques = embarques.merge(clientes[["customer_id", "customer_zip_code_prefix"]], on="customer_id").merge(
        vendedores[["seller_id", "seller_zip_code_prefix"]], on="seller_id"
    )
    origem = geo.rename(columns={"zip_prefix": "seller_zip_code_prefix", "lat": "origin_lat", "lng": "origin_lng", "city": "origin_city", "state": "origin_state"})
    destino = geo.rename(columns={"zip_prefix": "customer_zip_code_prefix", "lat": "dest_lat", "lng": "dest_lng", "city": "dest_city", "state": "dest_state"})
    embarques = embarques.merge(origem, on="seller_zip_code_prefix", how="left").merge(destino, on="customer_zip_code_prefix", how="left")
    embarques["distance_km"] = haversine_km(embarques["origin_lat"], embarques["origin_lng"], embarques["dest_lat"], embarques["dest_lng"])
    embarques["weight_total_kg"] = embarques["weight_total_g"] / 1000.0
    embarques["month"] = pd.to_datetime(embarques["order_purchase_timestamp"], errors="coerce").dt.month
    embarques["origin_state"] = embarques["origin_state"].astype(str).str.upper()
    embarques["dest_state"] = embarques["dest_state"].astype(str).str.upper()
    embarques["route"] = embarques["origin_state"] + ">" + embarques["dest_state"]
    embarques["same_state"] = np.where(embarques["origin_state"].eq(embarques["dest_state"]), "same_state", "interstate")
    embarques["geo_profile"] = [perfil_geografico(a, b) for a, b in zip(embarques["origin_city"], embarques["dest_city"])]
    obrigatorias = [*NUMERICAS, *CATEGORICAS, "freight_value", "order_purchase_timestamp"]
    embarques = embarques.replace([np.inf, -np.inf], np.nan).dropna(subset=obrigatorias)
    embarques = embarques.loc[(embarques["freight_value"] >= 0) & (embarques["weight_total_kg"] > 0) & (embarques["quantity_items"] > 0)].copy()
    return embarques.sort_values("order_purchase_timestamp").reset_index(drop=True), geo


def _pipeline_outlier(contamination: float, seed: int) -> Pipeline:
    transformador = ColumnTransformer([
        ("num", StandardScaler(), NUMERICAS),
        ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=True), CATEGORICAS),
    ])
    return Pipeline([
        ("features", transformador),
        # Um processo evita falhas de criação de pipes no Windows, comuns em
        # laboratórios/sandboxes, e mantém o resultado reprodutível.
        ("isolation", IsolationForest(n_estimators=200, contamination=contamination, random_state=seed, n_jobs=1)),
    ])


def treinar(caminho_zip: Path, saida: Path, *, iterations: int = 450, depth: int = 8, learning_rate: float = .07, contamination: float = .01, seed: int = 42) -> dict:
    dados, geo = carregar_embarques(localizar_zip_olist(caminho_zip))
    n = len(dados); fim_treino = int(n * .70); fim_calibracao = int(n * .85)
    treino, calibracao, teste = dados.iloc[:fim_treino], dados.iloc[fim_treino:fim_calibracao], dados.iloc[fim_calibracao:]
    x_treino, x_calibracao, x_teste = map(preparar_features, (treino, calibracao, teste))
    y_treino = np.log1p(treino["freight_value"].to_numpy(float))
    y_calibracao = np.log1p(calibracao["freight_value"].to_numpy(float))
    parametros = {"iterations": iterations, "depth": depth, "learning_rate": learning_rate, "random_seed": seed,
                  "verbose": False, "allow_writing_files": False, "thread_count": -1}
    saida.mkdir(parents=True, exist_ok=True)
    modelos, ajustes = {}, {}
    for nome, alpha in (("p25", .25), ("p50", .50), ("p75", .75)):
        modelo = CatBoostRegressor(loss_function=f"Quantile:alpha={alpha}", **parametros)
        modelo.fit(x_treino, y_treino, cat_features=CATEGORICAS)
        modelo.save_model(str(saida / f"model_{nome}.cbm"))
        modelos[nome] = modelo
        residuos = y_calibracao - modelo.predict(x_calibracao)
        ajustes[nome] = float(np.quantile(residuos, alpha))

    previstos = {nome: np.expm1(modelo.predict(x_teste) + ajustes[nome]) for nome, modelo in modelos.items()}
    matriz = np.sort(np.maximum(np.vstack([previstos["p25"], previstos["p50"], previstos["p75"]]).T, 0), axis=1)
    real = teste["freight_value"].to_numpy(float); p25, p50, p75 = matriz.T
    metricas = {
        "mae_p50": float(mean_absolute_error(real, p50)),
        "median_absolute_error_p50": float(median_absolute_error(real, p50)),
        "median_absolute_percentage_error_p50": float(np.median(np.abs((real[real > 0] - p50[real > 0]) / real[real > 0])) * 100),
        "coverage_p25_p75": float(np.mean((real >= p25) & (real <= p75))),
        "below_p25": float(np.mean(real < p25)), "above_p75": float(np.mean(real > p75)),
    }
    estrutural = _pipeline_outlier(contamination, seed); estrutural.fit(x_treino)
    joblib.dump(estrutural, saida / "structural_outlier.joblib")
    geo.to_csv(saida / "geo_map.csv", index=False)
    versao = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    metadata = {
        "model_version": versao, "trained_at": datetime.now(timezone.utc).isoformat(),
        "unit_of_analysis": "order_id+seller_id", "target": "sum(freight_value)",
        "total_rows": n, "training_rows": len(treino), "calibration_rows": len(calibracao), "test_rows": len(teste),
        "features": FEATURES, "log1p_numeric_features": [c for c in NUMERICAS if c != "month"],
        "calibration_log_adjustments": ajustes, "catboost_parameters": parametros,
        "isolation_contamination": contamination, "metrics": metricas,
        "limitations": "Benchmark de encomendas Olist; cargas especiais, pallets e modais não rodoviários exigem revisão.",
    }
    (saida / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return metadata
