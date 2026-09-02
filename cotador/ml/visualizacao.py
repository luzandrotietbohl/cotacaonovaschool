from __future__ import annotations

import json
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from catboost import CatBoostRegressor
from sklearn.decomposition import TruncatedSVD
from sklearn.manifold import TSNE

from .features import FEATURES, preparar_features
from .treinamento import carregar_embarques, localizar_zip_olist


def gerar_tsne(caminho_zip: Path, artefatos: Path, saida: Path, limite: int = 5000, seed: int = 42) -> dict:
    dados, _ = carregar_embarques(localizar_zip_olist(caminho_zip))
    metadata = json.loads((artefatos / "metadata.json").read_text(encoding="utf-8"))
    ajustes = metadata["calibration_log_adjustments"]
    x = preparar_features(dados)
    previstos = {}
    for nome in ("p25", "p50", "p75"):
        modelo = CatBoostRegressor(); modelo.load_model(str(artefatos / f"model_{nome}.cbm"))
        previstos[nome] = np.maximum(0, np.expm1(modelo.predict(x) + ajustes[nome]))
    matriz = np.sort(np.vstack([previstos["p25"], previstos["p50"], previstos["p75"]]).T, axis=1)
    p25, p50, p75 = matriz.T
    sigma = np.maximum((np.log1p(p75) - np.log1p(p25)) / 1.349, .05)
    z = (np.log1p(dados["freight_value"].to_numpy()) - np.log1p(p50)) / sigma
    estrutural = joblib.load(artefatos / "structural_outlier.joblib")
    flag_estrutural = estrutural.predict(x) == -1
    score = estrutural.decision_function(x)
    importantes = np.flatnonzero(flag_estrutural | (np.abs(z) > 3.5))
    importantes = importantes[np.argsort(np.abs(z[importantes]))[::-1]][: limite // 2]
    restantes = np.setdiff1d(np.arange(len(dados)), importantes)
    rng = np.random.default_rng(seed)
    normais = rng.choice(restantes, size=min(limite - len(importantes), len(restantes)), replace=False)
    indices = np.concatenate([importantes, normais])
    amostra = dados.iloc[indices].reset_index(drop=True)
    x_amostra = preparar_features(amostra)
    transformado = estrutural.named_steps["features"].transform(x_amostra)
    componentes = min(30, transformado.shape[0] - 1, transformado.shape[1] - 1)
    reduzido = TruncatedSVD(n_components=max(2, componentes), random_state=seed).fit_transform(transformado)
    perplexidade = min(40.0, max(5.0, (len(amostra) - 1) / 3))
    coordenadas = TSNE(n_components=2, perplexity=perplexidade, init="pca", learning_rate="auto", random_state=seed).fit_transform(reduzido)
    resultado = amostra[[*FEATURES, "freight_value"]].copy()
    resultado.insert(0, "tsne_2", coordenadas[:, 1]); resultado.insert(0, "tsne_1", coordenadas[:, 0])
    resultado["structural_outlier"] = flag_estrutural[indices]
    resultado["structural_score"] = score[indices]
    resultado["price_outlier"] = np.abs(z[indices]) > 3.5
    resultado["price_z_robust"] = z[indices]
    resultado["both_outliers"] = resultado["structural_outlier"] & resultado["price_outlier"]
    saida.mkdir(parents=True, exist_ok=True)
    csv, png = saida / "olist_tsne.csv", saida / "olist_tsne.png"
    resultado.to_csv(csv, index=False)
    cores = np.select([resultado["both_outliers"], resultado["structural_outlier"], resultado["price_outlier"]], ["purple", "red", "orange"], default="steelblue")
    plt.figure(figsize=(10, 7)); plt.scatter(resultado["tsne_1"], resultado["tsne_2"], c=cores, s=9, alpha=.7)
    plt.title("Olist: embarques no espaço logístico"); plt.xlabel("t-SNE 1"); plt.ylabel("t-SNE 2")
    plt.tight_layout(); plt.savefig(png, dpi=160); plt.close()
    return {"csv": str(csv), "png": str(png), "rows": len(resultado)}
