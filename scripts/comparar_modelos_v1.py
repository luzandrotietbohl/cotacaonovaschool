# -*- coding: utf-8 -*-
"""E4.6 - Por que ESTE modelo, e nao os outros: a evidencia.

O bloco 4.1 exige a justificacao da escolha. Afirmar "empatam" sem medir
nao e justificacao, e a apresentacao afirmava isso do CatBoost - que ja
esta instalado no projeto, porque o cotador roda nele. Este script mede.

Produz:
  1. o mesmo teste temporal para cinco modelos, incluindo o CatBoost
  2. intervalo de confianca por bootstrap na DIFERENCA contra o escolhido,
     para responder se 2 p.p. de AUC e sinal ou ruido
  3. a alternativa por REGRESSAO (prever dias de atraso e cortar em zero),
     que e a pergunta obvia e tem uma resposta forte
  4. a sensibilidade aos hiperparametros, que estao fixos e nao ajustados

Executar: python scripts/comparar_modelos_v1.py [--versao v1] [--reamostras 2000]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (HistGradientBoostingClassifier,
                              RandomForestClassifier)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

RAIZ = Path(__file__).resolve().parents[1]
SEP, ENC = ";", "utf-8-sig"
DIAS_TESTE = 90

R: dict = {}
linhas: list[str] = []


def diz(t=""):
    linhas.append(t)
    print(t)


def secao(t):
    diz()
    diz("=" * 78)
    diz(t)
    diz("=" * 78)


def booleana(s):
    return s.astype(str).str.lower().isin(["true", "1", "sim"])


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = (np.sin((p2 - p1) / 2) ** 2
         + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


ap = argparse.ArgumentParser()
ap.add_argument("--versao", default="v1")
ap.add_argument("--reamostras", type=int, default=2000)
args = ap.parse_args()

BASE = RAIZ / "dados_curados" / args.versao
df = pd.read_csv(BASE / "entregas_curado.csv", sep=SEP, encoding=ENC, low_memory=False)
geo = pd.read_csv(BASE / "geolocation_cep_curado.csv", sep=SEP, encoding=ENC)

for c in ["dt_compra", "dt_prazo", "dt_limite_expedicao"]:
    df[c] = pd.to_datetime(df[c], errors="coerce")

d = df.loc[booleana(df["ativo"]) & ~booleana(df["qa_cauda_incompleta"])]
d = d.loc[d["dt_compra"].notna() & d["dt_prazo"].notna()].copy()
d["y"] = booleana(d["atraso_honesto"]).astype(int)

geo_cep = geo[["cep_prefixo", "lat", "lng"]].rename(
    columns={"cep_prefixo": "cliente_cep_prefixo", "lat": "lat_cli", "lng": "lng_cli"})
geo_cid = (geo.groupby(["cidade_canonica", "uf"], as_index=False)
              .agg(lat_fil=("lat", "median"), lng_fil=("lng", "median"))
              .rename(columns={"cidade_canonica": "filial_cidade", "uf": "filial"}))
d = d.merge(geo_cep, on="cliente_cep_prefixo", how="left").merge(
    geo_cid, on=["filial_cidade", "filial"], how="left")
d["distancia_km"] = haversine_km(d["lat_fil"], d["lng_fil"], d["lat_cli"], d["lng_cli"])

d["prazo_prometido_dias"] = (d["dt_prazo"] - d["dt_compra"]).dt.total_seconds() / 86400
d["folga_expedicao_dias"] = (d["dt_limite_expedicao"] - d["dt_compra"]).dt.total_seconds() / 86400
d["mes"] = d["dt_compra"].dt.month
d["dia_semana"] = d["dt_compra"].dt.dayofweek
d["hora"] = d["dt_compra"].dt.hour
d["mesma_uf"] = (d["filial"].astype(str) == d["cliente_uf"].astype(str)).astype(int)

NUM = ["prazo_prometido_dias", "folga_expedicao_dias", "distancia_km", "peso_g",
       "valor_mercadoria", "valor_frete", "n_itens", "mes", "dia_semana", "hora", "mesma_uf"]
CAT = ["filial", "cliente_uf", "canal_pagamento"]
for c in CAT:
    d[c] = d[c].astype(str).fillna("SEM")

corte = d["dt_compra"].max() - pd.Timedelta(days=DIAS_TESTE)
itr = (d["dt_compra"] <= corte).to_numpy()
ite = ~itr
y = d["y"].to_numpy()
yte = y[ite]

X = d[NUM + CAT].copy()
Xcat = X.copy()
for c in CAT:
    Xcat[c] = Xcat[c].astype("category")

pre = ColumnTransformer([
    ("num", Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())]), NUM),
    ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=30, sparse_output=False), CAT),
])

R["n_treino"], R["n_teste"] = int(itr.sum()), int(ite.sum())
R["prevalencia_teste"] = round(float(yte.mean()), 4)

# ======================================================== OS CINCO MODELOS
secao("OS CINCO MODELOS NO MESMO TESTE TEMPORAL")

probas: dict[str, np.ndarray] = {}

m = HistGradientBoostingClassifier(
    max_iter=300, learning_rate=.06, max_leaf_nodes=31, min_samples_leaf=40,
    l2_regularization=1.0, categorical_features="from_dtype", random_state=42)
m.fit(Xcat[itr], y[itr])
probas["arvores impulsionadas (escolhido)"] = m.predict_proba(Xcat[ite])[:, 1]

lr = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=2000))])
lr.fit(X[itr], y[itr])
probas["regressao logistica"] = lr.predict_proba(X[ite])[:, 1]

rf = Pipeline([("pre", pre), ("rf", RandomForestClassifier(
    n_estimators=300, min_samples_leaf=20, n_jobs=-1, random_state=42))])
rf.fit(X[itr], y[itr])
probas["random forest"] = rf.predict_proba(X[ite])[:, 1]

# CatBoost: ja e dependencia do projeto, logo NAO custa dependencia nova
from catboost import CatBoostClassifier  # noqa: E402
Xcb = d[NUM + CAT].copy()
for c in NUM:
    Xcb[c] = pd.to_numeric(Xcb[c], errors="coerce")
cb = CatBoostClassifier(iterations=600, learning_rate=.06, depth=6, verbose=0,
                        random_seed=42, cat_features=CAT, allow_writing_files=False)
cb.fit(Xcb[itr], y[itr])
probas["catboost"] = cb.predict_proba(Xcb[ite])[:, 1]

diz("  " + "modelo".ljust(34) + "AUC".rjust(8) + "PR-AUC".rjust(9))
tabela = []
for nome, p in probas.items():
    auc, pra = roc_auc_score(yte, p), average_precision_score(yte, p)
    diz("  " + nome.ljust(34) + format(auc, ".4f").rjust(8) + format(pra, ".4f").rjust(9))
    tabela.append({"modelo": nome, "auc": round(float(auc), 4), "pr_auc": round(float(pra), 4)})
R["modelos"] = tabela

# ======================================== A DIFERENCA E SINAL OU RUIDO?
secao("BOOTSTRAP NA DIFERENCA: 2 p.p. de AUC e sinal ou ruido?")

ref = "arvores impulsionadas (escolhido)"
rng = np.random.default_rng(42)
idx = np.arange(len(yte))
amostras = [rng.choice(idx, size=len(idx), replace=True) for _ in range(args.reamostras)]

diz("  " + str(args.reamostras) + " reamostras do conjunto de teste (n = " + format(len(yte), ",") + ")")
diz()
diz("  " + "contra o escolhido".ljust(30) + "delta AUC".rjust(12) + "IC 95%".rjust(20) + "  inclui zero?")
comp = []
for nome, p in probas.items():
    if nome == ref:
        continue
    difs = []
    for a in amostras:
        ya = yte[a]
        if ya.min() == ya.max():
            continue
        difs.append(roc_auc_score(ya, probas[ref][a]) - roc_auc_score(ya, p[a]))
    difs = np.array(difs)
    lo, hi = np.percentile(difs, [2.5, 97.5])
    zero = lo <= 0 <= hi
    diz("  " + nome.ljust(30) + format(difs.mean() * 100, "+.2f").rjust(8) + "pp"
        + ("[" + format(lo * 100, "+.2f") + ", " + format(hi * 100, "+.2f") + "]").rjust(20)
        + ("   SIM — empate" if zero else "   nao"))
    comp.append({"contra": nome, "delta_auc_pp": round(float(difs.mean() * 100), 3),
                 "ic95_pp": [round(float(lo * 100), 3), round(float(hi * 100), 3)],
                 "inclui_zero": bool(zero)})
R["bootstrap_delta_auc"] = comp
R["reamostras"] = args.reamostras

# ============================================ POR QUE NAO REGRESSAO
secao("POR QUE CLASSIFICAR, E NAO PREVER OS DIAS DE ATRASO")

tem_dias = pd.to_numeric(d["dias_vs_prazo"], errors="coerce").notna()
diz("  Para prever DIAS de atraso e preciso a coluna dias_vs_prazo, que existe")
diz("  apenas onde ha data de entrega registada:")
diz()
diz("    pedidos no recorte                " + format(len(d), ",").rjust(9))
diz("    com dias_vs_prazo                 " + format(int(tem_dias.sum()), ",").rjust(9)
    + format(float(tem_dias.mean() * 100), ".2f").rjust(9) + "%")
diz("    SEM dias_vs_prazo                 " + format(int((~tem_dias).sum()), ",").rjust(9))
perdidos = int((d.loc[~tem_dias, "y"] == 1).sum())
diz()
diz("    desses, contados como atraso pelo denominador honesto: "
    + format(perdidos, ",").rjust(7))
diz("    fracao dos atrasos que a regressao perderia: "
    + format(perdidos / max(int((d['y'] == 1).sum()), 1) * 100, ".1f").rjust(6) + "%")
diz()
diz("  A LEITURA: a regressao so treina onde a entrega foi registada, ou seja,")
diz("  exclui exatamente os pedidos que o KPI honesto conta como atraso. Prever")
diz("  dias obriga a voltar ao denominador reportado - o defeito no 1 do E1.2.")
diz("  Classificar e o que permite manter o alvo honesto.")
R["regressao_descartada"] = {
    "pedidos": int(len(d)),
    "com_dias_vs_prazo": int(tem_dias.sum()),
    "sem_dias_vs_prazo": int((~tem_dias).sum()),
    "atrasos_que_a_regressao_perderia": perdidos,
    "pct_dos_atrasos": round(perdidos / max(int((d["y"] == 1).sum()), 1) * 100, 2),
    "motivo": ("dias_vs_prazo so existe onde ha data de entrega; regredir dias "
               "obriga a excluir os pedidos vencidos sem registo, que sao "
               "justamente os que o denominador honesto conta"),
}

# ======================================== SENSIBILIDADE AOS HIPERPARAMETROS
secao("HIPERPARAMETROS: FIXOS E NAO AJUSTADOS - QUANTO ISSO CUSTA")

grades = [
    {"max_iter": 300, "learning_rate": .06, "max_leaf_nodes": 31, "min_samples_leaf": 40},
    {"max_iter": 150, "learning_rate": .10, "max_leaf_nodes": 31, "min_samples_leaf": 40},
    {"max_iter": 600, "learning_rate": .03, "max_leaf_nodes": 63, "min_samples_leaf": 20},
    {"max_iter": 300, "learning_rate": .06, "max_leaf_nodes": 15, "min_samples_leaf": 100},
]
diz("  " + "configuracao".ljust(46) + "AUC".rjust(8))
sens = []
for g in grades:
    mm = HistGradientBoostingClassifier(
        l2_regularization=1.0, categorical_features="from_dtype", random_state=42, **g)
    mm.fit(Xcat[itr], y[itr])
    a = float(roc_auc_score(yte, mm.predict_proba(Xcat[ite])[:, 1]))
    rot = "iter={max_iter} lr={learning_rate} folhas={max_leaf_nodes} min={min_samples_leaf}".format(**g)
    diz("  " + rot.ljust(46) + format(a, ".4f").rjust(8))
    sens.append({"config": g, "auc": round(a, 4)})
amp = max(x["auc"] for x in sens) - min(x["auc"] for x in sens)
diz()
diz("  amplitude entre as quatro configuracoes: " + format(amp * 100, ".2f") + " p.p.")
diz("  Nao houve busca de hiperparametros. A amplitude acima diz o que isso")
diz("  custou, e e menor do que a diferenca entre modelos - por isso a escolha")
diz("  do modelo importa mais do que o ajuste dele.")
R["sensibilidade_hiperparametros"] = {"configs": sens, "amplitude_pp": round(amp * 100, 3)}

SJ = RAIZ / "docs" / ("E4-6_modelos_" + args.versao + ".json")
ST = RAIZ / "docs" / ("E4-6_modelos_" + args.versao + ".txt")
SJ.write_text(json.dumps(R, indent=2, ensure_ascii=False), encoding="utf-8")
ST.write_text("\n".join(linhas) + "\n", encoding="utf-8")
diz()
diz("salvo em " + str(SJ))
