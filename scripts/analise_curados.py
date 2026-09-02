# -*- coding: utf-8 -*-
"""Analise dos dados curados em dados_curados/vN/.

Le a saida de scripts/curar_dados.py e responde: onde esta o atraso, quanto custa
o frete por rota, que filiais sustentam o volume e que parte do diagnostico muda
quando se usa o denominador honesto em vez do reportado.

PRINCIPIO: nenhum numero aqui esconde o seu denominador. Onde uma flag de qualidade
altera a conclusao, os dois recortes aparecem lado a lado.

Executar: python scripts/analise_curados.py [--versao v1]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
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


def sub(titulo):
    diz()
    diz("-" * 78)
    diz(titulo)
    diz("-" * 78)


def pct(x):
    return "-" if pd.isna(x) else "{:.2f}%".format(x * 100)


def num(x):
    return "-" if pd.isna(x) else "{:,.0f}".format(x)


def rs(x):
    return "-" if pd.isna(x) else "R$ {:,.2f}".format(x)


def dias(x):
    return "-" if pd.isna(x) else "{:.1f}d".format(x)


def vezes(x):
    return "-" if pd.isna(x) else "{:.2f}x".format(x)


# --------------------------------------------------------------------------- 0
ap = argparse.ArgumentParser()
ap.add_argument("--versao", default="v1")
args = ap.parse_args()

BASE = RAIZ / "dados_curados" / args.versao
SAIDA_TXT = RAIZ / "docs" / "analise_curados_{}.txt".format(args.versao)
SAIDA_JSON = RAIZ / "docs" / "analise_curados_{}.json".format(args.versao)

manifest = json.loads((BASE / "MANIFEST.json").read_text(encoding="utf-8"))

DATAS = ["dt_compra", "dt_aprovacao", "dt_coleta", "dt_entrega", "dt_prazo",
         "dt_limite_expedicao"]
e = pd.read_csv(BASE / "entregas_curado.csv", sep=SEP, encoding=ENC,
                parse_dates=DATAS, low_memory=False)
geo = pd.read_csv(BASE / "geolocation_cep_curado.csv", sep=SEP, encoding=ENC)
quar = pd.read_csv(BASE / "quarentena.csv", sep=SEP, encoding=ENC, low_memory=False)
mapa_cid = pd.read_csv(BASE / "cidades_mapa.csv", sep=SEP, encoding=ENC)

QA = [c for c in e.columns if c.startswith("qa_")
      and c not in ("qa_flags_total", "qa_quarentena")]
for c in ["ativo", "atraso_mensuravel", "vencido_sem_registo", "qa_quarentena",
          "atrasado", "atraso_honesto"] + QA:
    e[c] = e[c].astype("boolean")

secao("ANALISE DOS DADOS CURADOS - {}  (curadoria de {})".format(
    args.versao, manifest["gerado_em"]))
diz("  fonte     dados_curados/{}/entregas_curado.csv".format(args.versao))
diz("  janela    {} a {}".format(manifest["janela_dos_dados"]["inicio"],
                                 manifest["janela_dos_dados"]["fim"]))
diz("  pedidos   {:,} linhas x {} colunas".format(len(e), e.shape[1]))
diz("  limpos    {:,} pedidos sem nenhuma flag ({:.1f}%)".format(
    int(e.qa_flags_total.eq(0).sum()), e.qa_flags_total.eq(0).mean() * 100))
R["versao"] = args.versao
R["gerado_em_curadoria"] = manifest["gerado_em"]
R["pedidos"] = int(len(e))

# --------------------------------------------------------------------------- 1
secao("1) O QUE SOBROU PARA ANALISAR - os recortes e o que cada um custa")

ativos = e[e.ativo.fillna(False)]
limpos = e[e.qa_flags_total.eq(0)]
mensur = ativos[ativos.atraso_mensuravel.fillna(False)]
analitico = ativos[~ativos.qa_quarentena.fillna(False)
                   & ~ativos.qa_cauda_incompleta.fillna(False)]

recortes = [
    ("todos os pedidos", len(e)),
    ("ativos (exclui cancelado)", len(ativos)),
    ("ativos fora da quarentena e da cauda", len(analitico)),
    ("com data de entrega (mensuravel)", len(mensur)),
    ("sem nenhuma flag de qualidade", len(limpos)),
]
diz()
for nome, n in recortes:
    diz("  {:<38} {:>9}  {:>8}".format(nome, num(n), pct(n / len(e))))
R["recortes"] = {nome: int(n) for nome, n in recortes}

diz()
diz("  O recorte 'analitico' (ativos, sem quarentena, sem cauda) e o que usamos abaixo")
diz("  para medias e comparacoes. A cauda sai porque o registro ainda chegava;")
diz("  a quarentena sai porque a linha viola a fisica do processo, nao a estatistica.")

status = e.order_status.value_counts()
sub("Status dos pedidos")
for k, v in status.items():
    diz("  {:<16} {:>8,}  {:>6.2f}%".format(k, v, v / len(e) * 100))
R["status"] = {k: int(v) for k, v in status.items()}

# --------------------------------------------------------------------------- 2
secao("2) SLA - os dois denominadores e para onde a diferenca aponta")

sla_rep = mensur.atrasado.mean()
sla_hon = ativos.atraso_honesto.mean()
vencidos = int(ativos.vencido_sem_registo.sum())
diz()
diz("  reportado  (so quem tem data de entrega)  {:>8}  sobre {:,} pedidos".format(
    pct(sla_rep), len(mensur)))
diz("  honesto    (todos os ativos)              {:>8}  sobre {:,} pedidos".format(
    pct(sla_hon), len(ativos)))
diz("  diferenca  {:+.2f} p.p. - {:,} pedidos com prazo vencido e sem registro".format(
    (sla_hon - sla_rep) * 100, vencidos))
diz()
diz("  A leitura: 1 em cada 10 pedidos ativos chegou atrasado ou nunca foi dado por")
diz("  chegado. O KPI reportado nao mente por calculo, mente por ausencia: quem nao")
diz("  tem data sai do denominador exatamente por ter corrido mal.")
R["sla"] = {"reportado": float(sla_rep), "honesto": float(sla_hon),
            "vencidos_sem_registo": vencidos,
            "delta_pp": float((sla_hon - sla_rep) * 100)}

sub("Onde estao os pedidos vencidos sem registro (top 10 filiais)")
vsr = (ativos[ativos.vencido_sem_registo.fillna(False)]
       .groupby("filial").order_id.count().sort_values(ascending=False).head(10))
base_f = ativos.groupby("filial").order_id.count()
diz()
diz("  " + "filial".ljust(12) + "vencidos".rjust(10) + "ativos".rjust(10) + "taxa".rjust(10))
for f, v in vsr.items():
    diz("  {:<12}{:>10,}{:>10,}{:>9.2f}%".format(str(f), v, base_f[f], v / base_f[f] * 100))
R["vencidos_por_filial"] = {str(k): int(v) for k, v in vsr.items()}

sub("Atraso por mes de compra - o KPI honesto ao longo da janela")
mes = ativos.assign(m=ativos.dt_compra.dt.to_period("M").astype(str))
serie = mes.groupby("m").agg(pedidos=("order_id", "count"),
                             honesto=("atraso_honesto", "mean"),
                             reportado=("atrasado", "mean"),
                             cauda=("qa_cauda_incompleta", "mean"))
serie = serie[serie.pedidos >= 50]
diz()
diz("  " + "mes".ljust(9) + "pedidos".rjust(9) + "reportado".rjust(11)
    + "honesto".rjust(10) + "  na cauda")
for m, l in serie.iterrows():
    marca = "  <- registro incompleto" if l.cauda > 0.5 else ""
    diz("  {:<9}{:>9,.0f}{:>10.2f}%{:>9.2f}%{}".format(
        m, l.pedidos, l.reportado * 100, l.honesto * 100, marca))
R["sla_por_mes"] = {m: {"pedidos": int(l.pedidos), "reportado": float(l.reportado),
                        "honesto": float(l.honesto)} for m, l in serie.iterrows()}

# --------------------------------------------------------------------------- 3
secao("3) LEAD TIME - distribuicao e folga contra o prazo prometido")

lt = analitico.loc[analitico.lead_dias.notna(), "lead_dias"]
qs = lt.quantile([.05, .25, .5, .75, .9, .95, .99])
diz()
diz("  n={:,}  media={:.1f}d  desvio={:.1f}d".format(len(lt), lt.mean(), lt.std()))
diz("  " + "  ".join("p{}={:.1f}d".format(int(q * 100), v) for q, v in qs.items()))
diz()
diz("  A mediana ({:.1f}d) fica bem abaixo da media ({:.1f}d): a cauda direita e que".format(
    lt.median(), lt.mean()))
diz("  faz o KPI. Metade dos pedidos nao e o problema.")
R["lead_dias"] = {"n": int(len(lt)), "media": float(lt.mean()), "desvio": float(lt.std()),
                  **{"p{}".format(int(q * 100)): float(v) for q, v in qs.items()}}

folga = analitico.loc[analitico.dias_vs_prazo.notna(), "dias_vs_prazo"]
sub("Folga contra o prazo (dias_vs_prazo: negativo = chegou antes)")
qf = folga.quantile([.05, .25, .5, .75, .9, .95, .99])
diz()
diz("  n={:,}  mediana={:.1f}d  media={:.1f}d".format(len(folga), folga.median(), folga.mean()))
diz("  " + "  ".join("p{}={:+.1f}d".format(int(q * 100), v) for q, v in qf.items()))
diz()
diz("  O prazo prometido e folgado por construcao: a mediana chega {:.0f} dias antes.".format(
    abs(folga.median())))
diz("  Bater o prazo nao e o mesmo que ser rapido - o prazo tem gordura embutida.")
R["dias_vs_prazo"] = {"mediana": float(folga.median()), "media": float(folga.mean()),
                      **{"p{}".format(int(q * 100)): float(v) for q, v in qf.items()}}

sub("Decomposicao do lead time em etapas (dias)")
et = analitico.copy()
et["aprovacao"] = (et.dt_aprovacao - et.dt_compra).dt.total_seconds() / 86400
et["expedicao"] = (et.dt_coleta - et.dt_aprovacao).dt.total_seconds() / 86400
et["transporte"] = (et.dt_entrega - et.dt_coleta).dt.total_seconds() / 86400
etapas = [("compra -> aprovacao", "aprovacao"), ("aprovacao -> coleta", "expedicao"),
          ("coleta -> entrega", "transporte")]
tot_med = sum(et[c].median() for _, c in etapas)
diz()
diz("  " + "etapa".ljust(24) + "mediana".rjust(9) + "media".rjust(9)
    + "p90".rjust(9) + "p99".rjust(9) + "  share mediana")
for nome, c in etapas:
    s = et.loc[et[c].notna(), c]
    diz("  {:<24}{:>8.2f}d{:>8.2f}d{:>8.2f}d{:>8.2f}d{:>13.0f}%".format(
        nome, s.median(), s.mean(), s.quantile(.9), s.quantile(.99),
        s.median() / tot_med * 100))
diz()
diz("  O transporte domina ({:.0f}% da mediana), mas a etapa mais volatil e a".format(
    et.transporte.median() / tot_med * 100))
diz("  expedicao: p99 de {:.1f}d contra mediana de {:.2f}d.".format(
    et.expedicao.quantile(.99), et.expedicao.median()))
R["etapas"] = {nome: {"mediana": float(et[c].median()), "media": float(et[c].mean()),
                      "p90": float(et[c].quantile(.9)), "p99": float(et[c].quantile(.99))}
               for nome, c in etapas}

# --------------------------------------------------------------------------- 4
secao("4) FILIAIS (UF do vendedor) - volume, atraso e a filial que nao existe")

g = analitico.groupby("filial").agg(
    pedidos=("order_id", "count"),
    atraso=("atraso_honesto", "mean"),
    lead=("lead_dias", "median"),
    frete=("valor_frete", "median"),
    mercadoria=("valor_mercadoria", "median"),
).sort_values("pedidos", ascending=False)
g["share"] = g.pedidos / g.pedidos.sum()
g["frete_sobre_merc"] = g.frete / g.mercadoria

diz()
diz("  " + "filial".ljust(11) + "pedidos".rjust(9) + "share".rjust(8)
    + "atraso".rjust(9) + "lead med".rjust(10) + "frete med".rjust(11)
    + "frete/merc".rjust(11))
for f, l in g.head(15).iterrows():
    diz("  {:<11}{:>9,.0f}{:>7.1f}%{:>8.2f}%{:>10}{:>11}{:>11}".format(
        str(f), l.pedidos, l.share * 100, l.atraso * 100, dias(l.lead), rs(l.frete),
        vezes(l.frete_sobre_merc)))
diz()
top3 = g.head(3)
diz("  Concentracao: {} respondem por {:.1f}% dos pedidos.".format(
    ", ".join(map(str, top3.index)), top3.share.sum() * 100))
# SEM_FILIAL fica fora da comparacao: sem vendedor nao ha data de entrega, logo o
# atraso e 100% por definicao. Compara-la com uma filial real seria comparar um
# buraco de registro com um processo.
reais = g[(g.pedidos >= 300) & (g.index != "SEM_FILIAL")].sort_values(
    "atraso", ascending=False)
diz("  Entre filiais reais com >=300 pedidos: {} ({:.2f}%) contra {} ({:.2f}%)".format(
    reais.index[0], reais.atraso.iloc[0] * 100,
    reais.index[-1], reais.atraso.iloc[-1] * 100))
diz("  - fator {:.1f}x entre a pior e a melhor.".format(
    reais.atraso.iloc[0] / reais.atraso.iloc[-1]))
if "SEM_FILIAL" in g.index:
    sf = g.loc["SEM_FILIAL"]
    diz("  SEM_FILIAL: {:,.0f} pedidos sem vendedor, atraso 100% por definicao (sem".format(
        sf.pedidos))
    diz("  vendedor nao ha coleta nem entrega registada). Num GROUP BY ingenuo")
    diz("  desapareciam do relatorio; aqui aparecem como o que sao - um buraco de registro.")
R["filiais"] = {str(f): {"pedidos": int(l.pedidos), "share": float(l.share),
                         "atraso_honesto": float(l.atraso),
                         "lead_mediana": float(l.lead) if pd.notna(l.lead) else None}
                for f, l in g.iterrows()}

sub("Maturidade do vendedor (origem_registo) contra desempenho")
mat = analitico.groupby("origem_registo").agg(
    pedidos=("order_id", "count"), atraso=("atraso_honesto", "mean"),
    lead=("lead_dias", "median"), frete=("valor_frete", "median"),
).sort_values("pedidos", ascending=False)
diz()
diz("  " + "faixa".ljust(16) + "pedidos".rjust(9) + "atraso".rjust(9)
    + "lead med".rjust(10) + "frete med".rjust(11))
for f, l in mat.iterrows():
    diz("  {:<16}{:>9,.0f}{:>8.2f}%{:>10}{:>11}".format(
        str(f), l.pedidos, l.atraso * 100, dias(l.lead), rs(l.frete)))
diz()
diz("  Vendedores de 1-10 pedidos atrasam {:.2f}% contra {:.2f}% dos de >1000: a".format(
    mat.loc["1-10 pedidos", "atraso"] * 100, mat.loc[">1000", "atraso"] * 100))
diz("  cauda de vendedores pequenos e pior, mas responde por so {:.1f}% do volume.".format(
    mat.loc["1-10 pedidos", "pedidos"] / mat.pedidos.sum() * 100))
R["maturidade"] = {str(f): {"pedidos": int(l.pedidos), "atraso": float(l.atraso)}
                   for f, l in mat.iterrows()}

# --------------------------------------------------------------------------- 5
secao("5) DESTINO - a geografia do atraso")

d = analitico.groupby("cliente_uf").agg(
    pedidos=("order_id", "count"), atraso=("atraso_honesto", "mean"),
    lead=("lead_dias", "median"), frete=("valor_frete", "median"),
    merc=("valor_mercadoria", "median"),
).sort_values("pedidos", ascending=False)
d["frete_sobre_merc"] = d.frete / d.merc
diz()
diz("  Top 12 destinos por volume")
diz("  " + "UF".ljust(6) + "pedidos".rjust(9) + "atraso".rjust(9)
    + "lead med".rjust(10) + "frete med".rjust(11) + "frete/merc".rjust(11))
for f, l in d.head(12).iterrows():
    diz("  {:<6}{:>9,.0f}{:>8.2f}%{:>9.1f}d{:>11}{:>10.2f}x".format(
        str(f), l.pedidos, l.atraso * 100, l.lead, rs(l.frete), l.frete_sobre_merc))

piores = d[d.pedidos >= 200].sort_values("atraso", ascending=False).head(8)
diz()
diz("  Piores destinos por atraso (>=200 pedidos)")
diz("  " + "UF".ljust(6) + "pedidos".rjust(9) + "atraso".rjust(9)
    + "lead med".rjust(10) + "frete/merc".rjust(11))
for f, l in piores.iterrows():
    diz("  {:<6}{:>9,.0f}{:>8.2f}%{:>9.1f}d{:>10.2f}x".format(
        str(f), l.pedidos, l.atraso * 100, l.lead, l.frete_sobre_merc))
diz()
diz("  O padrao e regional, nao aleatorio: Norte e Nordeste concentram lead alto,")
diz("  atraso alto e frete que pesa mais sobre o valor da mercadoria.")
R["destinos"] = {str(f): {"pedidos": int(l.pedidos), "atraso": float(l.atraso),
                          "lead_mediana": float(l.lead) if pd.notna(l.lead) else None,
                          "frete_sobre_mercadoria": float(l.frete_sobre_merc)}
                 for f, l in d.iterrows()}

sub("Rota: dentro da UF contra fora da UF")
rot = analitico.assign(interna=analitico.filial.eq(analitico.cliente_uf)).groupby(
    "interna").agg(pedidos=("order_id", "count"), atraso=("atraso_honesto", "mean"),
                   lead=("lead_dias", "median"), frete=("valor_frete", "median"))
diz()
for k, l in rot.iterrows():
    nome = "mesma UF" if k else "UF diferente"
    diz("  {:<14}{:>9,.0f} pedidos   atraso {:>5.2f}%   lead {:>4.1f}d   frete {}".format(
        nome, l.pedidos, l.atraso * 100, l.lead, rs(l.frete)))
R["rota_interna"] = {("mesma_uf" if k else "outra_uf"):
                     {"pedidos": int(l.pedidos), "atraso": float(l.atraso),
                      "lead_mediana": float(l.lead)} for k, l in rot.iterrows()}

# --------------------------------------------------------------------------- 6
secao("6) DINHEIRO - frete, mercadoria e canal de pagamento")

fin = analitico
diz()
diz("  mercadoria  total {}   mediana {}".format(
    rs(fin.valor_mercadoria.sum()), rs(fin.valor_mercadoria.median())))
diz("  frete       total {}   mediana {}".format(
    rs(fin.valor_frete.sum()), rs(fin.valor_frete.median())))
diz("  no agregado, o frete e {:.1f}% do valor da mercadoria".format(
    fin.valor_frete.sum() / fin.valor_mercadoria.sum() * 100))
R["financeiro"] = {"mercadoria_total": float(fin.valor_mercadoria.sum()),
                   "frete_total": float(fin.valor_frete.sum()),
                   "frete_share": float(fin.valor_frete.sum() / fin.valor_mercadoria.sum())}

sub("Frete por faixa de peso (mediana)")
faixas = pd.cut(fin.peso_g, [0, 300, 750, 2000, 5000, 15000, np.inf],
                labels=["<=300g", "301-750g", "751g-2kg", "2-5kg", "5-15kg", ">15kg"])
fp = fin.assign(faixa=faixas).groupby("faixa", observed=True).agg(
    pedidos=("order_id", "count"), frete=("valor_frete", "median"),
    merc=("valor_mercadoria", "median"), lead=("lead_dias", "median"),
    atraso=("atraso_honesto", "mean"))
diz()
diz("  " + "faixa".ljust(11) + "pedidos".rjust(9) + "frete med".rjust(11)
    + "merc med".rjust(11) + "frete/merc".rjust(11) + "lead".rjust(8) + "atraso".rjust(9))
for f, l in fp.iterrows():
    diz("  {:<11}{:>9,.0f}{:>11}{:>11}{:>10.2f}x{:>7.1f}d{:>8.2f}%".format(
        str(f), l.pedidos, rs(l.frete), rs(l.merc), l.frete / l.merc, l.lead,
        l.atraso * 100))
R["frete_por_peso"] = {str(f): {"pedidos": int(l.pedidos),
                                "frete_mediana": float(l.frete),
                                "atraso": float(l.atraso)} for f, l in fp.iterrows()}

sub("Canal de pagamento")
cp = fin.groupby("canal_pagamento").agg(
    pedidos=("order_id", "count"), ticket=("valor_pago", "median"),
    atraso=("atraso_honesto", "mean"), lead=("lead_dias", "median"),
).sort_values("pedidos", ascending=False)
diz()
diz("  " + "canal".ljust(14) + "pedidos".rjust(9) + "ticket med".rjust(12)
    + "atraso".rjust(9) + "lead med".rjust(10))
for f, l in cp.iterrows():
    diz("  {:<14}{:>9,.0f}{:>12}{:>8.2f}%{:>9.1f}d".format(
        str(f), l.pedidos, rs(l.ticket), l.atraso * 100, l.lead))
lag_ap = fin.assign(lag=(fin.dt_aprovacao - fin.dt_compra).dt.total_seconds() / 3600)
diz()
diz("  Latencia de aprovacao por canal (horas, mediana) - onde o boleto cobra o seu preco")
for f, v in lag_ap.groupby("canal_pagamento").lag.median().sort_values(
        ascending=False).items():
    diz("    {:<14}{:>8.1f} h".format(str(f), v))
R["pagamento"] = {str(f): {"pedidos": int(l.pedidos), "ticket_mediana": float(l.ticket),
                           "atraso": float(l.atraso)} for f, l in cp.iterrows()}

# --------------------------------------------------------------------------- 7
secao("7) AS FLAGS IMPORTAM? - o efeito de cada defeito no KPI")

diz()
diz("  Para cada flag: o atraso honesto dentro dela contra o atraso de quem nao a tem.")
diz("  Se as duas colunas coincidem, a flag e ruido para este KPI; se divergem, e sinal.")

# Flags que ALIMENTAM a definicao de atraso_honesto: sem data de entrega, o pedido
# com prazo vencido conta como atraso. O delta enorme delas e tautologia, nao achado
# - por isso ficam numa tabela separada, para nao passarem por descoberta.
DEFINICIONAIS = {"sem_dt_entrega", "sem_dt_coleta", "sem_item", "sem_peso",
                 "lead_implausivel"}

efeito = []
for c in QA:
    dentro = ativos[ativos[c].fillna(False)]
    fora = ativos[~ativos[c].fillna(False)]
    if len(dentro) < 20:
        continue
    a_d, a_f = dentro.atraso_honesto.mean(), fora.atraso_honesto.mean()
    efeito.append((c.replace("qa_", ""), len(dentro), a_d, a_f, a_d - a_f))
efeito.sort(key=lambda t: -abs(t[4]))

cab = ("  " + "flag".ljust(26) + "pedidos".rjust(8) + "atraso c/".rjust(11)
       + "atraso s/".rjust(11) + "delta".rjust(11))


def linha_flag(t):
    nome, n, a_d, a_f, dl = t
    diz("  {:<26}{:>8,}{:>10.2f}%{:>10.2f}%{:>+9.2f}pp".format(
        nome, n, a_d * 100, a_f * 100, dl * 100))


diz()
diz("  a) Tautologicas - a flag entra na propria definicao de atraso_honesto")
diz(cab)
for t in efeito:
    if t[0] in DEFINICIONAIS:
        linha_flag(t)
diz()
diz("     Estas nao sao achado nenhum: sem data de entrega e com prazo vencido, o")
diz("     pedido conta como atraso por regra. Estao aqui para mostrar o tamanho do")
diz("     buraco de registro, nao para explicar o KPI.")

diz()
diz("  b) Informativas - a flag e independente da definicao do KPI")
diz(cab)
for t in efeito:
    if t[0] not in DEFINICIONAIS:
        linha_flag(t)
R["efeito_flags"] = [{"flag": n, "pedidos": int(k), "atraso_com": float(a),
                      "atraso_sem": float(b), "delta_pp": float(dl * 100),
                      "tautologica": n in DEFINICIONAIS}
                     for n, k, a, b, dl in efeito]

diz()
diz("  Na tabela informativa o sinal e quase todo NEGATIVO: pedidos com defeito de")
diz("  registro (lag de despacho negativo, frete zero, pagamento divergente,")
diz("  quase-duplicado) atrasam MENOS do que a media. Sao pedidos que chegaram e")
diz("  cujo defeito e de lancamento, nao de operacao.")
diz("  As excecoes, essas sim uteis: cep_sem_geo ({:+.2f}pp) e frete_desproporcional".format(
    [t[4] for t in efeito if t[0] == "cep_sem_geo"][0] * 100))
diz("  ({:+.2f}pp) apontam para destinos que o cadastro nao cobre e o frete ja denunciava.".format(
    [t[4] for t in efeito if t[0] == "frete_desproporcional"][0] * 100))

sub("Quarentena - as linhas que violam a fisica do processo")
mot = quar.motivo_quarentena.value_counts()
diz()
diz("  {:,} linhas em quarentena ({:.2f}% dos pedidos)".format(
    len(quar), len(quar) / len(e) * 100))
for k, v in mot.items():
    diz("  {:<28}{:>7,}  {:>6.2f}%".format(k, v, v / len(quar) * 100))
diz()
diz("  Nenhuma foi apagada: a quarentena e um extrato para alguem corrigir na origem.")
R["quarentena"] = {k: int(v) for k, v in mot.items()}

# --------------------------------------------------------------------------- 8
secao("8) GEOLOCATION CURADO - o que a deduplicacao mudou")

cont = manifest["contagens"]
diz()
diz("  prefixos de CEP com centroide: {:,}".format(len(geo)))
diz("  duplicados exatos removidos:   {:,} de {:,} ({:.1f}%)".format(
    cont["geolocation_duplicados_removidos"], cont["geolocation_linhas_brutas"],
    cont["geolocation_duplicados_removidos"] / cont["geolocation_linhas_brutas"] * 100))
corr = geo.correcao_centroide_m
diz("  correcao do centroide (m):     mediana {:.1f} | p90 {:.1f} | p99 {:.1f} | max {:,.0f}".format(
    corr.median(), corr.quantile(.9), corr.quantile(.99), corr.max()))
graves = geo[corr > 1000]
diz("  prefixos onde a dedup moveu o centroide >1 km: {:,} ({:.2f}%)".format(
    len(graves), len(graves) / len(geo) * 100))
diz()
diz("  A mediana de {:.0f} m e irrelevante para roteirizacao; a cauda nao e. Os prefixos".format(
    corr.median()))
diz("  com correcao quilometrica eram os que puxavam distancias erradas para o modelo.")
diz()
diz("  Piores 8 prefixos por correcao")
diz("  " + "CEP".ljust(7) + "cidade".ljust(26) + "UF".rjust(4)
    + "pontos".rjust(8) + "dups".rjust(7) + "correcao".rjust(13))
for _, l in geo.nlargest(8, "correcao_centroide_m").iterrows():
    diz("  {:<7}{:<26}{:>4}{:>8,.0f}{:>7,.0f}{:>12,.0f}m".format(
        str(l.cep_prefixo), str(l.cidade_canonica)[:25], str(l.uf), l.n_pontos,
        l.duplicados_removidos, l.correcao_centroide_m))
R["geolocation"] = {"prefixos": int(len(geo)),
                    "correcao_mediana_m": float(corr.median()),
                    "correcao_p99_m": float(corr.quantile(.99)),
                    "prefixos_acima_1km": int(len(graves))}

sub("Cobertura: pedidos cujo CEP nao tem geolocation")
sem_geo = int(e.qa_cep_sem_geo.fillna(False).sum())
diz()
diz("  {:,} pedidos ({:.2f}%) - sem centroide nao ha distancia,".format(
    sem_geo, sem_geo / len(e) * 100))
diz("  e sem distancia o modelo de frete perde a sua variavel mais forte.")
R["cep_sem_geo"] = sem_geo

# --------------------------------------------------------------------------- 9
secao("9) CONSISTENCIA DE CIDADE - o que o mapa canonico resolveu")

estrut = mapa_cid[mapa_cid.mudanca.eq("estrutural")]
diz()
diz("  grafias de entrada: {:,} -> canonicas: {:,}".format(
    cont["cidades_grafias_entrada"], cont["cidades_canonicas_saida"]))
diz("  regrafias estruturais (alias, sufixo de UF, espacos): {:,} grafias, {:,} linhas".format(
    len(estrut), estrut.linhas.sum()))
diz("  pedidos com cidade regrafada: {:,}".format(
    int(e.qa_cidade_regrafada.fillna(False).sum())))
diz()
diz("  Por regra aplicada")
for k, v in estrut.regra_aplicada.value_counts().items():
    diz("    {:<16}{:>5} grafias".format(str(k), v))
diz()
diz("  {} pares suspeitos (erro de digitacao) ficaram fora: unir 'Sao Pauo' a 'Sao Paulo'".format(
    cont["cidades_pares_suspeitos"]))
diz("  por distancia de edicao e adivinhar. Vao para revisao humana, nao para o pipeline.")
R["cidades"] = {"grafias_entrada": cont["cidades_grafias_entrada"],
                "canonicas": cont["cidades_canonicas_saida"],
                "regrafias_estruturais": int(len(estrut)),
                "pares_suspeitos": cont["cidades_pares_suspeitos"]}

# --------------------------------------------------------------------------- 10
secao("10) O QUE ISTO SUSTENTA - e o que ainda nao sustenta")

diz()
diz("  Sustenta:")
diz("    - KPI de atraso com denominador defensavel ({} e nao {}).".format(
    pct(sla_hon), pct(sla_rep)))
diz("    - Comparacao entre filiais e entre destinos, porque a cidade tem grafia unica")
diz("      e a UF do vendedor foi conferida contra o CEP.")
diz("    - Distancia origem-destino, porque o centroide de CEP deixou de ser puxado por")
diz("      {:,} linhas duplicadas.".format(cont["geolocation_duplicados_removidos"]))
diz("    - Decomposicao do lead time por etapa, com a expedicao isolada como etapa volatil.")
diz()
diz("  Nao sustenta ainda:")
diz("    - {:,} pedidos com quase-duplicado em order_items: duas unidades ou lancamento".format(
    int(e.qa_quase_duplicado.fillna(False).sum())))
diz("      repetido? Sem essa resposta, receita por pedido tem margem de erro conhecida")
diz("      e nao corrigida.")
diz("    - {:,} pedidos onde a UF declarada do vendedor discorda do CEP. A filial nao foi".format(
    cont["pedidos_uf_filial_incoerente"]))
diz("      sobrescrita - logo qualquer analise por filial carrega essa ambiguidade.")
diz("    - {:,} pedidos sem centroide de CEP: ficam sem distancia.".format(sem_geo))
diz("    - {} grafias de cidade suspeitas a espera de revisao humana.".format(
    cont["cidades_pares_suspeitos"]))
diz()
diz("  Proximo passo com maior retorno: resolver a semantica de order_items")
diz("  (quase-duplicados) - e o unico defeito que ainda contamina valor, peso e")
diz("  contagem de itens ao mesmo tempo.")

# --------------------------------------------------------------------------- 11
SAIDA_TXT.parent.mkdir(parents=True, exist_ok=True)
SAIDA_TXT.write_text("\n".join(linhas) + "\n", encoding="utf-8")
SAIDA_JSON.write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
print()
print("gravado  {}".format(SAIDA_TXT.relative_to(RAIZ)))
print("gravado  {}".format(SAIDA_JSON.relative_to(RAIZ)))
