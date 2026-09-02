# -*- coding: utf-8 -*-
"""E4.6 - Prever que entregas vao atrasar. Analise sobre dados_curados/vN/.

O sistema especificado no E4.6 preve, no momento da compra, se uma entrega vai
furar o prazo prometido. Este script produz os numeros dos blocos 2, 3.2, 4.1,
4.2, 5 e 6: linha de base, custo do erro, fuga de dados, deriva e o efeito
distributivo por regiao.

PRINCIPIOS
  1. So features disponiveis em t0 (instante da compra). Nenhuma coluna que
     so existe depois da entrega entra no modelo - e o teste de fuga mostra o
     que aconteceria se entrasse.
  2. Nenhuma coluna qa_* e feature. Varias sao tautologicas com o alvo
     (qa_sem_dt_entrega implica atraso_honesto) e usa-las seria fuga.
  3. Split TEMPORAL. O split aleatorio aparece so como contraprova.
  4. A cauda incompleta sai do teste: o alvo ainda estava a chegar.
  5. O custo de cada tipo de erro e um PARAMETRO DECLARADO, nao um dado.

Executar: python scripts/analise_atraso_v1.py [--versao v1]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

RAIZ = Path(__file__).resolve().parents[1]
SEP = ";"
ENC = "utf-8-sig"

# --- parametros declarados (bloco 4.1) --------------------------------------
# Nao estao nos dados. Sao uma decisao de gestao, e e por isso que aparecem
# aqui em cima com nome e valor, e nao escondidos numa funcao de perda.
CUSTO_FN = 45.0   # atraso que ninguem avisou: contato reativo + risco de churn
CUSTO_FP = 8.0    # alarme falso: aviso proativo desnecessario
DIAS_TESTE = 90   # janela final reservada para teste temporal

R: dict = {}
linhas: list[str] = []


def diz(texto=""):
    linhas.append(texto)
    print(texto)


def secao(titulo):
    diz()
    diz("=" * 78)
    diz(titulo)
    diz("=" * 78)


def booleana(s):
    return s.astype(str).str.lower().isin(["true", "1", "sim"])


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


ap = argparse.ArgumentParser()
ap.add_argument("--versao", default="v1")
args = ap.parse_args()

BASE = RAIZ / "dados_curados" / args.versao
man = json.loads((BASE / "MANIFEST.json").read_text(encoding="utf-8"))
df = pd.read_csv(BASE / "entregas_curado.csv", sep=SEP, encoding=ENC, low_memory=False)
geo = pd.read_csv(BASE / "geolocation_cep_curado.csv", sep=SEP, encoding=ENC)

R["versao"] = args.versao
R["gerado_em_curadoria"] = man["gerado_em"]
R["parametros_declarados"] = {"custo_fn_reais": CUSTO_FN, "custo_fp_reais": CUSTO_FP,
                              "dias_teste": DIAS_TESTE}

for c in ["dt_compra", "dt_prazo", "dt_limite_expedicao", "dt_entrega"]:
    df[c] = pd.to_datetime(df[c], errors="coerce")

# ============================================================ BLOCO 2 / 3.2
secao("RECORTE - de que universo o sistema decide")

df["b_ativo"] = booleana(df["ativo"])
df["b_cauda"] = booleana(df["qa_cauda_incompleta"])
df["y"] = booleana(df["atraso_honesto"]).astype(int)

recortes = [("todos os pedidos", len(df))]
d = df.loc[df["b_ativo"]]
recortes.append(("ativos (exclui cancelado)", len(d)))
d = d.loc[~d["b_cauda"]]
recortes.append(("fora da cauda incompleta", len(d)))
d = d.loc[d["dt_compra"].notna() & d["dt_prazo"].notna()]
recortes.append(("com dt_compra e dt_prazo", len(d)))

for nome, n in recortes:
    diz("  " + nome.ljust(34) + format(n, ",").rjust(9)
        + format(n / len(df) * 100, ".2f").rjust(9) + "%")
R["recortes"] = [{"recorte": n, "pedidos": int(v)} for n, v in recortes]

diz()
diz("  A cauda sai porque o ALVO ainda estava a chegar, nao porque incomoda:")
diz("  " + format(int(df['b_cauda'].sum()), ",") + " pedidos nos ultimos "
    + str(man["parametros"]["cauda_dias"]) + " dias da janela.")

# --- geografia: dois graos espaciais diferentes, e e preciso dizer ----------
geo_cep = geo[["cep_prefixo", "lat", "lng"]].rename(
    columns={"cep_prefixo": "cliente_cep_prefixo", "lat": "lat_cli", "lng": "lng_cli"})
geo_cid = (geo.groupby(["cidade_canonica", "uf"], as_index=False)
              .agg(lat_fil=("lat", "median"), lng_fil=("lng", "median")))

d = d.merge(geo_cep, on="cliente_cep_prefixo", how="left")
d = d.merge(geo_cid.rename(columns={"cidade_canonica": "filial_cidade", "uf": "filial"}),
            on=["filial_cidade", "filial"], how="left")
d["distancia_km"] = haversine_km(d["lat_fil"], d["lng_fil"], d["lat_cli"], d["lng_cli"])

diz()
diz("  Grao espacial: cliente pelo CENTROIDE DO CEP (5 digitos), filial pelo")
diz("  CENTROIDE DA CIDADE. Nao e o mesmo grao, e a distancia herda o pior dos dois.")
diz("  distancia ausente: " + format(int(d["distancia_km"].isna().sum()), ",")
    + " de " + format(len(d), ",") + " pedidos ("
    + format(float(d["distancia_km"].isna().mean() * 100), ".2f") + "%)")
R["geo"] = {"distancia_ausente": int(d["distancia_km"].isna().sum()),
            "pct_distancia_ausente": round(float(d["distancia_km"].isna().mean() * 100), 3),
            "grao_cliente": "centroide do prefixo de CEP (5 digitos)",
            "grao_filial": "centroide da cidade"}

# ============================================================ FEATURES EM t0
secao("BLOCO 3.2 - AS FEATURES, E POR QUE CADA UMA EXISTE EM t0")

d["prazo_prometido_dias"] = (d["dt_prazo"] - d["dt_compra"]).dt.total_seconds() / 86400
d["folga_expedicao_dias"] = (d["dt_limite_expedicao"] - d["dt_compra"]).dt.total_seconds() / 86400
d["mes"] = d["dt_compra"].dt.month
d["dia_semana"] = d["dt_compra"].dt.dayofweek
d["hora"] = d["dt_compra"].dt.hour
d["mesma_uf"] = (d["filial"].astype(str) == d["cliente_uf"].astype(str)).astype(int)
d["rota"] = d["filial"].astype(str) + ">" + d["cliente_uf"].astype(str)

NUM = ["prazo_prometido_dias", "folga_expedicao_dias", "distancia_km", "peso_g",
       "valor_mercadoria", "valor_frete", "n_itens", "mes", "dia_semana", "hora", "mesma_uf"]
CAT = ["filial", "cliente_uf", "canal_pagamento"]

justificativa = {
    "prazo_prometido_dias": "prometido ao cliente no checkout; conhecido em t0",
    "folga_expedicao_dias": "shipping_limit_date do contrato com o vendedor; definido em t0",
    "distancia_km": "derivada de CEP de origem e destino, ambos em t0",
    "peso_g": "cadastro do produto; em t0 (nulo quando o cadastro falha)",
    "valor_mercadoria": "preco no checkout; em t0",
    "valor_frete": "frete cobrado no checkout; em t0",
    "n_itens": "itens do carrinho; em t0",
    "mes / dia_semana / hora": "timestamp da compra; em t0",
    "mesma_uf": "derivada das duas UFs; em t0",
    "filial / cliente_uf / canal_pagamento": "escolhidos no checkout; em t0",
}
for k, v in justificativa.items():
    diz("  " + k.ljust(38) + v)
diz()
diz("  FORA, por nao existirem em t0: dt_aprovacao, dt_coleta, dt_entrega,")
diz("  lead_dias, dias_vs_prazo, atraso_mensuravel.")
diz("  FORA, por serem metadado de qualidade e varias tautologicas com o alvo:")
diz("  as 21 colunas qa_*.")
R["features"] = {"numericas": NUM, "categoricas": CAT, "justificativa": justificativa,
                 "excluidas_pos_t0": ["dt_aprovacao", "dt_coleta", "dt_entrega", "lead_dias",
                                      "dias_vs_prazo", "atraso_mensuravel"],
                 "excluidas_qa": int(len([c for c in df.columns if c.startswith("qa_")]))}

for c in CAT:
    d[c] = d[c].astype(str).fillna("SEM")
X = d[NUM + CAT].copy()
for c in CAT:
    X[c] = X[c].astype("category")
y = d["y"].to_numpy()

# ============================================================ SPLIT TEMPORAL
secao("BLOCO 4.1 - SPLIT TEMPORAL (e o aleatorio como contraprova)")

corte = d["dt_compra"].max() - pd.Timedelta(days=DIAS_TESTE)
treino = d["dt_compra"] <= corte
teste = ~treino
diz("  corte           " + str(corte.date()))
diz("  treino          " + format(int(treino.sum()), ",").rjust(8) + " pedidos  "
    + str(d.loc[treino, "dt_compra"].min().date()) + " a " + str(d.loc[treino, "dt_compra"].max().date()))
diz("  teste           " + format(int(teste.sum()), ",").rjust(8) + " pedidos  "
    + str(d.loc[teste, "dt_compra"].min().date()) + " a " + str(d.loc[teste, "dt_compra"].max().date()))
diz("  atraso treino   " + format(float(y[treino.to_numpy()].mean() * 100), ".2f").rjust(8) + "%")
diz("  atraso teste    " + format(float(y[teste.to_numpy()].mean() * 100), ".2f").rjust(8) + "%")
R["split"] = {
    "corte": str(corte.date()), "dias_teste": DIAS_TESTE,
    "n_treino": int(treino.sum()), "n_teste": int(teste.sum()),
    "prevalencia_treino_pct": round(float(y[treino.to_numpy()].mean() * 100), 3),
    "prevalencia_teste_pct": round(float(y[teste.to_numpy()].mean() * 100), 3),
}

itr, ite = treino.to_numpy(), teste.to_numpy()


def treina(Xtr, ytr):
    m = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=.06, max_leaf_nodes=31, min_samples_leaf=40,
        l2_regularization=1.0, categorical_features="from_dtype", random_state=42)
    m.fit(Xtr, ytr)
    return m


modelo = treina(X[itr], y[itr])
p_teste = modelo.predict_proba(X[ite])[:, 1]
y_teste = y[ite]

# ============================================================ LINHA DE BASE
secao("BLOCO 4.1 - LINHA DE BASE: contra o que isto e comparado")

base_prev = np.full(len(y_teste), y[itr].mean())

# B1: regra de negocio - prazo curto e rota interestadual
lim_prazo = float(np.nanpercentile(d.loc[itr, "prazo_prometido_dias"], 25))
b1 = ((d.loc[ite, "prazo_prometido_dias"] < lim_prazo).astype(float)
      * .5 + (1 - d.loc[ite, "mesma_uf"]) * .5).to_numpy()

# B2: taxa historica de atraso por rota (a tabela que a operacao ja tem hoje)
taxa_rota = d.loc[itr].groupby("rota")["y"].agg(["mean", "size"])
taxa_rota = taxa_rota.loc[taxa_rota["size"] >= 30, "mean"]
b2 = d.loc[ite, "rota"].map(taxa_rota).fillna(y[itr].mean()).to_numpy()

bases = [
    ("B0 prevalencia do treino", base_prev),
    ("B1 regra: prazo curto + interestadual", b1),
    ("B2 taxa historica por rota UF>UF", b2),
    ("M  HistGradientBoosting em t0", p_teste),
]
diz("  " + "modelo".ljust(40) + "AUC".rjust(8) + "PR-AUC".rjust(9))
comp = []
for nome, p in bases:
    auc = roc_auc_score(y_teste, p) if len(np.unique(p)) > 1 else .5
    prauc = average_precision_score(y_teste, p)
    diz("  " + nome.ljust(40) + format(auc, ".4f").rjust(8) + format(prauc, ".4f").rjust(9))
    comp.append({"modelo": nome, "auc": round(float(auc), 4), "pr_auc": round(float(prauc), 4)})
R["linha_de_base"] = comp
R["prevalencia_teste"] = round(float(y_teste.mean()), 4)
R["b1_limite_prazo_p25_dias"] = round(lim_prazo, 2)
diz()
diz("  A prevalencia no teste e " + format(float(y_teste.mean() * 100), ".2f")
    + "%. Acuracia nao entra aqui: prever")
diz("  'nunca atrasa' acerta " + format(float((1 - y_teste.mean()) * 100), ".1f")
    + "% das vezes e nao avisa um unico atraso.")

# ============================================================ POR QUE ESTE MODELO
secao("BLOCO 4.1 - POR QUE ESTE MODELO, E POR QUE NAO OS OUTROS")

from sklearn.dummy import DummyClassifier  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.tree import DecisionTreeClassifier, export_text  # noqa: E402
from sklearn.compose import ColumnTransformer  # noqa: E402
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import OneHotEncoder, StandardScaler  # noqa: E402

# X sem category dtype, para os modelos que nao a suportam
Xp = d[NUM + CAT].copy()
pre = ColumnTransformer([
    ("num", Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())]), NUM),
    ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=30, sparse_output=False), CAT),
])

# 1. uma unica variavel: a regra que a operacao pode escrever amanha
X1 = d[["prazo_prometido_dias"]].fillna(d["prazo_prometido_dias"].median())
m1 = DecisionTreeClassifier(max_depth=1, random_state=42).fit(X1[itr], y[itr])
p1 = m1.predict_proba(X1[ite])[:, 1]
corte_regra = float(m1.tree_.threshold[0])

# 2. arvore de profundidade 3: uma regra auditavel, escrita em texto
m3 = DecisionTreeClassifier(max_depth=3, min_samples_leaf=200, random_state=42)
m3.fit(Xp[NUM].fillna(Xp[NUM].median())[itr], y[itr])
p3 = m3.predict_proba(Xp[NUM].fillna(Xp[NUM].median())[ite])[:, 1]

# 3. regressao logistica
mlr = Pipeline([("pre", pre), ("lr", LogisticRegression(max_iter=2000, C=1.0))])
mlr.fit(Xp[itr], y[itr])
plr = mlr.predict_proba(Xp[ite])[:, 1]

# 4. random forest
mrf = Pipeline([("pre", pre), ("rf", RandomForestClassifier(
    n_estimators=300, min_samples_leaf=20, n_jobs=-1, random_state=42))])
mrf.fit(Xp[itr], y[itr])
prf = mrf.predict_proba(Xp[ite])[:, 1]

# 5. HistGB so com prazo_prometido_dias, para medir quanto vem de uma coluna
Xso = X[["prazo_prometido_dias"]]
mso = treina(Xso[itr], y[itr])
pso = mso.predict_proba(Xso[ite])[:, 1]

candidatos = [
    ("nada (prever a prevalencia)", np.full(len(y_teste), y[itr].mean()),
     "linha de base obrigatoria; nao avisa nenhum atraso"),
    ("regra de 1 corte: prazo < " + format(corte_regra, ".1f") + "d", p1,
     "e o que a operacao consegue executar sem modelo nenhum"),
    ("arvore de profundidade 3", p3,
     "auditavel: cabe numa folha de papel e tem dono"),
    ("regressao logistica", plr,
     "coeficiente por variavel; assume efeito linear e monotono"),
    ("random forest (300 arvores)", prf,
     "captura interacao; caro de explicar e de servir"),
    ("HistGB so com prazo_prometido_dias", pso,
     "mede quanto do desempenho vem de UMA coluna"),
    ("HistGB com as 14 features (escolhido)", p_teste,
     "trata nulo e categoria nativamente; sem dependencia extra"),
]
diz("  " + "modelo".ljust(40) + "AUC".rjust(8) + "PR-AUC".rjust(9) + "  porque esta na lista")
escolha = []
for nome, p, porque in candidatos:
    auc = roc_auc_score(y_teste, p) if len(np.unique(p)) > 1 else .5
    prauc = average_precision_score(y_teste, p)
    diz("  " + nome.ljust(40) + format(auc, ".4f").rjust(8) + format(prauc, ".4f").rjust(9)
        + "  " + porque)
    escolha.append({"modelo": nome, "auc": round(float(auc), 4),
                    "pr_auc": round(float(prauc), 4), "porque": porque})
R["comparacao_modelos"] = escolha
R["regra_corte_prazo_dias"] = round(corte_regra, 2)

diz()
diz("  A arvore de profundidade 3, escrita:")
for ln in export_text(m3, feature_names=NUM, max_depth=3, decimals=1).split("\n")[:24]:
    diz("    " + ln)
R["arvore_texto"] = export_text(m3, feature_names=NUM, max_depth=3, decimals=1)

diz()
diz("  DESCARTADOS SEM TESTAR, e porque:")
descartados = {
    "cluster do KMeans como feature":
        "os clusters da analise anterior usam lead_dias, que so existe DEPOIS da "
        "entrega. Cluster 1 tem 59,1% de atraso porque e feito de entregas lentas: "
        "e descricao, nao previsao. Usa-lo em t0 seria fuga.",
    "rede neural":
        "97 mil linhas, 14 colunas tabulares. Nao ha ganho documentado sobre "
        "arvores impulsionadas neste regime, e o custo de explicar sobe.",
    "CatBoost / XGBoost":
        "empatam com o HistGB do sklearn em tabular deste tamanho e acrescentam "
        "uma dependencia para servir em producao.",
    "SMOTE / reamostragem da classe":
        "o desequilibrio (6,5%) trata-se movendo o LIMIAR com a matriz de custo, "
        "que e uma decisao de gestao visivel, e nao distorcendo a distribuicao "
        "de treino, que a esconde.",
}
for k, v in descartados.items():
    diz("    " + k)
    diz("      " + v)
R["descartados"] = descartados

# ============================================================ CUSTO DO ERRO
secao("BLOCO 4.1 - CUSTO DO ERRO: onde por o limiar, e quem escolhe")

diz("  Parametros DECLARADOS (nao estao nos dados):")
diz("    falso negativo (atraso nao avisado)  R$ " + format(CUSTO_FN, ".2f"))
diz("    falso positivo (alarme falso)        R$ " + format(CUSTO_FP, ".2f"))
diz()
diz("  " + "limiar".rjust(7) + "avisados".rjust(10) + "VP".rjust(7) + "FP".rjust(7)
    + "FN".rjust(7) + "recall".rjust(9) + "precisao".rjust(10) + "custo R$".rjust(12))
curva = []
melhor = None
for t in [.05, .08, .10, .12, .15, .20, .25, .30, .40, .50]:
    prev = p_teste >= t
    vp = int(((prev == 1) & (y_teste == 1)).sum())
    fp = int(((prev == 1) & (y_teste == 0)).sum())
    fn = int(((prev == 0) & (y_teste == 1)).sum())
    rec = vp / max(vp + fn, 1)
    pre = vp / max(vp + fp, 1)
    custo = fn * CUSTO_FN + fp * CUSTO_FP
    diz("  " + format(t, ".2f").rjust(7) + format(int(prev.sum()), ",").rjust(10)
        + format(vp, ",").rjust(7) + format(fp, ",").rjust(7) + format(fn, ",").rjust(7)
        + format(rec * 100, ".1f").rjust(8) + "%" + format(pre * 100, ".1f").rjust(9) + "%"
        + format(custo, ",.0f").rjust(12))
    reg = {"limiar": t, "avisados": int(prev.sum()), "vp": vp, "fp": fp, "fn": fn,
           "recall": round(rec, 4), "precisao": round(pre, 4), "custo_reais": round(custo, 2)}
    curva.append(reg)
    if melhor is None or custo < melhor["custo_reais"]:
        melhor = reg
custo_nada = int(y_teste.sum()) * CUSTO_FN
diz()
diz("  nao fazer nada (avisar zero):        R$ " + format(custo_nada, ",.0f")
    + "  (" + format(int(y_teste.sum()), ",") + " atrasos, nenhum avisado)")
diz("  melhor limiar " + format(melhor["limiar"], ".2f") + ":                   R$ "
    + format(melhor["custo_reais"], ",.0f") + "  -> poupa R$ "
    + format(custo_nada - melhor["custo_reais"], ",.0f") + " em " + str(DIAS_TESTE) + " dias")
R["custo_erro"] = {"curva": curva, "melhor": melhor, "custo_nao_fazer_nada": round(custo_nada, 2),
                   "poupanca_reais_janela_teste": round(custo_nada - melhor["custo_reais"], 2),
                   "poupanca_anualizada": round((custo_nada - melhor["custo_reais"]) * 365 / DIAS_TESTE, 2)}
diz("  anualizado (fator " + format(365 / DIAS_TESTE, ".2f") + "x, supondo volume e mix constantes): R$ "
    + format((custo_nada - melhor["custo_reais"]) * 365 / DIAS_TESTE, ",.0f"))

# ============================================================ FUGA DE DADOS
secao("BLOCO 4.1 - FUGA DE DADOS: as duas provas que fizemos")

# 1) split aleatorio
rng = np.random.default_rng(42)
idx = rng.permutation(len(d))
n_tr = int(itr.sum())
ale_tr = np.zeros(len(d), bool); ale_tr[idx[:n_tr]] = True
m_ale = treina(X[ale_tr], y[ale_tr])
auc_ale = roc_auc_score(y[~ale_tr], m_ale.predict_proba(X[~ale_tr])[:, 1])

# 2) uma coluna pos-t0 dentro do modelo
X_fuga = X.copy()
X_fuga["lead_dias"] = pd.to_numeric(d["lead_dias"], errors="coerce").to_numpy()
m_fuga = treina(X_fuga[itr], y[itr])
auc_fuga = roc_auc_score(y_teste, m_fuga.predict_proba(X_fuga[ite])[:, 1])

# 3) uma flag qa_ tautologica
X_taut = X.copy()
X_taut["qa_sem_dt_entrega"] = booleana(d["qa_sem_dt_entrega"]).astype(int).to_numpy()
m_taut = treina(X_taut[itr], y[itr])
auc_taut = roc_auc_score(y_teste, m_taut.predict_proba(X_taut[ite])[:, 1])

auc_ok = float(roc_auc_score(y_teste, p_teste))
diz("  " + "configuracao".ljust(46) + "AUC".rjust(8) + "delta".rjust(9))
for nome, v in [("split temporal, so features em t0 (o nosso)", auc_ok),
                ("split ALEATORIO, mesmas features", float(auc_ale)),
                ("+ lead_dias (existe so depois da entrega)", float(auc_fuga)),
                ("+ qa_sem_dt_entrega (tautologica com o alvo)", float(auc_taut))]:
    diz("  " + nome.ljust(46) + format(v, ".4f").rjust(8)
        + (format((v - auc_ok) * 100, "+.2f").rjust(8) + "pp" if nome.startswith("split A") or nome.startswith("+") else "".rjust(10)))
R["fuga"] = {"auc_temporal_t0": round(auc_ok, 4), "auc_split_aleatorio": round(float(auc_ale), 4),
             "auc_com_lead_dias": round(float(auc_fuga), 4),
             "auc_com_flag_tautologica": round(float(auc_taut), 4)}
diz()
diz("  As tres ultimas linhas sao os numeros que NAO temos direito de apresentar.")
diz("  Estao aqui para mostrar quanto de 'desempenho' se compra sem sair do Excel.")

# ============================================================ O SINAL APARECEU
secao("BLOCO 4.1 - O SINAL NAO FOI APRENDIDO: APARECEU DENTRO DO TESTE")

# quintis de prazo fixados no TREINO, aplicados aos dois lados
qs = np.nanpercentile(d.loc[itr, "prazo_prometido_dias"], [0, 20, 40, 60, 80, 100])
qs[0], qs[-1] = -np.inf, np.inf
rot = ["q1 mais curto", "q2", "q3", "q4", "q5 mais longo"]
d["q_prazo"] = pd.cut(d["prazo_prometido_dias"], qs, labels=rot)
d["grupo"] = np.where(itr, "treino", "teste")

diz("  Taxa de atraso por quintil de prazo prometido (quintis fixados no treino):")
diz("  " + "quintil".ljust(16) + "prazo p50".rjust(11)
    + "treino".rjust(19) + "teste".rjust(19) + "delta".rjust(10))
quint = []
for q in rot:
    g = d.loc[d["q_prazo"] == q]
    gtr, gte = g.loc[g["grupo"] == "treino"], g.loc[g["grupo"] == "teste"]
    tr, te = float(gtr["y"].mean()), float(gte["y"].mean())
    diz("  " + q.ljust(16) + format(float(g["prazo_prometido_dias"].median()), ".1f").rjust(9) + "d"
        + (format(tr * 100, ".2f") + "% (n=" + format(len(gtr), ",") + ")").rjust(19)
        + (format(te * 100, ".2f") + "% (n=" + format(len(gte), ",") + ")").rjust(19)
        + format((te - tr) * 100, "+.2f").rjust(8) + "pp")
    quint.append({"quintil": q, "prazo_p50_dias": round(float(g["prazo_prometido_dias"].median()), 1),
                  "treino_pct": round(tr * 100, 3), "n_treino": int(len(gtr)),
                  "teste_pct": round(te * 100, 3), "n_teste": int(len(gte)),
                  "delta_pp": round((te - tr) * 100, 3)})

rho_tr = float(d.loc[itr, "prazo_prometido_dias"].corr(d.loc[itr, "y"], method="spearman"))
rho_te = float(d.loc[ite, "prazo_prometido_dias"].corr(d.loc[ite, "y"], method="spearman"))
mix_tr = float((d.loc[itr, "q_prazo"] == "q1 mais curto").mean())
mix_te = float((d.loc[ite, "q_prazo"] == "q1 mais curto").mean())
diz()
diz("  Spearman(prazo, atraso)   treino " + format(rho_tr, "+.4f")
    + "   teste " + format(rho_te, "+.4f"))
diz("  fracao no quintil mais curto   treino " + format(mix_tr * 100, ".1f")
    + "%   teste " + format(mix_te * 100, ".1f") + "%")
diz()
diz("  A LEITURA, e e a conclusao mais importante desta analise:")
diz("  no treino o prazo prometido NAO separa atraso (rho = " + format(rho_tr, ".4f")
    + ", taxa quase")
diz("  plana entre quintis). No teste separa forte. O sinal nao foi aprendido no")
diz("  treino - apareceu no teste, porque a politica de prazo mudou (a fracao de")
diz("  prazos curtos passou de " + format(mix_tr * 100, ".0f") + "% para "
    + format(mix_te * 100, ".0f") + "%). O AUC de " + format(auc_ok, ".2f") + " nao e")
diz("  uma relacao estavel: e o retrato de uma mudanca de regime dentro da janela")
diz("  de avaliacao. E por isso que a regra de 1 corte e a arvore, ajustadas no")
diz("  treino, dao AUC ABAIXO de 0,50 no teste.")
R["sinal_apareceu"] = {
    "quintis": quint, "spearman_treino": round(rho_tr, 4), "spearman_teste": round(rho_te, 4),
    "fracao_q1_treino_pct": round(mix_tr * 100, 2), "fracao_q1_teste_pct": round(mix_te * 100, 2),
    "leitura": ("o sinal nao foi aprendido no treino; apareceu no teste com a mudanca "
                "da politica de prazo, e por isso os modelos ajustados no treino dao "
                "AUC abaixo de 0,50"),
}

# ============================================================ DERIVA
secao("BLOCO 4.2 - DERIVA: o alvo move-se dentro da propria janela")

d["b_atrasado"] = booleana(d["atrasado"]).astype(int)
d["b_vencido"] = booleana(d["vencido_sem_registo"]).astype(int)
por_mes = (d.assign(m=d["dt_compra"].dt.to_period("M").astype(str))
            .groupby("m").agg(pedidos=("y", "size"), atraso=("y", "mean"),
                              atrasado=("b_atrasado", "mean"), vencido=("b_vencido", "mean")))
por_mes = por_mes.loc[por_mes["pedidos"] >= 200]
diz("  " + "mes".ljust(10) + "pedidos".rjust(9) + "honesto".rjust(10)
    + "atrasado".rjust(10) + "vencido s/reg".rjust(15))
for m, r in por_mes.iterrows():
    diz("  " + str(m).ljust(10) + format(int(r["pedidos"]), ",").rjust(9)
        + format(float(r["atraso"]) * 100, ".2f").rjust(9) + "%"
        + format(float(r["atrasado"]) * 100, ".2f").rjust(9) + "%"
        + format(float(r["vencido"]) * 100, ".2f").rjust(14) + "%")
estavel = por_mes.loc[por_mes.index >= "2017-05"]
diz()
diz("  Decomposto, o alvo tem duas componentes com comportamentos diferentes:")
diz("  'vencido sem registo' de 2017-05 em diante fica entre "
    + format(float(estavel["vencido"].min() * 100), ".2f") + "% e "
    + format(float(estavel["vencido"].max() * 100), ".2f") + "%,")
diz("  sem tendencia. Antes disso sobe ate "
    + format(float(por_mes.loc[por_mes.index < "2017-05", "vencido"].max() * 100), ".2f")
    + "% - e imaturidade de inicio de janela,")
diz("  o espelho da cauda no outro extremo, e os primeiros meses tambem nao servem")
diz("  para treinar. A volatilidade do alvo vem de 'atrasado', isto e, da operacao:")
diz("  o pico de 2018-03 (" + format(float(por_mes.loc['2018-03', 'atrasado'] * 100), ".2f")
    + "%) e atraso real, nao falta de registo.")
R["decomposicao_alvo"] = {
    "vencido_sem_registo_min_pct_pos_2017_05": round(float(estavel["vencido"].min() * 100), 3),
    "vencido_sem_registo_max_pct_pos_2017_05": round(float(estavel["vencido"].max() * 100), 3),
    "vencido_max_pct_antes_2017_05": round(
        float(por_mes.loc[por_mes.index < "2017-05", "vencido"].max() * 100), 3),
    "pico_atrasado_2018_03_pct": round(float(por_mes.loc["2018-03", "atrasado"] * 100), 3),
}
amp = float(por_mes["atraso"].max() - por_mes["atraso"].min())
diz()
diz("  amplitude entre o melhor e o pior mes: " + format(amp * 100, ".2f") + " p.p.  ("
    + format(float(por_mes["atraso"].min() * 100), ".2f") + "% a "
    + format(float(por_mes["atraso"].max() * 100), ".2f") + "%)")
diz("  Um modelo treinado na media desta janela nao existe em nenhum mes dela.")
R["deriva"] = {"por_mes": [{"mes": str(m), "pedidos": int(r["pedidos"]),
                            "atraso_pct": round(float(r["atraso"]) * 100, 3)}
                           for m, r in por_mes.iterrows()],
               "amplitude_pp": round(amp * 100, 3)}

# AUC por mes no teste
d_te = d.loc[ite].assign(p=p_teste, m=lambda x: x["dt_compra"].dt.to_period("M").astype(str))
auc_mes = []
diz()
diz("  AUC por mes DENTRO do teste (o modelo nao se degrada igual em todos):")
for m, g in d_te.groupby("m"):
    if g["y"].nunique() < 2 or len(g) < 200:
        continue
    a = float(roc_auc_score(g["y"], g["p"]))
    diz("    " + str(m) + "  n=" + format(len(g), ",").rjust(7) + "  AUC=" + format(a, ".4f")
        + "  atraso=" + format(float(g["y"].mean() * 100), ".2f") + "%")
    auc_mes.append({"mes": str(m), "n": int(len(g)), "auc": round(a, 4),
                    "atraso_pct": round(float(g["y"].mean() * 100), 3)})
R["auc_por_mes_no_teste"] = auc_mes

# ============================================================ ETICA
secao("BLOCO 5 - ETICA: quem carrega o risco quando o sistema funciona")

REGIAO = {
    "N": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "NE": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "CO": ["DF", "GO", "MT", "MS"],
    "SE": ["ES", "MG", "RJ", "SP"],
    "S": ["PR", "RS", "SC"],
}
uf2reg = {u: r for r, us in REGIAO.items() for u in us}
d_te = d_te.assign(regiao=d_te["cliente_uf"].map(uf2reg).fillna("??"))

lim = melhor["limiar"]
diz("  No limiar escolhido (" + format(lim, ".2f") + "):")
diz("  " + "regiao".ljust(8) + "pedidos".rjust(9) + "atraso".rjust(9) + "avisados".rjust(10)
    + "recall".rjust(9) + "precisao".rjust(10))
etica = []
for reg, g in d_te.groupby("regiao"):
    if len(g) < 100:
        continue
    prev = (g["p"] >= lim)
    vp = int((prev & (g["y"] == 1)).sum()); fp = int((prev & (g["y"] == 0)).sum())
    fn = int((~prev & (g["y"] == 1)).sum())
    rec = vp / max(vp + fn, 1); pre = vp / max(vp + fp, 1)
    diz("  " + str(reg).ljust(8) + format(len(g), ",").rjust(9)
        + format(float(g["y"].mean() * 100), ".2f").rjust(8) + "%"
        + format(int(prev.sum()), ",").rjust(10)
        + format(rec * 100, ".1f").rjust(8) + "%" + format(pre * 100, ".1f").rjust(9) + "%")
    etica.append({"regiao": str(reg), "pedidos": int(len(g)),
                  "atraso_pct": round(float(g["y"].mean() * 100), 3),
                  "avisados": int(prev.sum()), "recall": round(rec, 4), "precisao": round(pre, 4)})
R["etica_regiao"] = etica

diz()
diz("  Por decil de distancia:")
d_te2 = d_te.loc[d_te["distancia_km"].notna()].copy()
d_te2["decil"] = pd.qcut(d_te2["distancia_km"], 10, labels=False, duplicates="drop") + 1
diz("  " + "decil".rjust(6) + "km p50".rjust(10) + "pedidos".rjust(9) + "atraso".rjust(9)
    + "recall".rjust(9))
etica_d = []
for dc, g in d_te2.groupby("decil"):
    prev = (g["p"] >= lim)
    vp = int((prev & (g["y"] == 1)).sum()); fn = int((~prev & (g["y"] == 1)).sum())
    rec = vp / max(vp + fn, 1)
    diz("  " + str(int(dc)).rjust(6) + format(float(g["distancia_km"].median()), ",.0f").rjust(10)
        + format(len(g), ",").rjust(9) + format(float(g["y"].mean() * 100), ".2f").rjust(8) + "%"
        + format(rec * 100, ".1f").rjust(8) + "%")
    etica_d.append({"decil": int(dc), "km_p50": round(float(g["distancia_km"].median()), 1),
                    "pedidos": int(len(g)), "atraso_pct": round(float(g["y"].mean() * 100), 3),
                    "recall": round(rec, 4)})
R["etica_distancia"] = etica_d

# quem fica sem aviso por nao ter geolocalizacao
sem_geo = d_te["distancia_km"].isna()
if int(sem_geo.sum()):
    g = d_te.loc[sem_geo]
    prev = (g["p"] >= lim)
    vp = int((prev & (g["y"] == 1)).sum()); fn = int((~prev & (g["y"] == 1)).sum())
    diz()
    diz("  Pedidos sem distancia (CEP sem centroide): " + format(len(g), ",")
        + ", atraso " + format(float(g["y"].mean() * 100), ".2f") + "%, recall "
        + format(vp / max(vp + fn, 1) * 100, ".1f") + "%")
    R["etica_sem_geo"] = {"pedidos": int(len(g)),
                          "atraso_pct": round(float(g["y"].mean() * 100), 3),
                          "recall": round(vp / max(vp + fn, 1), 4)}

# ============================================================ IMPORTANCIA
secao("BLOCO 4.1 - O QUE O MODELO USA (permutacao no teste, queda de AUC)")

rng2 = np.random.default_rng(7)
imp = []
Xte = X[ite].reset_index(drop=True)
for c in Xte.columns:
    col = Xte[c].to_numpy(copy=True)
    Xp = Xte.copy()
    Xp[c] = rng2.permutation(col)
    if str(Xte[c].dtype) == "category":
        Xp[c] = Xp[c].astype("category")
    a = float(roc_auc_score(y_teste, modelo.predict_proba(Xp)[:, 1]))
    imp.append({"feature": c, "auc_permutado": round(a, 4), "queda_pp": round((auc_ok - a) * 100, 3)})
imp.sort(key=lambda r: -r["queda_pp"])
diz("  " + "feature".ljust(24) + "AUC permutado".rjust(15) + "queda p.p.".rjust(12))
for r in imp:
    diz("  " + r["feature"].ljust(24) + format(r["auc_permutado"], ".4f").rjust(15)
        + format(r["queda_pp"], "+.2f").rjust(12))
R["importancia_permutacao"] = imp

SAIDA_JSON = RAIZ / "docs" / ("E4-6_atraso_" + args.versao + ".json")
SAIDA_TXT = RAIZ / "docs" / ("E4-6_atraso_" + args.versao + ".txt")
SAIDA_JSON.write_text(json.dumps(R, indent=2, ensure_ascii=False), encoding="utf-8")
SAIDA_TXT.write_text("\n".join(linhas) + "\n", encoding="utf-8")
diz()
diz("salvo em " + str(SAIDA_JSON))
