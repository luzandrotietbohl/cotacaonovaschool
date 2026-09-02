# Painel de Gestão do Cotador — Design

Data: 2026-09-01
Status: aprovado pelo usuário (brainstorming em sessão)

## Objetivo

Interface web local para gerir o agente de cotação de frete por email: monitorar a
operação, tratar a fila de revisão humana, cotar manualmente e controlar o loop do
agente — sem abrir terminal nem Gmail para o dia a dia.

## Decisões de produto (validadas com o usuário)

| Decisão | Escolha |
|---|---|
| Escopo | Monitoramento + fila de revisão + cotação manual + controle do agente |
| Acesso | Somente local (localhost), usuário único, sem login |
| Stack | Flask + templates Jinja, server-rendered, sem build de frontend |
| Execução do agente | O painel embute o loop numa thread de fundo (um processo só) |
| Layout | Menu lateral fixo com 4 páginas |
| Estilo | Tema escuro operacional (sala de controle) |
| Revisão | Ver detalhes + botão "devolver à fila"; resposta continua no Gmail |
| Cotação manual | Formulário estruturado; não usa a Claude API |

## Arquitetura

Novo pacote `painel/` no mesmo repositório. Ponto de entrada: `python main.py --painel`
(sobe o Flask em `http://localhost:8000` com o loop **desligado** e em **modo rascunho**).

```
painel/app.py             fábrica do app Flask + rotas
painel/servico_agente.py  thread do loop: ligar/desligar, status, último ciclo
painel/consultas.py       leituras do SQLite para as telas
painel/templates/         base.html (sidebar) + visao_geral, revisao, cotar, agente
painel/static/estilo.css  tema escuro, sem framework CSS
```

O painel não duplica lógica de negócio: cotação manual usa `precificacao` +
`TabelaTarifas`; o loop chama `Agente.rodar_ciclo()`; leituras vêm do SQLite existente.

## Páginas

### Visão geral
- Cartões do dia: cotados, para revisar, aguardando dados, sem rota.
- Tabela dos últimos 50 processados: quando, remetente, assunto, rota (origem→destino),
  desfecho, valor do frete.
- Auto-atualização a cada 10s via polling de `GET /api/status` (JSON com contadores).

### Revisão
- Lista das threads com label `cotador-revisar`, mais recente primeiro.
- Por item: remetente, assunto, motivo (campo `erro` ou desfecho), extração do LLM
  formatada, data.
- Botão **Devolver à fila** por item: apaga o(s) registro(s) da thread no SQLite,
  remove o label no Gmail e marca o email como não lido; o agente reprocessa no
  próximo ciclo. Confirmação antes de executar.

### Cotar
- Formulário: origem, destino, quantidade de volumes, valor da NF, peso total (kg),
  modal (auto/rodoviário/aéreo).
- Resultado: composição idêntica à do email (frete por volume, pedágio, GRIS/advalorem,
  taxa entrega difícil, frete mínimo quando acionado, prazo) ou o motivo de não cotar
  (rota inexistente, sem tarifa vigente, peso acima do limite).
- Requer credenciais do Sheets configuradas; tarifas carregadas sob demanda com cache
  no processo.

### Agente
- Ligar/desligar o loop (thread com `threading.Event`); estado atual visível.
- "Rodar 1 ciclo agora" (síncrono, mostra o resumo do ciclo).
- Intervalo entre ciclos configurável na tela (default: o do `.env`).
- Seletor de modo: rascunho / enviar (aplica ao processo em execução).
- Resultado do último ciclo (contagem por desfecho) e última exceção, se houver.
- Alerta em destaque quando `ALERTA_CREDENCIAL.txt` existe, com o conteúdo.

## Mudanças no código existente

1. **`cotador/integracoes/banco.py`** — nova coluna `label TEXT` em `processados`
   (migração: `ALTER TABLE ... ADD COLUMN` se ausente). Hoje o desfecho não distingue
   o que foi para revisão. Novas consultas: contadores por desfecho no dia, últimos N,
   listagem por label, remoção por thread.
2. **`cotador/agente.py`** — `_fechar` e o registro de erro passam o label aplicado ao
   `banco.registrar` (o label já é conhecido no ponto de chamada).
3. **`cotador/integracoes/caixa_imap.py`** — `devolver_para_fila(id_email)`: busca o
   UID por `X-GM-MSGID`, remove o label (`-X-GM-LABELS`) e restaura não-lido
   (`-FLAGS \Seen`).
4. **`main.py`** — flag `--painel` (porta opcional `--porta`, default 8000).
5. **`requirements.txt`** — adicionar `flask>=3.0`.

## Concorrência e erros

- Loop em `threading.Thread` daemon; parada cooperativa via `threading.Event` checado
  entre ciclos e durante a espera do intervalo.
- SQLite: conexão por operação (padrão já existente) — sem conflito web × loop.
- Exceção num ciclo não derruba o painel: fica registrada e visível na página Agente.
- `CredencialInvalida` desliga o loop (como hoje) e o painel exibe o alerta.
- Estado inicial seguro: loop desligado, modo rascunho.

## Testes (unittest, sem rede, sem credenciais)

- `consultas.py` e novas funções do `Banco` com SQLite temporário (inclui migração de
  banco antigo sem a coluna `label`).
- Rotas do painel via `test_client` do Flask, com fakes de `TabelaTarifas`, `CaixaIMAP`
  e `Agente` (mesmo padrão de fakes dos testes atuais).
- `servico_agente.py`: liga/desliga, ciclo único, captura de exceção, efeito de
  `CredencialInvalida`.
- Página Cotar reproduz o caso da aba EXEMPLO_CALCULO (R$ 252,50).
- Suíte existente (65 testes) permanece verde.

## Fora de escopo (desta versão)

- Login/HTTPS/acesso remoto.
- Responder ou editar emails pelo painel (fica no Gmail, via rascunhos).
- Edição da planilha de tarifas.
- Extração por texto livre (LLM) no painel.
