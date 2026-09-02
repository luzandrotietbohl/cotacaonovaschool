"""E4.6 parte 2 - Analise (bloco 4), etica (bloco 5) e configuracao da decisao (bloco 6).

Usa docs/_e46_embarques.pkl (produzido por scripts/analise_e46.py) e os artefatos
de modelos/olist/atual. Saida: docs/E4-6_analises_parte2.{json,txt}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cotador.ml.features import CATEGORICAS, NUMERICAS, preparar_features

MODELOS = RAIZ / "modelos" / "olist" / "atual"
SAIDA_JSON = RAIZ / "docs" / "E4-6_analises_parte2.json"
SAIDA_TXT = RAIZ / "docs" / "E4-6_analises_parte2.txt"

R = {}
linhas = []


def diz(t=""):
    linhas.append(t)
    print(t)


def secao(t):
    diz("")
    diz("=" * 78)
    diz(t)
    diz("=" * 78)


dados = pd.read_pickle(RAIZ / "docs" / "_e46_embarques.pkl")
meta = json.loads((MODELOS / "metadata.json").read_text(encoding="utf-8"))
ajustes = meta["calibration_log_adjustments"]

n = len(dados)
fim_treino, fim_calib = int(n * .70), int(n * .85)
treino = dados.iloc[:fim_treino]
calib = dados.iloc[fim_treino:fim_calib]
teste = dados.iloc[fim_calib:].copy()
x_treino, x_teste = preparar_features(treino), preparar_features(teste)
real = teste["freight_value"].to_numpy(float)

modelos = {}
for nome in ("p25", "p50", "p75"):
    m = CatBoostRegressor()
    m.load_model(str(MODELOS / ("model_" + nome + ".cbm")))
    modelos[nome] = m
prev = {k: np.expm1(m.predict(x_teste) + ajustes[k]) for k, m in modelos.items()}
matriz = np.sort(np.maximum(np.vstack([prev["p25"], prev["p50"], prev["p75"]]).T, 0), axis=1)
p25, p50, p75 = matriz.T
teste["p25"], teste["p50"], teste["p75"] = p25, p50, p75
teste["erro"] = p50 - real
teste["erro_pct"] = np.where(real > 0, (p50 - real) / real * 100, np.nan)

secao("SPLIT TEMPORAL")
diz("  treino     " + format(len(treino), ",") + "  " + str(treino['order_purchase_timestamp'].min())[:10]
    + " a " + str(treino['order_purchase_timestamp'].max())[:10])
diz("  calibracao " + format(len(calib), ",") + "  " + str(calib['order_purchase_timestamp'].min())[:10]
    + " a " + str(calib['order_purchase_timestamp'].max())[:10])
diz("  teste      " + format(len(teste), ",") + "  " + str(teste['order_purchase_timestamp'].min())[:10]
    + " a " + str(teste['order_purchase_timestamp'].max())[:10])
R["split"] = {"treino": len(treino), "calibracao": len(calib), "teste": len(teste),
              "treino_ate": str(treino["order_purchase_timestamp"].max())[:10],
              "teste_de": str(teste["order_purchase_timestamp"].min())[:10],
              "teste_ate": str(teste["order_purchase_timestamp"].max())[:10]}


def metricas(pred, y=real):
    pred = np.asarray(pred, float)
    ok = y > 0
    return {"mae": round(float(np.mean(np.abs(pred - y))), 2),
            "medae": round(float(np.median(np.abs(pred - y))), 2),
            "medape": round(float(np.median(np.abs((pred[ok] - y[ok]) / y[ok])) * 100), 2),
            "vies_medio": round(float(np.mean(pred - y)), 2)}


# ==================================================== 4.1 LINHA DE BASE
secao("BLOCO 4.1 - LINHA DE BASE: contra o que o modelo e comparado")

mediana_global = float(treino["freight_value"].median())
b_global = np.full(len(teste), mediana_global)

med_rota = treino.groupby("route")["freight_value"].median()
b_rota = teste["route"].map(med_rota).fillna(mediana_global).to_numpy(float)

faixas = [0, .3, .5, 1, 2, 5, 10, 30, 1e9]
rot = ["<0.3", "0.3-0.5", "0.5-1", "1-2", "2-5", "5-10", "10-30", ">30"]
tr = treino.assign(fx=pd.cut(treino["weight_total_kg"], faixas, labels=rot))
te = teste.assign(fx=pd.cut(teste["weight_total_kg"], faixas, labels=rot))
med_fx = tr.groupby("fx", observed=True)["freight_value"].median()
b_peso = te["fx"].map(med_fx).astype(float).fillna(mediana_global).to_numpy()

med_rf = tr.groupby(["route", "fx"], observed=True)["freight_value"].median()
chave = list(zip(te["route"], te["fx"]))
b_tabela = np.array([med_rf.get(k, np.nan) for k in chave], float)
faltou = np.isnan(b_tabela)
b_tabela[faltou] = b_rota[faltou]
R["tabela_cobertura_celula"] = round(float(1 - faltou.mean()) * 100, 2)

# regra linear tipo tarifario: frete = a + b*kg + c*km, ajustada por minimos quadrados no treino
A = np.column_stack([np.ones(len(treino)), treino["weight_total_kg"], treino["distance_km"]])
coef, *_ = np.linalg.lstsq(A, treino["freight_value"].to_numpy(float), rcond=None)
b_linear = (np.column_stack([np.ones(len(teste)), teste["weight_total_kg"], teste["distance_km"]]) @ coef)
R["regra_linear_coef"] = {"intercepto": round(float(coef[0]), 2), "por_kg": round(float(coef[1]), 2),
                          "por_km": round(float(coef[2]), 4)}

bases = {
    "B0 mediana global do treino": b_global,
    "B1 mediana por rota UF": b_rota,
    "B2 mediana por faixa de peso": b_peso,
    "B3 tabela rota x faixa de peso": b_tabela,
    "B4 regra linear a+b*kg+c*km": b_linear,
    "M  CatBoost quantilico P50": p50,
}
R["linha_de_base"] = {}
diz("modelo".ljust(34) + "MAE".rjust(9) + "MedAE".rjust(9) + "MedAPE".rjust(9) + "vies".rjust(9) + "ganho MAE".rjust(11))
mae_b3 = metricas(b_tabela)["mae"]
for nome, pred in bases.items():
    m = metricas(pred)
    m["ganho_vs_B3_pct"] = round((mae_b3 - m["mae"]) / mae_b3 * 100, 1)
    R["linha_de_base"][nome] = m
    diz(nome.ljust(34) + format(m["mae"], ".2f").rjust(9) + format(m["medae"], ".2f").rjust(9)
        + format(m["medape"], ".1f").rjust(8) + "%" + format(m["vies_medio"], ".2f").rjust(9)
        + format(m["ganho_vs_B3_pct"], ".1f").rjust(10) + "%")

cobertura = float(np.mean((real >= p25) & (real <= p75)))
R["cobertura_p25_p75"] = round(cobertura * 100, 2)
R["abaixo_p25"] = round(float(np.mean(real < p25)) * 100, 2)
R["acima_p75"] = round(float(np.mean(real > p75)) * 100, 2)
diz("")
diz("  Intervalo P25-P75: cobertura real " + format(cobertura * 100, ".2f") + "% (nominal 50%); "
    + format(R['abaixo_p25'], ".2f") + "% abaixo, " + format(R['acima_p75'], ".2f") + "% acima")

# ==================================================== 4.1 FUGA DE DADOS
secao("BLOCO 4.1 - FUGA DE DADOS: o que muda quando o split e aleatorio")

params = dict(meta["catboost_parameters"])
params["iterations"] = 250


def treina_p50(idx_tr, idx_cal, idx_te):
    tr_, cal_, te_ = dados.iloc[idx_tr], dados.iloc[idx_cal], dados.iloc[idx_te]
    m = CatBoostRegressor(loss_function="Quantile:alpha=0.5", **params)
    m.fit(preparar_features(tr_), np.log1p(tr_["freight_value"].to_numpy(float)), cat_features=CATEGORICAS)
    res = np.log1p(cal_["freight_value"].to_numpy(float)) - m.predict(preparar_features(cal_))
    aj = float(np.quantile(res, .5))
    pred = np.maximum(np.expm1(m.predict(preparar_features(te_)) + aj), 0)
    return metricas(pred, te_["freight_value"].to_numpy(float))


idx = np.arange(n)
m_temporal = treina_p50(idx[:fim_treino], idx[fim_treino:fim_calib], idx[fim_calib:])
rng = np.random.default_rng(42)
emb = rng.permutation(idx)
m_aleatorio = treina_p50(emb[:fim_treino], emb[fim_treino:fim_calib], emb[fim_calib:])
R["fuga_split"] = {"temporal": m_temporal, "aleatorio": m_aleatorio,
                   "otimismo_mae_pct": round((m_temporal["mae"] - m_aleatorio["mae"]) / m_temporal["mae"] * 100, 1)}
diz("  split temporal  (honesto):  MAE " + format(m_temporal["mae"], ".2f") + "  MedAPE "
    + format(m_temporal["medape"], ".1f") + "%")
diz("  split aleatorio (fuga):     MAE " + format(m_aleatorio["mae"], ".2f") + "  MedAPE "
    + format(m_aleatorio["medape"], ".1f") + "%")
diz("  otimismo do split aleatorio: " + format(R["fuga_split"]["otimismo_mae_pct"], ".1f") + "% de MAE a menos")

# fuga por sobrevivencia: o filtro delivered so e conhecido depois
diz("")
diz("  Fuga por sobrevivencia: o treino so ve pedidos com order_status=delivered,")
diz("  status que so existe ~10 dias depois da cotacao. Ver bloco 3.4 (2.453 linhas")
diz("  excluidas, frete mediano R$ 16,46 contra R$ 16,26 das mantidas).")

# ==================================================== 4.1 CUSTO EM REAIS
secao("BLOCO 4.1 - CUSTO EM REAIS DE CADA TIPO DE ERRO")

sub = teste.loc[teste["erro"] < 0]
sobre = teste.loc[teste["erro"] > 0]
dias_teste = (pd.to_datetime(teste["order_purchase_timestamp"]).max()
              - pd.to_datetime(teste["order_purchase_timestamp"]).min()).days
fator_ano = 365 / max(dias_teste, 1)

custo = {
    "dias_no_teste": int(dias_teste),
    "fator_anualizacao": round(fator_ano, 2),
    "n_subprecificado": int(len(sub)),
    "pct_subprecificado": round(len(sub) / len(teste) * 100, 1),
    "margem_perdida_total": round(float(-sub["erro"].sum()), 2),
    "margem_perdida_media": round(float(-sub["erro"].mean()), 2),
    "n_sobreprecificado": int(len(sobre)),
    "pct_sobreprecificado": round(len(sobre) / len(teste) * 100, 1),
    "excesso_cobrado_total": round(float(sobre["erro"].sum()), 2),
}
for limite in (10, 20, 30):
    perdidos = teste.loc[teste["erro_pct"] > limite]
    custo["cotacoes_acima_de_" + str(limite) + "pct"] = {
        "n": int(len(perdidos)), "pct": round(len(perdidos) / len(teste) * 100, 1),
        "receita_de_frete_em_risco": round(float(perdidos["freight_value"].sum()), 2)}
custo["margem_perdida_ano"] = round(custo["margem_perdida_total"] * fator_ano, 2)
custo["receita_em_risco_ano_corte20pct"] = round(
    custo["cotacoes_acima_de_20pct"]["receita_de_frete_em_risco"] * fator_ano, 2)
for k, v in custo.items():
    diz("  " + k + ": " + json.dumps(v, ensure_ascii=False))
R["custo_erro"] = custo

# ==================================================== 4.2 DERIVA
secao("BLOCO 4.2 - DERIVA: o alvo se move dentro da propria janela")
d = dados.copy()
d["mes"] = pd.to_datetime(d["order_purchase_timestamp"]).dt.to_period("M").astype(str)
serie = d.groupby("mes").agg(n=("freight_value", "size"), frete_p50=("freight_value", "median"),
                             kg_p50=("weight_total_kg", "median"), km_p50=("distance_km", "median"))
serie = serie.loc[serie["n"] >= 200]
R["deriva_mensal"] = [{"mes": i, "n": int(r["n"]), "frete_p50": round(float(r["frete_p50"]), 2),
                       "kg_p50": round(float(r["kg_p50"]), 3), "km_p50": round(float(r["km_p50"]), 1)}
                      for i, r in serie.iterrows()]
diz("  mes        n     frete_p50  kg_p50   km_p50")
for r in R["deriva_mensal"]:
    diz("  " + r["mes"] + format(r["n"], ",").rjust(8) + format(r["frete_p50"], ".2f").rjust(11)
        + format(r["kg_p50"], ".3f").rjust(9) + format(r["km_p50"], ".0f").rjust(9))
amp = serie["frete_p50"].max() / serie["frete_p50"].min() - 1
R["deriva_amplitude_frete_p50_pct"] = round(float(amp) * 100, 1)
diz("  amplitude do frete mediano mensal: " + format(amp * 100, ".1f") + "%")

t = teste.copy()
t["mes"] = pd.to_datetime(t["order_purchase_timestamp"]).dt.to_period("M").astype(str)
erro_mes = t.groupby("mes").agg(n=("erro", "size"), mae=("erro", lambda s: float(np.mean(np.abs(s)))))
erro_mes = erro_mes.loc[erro_mes["n"] >= 200]
R["mae_por_mes_no_teste"] = [{"mes": i, "n": int(r["n"]), "mae": round(float(r["mae"]), 2)}
                             for i, r in erro_mes.iterrows()]
diz("")
diz("  MAE por mes dentro do teste (o gatilho de revalidacao):")
for r in R["mae_por_mes_no_teste"]:
    diz("    " + r["mes"] + format(r["n"], ",").rjust(8) + format(r["mae"], ".2f").rjust(9))

# ==================================================== 5 ETICA
secao("BLOCO 5 - QUEM CARREGA O RISCO QUANDO O SISTEMA FUNCIONA COMO DESENHADO")

REGIAO = {"AC": "N", "AP": "N", "AM": "N", "PA": "N", "RO": "N", "RR": "N", "TO": "N",
          "AL": "NE", "BA": "NE", "CE": "NE", "MA": "NE", "PB": "NE", "PE": "NE", "PI": "NE",
          "RN": "NE", "SE": "NE", "DF": "CO", "GO": "CO", "MT": "CO", "MS": "CO",
          "ES": "SE", "MG": "SE", "RJ": "SE", "SP": "SE", "PR": "S", "RS": "S", "SC": "S"}
teste["regiao_destino"] = teste["dest_state"].map(REGIAO).fillna("?")


def resumo(df):
    return {"n": int(len(df)),
            "frete_real_p50": round(float(df["freight_value"].median()), 2),
            "erro_pct_p50": round(float(df["erro_pct"].median()), 2),
            "pct_sobreprecificado": round(float((df["erro"] > 0).mean() * 100), 1),
            "medape": round(float(np.median(np.abs(df["erro_pct"].dropna()))), 1)}


por_regiao = {k: resumo(g) for k, g in teste.groupby("regiao_destino") if len(g) >= 100}
diz("  por regiao de destino:")
diz("    regiao      n  frete_p50  erro%_p50  %sobrepreco  MedAPE")
for k, v in sorted(por_regiao.items(), key=lambda kv: -kv[1]["erro_pct_p50"]):
    diz("    " + k.ljust(6) + format(v["n"], ",").rjust(7) + format(v["frete_real_p50"], ".2f").rjust(11)
        + format(v["erro_pct_p50"], "+.2f").rjust(11) + format(v["pct_sobreprecificado"], ".1f").rjust(12)
        + "%" + format(v["medape"], ".1f").rjust(8) + "%")
R["etica_por_regiao"] = por_regiao

por_perfil = {k: resumo(g) for k, g in teste.groupby("geo_profile") if len(g) >= 100}
diz("")
diz("  por perfil capital/interior (origem_destino):")
for k, v in sorted(por_perfil.items(), key=lambda kv: -kv[1]["erro_pct_p50"]):
    diz("    " + k.ljust(20) + format(v["n"], ",").rjust(7) + format(v["frete_real_p50"], ".2f").rjust(11)
        + format(v["erro_pct_p50"], "+.2f").rjust(11) + format(v["pct_sobreprecificado"], ".1f").rjust(12) + "%")
R["etica_por_perfil"] = por_perfil

teste["decil_km"] = pd.qcut(teste["distance_km"], 10, labels=False, duplicates="drop") + 1
por_km = {int(k): resumo(g) for k, g in teste.groupby("decil_km")}
R["etica_por_decil_distancia"] = por_km
diz("")
diz("  por decil de distancia (1 = mais perto):")
for k, v in por_km.items():
    diz("    decil " + str(k).rjust(2) + format(v["n"], ",").rjust(8) + format(v["frete_real_p50"], ".2f").rjust(11)
        + format(v["erro_pct_p50"], "+.2f").rjust(11) + "%")

# vendedor pequeno x grande: quem e medido pelo dado que produz
vol = dados.groupby("seller_id").size()
teste["porte_vendedor"] = pd.cut(teste["seller_id"].map(vol), [0, 10, 100, 1000, 1e9],
                                 labels=["1-10", "11-100", "101-1000", ">1000"])
por_porte = {str(k): resumo(g) for k, g in teste.groupby("porte_vendedor", observed=True) if len(g) >= 100}
R["etica_por_porte_vendedor"] = por_porte
diz("")
diz("  por porte do vendedor (n de embarques na base):")
for k, v in por_porte.items():
    diz("    " + k.ljust(10) + format(v["n"], ",").rjust(7) + format(v["frete_real_p50"], ".2f").rjust(11)
        + format(v["erro_pct_p50"], "+.2f").rjust(11) + "%" + format(v["medape"], ".1f").rjust(9) + "%")

# ==================================================== 6 PORTAO DE DECISAO
secao("BLOCO 6 - O PORTAO: o que sai automatico e o que vai para humano")

detector = joblib.load(MODELOS / "structural_outlier.joblib")
fora = detector.predict(x_teste) == -1
teste["fora_dominio"] = fora
largura = np.where(p50 > 0, (p75 - p25) / p50, np.nan)
teste["largura_intervalo"] = largura

portao = {
    "regra_1_fora_do_dominio": {"n": int(fora.sum()), "pct": round(float(fora.mean() * 100), 2),
                                "mae_dentro": metricas(p50[~fora], real[~fora])["mae"],
                                "mae_fora": metricas(p50[fora], real[fora])["mae"],
                                "medape_dentro": metricas(p50[~fora], real[~fora])["medape"],
                                "medape_fora": metricas(p50[fora], real[fora])["medape"]},
}
diz("  IsolationForest (contaminacao 1%): " + str(portao["regra_1_fora_do_dominio"]["n"]) + " cotacoes ("
    + format(portao["regra_1_fora_do_dominio"]["pct"], ".2f") + "%)")
diz("    MAE dentro do dominio: R$ " + format(portao["regra_1_fora_do_dominio"]["mae_dentro"], ".2f")
    + "   |  fora: R$ " + format(portao["regra_1_fora_do_dominio"]["mae_fora"], ".2f"))
diz("    MedAPE dentro: " + format(portao["regra_1_fora_do_dominio"]["medape_dentro"], ".1f")
    + "%  |  fora: " + format(portao["regra_1_fora_do_dominio"]["medape_fora"], ".1f") + "%")

diz("")
diz("  Portao adicional por largura do intervalo (P75-P25)/P50:")
diz("    corte   %p/humano   MAE automatico   MedAPE automatico   MAE p/humano")
for corte in (0.6, 0.8, 1.0, 1.2):
    envia = fora | (largura > corte)
    auto = ~envia
    if auto.sum() < 100 or envia.sum() < 10:
        continue
    reg = {"corte": corte, "pct_humano": round(float(envia.mean() * 100), 1),
           "mae_auto": metricas(p50[auto], real[auto])["mae"],
           "medape_auto": metricas(p50[auto], real[auto])["medape"],
           "mae_humano": metricas(p50[envia], real[envia])["mae"]}
    portao["largura_" + str(corte)] = reg
    diz("    " + format(corte, ".1f").rjust(5) + format(reg["pct_humano"], ".1f").rjust(10) + "%"
        + format(reg["mae_auto"], ".2f").rjust(16) + format(reg["medape_auto"], ".1f").rjust(19) + "%"
        + format(reg["mae_humano"], ".2f").rjust(15))
R["portao"] = portao

R["metricas_producao_metadata"] = meta["metrics"]
SAIDA_JSON.write_text(json.dumps(R, indent=2, ensure_ascii=False), encoding="utf-8")
SAIDA_TXT.write_text("\n".join(linhas), encoding="utf-8")
print("")
print("parte 2 salva em " + str(SAIDA_JSON))
