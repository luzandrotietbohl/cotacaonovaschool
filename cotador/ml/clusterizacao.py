"""Segmentação não supervisionada dos embarques Olist.

Agrupa os embarques no mesmo grão do precificador (order_id + seller_id) para
descobrir regimes logísticos distintos. O frete fica fora das features de
propósito: ele é a variável a explicar, não a agrupar, e entra apenas no perfil
dos grupos.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans, MiniBatchKMeans
from sklearn.decomposition import PCA
from sklearn.metrics import adjusted_rand_score, davies_bouldin_score, silhouette_score
from sklearn.preprocessing import StandardScaler

from .features import ARQUIVOS_OLIST, haversine_km, mapa_geografico, perfil_geografico

FEATURES_CLUSTER = [
    "distance_km", "weight_total_kg", "cubic_weight_kg",
    "declared_value", "quantity_items", "log_density",
]
LOG1P_CLUSTER = ["distance_km", "weight_total_kg", "cubic_weight_kg", "declared_value", "quantity_items"]
DIVISOR_CUBAGEM = 6000.0
AMOSTRA_SILHUETA = 15000


def _leitor(caminho: Path) -> Callable[[str], pd.DataFrame]:
    """Devolve um leitor de CSV por nome, aceitando ZIP Olist ou pasta de CSVs."""
    caminho = Path(caminho)
    candidatos = [caminho] if caminho.is_file() else sorted(caminho.glob("*.zip"))
    for zipado in candidatos:
        try:
            with zipfile.ZipFile(zipado) as arquivo:
                nomes = {Path(n).name for n in arquivo.namelist()}
        except zipfile.BadZipFile:
            continue
        if ARQUIVOS_OLIST.issubset(nomes):
            def ler(nome: str, _zip: Path = zipado) -> pd.DataFrame:
                with zipfile.ZipFile(_zip) as arquivo:
                    membro = next(n for n in arquivo.namelist() if Path(n).name == nome)
                    with arquivo.open(membro) as stream:
                        return pd.read_csv(stream, low_memory=False)
            return ler
    if caminho.is_dir() and ARQUIVOS_OLIST.issubset({p.name for p in caminho.glob("*.csv")}):
        return lambda nome: pd.read_csv(caminho / nome, low_memory=False)
    raise FileNotFoundError(f"nenhum ZIP nem pasta de CSVs Olist válida em {caminho}")


def carregar_embarques(caminho: Path) -> pd.DataFrame:
    """Base de embarques enriquecida com cubagem, densidade, prazo e nota.

    Mantém o mesmo grão e os mesmos filtros do treinamento do precificador, e
    acrescenta as colunas que só a segmentação usa.
    """
    ler = _leitor(caminho)
    clientes = ler("olist_customers_dataset.csv")
    geo = mapa_geografico(ler("olist_geolocation_dataset.csv"))
    itens = ler("olist_order_items_dataset.csv")
    pedidos = ler("olist_orders_dataset.csv")
    produtos = ler("olist_products_dataset.csv")
    vendedores = ler("olist_sellers_dataset.csv")
    avaliacoes = ler("olist_order_reviews_dataset.csv")

    datas = ["order_purchase_timestamp", "order_delivered_customer_date", "order_estimated_delivery_date"]
    for coluna in datas:
        pedidos[coluna] = pd.to_datetime(pedidos[coluna], errors="coerce")
    pedidos = pedidos.loc[pedidos["order_status"].eq("delivered"), ["order_id", "customer_id", *datas]]

    produtos["volume_cm3"] = produtos["product_length_cm"] * produtos["product_height_cm"] * produtos["product_width_cm"]
    base = itens.merge(pedidos, on="order_id").merge(
        produtos[["product_id", "product_category_name", "product_weight_g", "volume_cm3"]], on="product_id", how="left"
    )
    embarques = base.groupby(["order_id", "seller_id", "customer_id"], as_index=False).agg(
        order_purchase_timestamp=("order_purchase_timestamp", "first"),
        delivered_at=("order_delivered_customer_date", "first"),
        estimated_at=("order_estimated_delivery_date", "first"),
        quantity_items=("order_item_id", "count"),
        declared_value=("price", "sum"),
        freight_value=("freight_value", "sum"),
        weight_total_g=("product_weight_g", "sum"),
        volume_cm3=("volume_cm3", "sum"),
        category=("product_category_name", "first"),
    )
    embarques = embarques.merge(clientes[["customer_id", "customer_zip_code_prefix"]], on="customer_id").merge(
        vendedores[["seller_id", "seller_zip_code_prefix"]], on="seller_id"
    )
    origem = geo.rename(columns={"zip_prefix": "seller_zip_code_prefix", "lat": "origin_lat", "lng": "origin_lng", "city": "origin_city", "state": "origin_state"})
    destino = geo.rename(columns={"zip_prefix": "customer_zip_code_prefix", "lat": "dest_lat", "lng": "dest_lng", "city": "dest_city", "state": "dest_state"})
    embarques = embarques.merge(origem, on="seller_zip_code_prefix", how="left").merge(destino, on="customer_zip_code_prefix", how="left")
    embarques = embarques.merge(
        avaliacoes.groupby("order_id", as_index=False)["review_score"].mean(), on="order_id", how="left"
    )

    embarques["distance_km"] = haversine_km(embarques["origin_lat"], embarques["origin_lng"], embarques["dest_lat"], embarques["dest_lng"])
    embarques["weight_total_kg"] = embarques["weight_total_g"] / 1000.0
    embarques["cubic_weight_kg"] = embarques["volume_cm3"] / DIVISOR_CUBAGEM
    embarques["density"] = embarques["weight_total_kg"] / embarques["cubic_weight_kg"].replace(0, np.nan)
    embarques["month"] = embarques["order_purchase_timestamp"].dt.month
    embarques["origin_state"] = embarques["origin_state"].astype(str).str.upper()
    embarques["dest_state"] = embarques["dest_state"].astype(str).str.upper()
    embarques["same_state"] = embarques["origin_state"].eq(embarques["dest_state"])
    embarques["geo_profile"] = [perfil_geografico(a, b) for a, b in zip(embarques["origin_city"], embarques["dest_city"])]
    embarques["lead_days"] = (embarques["delivered_at"] - embarques["order_purchase_timestamp"]).dt.total_seconds() / 86400
    embarques["delay_days"] = (embarques["delivered_at"] - embarques["estimated_at"]).dt.total_seconds() / 86400
    embarques["freight_per_kg"] = embarques["freight_value"] / embarques["weight_total_kg"].replace(0, np.nan)
    embarques["freight_pct_value"] = 100 * embarques["freight_value"] / embarques["declared_value"].replace(0, np.nan)

    obrigatorias = ["distance_km", "weight_total_kg", "cubic_weight_kg", "declared_value", "quantity_items", "freight_value", "lead_days"]
    embarques = embarques.replace([np.inf, -np.inf], np.nan).dropna(subset=obrigatorias)
    validos = (
        (embarques["freight_value"] > 0) & (embarques["weight_total_kg"] > 0)
        & (embarques["cubic_weight_kg"] > 0) & (embarques["declared_value"] > 0)
        & (embarques["quantity_items"] > 0) & (embarques["lead_days"] > 0)
    )
    return embarques.loc[validos].sort_values("order_purchase_timestamp").reset_index(drop=True)


def preparar_features(dados: pd.DataFrame) -> pd.DataFrame:
    x = pd.DataFrame(index=dados.index)
    for coluna in LOG1P_CLUSTER:
        x[coluna] = np.log1p(pd.to_numeric(dados[coluna], errors="coerce").clip(lower=0))
    x["log_density"] = np.log(dados["density"].clip(lower=1e-3))
    return x[FEATURES_CLUSTER].fillna(0)


def _amostra(n: int, tamanho: int, seed: int) -> np.ndarray:
    if n <= tamanho:
        return np.arange(n)
    return np.random.default_rng(seed).choice(n, tamanho, replace=False)


def escolher_k(matriz: np.ndarray, *, k_min: int = 2, k_max: int = 10, seed: int = 42) -> list[dict]:
    """Varredura de k com MiniBatchKMeans: inércia, silhueta e Davies-Bouldin."""
    indices = _amostra(len(matriz), AMOSTRA_SILHUETA, seed)
    diagnostico = []
    for k in range(k_min, k_max + 1):
        modelo = MiniBatchKMeans(n_clusters=k, n_init=10, random_state=seed, batch_size=4096).fit(matriz)
        diagnostico.append({
            "k": k,
            "inertia": float(modelo.inertia_),
            "silhouette": float(silhouette_score(matriz[indices], modelo.labels_[indices])),
            "davies_bouldin": float(davies_bouldin_score(matriz, modelo.labels_)),
        })
    return diagnostico


def _nomear(perfil: pd.DataFrame) -> dict[int, str]:
    """Rótulos legíveis atribuídos pelo traço dominante de cada grupo.

    Os ids do KMeans não têm significado estável entre execuções, então o nome
    vem sempre dos dados: cada regra reivindica o grupo mais extremo ainda livre.
    """
    regras = [
        ("local_metropolitano", "distance_km", "min"),
        ("carga_pesada", "weight_total_kg", "max"),
        ("multi_item", "quantity_items", "max"),
        ("miudo_baixo_valor", "freight_pct_value", "max"),
        ("denso_compacto", "density", "max"),
        ("volumoso_leve", "cubic_weight_kg", "max"),
    ]
    nomes, livres = {}, list(perfil.index)
    for nome, coluna, sentido in regras:
        if not livres:
            break
        serie = perfil.loc[livres, coluna]
        escolhido = serie.idxmin() if sentido == "min" else serie.idxmax()
        nomes[escolhido] = nome
        livres.remove(escolhido)
    for sobra in livres:
        nomes[sobra] = f"grupo_{sobra}"
    return nomes


def _perfil(dados: pd.DataFrame) -> pd.DataFrame:
    perfil = dados.groupby("cluster").agg(
        shipments=("freight_value", "size"),
        distance_km=("distance_km", "median"),
        weight_total_kg=("weight_total_kg", "median"),
        cubic_weight_kg=("cubic_weight_kg", "median"),
        density=("density", "median"),
        declared_value=("declared_value", "median"),
        quantity_items=("quantity_items", "mean"),
        freight_value=("freight_value", "median"),
        freight_mean=("freight_value", "mean"),
        freight_cv=("freight_value", lambda s: s.std() / s.mean()),
        freight_per_kg=("freight_per_kg", "median"),
        freight_pct_value=("freight_pct_value", "median"),
        lead_days=("lead_days", "median"),
        late_pct=("delay_days", lambda s: 100 * (s > 0).mean()),
        review_score=("review_score", "mean"),
        same_state_pct=("same_state", lambda s: 100 * s.mean()),
    )
    perfil["share_pct"] = 100 * perfil["shipments"] / len(dados)
    perfil["freight_share_pct"] = 100 * dados.groupby("cluster")["freight_value"].sum() / dados["freight_value"].sum()
    perfil["value_share_pct"] = 100 * dados.groupby("cluster")["declared_value"].sum() / dados["declared_value"].sum()
    return perfil


def _estabilidade(matriz: np.ndarray, rotulos: np.ndarray, k: int, *, replicas: int, seed: int) -> dict:
    """ARI entre a partição final e reajustes em subamostras de 80%."""
    aris = []
    for replica in range(replicas):
        gerador = np.random.default_rng(seed + replica)
        sub = gerador.choice(len(matriz), int(.8 * len(matriz)), replace=False)
        reajuste = KMeans(n_clusters=k, n_init=10, random_state=seed + replica).fit(matriz[sub])
        aris.append(float(adjusted_rand_score(rotulos[sub], reajuste.labels_)))
    return {"ari_bootstrap": aris, "ari_bootstrap_mean": float(np.mean(aris))}


def _figura(dados: pd.DataFrame, coordenadas: np.ndarray, perfil: pd.DataFrame, diagnostico: list[dict], variancia: np.ndarray, destino: Path, seed: int) -> None:
    ordem = perfil.sort_values("declared_value").index.tolist()
    cores = dict(zip(ordem, plt.colormaps["tab10"](np.linspace(0, .9, len(ordem)))))
    rotulo = {c: f"{perfil.loc[c, 'nome']} ({perfil.loc[c, 'share_pct']:.0f}%)" for c in ordem}
    figura, eixos = plt.subplots(2, 2, figsize=(15, 11))

    diag = pd.DataFrame(diagnostico)
    eixo = eixos[0, 0]
    eixo.plot(diag["k"], diag["inertia"], "o-", color="#1565C0")
    eixo.set_xlabel("k"); eixo.set_ylabel("inércia", color="#1565C0")
    gemeo = eixo.twinx()
    gemeo.plot(diag["k"], diag["davies_bouldin"], "s--", color="#D84315")
    gemeo.set_ylabel("Davies-Bouldin", color="#D84315")
    eixo.axvline(dados["cluster"].nunique(), color="gray", ls=":")
    eixo.set_title("Escolha de k — inércia e Davies-Bouldin")

    eixo = eixos[0, 1]
    indices = _amostra(len(dados), 12000, seed)
    for grupo in ordem:
        marca = indices[dados["cluster"].to_numpy()[indices] == grupo]
        eixo.scatter(coordenadas[marca, 0], coordenadas[marca, 1], s=4, alpha=.35, color=cores[grupo], label=rotulo[grupo])
    eixo.set_xlabel(f"PC1 {variancia[0]:.0%} — tamanho/valor do embarque")
    eixo.set_ylabel(f"PC2 {variancia[1]:.0%} — densidade")
    eixo.set_title(f"Clusters no plano PCA ({variancia[:2].sum():.0%} da variância)")
    eixo.legend(markerscale=4, fontsize=8, loc="upper left")

    colunas = ["distance_km", "weight_total_kg", "cubic_weight_kg", "density", "declared_value",
               "quantity_items", "freight_value", "freight_per_kg", "freight_pct_value", "lead_days"]
    tabela = perfil.loc[ordem, colunas]
    z = (tabela - tabela.mean()) / tabela.std()
    eixo = eixos[1, 0]
    imagem = eixo.imshow(z.to_numpy(dtype=float), cmap="RdBu_r", vmin=-1.8, vmax=1.8, aspect="auto")
    eixo.set_xticks(range(len(colunas))); eixo.set_xticklabels(colunas, rotation=45, ha="right", fontsize=8)
    eixo.set_yticks(range(len(ordem))); eixo.set_yticklabels([rotulo[c] for c in ordem], fontsize=8)
    for linha in range(len(ordem)):
        for coluna in range(len(colunas)):
            eixo.text(coluna, linha, f"{tabela.iloc[linha, coluna]:.2f}", ha="center", va="center", fontsize=7)
    eixo.set_title("Perfil dos clusters (mediana; cor = z-score entre clusters)")
    figura.colorbar(imagem, ax=eixo, shrink=.7)

    eixo = eixos[1, 1]
    faixas = pd.cut(dados["distance_km"], [0, 50, 150, 300, 600, 1000, 1600, 2500, 9000])
    for grupo in ordem:
        mediana = dados.loc[dados["cluster"].eq(grupo)].groupby(faixas, observed=True)["freight_value"].median()
        centros = [(iv.left + iv.right) / 2 for iv in mediana.index]
        eixo.plot(centros, mediana.to_numpy(), "o-", color=cores[grupo], label=rotulo[grupo])
    eixo.set_xscale("log"); eixo.set_xlabel("distância km (log)"); eixo.set_ylabel("frete mediano R$")
    eixo.set_title("Curva frete × distância por cluster"); eixo.legend(fontsize=8)

    plt.tight_layout(); plt.savefig(destino, dpi=130); plt.close()


def _markdown(perfil: pd.DataFrame, diagnostico: list[dict], metadata: dict, dados: pd.DataFrame) -> str:
    def tabela(df: pd.DataFrame, casas: int = 2) -> str:
        corpo = df.round(casas)
        cabecalho = "| " + " | ".join([corpo.index.name or ""] + list(corpo.columns)) + " |"
        separador = "|" + "---|" * (len(corpo.columns) + 1)
        linhas = [f"| {indice} | " + " | ".join(str(v) for v in linha) + " |" for indice, linha in zip(corpo.index, corpo.to_numpy())]
        return "\n".join([cabecalho, separador, *linhas])

    escolhido = metadata["k"]
    diag = pd.DataFrame(diagnostico).set_index("k")
    diag.index.name = "k"
    resumo = perfil.set_index("nome")[[
        "share_pct", "distance_km", "weight_total_kg", "cubic_weight_kg", "density", "declared_value",
        "quantity_items", "freight_value", "freight_per_kg", "freight_pct_value", "lead_days",
        "late_pct", "review_score", "freight_cv", "freight_share_pct",
    ]]
    resumo.index.name = "cluster"
    categorias = []
    for grupo in perfil.index:
        top = dados.loc[dados["cluster"].eq(grupo), "category"].value_counts(normalize=True).mul(100).round(1).head(4)
        categorias.append(f"- **{perfil.loc[grupo, 'nome']}** — " + ", ".join(f"{nome} {pct}%" for nome, pct in top.items()))
    ufs = []
    for grupo in perfil.index:
        top = dados.loc[dados["cluster"].eq(grupo), "dest_state"].value_counts(normalize=True).mul(100).round(1).head(4)
        ufs.append(f"- **{perfil.loc[grupo, 'nome']}** — " + ", ".join(f"{nome} {pct}%" for nome, pct in top.items()))

    return f"""# Clusterização dos embarques Olist

Gerado em {metadata["generated_at"]} · fonte `{metadata["source"]}` · {metadata["rows"]} embarques
· grão `{metadata["unit_of_analysis"]}` · k = {escolhido} · seed {metadata["seed"]}

## Método

Features (log1p + StandardScaler): {", ".join(f"`{c}`" for c in FEATURES_CLUSTER)}.
O frete **não** entra como feature — é a variável a explicar, não a agrupar; aparece
somente no perfil dos grupos. Cubagem usa divisor {DIVISOR_CUBAGEM:.0f} (cm³ → kg).

## Escolha de k

{tabela(diag, 3)}

Menor inércia sempre favorece k maior; a leitura combina o joelho da inércia com o
mínimo de Davies-Bouldin. Silhueta baixa em termos absolutos é esperada: os dados são
um contínuo log-normal, não bolhas separadas.

## Perfil dos clusters

Medianas, exceto `quantity_items`, `review_score` (médias) e `freight_cv`
(desvio/média do frete dentro do grupo — o global é {dados["freight_value"].std() / dados["freight_value"].mean():.3f}).

{tabela(resumo)}

## Estabilidade

- ARI médio em reajustes com 80% da base ({len(metadata["ari_bootstrap"])} réplicas): **{metadata["ari_bootstrap_mean"]:.3f}** — {", ".join(f"{a:.3f}" for a in metadata["ari_bootstrap"])}
- Silhueta da partição final: {metadata["silhouette"]:.3f} · Davies-Bouldin: {metadata["davies_bouldin"]:.3f}
- PCA: {" · ".join(f"PC{i + 1} {v:.0%}" for i, v in enumerate(metadata["pca_variance"][:3]))}

## Mix por cluster

Top categorias:

{chr(10).join(categorias)}

UF de destino:

{chr(10).join(ufs)}

## Limitações

A segmentação vale como recorte operacional, não como prova de estrutura latente:
silhuetas nessa faixa indicam fronteiras convencionadas dentro de um contínuo. Base
restrita a pedidos `delivered` com CEP geocodificável, peso e cubagem positivos.
"""


def clusterizar(
    caminho: Path, saida: Path, *, k: int | None = 6, k_min: int = 2, k_max: int = 10,
    replicas: int = 3, seed: int = 42,
) -> dict:
    """Segmenta os embarques, escreve relatório/figura/rótulos e devolve o metadata."""
    dados = carregar_embarques(caminho)
    x = preparar_features(dados)
    matriz = StandardScaler().fit_transform(x)
    diagnostico = escolher_k(matriz, k_min=k_min, k_max=k_max, seed=seed)
    if k is None:
        k = min(diagnostico, key=lambda d: d["davies_bouldin"])["k"]

    modelo = KMeans(n_clusters=k, n_init=20, random_state=seed).fit(matriz)
    dados["cluster"] = modelo.labels_
    perfil = _perfil(dados)
    perfil.insert(0, "nome", pd.Series(_nomear(perfil)))
    dados["cluster_nome"] = dados["cluster"].map(perfil["nome"])

    pca = PCA(random_state=seed).fit(matriz)
    coordenadas = pca.transform(matriz)[:, :2]
    indices = _amostra(len(matriz), AMOSTRA_SILHUETA, seed)
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(caminho), "unit_of_analysis": "order_id+seller_id",
        "rows": len(dados), "k": k, "seed": seed, "features": FEATURES_CLUSTER,
        "log1p_features": LOG1P_CLUSTER, "cubage_divisor": DIVISOR_CUBAGEM,
        "silhouette": float(silhouette_score(matriz[indices], modelo.labels_[indices])),
        "davies_bouldin": float(davies_bouldin_score(matriz, modelo.labels_)),
        "inertia": float(modelo.inertia_),
        "pca_variance": [float(v) for v in pca.explained_variance_ratio_],
        "pca_loadings": {c: [float(v) for v in pca.components_[:2, i]] for i, c in enumerate(FEATURES_CLUSTER)},
        "k_diagnostics": diagnostico,
        **_estabilidade(matriz, modelo.labels_, k, replicas=replicas, seed=seed),
        "limitations": "Recorte operacional, não estrutura latente comprovada; base restrita a pedidos entregues com CEP geocodificável.",
    }

    saida.mkdir(parents=True, exist_ok=True)
    colunas = ["order_id", "seller_id", "customer_id", "cluster", "cluster_nome", *FEATURES_CLUSTER[:-1],
               "density", "freight_value", "lead_days", "origin_state", "dest_state", "category"]
    dados[colunas].to_csv(saida / "clusters_embarques.csv", index=False)
    perfil.to_csv(saida / "clusters_perfil.csv")
    _figura(dados, coordenadas, perfil, diagnostico, pca.explained_variance_ratio_, saida / "clusters.png", seed)
    (saida / "clusters.md").write_text(_markdown(perfil, diagnostico, metadata, dados), encoding="utf-8")
    (saida / "clusters_metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    metadata["outputs"] = {nome: str(saida / arquivo) for nome, arquivo in {
        "labels": "clusters_embarques.csv", "profile": "clusters_perfil.csv",
        "figure": "clusters.png", "report": "clusters.md", "metadata": "clusters_metadata.json",
    }.items()}
    metadata["profile"] = json.loads(perfil.round(4).to_json(orient="index"))
    return metadata
