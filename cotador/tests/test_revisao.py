"""A fila de revisao humana: para onde vai um erro, e como se sai dela.

Cobre as tres correcoes do desfecho `erro`: o email fica nao-lido, o cliente
e avisado quando a falha nao e dele, e a fila e contavel por motivo.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cotador.agente import (
    MOTIVO_DESCONHECIDO,
    Agente,
    classificar_erro,
)
from cotador.core import mensagens
from cotador.core.modelos import Email, PedidoCotacao
from cotador.integracoes.banco import Banco


def email_exemplo() -> Email:
    return Email(
        id="msg-1",
        thread_id="thr-1",
        remetente="cliente@exemplo.com",
        nome_remetente="Roberto Silva",
        assunto="cotacao de frete",
        corpo="10 volumes de SP para Campinas",
        message_id_header="<a@b>",
        references_header=None,
        uid="42",
    )


class TestClassificacaoDaFila(unittest.TestCase):
    """O texto livre da coluna `erro` tem de virar categoria contavel."""

    def test_corte_de_035_e_corte_de_060_sao_motivos_diferentes(self):
        baixa = classificar_erro("confianca 0.20 abaixo de 0.35")
        preco = classificar_erro("confianca 0.42 abaixo de 0.6 para emitir preco")
        self.assertNotEqual(baixa, preco)
        self.assertIn("nao sabemos", baixa)
        self.assertIn("emitir preco", preco)

    def test_quarentena_da_curadoria(self):
        texto = (
            "trecho Sao Paulo/SP -> Campinas/SP em quarentena pela curadoria: "
            "linha 2 R00001 | gris_percentual: 30,00% fora da faixa"
        )
        self.assertIn("quarentena", classificar_erro(texto))

    def test_tarifa_vencida_nao_se_confunde_com_quarentena(self):
        texto = "trecho X -> Y cadastrado, porem sem tarifa vigente (INATIVO ou vigencia expirada)"
        rotulo = classificar_erro(texto)
        self.assertIn("inativa", rotulo)
        self.assertNotIn("quarentena", rotulo)

    def test_limite_de_peso(self):
        texto = "peso medio de 250.0 kg por volume excede o limite de 100 kg da rota R00001"
        self.assertIn("peso", classificar_erro(texto))

    def test_texto_desconhecido_cai_em_falha_tecnica(self):
        self.assertEqual(classificar_erro("ConnectionResetError(...)"), MOTIVO_DESCONHECIDO)
        self.assertEqual(classificar_erro(None), MOTIVO_DESCONHECIDO)
        self.assertEqual(classificar_erro(""), MOTIVO_DESCONHECIDO)


class TestEmailDeErroFicaNaoLido(unittest.TestCase):
    """A busca do agente e `is:unread`: marcar como lido some com o email."""

    def _agente(self):
        ag = object.__new__(Agente)
        ag.cfg = mock.Mock(LABEL_REVISAR="cotador-revisar")
        ag.caixa = mock.Mock()
        ag.banco = mock.Mock()
        return ag

    def _remover_unread(self, ag):
        return ag.caixa.aplicar_labels.call_args.kwargs["remover_unread"]

    def test_erro_preserva_o_nao_lido(self):
        ag = self._agente()
        ag._fechar(email_exemplo(), "erro", label="cotador-revisar", erro="qualquer")
        self.assertFalse(self._remover_unread(ag))

    def test_cotado_marca_como_lido(self):
        ag = self._agente()
        ag._fechar(email_exemplo(), "cotado", label="cotador-processado")
        self.assertTrue(self._remover_unread(ag))

    def test_incompleto_marca_como_lido(self):
        # O cliente vai responder, e a resposta chega como um email novo.
        ag = self._agente()
        ag._fechar(email_exemplo(), "incompleto", label="cotador-aguardando-dados")
        self.assertTrue(self._remover_unread(ag))

    def test_registro_no_banco_acontece_em_todos_os_casos(self):
        ag = self._agente()
        ag._fechar(email_exemplo(), "erro", label="cotador-revisar", erro="x")
        self.assertEqual(ag.banco.registrar.call_args.kwargs["desfecho"], "erro")


class TestRespostaQuandoAFalhaNaoEDoCliente(unittest.TestCase):
    def _pedido(self):
        return PedidoCotacao(
            e_cotacao=True,
            confianca=0.9,
            origem="Sao Paulo/SP",
            destino="Campinas/SP",
            qtd_volumes=10,
            valor_nf=8000.0,
            peso_kg=2500.0,
        )

    def test_diz_o_trecho_e_o_prazo(self):
        texto = mensagens.aguardar_analise(self._pedido(), "Roberto")
        self.assertIn("Roberto", texto)
        self.assertIn("Sao Paulo/SP -> Campinas/SP", texto)
        self.assertIn(mensagens.PRAZO_REVISAO_HUMANA, texto)

    def test_nao_vaza_o_motivo_interno(self):
        # O motivo e cadastro nosso ou limite de rota: nada disso vai ao cliente.
        texto = mensagens.aguardar_analise(self._pedido(), "Roberto").lower()
        for vazamento in ("gris", "quarentena", "tarifa", "curadoria", "peso maximo", "planilha"):
            self.assertNotIn(vazamento, texto, vazamento)

    def test_funciona_sem_trecho_extraido(self):
        magro = PedidoCotacao(e_cotacao=True, confianca=0.9)
        texto = mensagens.aguardar_analise(magro, "cliente")
        self.assertIn("Recebemos sua solicitacao", texto)
        self.assertNotIn("None", texto)

    def test_sem_linha_em_branco_sobrando(self):
        texto = mensagens.aguardar_analise(self._pedido(), "Roberto")
        nl = chr(10)
        self.assertNotIn(nl + " " + nl, texto)
        self.assertNotIn(nl + "  " + nl, texto)


class TestTodoEmailSeIdentificaComoAutomatico(unittest.TestCase):
    """Recurso de quem recebe: saber que foi um sistema, e como falar com gente."""

    def _todos(self) -> list[str]:
        from cotador.core.precificacao import calcular
        from cotador.tests.test_curadoria import tarifa

        pedido = PedidoCotacao(
            e_cotacao=True,
            confianca=1.0,
            origem="Sao Paulo/SP",
            destino="Campinas/SP",
            qtd_volumes=10,
            valor_nf=8000.0,
            peso_kg=300.0,
        )
        return [
            mensagens.enviar_cotacao(pedido, calcular(pedido, tarifa()), "Roberto"),
            mensagens.solicitar_dados(PedidoCotacao(e_cotacao=True, confianca=0.5), "Ana"),
            mensagens.sem_rota(pedido, "Ana"),
            mensagens.aguardar_analise(pedido, "Ana"),
        ]

    def test_diz_que_e_automatico(self):
        for texto in self._todos():
            self.assertIn("automaticamente", texto)

    def test_oferece_uma_pessoa(self):
        for texto in self._todos():
            self.assertIn("atendimento humano", texto)


class TestFilaNoBanco(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.banco = Banco(Path(self.dir.name) / "teste.sqlite3")

    def tearDown(self):
        self.dir.cleanup()

    def _erro(self, id_email: str, quando: datetime, motivo: str) -> None:
        self.banco.registrar(
            id_email=id_email,
            thread_id="thr-" + id_email,
            remetente=f"{id_email}@exemplo.com",
            assunto="cotacao",
            desfecho="erro",
            origem="SAO PAULO/SP",
            destino="CAMPINAS/SP",
            erro=motivo,
        )
        # `registrar` grava o agora; reescrevemos a data para testar a ordem.
        import sqlite3

        con = sqlite3.connect(self.banco._caminho)
        try:
            con.execute(
                "UPDATE processados SET criado_em = ? WHERE id_email = ?",
                (quando.isoformat(timespec="seconds"), id_email),
            )
            con.commit()
        finally:
            con.close()

    def test_fila_vazia(self):
        self.assertEqual(self.banco.contar_pendentes_revisao(), 0)
        self.assertEqual(self.banco.pendentes_revisao(), [])

    def test_conta_e_ordena_do_mais_antigo(self):
        agora = datetime.now(timezone.utc)
        self._erro("novo", agora, "confianca 0.20 abaixo de 0.35")
        self._erro("velho", agora - timedelta(days=5), "peso medio de 250.0 kg")

        self.assertEqual(self.banco.contar_pendentes_revisao(), 2)
        fila = self.banco.pendentes_revisao()
        self.assertEqual([i["remetente"] for i in fila],
                         ["velho@exemplo.com", "novo@exemplo.com"])
        self.assertIn("peso", classificar_erro(fila[0]["erro"]))

    def test_cotado_nao_entra_na_fila(self):
        self.banco.registrar(
            id_email="ok-1",
            thread_id="t",
            remetente="c@x.com",
            assunto="a",
            desfecho="cotado",
            valor_frete=252.50,
        )
        self.assertEqual(self.banco.contar_pendentes_revisao(), 0)

    def test_reprocessar_limpa_a_fila(self):
        self._erro("a", datetime.now(timezone.utc), "confianca 0.20 abaixo de 0.35")
        self.assertEqual(self.banco.limpar_erros(), 1)
        self.assertEqual(self.banco.contar_pendentes_revisao(), 0)

    def test_limite_respeitado(self):
        agora = datetime.now(timezone.utc)
        for n in range(5):
            self._erro(f"e{n}", agora - timedelta(hours=n), "falha X")
        self.assertEqual(len(self.banco.pendentes_revisao(limite=2)), 2)
        self.assertEqual(self.banco.contar_pendentes_revisao(), 5)


if __name__ == "__main__":
    unittest.main()
