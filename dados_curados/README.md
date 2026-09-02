# dados_curados/

Versões curadas da base Olist, geradas por [`scripts/curar_dados.py`](../scripts/curar_dados.py)
a partir de `archive_olist/`. Cada defeito tratado aqui está diagnosticado em
[`docs/E1-2_cartao_qualidade.md`](../docs/E1-2_cartao_qualidade.md).

```bash
python scripts/curar_dados.py --versao v1
```

## Versionamento

Uma pasta por versão: `v1/`, `v2/`, … A pasta é imutável depois de gerada — uma
correção de regra vira **uma nova versão**, nunca uma sobrescrita. Cada pasta traz
`MANIFEST.json` com o `sha256` de cada ficheiro de entrada e de saída, então qualquer
resultado é rastreável até os bytes exatos que o produziram.

| Versão | Data | O que mudou |
|---|---|---|
| `v1` | 2026-09-02 | Primeira curadoria: cidades canónicas, geolocalização deduplicada, 20 flags de qualidade por pedido, SLA com denominador honesto, UF conferida contra o CEP |

## O princípio

**A curadoria nunca apaga em silêncio.** As três consequências disso:

1. **Nenhuma linha sai da tabela principal.** `entregas_curado.csv` tem exatamente as
   99 441 linhas da entrada. `quarentena.csv` é um *extrato* para alguém corrigir, não
   um destino onde a linha desaparece.
2. **Toda correcção deixa rastro.** Peso ≤ 0 vira nulo, mas o valor original fica em
   `peso_g_original` e a linha ganha `qa_peso_invalido`. São 20 colunas `qa_*`, e 85,9% dos pedidos saem sem nenhuma.
3. **Implausível não é corrigido, é marcado.** Um lead time de 210 dias pode ser real.
   `qa_lead_implausivel` diz que alguém precisa olhar; o valor continua lá.

## Ficheiros

Separador `;` e UTF-8 com BOM — abrem no Excel português sem passo de importação,
como pede o enunciado do exercício.

| Ficheiro | Linhas | O que é |
|---|--:|---|
| `entregas_curado.csv` | 99 441 | 1 linha = 1 pedido, 56 colunas. Datas, valores, filial e cidade canónicas, proveniência, SLA nos dois denominadores, 20 flags `qa_*` |
| `geolocation_cep_curado.csv` | 19 010 | 1 linha = 1 prefixo de CEP. Centroide calculado **sem** os 261 831 duplicados exatos e **sem** as 38 coordenadas fora do Brasil. `correcao_centroide_m` diz de quanto o centroide bruto estava errado |
| `cidades_mapa.csv` | 8 617 | Grafia original → canónica, com a regra aplicada e se a mudança foi estrutural ou só de formatação |
| `cidades_suspeitas.csv` | 73 | Prováveis erros de digitação (`Sao Pauo` ~ `São Paulo`, distância de edição 1). **Não unidos automaticamente** — a decisão é humana |
| `geolocation_fora_br.csv` | 38 | Coordenadas fora dos limites do Brasil, preservadas para investigação |
| `quarentena.csv` | 1 391 | Pedidos com violação dura de domínio, com `motivo_quarentena` |
| `MANIFEST.json` | — | Versão, data, hashes, contagens antes/depois, parâmetros e as regras aplicadas |
| `curadoria.log` | — | Saída completa da execução que gerou a pasta |

## As correcções, uma por defeito do cartão

| Dimensão | O que a curadoria faz |
|---|---|
| **Consistência** | Chave de cidade por NFC + sem acento + sem sufixo de UF, agrupada por UF. Canónica = a grafia acentuada mais frequente do grupo, capitalizada. Colapsa as 9 grafias regulares de São Paulo — incluindo a que só difere nos bytes (`a` + til combinante vs `ã`). Aliases (`sp`, `sbc`, `rj`, `bh`) numa tabela curta e explícita no script, aplicados só quando a UF confirma |
| **Unicidade** | 261 831 duplicados exatos removidos de `geolocation` **antes** de calcular o centroide — 80,5% dos prefixos de CEP eram afetados. Quase-duplicados de `order_items` ficam marcados: duas unidades do mesmo item ou lançamento duplicado é decisão de negócio, e o ficheiro não diz qual |
| **Completude** | Os 775 pedidos sem vendedor recebem `filial = 'SEM_FILIAL'` em vez de sumirem no `GROUP BY`. Ausências marcadas em `qa_sem_dt_entrega`, `qa_sem_dt_coleta`, `qa_sem_peso`, `qa_sem_item` |
| **Validade** | Peso ≤ 0 → nulo, original preservado. Marcadas: entrega antes da coleta, lag de despacho negativo, pagamento ≤ 0, canal indefinido, CEP sem geolocalização. Coordenadas fora do Brasil excluídas do centroide |
| **Validade da chave** | A UF declarada é conferida contra a UF do prefixo de CEP. **35 vendedores declaram uma UF que o CEP contradiz** — 581 pedidos. A `filial` não é sobrescrita; a UF derivada do CEP fica em `filial_uf_cep`, com `qa_uf_filial_incoerente` |
| **Exatidão** | Marcado, nunca corrigido: lead time acima de 100 dias, peso acima de 30 kg, frete acima de 3× a mercadoria, pagamento que não fecha com mercadoria + frete. Os limiares são julgamento, não calibração — estão no topo do script |
| **Atualidade** | `qa_cauda_incompleta` marca os 1 503 pedidos dos últimos 60 dias da janela, onde 3,5% ainda não tinham entrega registada. Impede ler a cauda de corte como piora de desempenho |
| **Proveniência** | `origem_registo` traz o porte do vendedor que deu baixa no despacho (`1-10 pedidos` … `>1000`, `SEM_VENDEDOR`) — a variável que explica por que a ausência de registo varia 39× entre pontas |

## O que muda na prática: o denominador do SLA

O defeito nº 1 do cartão está resolvido no próprio esquema. Três colunas em vez de uma:

- `atraso_mensuravel` — existe data de entrega?
- `atrasado` — entregou depois do prazo. Nulo quando não há data. **É o indicador de hoje.**
- `vencido_sem_registo` — pedido ativo, sem data e com prazo já vencido.
- `atraso_honesto` — `atrasado` quando há data; `vencido_sem_registo` quando não há.

```
% atraso reportado   8,11%   sobre 96 470 pedidos (só quem tem data)
% atraso honesto    10,29%   sobre 98 816 pedidos (todos os ativos)
```

Os 2 346 pedidos de diferença são os que faziam GO parecer a melhor filial.
Para reproduzir o ranking honesto, agrupe por `filial` e use `atraso_honesto`.

Refazendo o ranking por `filial_uf_cep` em vez de `filial`, a ordem praticamente não
muda — PE continua em primeiro com 4,94% e GO em terceiro com 5,87%; SC melhora de
7,28% para 7,05%. **A UF incoerente é um defeito real e não derruba a conclusão do
cartão.** Vale registá-lo por isso mesmo: foi verificado, não presumido.

## Nota sobre tamanho

`entregas_curado.csv` tem 53 MB. O `MANIFEST.json` guarda o `sha256` das entradas e das
saídas, então o ficheiro é reproduzível e verificável a partir de `archive_olist/` —
versioná-lo no git é opcional.
