# -*- coding: utf-8 -*-
"""
E1.2 - Cartao de Pontuacao de Qualidade dos Dados
Fonte real: dataset Olist (archive_olist/) usado no lugar de entregas.csv.
Uma linha = um pedido (viagem/entrega).
Executar: python scripts/analise_qualidade_e12.py
"""
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1] / "archive_olist"
OUT = []


def p(*a):
    s = " ".join(str(x) for x in a)
    OUT.append(s)
    enc = sys.stdout.encoding or "utf-8"
    print(s.encode(enc, errors="replace").decode(enc, errors="replace"))


def norm(s):
    if not isinstance(s, str):
        return s
    s = unicodedata.normalize("NFKD", s.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace(".", " ").replace(chr(39), " ").replace("`", " ")
    return " ".join(s.split())


# ---------------------------------------------------------------- carregamento
orders = pd.read_csv(BASE / "olist_orders_dataset.csv")
items = pd.read_csv(BASE / "olist_order_items_dataset.csv")
cust = pd.read_csv(BASE / "olist_customers_dataset.csv")
sellers = pd.read_csv(BASE / "olist_sellers_dataset.csv")
prods = pd.read_csv(BASE / "olist_products_dataset.csv")
revs = pd.read_csv(BASE / "olist_order_reviews_dataset.csv")
geo = pd.read_csv(BASE / "olist_geolocation_dataset.csv")
pays = pd.read_csv(BASE / "olist_order_payments_dataset.csv")
tr = pd.read_csv(BASE / "product_category_name_translation.csv")

DATES = ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
         "order_delivered_customer_date", "order_estimated_delivery_date"]
for c in DATES:
    orders[c] = pd.to_datetime(orders[c], errors="coerce")

# tabela "entregas": 1 linha = 1 pedido; filial de origem = UF do vendedor principal
first_item = items.sort_values("order_item_id").groupby("order_id", as_index=False).first()
agg_it = items.groupby("order_id", as_index=False).agg(
    frete_total=("freight_value", "sum"),
    valor_total=("price", "sum"),
    n_itens=("order_item_id", "count"))

ent = (orders
       .merge(first_item[["order_id", "product_id", "seller_id", "shipping_limit_date"]],
              on="order_id", how="left")
       .merge(agg_it, on="order_id", how="left")
       .merge(sellers[["seller_id", "seller_state", "seller_city"]], on="seller_id", how="left")
       .merge(cust[["customer_id", "customer_state", "customer_city"]], on="customer_id", how="left")
       .merge(prods[["product_id", "product_weight_g", "product_length_cm",
                     "product_height_cm", "product_width_cm"]], on="product_id", how="left"))
ent = ent.rename(columns={"seller_state": "filial"})

N = len(ent)
p("=" * 78)
p("E1.2 - CARTAO DE QUALIDADE DOS DADOS - fonte: Olist (archive_olist/)")
p("=" * 78)
p("Linhas na tabela de entregas construida: {:,}  (1 linha = 1 pedido)".format(N))
p("Janela temporal: {:%Y-%m-%d} a {:%Y-%m-%d}".format(
    orders.order_purchase_timestamp.min(), orders.order_purchase_timestamp.max()))
p("Apoio: {:,} itens, {:,} pagamentos, {:,} avaliacoes, {:,} geolocalizacoes".format(
    len(items), len(pays), len(revs), len(geo)))

deliv = ent[ent.order_status == "delivered"].copy()
p("Pedidos com status delivered: {:,}".format(len(deliv)))

# ------------------------------------------------------------- 1. COMPLETUDE
p("")
p("-" * 78)
p("1) COMPLETUDE - campos vazios, por coluna e por filial")
p("-" * 78)
comp = pd.DataFrame({"nulos": ent.isna().sum()})
comp["pct"] = (comp.nulos / N * 100).round(2)
for c, r in comp[comp.nulos > 0].sort_values("nulos", ascending=False).iterrows():
    p("  {:<34} {:>8,}  ({:>5.2f}%)".format(c, int(r.nulos), r.pct))

p("")
p("  Buraco critico: data de entrega ausente em pedidos NAO cancelados")
naoc = ent[ent.order_status != "canceled"]
sem_entrega = naoc[naoc.order_delivered_customer_date.isna()]
p("    {:,} linhas ({:.2f}% dos nao cancelados)".format(
    len(sem_entrega), len(sem_entrega) / len(naoc) * 100))
p("    por status: " + ", ".join("{}={}".format(k, v)
                                for k, v in sem_entrega.order_status.value_counts().items()))

p("")
p("  Ausencias por filial (UF do vendedor) - base = TODOS os pedidos nao cancelados")
p("  (medir so sobre status=delivered esconde o problema: por definicao esses tem data)")
g = naoc.groupby("filial").agg(
    pedidos=("order_id", "count"),
    sem_dt_transportadora=("order_delivered_carrier_date", lambda s: s.isna().sum()),
    sem_dt_cliente=("order_delivered_customer_date", lambda s: s.isna().sum()),
    sem_peso=("product_weight_g", lambda s: s.isna().sum()),
    sem_aprovacao=("order_approved_at", lambda s: s.isna().sum()))
g["pct_sem_transp"] = (g.sem_dt_transportadora / g.pedidos * 100).round(2)
g["pct_sem_dt_cliente"] = (g.sem_dt_cliente / g.pedidos * 100).round(2)
p("   " + g.sort_values("pedidos", ascending=False).head(14)[
    ["pedidos", "sem_dt_transportadora", "sem_dt_cliente", "sem_peso", "sem_aprovacao",
     "pct_sem_transp", "pct_sem_dt_cliente"]].to_string().replace("\n", "\n   "))
p("")
p("  Filiais com pct_sem_dt_cliente acima da media geral ({:.2f}%), min. 100 pedidos:".format(
    naoc.order_delivered_customer_date.isna().mean() * 100))
alvo = g[(g.pedidos >= 100)].sort_values("pct_sem_dt_cliente", ascending=False).head(8)
p("   " + alvo[["pedidos", "sem_dt_cliente", "pct_sem_dt_cliente", "pct_sem_transp"]]
  .to_string().replace("\n", "\n   "))
p("")
p("  Linhas sem UF de vendedor (seller_id nulo/orfao): {:,}".format(int(ent.filial.isna().sum())))

# --------------------------------------------------------------- 2. UNICIDADE
p("")
p("-" * 78)
p("2) UNICIDADE - duplicados exatos e quase-duplicados")
p("-" * 78)
p("  orders: linhas duplicadas exatas ............... {:,}".format(int(orders.duplicated().sum())))
p("  orders: order_id repetido ...................... {:,}".format(int(orders.order_id.duplicated().sum())))
p("  order_items: linhas duplicadas exatas ......... {:,}".format(int(items.duplicated().sum())))
qd = items.duplicated(subset=["order_id", "product_id", "seller_id", "price", "freight_value"], keep=False)
ped_qd = items[qd].order_id.nunique()
p("  order_items: QUASE-duplicados (mesmo pedido+produto+vendedor+preco+frete,")
p("               separados so por order_item_id) .. {:,} linhas em {:,} pedidos".format(int(qd.sum()), ped_qd))
p("  reviews: review_id repetido ................... {:,}".format(int(revs.review_id.duplicated().sum())))
dup_rev = revs.order_id.duplicated(keep=False)
p("  reviews: mesmo order_id com >1 avaliacao ...... {:,} linhas em {:,} pedidos".format(
    int(dup_rev.sum()), revs[dup_rev].order_id.nunique()))
p("  payments: order_id com >1 linha de pagamento .. {:,}".format(int(pays.order_id.duplicated(keep=False).sum())))
n_geo_dup = int(geo.duplicated().sum())
p("  geolocation: linhas duplicadas exatas ......... {:,} de {:,} ({:.1f}%)".format(
    n_geo_dup, len(geo), n_geo_dup / len(geo) * 100))
p("  customers: customer_unique_id repetido ........ {:,} (mesma pessoa com varios ids tecnicos)".format(
    int(cust.customer_unique_id.duplicated().sum())))

# ------------------------------------------------------------ 3. CONSISTENCIA
p("")
p("-" * 78)
p("3) CONSISTENCIA - mesma entidade escrita de formas diferentes")
p("-" * 78)
gc = geo[["geolocation_city", "geolocation_state"]].drop_duplicates().copy()
gc["k"] = gc.geolocation_city.map(norm)
var = gc.groupby("k").geolocation_city.nunique().sort_values(ascending=False)
p("  geolocation_city: {:,} grafias distintas -> {:,} cidades apos normalizar acento/caixa/pontuacao".format(
    int(geo.geolocation_city.nunique()), int(gc.k.nunique())))
cidades_multi = int((var >= 3).sum())
p("  Cidades com >=3 grafias diferentes: {:,}".format(cidades_multi))
CAPITAL = {"sao paulo", "sp", "saopaulo", "sao paulop", "sao pauo", "s paulo"}


def eh_capital_sp(v):
    """True se o valor designa a capital de Sao Paulo, seja qual for a grafia."""
    k = norm(v)
    for suf in (" - sp", " / sao paulo", " / sp", " sp", "/sp", "-sp", " /sp"):
        if k.endswith(suf):
            k = k[: -len(suf)].strip()
    k = k.replace(" / ", " ").strip()
    return k in CAPITAL


cand = []
for df_, col in [(geo, "geolocation_city"), (sellers, "seller_city"), (cust, "customer_city")]:
    for v in df_[col].dropna().astype(str).unique():
        if eh_capital_sp(v):
            cand.append((col, v))
sp = sorted({v for _, v in cand})
p("")
p("  'Sao Paulo' escrito de quantas formas? Varredura nos 3 campos de cidade:")
p("  {} variantes distintas encontradas:".format(len(sp)))
for col in ["geolocation_city", "seller_city", "customer_city"]:
    vs = sorted({v for c, v in cand if c == col})
    p("    {:<18} {:>3} variantes: {}".format(col, len(vs), " | ".join(vs)))
p("")
p("  Top 8 cidades por numero de grafias:")
for k, v in var.head(8).items():
    ex = sorted(gc.loc[gc.k == k, "geolocation_city"].unique())[:6]
    p("    {:<28} {:>3} grafias  ex.: {}".format(k, int(v), ", ".join(ex)))

cc = pd.Series(cust.customer_city.unique())
p("")
p("  customer_city: {:,} grafias -> {:,} normalizadas".format(len(cc), int(cc.map(norm).nunique())))
sc = pd.Series(sellers.seller_city.unique())
p("  seller_city:   {:,} grafias -> {:,} normalizadas".format(len(sc), int(sc.map(norm).nunique())))
bad = [c for c in sc if isinstance(c, str) and ("/" in c or c != c.strip())]
p("  seller_city com UF ou barra dentro do proprio campo: {} ex.: {}".format(len(bad), bad[:6]))

falta_tr = set(prods.product_category_name.dropna()) - set(tr[tr.columns[0]])
p("  product_category_name sem traducao na tabela de dominio: {} -> {}".format(
    len(falta_tr), sorted(falta_tr)))

# ---------------------------------------------------------------- 4. VALIDADE
p("")
p("-" * 78)
p("4) VALIDADE - valores fora do dominio possivel")
p("-" * 78)
inv_ordem = deliv[deliv.order_delivered_customer_date < deliv.order_purchase_timestamp]
p("  Entrega ANTES da compra (chegada antes da partida) ....... {:,}".format(len(inv_ordem)))
inv_carrier = deliv[deliv.order_delivered_carrier_date.notna()
                    & deliv.order_delivered_customer_date.notna()
                    & (deliv.order_delivered_customer_date < deliv.order_delivered_carrier_date)]
p("  Entrega ao cliente ANTES da coleta pela transportadora ... {:,}".format(len(inv_carrier)))
inv_aprov = orders[orders.order_approved_at.notna()
                   & (orders.order_approved_at < orders.order_purchase_timestamp)]
p("  Aprovacao ANTES da compra ................................ {:,}".format(len(inv_aprov)))
lim = deliv.copy()
lim["shipping_limit_date"] = pd.to_datetime(lim.shipping_limit_date, errors="coerce")
inv_lim = lim[lim.shipping_limit_date < lim.order_purchase_timestamp]
p("  Prazo-limite de expedicao ANTES da compra ................ {:,}".format(len(inv_lim)))
n_peso0 = int((prods.product_weight_g <= 0).sum())
p("  product_weight_g <= 0 .................................... {:,}   (nulos: {})".format(
    n_peso0, int(prods.product_weight_g.isna().sum())))
dims = ["product_length_cm", "product_height_cm", "product_width_cm"]
p("  Dimensao <= 0 em algum eixo .............................. {:,}".format(
    int((prods[dims] <= 0).any(axis=1).sum())))
p("  price <= 0 (itens) ....................................... {:,}".format(int((items.price <= 0).sum())))
p("  freight_value < 0 ........................................ {:,}".format(int((items.freight_value < 0).sum())))
p("  freight_value == 0 em pedido entregue .................... {:,}  (frete gratis ou registo ausente?)".format(
    int((deliv.frete_total == 0).sum())))
p("  payment_value <= 0 ....................................... {:,}".format(int((pays.payment_value <= 0).sum())))
p("  installments == 0 com credit_card ........................ {:,}".format(
    int(((pays.payment_installments == 0) & (pays.payment_type == "credit_card")).sum())))
p("  payment_type = not_defined ............................... {:,}".format(
    int((pays.payment_type == "not_defined").sum())))
p("  review_score fora de 1..5 ................................ {:,}".format(
    int((~revs.review_score.isin([1, 2, 3, 4, 5])).sum())))
p("  ZIP de cliente sem correspondencia em geolocation ........ {:,}".format(
    int(cust[~cust.customer_zip_code_prefix.isin(geo.geolocation_zip_code_prefix)].shape[0])))

# ---------------------------------------------------------------- 5. EXATIDAO
p("")
p("-" * 78)
p("5) EXATIDAO - valores possiveis mas implausiveis")
p("-" * 78)
deliv["lead_dias"] = (deliv.order_delivered_customer_date
                      - deliv.order_purchase_timestamp).dt.total_seconds() / 86400
p("  Lead time compra->entrega (dias):")
p("   " + deliv.lead_dias.describe(percentiles=[.5, .9, .99]).round(2).to_string().replace("\n", "\n   "))
p("  Lead time > 100 dias ..................................... {:,}  (max {:.0f} dias)".format(
    int((deliv.lead_dias > 100).sum()), deliv.lead_dias.max()))
p("  Lead time < 12 horas ..................................... {:,}".format(int((deliv.lead_dias < 0.5).sum())))
p("  product_weight_g > 30.000 g em pacote de e-commerce ...... {:,}  (max {:,.0f} g)".format(
    int((prods.product_weight_g > 30000).sum()), prods.product_weight_g.max()))
p("  Soma das tres dimensoes > 250 cm ......................... {:,}".format(
    int((prods[dims].sum(axis=1) > 250).sum())))
p("  Frete > 3x o valor da mercadoria ......................... {:,}".format(
    int(((deliv.frete_total > 3 * deliv.valor_total) & (deliv.valor_total > 0)).sum())))
soma_pag = pays.groupby("order_id").payment_value.sum()
chk = deliv.set_index("order_id")[["valor_total", "frete_total"]].join(soma_pag)
chk["dif"] = (chk.payment_value - (chk.valor_total + chk.frete_total)).abs()
n_dif = int((chk.dif > 1).sum())
p("  Pagamento != mercadoria+frete (dif > R$1) ................ {:,} pedidos (dif mediana R${:.2f})".format(
    n_dif, chk.dif.median()))

# --------------------------------------------------------------- 6. ATUALIDADE
p("")
p("-" * 78)
p("6) ATUALIDADE - atrasos entre o evento e o registo")
p("-" * 78)
orders["lag_aprov_h"] = (orders.order_approved_at - orders.order_purchase_timestamp).dt.total_seconds() / 3600
d2 = deliv.copy()
d2["lag_desp_h"] = (d2.order_delivered_carrier_date - d2.order_approved_at).dt.total_seconds() / 3600
revs["review_creation_date"] = pd.to_datetime(revs.review_creation_date, errors="coerce")
revs["review_answer_timestamp"] = pd.to_datetime(revs.review_answer_timestamp, errors="coerce")
revs["lag_resp_h"] = (revs.review_answer_timestamp - revs.review_creation_date).dt.total_seconds() / 3600
for nome, s in [("compra -> aprovacao (h)", orders.lag_aprov_h),
                ("aprovacao -> despacho (h)", d2.lag_desp_h),
                ("avaliacao -> resposta (h)", revs.lag_resp_h)]:
    q = s.dropna()
    p("  {:<28} mediana {:>7.1f} | p90 {:>8.1f} | max {:>9.1f} | negativos {:>5,}".format(
        nome, q.median(), q.quantile(.9), q.max(), int((q < 0).sum())))
hoje = pd.Timestamp("2026-09-02")
idade = (hoje - orders.order_purchase_timestamp.max()).days
p("")
p("  Ultima compra registada: {:%Y-%m-%d} -> dataset com {:,} dias de idade".format(
    orders.order_purchase_timestamp.max(), idade))
p("  Ultima entrega registada: {:%Y-%m-%d}".format(orders.order_delivered_customer_date.max()))
ult = orders[orders.order_purchase_timestamp >= orders.order_purchase_timestamp.max() - pd.Timedelta(days=60)]
pct_cauda = ult.order_delivered_customer_date.isna().mean() * 100
p("  Ultimos 60 dias da janela: {:,} pedidos, {:,} ({:.1f}%) sem data de entrega -> cauda incompleta".format(
    len(ult), int(ult.order_delivered_customer_date.isna().sum()), pct_cauda))

# ------------------------------------------------------------- 7. PROVENIENCIA
p("")
p("-" * 78)
p("7) PROVENIENCIA - a qualidade varia por origem do registo?")
p("-" * 78)
base = naoc.copy()
base["lead_dias"] = (base.order_delivered_customer_date
                     - base.order_purchase_timestamp).dt.total_seconds() / 86400
base["atraso"] = base.order_delivered_customer_date > base.order_estimated_delivery_date
vol = base.groupby("seller_id").order_id.count()
tier = pd.cut(vol, [0, 10, 100, 1000, np.inf], labels=["1-10 pedidos", "11-100", "101-1000", ">1000"])
d3 = base.merge(tier.rename("tier_vendedor"), left_on="seller_id", right_index=True, how="left")
gt = d3.groupby("tier_vendedor", observed=True, dropna=False).agg(
    pedidos=("order_id", "count"),
    pct_sem_transp=("order_delivered_carrier_date", lambda s: round(s.isna().mean() * 100, 2)),
    pct_sem_dt_cliente=("order_delivered_customer_date", lambda s: round(s.isna().mean() * 100, 2)),
    pct_sem_peso=("product_weight_g", lambda s: round(s.isna().mean() * 100, 2)),
    lead_mediano=("lead_dias", lambda s: round(s.median(), 1)))
p("  Por porte do vendedor (quem registra o despacho) - base nao cancelados:")
p("   " + gt.to_string().replace("\n", "\n   "))
p("  Nota: a linha NaN = pedidos sem seller_id (sem item registado) - 100% sem entrega.")
pv = pays.groupby("order_id").payment_type.first()
d4 = base.merge(pv, left_on="order_id", right_index=True, how="left")
gp = d4.groupby("payment_type", dropna=False, observed=True).agg(
    pedidos=("order_id", "count"),
    pct_atraso=("atraso", lambda s: round(s.mean() * 100, 2)),
    pct_sem_dt_cliente=("order_delivered_customer_date", lambda s: round(s.isna().mean() * 100, 2)))
p("")
p("  Por canal de pagamento (origem do registo comercial):")
p("   " + gp.to_string().replace("\n", "\n   "))

# ---------------------------------------- PERGUNTA OBRIGATORIA: atraso x filial
p("")
p("=" * 78)
p("PERGUNTA OBRIGATORIA - atraso por filial vs. dados em falta por filial")
p("=" * 78)
fim = orders.order_purchase_timestamp.max()
base["mensuravel"] = base.order_delivered_customer_date.notna()
base["vencido_sem_registo"] = (~base.mensuravel) & (base.order_estimated_delivery_date < fim)

tab = base.groupby("filial", dropna=False).agg(
    pedidos=("order_id", "count"),
    mensuraveis=("mensuravel", "sum"),
    sem_registo=("mensuravel", lambda s: int((~s).sum())),
    atrasos=("atraso", "sum"),
    lead_mediano=("lead_dias", lambda s: round(s.median(), 1)))
tab["pct_sem_registo"] = (tab.sem_registo / tab.pedidos * 100).round(2)
# (A) metrica que a empresa reporta hoje: atraso / entregas com data
tab["pct_atraso_reportado"] = (tab.atrasos / tab.mensuraveis * 100).round(2)
# (B) metrica honesta: sem registo com prazo vencido conta como atraso
venc_f = base.groupby("filial", dropna=False).vencido_sem_registo.sum()
tab["pct_atraso_honesto"] = ((tab.atrasos + venc_f) / tab.pedidos * 100).round(2)
tab["salto_pp"] = (tab.pct_atraso_honesto - tab.pct_atraso_reportado).round(2)

cols = ["pedidos", "mensuraveis", "sem_registo", "pct_sem_registo",
        "pct_atraso_reportado", "pct_atraso_honesto", "salto_pp", "lead_mediano"]
p("")
p("  (A) pct_atraso_reportado = atrasos / pedidos COM data de entrega  <- o KPI de hoje")
p("  (B) pct_atraso_honesto   = (atrasos + prazos vencidos sem registo) / TODOS os pedidos")
p("")
p("  Ranking pelo KPI de hoje (melhor primeiro) - todas as filiais:")
p("   " + tab.sort_values("pct_atraso_reportado")[cols].to_string().replace("\n", "\n   "))
p("")
p("  Somente filiais com >= 200 pedidos, ordenado pelo KPI de hoje:")
big = tab[tab.pedidos >= 200]
p("   " + big.sort_values("pct_atraso_reportado")[cols].to_string().replace("\n", "\n   "))
p("")
p("  As MESMAS filiais, reordenadas pela metrica honesta:")
p("   " + big.sort_values("pct_atraso_honesto")[cols].to_string().replace("\n", "\n   "))

melhor_ing = tab.sort_values("pct_atraso_reportado").index[0]
melhor_rob = big.sort_values("pct_atraso_reportado").index[0]
melhor_hon = big.sort_values("pct_atraso_honesto").index[0]
p("")
p("  Melhor filial pelo ranking ingenuo (sem filtro de volume): {} ({} pedidos)".format(
    melhor_ing, tab.loc[melhor_ing, "pedidos"]))
p("  Melhor com >=200 pedidos pelo KPI de hoje: {}  ({}% reportado / {}% honesto)".format(
    melhor_rob, big.loc[melhor_rob, "pct_atraso_reportado"], big.loc[melhor_rob, "pct_atraso_honesto"]))
p("  Melhor com >=200 pedidos pela metrica honesta: {}  ({}% honesto)".format(
    melhor_hon, big.loc[melhor_hon, "pct_atraso_honesto"]))
sub = tab[tab.pedidos >= 50]
corr = sub[["pct_atraso_reportado", "pct_sem_registo"]].corr().iloc[0, 1]
p("")
p("  Correlacao (filiais com >=50 pedidos) entre KPI reportado e % sem registo: {:+.3f}".format(corr))
p("  Maior salto reportado -> honesto: " + ", ".join(
    "{} +{}pp".format(i, v) for i, v in big.salto_pp.sort_values(ascending=False).head(5).items()))

sem_med = base[~base.mensuravel]
venc = base[base.vencido_sem_registo]
p("")
p("  Pedidos nao cancelados SEM data de entrega: {:,}".format(len(sem_med)))
p("  Destes, com prazo estimado JA vencido no fim da janela: {:,} (100%)".format(len(venc)))
p("  -> atrasos quase certos que nao entram em nenhuma estatistica de SLA")
p("  Status desses pedidos: " + ", ".join(
    "{}={}".format(k, v) for k, v in venc.order_status.value_counts().items()))

# ------------------------------------------------------- 3 DEFEITOS MAIS GRAVES
p("")
p("=" * 78)
p("OS TRES DEFEITOS MAIS GRAVES + CUSTO ESTIMADO / ANO")
p("=" * 78)
MULTA_PCT = 0.035
REENTREGA = 180.0
dias_janela = (orders.order_purchase_timestamp.max() - orders.order_purchase_timestamp.min()).days
fator_ano = 365 / dias_janela
frete_med = deliv.frete_total.median()
p("Pressupostos comuns: multa {:.1f}% do frete (faixa 2-5% do enunciado), reentrega R${:.0f},".format(
    MULTA_PCT * 100, REENTREGA))
p("frete mediano por pedido R${:.2f}, janela = {} dias -> anualizacao {:.2f}x".format(
    frete_med, dias_janela, fator_ano))

n1 = len(venc)
custo1 = n1 * (MULTA_PCT * frete_med + REENTREGA) * fator_ano
p("")
p("[1] Data de entrega ausente em pedidos com prazo vencido (atraso invisivel)")
p("    Linhas afetadas: {:,} (de {:,} sem data de entrega)".format(n1, len(sem_med)))
p("    Decisao que estraga: ranking de filiais e apuracao de SLA. A filial que nao")
p("    registra a entrega aparece como a melhor; bonus e contrato premiam pior registo.")
p("    Custo/ano: {:,} x (3,5% x R${:.2f} + R${:.0f}) x {:.2f} = R$ {:,.0f}".format(
    n1, frete_med, REENTREGA, fator_ano, custo1))
p("    Pressuposto: cada prazo vencido sem registo e um atraso real com multa + reentrega.")

custo2 = ped_qd * (MULTA_PCT * frete_med) * fator_ano
p("")
p("[2] Duplicacao: {:,} linhas duplicadas exatas em geolocation ({:.1f}% do ficheiro)".format(
    n_geo_dup, n_geo_dup / len(geo) * 100))
p("    + {:,} quase-duplicados em order_items ({:,} pedidos)".format(int(qd.sum()), ped_qd))
p("    Decisao que estraga: calculo de frete e custo por rota. Contagem de itens e")
p("    centroide de CEP enviesados -> tabela de preco e faturamento errados.")
p("    Custo/ano (reemissao de frete dos pedidos afetados): R$ {:,.0f}".format(custo2))
p("    Pressuposto: 1 reemissao de frete por pedido afetado, ao valor da multa.")

n3 = (int(geo.geolocation_city.dropna().map(eh_capital_sp).sum())
      + int(cust.customer_city.dropna().map(eh_capital_sp).sum())
      + int(sellers.seller_city.dropna().map(eh_capital_sp).sum()))
custo3 = cidades_multi * 4 * (MULTA_PCT * frete_med + REENTREGA) * fator_ano
p("")
p("[3] Inconsistencia de nome de cidade: {} grafias so para sao paulo ({:,} linhas);".format(len(sp), n3))
p("    {:,} cidades com >=3 grafias".format(cidades_multi))
p("    Decisao que estraga: agrupamento por praca, escolha de hub e roteirizacao.")
p("    Sao Paulo vira varios mercados distintos; o volume real fica escondido.")
p("    Custo/ano: {:,} cidades x 4 entregas mal roteadas x (multa+reentrega) x {:.2f} = R$ {:,.0f}".format(
    cidades_multi, fator_ano, custo3))
p("    Pressuposto: 4 entregas/ano mal roteadas por cidade com grafia ambigua.")
p("")
p("TOTAL ESTIMADO DOS TRES DEFEITOS: R$ {:,.0f} / ano".format(custo1 + custo2 + custo3))

# ---------------------------------------------------------------------- notas
p("")
p("=" * 78)
p("NOTAS 1-5 POR DIMENSAO (5 = sem problema material)")
p("=" * 78)
notas = [
    ("Completude", 2, "{:,} pedidos nao cancelados sem data de entrega; {} produtos sem peso; "
                      "ausencia concentrada por filial".format(len(sem_entrega),
                                                               int(prods.product_weight_g.isna().sum()))),
    ("Unicidade", 2, "{:,} dups exatas em geolocation; {:,} quase-dups em order_items; "
                     "{:,} pedidos com avaliacao repetida".format(n_geo_dup, int(qd.sum()),
                                                                  revs[dup_rev].order_id.nunique())),
    ("Consistencia", 1, "{:,} grafias -> {:,} cidades; {} grafias de sao paulo; "
                        "{} categorias fora do dominio".format(int(geo.geolocation_city.nunique()),
                                                               int(gc.k.nunique()), len(sp), len(falta_tr))),
    ("Validade", 3, "{:,} entregas antes da coleta; {:,} prazos-limite antes da compra; "
                    "{} pesos <= 0".format(len(inv_carrier), len(inv_lim), n_peso0)),
    ("Exatidao", 2, "lead time ate {:.0f} dias; peso ate {:,.0f} g; {:,} pedidos com "
                    "pagamento != mercadoria+frete".format(deliv.lead_dias.max(),
                                                           prods.product_weight_g.max(), n_dif)),
    ("Atualidade", 2, "dataset com {:,} dias de idade; cauda dos ultimos 60 dias com {:.1f}% "
                      "sem entrega".format(idade, pct_cauda)),
    ("Proveniencia", 2, "vendedores de 1-10 pedidos e canais de pagamento tem taxas de ausencia "
                        "diferentes -> qualidade depende de quem registra"),
]
p("{:<14}{:<7}{}".format("Dimensao", "Nota", "Evidencia"))
for d, n, e in notas:
    p("{:<14}{}/5    {}".format(d, n, e))
p("")
p("Nota global (media): {:.1f}/5".format(np.mean([n for _, n, _ in notas])))
p("")
p("Veredito: os dados servem para ver tendencia, NAO para premiar filial nem fechar")
p("contrato de SLA. Corrigir completude e consistencia antes de qualquer ranking.")

rep = Path(__file__).resolve().parents[1] / "docs" / "E1-2_cartao_qualidade_saida.txt"
rep.parent.mkdir(exist_ok=True)
rep.write_text("\n".join(OUT), encoding="utf-8")
print("")
print("[relatorio gravado em {}]".format(rep))
