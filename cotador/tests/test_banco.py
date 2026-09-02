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

    def test_ids_da_thread_filtra_por_label(self):
        # Thread mista: so o que esta em revisao pode voltar para a fila.
        self.registrar(id_email="a", thread_id="thr-9", label="cotador-revisar")
        self.registrar(id_email="b", thread_id="thr-9", label="cotador-processado")

        self.assertEqual(
            self.banco.ids_da_thread("thr-9", label="cotador-revisar"), ["a"]
        )
        self.assertEqual(sorted(self.banco.ids_da_thread("thr-9")), ["a", "b"])

    def test_ids_da_thread_lista_sem_apagar(self):
        # O painel precisa saber o que devolver ao Gmail ANTES de apagar.
        self.registrar(id_email="a", thread_id="thr-9")
        self.registrar(id_email="b", thread_id="thr-9")
        self.registrar(id_email="fora", thread_id="outra")

        ids = self.banco.ids_da_thread("thr-9")

        self.assertEqual(sorted(ids), ["a", "b"])
        self.assertTrue(self.banco.ja_processado("a"))  # nada foi removido

    def test_apagar_emails_remove_so_os_ids_pedidos(self):
        self.registrar(id_email="a", thread_id="thr-9")
        self.registrar(id_email="b", thread_id="thr-9")
        self.registrar(id_email="c", thread_id="thr-9")

        apagados = self.banco.apagar_emails(["a", "c"])

        self.assertEqual(apagados, 2)
        self.assertFalse(self.banco.ja_processado("a"))
        self.assertTrue(self.banco.ja_processado("b"))
        self.assertFalse(self.banco.ja_processado("c"))

    def test_apagar_emails_com_lista_vazia_nao_apaga_nada(self):
        self.registrar(id_email="a")
        self.assertEqual(self.banco.apagar_emails([]), 0)
        self.assertTrue(self.banco.ja_processado("a"))


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


if __name__ == "__main__":
    unittest.main()


class TestCaminhoDoBancoViaEnv(unittest.TestCase):
    """BANCO_CAMINHO no .env move o SQLite para fora de pastas sincronizadas
    (Google Drive/OneDrive corrompem gravacoes do SQLite)."""

    _OBRIGATORIAS = {"GMAIL_USER": "conta@gmail.com", "SHEET_ID": "sheet"}

    def _carregar_com(self, **extras):
        import os
        from cotador.config import Config

        antes = {k: os.environ.get(k) for k in {**self._OBRIGATORIAS, **extras}}

        def restaurar():
            for k, v in antes.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(restaurar)
        os.environ.update(self._OBRIGATORIAS)
        os.environ.update(extras)
        return Config.carregar()

    def test_env_define_o_caminho(self):
        cfg = self._carregar_com(BANCO_CAMINHO=r"C:\dados\meu.sqlite3")
        self.assertEqual(str(cfg.banco), r"C:\dados\meu.sqlite3")

    def test_env_expande_variaveis_do_windows(self):
        import os

        os.environ["_TESTE_BASE"] = r"C:\base"
        self.addCleanup(lambda: os.environ.pop("_TESTE_BASE", None))
        cfg = self._carregar_com(BANCO_CAMINHO=r"%_TESTE_BASE%\cotador.sqlite3")
        self.assertEqual(str(cfg.banco), r"C:\base\cotador.sqlite3")

    def test_sem_env_usa_o_padrao_no_repo(self):
        cfg = self._carregar_com(BANCO_CAMINHO="")
        self.assertTrue(str(cfg.banco).endswith("cotador.sqlite3"))
        self.assertIn("dados", str(cfg.banco))
