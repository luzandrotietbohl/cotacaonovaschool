# Agente de Cotacao de Frete por Email

Le a caixa do Gmail por IMAP, identifica pedidos de cotacao, extrai os dados com a
Claude API, valida a rota numa planilha do Google Sheets e responde o cliente por
SMTP. O preço padrão é o P50 de um modelo histórico Olist; a planilha continua
definindo cobertura, modal, prazo e restrições operacionais.

## Modelo de preço histórico

O modelo agrega o Olist por `order_id + seller_id`, aproximando cada linha de um
embarque com uma origem, um destino e uma ou mais unidades. O alvo é a soma de
`freight_value`. As features são distância, peso total, quantidade, valor declarado,
mês, UFs, rota, mesma UF/interestadual e perfil capital/interior.

Três CatBoosts quantílicos estimam P25, P50 e P75. P50 é o preço enviado ao cliente.
Um IsolationForest sem preço detecta cargas fora do domínio; nesses casos o agente
encaminha para revisão humana. A inferência usa os artefatos em
`modelos/olist/atual/` e não depende do ZIP Olist.

Treino atual: 97.311 embarques, com 70% para treino, 15% para calibração e 15% para
teste temporal. Resultados: MAE P50 de R$ 5,62, mediana absoluta de R$ 2,11,
mediana percentual de 12,10% e cobertura P25-P75 de 47,42%.

`scripts/clusterizar_olist.py` segmenta os mesmos embarques com KMeans para expor os
regimes logisticos da base. O frete fica fora das features de proposito: entra apenas
no perfil dos grupos. Aceita o ZIP Olist ou uma pasta de CSVs e grava CSV de rotulos,
perfil, figura, metadata e relatorio em `relatorios/clusterizacao/`. Com k=6 saem seis
regimes: local metropolitano, miudo de baixo valor, denso compacto, volumoso leve,
multi-item e carga pesada. E um recorte operacional, nao estrutura latente comprovada
- a silhueta fica em torno de 0,22 porque os dados sao um continuo log-normal.

```bash
python scripts/treinar_olist.py --zip caminho/para/archive_olist.zip
python scripts/gerar_tsne.py --zip caminho/para/archive_olist.zip
python scripts/clusterizar_olist.py --dados caminho/para/archive_olist
python main.py --modelo-info
```

O preço anterior por tabela permanece como fallback (`PRECIFICADOR=tabela`):

```
MAX(QTD_VOLUMES * (VALOR_POR_VOLUME + PEDAGIO_POR_VOLUME), FRETE_MINIMO)
  + VALOR_NF * (GRIS_PERCENTUAL + ADVALOREM_PERCENTUAL) / 100
  + TAXA_ENTREGA_DIFICIL
```

No modo histórico, peso é feature obrigatória e continua validando
`PESO_MAXIMO_VOLUME_KG` da rota.

Dados obrigatorios no email do cliente: origem, destino, quantidade de volumes,
valor da nota fiscal e (se `EXIGIR_PESO=true`) peso total.

## Credenciais: sem OAuth

O projeto nao usa OAuth de usuario. A primeira versao usava, e o Google desabilitou
o OAuth client: app nao verificado, escopo restrito de Gmail e envio automatico e
um padrao que o antiabuso derruba. Alem disso, em modo *Testing* o refresh token
expira a cada 7 dias, exigindo reautorizacao manual toda semana.

| Funcao | Canal | Credencial |
|---|---|---|
| Ler a caixa, labels, rascunhos | IMAP (`imap.gmail.com`) | senha de app |
| Enviar a resposta | SMTP (`smtp.gmail.com`) | a mesma senha de app |
| Ler a tabela de tarifas | Sheets API | conta de servico |

Nenhuma das tres expira sozinha nem precisa de verificacao do Google.

A leitura usa as extensoes IMAP do Gmail, que preservam o que a API oferecia:
`X-GM-RAW` (busca com a sintaxe do Gmail), `X-GM-MSGID` (id estavel para
idempotencia), `X-GM-THRID` (conversa) e `X-GM-LABELS` (labels).

## Curadoria da tabela de tarifas

A tabela e editada a mao pelo comercial e recarregada a cada ciclo: um erro de
digitacao entra em producao em dois minutos. Tres camadas cuidam disso.

**Limites duros** (`cotador/core/curadoria.py`). Intervalos declarados por
pessoas, nao estatistica. Duas severidades, porque as consequencias diferem:

| | O que e | O que acontece |
|---|---|---|
| **BLOQUEIO** | valor implausivel para o negocio | a linha entra em quarentena e para de cotar |
| **ALERTA** | valor incomum, possivelmente legitimo | relata e segue cotando |

O caso que a camada existe para pegar: `GRIS 0,30` digitado como `30` multiplica
por 100 a parcela cobrada sobre a nota fiscal. Tambem bloqueia duas tarifas
vigentes para o mesmo trecho e modal — `buscar` devolveria a primeira da lista,
e o preco do cliente dependeria da ordem das linhas na planilha.

Alertas sao silenciaveis: preencha `REVISADO_POR` e `REVISADO_EM` na linha (as
colunas sao opcionais; sem elas nenhuma linha esta revisada). **Bloqueio nunca
e silenciavel** — um GRIS de 30% nao e uma tarifa a autorizar, e um 0,30
digitado errado. Corrige-se na planilha.

**Quarentena** (`MODO`: `AUDITORIA_BLOQUEIA`). A linha bloqueada sai de `rotas`
e nao cota, mas continua em `trecho_cadastrado`. E isso que garante que o
cliente **nunca ouca «nao atendemos» por causa de um erro nosso**: a thread vai
para `cotador-revisar` com o motivo em portugues. Comece com
`AUDITORIA_BLOQUEIA=false` para ver o que a tabela real viola sem tirar nada
do ar.

**Versionamento** (tabela `versoes_tabela` no SQLite). Cada carga da planilha e
guardada por hash de conteudo, com um snapshot dos campos que mexem no preco.
Cada cotacao grava a `Tarifa` inteira em `tarifa_json`, nao so o `id_rota`:
depois que o comercial corrigir a planilha, a cotacao enviada continua
reconstruivel. Quando o conteudo muda, o agente loga campo a campo o que mudou.

Isto e o que pega o erro **plausivel**. `16,65 -> 166,50` qualquer limite pega;
`16,65 -> 18,65` nao viola faixa nenhuma e cotaria errado para sempre — so
aparece na comparacao com a versao anterior.

```bash
python main.py --auditar-planilha
```

Imprime bloqueios, alertas e o diff contra a versao anterior. Sai com codigo 1
quando ha bloqueio, para virar tarefa agendada.

## Memoria da thread

O cliente responde so o campo que faltava ("valor da nota R$ 200"), e o historico
citado e cortado antes de ir ao LLM. Por isso o agente recupera do SQLite a ultima
extracao da mesma `thread_id` e mescla (`PedidoCotacao.mesclar`): o que o cliente
acabou de dizer vence, os vazios sao preenchidos com o que a thread ja tinha, e a
confianca e a maior das duas — uma resposta curta e curta, nao duvidosa.

Sem isso o agente pediria os mesmos dados para sempre e nenhuma cotacao fecharia.

## Dois cortes de confianca

Os erros custam diferente, entao ha dois limites (`cotador/agente.py`):

- **0,35** para responder pedindo dados. Um pedido vago ("quanto fica um frete de SP
  para Vitoria?") sai do LLM com confianca ~0,55: e cotacao de verdade, so incompleta.
  Pedir dados a um falso positivo e inofensivo.
- **0,60** para emitir preco. Mandar valor errado ao cliente e caro, entao o corte
  sobe justamente no ponto em que um numero sairia da porta.

## Fluxo

```
IMAP (nao lidos)
   -> Claude API: e cotacao? extrai origem, destino, volumes, valor NF, peso
      -> nao e cotacao ............ label cotador-processado, nada e enviado
      -> confianca < 0.35 ......... label cotador-revisar (revisao humana)
      -> rota fora da planilha .... responde "nao atendemos", label cotador-sem-rota
      -> rota sem tarifa vigente .. responde "em analise", label cotador-revisar
      -> rota em quarentena ....... responde "em analise", label + motivo
      -> faltam dados ............. responde pedindo, label cotador-aguardando-dados
      -> completo, confianca < 0.6  label cotador-revisar (nao emite preco)
      -> peso acima do limite ..... responde "em analise", label cotador-revisar
      -> fora do dominio historico  responde "em analise", label cotador-revisar
      -> completo ................. calcula P25/P50/P75 e responde P50 + prazo
   -> registra tudo em SQLite (idempotencia + auditoria)
```

A rota e checada **antes** de pedir dados faltantes: nao adianta perguntar o peso de
uma carga para um trecho que nao atendemos.

Somente rotas com `STATUS=ATIVO` e dentro da janela `VIGENCIA_INICIO..VIGENCIA_FIM`
sao cotadas. Se o trecho **existe** na planilha mas nao tem tarifa vigente, o agente
**nao diz ao cliente que nao atendemos** — manda para revisao humana, porque isso e
falha de cadastro, nao ausencia de rota.

Havendo `RODOVIARIO` e `AEREO` no mesmo trecho, usa o modal que o cliente pediu; sem
pedido explicito, prefere `RODOVIARIO`.

## Instalacao

```bash
python -m pip install -r requirements.txt
```

```bash
copy .env.example .env
```

Preencha no `.env`:

- `ANTHROPIC_API_KEY` — chave de workspace do console da Anthropic
- `SMTP_USUARIO` — a conta Gmail monitorada
- `SMTP_SENHA_APP` — senha de app de 16 caracteres, gerada em
  https://myaccount.google.com/apppasswords (exige verificacao em duas etapas ativa)
- `SHEET_ID` — o trecho do link `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`

E crie a conta de servico para a planilha:

1. Google Cloud Console: novo projeto, habilite a **Google Sheets API**
2. IAM > Contas de servico > criar (nenhum papel de IAM e necessario)
3. Aba Chaves > adicionar chave > JSON; salve como `service_account.json` na raiz
4. Compartilhe a planilha com o email da conta de servico, como **Leitor**

## Uso

```bash
python main.py --testar-imap
```

```bash
python main.py --testar-smtp
```

```bash
python main.py --validar-planilha
```

```bash
python main.py --auditar-planilha
```

```bash
python main.py --cotar "Sao Paulo/SP" "Campinas/SP" 10 8000 --peso 300
```

Aceites e rejeições ficam no SQLite, separados dos emails processados:

```bash
python main.py --confirmar Q-XXXXXXXXXXXX 225.00 --custo 180.00
python main.py --rejeitar Q-XXXXXXXXXXXX --notas "cliente considerou caro"
```

```bash
python main.py --testar-texto "10 volumes de Sao Paulo/SP para Campinas/SP, NF 8000, 300kg"
```

```bash
python main.py --once
```

```bash
python main.py --loop
```

## Painel de gestao

```bash
python main.py --painel
```

Sobe a interface web local de gestao em http://localhost:8000 (use `--porta` para
trocar). O painel mostra a operacao do dia, a fila de revisao humana (com botao
que devolve a fila os emails em revisao daquela thread), cotacao manual sem
gastar API e o controle do loop (ligar/desligar, ciclo unico, modo
rascunho/enviar). Inicia sempre com o loop desligado e em modo rascunho.

A cotacao manual do painel cota mesmo quando o peso estoura o limite da rota,
exibindo um alerta — de proposito, para o humano ver o numero e decidir. Por
email o agente **nao** responde esse caso: manda para revisao.

## A fila de revisao humana

Todo desfecho `erro` recebe o label `cotador-revisar` e uma linha no SQLite com o
motivo. **O email fica nao-lido de proposito**: a busca do agente e `is:unread`,
entao marcar como lido tirava o email da caixa e da fila ao mesmo tempo — e
`--reprocessar-erros` apagava o registro sem que a busca voltasse a encontra-lo.
A dedupe por `X-GM-MSGID` e que impede o reprocessamento em loop.

O cliente **e avisado** quando a falha nao e dele — peso acima do limite da rota,
tarifa em quarentena ou sem vigencia: recebe uma mensagem dizendo que o pedido
esta em analise humana, sem o motivo interno. Abaixo de 0,35 de confianca o
agente **nao responde**: nesse ponto ele nao sabe o que o email pede, e mandar um
formulario de cotacao para uma reclamacao e pior que o silencio.

O painel e a tela para trabalhar a fila. O comando abaixo e a versao para
agendador: agrupa por motivo e sai com codigo 1 quando ha qualquer item.

```bash
python main.py --resumo-revisar
```

Quantos esperam, agrupados por motivo, ha quanto tempo, e os 20 mais antigos.
Sai com codigo 1 quando ha qualquer item, para virar tarefa agendada — o label
existia desde o inicio, mas nada avisava ninguem, e fila que nao avisa e
deposito.

Corrigido o que causou a falha, devolva os emails a fila do agente:

```bash
python main.py --reprocessar-erros
```

Codigos de saida: `0` ok, `1` falha de dados, `2` credencial recusada. No codigo 2 o
agente grava `ALERTA_CREDENCIAL.txt` na raiz e **para** o loop — insistir nao resolve
credencial ruim, e girar em silencio esconde o problema.

## Testes

```bash
python -m unittest discover -s cotador/tests -t . -v
```

215 testes, sem rede e sem credenciais. Cobrem o calculo, a mesclagem de
thread, os templates de email, a busca de rota, a curadoria da tabela, a fila de
revisao humana, as rotas do painel, a clusterizacao e as regressoes conhecidas.

`TestExemploCalculoDaPlanilha` reproduz a aba EXEMPLO_CALCULO: 10 volumes, NF
R$ 8.000, rota R00001 -> total R$ 252,50.

## Seguranca operacional

- `MODO_RESPOSTA=rascunho` grava a resposta em `[Gmail]/Rascunhos` e **nao envia**;
  `enviar` responde o cliente de verdade. Comece em rascunho.
- Nunca responde a `noreply@`, `mailer-daemon` ou a propria conta — evita loop de robos.
- Cada email so e processado uma vez (`X-GM-MSGID` como chave no SQLite + label).
- Se o projeto estiver em pasta sincronizada (Google Drive/OneDrive), configure
  `BANCO_CAMINHO` no `.env` apontando para disco local (ex.:
  `%LOCALAPPDATA%\cotador\cotador.sqlite3`) — clientes de sync corrompem
  gravacoes do SQLite e perder linhas ali quebra a idempotencia.
- O historico citado e cortado antes de ir ao LLM, para nao cotar dados antigos.
- Um email problematico nao derruba o ciclo: falha isolada, os demais seguem.
- Todo email do agente diz que foi enviado automaticamente e como falar com uma
  pessoa. E o recurso de quem recebe: sem isso o cliente nao sabe que foi
  atendido por um sistema, nem a quem recorrer.
- `.env` e `service_account.json` estao no `.gitignore`. Nunca versione os dois.
- O painel escuta so em `127.0.0.1` (nao fica exposto na rede), protege cada POST
  com um token anti-CSRF gerado por processo e sobe com o loop desligado e em modo
  rascunho, independentemente do `MODO_RESPOSTA` do `.env`.

## Estrutura

```
main.py                             CLI e tratamento de alerta
cotador/config.py                   configuracao via .env
cotador/core/curadoria.py           limites duros, quarentena e diff de versoes
cotador/agente.py                   orquestracao e decisao de desfecho
cotador/core/modelos.py             dataclasses do dominio
cotador/core/extracao.py            Claude API com tool_choice forcado
cotador/core/precificacao.py        formula da planilha + limite de peso
cotador/ml/historico.py             inferencia P25/P50/P75 e bloqueio de outlier
cotador/ml/geografia.py             resolucao por CEP ou cidade/UF
cotador/ml/treinamento.py           agregacao order+seller, treino e calibracao
cotador/ml/clusterizacao.py         segmentacao KMeans dos embarques e relatorio
scripts/treinar_olist.py            CLI reproduzivel de treinamento
scripts/gerar_tsne.py               visualizacao exploratoria e outliers de preco
scripts/clusterizar_olist.py        CLI da clusterizacao e do relatorio
modelos/olist/atual/                modelos, mapa geografico e metadados
cotador/core/mensagens.py           templates dos emails
cotador/integracoes/caixa_imap.py   leitura, labels e rascunhos por IMAP
cotador/integracoes/email_smtp.py   envio das respostas
cotador/integracoes/mime.py         decodificacao de email cru
cotador/integracoes/planilha.py     TABELA_ROTAS -> Tarifa normalizada
cotador/integracoes/google_sa.py    credencial da conta de servico
cotador/integracoes/banco.py        SQLite (idempotencia + auditoria)
painel/app.py                       rotas e telas do painel web
painel/servico_agente.py            loop do agente em thread (liga/desliga)
painel/consultas.py                 leituras do SQLite para as telas
```
