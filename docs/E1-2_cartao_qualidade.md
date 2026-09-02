# E1.2 — Cartão de Pontuação de Qualidade dos Dados

**Ficheiro analisado:** `archive_olist/` (9 CSV do dataset Olist Brazilian E-Commerce)
**Nota de escopo:** o enunciado pede `entregas.csv` (23 175 linhas). Esse ficheiro não existe
nesta máquina — a análise foi feita sobre os dados reais indicados pelo utilizador. Construiu-se
uma tabela "entregas" de **99 441 linhas** (1 linha = 1 pedido = 1 viagem), juntando
`orders` + `order_items` + `sellers` + `customers` + `products`.
**Filial** = UF do vendedor principal (origem do envio). **Origem do registo** = porte do
vendedor e canal de pagamento.
**Janela:** 2016-09-04 a 2018-10-17 (772 dias). **Data da análise:** 2026-09-02.
**Reprodutível com:** `python scripts/analise_qualidade_e12.py` → `docs/E1-2_cartao_qualidade_saida.txt`

---

## Cartão de pontuação

| Dimensão | O que verifiquei | Nota | Evidência (linhas) |
|---|---|:--:|---|
| **Completude** | Campos vazios por coluna e por filial | **2/5** | 2 965 pedidos sem `order_delivered_customer_date` (2,98%); **2 346 deles não estão cancelados**; 1 783 sem data de coleta; 791 produtos sem peso/dimensões; **775 pedidos sem nenhum item** (sem vendedor, sem frete, sem filial); 160 sem aprovação |
| **Unicidade** | Duplicados exatos e quase-duplicados | **2/5** | **261 831 linhas duplicadas exatas em `geolocation` (26,2% do ficheiro)**; 17 313 quase-duplicados em `order_items` (6 968 pedidos) idênticos em pedido+produto+vendedor+preço+frete, separados só por `order_item_id`; 814 `review_id` repetidos; 547 pedidos com >1 avaliação; 3 345 `customer_unique_id` repetidos |
| **Consistência** | Mesma entidade escrita de formas diferentes | **1/5** | 8 011 grafias de cidade → 5 939 cidades após normalizar; **12 grafias distintas da capital "São Paulo"** (`sao paulo`, `são paulo`, `sãopaulo`, `sp`, `sp / sp`, `sao paulo - sp`, `sao paulo / sao paulo`, `sao paulo sp`, `sao  paulo`, `sao paulop`, `sao pauo`, `são paulo`) em 176 971 linhas; 18 cidades com ≥3 grafias; 15 `seller_city` com UF dentro do próprio campo; 2 categorias de produto fora da tabela de domínio |
| **Validade** | Valores fora do domínio possível | **3/5** | 23 entregas ao cliente **antes** da coleta pela transportadora; 1 350 despachos antes da aprovação (lag negativo); 4 pesos ≤ 0 g; 9 pagamentos ≤ R$0; 3 `payment_type = not_defined`; 2 parcelas = 0 em cartão de crédito; 278 CEP de cliente sem correspondência em `geolocation` |
| **Exatidão** | Valores possíveis mas implausíveis | **2/5** | Lead time até **210 dias** (mediana 10,2; p99 46); 64 entregas > 100 dias; 1 produto de 40,4 kg; 73 pedidos com frete > 3× a mercadoria; 246 pedidos em que o pagamento ≠ mercadoria + frete |
| **Atualidade** | Atrasos entre o evento e o registo | **2/5** | Compra→aprovação: mediana 0,3 h, **máx 4 509 h (188 dias)**; aprovação→despacho: mediana 43,6 h, máx 3 018 h, **1 350 negativos**; avaliação→resposta: máx 12 449 h. Dataset com **2 876 dias de idade**; nos últimos 60 dias da janela, 3,5% dos pedidos sem entrega registada (cauda incompleta) |
| **Proveniência** | A qualidade varia por origem do registo? | **2/5** | **Sim, monotonicamente.** Vendedores de 1–10 pedidos: 4,40% sem data de entrega e 3,55% sem coleta. 11–100: 1,76% / 0,63%. 101–1 000: 1,51% / 0,37%. >1 000: **1,34% / 0,09%**. Quem registra menos volume registra pior — 33× mais buracos na data de coleta. Boleto tem 2,53% de ausência vs 2,32% do cartão |

**Nota global: 2,0/5.**

---

## Os três defeitos mais graves

Pressupostos comuns: multa **3,5% do frete** (meio da faixa 2–5% do enunciado) + **R$180** de
reentrega; frete mediano por pedido **R$17,17**; janela de 772 dias → fator de anualização **0,47×**.

| # | Defeito | Linhas afetadas | Que decisão isto estraga | Custo estimado / ano | Pressuposto usado |
|:--:|---|---|---|---|---|
| **1** | Data de entrega ausente em pedidos não cancelados — e **100% deles já com prazo estimado vencido** | **2 346** (1 107 `shipped`, 609 `unavailable`, 314 `invoiced`, 301 `processing`, 8 `delivered`) | Ranking de filiais e apuração de SLA. Atraso só é contável quando a data existe: a filial que não registra a entrega sobe no ranking. Bónus e renovação de contrato premiam o pior registo | **R$ 200 320** | Cada prazo vencido sem registo é um atraso real → multa 3,5% do frete + R$180 de reentrega |
| **2** | Duplicação: 261 831 linhas duplicadas exatas em `geolocation` (26,2%) + 17 313 quase-duplicados em `order_items` | **279 144** (6 968 pedidos) | Cálculo de frete e custo por rota. O centroide de CEP é calculado sobre linhas repetidas e a contagem de itens fica inflada → tabela de preço e faturação erradas | **R$ 1 980** | 1 reemissão de frete por pedido afetado, ao valor da multa (3,5% do frete) |
| **3** | Inconsistência de nome de cidade: 12 grafias da capital de São Paulo; 18 cidades com ≥3 grafias | **176 971** linhas de cidade "São Paulo" | Agrupamento por praça, escolha de hub e roteirização. "São Paulo" vira vários mercados distintos e o volume real fica escondido; `sp / sp` e `sbc/sp` não casam com nada | **R$ 6 148** | 4 entregas/ano mal roteadas por cidade com grafia ambígua |
| | | | | **Total: R$ 208 447/ano** | |

---

## Pergunta obrigatória

> *Olhando para a taxa de atraso por filial e para os dados em falta por filial — que filial parece
> a melhor, e você acredita nisso?*

**Parece a melhor:** GO — 3,99% de atraso, o menor entre as filiais com ≥200 pedidos.
(Sem filtro de volume o ranking devolve **PI com 0,00%**, sobre **12 pedidos** — ruído, não desempenho.)

**Não, não acredito.** Três razões:

1. **O KPI é calculado sobre o denominador errado.** "% atraso" hoje é
   `atrasos ÷ pedidos com data de entrega`. Os 2 346 pedidos sem data saem da conta — e todos os
   2 346 já tinham prazo estimado vencido. Recalculando com o denominador honesto
   (`(atrasos + prazos vencidos sem registo) ÷ todos os pedidos`), **GO sai do primeiro lugar**:

   | Filial | Pedidos | Sem registo | % atraso reportado | % atraso honesto | Salto |
   |---|--:|--:|--:|--:|--:|
   | PE | 405 | 2 | 4,47% | **4,94%** | +0,47 pp |
   | RS | 1 970 | 21 | 4,31% | 5,33% | +1,02 pp |
   | **GO** | 460 | 9 | **3,99%** | 5,87% | +1,88 pp |
   | BA | 566 | 17 | 5,83% | 8,66% | **+2,83 pp** |
   | RJ | 4 290 | 105 | 8,46% | 10,70% | +2,24 pp |

   GO cai de 1.º para 3.º; **PE** assume a liderança. BA e RJ, as filiais com mais buracos, são
   as que mais sobem — exatamente o padrão esperado quando a ausência de registo mascara atraso.

2. **A filial com pior qualidade de dados não aparece no ranking.** Há **775 pedidos sem nenhum
   item registado** — sem vendedor, sem frete, sem filial. 100% deles sem data de entrega. Essa
   "filial fantasma" tem 100% de atraso honesto e simplesmente não entra na tabela: nem premiada,
   nem punida, invisível.

3. **A qualidade do registo depende de quem registra, não do desempenho.** Vendedores de 1–10
   pedidos deixam 4,40% das entregas sem data; os de >1 000 pedidos, 1,34%. É 3,3× mais buraco na
   ponta pequena. Uma filial concentrada em vendedores pequenos parece pior por artefato de
   registo; uma concentrada em grandes parece melhor pelo mesmo motivo.

**O que eu faria antes de premiar qualquer filial:**
fechar o registo de entrega dos 2 346 pedidos vencidos, atribuir os 775 pedidos órfãos a uma
filial, e passar a reportar SLA com denominador = *todos* os pedidos, tratando "sem registo" como
falha até prova em contrário. Só então o ranking significa algo.

**Veredito geral:** estes dados servem para ver tendência. **Não** servem para premiar filial nem
para fechar contrato de SLA.
