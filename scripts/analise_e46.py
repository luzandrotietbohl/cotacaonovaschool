"""E4.6 - Analises que sustentam a especificacao dos seis blocos.

Roda sobre archive_olist/ (CSV soltos) e usa os artefatos de modelos/olist/atual.
Saida: docs/E4-6_analises.json e docs/E4-6_analises.txt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cotador.ml.features import (CATEGORICAS, FEATURES, NUMERICAS, haversine_km,
                                 mapa_geografico, perfil_geografico)

DADOS = RAIZ / "archive_olist"
SAIDA_JSON = RAIZ / "docs" / "E4-6_analises_parte1.json"
SAIDA_TXT = RAIZ / "docs" / "E4-6_analises_parte1.txt"

R = {}
linhas = []


def secao(titulo):
    diz("")
    diz("=" * 78)
    diz(titulo)
    diz("=" * 78)


def diz(texto=""):
    linhas.append(texto)
    print(texto)


def ler(nome):
    return pd.read_csv(DADOS / (nome + ".csv"), low_memory=False)


clientes = ler("olist_customers_dataset")
geo_bruto = ler("olist_geolocation_dataset")
itens = ler("olist_order_items_dataset")
pedidos_todos = ler("olist_orders_dataset")
produtos = ler("olist_products_dataset")
vendedores = ler("olist_sellers_dataset")
geo = mapa_geografico(geo_bruto)

# ============================================================ BLOCO 3.4
secao("BLOCO 3.4 - CURADORIA: o funil de exclusao, numero antes e depois")

funil = []
n0_itens = len(itens)
funil.append(("0. order_items bruto", n0_itens, float(itens["freight_value"].sum())))

pedidos = pedidos_todos.loc[pedidos_todos["order_status"].eq("delivered"),
                            ["order_id", "customer_id", "order_purchase_timestamp"]]
base = itens.merge(pedidos, on="order_id")
funil.append(("1. so order_status=delivered", len(base), float(base["freight_value"].sum())))

base = base.merge(produtos[["product_id", "product_weight_g"]], on="product_id", how="left")
embarques = base.groupby(["order_id", "seller_id", "customer_id", "order_purchase_timestamp"],
                         as_index=False).agg(
    quantity_items=("order_item_id", "count"), declared_value=("price", "sum"),
    freight_value=("freight_value", "sum"), weight_total_g=("product_weight_g", "sum"))
funil.append(("2. agregado por order_id+seller_id", len(embarques), float(embarques["freight_value"].sum())))

embarques = embarques.merge(clientes[["customer_id", "customer_zip_code_prefix"]], on="customer_id") \
                     .merge(vendedores[["seller_id", "seller_zip_code_prefix"]], on="seller_id")
funil.append(("3. join clientes+vendedores", len(embarques), float(embarques["freight_value"].sum())))

origem = geo.rename(columns={"zip_prefix": "seller_zip_code_prefix", "lat": "origin_lat",
                             "lng": "origin_lng", "city": "origin_city", "state": "origin_state"})
destino = geo.rename(columns={"zip_prefix": "customer_zip_code_prefix", "lat": "dest_lat",
                              "lng": "dest_lng", "city": "dest_city", "state": "dest_state"})
embarques = embarques.merge(origem, on="seller_zip_code_prefix", how="left") \
                     .merge(destino, on="customer_zip_code_prefix", how="left")
embarques["distance_km"] = haversine_km(embarques["origin_lat"], embarques["origin_lng"],
                                        embarques["dest_lat"], embarques["dest_lng"])
embarques["weight_total_kg"] = embarques["weight_total_g"] / 1000.0
embarques["month"] = pd.to_datetime(embarques["order_purchase_timestamp"], errors="coerce").dt.month
embarques["origin_state"] = embarques["origin_state"].astype(str).str.upper()
embarques["dest_state"] = embarques["dest_state"].astype(str).str.upper()
embarques["route"] = embarques["origin_state"] + ">" + embarques["dest_state"]
embarques["same_state"] = np.where(embarques["origin_state"].eq(embarques["dest_state"]),
                                   "same_state", "interstate")
embarques["geo_profile"] = [perfil_geografico(a, b)
                            for a, b in zip(embarques["origin_city"], embarques["dest_city"])]

pre_dropna = embarques.copy()
obrigatorias = [c for c in NUMERICAS] + [c for c in CATEGORICAS] + ["freight_value", "order_purchase_timestamp"]
embarques = embarques.replace([np.inf, -np.inf], np.nan).dropna(subset=obrigatorias)
descartados_geo = pre_dropna.loc[~pre_dropna.index.isin(embarques.index)]
funil.append(("4. dropna geo/peso obrigatorios", len(embarques), float(embarques["freight_value"].sum())))

pre_filtro = embarques.copy()
embarques = embarques.loc[(embarques["freight_value"] >= 0) & (embarques["weight_total_kg"] > 0)
                          & (embarques["quantity_items"] > 0)].copy()
descartados_filtro = pre_filtro.loc[~pre_filtro.index.isin(embarques.index)]
funil.append(("5. freight>=0, peso>0, qtd>0", len(embarques), float(embarques["freight_value"].sum())))

embarques = embarques.sort_values("order_purchase_timestamp").reset_index(drop=True)

diz("etapa".ljust(38) + "linhas".rjust(10) + "% bruto".rjust(11) + "frete R$".rjust(16))
for nome, n, frete in funil:
    diz(nome.ljust(38) + format(n, ",").rjust(10) + format(n / n0_itens * 100, ".1f").rjust(10) + "%"
        + format(frete, ",.2f").rjust(16))
R["funil_curadoria"] = [{"etapa": n, "linhas": int(l), "frete_total": round(f, 2)} for n, l, f in funil]

diz("")
diz("Vies das exclusoes (o que sai nao e igual ao que fica):")
nao_entregues = set(pedidos_todos.loc[~pedidos_todos["order_status"].eq("delivered"), "order_id"])
itens_ne = itens.loc[itens["order_id"].isin(nao_entregues)]
comp = {
    "excluidos_por_status": {
        "linhas": int(len(itens_ne)),
        "frete_mediano": round(float(itens_ne["freight_value"].median()), 2),
        "frete_medio": round(float(itens_ne["freight_value"].mean()), 2),
        "preco_mediano": round(float(itens_ne["price"].median()), 2)},
    "mantidos_delivered": {
        "linhas": int(len(base)),
        "frete_mediano": round(float(base["freight_value"].median()), 2),
        "frete_medio": round(float(base["freight_value"].mean()), 2),
        "preco_mediano": round(float(base["price"].median()), 2)},
    "descartados_geo_peso": {
        "linhas": int(len(descartados_geo)),
        "frete_mediano": round(float(descartados_geo["freight_value"].median()), 2) if len(descartados_geo) else None,
        "sem_peso": int(descartados_geo["weight_total_kg"].isna().sum()),
        "sem_distancia": int(descartados_geo["distance_km"].isna().sum())},
    "descartados_dominio": {
        "linhas": int(len(descartados_filtro)),
        "peso_zero": int((descartados_filtro["weight_total_kg"] <= 0).sum())},
}
for k, v in comp.items():
    diz("  " + k + ": " + json.dumps(v, ensure_ascii=False))
R["vies_exclusoes"] = comp

status_conta = pedidos_todos["order_status"].value_counts()
R["status_pedidos"] = {str(k): int(v) for k, v in status_conta.items()}
diz("")
diz("  status dos pedidos: " + json.dumps(R["status_pedidos"], ensure_ascii=False))

# ============================================================ BLOCO 3.2 grao
secao("BLOCO 3.2 - GRAO E UNIDADE DE ANALISE")
itens_por_pedido = itens.groupby("order_id").size()
vend_por_pedido = itens.groupby("order_id")["seller_id"].nunique()
grao = {
    "linhas_order_items": int(len(itens)),
    "pedidos": int(itens["order_id"].nunique()),
    "embarques_pedido_vendedor": int(itens.groupby(["order_id", "seller_id"]).ngroups),
    "pedidos_multi_vendedor": int((vend_por_pedido > 1).sum()),
    "pct_pedidos_multi_vendedor": round(float((vend_por_pedido > 1).mean() * 100), 2),
    "itens_por_pedido_p50": float(itens_por_pedido.median()),
    "itens_por_pedido_max": int(itens_por_pedido.max()),
    "vendedores": int(vendedores["seller_id"].nunique()),
    "rotas_uf_distintas": int(embarques["route"].nunique()),
    "embarques_finais": int(len(embarques)),
    "janela_inicio": str(pd.to_datetime(pedidos_todos["order_purchase_timestamp"]).min().date()),
    "janela_fim": str(pd.to_datetime(pedidos_todos["order_purchase_timestamp"]).max().date()),
}
for k, v in grao.items():
    diz("  " + k + ": " + str(v))
R["grao"] = grao

# ============================================================ BLOCO 3.2 latencia
secao("BLOCO 3.2 - LATENCIA EM HORAS (nao em adjetivos)")
p = pedidos_todos.copy()
for c in ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
          "order_delivered_customer_date", "order_estimated_delivery_date"]:
    p[c] = pd.to_datetime(p[c], errors="coerce")
lat = {}
for nome, a, b in [("compra->aprovacao", "order_purchase_timestamp", "order_approved_at"),
                   ("aprovacao->coleta", "order_approved_at", "order_delivered_carrier_date"),
                   ("coleta->entrega", "order_delivered_carrier_date", "order_delivered_customer_date"),
                   ("compra->entrega", "order_purchase_timestamp", "order_delivered_customer_date")]:
    d = (p[b] - p[a]).dt.total_seconds() / 3600
    lat[nome] = {"p50_h": round(float(d.median()), 1), "p90_h": round(float(d.quantile(.90)), 1),
                 "max_h": round(float(d.max()), 1), "negativos": int((d < 0).sum()),
                 "ausentes": int(d.isna().sum())}
    diz("  " + nome.ljust(20) + " p50=" + format(lat[nome]["p50_h"], ".1f").rjust(9) + "h  p90="
        + format(lat[nome]["p90_h"], ".1f").rjust(9) + "h  max=" + format(lat[nome]["max_h"], ".1f").rjust(9)
        + "h  negativos=" + str(lat[nome]["negativos"]).rjust(5) + "  ausentes=" + str(lat[nome]["ausentes"]).rjust(6))
R["latencia"] = lat
dias = lat["compra->entrega"]["p50_h"] / 24
diz("")
diz("  O rotulo (freight_value) existe em t0. O FILTRO de treino (order_status=delivered)")
diz("  so existe " + format(dias, ".1f") + " dias depois, na mediana. Todas as " + str(len(FEATURES))
    + " features do modelo estao disponiveis em t0.")
R["disponibilidade_t0"] = {"features_em_t0": FEATURES, "dias_ate_conhecer_status_p50": round(dias, 1)}

# ============================================================ BLOCO 3.2 cobertura
secao("BLOCO 3.2 - COBERTURA GEOGRAFICA")
prefixos_geo = set(geo["zip_prefix"])
cob_cli = clientes["customer_zip_code_prefix"].isin(prefixos_geo)
cob_ven = vendedores["seller_zip_code_prefix"].isin(prefixos_geo)
cobertura = {
    "prefixos_cep_no_geolocation": int(len(prefixos_geo)),
    "linhas_geolocation_brutas": int(len(geo_bruto)),
    "clientes_sem_geo": int((~cob_cli).sum()),
    "pct_clientes_sem_geo": round(float((~cob_cli).mean() * 100), 3),
    "vendedores_sem_geo": int((~cob_ven).sum()),
    "pct_vendedores_sem_geo": round(float((~cob_ven).mean() * 100), 3),
}
por_uf = clientes.assign(ok=cob_cli).groupby("customer_state")["ok"].agg(["size", "mean"])
piores = (1 - por_uf["mean"]).sort_values(ascending=False).head(6)
cobertura["piores_ufs_sem_geo_pct"] = {str(k): round(float(v * 100), 2) for k, v in piores.items()}
for k, v in cobertura.items():
    diz("  " + k + ": " + json.dumps(v, ensure_ascii=False))
R["cobertura"] = cobertura

# ============================================================ BLOCO 3.1 autoria
secao("BLOCO 3.1 - AUTORIA: o frete e uma alegacao comercial, nao uma medicao")
fv = itens["freight_value"]
top = fv.value_counts().head(10)
autoria = {
    "valores_distintos_de_frete": int(fv.nunique()),
    "pct_nos_10_valores_mais_comuns": round(float(top.sum() / len(fv) * 100), 2),
    "top10_valores": {str(k): int(v) for k, v in top.items()},
    "frete_zero": int((fv == 0).sum()),
    "pct_frete_zero": round(float((fv == 0).mean() * 100), 3),
}
e = embarques.copy()
e["faixa_kg"] = pd.cut(e["weight_total_kg"], [0, .5, 1, 2, 5, 10, 30, 1e9],
                       labels=["<0.5", "0.5-1", "1-2", "2-5", "5-10", "10-30", ">30"])
g = e.groupby(["route", "faixa_kg"], observed=True)["freight_value"].agg(["size", "median", "std", "min", "max"])
g = g.loc[g["size"] >= 30]
cv = (g["std"] / g["median"]).replace([np.inf, -np.inf], np.nan).dropna()
autoria["celulas_rota_x_faixa_peso_n_min_30"] = int(len(g))
autoria["cv_mediano_dentro_da_celula"] = round(float(cv.median()), 3)
autoria["cv_p90_dentro_da_celula"] = round(float(cv.quantile(.9)), 3)
maior = g.loc[cv.sort_values(ascending=False).head(3).index]
autoria["exemplos_maior_dispersao"] = [
    {"rota": str(i[0]), "faixa_kg": str(i[1]), "n": int(r["size"]), "mediana": round(float(r["median"]), 2),
     "min": round(float(r["min"]), 2), "max": round(float(r["max"]), 2)} for i, r in maior.iterrows()]
for k, v in autoria.items():
    diz("  " + k + ": " + json.dumps(v, ensure_ascii=False))
R["autoria"] = autoria

embarques.to_pickle(RAIZ / "docs" / "_e46_embarques.pkl")
SAIDA_JSON.write_text(json.dumps(R, indent=2, ensure_ascii=False), encoding="utf-8")
SAIDA_TXT.write_text("\n".join(linhas), encoding="utf-8")
print("")
print("parte 1 salva em " + str(SAIDA_JSON))
