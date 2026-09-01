"""Testes do painel: IMAP de devolucao, servico do loop e rotas Flask."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cotador.integracoes.banco import Banco
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


class AgenteTravado:
    """Fica dentro de rodar_ciclo ate o teste liberar, simulando ciclo longo."""

    def __init__(self):
        self.entrou = threading.Event()
        self.liberar = threading.Event()
        self.ciclos = 0

    def rodar_ciclo(self):
        self.ciclos += 1
        self.entrou.set()
        self.liberar.wait(timeout=10)  # rede de seguranca: nao trava a suite
        return {"cotado": 0}


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
            time.sleep(0.01)
        servico.desligar()

        self.assertFalse(servico.rodando)
        self.assertGreaterEqual(agente.ciclos, 1)
        ciclos_apos_desligar = agente.ciclos
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
            time.sleep(0.01)

        self.assertFalse(servico.rodando)
        self.assertTrue(servico.credencial_recusada)
        self.assertIn("senha ruim", servico.ultimo_erro)
        self.assertEqual(agente.ciclos, 1)  # nao insiste em credencial ruim

    def test_ligar_duas_vezes_nao_cria_segunda_thread(self):
        from painel.servico_agente import ServicoAgente

        servico = ServicoAgente(lambda: AgenteFake(), intervalo_segundos=60)

        servico.ligar()
        self.addCleanup(servico.desligar)
        primeira = servico._thread
        servico.ligar()

        self.assertIs(servico._thread, primeira)
        self.assertTrue(servico.rodando)

    def test_desligar_sem_ligar_nao_quebra(self):
        from painel.servico_agente import ServicoAgente

        servico = ServicoAgente(lambda: AgenteFake(), intervalo_segundos=60)

        servico.desligar()

        self.assertFalse(servico.rodando)

    def test_desligar_com_ciclo_travado_preserva_o_handle(self):
        from painel.servico_agente import ServicoAgente

        agente = AgenteTravado()
        servico = ServicoAgente(
            lambda: agente, intervalo_segundos=0.01, timeout_desligar=0.05
        )
        self.addCleanup(agente.liberar.set)  # nunca deixa a thread pendurada

        servico.ligar()
        self.assertTrue(agente.entrou.wait(timeout=5))

        # O join estoura: o ciclo ainda esta dentro do fake.
        with self.assertLogs("painel.servico_agente", "WARNING") as reg:
            servico.desligar()
        self.assertIn("ainda em andamento", "\n".join(reg.output))

        self.assertTrue(servico.rodando)
        self.assertIsNotNone(servico._thread)
        orfa = servico._thread

        servico.ligar()  # com a orfa viva, ligar nao pode criar um segundo laco
        self.assertIs(servico._thread, orfa)

        agente.liberar.set()
        for _ in range(500):
            if not servico.rodando:
                break
            time.sleep(0.01)

        self.assertFalse(servico.rodando)
        self.assertEqual(agente.ciclos, 1)  # o pedido de parada seguiu valendo
        servico.desligar()  # agora sim recolhe o handle, sem erro
        self.assertIsNone(servico._thread)

    def test_ligar_de_novo_apos_credencial_recusada_rearma_o_loop(self):
        from cotador.integracoes.caixa_imap import CredencialInvalida
        from painel.servico_agente import ServicoAgente

        agente = AgenteFake(excecao=CredencialInvalida("senha ruim"))
        servico = ServicoAgente(lambda: agente, intervalo_segundos=0.01)

        servico.ligar()
        for _ in range(200):
            if not servico.rodando:
                break
            time.sleep(0.01)
        self.assertTrue(servico.credencial_recusada)

        agente.excecao = None  # humano corrigiu a senha e religou
        servico.ligar()
        self.addCleanup(servico.desligar)
        for _ in range(200):
            if not servico.credencial_recusada:
                break
            time.sleep(0.01)

        self.assertFalse(servico.credencial_recusada)
        self.assertEqual(servico.ultimo_resumo, {"cotado": 1})
        self.assertIsNone(servico.ultimo_erro)


class TestConsultas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.caminho = Path(self._tmp.name) / "t.sqlite3"
        self.banco = Banco(self.caminho)

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

    def test_fila_de_revisao_tolera_extracao_corrompida(self):
        from painel import consultas

        self.banco.registrar(
            id_email="a", thread_id="t", remetente="x@y.com", assunto="s",
            desfecho="erro", label="cotador-revisar", extracao={"origem": "SP"},
        )
        con = sqlite3.connect(self.caminho)
        con.execute(
            "UPDATE processados SET extracao_json = ? WHERE id_email = ?",
            ("{isto nao e json", "a"),
        )
        con.commit()
        con.close()

        itens = consultas.fila_de_revisao(self.banco, "cotador-revisar")

        self.assertEqual(itens[0]["extracao"], "{isto nao e json")


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

    def test_banco_vazio_mostra_a_linha_de_tabela_vazia(self):
        corpo = self.cliente.get("/").get_data(as_text=True)
        self.assertIn("Nenhum email processado ainda.", corpo)

    def test_frete_ausente_vira_travessao_na_tabela(self):
        # Desfecho sem cotacao (sem_rota) nao tem valor_frete: a celula precisa
        # de um placeholder, nao do "None" cru.
        self.banco.registrar(
            id_email="a", thread_id="t", remetente="cliente@acme.com",
            assunto="Cotacao", desfecho="sem_rota",
            origem="Manaus/AM", destino="Campinas/SP",
            label="cotador-processado",
        )
        corpo = self.cliente.get("/").get_data(as_text=True)
        self.assertNotIn("None", corpo)
        self.assertIn("<td>—</td>", corpo)

    def test_assunto_com_html_sai_escapado(self):
        # O assunto vem de email de terceiro: nada de |safe no template.
        self.banco.registrar(
            id_email="a", thread_id="t", remetente="cliente@acme.com",
            assunto="<script>alert(1)</script>", desfecho="cotado",
            label="cotador-processado",
        )
        corpo = self.cliente.get("/").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", corpo)
        self.assertIn("&lt;script&gt;", corpo)


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


if __name__ == "__main__":
    unittest.main()
