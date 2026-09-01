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
