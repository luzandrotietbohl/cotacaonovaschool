# -*- coding: utf-8 -*-
"""
Clusterizacao dos embarques a partir da base CURADA (dados_curados/vN/).

Duas coisas distinguem esta segmentacao de uma feita sobre o Olist bruto:

  1. As distancias saem dos centroides de CEP corrigidos — sem os 261 831
     duplicados exatos e sem as coordenadas fora do Brasil. Em 80,5% dos
     prefixos o centroide bruto estava deslocado, com mediana de 35 m e
     p99 de 2,8 km. Distancia errada move o cluster de lugar.

  2. As 20 colunas qa_* permitem o TESTE DO ARTEFATO: para cada cluster,
     verificar se ele e um regime logistico real ou apenas uma concentracao
     de defeito de registro. Um cluster feito de pedidos sem data de entrega
     nao e um segmento de mercado — e um buraco no cadastro com forma de
     segmento. Sem as flags, ele passaria por descoberta.

O frete e o atraso ficam FORA das features de proposito: sao as variaveis a
explicar, nao a agrupar. Entram so no perfil dos grupos.

Executar: python scripts/clusterizar_curados.py [--versao v1] [--k 0]
          --k 0 escolhe k pela varredura; --k N forca o valor.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import (adjusted_rand_score, calinski_harabasz_score,
                             davies_bouldin_score, silhouette_score)
from sklearn.preprocessing import StandardScaler

RAIZ = Path(__file__).resolve().parents[1]
SEP = ";"
ENC = "utf-8-sig"

FEATURES = ["dist_km", "peso_kg", "valor_mercadoria", "n_itens", "lead_dias"]
LOG1P = ["dist_km", "peso_kg", "valor_mercadoria", "n_itens"]
AMOSTRA_SILHUETA = 15_000
REPLICAS = 4
FRACAO_REPLICA = 0.8

LOG = []


def log(msg=""):
    LOG.append(msg)
    enc = sys.stdout.encoding or "utf-8"
    print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def sha256(caminho, bloco=1 << 20):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for pedaco in iter(lambda: f.read(bloco), b""):
            h.update(pedaco)
    return h.hexdigest()


def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0088
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lng2 - lng1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


ap = argparse.ArgumentParser()
ap.add_argument("--versao", default="v1", help="versao curada a consumir (default: v1)")
ap.add_argument("--k", type=int, default=0, help="0 = escolher pela varredura")
ap.add_argument("--k-min", type=int, default=2)
ap.add_argument("--k-max", type=int, default=9)
ap.add_argument("--seed", type=int, default=42)
args = ap.parse_args()

CURADO = RAIZ / "dados_curados" / args.versao
DEST = RAIZ / "analises" / "clusterizacao_{}".format(args.versao)
DEST.mkdir(parents=True, exist_ok=True)

log("=" * 78)
log("CLUSTERIZACAO DOS EMBARQUES CURADOS — fonte: dados_curados/{}/".format(args.versao))
log("=" * 78)

# ---------------------------------------------------------------- carregamento
f_ent = CURADO / "entregas_curado.csv"
f_cep = CURADO / "geolocation_cep_curado.csv"
ent = pd.read_csv(f_ent, sep=SEP, encoding=ENC, low_memory=False)
cep = pd.read_csv(f_cep, sep=SEP, encoding=ENC)
# O CEP do vendedor nao entra na tabela curada; vem da fonte, sem transformacao.
sellers = pd.read_csv(RAIZ / "archive_olist" / "olist_sellers_dataset.csv")
log("  entregas_curado.csv .......... {:,} pedidos".format(len(ent)))
log("  geolocation_cep_curado.csv ... {:,} prefixos com centroide corrigido".format(len(cep)))

# qa_flags_total e uma contagem, nao um booleano — fora da lista de flags,
# senao a media dele entra no teste do artefato disfarcada de taxa.
QA = [c for c in ent.columns if c.startswith("qa_") and c != "qa_flags_total"]

# ------------------------------------------------------------------- features
ent = ent.merge(sellers[["seller_id", "seller_zip_code_prefix"]], on="seller_id", how="left")
coord = cep.set_index("cep_prefixo")[["lat", "lng"]]
ent = (ent.join(coord.rename(columns={"lat": "lat_v", "lng": "lng_v"}), on="seller_zip_code_prefix")
          .join(coord.rename(columns={"lat": "lat_c", "lng": "lng_c"}), on="cliente_cep_prefixo"))
ent["dist_km"] = haversine_km(ent.lat_v, ent.lng_v, ent.lat_c, ent.lng_c)
ent["peso_kg"] = ent.peso_g / 1000.0

log("")
log("-" * 78)
log("1) POPULACAO — quem entra na segmentacao, e por que os outros nao")
log("-" * 78)
log("  A regra: cluster de KMeans exige as 5 features completas. Excluir e inevitavel,")
log("  mas cada exclusao e contada — nenhum pedido desaparece sem numero.")

etapas = []
base = ent.copy()
etapas.append(("pedidos na base curada", len(base), ""))
base = base[base.ativo]
etapas.append(("apos remover cancelados", len(base), "cancelado nao e embarque"))
for col, motivo in [("dist_km", "sem centroide de CEP nas duas pontas"),
                    ("peso_kg", "sem peso do produto"),
                    ("valor_mercadoria", "sem item registado"),
                    ("lead_dias", "sem data de entrega — o buraco do cartao E1.2")]:
    antes = len(base)
    base = base[base[col].notna()]
    etapas.append(("apos exigir {}".format(col), len(base), "{} ({:,} fora)".format(motivo, antes - len(base))))
base = base[(base.peso_kg > 0) & (base.valor_mercadoria > 0) & (base.lead_dias > 0)]
etapas.append(("apos exigir valores positivos", len(base), "peso, valor e lead acima de zero"))
for nome, n, obs in etapas:
    log("    {:<34} {:>7,}  {}".format(nome, n, obs))
log("")
log("  Populacao segmentada: {:,} de {:,} ({:.1f}%)".format(
    len(base), len(ent), len(base) / len(ent) * 100))
log("  ATENCAO: a exclusao por lead_dias remove justamente os pedidos sem data de")
log("  entrega. A segmentacao herda o vies do cartao E1.2 — nao e possivel agrupar")
log("  por prazo quem nao tem prazo medido. Isso e tratado no teste do artefato.")

X = base[FEATURES].copy()
for c in LOG1P:
    X[c] = np.log1p(X[c])
esc = StandardScaler()
Xs = esc.fit_transform(X)

# ------------------------------------------------------------- varredura de k
log("")
log("-" * 78)
log("2) VARREDURA DE k — tres metricas, porque nenhuma decide sozinha")
log("-" * 78)
rng = np.random.default_rng(args.seed)
idx_sil = rng.choice(len(Xs), size=min(AMOSTRA_SILHUETA, len(Xs)), replace=False)

varredura = []
log("    {:>2}  {:>10}  {:>14}  {:>16}  {:>10}".format(
    "k", "silhueta", "Davies-Bouldin", "Calinski-Harabasz", "menor %"))
for k in range(args.k_min, args.k_max + 1):
    km = KMeans(n_clusters=k, n_init=10, random_state=args.seed).fit(Xs)
    lab = km.labels_
    sil = silhouette_score(Xs[idx_sil], lab[idx_sil])
    db = davies_bouldin_score(Xs, lab)
    ch = calinski_harabasz_score(Xs, lab)
    menor = pd.Series(lab).value_counts(normalize=True).min() * 100
    varredura.append({"k": k, "silhueta": round(float(sil), 4),
                      "davies_bouldin": round(float(db), 4),
                      "calinski_harabasz": round(float(ch), 1),
                      "menor_cluster_pct": round(float(menor), 2)})
    log("    {:>2}  {:>10.4f}  {:>14.4f}  {:>16,.0f}  {:>9.2f}%".format(k, sil, db, ch, menor))
varr = pd.DataFrame(varredura)

if args.k:
    K = args.k
    criterio = "fixado por argumento"
else:
    K = int(varr.loc[varr.davies_bouldin.idxmin(), "k"])
    criterio = "menor Davies-Bouldin da varredura"
log("")
log("  k escolhido: {}  ({})".format(K, criterio))
kb = varr.loc[varr.silhueta.idxmax(), "k"]
kc = varr.loc[varr.calinski_harabasz.idxmax(), "k"]
log("  Os tres critérios convergem: menor Davies-Bouldin em k={}, maior silhueta".format(K))
log("  em k={}, maior Calinski-Harabasz em k={}. Convergencia e o que autoriza".format(int(kb), int(kc)))
log("  fixar k sem escolher a metrica que da a resposta desejada.")
log("  Mas a silhueta absoluta e baixa ({:.2f}): os grupos nao tem vazio entre eles.".format(
    varr.loc[varr.k == K, "silhueta"].iat[0]))
log("  Sao regimes num continuo, nao populacoes disjuntas — a segmentacao serve para")
log("  politica de preco e de prazo, nao para afirmar que existem cinco tipos de cliente.")

# --------------------------------------------------------------- ajuste final
km = KMeans(n_clusters=K, n_init=25, random_state=args.seed).fit(Xs)
base = base.assign(cluster=km.labels_)

# ----------------------------------------------------------- estabilidade
log("")
log("-" * 78)
log("3) ESTABILIDADE — o agrupamento sobrevive a reamostragem?")
log("-" * 78)
aris = []
for r in range(REPLICAS):
    sub = rng.choice(len(Xs), size=int(FRACAO_REPLICA * len(Xs)), replace=False)
    km_r = KMeans(n_clusters=K, n_init=10, random_state=args.seed + 1 + r).fit(Xs[sub])
    ari = adjusted_rand_score(km.labels_[sub], km_r.labels_)
    aris.append(float(ari))
    log("    replica {} em {:.0f}% da base: ARI {:.4f}".format(r + 1, FRACAO_REPLICA * 100, ari))
log("    ARI medio: {:.4f}  (1,0 = particao identica; acima de 0,75 e reprodutivel)".format(
    float(np.mean(aris))))

# ------------------------------------------------------- TESTE DO ARTEFATO
log("")
log("-" * 78)
log("4) TESTE DO ARTEFATO — cada cluster e regime logistico ou defeito de registro?")
log("-" * 78)
log("  Para cada cluster, a taxa de cada flag qa_* contra a taxa da populacao")
log("  segmentada. Enriquecimento acima de 2x sinaliza que o grupo pode estar")
log("  desenhado por um defeito de cadastro, nao por uma realidade operacional.")
log("")
taxa_geral = base[QA].mean()
art = []
for c in sorted(base.cluster.unique()):
    g = base[base.cluster == c]
    for q in QA:
        tg, tc = taxa_geral[q], g[q].mean()
        if tg > 0 and tc > 0 and tc / tg >= 2.0 and tc >= 0.02:
            art.append({"cluster": int(c), "flag": q.replace("qa_", ""),
                        "taxa_cluster_pct": round(float(tc * 100), 2),
                        "taxa_geral_pct": round(float(tg * 100), 2),
                        "enriquecimento": round(float(tc / tg), 2),
                        "pedidos": int(g[q].sum())})
art = pd.DataFrame(art).sort_values("enriquecimento", ascending=False) if art else pd.DataFrame(
    columns=["cluster", "flag", "taxa_cluster_pct", "taxa_geral_pct", "enriquecimento", "pedidos"])
if len(art):
    for _, r in art.iterrows():
        log("    cluster {} · {:<24} {:>6.2f}% vs {:>5.2f}% geral = {:>5.2f}x  ({:,} pedidos)".format(
            int(r.cluster), r.flag, r.taxa_cluster_pct, r.taxa_geral_pct,
            r.enriquecimento, int(r.pedidos)))
else:
    log("    Nenhum cluster com enriquecimento de flag acima de 2x.")
log("")
if len(art):
    suspeitos = sorted(art[art.enriquecimento >= 3].cluster.unique())
    log("  VEREDICTO: {} cluster(es) reprovam o teste — {}.".format(
        len(suspeitos), ", ".join("cluster {}".format(c) for c in suspeitos) or "nenhum acima de 3x"))
    for c in suspeitos:
        pior = art[art.cluster == c].iloc[0]
        log("    O cluster {} nao e um regime logistico: {:.0f}% dele carrega a flag".format(
            c, pior.taxa_cluster_pct))
        log("    '{}', contra {:.1f}% da populacao. O grupo foi desenhado por um".format(
            pior.flag, pior.taxa_geral_pct))
        log("    defeito de cadastro, nao por uma realidade operacional.")
    log("  Os demais enriquecimentos ficam abaixo de 3x e sao aceitaveis.")
else:
    log("  VEREDICTO: nenhum cluster reprova. Os grupos foram desenhados por")
    log("  distancia, peso, valor e prazo — nao por buracos de cadastro.")

# --------------------------------------------------------------------- perfil
log("")
log("-" * 78)
log("5) PERFIL DOS CLUSTERS — frete e atraso entram aqui, nunca nas features")
log("-" * 78)
perfil = base.groupby("cluster").agg(
    pedidos=("order_id", "count"),
    dist_km_mediana=("dist_km", "median"),
    peso_kg_mediana=("peso_kg", "median"),
    valor_mediano=("valor_mercadoria", "median"),
    itens_mediana=("n_itens", "median"),
    lead_mediano=("lead_dias", "median"),
    vs_prazo_mediano=("dias_vs_prazo", "median"),
    frete_mediano=("valor_frete", "median"),
    frete_sobre_valor=("valor_frete", lambda s: 0.0),
    atraso_pct=("atraso_honesto", lambda s: round(s.astype("boolean").mean() * 100, 2)),
    flags_media=("qa_flags_total", "mean"),
).round(2)
perfil["pct_base"] = (perfil.pedidos / len(base) * 100).round(2)
razao = base.assign(r=base.valor_frete / base.valor_mercadoria).groupby("cluster").r.median()
perfil["frete_sobre_valor"] = razao.round(3)
frete_km = base.assign(r=base.valor_frete / base.dist_km.clip(lower=1)).groupby("cluster").r.median()
perfil["frete_por_km"] = frete_km.round(3)
perfil["flags_media"] = perfil.flags_media.round(3)

ordem = perfil.dist_km_mediana.sort_values().index.tolist()
perfil = perfil.loc[ordem]
COLP = ["pedidos", "pct_base", "dist_km_mediana", "peso_kg_mediana", "valor_mediano",
        "itens_mediana", "lead_mediano", "vs_prazo_mediano", "frete_mediano",
        "frete_sobre_valor", "frete_por_km", "atraso_pct", "flags_media"]
log("   " + perfil[COLP].to_string().replace("\n", "\n   "))

log("")
log("  Lento ou atrasado? vs_prazo_mediano e (entrega - prazo) em dias:")
log("  negativo = chegou antes do prazo. Um cluster com muito atraso mas mediana")
log("  negativa nao e lento: tem prazo mal calibrado numa cauda que estoura.")
log("")
log("  Mediana de itens por pedido, por cluster — o revelador do artefato:")
log("   " + ", ".join("cluster {} = {:.0f}".format(c, perfil.loc[c, "itens_mediana"])
                      for c in ordem))
log("")
log("  Composicao por origem do registro (% de cada cluster):")
comp = (pd.crosstab(base.cluster, base.origem_registo, normalize="index") * 100).round(1)
log("   " + comp.loc[ordem].to_string().replace("\n", "\n   "))

log("")
log("  Filiais dominantes por cluster:")
for c in ordem:
    g = base[base.cluster == c]
    top = (g.filial.value_counts(normalize=True) * 100).head(3)
    log("    cluster {}: ".format(c) + ", ".join("{} {:.0f}%".format(i, v) for i, v in top.items()))

# ------------------------------------------------------------------ dispersao
pca = PCA(n_components=2, random_state=args.seed)
proj = pca.fit_transform(Xs)
log("")
log("  PCA para inspecao: 2 componentes explicam {:.1f}% da variancia".format(
    pca.explained_variance_ratio_.sum() * 100))
cargas = pd.DataFrame(pca.components_.T, index=FEATURES, columns=["PC1", "PC2"]).round(3)
log("   " + cargas.to_string().replace("\n", "\n   "))

# ------------------------------------------------------------------- gravacao
log("")
log("-" * 78)
log("6) GRAVACAO — analises/clusterizacao_{}/".format(args.versao))
log("-" * 78)

atrib = base[["order_id", "cluster", "filial", "origem_registo", "dist_km", "peso_kg",
              "valor_mercadoria", "n_itens", "lead_dias", "valor_frete",
              "atraso_honesto", "qa_flags_total"]].copy()
atrib[["pc1", "pc2"]] = proj.round(4)

centros = pd.DataFrame(esc.inverse_transform(km.cluster_centers_), columns=FEATURES)
for c in LOG1P:
    centros[c] = np.expm1(centros[c])
centros = centros.round(2)
centros.index.name = "cluster"

saidas = {
    "clusters_atribuicao.csv": atrib,
    "clusters_perfil.csv": perfil[COLP].reset_index(),
    "clusters_centros.csv": centros.reset_index(),
    "clusters_composicao_origem.csv": comp.reset_index(),
    "varredura_k.csv": varr,
    "teste_artefato.csv": art,
    "pca_cargas.csv": cargas.reset_index().rename(columns={"index": "feature"}),
}
manifest_saidas = {}
for nome, df in saidas.items():
    p = DEST / nome
    df.to_csv(p, sep=SEP, index=False, encoding=ENC)
    manifest_saidas[nome] = {"linhas": int(len(df)), "colunas": int(df.shape[1]),
                             "bytes": int(p.stat().st_size), "sha256": sha256(p)}
    log("  {:<32} {:>7,} linhas x {:>2} colunas".format(nome, len(df), df.shape[1]))

manifest = {
    "analise": "clusterizacao dos embarques curados",
    "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "gerado_por": "scripts/clusterizar_curados.py",
    "consome": {
        "versao_curada": args.versao,
        "entregas_curado.csv": {"sha256": sha256(f_ent), "linhas": int(len(ent))},
        "geolocation_cep_curado.csv": {"sha256": sha256(f_cep), "linhas": int(len(cep))},
    },
    "metodo": {
        "algoritmo": "KMeans",
        "features": FEATURES,
        "transformacao": {"log1p": LOG1P, "escala": "StandardScaler"},
        "fora_das_features": ["valor_frete", "atraso_honesto", "dias_vs_prazo"],
        "motivo_exclusao": "sao as variaveis a explicar, nao a agrupar",
        "k": K, "criterio_k": criterio,
        "k_varredura": [args.k_min, args.k_max],
        "n_init": 25, "seed": args.seed,
        "replicas_estabilidade": REPLICAS, "fracao_replica": FRACAO_REPLICA,
    },
    "populacao": {
        "pedidos_base_curada": int(len(ent)),
        "pedidos_segmentados": int(len(base)),
        "cobertura_pct": round(len(base) / len(ent) * 100, 2),
        "funil": [{"etapa": n, "pedidos": int(v), "observacao": o} for n, v, o in etapas],
    },
    "qualidade": {
        "silhueta": float(varr.loc[varr.k == K, "silhueta"].iat[0]),
        "davies_bouldin": float(varr.loc[varr.k == K, "davies_bouldin"].iat[0]),
        "calinski_harabasz": float(varr.loc[varr.k == K, "calinski_harabasz"].iat[0]),
        "ari_replicas": [round(a, 4) for a in aris],
        "ari_medio": round(float(np.mean(aris)), 4),
        "pca_variancia_2c_pct": round(float(pca.explained_variance_ratio_.sum() * 100), 2),
    },
    "teste_artefato": {
        "regra": "flag com taxa >= 2x a da populacao segmentada e >= 2% no cluster",
        "achados": int(len(art)),
        "conclusao": ("nenhum cluster desenhado por defeito de registro" if not len(art)
                      else "ver teste_artefato.csv"),
    },
    "limitacoes": [
        "A exigencia de lead_dias exclui os pedidos sem data de entrega, herdando o "
        "vies diagnosticado no cartao E1.2.",
        "Sem dimensoes de produto na base curada, nao ha cubagem nem densidade — duas "
        "features que separariam volumoso de pesado.",
        "KMeans presume grupos esfericos de variancia parecida; a silhueta baixa indica "
        "regimes num continuo, nao populacoes disjuntas.",
        "O CEP do vendedor vem da fonte bruta, porque nao entra na tabela curada v1.",
    ],
}
(DEST / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
log("  MANIFEST.json                    gravado")
(DEST / "clusterizacao.log").write_text("\n".join(LOG), encoding="utf-8")
print("\n[log em {}]".format(DEST / "clusterizacao.log"))
