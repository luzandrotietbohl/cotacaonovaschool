# Agente de Cotacao de Frete por Email

Le a caixa do Gmail por IMAP, identifica pedidos de cotacao, extrai os dados com a
Claude API, busca a rota numa planilha do Google Sheets e responde o cliente por
SMTP. Quando faltam dados, responde pedindo o que falta e cota na resposta seguinte.

## Modelo de preco

Vem da aba `DICIONARIO` da planilha. O frete e cobrado **por volume**, nao por peso
nem por cubagem:

```
MAX(QTD_VOLUMES * (VALOR_POR_VOLUME + PEDAGIO_POR_VOLUME), FRETE_MINIMO)
  + VALOR_NF * (GRIS_PERCENTUAL + ADVALOREM_PERCENTUAL) / 100
  + TAXA_ENTREGA_DIFICIL
```

Peso serve apenas para validar `PESO_MAXIMO_VOLUME_KG` da rota. Se o peso medio por
volume estourar o limite, o agente **nao responde** e marca a thread para revisao.

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
      -> rota sem tarifa vigente .. label cotador-revisar, nada e enviado
      -> faltam dados ............. responde pedindo, label cotador-aguardando-dados
      -> completo, confianca < 0.6  label cotador-revisar (nao emite preco)
      -> peso acima do limite ..... label cotador-revisar, nada e enviado
      -> completo ................. calcula e responde com a composicao + prazo
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
python main.py --cotar "Sao Paulo/SP" "Campinas/SP" 10 8000
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

Se algum email falhar por erro tecnico (label `cotador-revisar`), devolva-o a fila:

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

65 testes, sem rede e sem credenciais. Cobrem o calculo, a mesclagem de thread, os
templates de email, a busca de rota e as regressoes conhecidas.

`TestExemploCalculoDaPlanilha` reproduz a aba EXEMPLO_CALCULO: 10 volumes, NF
R$ 8.000, rota R00001 -> total R$ 252,50.

## Seguranca operacional

- `MODO_RESPOSTA=rascunho` grava a resposta em `[Gmail]/Rascunhos` e **nao envia**;
  `enviar` responde o cliente de verdade. Comece em rascunho.
- Nunca responde a `noreply@`, `mailer-daemon` ou a propria conta — evita loop de robos.
- Cada email so e processado uma vez (`X-GM-MSGID` como chave no SQLite + label).
- O historico citado e cortado antes de ir ao LLM, para nao cotar dados antigos.
- Um email problematico nao derruba o ciclo: falha isolada, os demais seguem.
- `.env` e `service_account.json` estao no `.gitignore`. Nunca versione os dois.

## Estrutura

```
main.py                             CLI e tratamento de alerta
cotador/config.py                   configuracao via .env
cotador/agente.py                   orquestracao e decisao de desfecho
cotador/core/modelos.py             dataclasses do dominio
cotador/core/extracao.py            Claude API com tool_choice forcado
cotador/core/precificacao.py        formula da planilha + limite de peso
cotador/core/mensagens.py           templates dos emails
cotador/integracoes/caixa_imap.py   leitura, labels e rascunhos por IMAP
cotador/integracoes/email_smtp.py   envio das respostas
cotador/integracoes/mime.py         decodificacao de email cru
cotador/integracoes/planilha.py     TABELA_ROTAS -> Tarifa normalizada
cotador/integracoes/google_sa.py    credencial da conta de servico
cotador/integracoes/banco.py        SQLite (idempotencia + auditoria)
```
