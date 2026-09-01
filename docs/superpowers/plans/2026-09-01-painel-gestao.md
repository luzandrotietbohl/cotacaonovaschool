# Painel de Gestão do Cotador — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Interface web local (Flask, tema escuro) para monitorar o agente de cotação, tratar a fila de revisão, cotar manualmente e ligar/desligar o loop — um processo só via `python main.py --painel`.

**Architecture:** Novo pacote `painel/` (app Flask server-rendered + thread de fundo que chama `Agente.rodar_ciclo()`). O painel não duplica lógica de negócio: lê o SQLite existente, usa `precificacao`/`TabelaTarifas` para cotar e `CaixaIMAP` para devolver threads à fila. Mudanças cirúrgicas no código existente: coluna `label` no banco, `devolver_para_fila` no IMAP, flag `--painel` no CLI.

**Tech Stack:** Python 3.12, Flask 3 + Jinja, SQLite (stdlib), threading (stdlib), unittest (sem rede, sem credenciais).

**Spec:** `docs/superpowers/specs/2026-09-01-painel-gestao-design.md`

**Comandos de teste** (rodar sempre da raiz do repo):
- Suíte inteira: `python -m unittest discover -s cotador/tests -t . -v`
- Um arquivo: `python -m unittest cotador.tests.test_banco -v`

---

## Estrutura de arquivos

```
Modificar: cotador/integracoes/banco.py       coluna label + consultas novas
Modificar: cotador/agente.py                  passa o label ao registrar
Modificar: cotador/integracoes/caixa_imap.py  devolver_para_fila
Modificar: main.py                            flag --painel e --porta
Modificar: requirements.txt                   + flask
Criar:     painel/__init__.py
Criar:     painel/servico_agente.py           thread do loop (liga/desliga/status)
Criar:     painel/consultas.py                dados prontos para as telas
Criar:     painel/app.py                      fábrica Flask + rotas
Criar:     painel/templates/base.html         sidebar escura
Criar:     painel/templates/visao_geral.html
Criar:     painel/templates/revisao.html
Criar:     painel/templates/cotar.html
Criar:     painel/templates/agente.html
Criar:     painel/static/estilo.css           tema escuro
Criar:     cotador/tests/test_banco.py        banco + label no agente
Criar:     cotador/tests/test_painel.py       serviço, consultas e rotas Flask
```

---

### Task 1: Coluna `label` no banco (com migração)

Hoje o SQLite não diz quais linhas foram para revisão humana (o desfecho `erro` não distingue de outros). A coluna `label` guarda o label Gmail aplicado ao fechar o email.

**Files:**
- Modify: `cotador/integracoes/banco.py`
- Test: `cotador/tests/test_banco.py` (novo)

- [ ] **Step 1: Write the failing tests**

Criar `cotador/tests/test_banco.py`:

```python
"""Testes do SQLite: coluna label, migracao e consultas do painel."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cotador.integracoes.banco import Banco


class BaseComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.caminho = Path(self._tmp.name) / "teste.sqlite3"
        self.banco = Banco(self.caminho)

    def registrar(self, **over):
        base = dict(
            id_email="msg-1",
            thread_id="thr-1",
            remetente="cliente@acme.com",
            assunto="Cotacao",
            desfecho="cotado",
            label="cotador-processado",
        )
        base.update(over)
        self.banco.registrar(**base)


class TestColunaLabel(BaseComBanco):
    def test_registrar_guarda_o_label(self):
        self.registrar(label="cotador-revisar")
        con = sqlite3.connect(self.caminho)
        try:
            valor = con.execute(
                "SELECT label FROM processados WHERE id_email = 'msg-1'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(valor, "cotador-revisar")

    def test_label_e_opcional(self):
        # Chamadas antigas (sem label) continuam funcionando.
        self.banco.registrar(
            id_email="m2",
            thread_id="t2",
            remetente="a@b.com",
            assunto="x",
            desfecho="erro",
        )
        self.assertTrue(self.banco.ja_processado("m2"))


class TestMigracaoDeBancoAntigo(unittest.TestCase):
    def test_banco_sem_coluna_label_ganha_a_coluna(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "antigo.sqlite3"
            con = sqlite3.connect(caminho)
            # Esquema da versao anterior, sem a coluna label.
            con.execute(
                """CREATE TABLE processados (
                    id_email TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    remetente TEXT, assunto TEXT, desfecho TEXT NOT NULL,
                    origem TEXT, destino TEXT, id_rota TEXT,
                    qtd_volumes INTEGER, valor_nf REAL, peso_kg REAL,
                    valor_frete REAL, extracao_json TEXT, erro TEXT,
                    criado_em TEXT NOT NULL)"""
            )
            con.execute(
                "INSERT INTO processados (id_email, thread_id, desfecho, criado_em)"
                " VALUES ('velho', 'thr', 'cotado', '2026-01-01T00:00:00+00:00')"
            )
            con.commit()
            con.close()

            banco = Banco(caminho)  # deve migrar sem quebrar
            banco.registrar(
                id_email="novo",
                thread_id="thr",
                remetente="a@b.com",
                assunto="x",
                desfecho="cotado",
                label="cotador-processado",
            )
            self.assertTrue(banco.ja_processado("velho"))
            self.assertTrue(banco.ja_processado("novo"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_banco -v`
Expected: FAIL/ERROR com `TypeError: registrar() got an unexpected keyword argument 'label'`

- [ ] **Step 3: Implement**

Em `cotador/integracoes/banco.py`:

1. No `ESQUEMA`, adicionar a coluna após `extracao_json TEXT,`... na verdade após `erro TEXT,` e antes de `criado_em`:

```sql
    extracao_json   TEXT,
    erro            TEXT,
    label           TEXT,
    criado_em       TEXT NOT NULL
```

2. Em `_CAMPOS`, adicionar `"label",` entre `"erro",` e `"criado_em",`.

3. No `__init__`, migrar bancos antigos (substituir o bloco `with closing(...)` existente):

```python
        with closing(self._conectar()) as con:
            con.executescript(ESQUEMA)
            # Bancos criados antes da coluna label: CREATE IF NOT EXISTS nao
            # altera tabela existente, entao o ALTER cobre a migracao.
            colunas = [c[1] for c in con.execute("PRAGMA table_info(processados)")]
            if "label" not in colunas:
                con.execute("ALTER TABLE processados ADD COLUMN label TEXT")
            con.commit()
```

4. Em `registrar`, adicionar o parâmetro `label: str | None = None` (após `erro: str | None = None`) e, na tupla `valores`, inserir `label,` entre `erro,` e o `datetime.now(...)` (a ordem deve espelhar `_CAMPOS`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_banco -v`
Expected: PASS (3 testes)

Run: `python -m unittest discover -s cotador/tests -t .`
Expected: 68 testes OK (65 antigos + 3 novos)

- [ ] **Step 5: Commit**

```bash
git add cotador/integracoes/banco.py cotador/tests/test_banco.py
git commit -m "feat: coluna label em processados, com migracao de banco antigo"
```

---

### Task 2: Consultas do painel no banco

**Files:**
- Modify: `cotador/integracoes/banco.py`
- Test: `cotador/tests/test_banco.py`

- [ ] **Step 1: Write the failing tests**

Adicionar ao final de `cotador/tests/test_banco.py` (antes do `if __name__`):

```python
class TestConsultasDoPainel(BaseComBanco):
    def test_contar_por_desfecho_filtra_pelo_dia(self):
        self.registrar(id_email="a", desfecho="cotado")
        self.registrar(id_email="b", desfecho="cotado")
        self.registrar(id_email="c", desfecho="erro", label="cotador-revisar")
        hoje = self.banco.ultimos(1)[0]["criado_em"][:10]

        contagem = self.banco.contar_por_desfecho(prefixo_dia=hoje)
        self.assertEqual(contagem.get("cotado"), 2)
        self.assertEqual(contagem.get("erro"), 1)

        ontem = self.banco.contar_por_desfecho(prefixo_dia="1999-01-01")
        self.assertEqual(ontem, {})

    def test_ultimos_vem_em_ordem_decrescente_como_dicts(self):
        self.registrar(id_email="a")
        self.registrar(id_email="b")
        linhas = self.banco.ultimos(50)
        self.assertEqual(len(linhas), 2)
        self.assertIsInstance(linhas[0], dict)
        # Mesmo timestamp (mesmo segundo): o rowid desempata, ultimo primeiro.
        self.assertEqual(linhas[0]["id_email"], "b")

    def test_por_label_lista_somente_o_label_pedido(self):
        self.registrar(id_email="a", desfecho="erro", label="cotador-revisar")
        self.registrar(id_email="b", desfecho="cotado", label="cotador-processado")
        itens = self.banco.por_label("cotador-revisar")
        self.assertEqual([i["id_email"] for i in itens], ["a"])

    def test_apagar_thread_devolve_os_ids_removidos(self):
        self.registrar(id_email="a", thread_id="thr-9")
        self.registrar(id_email="b", thread_id="thr-9")
        self.registrar(id_email="fora", thread_id="outra")
        removidos = self.banco.apagar_thread("thr-9")
        self.assertEqual(sorted(removidos), ["a", "b"])
        self.assertFalse(self.banco.ja_processado("a"))
        self.assertTrue(self.banco.ja_processado("fora"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_banco -v`
Expected: ERROR com `AttributeError: 'Banco' object has no attribute 'contar_por_desfecho'` (e equivalentes)

- [ ] **Step 3: Implement**

Adicionar ao final da classe `Banco` em `cotador/integracoes/banco.py`:

```python
    # ---------------- consultas do painel ----------------
    def contar_por_desfecho(self, prefixo_dia: str | None = None) -> dict[str, int]:
        """Contagem por desfecho; `prefixo_dia` ('2026-09-01') filtra o dia UTC."""
        sql = "SELECT desfecho, COUNT(*) FROM processados"
        parametros: tuple = ()
        if prefixo_dia:
            sql += " WHERE criado_em LIKE ?"
            parametros = (f"{prefixo_dia}%",)
        sql += " GROUP BY desfecho"
        with closing(self._conectar()) as con:
            return dict(con.execute(sql, parametros).fetchall())

    def ultimos(self, limite: int = 50) -> list[dict]:
        """Registros mais recentes primeiro. rowid desempata o mesmo segundo."""
        with closing(self._conectar()) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute(
                "SELECT * FROM processados ORDER BY criado_em DESC, rowid DESC LIMIT ?",
                (limite,),
            )
            return [dict(linha) for linha in cur.fetchall()]

    def por_label(self, label: str) -> list[dict]:
        with closing(self._conectar()) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute(
                "SELECT * FROM processados WHERE label = ? "
                "ORDER BY criado_em DESC, rowid DESC",
                (label,),
            )
            return [dict(linha) for linha in cur.fetchall()]

    def apagar_thread(self, thread_id: str) -> list[str]:
        """Apaga os registros da thread (para reprocessar) e devolve os ids."""
        with closing(self._conectar()) as con:
            cur = con.execute(
                "SELECT id_email FROM processados WHERE thread_id = ?", (thread_id,)
            )
            ids = [linha[0] for linha in cur.fetchall()]
            con.execute("DELETE FROM processados WHERE thread_id = ?", (thread_id,))
            con.commit()
            return ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_banco -v`
Expected: PASS (7 testes)

- [ ] **Step 5: Commit**

```bash
git add cotador/integracoes/banco.py cotador/tests/test_banco.py
git commit -m "feat: consultas do painel no Banco (contadores, ultimos, por label, apagar thread)"
```

---

### Task 3: Agente grava o label aplicado

**Files:**
- Modify: `cotador/agente.py` (método `_fechar` e o registro de erro de extração, ~linha 96)
- Test: `cotador/tests/test_banco.py`

- [ ] **Step 1: Write the failing test**

Adicionar ao final de `cotador/tests/test_banco.py` (antes do `if __name__`):

```python
class TestAgenteGravaLabel(unittest.TestCase):
    """O label Gmail aplicado ao fechar o email deve ir para o SQLite,
    senao o painel nao sabe o que esta em revisao."""

    def test_fechar_passa_o_label_ao_banco(self):
        from cotador.agente import Agente
        from cotador.core.modelos import Email

        class BancoGravador:
            def __init__(self):
                self.registros = []

            def registrar(self, **kw):
                self.registros.append(kw)

        class CaixaNula:
            def aplicar_labels(self, *a, **k):
                pass

        class CfgFake:
            LABEL_PROCESSADO = "cotador-processado"

        banco = BancoGravador()
        agente = Agente(
            cfg=CfgFake(),
            caixa=CaixaNula(),
            tarifas=None,
            extrator=None,
            banco=banco,
        )
        email = Email(
            id="m1",
            thread_id="t1",
            remetente="a@b.com",
            nome_remetente="Ana",
            assunto="Cotacao",
            corpo="",
            message_id_header=None,
            references_header=None,
            uid="7",
        )
        agente._fechar(email, "cotado", label="cotador-processado")
        self.assertEqual(banco.registros[0]["label"], "cotador-processado")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest cotador.tests.test_banco.TestAgenteGravaLabel -v`
Expected: FAIL com `KeyError: 'label'`

- [ ] **Step 3: Implement**

Em `cotador/agente.py`:

1. No método `_fechar`, incluir `label=label` na chamada `self.banco.registrar(...)`:

```python
    def _fechar(self, email: Email, desfecho: Desfecho, *, label: str, **extra) -> Desfecho:
        self.banco.registrar(
            id_email=email.id,
            thread_id=email.thread_id,
            remetente=email.remetente,
            assunto=email.assunto,
            desfecho=desfecho,
            label=label,
            **extra,
        )
        self.caixa.aplicar_labels(email.uid, [label])
        return desfecho
```

2. No tratamento de falha de extração (o `self.banco.registrar(...)` dentro do
`except` em `_processar`, que hoje registra `desfecho="erro"`), adicionar a linha
`label=self.cfg.LABEL_REVISAR,` logo após `desfecho="erro",`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s cotador/tests -t .`
Expected: todos passam (8 no test_banco + 65 antigos)

- [ ] **Step 5: Commit**

```bash
git add cotador/agente.py cotador/tests/test_banco.py
git commit -m "feat: agente grava no banco o label aplicado ao email"
```

---

### Task 4: `CaixaIMAP.devolver_para_fila`

Operação inversa do `aplicar_labels`: acha o UID atual pelo `X-GM-MSGID` (o UID salvo em outra sessão não vale mais), remove os labels e restaura como não lido.

**Files:**
- Modify: `cotador/integracoes/caixa_imap.py`
- Test: `cotador/tests/test_painel.py` (novo)

- [ ] **Step 1: Write the failing tests**

Criar `cotador/tests/test_painel.py`:

```python
"""Testes do painel: IMAP de devolucao, servico do loop e rotas Flask."""
from __future__ import annotations

import unittest

from cotador.integracoes.caixa_imap import CaixaIMAP


class ConIMAPFake:
    """Grava os comandos UID emitidos e devolve respostas configuraveis."""

    def __init__(self, uid_encontrado: bytes | None = b"7"):
        self.comandos: list[tuple] = []
        self._uid = uid_encontrado

    def uid(self, comando, *args):
        self.comandos.append((comando, *args))
        if comando == "SEARCH":
            return "OK", [self._uid or b""]
        return "OK", [b""]


def caixa_com_con(con) -> CaixaIMAP:
    caixa = CaixaIMAP(host="x", porta=993, usuario="u@x.com", senha="s")
    caixa._con = con  # injeta a conexao fake; nada de rede nos testes
    return caixa


class TestDevolverParaFila(unittest.TestCase):
    def test_remove_labels_e_marca_nao_lido(self):
        con = ConIMAPFake(uid_encontrado=b"42")
        caixa = caixa_com_con(con)

        ok = caixa.devolver_para_fila("111222333", ["cotador-revisar"])

        self.assertTrue(ok)
        self.assertIn(("SEARCH", "X-GM-MSGID", "111222333"), con.comandos)
        self.assertIn(("STORE", "42", "-X-GM-LABELS", '("cotador-revisar")'), con.comandos)
        self.assertIn(("STORE", "42", "-FLAGS", r"(\Seen)"), con.comandos)

    def test_email_nao_encontrado_devolve_false_sem_store(self):
        con = ConIMAPFake(uid_encontrado=None)
        caixa = caixa_com_con(con)

        ok = caixa.devolver_para_fila("999", ["cotador-revisar"])

        self.assertFalse(ok)
        stores = [c for c in con.comandos if c[0] == "STORE"]
        self.assertEqual(stores, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: ERROR com `AttributeError: 'CaixaIMAP' object has no attribute 'devolver_para_fila'`

- [ ] **Step 3: Implement**

Adicionar em `cotador/integracoes/caixa_imap.py`, na seção `# ---------------- escrita ----------------`, após `aplicar_labels`:

```python
    def devolver_para_fila(self, id_email: str, labels_remover: list[str]) -> bool:
        """Desfaz o fechamento: remove labels e restaura como nao lida.

        Localiza pelo X-GM-MSGID porque o UID gravado em outra sessao ja nao
        vale. Com o email de volta a 'is:unread' sem os labels, o proximo
        ciclo o reprocessa.
        """
        ok, dados = self.con.uid("SEARCH", "X-GM-MSGID", id_email)
        uids = (dados[0] or b"").split() if ok == "OK" else []
        if not uids:
            log.warning("Email %s nao encontrado na caixa para devolver", id_email)
            return False
        uid = uids[-1].decode()
        for nome in labels_remover:
            self.con.uid("STORE", uid, "-X-GM-LABELS", f'("{nome}")')
        self.con.uid("STORE", uid, "-FLAGS", r"(\Seen)")
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: PASS (2 testes)

- [ ] **Step 5: Commit**

```bash
git add cotador/integracoes/caixa_imap.py cotador/tests/test_painel.py
git commit -m "feat: devolver_para_fila no IMAP (remove labels e restaura nao lido)"
```

---

### Task 5: `painel/servico_agente.py` — o loop numa thread

**Files:**
- Create: `painel/__init__.py` (vazio)
- Create: `painel/servico_agente.py`
- Test: `cotador/tests/test_painel.py`

- [ ] **Step 1: Write the failing tests**

Adicionar a `cotador/tests/test_painel.py` (antes do `if __name__`):

```python
class AgenteFake:
    """Dubla o Agente: conta ciclos e falha sob demanda."""

    def __init__(self, resumo=None, excecao=None):
        self.resumo = resumo or {"cotado": 1}
        self.excecao = excecao
        self.ciclos = 0

    def rodar_ciclo(self):
        self.ciclos += 1
        if self.excecao:
            raise self.excecao
        return self.resumo


class TestServicoAgente(unittest.TestCase):
    def test_ciclo_unico_guarda_o_resumo(self):
        from painel.servico_agente import ServicoAgente

        agente = AgenteFake(resumo={"cotado": 2, "erro": 1})
        servico = ServicoAgente(lambda: agente, intervalo_segundos=60)

        resumo = servico.ciclo_unico()

        self.assertEqual(resumo, {"cotado": 2, "erro": 1})
        self.assertEqual(servico.ultimo_resumo, {"cotado": 2, "erro": 1})
        self.assertIsNone(servico.ultimo_erro)
        self.assertIsNotNone(servico.ultimo_ciclo_em)
        self.assertFalse(servico.rodando)

    def test_excecao_no_ciclo_fica_registrada(self):
        from painel.servico_agente import ServicoAgente

        servico = ServicoAgente(
            lambda: AgenteFake(excecao=RuntimeError("boom")), intervalo_segundos=60
        )
        with self.assertRaises(RuntimeError):
            servico.ciclo_unico()
        self.assertIn("boom", servico.ultimo_erro)

    def test_ligar_roda_ciclos_e_desligar_para(self):
        from painel.servico_agente import ServicoAgente

        agente = AgenteFake()
        servico = ServicoAgente(lambda: agente, intervalo_segundos=0.01)

        servico.ligar()
        self.assertTrue(servico.rodando)
        # Espera ao menos um ciclo acontecer, sem depender de sleep fixo.
        for _ in range(200):
            if agente.ciclos >= 1:
                break
            import time

            time.sleep(0.01)
        servico.desligar()

        self.assertFalse(servico.rodando)
        self.assertGreaterEqual(agente.ciclos, 1)
        ciclos_apos_desligar = agente.ciclos
        import time

        time.sleep(0.05)
        self.assertEqual(agente.ciclos, ciclos_apos_desligar)

    def test_credencial_recusada_desliga_o_loop_e_sinaliza(self):
        from cotador.integracoes.caixa_imap import CredencialInvalida
        from painel.servico_agente import ServicoAgente

        agente = AgenteFake(excecao=CredencialInvalida("senha ruim"))
        servico = ServicoAgente(lambda: agente, intervalo_segundos=0.01)

        servico.ligar()
        for _ in range(200):
            if not servico.rodando:
                break
            import time

            time.sleep(0.01)

        self.assertFalse(servico.rodando)
        self.assertTrue(servico.credencial_recusada)
        self.assertIn("senha ruim", servico.ultimo_erro)
        self.assertEqual(agente.ciclos, 1)  # nao insiste em credencial ruim
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: ERROR com `ModuleNotFoundError: No module named 'painel'`

- [ ] **Step 3: Implement**

Criar `painel/__init__.py` vazio e `painel/servico_agente.py`:

```python
"""Roda o ciclo do agente numa thread de fundo, com parada cooperativa.

O painel e o loop vivem no mesmo processo: ligar/desligar e um Event, nao um
processo a gerenciar. `fabrica_agente` e chamada a cada ciclo para montar o
Agente com a configuracao vigente (o modo rascunho/enviar muda em runtime).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from cotador.integracoes.caixa_imap import CredencialInvalida

log = logging.getLogger(__name__)


class ServicoAgente:
    def __init__(self, fabrica_agente: Callable, intervalo_segundos: float) -> None:
        self._fabrica = fabrica_agente
        self.intervalo_segundos = intervalo_segundos
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        # Um ciclo por vez, mesmo se "rodar agora" coincidir com o loop.
        self._trava = threading.Lock()
        self.ultimo_resumo: dict[str, int] | None = None
        self.ultimo_erro: str | None = None
        self.ultimo_ciclo_em: str | None = None
        self.credencial_recusada = False

    @property
    def rodando(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ligar(self) -> None:
        if self.rodando:
            return
        self._parar.clear()
        self._thread = threading.Thread(
            target=self._laco, daemon=True, name="loop-agente"
        )
        self._thread.start()

    def desligar(self) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def ciclo_unico(self) -> dict[str, int]:
        with self._trava:
            return self._um_ciclo()

    # ---------------- interno ----------------
    def _um_ciclo(self) -> dict[str, int]:
        try:
            resumo = self._fabrica().rodar_ciclo()
            self.ultimo_resumo = resumo
            self.ultimo_erro = None
            self.credencial_recusada = False
            return resumo
        except CredencialInvalida as exc:
            self.ultimo_erro = str(exc)
            self.credencial_recusada = True
            raise
        except Exception as exc:
            self.ultimo_erro = repr(exc)
            raise
        finally:
            self.ultimo_ciclo_em = datetime.now().isoformat(timespec="seconds")

    def _laco(self) -> None:
        while not self._parar.is_set():
            try:
                with self._trava:
                    self._um_ciclo()
            except CredencialInvalida:
                # Insistir nao resolve credencial ruim; o painel exibe o erro.
                log.error("Credencial recusada; loop desligado")
                return
            except Exception:
                log.exception("Ciclo falhou; proxima tentativa no intervalo")
            self._parar.wait(self.intervalo_segundos)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: PASS (6 testes)

- [ ] **Step 5: Commit**

```bash
git add painel/__init__.py painel/servico_agente.py cotador/tests/test_painel.py
git commit -m "feat: ServicoAgente roda o loop em thread com liga/desliga"
```

---

### Task 6: `painel/consultas.py` — dados prontos para as telas

**Files:**
- Create: `painel/consultas.py`
- Test: `cotador/tests/test_painel.py`

- [ ] **Step 1: Write the failing tests**

Adicionar a `cotador/tests/test_painel.py`. Também acrescentar estes imports no topo do arquivo (junto aos existentes):

```python
import tempfile
from pathlib import Path

from cotador.integracoes.banco import Banco
```

E a classe de teste:

```python
class TestConsultas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.banco = Banco(Path(self._tmp.name) / "t.sqlite3")

    def test_contadores_de_hoje_zera_o_que_nao_ha(self):
        from painel import consultas

        self.banco.registrar(
            id_email="a", thread_id="t", remetente="x@y.com", assunto="s",
            desfecho="cotado", label="cotador-processado",
        )
        contadores = consultas.contadores_de_hoje(self.banco)
        self.assertEqual(contadores["cotado"], 1)
        self.assertEqual(contadores["erro"], 0)
        self.assertEqual(contadores["incompleto"], 0)
        self.assertEqual(contadores["sem_rota"], 0)

    def test_ultimos_processados_formata_quando_e_rota(self):
        from painel import consultas

        self.banco.registrar(
            id_email="a", thread_id="t", remetente="x@y.com", assunto="s",
            desfecho="cotado", origem="Sao Paulo/SP", destino="Campinas/SP",
            valor_frete=252.5, label="cotador-processado",
        )
        self.banco.registrar(
            id_email="b", thread_id="t2", remetente="w@y.com", assunto="s2",
            desfecho="erro", label="cotador-revisar",
        )
        linhas = consultas.ultimos_processados(self.banco)
        self.assertEqual(len(linhas), 2)
        por_id = {l["id_email"]: l for l in linhas}
        self.assertEqual(por_id["a"]["rota"], "Sao Paulo/SP → Campinas/SP")
        self.assertEqual(por_id["b"]["rota"], "—")
        self.assertRegex(por_id["a"]["quando"], r"\d{2}/\d{2} \d{2}:\d{2}")

    def test_fila_de_revisao_expande_a_extracao(self):
        from painel import consultas

        self.banco.registrar(
            id_email="a", thread_id="t", remetente="x@y.com", assunto="s",
            desfecho="erro", label="cotador-revisar",
            erro="confianca 0.20 abaixo de 0.35",
            extracao={"e_cotacao": True, "confianca": 0.2, "origem": "SP"},
        )
        self.banco.registrar(
            id_email="b", thread_id="t2", remetente="w@y.com", assunto="s2",
            desfecho="cotado", label="cotador-processado",
        )
        itens = consultas.fila_de_revisao(self.banco, "cotador-revisar")
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0]["id_email"], "a")
        self.assertIn("confianca 0.20", itens[0]["erro"])
        self.assertIn('"origem": "SP"', itens[0]["extracao"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_painel.TestConsultas -v`
Expected: ERROR com `ImportError: cannot import name 'consultas'` (ou ModuleNotFoundError)

- [ ] **Step 3: Implement**

Criar `painel/consultas.py`:

```python
"""Monta os dados que as telas exibem, a partir do SQLite do agente."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from cotador.integracoes.banco import Banco

DESFECHOS = ("cotado", "erro", "incompleto", "sem_rota", "ignorado")


def contadores_de_hoje(banco: Banco) -> dict[str, int]:
    # criado_em e gravado em UTC; o dia do filtro segue o mesmo relogio.
    hoje = datetime.now(timezone.utc).date().isoformat()
    bruto = banco.contar_por_desfecho(prefixo_dia=hoje)
    return {desfecho: bruto.get(desfecho, 0) for desfecho in DESFECHOS}


def _quando(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")
    except ValueError:
        return iso


def ultimos_processados(banco: Banco, limite: int = 50) -> list[dict]:
    linhas = banco.ultimos(limite)
    for linha in linhas:
        linha["quando"] = _quando(linha["criado_em"])
        linha["rota"] = (
            f"{linha['origem']} → {linha['destino']}"
            if linha["origem"] and linha["destino"]
            else "—"
        )
    return linhas


def fila_de_revisao(banco: Banco, label_revisar: str) -> list[dict]:
    itens = banco.por_label(label_revisar)
    for item in itens:
        item["quando"] = _quando(item["criado_em"])
        item["extracao"] = (
            json.dumps(
                json.loads(item["extracao_json"]), indent=2, ensure_ascii=False
            )
            if item["extracao_json"]
            else None
        )
    return itens
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: PASS (9 testes)

- [ ] **Step 5: Commit**

```bash
git add painel/consultas.py cotador/tests/test_painel.py
git commit -m "feat: consultas do painel (contadores, ultimos, fila de revisao)"
```

---

### Task 7: App Flask — esqueleto, tema escuro e Visão geral

**Files:**
- Modify: `requirements.txt`
- Create: `painel/app.py`
- Create: `painel/templates/base.html`
- Create: `painel/templates/visao_geral.html`
- Create: `painel/static/estilo.css`
- Test: `cotador/tests/test_painel.py`

- [ ] **Step 1: Install Flask and pin it**

```bash
python -m pip install "flask>=3.0"
```

Adicionar linha ao `requirements.txt`:

```
flask>=3.0
```

- [ ] **Step 2: Write the failing tests**

Adicionar a `cotador/tests/test_painel.py`. Primeiro os fakes e o montador de app, usados por todos os testes de rota (colocar após `TestConsultas`):

```python
from cotador.core.precificacao import normalizar_local
from cotador.tests.test_precificacao import tarifa_exemplo


class TarifasFake:
    """Mesma interface de TabelaTarifas, servida de uma lista em memoria."""

    def __init__(self, tarifas):
        self._tarifas = list(tarifas)
        self.carregamentos = 0

    def carregar(self):
        self.carregamentos += 1
        return len(self._tarifas)

    def buscar(self, origem, destino, modal=None):
        o, d = normalizar_local(origem), normalizar_local(destino)
        for t in self._tarifas:
            if (
                normalizar_local(t.chave_origem) == o
                and normalizar_local(t.chave_destino) == d
            ):
                return t
        return None

    def trecho_cadastrado(self, origem, destino):
        return self.buscar(origem, destino) is not None


class CaixaDevolvedoraFake:
    def __init__(self):
        self.devolvidos = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def devolver_para_fila(self, id_email, labels_remover):
        self.devolvidos.append(id_email)
        return True


def cfg_de_teste(tmp: Path):
    from cotador.config import Config

    return Config(
        anthropic_api_key="",
        anthropic_model="claude-sonnet-5",
        anthropic_workspace_id="",
        gmail_user="conta@gmail.com",
        gmail_query="is:unread",
        sheet_id="sheet",
        sheet_aba="TABELA_ROTAS",
        modo_resposta="rascunho",
        intervalo_segundos=60,
        exigir_peso=True,
        smtp_host="smtp",
        smtp_porta=465,
        smtp_usuario="conta@gmail.com",
        smtp_senha="senha",
        smtp_remetente="",
        imap_host="imap",
        imap_porta=993,
        service_account_json=tmp / "service_account.json",
        banco=tmp / "cotador.sqlite3",
    )


class BasePainel(unittest.TestCase):
    def setUp(self):
        from painel.app import criar_app
        from painel.servico_agente import ServicoAgente

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.cfg = cfg_de_teste(tmp)
        self.banco = Banco(self.cfg.banco)
        self.tarifas = TarifasFake([tarifa_exemplo()])
        self.agente_fake = AgenteFake()
        self.servico = ServicoAgente(lambda: self.agente_fake, intervalo_segundos=60)
        self.addCleanup(self.servico.desligar)
        self.caixa = CaixaDevolvedoraFake()
        self.estado = {"modo": "rascunho"}
        app = criar_app(
            self.cfg, self.banco, self.tarifas, self.servico,
            lambda: self.caixa, self.estado,
        )
        app.config["TESTING"] = True
        self.cliente = app.test_client()


class TestVisaoGeral(BasePainel):
    def test_pagina_mostra_contadores_e_tabela(self):
        self.banco.registrar(
            id_email="a", thread_id="t", remetente="cliente@acme.com",
            assunto="Cotacao SP-Campinas", desfecho="cotado",
            origem="Sao Paulo/SP", destino="Campinas/SP",
            valor_frete=252.5, label="cotador-processado",
        )
        resposta = self.cliente.get("/")
        corpo = resposta.get_data(as_text=True)
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Visão geral", corpo)
        self.assertIn("cliente@acme.com", corpo)
        self.assertIn("252,50", corpo)

    def test_api_status_devolve_contadores_e_estado(self):
        resposta = self.cliente.get("/api/status")
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.get_json()
        self.assertIn("contadores", dados)
        self.assertFalse(dados["rodando"])
        self.assertEqual(dados["modo"], "rascunho")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_painel.TestVisaoGeral -v`
Expected: ERROR com `ModuleNotFoundError: No module named 'painel.app'`

- [ ] **Step 4: Implement**

Criar `painel/app.py`:

```python
"""Fabrica do app Flask do painel. Toda dependencia entra por parametro,
para os testes injetarem fakes (mesmo padrao do Agente)."""
from __future__ import annotations

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from cotador.core import precificacao
from cotador.core.modelos import PedidoCotacao
from painel import consultas


def _reais(valor) -> str:
    if valor is None:
        return "—"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def criar_app(cfg, banco, tarifas, servico, fabrica_caixa, estado) -> Flask:
    app = Flask(__name__)
    # So para flash() de mensagens; o painel e local e sem login.
    app.config["SECRET_KEY"] = "painel-local"
    app.jinja_env.filters["reais"] = _reais

    @app.context_processor
    def _globais():
        return {"servico": servico, "modo": estado["modo"]}

    # ---------------- visao geral ----------------
    @app.get("/")
    def visao_geral():
        return render_template(
            "visao_geral.html",
            pagina="visao",
            contadores=consultas.contadores_de_hoje(banco),
            processados=consultas.ultimos_processados(banco),
        )

    @app.get("/api/status")
    def api_status():
        return jsonify(
            contadores=consultas.contadores_de_hoje(banco),
            rodando=servico.rodando,
            modo=estado["modo"],
            ultimo_resumo=servico.ultimo_resumo,
            ultimo_ciclo_em=servico.ultimo_ciclo_em,
            ultimo_erro=servico.ultimo_erro,
            credencial_recusada=servico.credencial_recusada,
        )

    return app
```

Criar `painel/templates/base.html`:

```html
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cotador — Painel</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='estilo.css') }}">
</head>
<body>
  <aside class="menu">
    <h1>Cotador</h1>
    <nav>
      <a href="/" class="{{ 'ativo' if pagina == 'visao' }}">📊 Visão geral</a>
      <a href="/revisao" class="{{ 'ativo' if pagina == 'revisao' }}">🔍 Revisão</a>
      <a href="/cotar" class="{{ 'ativo' if pagina == 'cotar' }}">🧮 Cotar</a>
      <a href="/agente" class="{{ 'ativo' if pagina == 'agente' }}">⚙️ Agente</a>
    </nav>
    <div class="estado-loop {{ 'ligado' if servico.rodando else 'desligado' }}">
      {{ '● rodando' if servico.rodando else '○ parado' }} · {{ modo }}
    </div>
  </aside>
  <main>
    {% for mensagem in get_flashed_messages() %}
      <div class="flash">{{ mensagem }}</div>
    {% endfor %}
    {% block conteudo %}{% endblock %}
  </main>
</body>
</html>
```

Os `href` do menu são âncoras diretas (`/revisao`, `/cotar`, `/agente`) de
propósito: essas rotas só passam a existir nas Tasks 8–10, e `url_for` para uma
rota inexistente quebraria a renderização já nesta task.

Criar `painel/templates/visao_geral.html`:

```html
{% extends "base.html" %}
{% block conteudo %}
<h2>Visão geral</h2>
<div class="cartoes">
  <div class="cartao"><span id="c-cotado" class="numero ok">{{ contadores.cotado }}</span><span>cotados hoje</span></div>
  <div class="cartao"><span id="c-erro" class="numero ruim">{{ contadores.erro }}</span><span>para revisar</span></div>
  <div class="cartao"><span id="c-incompleto" class="numero atencao">{{ contadores.incompleto }}</span><span>aguardando dados</span></div>
  <div class="cartao"><span id="c-sem_rota" class="numero neutro">{{ contadores.sem_rota }}</span><span>sem rota</span></div>
</div>

<h3>Últimos processados</h3>
<table>
  <thead>
    <tr><th>Quando</th><th>Remetente</th><th>Assunto</th><th>Rota</th><th>Desfecho</th><th>Frete</th></tr>
  </thead>
  <tbody>
    {% for linha in processados %}
    <tr>
      <td>{{ linha.quando }}</td>
      <td>{{ linha.remetente or "—" }}</td>
      <td>{{ linha.assunto or "—" }}</td>
      <td>{{ linha.rota }}</td>
      <td><span class="selo selo-{{ linha.desfecho }}">{{ linha.desfecho }}</span></td>
      <td>{{ linha.valor_frete | reais }}</td>
    </tr>
    {% else %}
    <tr><td colspan="6" class="vazio">Nenhum email processado ainda.</td></tr>
    {% endfor %}
  </tbody>
</table>

<script>
  setInterval(async () => {
    try {
      const resposta = await fetch("/api/status");
      const status = await resposta.json();
      for (const chave of ["cotado", "erro", "incompleto", "sem_rota"]) {
        const el = document.getElementById("c-" + chave);
        if (el) el.textContent = status.contadores[chave];
      }
    } catch (e) { /* servidor reiniciando; tenta no proximo tick */ }
  }, 10000);
</script>
{% endblock %}
```

Criar `painel/static/estilo.css`:

```css
/* Tema escuro operacional (escolhido no brainstorming). */
:root {
  --fundo: #0f172a;
  --painel: #1e293b;
  --borda: #334155;
  --texto: #e2e8f0;
  --texto-fraco: #94a3b8;
  --acento: #38bdf8;
  --ok: #4ade80;
  --ruim: #f87171;
  --atencao: #fbbf24;
}
* { box-sizing: border-box; }
body {
  margin: 0; display: flex; min-height: 100vh;
  background: var(--fundo); color: var(--texto);
  font: 15px/1.5 system-ui, "Segoe UI", sans-serif;
}
.menu {
  width: 220px; padding: 20px 14px; background: var(--painel);
  display: flex; flex-direction: column; gap: 6px; flex-shrink: 0;
}
.menu h1 { font-size: 20px; margin: 0 8px 14px; color: var(--acento); }
.menu nav { display: flex; flex-direction: column; gap: 4px; }
.menu a {
  color: var(--texto); text-decoration: none; padding: 8px 10px; border-radius: 8px;
}
.menu a:hover { background: #27364d; }
.menu a.ativo { background: #0b3a55; color: var(--acento); font-weight: 600; }
.estado-loop { margin-top: auto; padding: 8px 10px; font-size: 13px; color: var(--texto-fraco); }
.estado-loop.ligado { color: var(--ok); }
main { flex: 1; padding: 24px 32px; max-width: 1100px; }
h2 { margin-top: 0; }
h3 { color: var(--texto-fraco); }
.cartoes { display: flex; gap: 14px; margin: 16px 0 24px; flex-wrap: wrap; }
.cartao {
  background: var(--painel); border-radius: 10px; padding: 14px 18px;
  min-width: 150px; display: flex; flex-direction: column;
}
.cartao .numero { font-size: 26px; font-weight: 700; }
.numero.ok { color: var(--ok); } .numero.ruim { color: var(--ruim); }
.numero.atencao { color: var(--atencao); } .numero.neutro { color: var(--texto-fraco); }
table { width: 100%; border-collapse: collapse; background: var(--painel); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--borda); font-size: 14px; }
th { color: var(--texto-fraco); font-weight: 600; }
td.vazio { color: var(--texto-fraco); text-align: center; padding: 24px; }
.selo { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.selo-cotado { background: #14532d; color: var(--ok); }
.selo-erro { background: #7f1d1d; color: #fecaca; }
.selo-incompleto { background: #713f12; color: var(--atencao); }
.selo-sem_rota { background: #334155; color: var(--texto-fraco); }
.selo-ignorado { background: #334155; color: var(--texto-fraco); }
.flash { background: #0b3a55; border: 1px solid var(--acento); border-radius: 8px; padding: 10px 14px; margin-bottom: 16px; }
.cartao-detalhe { background: var(--painel); border-radius: 10px; padding: 16px 20px; margin-bottom: 14px; }
.cartao-detalhe pre {
  background: var(--fundo); padding: 10px; border-radius: 8px;
  overflow-x: auto; font-size: 13px;
}
.motivo { color: var(--ruim); }
form.linha { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--texto-fraco); }
input, select {
  background: var(--fundo); color: var(--texto); border: 1px solid var(--borda);
  border-radius: 8px; padding: 8px 10px; font-size: 14px;
}
button {
  background: var(--acento); color: #082f49; border: 0; border-radius: 8px;
  padding: 9px 16px; font-size: 14px; font-weight: 600; cursor: pointer;
}
button:hover { filter: brightness(1.1); }
button.perigo { background: var(--ruim); color: #450a0a; }
button.neutro { background: var(--borda); color: var(--texto); }
.alerta {
  background: #7f1d1d; border: 1px solid var(--ruim); border-radius: 10px;
  padding: 14px 18px; margin-bottom: 18px; white-space: pre-wrap; font-size: 13px;
}
.subtitulo { color: var(--texto-fraco); font-size: 14px; }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: PASS (11 testes)

- [ ] **Step 6: Commit**

```bash
git add requirements.txt painel/app.py painel/templates/ painel/static/ cotador/tests/test_painel.py
git commit -m "feat: painel Flask com tema escuro, visao geral e /api/status"
```

---

### Task 8: Página Revisão + devolver à fila

**Files:**
- Modify: `painel/app.py`
- Create: `painel/templates/revisao.html`
- Test: `cotador/tests/test_painel.py`

- [ ] **Step 1: Write the failing tests**

Adicionar a `cotador/tests/test_painel.py`:

```python
class TestRevisao(BasePainel):
    def registrar_para_revisar(self, id_email="r1", thread_id="thr-r"):
        self.banco.registrar(
            id_email=id_email, thread_id=thread_id, remetente="cliente@acme.com",
            assunto="Cotacao urgente", desfecho="erro", label="cotador-revisar",
            erro="confianca 0.20 abaixo de 0.35",
            extracao={"e_cotacao": True, "confianca": 0.2},
        )

    def test_lista_o_que_esta_em_revisao_com_motivo(self):
        self.registrar_para_revisar()
        corpo = self.cliente.get("/revisao").get_data(as_text=True)
        self.assertIn("cliente@acme.com", corpo)
        self.assertIn("confianca 0.20", corpo)
        self.assertIn("Devolver à fila", corpo)

    def test_devolver_apaga_do_banco_e_aciona_o_imap(self):
        self.registrar_para_revisar(id_email="r1", thread_id="thr-r")
        self.registrar_para_revisar(id_email="r2", thread_id="thr-r")

        resposta = self.cliente.post(
            "/revisao/devolver", data={"thread_id": "thr-r"}, follow_redirects=True
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(sorted(self.caixa.devolvidos), ["r1", "r2"])
        self.assertFalse(self.banco.ja_processado("r1"))
        self.assertFalse(self.banco.ja_processado("r2"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_painel.TestRevisao -v`
Expected: FAIL com 404 (rota `/revisao` não existe)

- [ ] **Step 3: Implement**

Em `painel/app.py`, adicionar antes do `return app`:

```python
    # ---------------- revisao ----------------
    @app.get("/revisao")
    def revisao():
        return render_template(
            "revisao.html",
            pagina="revisao",
            itens=consultas.fila_de_revisao(banco, cfg.LABEL_REVISAR),
        )

    @app.post("/revisao/devolver")
    def revisao_devolver():
        thread_id = request.form["thread_id"]
        ids = banco.apagar_thread(thread_id)
        devolvidos = 0
        # Conexao IMAP propria da acao: abre, devolve e fecha (mesmo padrao
        # do ciclo do agente — nada de sessao ociosa pendurada).
        with fabrica_caixa() as caixa:
            for id_email in ids:
                if caixa.devolver_para_fila(id_email, [cfg.LABEL_REVISAR]):
                    devolvidos += 1
        flash(
            f"{devolvidos} de {len(ids)} email(s) devolvidos à fila; "
            "o agente reprocessa no próximo ciclo."
        )
        return redirect(url_for("revisao"))
```

Criar `painel/templates/revisao.html`:

```html
{% extends "base.html" %}
{% block conteudo %}
<h2>Revisão humana</h2>
<p class="subtitulo">Threads com o label <code>cotador-revisar</code>. Devolver à fila apaga o registro e o agente tenta de novo no próximo ciclo.</p>

{% for item in itens %}
<div class="cartao-detalhe">
  <strong>{{ item.remetente or "—" }}</strong> · {{ item.assunto or "(sem assunto)" }}
  <span style="float:right;color:var(--texto-fraco)">{{ item.quando }}</span>
  {% if item.erro %}<p class="motivo">Motivo: {{ item.erro }}</p>{% endif %}
  {% if item.extracao %}
  <details>
    <summary>Extração do LLM</summary>
    <pre>{{ item.extracao }}</pre>
  </details>
  {% endif %}
  <form method="post" action="{{ url_for('revisao_devolver') }}"
        onsubmit="return confirm('Devolver a thread inteira à fila do agente?')">
    <input type="hidden" name="thread_id" value="{{ item.thread_id }}">
    <button class="perigo" type="submit">Devolver à fila</button>
  </form>
</div>
{% else %}
<p>Nada aguardando revisão. 🎉</p>
{% endfor %}
{% endblock %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: PASS (13 testes)

- [ ] **Step 5: Commit**

```bash
git add painel/app.py painel/templates/revisao.html cotador/tests/test_painel.py
git commit -m "feat: pagina de revisao com devolucao de thread a fila"
```

---

### Task 9: Página Cotar (formulário estruturado)

**Files:**
- Modify: `painel/app.py`
- Create: `painel/templates/cotar.html`
- Test: `cotador/tests/test_painel.py`

- [ ] **Step 1: Write the failing tests**

Adicionar a `cotador/tests/test_painel.py`:

```python
class TestCotar(BasePainel):
    def test_cotacao_reproduz_o_exemplo_da_planilha(self):
        resposta = self.cliente.post("/cotar", data={
            "origem": "Sao Paulo/SP", "destino": "Campinas/SP",
            "qtd_volumes": "10", "valor_nf": "8000", "peso_kg": "300",
            "modal": "",
        })
        corpo = resposta.get_data(as_text=True)
        self.assertIn("252,50", corpo)   # total da aba EXEMPLO_CALCULO
        self.assertIn("R00001", corpo)   # rota aplicada

    def test_rota_inexistente_avisa_sem_cotar(self):
        resposta = self.cliente.post("/cotar", data={
            "origem": "Manaus/AM", "destino": "Campinas/SP",
            "qtd_volumes": "10", "valor_nf": "8000", "peso_kg": "300",
            "modal": "",
        })
        corpo = resposta.get_data(as_text=True)
        self.assertIn("Rota não atendida", corpo)
        self.assertNotIn("252,50", corpo)

    def test_entrada_invalida_nao_estoura(self):
        resposta = self.cliente.post("/cotar", data={
            "origem": "Sao Paulo/SP", "destino": "Campinas/SP",
            "qtd_volumes": "abc", "valor_nf": "8000", "peso_kg": "",
            "modal": "",
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Não foi possível cotar", resposta.get_data(as_text=True))

    def test_get_mostra_o_formulario(self):
        corpo = self.cliente.get("/cotar").get_data(as_text=True)
        self.assertIn("Origem", corpo)
        self.assertIn("Valor da NF", corpo)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_painel.TestCotar -v`
Expected: FAIL com 404/405 (rota `/cotar` não existe)

- [ ] **Step 3: Implement**

Em `painel/app.py`, adicionar antes do `return app`:

```python
    # ---------------- cotacao manual ----------------
    @app.route("/cotar", methods=["GET", "POST"])
    def cotar():
        resultado = None
        erro = None
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            try:
                tarifas.carregar()
                pedido = PedidoCotacao(
                    e_cotacao=True,
                    confianca=1.0,
                    origem=form["origem"].strip(),
                    destino=form["destino"].strip(),
                    qtd_volumes=int(form["qtd_volumes"]),
                    valor_nf=float(form["valor_nf"].replace(".", "").replace(",", "."))
                    if "," in form["valor_nf"]
                    else float(form["valor_nf"]),
                    peso_kg=float(form["peso_kg"].replace(",", "."))
                    if form.get("peso_kg", "").strip()
                    else None,
                    modal=form.get("modal") or None,
                )
                tarifa = tarifas.buscar(pedido.origem, pedido.destino, pedido.modal)
                if tarifa is None:
                    if tarifas.trecho_cadastrado(pedido.origem, pedido.destino):
                        erro = (
                            "Trecho cadastrado, porém sem tarifa vigente "
                            "(INATIVO ou fora da vigência) — verifique a planilha."
                        )
                    else:
                        erro = "Rota não atendida."
                else:
                    resultado = precificacao.calcular(pedido, tarifa)
            except Exception as exc:
                erro = f"Não foi possível cotar: {exc}"
        return render_template(
            "cotar.html", pagina="cotar", resultado=resultado, erro=erro, form=form
        )
```

Criar `painel/templates/cotar.html`:

```html
{% extends "base.html" %}
{% block conteudo %}
<h2>Cotação manual</h2>
<p class="subtitulo">Usa a mesma tabela de tarifas do agente. Não consome a Claude API.</p>

<form method="post" class="linha">
  <label>Origem
    <input name="origem" required placeholder="Sao Paulo/SP" value="{{ form.get('origem', '') }}">
  </label>
  <label>Destino
    <input name="destino" required placeholder="Campinas/SP" value="{{ form.get('destino', '') }}">
  </label>
  <label>Volumes
    <input name="qtd_volumes" required type="number" min="1" value="{{ form.get('qtd_volumes', '') }}">
  </label>
  <label>Valor da NF (R$)
    <input name="valor_nf" required value="{{ form.get('valor_nf', '') }}">
  </label>
  <label>Peso total (kg)
    <input name="peso_kg" value="{{ form.get('peso_kg', '') }}">
  </label>
  <label>Modal
    <select name="modal">
      <option value="" {{ 'selected' if not form.get('modal') }}>automático</option>
      <option value="RODOVIARIO" {{ 'selected' if form.get('modal') == 'RODOVIARIO' }}>rodoviário</option>
      <option value="AEREO" {{ 'selected' if form.get('modal') == 'AEREO' }}>aéreo</option>
    </select>
  </label>
  <button type="submit">Cotar</button>
</form>

{% if erro %}<div class="alerta" style="margin-top:18px">{{ erro }}</div>{% endif %}

{% if resultado %}
<div class="cartao-detalhe" style="margin-top:18px">
  <h3>Rota {{ resultado.tarifa.id_rota }} [{{ resultado.tarifa.modal }}]
    · {{ resultado.tarifa.chave_origem }} → {{ resultado.tarifa.chave_destino }}</h3>
  <table>
    <tr><td>Frete por volume ({{ resultado.qtd_volumes }} vol.)</td><td>{{ resultado.frete_volumes | reais }}</td></tr>
    {% if resultado.usou_frete_minimo %}
    <tr><td>Frete mínimo aplicado</td><td>{{ resultado.frete_aplicado | reais }}</td></tr>
    {% endif %}
    <tr><td>GRIS + Advalorem</td><td>{{ resultado.gris_advalorem | reais }}</td></tr>
    {% if resultado.taxa_entrega_dificil %}
    <tr><td>Taxa entrega difícil</td><td>{{ resultado.taxa_entrega_dificil | reais }}</td></tr>
    {% endif %}
    <tr><th>Total</th><th>{{ resultado.total | reais }}</th></tr>
    {% if resultado.prazo_dias %}
    <tr><td>Prazo</td><td>{{ resultado.prazo_dias }} dia(s) úteis</td></tr>
    {% endif %}
  </table>
  {% if resultado.alerta_peso %}
  <div class="alerta" style="margin-top:12px">⚠ {{ resultado.alerta_peso }} — por email o agente NÃO responderia este caso.</div>
  {% endif %}
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: PASS (17 testes)

- [ ] **Step 5: Commit**

```bash
git add painel/app.py painel/templates/cotar.html cotador/tests/test_painel.py
git commit -m "feat: cotacao manual no painel com a composicao da planilha"
```

---

### Task 10: Página Agente (ligar/desligar, ciclo, modo, alerta)

**Files:**
- Modify: `painel/app.py`
- Create: `painel/templates/agente.html`
- Test: `cotador/tests/test_painel.py`

- [ ] **Step 1: Write the failing tests**

Adicionar a `cotador/tests/test_painel.py`:

```python
class TestPaginaAgente(BasePainel):
    def test_ligar_e_desligar_o_loop(self):
        self.cliente.post("/agente/acao", data={"acao": "ligar"})
        self.assertTrue(self.servico.rodando)
        self.cliente.post("/agente/acao", data={"acao": "desligar"})
        self.assertFalse(self.servico.rodando)

    def test_ciclo_agora_roda_um_ciclo(self):
        self.cliente.post("/agente/acao", data={"acao": "ciclo"}, follow_redirects=True)
        self.assertEqual(self.agente_fake.ciclos, 1)
        self.assertEqual(self.servico.ultimo_resumo, {"cotado": 1})

    def test_config_muda_modo_e_intervalo(self):
        self.cliente.post("/agente/acao", data={
            "acao": "config", "intervalo": "300", "modo": "enviar",
        })
        self.assertEqual(self.estado["modo"], "enviar")
        self.assertEqual(self.servico.intervalo_segundos, 300)

    def test_intervalo_tem_piso_de_30_segundos(self):
        self.cliente.post("/agente/acao", data={
            "acao": "config", "intervalo": "1", "modo": "rascunho",
        })
        self.assertEqual(self.servico.intervalo_segundos, 30)

    def test_alerta_de_credencial_aparece_na_pagina(self):
        arquivo = self.cfg.service_account_json.parent / "ALERTA_CREDENCIAL.txt"
        arquivo.write_text("AGENTE PARADO - CREDENCIAL RECUSADA", encoding="utf-8")
        corpo = self.cliente.get("/agente").get_data(as_text=True)
        self.assertIn("CREDENCIAL RECUSADA", corpo)

    def test_erro_num_ciclo_nao_derruba_a_rota(self):
        self.agente_fake.excecao = RuntimeError("planilha fora do ar")
        resposta = self.cliente.post(
            "/agente/acao", data={"acao": "ciclo"}, follow_redirects=True
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("planilha fora do ar", self.servico.ultimo_erro)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest cotador.tests.test_painel.TestPaginaAgente -v`
Expected: FAIL com 404 (rotas `/agente` e `/agente/acao` não existem)

- [ ] **Step 3: Implement**

Em `painel/app.py`, adicionar antes do `return app`:

```python
    # ---------------- controle do agente ----------------
    @app.get("/agente")
    def agente_pagina():
        arquivo_alerta = cfg.service_account_json.parent / "ALERTA_CREDENCIAL.txt"
        alerta = (
            arquivo_alerta.read_text(encoding="utf-8")
            if arquivo_alerta.exists()
            else None
        )
        return render_template(
            "agente.html",
            pagina="agente",
            alerta=alerta,
            intervalo=int(servico.intervalo_segundos),
        )

    @app.post("/agente/acao")
    def agente_acao():
        acao = request.form["acao"]
        if acao == "ligar":
            servico.ligar()
            flash("Loop ligado.")
        elif acao == "desligar":
            servico.desligar()
            flash("Loop desligado.")
        elif acao == "ciclo":
            try:
                resumo = servico.ciclo_unico()
                flash(f"Ciclo concluído: {resumo or 'nenhum email novo'}")
            except Exception:
                # O detalhe ja esta em servico.ultimo_erro, exibido na pagina.
                flash("Ciclo falhou — veja o último erro abaixo.")
        elif acao == "config":
            servico.intervalo_segundos = max(30, int(request.form["intervalo"]))
            estado["modo"] = (
                "enviar" if request.form.get("modo") == "enviar" else "rascunho"
            )
            flash("Configuração aplicada aos próximos ciclos.")
        return redirect(url_for("agente_pagina"))
```

Criar `painel/templates/agente.html`:

```html
{% extends "base.html" %}
{% block conteudo %}
<h2>Agente</h2>

{% if alerta %}<div class="alerta">{{ alerta }}</div>{% endif %}
{% if servico.credencial_recusada %}
<div class="alerta">Credencial recusada no último ciclo — o loop foi desligado. Corrija o .env e valide com --testar-imap.</div>
{% endif %}

<div class="cartao-detalhe">
  <h3>Loop</h3>
  <p>Estado: <strong>{{ '● rodando' if servico.rodando else '○ parado' }}</strong>
     · intervalo de {{ intervalo }}s · modo <strong>{{ modo }}</strong></p>
  <form method="post" action="{{ url_for('agente_acao') }}" class="linha">
    {% if servico.rodando %}
    <button name="acao" value="desligar" class="perigo" type="submit">Desligar</button>
    {% else %}
    <button name="acao" value="ligar" type="submit">Ligar</button>
    {% endif %}
    <button name="acao" value="ciclo" class="neutro" type="submit">Rodar 1 ciclo agora</button>
  </form>
</div>

<div class="cartao-detalhe">
  <h3>Configuração</h3>
  <form method="post" action="{{ url_for('agente_acao') }}" class="linha">
    <input type="hidden" name="acao" value="config">
    <label>Intervalo entre ciclos (s)
      <input name="intervalo" type="number" min="30" value="{{ intervalo }}">
    </label>
    <label>Modo de resposta
      <select name="modo">
        <option value="rascunho" {{ 'selected' if modo == 'rascunho' }}>rascunho (não envia)</option>
        <option value="enviar" {{ 'selected' if modo == 'enviar' }}>enviar ao cliente</option>
      </select>
    </label>
    <button type="submit">Aplicar</button>
  </form>
  <p class="subtitulo">Em modo rascunho a resposta fica em [Gmail]/Rascunhos e nada é enviado.</p>
</div>

<div class="cartao-detalhe">
  <h3>Último ciclo</h3>
  {% if servico.ultimo_ciclo_em %}
  <p>{{ servico.ultimo_ciclo_em }}</p>
  {% if servico.ultimo_resumo is not none %}<pre>{{ servico.ultimo_resumo }}</pre>{% endif %}
  {% if servico.ultimo_erro %}<p class="motivo">Último erro: {{ servico.ultimo_erro }}</p>{% endif %}
  {% else %}
  <p class="subtitulo">Nenhum ciclo rodou ainda nesta sessão.</p>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest cotador.tests.test_painel -v`
Expected: PASS (23 testes)

- [ ] **Step 5: Commit**

```bash
git add painel/app.py painel/templates/agente.html cotador/tests/test_painel.py
git commit -m "feat: pagina do agente com liga/desliga, ciclo unico, modo e alerta"
```

---

### Task 11: `main.py --painel` e documentação

**Files:**
- Modify: `main.py`
- Modify: `README.md`

Sem teste unitário novo: a montagem exige credenciais reais (Sheets/IMAP); toda a
lógica por trás já está coberta pelas tasks anteriores. A verificação é manual.

- [ ] **Step 1: Implement a flag**

Em `main.py`:

1. Adicionar `import dataclasses` junto aos imports.
2. No grupo de argumentos, adicionar:

```python
    grupo.add_argument(
        "--painel",
        action="store_true",
        help="sobe a interface web local de gestao (http://localhost:8000)",
    )
    # fora do grupo mutuamente exclusivo, junto do -v:
    p.add_argument("--porta", type=int, default=8000, help="porta do --painel")
```

3. Após a criação de `tabela` (depois do bloco `if args.cotar:` e antes da
   montagem do `agente`), adicionar:

```python
    if args.painel:
        from painel.app import criar_app
        from painel.servico_agente import ServicoAgente

        banco = Banco(cfg.banco)
        # Estado mutavel do painel. Inicia seguro: rascunho, loop desligado,
        # independentemente do MODO_RESPOSTA do .env.
        estado = {"modo": "rascunho"}

        def fabrica_agente() -> Agente:
            cfg_vigente = dataclasses.replace(cfg, modo_resposta=estado["modo"])
            return Agente(
                cfg=cfg_vigente,
                caixa=montar_caixa(cfg),
                tarifas=tabela,
                extrator=Extrator(
                    cfg.anthropic_api_key,
                    cfg.anthropic_model,
                    cfg.anthropic_workspace_id,
                ),
                banco=banco,
                enviador=montar_enviador(cfg) if estado["modo"] == "enviar" else None,
            )

        servico = ServicoAgente(fabrica_agente, cfg.intervalo_segundos)
        app = criar_app(
            cfg, banco, tabela, servico, lambda: montar_caixa(cfg), estado
        )
        print(f"Painel em http://localhost:{args.porta} (loop desligado, modo rascunho)")
        app.run(host="127.0.0.1", port=args.porta, debug=False)
        servico.desligar()
        return 0
```

- [ ] **Step 2: Sanity check de import e suíte completa**

```bash
python -c "import main; print('main importa ok')"
python -m unittest discover -s cotador/tests -t . -v
```

Expected: import ok; todos os testes passam.

- [ ] **Step 3: Verificação manual (requer .env e service_account.json)**

```bash
python main.py --painel
```

Expected: painel abre em http://localhost:8000; navegação entre as 4 páginas
funciona; loop parado; cotação manual devolve R$ 252,50 para o exemplo da
planilha. (Se as credenciais não estiverem disponíveis, registrar isso no
resultado da task — a página Cotar exibirá o erro tratado.)

- [ ] **Step 4: Atualizar o README**

Em `README.md`:

1. Na seção **Uso**, adicionar após o bloco do `--loop`:

```markdown
​```bash
python main.py --painel
​```

Sobe a interface web local de gestao em http://localhost:8000 (use `--porta` para
trocar). O painel mostra a operacao do dia, a fila de revisao humana (com botao
para devolver threads a fila), cotacao manual sem gastar API e o controle do
loop (ligar/desligar, ciclo unico, modo rascunho/enviar). Inicia sempre com o
loop desligado e em modo rascunho.
```

2. Na seção **Estrutura**, adicionar as linhas:

```
painel/app.py                       rotas e telas do painel web
painel/servico_agente.py            loop do agente em thread (liga/desliga)
painel/consultas.py                 leituras do SQLite para as telas
```

3. Na seção **Testes**, atualizar a contagem de "65 testes" para o total real
   após a implementação (rodar a suíte e usar o número exato).

- [ ] **Step 5: Commit**

```bash
git add main.py README.md
git commit -m "feat: flag --painel sobe a interface web local de gestao"
```

---

### Task 12: Verificação final

- [ ] **Step 1: Suíte completa**

```bash
python -m unittest discover -s cotador/tests -t . -v
```

Expected: todos os testes passam (65 antigos + 31 novos = 96).

- [ ] **Step 2: Conferir a spec**

Reler `docs/superpowers/specs/2026-09-01-painel-gestao-design.md` e conferir
item a item que cada requisito tem código e teste correspondente (4 páginas,
polling, devolver à fila, estado inicial seguro, migração do banco).

- [ ] **Step 3: Commit final (se houver ajustes)**

```bash
git add -A
git commit -m "chore: ajustes da verificacao final do painel"
```
