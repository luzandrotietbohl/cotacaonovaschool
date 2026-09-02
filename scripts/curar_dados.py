# -*- coding: utf-8 -*-
"""
Curadoria dos dados Olist — gera uma versao curada e versionada em dados_curados/vN/.

Corrige os defeitos levantados em docs/E1-2_cartao_qualidade.md.

PRINCIPIO: a curadoria nunca apaga em silencio.
  - Toda correcao deixa rastro: uma coluna qa_* por defeito e o valor original preservado.
  - Nada e removido da tabela principal. A quarentena e um EXTRATO para alguem corrigir,
    nao um lixo onde a linha desaparece.
  - Os 775 pedidos sem vendedor recebem filial 'SEM_FILIAL' em vez de sumirem no GROUP BY.

Saida (separador ';' e UTF-8 com BOM, para abrir no Excel portugues sem importacao):
  entregas_curado.csv        1 linha = 1 pedido, com flags de qualidade e SLA honesto
  geolocation_cep_curado.csv 1 linha = 1 prefixo de CEP, centroide sem duplicados
  cidades_mapa.csv           grafia original -> grafia canonica, com a regra aplicada
  quarentena.csv             linhas com violacao fisica, com o motivo
  MANIFEST.json              versao, data, hash das fontes, contagens antes/depois

Executar: python scripts/curar_dados.py [--versao v1]
"""
import argparse
import hashlib
import json
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
FONTE = RAIZ / "archive_olist"
SEP = ";"
ENC = "utf-8-sig"

# Limiares de exatidao. Sao julgamento, nao calibracao — por isso ficam a vista.
LEAD_MAX_DIAS = 100          # acima disto nao e entrega, e processo perdido
PESO_MAX_G = 30_000          # 30 kg num pacote de e-commerce
FRETE_SOBRE_MERCADORIA = 3   # frete acima de 3x a mercadoria
DIF_PAGAMENTO_REAIS = 1.00   # tolerancia entre pagamento e (mercadoria + frete)
CAUDA_DIAS = 60              # ultimos N dias da janela: coorte de registo incompleto

# Aliases de cidade. Tabela curta e explicita de proposito: cada entrada e uma decisao
# de negocio auditavel, aplicada so quando a UF confirma. Nada de adivinhacao por
# similaridade — um "sp" em Pernambuco nao e Sao Paulo.
ALIASES = {
    ("sp", "SP"): "sao paulo",
    ("sbc", "SP"): "sao bernardo do campo",
    ("sbcampo", "SP"): "sao bernardo do campo",
    ("rj", "RJ"): "rio de janeiro",
    ("bh", "MG"): "belo horizonte",
}

LOG = []


def log(msg):
    LOG.append(msg)
    enc = sys.stdout.encoding or "utf-8"
    print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def sha256(caminho, bloco=1 << 20):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for pedaco in iter(lambda: f.read(bloco), b""):
            h.update(pedaco)
    return h.hexdigest()


# --------------------------------------------------------------- normalizacao
def desacentua(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def chave_cidade(valor, uf=None):
    """Chave de agrupamento de cidade. Resolve os 12 casos de 'Sao Paulo' de uma vez.

    Passos, nesta ordem:
      1. NFC — junta 'a'+til combinante com 'ã' (duas linhas identicas na tela)
      2. minusculas, sem acento, pontuacao virando espaco, espacos colapsados
      3. remove o sufixo de UF que invadiu o campo ('sp / sp', 'sao paulo - sp')
      4. aplica o alias explicito, se a UF confirmar
    """
    if not isinstance(valor, str):
        return None
    s = unicodedata.normalize("NFC", valor).strip().lower()
    s = desacentua(s)
    for ch in ".'`\"":
        s = s.replace(ch, " ")
    s = s.replace("-", " ").replace("/", " / ")
    s = " ".join(s.split())

    # sufixo de UF dentro do campo da cidade
    partes = [p.strip() for p in s.split("/") if p.strip()]
    if len(partes) > 1:
        # 'sp / sp' -> 'sp'; 'sao paulo / sao paulo' -> 'sao paulo'
        if len(set(partes)) == 1:
            s = partes[0]
        else:
            s = partes[0]
    s = " ".join(s.split())
    if uf:
        sufixo = " " + uf.strip().lower()
        if s.endswith(sufixo) and len(s) > len(sufixo) + 1:
            s = s[: -len(sufixo)].strip()
        alias = ALIASES.get((s, uf.strip().upper()))
        if alias:
            return alias
    return s


def forma_simples(s):
    """Minusculas, sem acento, sem pontuacao, espacos colapsados.

    Serve para separar dois tipos de mudanca: restaurar acento e caixa ('sao paulo'
    -> 'São Paulo') nao e regrafia, e sim formatacao. Trocar 'sp' por 'sao paulo' ou
    tirar o sufixo de UF e regrafia estrutural, e essa merece flag.
    """
    if not isinstance(s, str):
        return None
    t = desacentua(unicodedata.normalize("NFC", s).lower())
    for ch in ".'`\"-/":
        t = t.replace(ch, " ")
    return " ".join(t.split())


def distancia(a, b, teto=2):
    """Levenshtein com corte. Usada apenas para SUGERIR pares suspeitos."""
    if abs(len(a) - len(b)) > teto:
        return teto + 1
    ant = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        atual = [i]
        for j, cb in enumerate(b, 1):
            atual.append(min(ant[j] + 1, atual[j - 1] + 1, ant[j - 1] + (ca != cb)))
        if min(atual) > teto:
            return teto + 1
        ant = atual
    return ant[-1]


def escolhe_canonica(grafias):
    """Grafia canonica de um grupo: prefere a acentuada mais frequente.

    'sao paulo' tem 152 034 linhas e 'sao paulo' acentuado tem 24 918 — a maioria
    simples devolveria a forma sem acento. O nome correto da cidade leva acento,
    logo a regra e: entre as grafias com acento, a mais frequente; se nenhuma tiver
    acento, a mais frequente do grupo. Depois, capitalizacao de nome proprio.
    """
    cont = Counter(grafias)
    acentuadas = {g: n for g, n in cont.items()
                  if any(unicodedata.combining(c) for c in unicodedata.normalize("NFKD", g))}
    base = acentuadas if acentuadas else cont
    escolhida = max(base.items(), key=lambda kv: (kv[1], kv[0]))[0]
    escolhida = unicodedata.normalize("NFC", " ".join(escolhida.split()))
    miudas = {"de", "da", "do", "das", "dos", "e", "d"}
    palavras = []
    for i, w in enumerate(escolhida.lower().split()):
        palavras.append(w if (i and w in miudas) else w[:1].upper() + w[1:])
    return " ".join(palavras)


# ------------------------------------------------------------------ argumentos
ap = argparse.ArgumentParser()
ap.add_argument("--versao", default="v1", help="pasta de destino em dados_curados/ (default: v1)")
args = ap.parse_args()
DEST = RAIZ / "dados_curados" / args.versao
DEST.mkdir(parents=True, exist_ok=True)

log("=" * 78)
log("CURADORIA DOS DADOS OLIST -> dados_curados/{}/".format(args.versao))
log("=" * 78)

# ---------------------------------------------------------------- carregamento
FICHEIROS = {
    "orders": "olist_orders_dataset.csv",
    "items": "olist_order_items_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
}
fontes = {}
dfs = {}
for nome, arq in FICHEIROS.items():
    p = FONTE / arq
    dfs[nome] = pd.read_csv(p)
    fontes[arq] = {"sha256": sha256(p), "linhas": int(len(dfs[nome]))}
    log("  lido  {:<40} {:>9,} linhas".format(arq, len(dfs[nome])))

orders, items, cust = dfs["orders"], dfs["items"], dfs["customers"]
sellers, prods, pays = dfs["sellers"], dfs["products"], dfs["payments"]
geo = dfs["geolocation"]

DATAS = ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
         "order_delivered_customer_date", "order_estimated_delivery_date"]
for c in DATAS:
    orders[c] = pd.to_datetime(orders[c], errors="coerce")
items["shipping_limit_date"] = pd.to_datetime(items["shipping_limit_date"], errors="coerce")

# =============================================================== 1. CONSISTENCIA
log("")
log("-" * 78)
log("1) CONSISTENCIA — mapa canonico de cidades")
log("-" * 78)

pares = pd.concat([
    geo[["geolocation_city", "geolocation_state"]].rename(
        columns={"geolocation_city": "cidade", "geolocation_state": "uf"}).assign(campo="geolocation_city"),
    cust[["customer_city", "customer_state"]].rename(
        columns={"customer_city": "cidade", "customer_state": "uf"}).assign(campo="customer_city"),
    sellers[["seller_city", "seller_state"]].rename(
        columns={"seller_city": "cidade", "seller_state": "uf"}).assign(campo="seller_city"),
], ignore_index=True).dropna(subset=["cidade", "uf"])

pares["cidade"] = pares.cidade.astype(str)
pares["uf"] = pares.uf.astype(str).str.upper()
pares["chave"] = [chave_cidade(c, u) for c, u in zip(pares.cidade, pares.uf)]

grupos = pares.groupby(["uf", "chave"], sort=False)
canon = {}
linhas_mapa = []
for (uf, chave), g in grupos:
    nome = escolhe_canonica(g.cidade.tolist())
    canon[(uf, chave)] = nome
    for grafia, n in Counter(g.cidade).most_common():
        regra = []
        if unicodedata.normalize("NFC", grafia) != grafia:
            regra.append("NFC")
        if grafia != grafia.strip() or "  " in grafia:
            regra.append("espacos")
        if "/" in grafia or grafia.lower().endswith(" " + uf.lower()) or "-" in grafia:
            regra.append("sufixo-uf")
        if (chave, uf) in [(k[0], k[1]) for k in ALIASES] and ALIASES.get((chave, uf)):
            regra.append("alias")
        if desacentua(grafia).lower() != grafia.lower():
            regra.append("acento")
        estrutural = forma_simples(grafia) != forma_simples(nome)
        linhas_mapa.append({
            "uf": uf, "grafia_original": grafia, "chave_normalizada": chave,
            "cidade_canonica": nome, "linhas": n,
            "regra_aplicada": "|".join(regra) if regra else "nenhuma",
            "mudanca": "estrutural" if estrutural else
                       ("formatacao" if grafia != nome else "nenhuma"),
        })

mapa = pd.DataFrame(linhas_mapa).sort_values(["uf", "cidade_canonica", "linhas"],
                                             ascending=[True, True, False])
n_grafias = mapa.grafia_original.nunique()
n_canon = mapa.cidade_canonica.nunique()
n_estrut = int((mapa.mudanca == "estrutural").sum())
log("  {:,} grafias distintas -> {:,} cidades canonicas ({:,} pares UF+cidade)".format(
    n_grafias, n_canon, len(canon)))
log("  regrafias estruturais (alias, sufixo de UF, espacos): {:,}".format(n_estrut))
log("  so formatacao (acento e caixa restaurados): {:,}".format(
    int((mapa.mudanca == "formatacao").sum())))
sp = mapa[(mapa.uf == "SP") & (mapa.cidade_canonica == "São Paulo")]
log("  Sao Paulo (capital): {} grafias colapsadas em '{}', {:,} linhas".format(
    len(sp), "São Paulo", int(sp.linhas.sum())))
for _, r in sp.sort_values("linhas", ascending=False).iterrows():
    log("      {:<26} {:>9,}  {} ({})".format(
        repr(r.grafia_original), r.linhas, r.mudanca, r.regra_aplicada))

# --- o que a regra NAO resolve: erro de digitacao exige julgamento humano
log("")
log("  Erros de digitacao nao colapsam por regra — sugeridos para revisao, nao unidos:")
freq = mapa.groupby(["uf", "cidade_canonica"], as_index=False).linhas.sum()
suspeitas = []
for uf, g in freq.groupby("uf"):
    raras = g[g.linhas <= 3]
    comuns = g[g.linhas >= 100]
    for _, r in raras.iterrows():
        a = forma_simples(r.cidade_canonica)
        for _, c in comuns.iterrows():
            b = forma_simples(c.cidade_canonica)
            if a == b:
                continue
            d = distancia(a, b)
            if d <= 2:
                suspeitas.append({
                    "uf": uf,
                    "cidade_rara": r.cidade_canonica, "linhas_rara": int(r.linhas),
                    "cidade_provavel": c.cidade_canonica, "linhas_provavel": int(c.linhas),
                    "distancia_edicao": int(d),
                    "acao": "revisar — nao unido automaticamente",
                })
suspeitas = pd.DataFrame(suspeitas).sort_values(
    ["distancia_edicao", "linhas_provavel"], ascending=[True, False]) if suspeitas else pd.DataFrame(
    columns=["uf", "cidade_rara", "linhas_rara", "cidade_provavel", "linhas_provavel",
             "distancia_edicao", "acao"])
log("    {:,} pares suspeitos em cidades_suspeitas.csv".format(len(suspeitas)))
for _, r in suspeitas.head(6).iterrows():
    log("      {} · {!r} ({}) ~ {!r} ({:,})  d={}".format(
        r.uf, r.cidade_rara, r.linhas_rara, r.cidade_provavel, r.linhas_provavel,
        r.distancia_edicao))


def curar_cidade(cidade, uf):
    k = chave_cidade(cidade, uf)
    if k is None or not isinstance(uf, str):
        return None
    return canon.get((uf.upper(), k))


# ================================================================ 2. UNICIDADE
log("")
log("-" * 78)
log("2) UNICIDADE — geolocation deduplicado e um centroide por CEP")
log("-" * 78)

n_geo_antes = len(geo)
geo_ded = geo.drop_duplicates()
n_dup = n_geo_antes - len(geo_ded)
log("  duplicados exatos removidos: {:,} de {:,} ({:.1f}%)".format(
    n_dup, n_geo_antes, n_dup / n_geo_antes * 100))

geo_ded = geo_ded.copy()
geo_ded["cidade_canonica"] = [curar_cidade(c, u) for c, u in
                              zip(geo_ded.geolocation_city, geo_ded.geolocation_state)]

# Validade das coordenadas: um ponto fora do Brasil nao e imprecisao, e erro de
# dominio — e arrasta o centroide do CEP por centenas de quilometros.
LAT_BR = (-34.0, 5.3)
LNG_BR = (-74.0, -34.8)
fora = ~(geo_ded.geolocation_lat.between(*LAT_BR) & geo_ded.geolocation_lng.between(*LNG_BR))
log("  coordenadas fora dos limites do Brasil: {:,} pontos em {:,} prefixos de CEP".format(
    int(fora.sum()), int(geo_ded.loc[fora, "geolocation_zip_code_prefix"].nunique())))
log("    lat entre {} e {} · lng entre {} e {} — excluidas do centroide, mantidas em"
    " geolocation_fora_br.csv".format(*LAT_BR, *LNG_BR))
geo_fora = geo_ded[fora].copy()
geo_ded = geo_ded[~fora]

cep = geo_ded.groupby("geolocation_zip_code_prefix").agg(
    lat=("geolocation_lat", "mean"),
    lng=("geolocation_lng", "mean"),
    n_pontos=("geolocation_lat", "size"),
).reset_index()
modo = (geo_ded.groupby("geolocation_zip_code_prefix")
        .agg(cidade_canonica=("cidade_canonica", lambda s: s.mode().iat[0] if not s.mode().empty else None),
             uf=("geolocation_state", lambda s: s.mode().iat[0] if not s.mode().empty else None))
        .reset_index())
antes = (geo.groupby("geolocation_zip_code_prefix")
         .agg(n_pontos_brutos=("geolocation_lat", "size"),
              lat_bruta=("geolocation_lat", "mean"),
              lng_bruta=("geolocation_lng", "mean")).reset_index())
cep = cep.merge(modo, on="geolocation_zip_code_prefix").merge(antes, on="geolocation_zip_code_prefix")
cep["duplicados_removidos"] = cep.n_pontos_brutos - cep.n_pontos
# de quanto o duplicado deslocava o centroide, em metros (1 grau ~ 111 km)
cep["correcao_centroide_m"] = (np.hypot(cep.lat - cep.lat_bruta,
                                      (cep.lng - cep.lng_bruta) * np.cos(np.radians(cep.lat)))
                             * 111_320).round(1)
cep = cep.rename(columns={"geolocation_zip_code_prefix": "cep_prefixo"})
log("  prefixos de CEP com centroide: {:,}".format(len(cep)))
log("  prefixos afetados por duplicacao: {:,} ({:.1f}%)".format(
    int((cep.duplicados_removidos > 0).sum()), (cep.duplicados_removidos > 0).mean() * 100))
log("  erro do centroide corrigido: mediana {:.1f} m | p99 {:.1f} m | max {:,.0f} m".format(
    cep.correcao_centroide_m.median(), cep.correcao_centroide_m.quantile(.99), cep.correcao_centroide_m.max()))

qd = items.duplicated(subset=["order_id", "product_id", "seller_id", "price", "freight_value"], keep=False)
ped_qd = set(items.loc[qd, "order_id"])
log("  quase-duplicados em order_items: {:,} linhas em {:,} pedidos — MARCADOS, nao removidos".format(
    int(qd.sum()), len(ped_qd)))
log("    (duas unidades do mesmo item ou lancamento em duplicado? o ficheiro nao diz;")
log("     a decisao e de negocio, entao a flag fica e a linha permanece)")

# ============================================== 3. TABELA DE ENTREGAS CURADA
log("")
log("-" * 78)
log("3) TABELA DE ENTREGAS — completude, validade, exatidao, proveniencia")
log("-" * 78)

primeiro = items.sort_values("order_item_id").groupby("order_id", as_index=False).first()
agreg = items.groupby("order_id", as_index=False).agg(
    n_itens=("order_item_id", "count"),
    valor_mercadoria=("price", "sum"),
    valor_frete=("freight_value", "sum"))
soma_pag = pays.groupby("order_id", as_index=False).agg(valor_pago=("payment_value", "sum"))
canal = pays.sort_values("payment_sequential").groupby("order_id", as_index=False).agg(
    canal_pagamento=("payment_type", "first"))

e = (orders
     .merge(primeiro[["order_id", "product_id", "seller_id", "shipping_limit_date"]], on="order_id", how="left")
     .merge(agreg, on="order_id", how="left")
     .merge(soma_pag, on="order_id", how="left")
     .merge(canal, on="order_id", how="left")
     .merge(sellers, on="seller_id", how="left")
     .merge(cust[["customer_id", "customer_city", "customer_state", "customer_zip_code_prefix"]],
            on="customer_id", how="left")
     .merge(prods[["product_id", "product_weight_g"]], on="product_id", how="left"))

n_entrada = len(e)

# --- completude: o pedido orfao ganha uma filial, em vez de sumir no GROUP BY
e["qa_sem_item"] = e.seller_id.isna()
e["filial"] = e.seller_state.fillna("SEM_FILIAL")
e["filial_cidade"] = [curar_cidade(c, u) for c, u in zip(e.seller_city, e.seller_state)]
e["cliente_cidade"] = [curar_cidade(c, u) for c, u in zip(e.customer_city, e.customer_state)]
def regrafia_estrutural(orig, curada):
    o, c = forma_simples(orig), forma_simples(curada)
    return bool(o is not None and c is not None and o != c)


e["qa_cidade_regrafada"] = [
    regrafia_estrutural(oc, cc) or regrafia_estrutural(os_, cs)
    for oc, cc, os_, cs in zip(e.customer_city, e.cliente_cidade,
                               e.seller_city, e.filial_cidade)]

# --- validade da propria chave de agrupamento: a UF declarada bate com o CEP?
# O CEP e um codigo estruturado e concorda com o nome da cidade; a UF digitada nao.
# Vendedor 289cdb32... tem CEP 31570 (Belo Horizonte) declarado como SP.
# NAO sobrescrevemos a filial: a UF do CEP entra numa coluna ao lado, com flag.
uf_por_cep = dict(zip(cep.cep_prefixo, cep.uf))
e["filial_uf_cep"] = e.seller_zip_code_prefix.map(uf_por_cep)
e["cliente_uf_cep"] = e.customer_zip_code_prefix.map(uf_por_cep)
e["qa_uf_filial_incoerente"] = (
    e.seller_state.notna() & e.filial_uf_cep.notna() & (e.seller_state != e.filial_uf_cep))
e["qa_uf_cliente_incoerente"] = (
    e.customer_state.notna() & e.cliente_uf_cep.notna() & (e.customer_state != e.cliente_uf_cep))

n_vend_inc = sellers.assign(uf_cep=sellers.seller_zip_code_prefix.map(uf_por_cep)) \
    .query("uf_cep.notna() and seller_state != uf_cep").shape[0]
log("  UF incoerente com o CEP: {:,} vendedores -> {:,} pedidos ({:.2f}%)".format(
    n_vend_inc, int(e.qa_uf_filial_incoerente.sum()), e.qa_uf_filial_incoerente.mean() * 100))
log("    e {:,} clientes -> {:,} pedidos".format(
    int(cust.assign(uf_cep=cust.customer_zip_code_prefix.map(uf_por_cep))
        .query("uf_cep.notna() and customer_state != uf_cep").shape[0]),
    int(e.qa_uf_cliente_incoerente.sum())))
log("    a filial NAO foi sobrescrita; a UF do CEP fica em filial_uf_cep para decisao")
mov = (e[e.qa_uf_filial_incoerente].groupby(["seller_state", "filial_uf_cep"], observed=True)
       .order_id.count().sort_values(ascending=False))
for (declarada, real), n in mov.head(5).items():
    log("      {:>3} declarada, CEP diz {:>3}: {:>5,} pedidos".format(declarada, real, n))

e["qa_sem_dt_entrega"] = e.order_delivered_customer_date.isna()
e["qa_sem_dt_coleta"] = e.order_delivered_carrier_date.isna()
e["qa_sem_dt_aprovacao"] = e.order_approved_at.isna()
e["qa_sem_peso"] = e.product_weight_g.isna()

# --- validade: valores fora do dominio possivel. Corrigidos para nulo, com flag.
e["qa_entrega_antes_coleta"] = (
    e.order_delivered_customer_date.notna() & e.order_delivered_carrier_date.notna()
    & (e.order_delivered_customer_date < e.order_delivered_carrier_date))
e["qa_lag_despacho_negativo"] = (
    e.order_delivered_carrier_date.notna() & e.order_approved_at.notna()
    & (e.order_delivered_carrier_date < e.order_approved_at))
e["peso_g_original"] = e.product_weight_g
e["qa_peso_invalido"] = e.product_weight_g.notna() & (e.product_weight_g <= 0)
e.loc[e.qa_peso_invalido, "product_weight_g"] = np.nan     # peso <= 0 nao e peso
e["qa_frete_zero"] = e.valor_frete.notna() & (e.valor_frete == 0)
e["qa_pagamento_invalido"] = e.valor_pago.notna() & (e.valor_pago <= 0)
e["qa_canal_indefinido"] = e.canal_pagamento.eq("not_defined")
e["qa_cep_sem_geo"] = ~e.customer_zip_code_prefix.isin(cep.cep_prefixo)

# --- exatidao: possivel mas implausivel. Nunca corrigido, sempre marcado.
e["lead_dias"] = ((e.order_delivered_customer_date - e.order_purchase_timestamp)
                  .dt.total_seconds() / 86400).round(3)
e["qa_lead_implausivel"] = e.lead_dias.notna() & (e.lead_dias > LEAD_MAX_DIAS)
e["qa_peso_implausivel"] = e.product_weight_g.notna() & (e.product_weight_g > PESO_MAX_G)
e["qa_frete_desproporcional"] = (
    e.valor_mercadoria.notna() & (e.valor_mercadoria > 0)
    & (e.valor_frete > FRETE_SOBRE_MERCADORIA * e.valor_mercadoria))
e["qa_pagamento_divergente"] = (
    e.valor_pago.notna() & e.valor_mercadoria.notna()
    & ((e.valor_pago - (e.valor_mercadoria + e.valor_frete)).abs() > DIF_PAGAMENTO_REAIS))
e["qa_quase_duplicado"] = e.order_id.isin(ped_qd)

# --- atualidade: a coorte de corte do extrato
fim = orders.order_purchase_timestamp.max()
e["qa_cauda_incompleta"] = e.order_purchase_timestamp >= fim - pd.Timedelta(days=CAUDA_DIAS)

# --- proveniencia: quem registrou o despacho
vol = e.groupby("seller_id").order_id.count()
faixa = pd.cut(vol, [0, 10, 100, 1000, np.inf],
               labels=["1-10 pedidos", "11-100", "101-1000", ">1000"])
e = e.merge(faixa.rename("origem_registo"), left_on="seller_id", right_index=True, how="left")
e["origem_registo"] = e.origem_registo.astype("object").fillna("SEM_VENDEDOR")

# --- SLA honesto: o defeito nº 1 do cartao, resolvido no proprio esquema
e["ativo"] = e.order_status != "canceled"
e["atraso_mensuravel"] = e.order_delivered_customer_date.notna()
e["dias_vs_prazo"] = ((e.order_delivered_customer_date - e.order_estimated_delivery_date)
                      .dt.total_seconds() / 86400).round(3)
e["atrasado"] = np.where(e.atraso_mensuravel, e.dias_vs_prazo > 0, None)
e["vencido_sem_registo"] = (e.ativo & ~e.atraso_mensuravel
                            & e.order_estimated_delivery_date.notna()
                            & (e.order_estimated_delivery_date < fim))
# atraso_honesto: o denominador passa a ser TODOS os pedidos ativos.
# Sem registo e prazo vencido conta como falha ate prova em contrario.
e["atraso_honesto"] = np.where(
    ~e.ativo, None,
    np.where(e.atraso_mensuravel, e.dias_vs_prazo > 0, e.vencido_sem_registo))

QA = [c for c in e.columns if c.startswith("qa_")]
e["qa_flags_total"] = e[QA].sum(axis=1).astype(int)

# quarentena = violacao FISICA ou de dominio, nao implausibilidade
DUROS = ["qa_entrega_antes_coleta", "qa_lag_despacho_negativo", "qa_peso_invalido",
         "qa_pagamento_invalido", "qa_canal_indefinido"]
e["qa_quarentena"] = e[DUROS].any(axis=1)

log("  pedidos na tabela curada: {:,} (entrada: {:,} — nenhuma linha perdida)".format(len(e), n_entrada))
log("  filial 'SEM_FILIAL' criada para {:,} pedidos orfaos".format(int(e.qa_sem_item.sum())))
log("")
log("  Flags de qualidade, por pedido:")
for c in sorted(QA, key=lambda x: -int(e[x].sum())):
    n = int(e[c].sum())
    if n:
        log("    {:<28} {:>7,}  ({:>5.2f}%)".format(c.replace("qa_", ""), n, n / len(e) * 100))
log("")
log("  pedidos sem nenhuma flag: {:,} ({:.1f}%)".format(
    int((e.qa_flags_total == 0).sum()), (e.qa_flags_total == 0).mean() * 100))
log("  pedidos em quarentena (violacao dura): {:,} ({:.2f}%)".format(
    int(e.qa_quarentena.sum()), e.qa_quarentena.mean() * 100))
log("")
log("  SLA, os dois denominadores lado a lado (pedidos ativos):")
ativ = e[e.ativo]
rep = ativ.atrasado.astype("boolean")
log("    reportado (so quem tem data): {:.2f}%  sobre {:,} pedidos".format(
    rep.mean(skipna=True) * 100, int(rep.notna().sum())))
log("    honesto  (todos os ativos):   {:.2f}%  sobre {:,} pedidos".format(
    ativ.atraso_honesto.astype("boolean").mean() * 100, len(ativ)))
log("    diferenca: {:,} pedidos vencidos sem registo entram na conta".format(
    int(ativ.vencido_sem_registo.sum())))

# ---------------------------------------------------------------- ordem final
COLS = [
    "order_id", "order_status", "ativo",
    "filial", "filial_cidade", "filial_uf_cep", "seller_id", "origem_registo",
    "customer_id", "cliente_cidade", "customer_state", "cliente_uf_cep",
    "customer_zip_code_prefix",
    "order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
    "order_delivered_customer_date", "order_estimated_delivery_date", "shipping_limit_date",
    "n_itens", "product_id", "product_weight_g", "peso_g_original",
    "valor_mercadoria", "valor_frete", "valor_pago", "canal_pagamento",
    "lead_dias", "dias_vs_prazo",
    "atraso_mensuravel", "atrasado", "vencido_sem_registo", "atraso_honesto",
] + sorted(QA) + ["qa_flags_total", "qa_quarentena"]
curado = e[COLS].rename(columns={
    "order_purchase_timestamp": "dt_compra",
    "order_approved_at": "dt_aprovacao",
    "order_delivered_carrier_date": "dt_coleta",
    "order_delivered_customer_date": "dt_entrega",
    "order_estimated_delivery_date": "dt_prazo",
    "shipping_limit_date": "dt_limite_expedicao",
    "customer_state": "cliente_uf",
    "customer_zip_code_prefix": "cliente_cep_prefixo",
    "product_weight_g": "peso_g",
})

# ------------------------------------------------------------------- gravacao
log("")
log("-" * 78)
log("4) GRAVACAO — dados_curados/{}/".format(args.versao))
log("-" * 78)

quarentena = curado[curado.qa_quarentena].copy()
motivos = []
for _, r in e[e.qa_quarentena].iterrows():
    motivos.append("|".join(d.replace("qa_", "") for d in DUROS if r[d]))
quarentena.insert(1, "motivo_quarentena", motivos)

saidas = {
    "entregas_curado.csv": curado,
    "geolocation_cep_curado.csv": cep,
    "geolocation_fora_br.csv": geo_fora,
    "cidades_mapa.csv": mapa,
    "cidades_suspeitas.csv": suspeitas,
    "quarentena.csv": quarentena,
}
manifest_saidas = {}
for nome, df in saidas.items():
    p = DEST / nome
    df.to_csv(p, sep=SEP, index=False, encoding=ENC, date_format="%Y-%m-%d %H:%M:%S")
    manifest_saidas[nome] = {
        "linhas": int(len(df)), "colunas": int(df.shape[1]),
        "bytes": int(p.stat().st_size), "sha256": sha256(p),
    }
    log("  {:<28} {:>8,} linhas x {:>3} colunas  {:>8.1f} MB".format(
        nome, len(df), df.shape[1], p.stat().st_size / 1e6))

manifest = {
    "versao": args.versao,
    "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "gerado_por": "scripts/curar_dados.py",
    "diagnostico": "docs/E1-2_cartao_qualidade.md",
    "janela_dos_dados": {
        "inicio": str(orders.order_purchase_timestamp.min().date()),
        "fim": str(fim.date()),
    },
    "fontes": fontes,
    "saidas": manifest_saidas,
    "parametros": {
        "lead_max_dias": LEAD_MAX_DIAS,
        "peso_max_g": PESO_MAX_G,
        "frete_sobre_mercadoria": FRETE_SOBRE_MERCADORIA,
        "dif_pagamento_reais": DIF_PAGAMENTO_REAIS,
        "cauda_dias": CAUDA_DIAS,
        "aliases_cidade": {"{}|{}".format(k[0], k[1]): v for k, v in ALIASES.items()},
        "separador": SEP,
        "codificacao": ENC,
    },
    "regras": [
        "Nenhuma linha e removida da tabela principal; a quarentena e um extrato.",
        "Peso <= 0 vira nulo; o valor original fica em peso_g_original.",
        "Grafias de cidade colapsadas por chave NFC + sem acento + sem sufixo de UF; "
        "a canonica e a acentuada mais frequente do grupo.",
        "Duplicados exatos de geolocation removidos antes de calcular o centroide do CEP.",
        "Coordenadas fora dos limites do Brasil excluidas do centroide e preservadas "
        "em geolocation_fora_br.csv.",
        "Erro de digitacao em nome de cidade NAO e unido automaticamente: vai para "
        "cidades_suspeitas.csv com a sugestao e a distancia de edicao.",
        "Quase-duplicados de order_items marcados, nunca removidos: a decisao e de negocio.",
        "Pedidos sem vendedor recebem filial 'SEM_FILIAL' para nao sumirem no GROUP BY.",
        "atraso_honesto usa TODOS os pedidos ativos como denominador; prazo vencido "
        "sem registo conta como atraso ate prova em contrario.",
        "UF declarada e conferida contra a UF do prefixo de CEP; divergencia vai para "
        "filial_uf_cep / cliente_uf_cep com flag, sem sobrescrever a filial.",
        "Implausibilidade (exatidao) e sempre marcada e nunca corrigida.",
    ],
    "contagens": {
        "pedidos_entrada": int(n_entrada),
        "pedidos_saida": int(len(curado)),
        "pedidos_sem_flag": int((e.qa_flags_total == 0).sum()),
        "pedidos_quarentena": int(e.qa_quarentena.sum()),
        "geolocation_linhas_brutas": int(n_geo_antes),
        "geolocation_duplicados_removidos": int(n_dup),
        "geolocation_pontos_fora_br": int(len(geo_fora)),
        "cidades_grafias_entrada": int(n_grafias),
        "cidades_canonicas_saida": int(n_canon),
        "cidades_regrafias_estruturais": int(n_estrut),
        "cidades_pares_suspeitos": int(len(suspeitas)),
        "vendedores_uf_incoerente": int(n_vend_inc),
        "pedidos_uf_filial_incoerente": int(e.qa_uf_filial_incoerente.sum()),
        "sla_reportado_pct": round(float(rep.mean(skipna=True) * 100), 4),
        "sla_honesto_pct": round(float(ativ.atraso_honesto.astype("boolean").mean() * 100), 4),
    },
}
(DEST / "MANIFEST.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
log("  MANIFEST.json                gravado")

(DEST / "curadoria.log").write_text("\n".join(LOG), encoding="utf-8")
print("\n[log em {}]".format(DEST / "curadoria.log"))
