from __future__ import annotations

import json
import math
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import numpy as np

from cotador.core import mensagens
from cotador.core.modelos import PedidoCotacao, Tarifa
from cotador.integracoes.banco import Banco
from cotador.ml.exceptions import CotacaoForaDoDominio, EntradaHistoricaInvalida
from cotador.ml.features import haversine_km
from cotador.ml.geografia import LocalNaoResolvido, ResolverGeografico
from cotador.ml.historico import PrecificadorHistorico


class ModeloFake:
    def __init__(self, valor):
        self.valor = valor

    def predict(self, dados):
        return np.full(len(dados), np.log1p(self.valor))


class OutlierFake:
    def __init__(self, anomalo=False):
        self.anomalo = anomalo

    def predict(self, dados):
        return np.full(len(dados), -1 if self.anomalo else 1)

    def decision_function(self, dados):
        return np.full(len(dados), -0.2 if self.anomalo else 0.2)


def tarifa() -> Tarifa:
    return Tarifa(
        id_rota="R1", cidade_origem="sao paulo", uf_origem="SP",
        cidade_destino="belo horizonte", uf_destino="MG", valor_por_volume=20,
        frete_minimo=50, prazo_dias=3, distancia_km=590,
    )


class BaseML(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        mapa = self.raiz / "geo_map.csv"
        mapa.write_text(
            "zip_prefix,lat,lng,city,state\n"
            "01001,-23.5505,-46.6333,sao paulo,SP\n"
            "30110,-19.9167,-43.9345,belo horizonte,MG\n",
            encoding="utf-8",
        )
        self.resolver = ResolverGeografico(mapa)

    def tearDown(self):
        self.tmp.cleanup()

    def precificador(self, anomalo=False):
        p = PrecificadorHistorico.__new__(PrecificadorHistorico)
        p.modelos = {"p25": ModeloFake(100), "p50": ModeloFake(150), "p75": ModeloFake(220)}
        p.outlier = OutlierFake(anomalo)
        p.metadata = {"model_version": "teste"}
        p.ajustes = {"p25": 0, "p50": 0, "p75": 0}
        p.resolver = self.resolver
        p.bloquear_outlier = True
        return p


class TestGeografia(BaseML):
    def test_haversine(self):
        self.assertTrue(math.isclose(float(haversine_km(0, 0, 0, 1)), 111.195, rel_tol=1e-4))

    def test_resolve_cidade_uf_e_cep(self):
        self.assertEqual(self.resolver.resolver("São Paulo/SP").uf, "SP")
        self.assertEqual(self.resolver.resolver("30110-028").cidade, "belo horizonte")

    def test_local_inexistente_nao_e_inventado(self):
        with self.assertRaises(LocalNaoResolvido):
            self.resolver.resolver("Cidade Inexistente/XX")


class TestPrecificadorHistorico(BaseML):
    def pedido(self):
        return PedidoCotacao(e_cotacao=True, confianca=.9, origem="São Paulo/SP",
                             destino="Belo Horizonte/MG", qtd_volumes=3,
                             valor_nf=1000, peso_kg=20)

    def test_p50_vira_preco_padrao(self):
        resultado = self.precificador().cotar(self.pedido(), tarifa())
        self.assertEqual(resultado.total, 150)
        self.assertEqual(resultado.p25, 100)
        self.assertEqual(resultado.p75, 220)
        self.assertEqual(resultado.fonte, "historico_olist")
        self.assertEqual(resultado.distancia_km, 590)
        self.assertTrue(resultado.quote_id.startswith("Q-"))

    def test_peso_e_obrigatorio(self):
        pedido = self.pedido(); pedido.peso_kg = None
        with self.assertRaises(EntradaHistoricaInvalida):
            self.precificador().cotar(pedido, tarifa())

    def test_outlier_bloqueia_preco_automatico(self):
        with self.assertRaises(CotacaoForaDoDominio):
            self.precificador(anomalo=True).cotar(self.pedido(), tarifa())

    def test_email_historico_nao_expoe_formula_antiga(self):
        cotacao = self.precificador().cotar(self.pedido(), tarifa())
        texto = mensagens.enviar_cotacao(self.pedido(), cotacao, "Ana")
        self.assertIn("R$ 150,00", texto)
        self.assertNotIn("GRIS", texto)
        self.assertNotIn("Frete minimo", texto)


class TestAgenteComHistorico(BaseML):
    def test_interface_existente_envia_p50_e_persiste_benchmark(self):
        from cotador.agente import Agente
        from cotador.core.modelos import Email

        pedido = PedidoCotacao(True, .9, "São Paulo/SP", "Belo Horizonte/MG", 1, 850, 3.2)
        email = Email("email-ml", "thread-ml", "cliente@exemplo.com", "Ana", "Cotação", "texto", None, None, "1")

        class Caixa:
            def __init__(self): self.rascunhos = []
            def ler(self, uid): return email
            def criar_rascunho(self, email_recebido, texto, remetente): self.rascunhos.append(texto)
            def aplicar_labels(self, *args, **kwargs): pass

        class Tarifas:
            def buscar(self, *args): return tarifa()

        class Extrator:
            def analisar(self, *args): return pedido

        class Cfg:
            precificador = "historico"; exigir_peso = True; modo_resposta = "rascunho"
            gmail_user = "agente@exemplo.com"; remetente = "Agente <agente@exemplo.com>"
            LABEL_REVISAR = "cotador-revisar"; LABEL_PROCESSADO = "cotador-processado"
            LABEL_INCOMPLETO = "cotador-incompleto"; LABEL_SEM_ROTA = "cotador-sem-rota"

        banco = Banco(self.raiz / "agente.sqlite3"); caixa = Caixa()
        agente = Agente(Cfg(), caixa, Tarifas(), Extrator(), banco,
                        precificador_historico=self.precificador())
        self.assertEqual(agente._processar("1"), "cotado")
        self.assertIn("R$ 150,00", caixa.rascunhos[0])
        registro = banco.ultimos(1)[0]
        self.assertTrue(registro["quote_id"].startswith("Q-"))
        self.assertEqual(banco.obter_cotacao(registro["quote_id"])["p50"], 150)


class TestHistoricoNoBanco(unittest.TestCase):
    def test_registra_aceita_rejeita_e_preserva_eventos(self):
        with tempfile.TemporaryDirectory() as pasta:
            banco = Banco(Path(pasta) / "cotador.sqlite3")
            banco.registrar_cotacao_modelo(
                quote_id="Q-TESTE", id_email="email-1", thread_id="thread-1",
                payload={"peso_kg": 10}, p25=10, p50=20, p75=30, recommended=20,
                distance_km=100, structural_outlier=False, structural_score=.1,
                model_version="v1",
            )
            aceita = banco.confirmar_cotacao("Q-TESTE", 19.9, actual_cost=12)
            self.assertEqual(aceita["status"], "ACCEPTED")
            self.assertEqual(aceita["contracted_price"], 19.9)
            rejeitada = banco.rejeitar_cotacao("Q-TESTE", "cliente desistiu")
            self.assertEqual(rejeitada["status"], "REJECTED")
            with closing(banco._conectar()) as con:
                eventos = con.execute("SELECT event_type FROM eventos_cotacao WHERE quote_id=? ORDER BY id", ("Q-TESTE",)).fetchall()
            self.assertEqual([e[0] for e in eventos], ["SENT", "ACCEPTED", "REJECTED"])
            json.loads(rejeitada["payload_json"])
