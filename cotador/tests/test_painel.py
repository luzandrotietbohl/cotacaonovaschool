"""Testes do painel: IMAP de devolucao, servico do loop e rotas Flask."""
from __future__ import annotations

import unittest

from cotador.integracoes.caixa_imap import CaixaIMAP


class ConIMAPFake:
    """Grava os comandos UID emitidos e devolve respostas configuraveis."""

    def __init__(
        self,
        uid_encontrado: bytes | None = b"7",
        store_ok: bool = True,
        search_ok: bool = True,
    ):
        self.comandos: list[tuple] = []
        self._uid = uid_encontrado
        self._store_ok = store_ok
        self._search_ok = search_ok

    def uid(self, comando, *args):
        self.comandos.append((comando, *args))
        if comando == "SEARCH":
            if not self._search_ok:
                return "NO", [b"erro de busca"]
            return "OK", [self._uid or b""]
        if not self._store_ok:
            return "NO", [b"erro"]
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

    def test_falha_ao_remover_label_devolve_false(self):
        # Sem o label removido o email nunca volta para 'is:unread -label:...',
        # entao reportar sucesso enganaria o painel.
        con = ConIMAPFake(uid_encontrado=b"42", store_ok=False)
        caixa = caixa_com_con(con)

        ok = caixa.devolver_para_fila("111222333", ["cotador-revisar"])

        self.assertFalse(ok)

    def test_busca_com_erro_avisa_causa_diferente(self):
        con = ConIMAPFake(search_ok=False)
        caixa = caixa_com_con(con)

        with self.assertLogs("cotador.integracoes.caixa_imap", "WARNING") as reg:
            ok = caixa.devolver_para_fila("999", ["cotador-revisar"])

        self.assertFalse(ok)
        self.assertNotIn("nao encontrado", "\n".join(reg.output))


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


if __name__ == "__main__":
    unittest.main()
