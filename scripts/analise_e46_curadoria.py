# -*- coding: utf-8 -*-
"""E4.6 blocos 3.3 e 3.4 sobre a base CURADA em dados_curados/vN/.

Substitui o funil que scripts/analise_e46.py calculava sobre archive_olist/ cru.
A diferenca nao e de numeros, e de regime: aquele pipeline EXCLUIA linhas; a
curadoria v1 marca e poe em quarentena, e mantem as 99 441 linhas na tabela
principal. Por isso o "antes e depois" que o enunciado pede aqui nao e um funil
descendente: e o mesmo indicador calculado com e sem cada regra.

3.3 proveniencia -> fontes com sha256, transformacoes, saidas com sha256.
3.4 curadoria    -> efeito de cada regra sobre o KPI e vies do que sai.

Executar: python scripts/analise_e46_curadoria.py [--versao v1]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
SEP = ";"
ENC = "utf-8-sig"

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


ap = argparse.ArgumentParser()
ap.add_argument("--versao", default="v1")
args = ap.parse_args()

BASE = RAIZ / "dados_curados" / args.versao
man = json.loads((BASE / "MANIFEST.json").read_text(encoding="utf-8"))
df = pd.read_csv(BASE / "entregas_curado.csv", sep=SEP, encoding=ENC, low_memory=False)
quar = pd.read_csv(BASE / "quarentena.csv", sep=SEP, encoding=ENC, low_memory=False)

R["versao"] = args.versao
R["gerado_em_curadoria"] = man["gerado_em"]

# ============================================================ BLOCO 3.3
secao("BLOCO 3.3 - PROVENIENCIA: origem com hash, transformacao e saida com hash")

diz("Fontes de entrada (sha256 truncado em 12):")
diz("  " + "arquivo".ljust(42) + "linhas".rjust(10) + "  sha256")
fontes = []
for nome, meta in man["fontes"].items():
    diz("  " + nome.ljust(42) + format(meta["linhas"], ",").rjust(10) + "  " + meta["sha256"][:12])
    fontes.append({"arquivo": nome, "linhas": meta["linhas"], "sha256": meta["sha256"]})
R["fontes"] = fontes

diz()
diz("Saidas versionadas:")
diz("  " + "arquivo".ljust(42) + "linhas".rjust(10) + "colunas".rjust(9) + "  sha256")
saidas = []
for nome, meta in man["saidas"].items():
    diz("  " + nome.ljust(42) + format(meta["linhas"], ",").rjust(10)
        + str(meta["colunas"]).rjust(9) + "  " + meta["sha256"][:12])
    saidas.append({"arquivo": nome, "linhas": meta["linhas"], "colunas": meta["colunas"],
                   "sha256": meta["sha256"]})
R["saidas"] = saidas

diz()
diz("Parametros que definem esta versao (mudar um deles muda a v):")
for k, v in man["parametros"].items():
    diz("  " + str(k).ljust(26) + json.dumps(v, ensure_ascii=False))
R["parametros"] = man["parametros"]

diz()
diz("Janela dos dados: " + man["janela_dos_dados"]["inicio"] + " a " + man["janela_dos_dados"]["fim"])
diz("Gerado por: " + man["gerado_por"] + " em " + man["gerado_em"])
diz("Diagnostico que a motivou: " + man["diagnostico"])

# ============================================================ BLOCO 3.4
secao("BLOCO 3.4 - CURADORIA: o numero antes e depois de cada regra")

diz("Regime: nenhuma linha e removida da tabela principal.")
diz("  pedidos na entrada  " + format(man["contagens"]["pedidos_entrada"], ",").rjust(10))
diz("  pedidos na saida    " + format(man["contagens"]["pedidos_saida"], ",").rjust(10))
diz("  sem nenhuma flag    " + format(man["contagens"]["pedidos_sem_flag"], ",").rjust(10)
    + format(man["contagens"]["pedidos_sem_flag"] / man["contagens"]["pedidos_saida"] * 100, ".1f").rjust(8) + "%")
diz("  em quarentena       " + format(man["contagens"]["pedidos_quarentena"], ",").rjust(10)
    + format(man["contagens"]["pedidos_quarentena"] / man["contagens"]["pedidos_saida"] * 100, ".1f").rjust(8) + "%")
R["regime"] = {
    "pedidos_entrada": man["contagens"]["pedidos_entrada"],
    "pedidos_saida": man["contagens"]["pedidos_saida"],
    "pedidos_sem_flag": man["contagens"]["pedidos_sem_flag"],
    "pedidos_quarentena": man["contagens"]["pedidos_quarentena"],
    "linhas_removidas": man["contagens"]["pedidos_entrada"] - man["contagens"]["pedidos_saida"],
}

# --- o KPI antes e depois de cada regra de denominador -----------------------
diz()
diz("A regra que mais move o KPI e a escolha do denominador do SLA:")
ativos = df.loc[df["ativo"].astype(str).str.lower().isin(["true", "1", "sim"])]
mensuravel = ativos.loc[ativos["atraso_mensuravel"].astype(str).str.lower().isin(["true", "1", "sim"])]
rep = float(mensuravel["atrasado"].astype(str).str.lower().isin(["true", "1", "sim"]).mean())
hon = float(ativos["atraso_honesto"].astype(str).str.lower().isin(["true", "1", "sim"]).mean())
diz("  reportado (so quem tem data de entrega) " + format(rep * 100, ".2f").rjust(7) + "%  sobre "
    + format(len(mensuravel), ",") + " pedidos")
diz("  honesto   (todos os ativos)             " + format(hon * 100, ".2f").rjust(7) + "%  sobre "
    + format(len(ativos), ",") + " pedidos")
diz("  diferenca                               " + format((hon - rep) * 100, "+.2f").rjust(7) + " p.p.  = "
    + format(len(ativos) - len(mensuravel), ",") + " pedidos vencidos sem registro")
R["denominador_sla"] = {
    "reportado_pct": round(rep * 100, 4), "n_reportado": int(len(mensuravel)),
    "honesto_pct": round(hon * 100, 4), "n_honesto": int(len(ativos)),
    "delta_pp": round((hon - rep) * 100, 4),
    "vencidos_sem_registo": int(len(ativos) - len(mensuravel)),
}

# --- vies: o que a quarentena tira nao e igual ao que fica -------------------
diz()
diz("Vies da quarentena (o que sai nao e igual ao que fica):")
em_quar = df["order_id"].isin(set(quar["order_id"]))
diz("  " + "recorte".ljust(22) + "pedidos".rjust(9) + "frete med".rjust(12)
    + "mercad. med".rjust(13) + "peso med g".rjust(12))
vies = {}
for nome, sub in [("em quarentena", df.loc[em_quar]), ("fora da quarentena", df.loc[~em_quar])]:
    reg = {"pedidos": int(len(sub)),
           "frete_mediano": round(float(sub["valor_frete"].median()), 2),
           "mercadoria_mediana": round(float(sub["valor_mercadoria"].median()), 2),
           "peso_mediano_g": round(float(sub["peso_g"].median()), 1)}
    vies[nome] = reg
    diz("  " + nome.ljust(22) + format(reg["pedidos"], ",").rjust(9)
        + format(reg["frete_mediano"], ",.2f").rjust(12)
        + format(reg["mercadoria_mediana"], ",.2f").rjust(13)
        + format(reg["peso_mediano_g"], ",.1f").rjust(12))
R["vies_quarentena"] = vies

diz()
diz("Composicao da quarentena (a regra que a poe la):")
motivos = man["contagens"]
comp_q = {}
NAO_FLAG = {"qa_flags_total", "qa_quarentena"}
for col in [c for c in quar.columns if c.startswith("qa_") and c not in NAO_FLAG]:
    n = int(quar[col].astype(str).str.lower().isin(["true", "1", "sim"]).sum())
    if n:
        comp_q[col.removeprefix("qa_")] = n
for k, v in sorted(comp_q.items(), key=lambda kv: -kv[1]):
    diz("  " + k.ljust(26) + format(v, ",").rjust(8))
R["composicao_quarentena"] = comp_q

# --- efeito de cada flag sobre o KPI ----------------------------------------
diz()
diz("Efeito de cada flag sobre a taxa de atraso honesta (antes e depois):")
diz("  " + "flag".ljust(26) + "pedidos".rjust(9) + "atraso c/".rjust(11)
    + "atraso s/".rjust(11) + "delta p.p.".rjust(12) + "  leitura")
atraso = ativos["atraso_honesto"].astype(str).str.lower().isin(["true", "1", "sim"])
efeito = []
for col in [c for c in ativos.columns if c.startswith("qa_") and c not in NAO_FLAG]:
    marc = ativos[col].astype(str).str.lower().isin(["true", "1", "sim"])
    n = int(marc.sum())
    if n < 20:
        continue
    com = float(atraso.loc[marc].mean())
    sem = float(atraso.loc[~marc].mean())
    taut = com >= 0.95
    reg = {"flag": col.removeprefix("qa_"), "pedidos": n,
           "atraso_com_pct": round(com * 100, 2), "atraso_sem_pct": round(sem * 100, 2),
           "delta_pp": round((com - sem) * 100, 2), "tautologica": taut,
           "protege": com <= 0.05}
    efeito.append(reg)
efeito.sort(key=lambda r: -abs(r["delta_pp"]))
for r in efeito:
    diz("  " + r["flag"].ljust(26) + format(r["pedidos"], ",").rjust(9)
        + format(r["atraso_com_pct"], ".1f").rjust(10) + "%"
        + format(r["atraso_sem_pct"], ".1f").rjust(10) + "%"
        + format(r["delta_pp"], "+.1f").rjust(12)
        + ("  tautologica: a flag E a ausencia do registro"
           if r["tautologica"] else
           "  a flag apanha pedido bom: excluir baixaria o KPI" if r["protege"] else ""))
R["efeito_flags"] = efeito

# --- as regras e os donos ---------------------------------------------------
diz()
diz("As " + str(len(man["regras"])) + " regras aplicadas (cada uma e uma escolha, e esta escrita):")
for i, regra in enumerate(man["regras"], 1):
    diz("  " + str(i).rjust(2) + ". " + regra)
R["regras"] = man["regras"]

diz()
diz("Curadoria geografica e de nomes (marcada, nunca corrigida em silencio):")
geo_cid = {
    "geolocation_duplicados_removidos": man["contagens"]["geolocation_duplicados_removidos"],
    "geolocation_pontos_fora_br": man["contagens"]["geolocation_pontos_fora_br"],
    "cidades_grafias_entrada": man["contagens"]["cidades_grafias_entrada"],
    "cidades_canonicas_saida": man["contagens"]["cidades_canonicas_saida"],
    "cidades_regrafias_estruturais": man["contagens"]["cidades_regrafias_estruturais"],
    "cidades_pares_suspeitos": man["contagens"]["cidades_pares_suspeitos"],
    "vendedores_uf_incoerente": man["contagens"]["vendedores_uf_incoerente"],
    "pedidos_uf_filial_incoerente": man["contagens"]["pedidos_uf_filial_incoerente"],
}
for k, v in geo_cid.items():
    diz("  " + k.ljust(36) + format(v, ",").rjust(10))
R["geo_cidades"] = geo_cid

SAIDA_JSON = RAIZ / "docs" / ("E4-6_curadoria_" + args.versao + ".json")
SAIDA_TXT = RAIZ / "docs" / ("E4-6_curadoria_" + args.versao + ".txt")
SAIDA_JSON.write_text(json.dumps(R, indent=2, ensure_ascii=False), encoding="utf-8")
SAIDA_TXT.write_text("\n".join(linhas) + "\n", encoding="utf-8")
diz()
diz("salvo em " + str(SAIDA_JSON))
