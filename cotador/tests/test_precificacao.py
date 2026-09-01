"""Testes das regras de negocio (rodam sem credenciais e sem rede)."""
from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path as pathlib_path

from cotador.core.modelos import PedidoCotacao, Tarifa, Volume
from cotador.core.precificacao import calcular, normalizar_local, verificar_peso


def tarifa_exemplo(**over) -> Tarifa:
    """Rota R00001 real da planilha: Sao Paulo -> Campinas (valores da aba EXEMPLO_CALCULO)."""
    base = dict(
        id_rota="R00001",
        cidade_origem="SAO PAULO",
        uf_origem="SP",
        cidade_destino="CAMPINAS",
        uf_destino="SP",
        valor_por_volume=16.65,
        frete_minimo=55.00,
        pedagio_por_volume=4.20,
        gris_percentual=0.30,
        advalorem_percentual=0.25,
        taxa_entrega_dificil=0.00,
        peso_maximo_volume_kg=100.0,
        prazo_dias=1,
    )
    base.update(over)
    return Tarifa(**base)


class TestNormalizarLocal(unittest.TestCase):
    def test_variacoes_da_mesma_cidade_convergem(self):
        alvo = "SAO JOSE DOS CAMPOS/SP"
        for entrada in [
            "Sao Jose dos Campos - sp",
            "SÃO JOSÉ DOS CAMPOS/SP",
            "são josé dos campos , SP",
        ]:
            self.assertEqual(normalizar_local(entrada), alvo, entrada)

    def test_cep_vira_digitos(self):
        self.assertEqual(normalizar_local("12245-000"), "12245000")

    def test_vazio(self):
        self.assertEqual(normalizar_local(None), "")


class TestExemploCalculoDaPlanilha(unittest.TestCase):
    """Confere o resultado contra os numeros da aba EXEMPLO_CALCULO:
    10 volumes, NF R$ 8.000, rota R00001 -> TOTAL R$ 252,50."""

    def setUp(self):
        self.pedido = PedidoCotacao(
            e_cotacao=True,
            confianca=1.0,
            origem="Sao Paulo/SP",
            destino="Campinas/SP",
            qtd_volumes=10,
            valor_nf=8000.0,
            peso_kg=300.0,
        )
        self.cotacao = calcular(self.pedido, tarifa_exemplo())

    def test_frete_por_volume(self):
        self.assertAlmostEqual(self.cotacao.frete_volumes, 208.50)

    def test_frete_aplicado_ignora_minimo_quando_maior(self):
        self.assertAlmostEqual(self.cotacao.frete_aplicado, 208.50)
        self.assertFalse(self.cotacao.usou_frete_minimo)

    def test_gris_mais_advalorem(self):
        self.assertAlmostEqual(self.cotacao.gris_advalorem, 44.00)

    def test_total(self):
        self.assertAlmostEqual(self.cotacao.total, 252.50)

    def test_prazo(self):
        self.assertEqual(self.cotacao.prazo_dias, 1)


class TestFreteMinimo(unittest.TestCase):
    def test_minimo_prevalece_em_carga_pequena(self):
        pedido = PedidoCotacao(
            e_cotacao=True, confianca=1, origem="a", destino="b", qtd_volumes=1, valor_nf=1000.0
        )
        c = calcular(pedido, tarifa_exemplo())
        self.assertAlmostEqual(c.frete_volumes, 20.85)
        self.assertAlmostEqual(c.frete_aplicado, 55.00)  # frete minimo
        self.assertTrue(c.usou_frete_minimo)
        self.assertAlmostEqual(c.total, 60.50)  # 55.00 + 1000*0.55%


class TestTaxaEntregaDificil(unittest.TestCase):
    def test_taxa_soma_no_total(self):
        pedido = PedidoCotacao(
            e_cotacao=True, confianca=1, origem="a", destino="b", qtd_volumes=10, valor_nf=8000.0
        )
        c = calcular(pedido, tarifa_exemplo(taxa_entrega_dificil=35.0))
        self.assertAlmostEqual(c.total, 287.50)


class TestQuantidadeDeVolumes(unittest.TestCase):
    def test_soma_a_lista_quando_nao_ha_numero_explicito(self):
        pedido = PedidoCotacao(
            e_cotacao=True,
            confianca=1,
            volumes=[Volume(quantidade=2), Volume(quantidade=1)],
        )
        self.assertEqual(pedido.volumes_efetivos, 3)

    def test_numero_explicito_tem_precedencia(self):
        pedido = PedidoCotacao(
            e_cotacao=True, confianca=1, qtd_volumes=12, volumes=[Volume(quantidade=2)]
        )
        self.assertEqual(pedido.volumes_efetivos, 12)

    def test_sem_volumes(self):
        self.assertIsNone(PedidoCotacao(e_cotacao=True, confianca=1).volumes_efetivos)


class TestCamposFaltantes(unittest.TestCase):
    def test_detecta_ausencias(self):
        p = PedidoCotacao(e_cotacao=True, confianca=0.9, origem="SP/SP")
        self.assertEqual(
            p.campos_faltantes(), ["destino", "qtd_volumes", "valor_nf", "peso"]
        )
        self.assertFalse(p.completo())

    def test_peso_opcional_quando_desligado(self):
        p = PedidoCotacao(
            e_cotacao=True,
            confianca=0.9,
            origem="SP/SP",
            destino="RJ/RJ",
            qtd_volumes=3,
            valor_nf=1500.0,
        )
        self.assertEqual(p.campos_faltantes(exigir_peso=False), [])
        self.assertTrue(p.completo(exigir_peso=False))
        self.assertEqual(p.campos_faltantes(exigir_peso=True), ["peso"])


class TestLimiteDePeso(unittest.TestCase):
    def test_alerta_quando_peso_por_volume_estoura(self):
        pedido = PedidoCotacao(
            e_cotacao=True, confianca=1, qtd_volumes=2, valor_nf=1000.0, peso_kg=500.0
        )
        alerta = verificar_peso(pedido, tarifa_exemplo(peso_maximo_volume_kg=100.0))
        self.assertIsNotNone(alerta)
        self.assertIn("250.0 kg", alerta)

    def test_sem_alerta_dentro_do_limite(self):
        pedido = PedidoCotacao(
            e_cotacao=True, confianca=1, qtd_volumes=10, valor_nf=1000.0, peso_kg=300.0
        )
        self.assertIsNone(verificar_peso(pedido, tarifa_exemplo(peso_maximo_volume_kg=100.0)))


class TestVigencia(unittest.TestCase):
    def test_inativa_nao_vigora(self):
        t = tarifa_exemplo(status="INATIVO")
        self.assertFalse(t.vigente_em(date(2026, 8, 31)))

    def test_fora_da_janela(self):
        t = tarifa_exemplo(
            vigencia_inicio=date(2026, 1, 1), vigencia_fim=date(2026, 6, 30)
        )
        self.assertFalse(t.vigente_em(date(2026, 8, 31)))
        self.assertTrue(t.vigente_em(date(2026, 3, 15)))

    def test_sem_janela_vigora_sempre(self):
        self.assertTrue(tarifa_exemplo().vigente_em(date(2026, 8, 31)))


class TestErrosDeEntrada(unittest.TestCase):
    def test_sem_volumes_estoura(self):
        p = PedidoCotacao(e_cotacao=True, confianca=1, valor_nf=100.0)
        with self.assertRaises(ValueError):
            calcular(p, tarifa_exemplo())

    def test_sem_valor_nf_estoura(self):
        p = PedidoCotacao(e_cotacao=True, confianca=1, qtd_volumes=2)
        with self.assertRaises(ValueError):
            calcular(p, tarifa_exemplo())


class TestBuscaDeRota(unittest.TestCase):
    """Separacao entre 'rota nao cadastrada' e 'tarifa sem vigencia'.

    TabelaTarifas e instanciada sem __init__ para nao exigir credenciais:
    os metodos de consulta so dependem da lista carregada.
    """

    def setUp(self):
        from cotador.integracoes.planilha import TabelaTarifas

        self.tabela = object.__new__(TabelaTarifas)
        self.tabela._tarifas = [
            tarifa_exemplo(),
            tarifa_exemplo(
                id_rota="R00045", cidade_destino="CURITIBA", uf_destino="PR", status="INATIVO"
            ),
            tarifa_exemplo(
                id_rota="R00900",
                cidade_destino="VITORIA",
                uf_destino="ES",
                modal="AEREO",
                valor_por_volume=136.93,
                prazo_dias=1,
            ),
            tarifa_exemplo(
                id_rota="R00901", cidade_destino="VITORIA", uf_destino="ES", prazo_dias=3
            ),
        ]

    def test_rota_ativa_cota(self):
        self.assertEqual(self.tabela.buscar("Sao Paulo/SP", "Campinas/SP").id_rota, "R00001")

    def test_rota_inativa_nao_cota_mas_esta_cadastrada(self):
        self.assertIsNone(self.tabela.buscar("Sao Paulo/SP", "Curitiba/PR"))
        self.assertTrue(self.tabela.trecho_cadastrado("Sao Paulo/SP", "Curitiba/PR"))

    def test_rota_inexistente_nao_esta_cadastrada(self):
        self.assertIsNone(self.tabela.buscar("Sao Paulo/SP", "Xique-Xique/BA"))
        self.assertFalse(self.tabela.trecho_cadastrado("Sao Paulo/SP", "Xique-Xique/BA"))

    def test_destino_sem_uf_casa(self):
        self.assertEqual(self.tabela.buscar("Sao Paulo", "Campinas").id_rota, "R00001")

    def test_modal_padrao_e_rodoviario(self):
        self.assertEqual(self.tabela.buscar("Sao Paulo/SP", "Vitoria/ES").modal, "RODOVIARIO")

    def test_modal_aereo_quando_pedido(self):
        t = self.tabela.buscar("Sao Paulo/SP", "Vitoria/ES", "AEREO")
        self.assertEqual(t.id_rota, "R00900")
        self.assertEqual(t.prazo_dias, 1)


class TestTextoDoEmail(unittest.TestCase):
    """O email vai ao cliente: numeros no padrao BR e sem linha sobrando."""

    def _cotacao(self, **over):
        from cotador.core import mensagens

        pedido = PedidoCotacao(
            e_cotacao=True,
            confianca=1,
            origem="Sao Paulo/SP",
            destino="Campinas/SP",
            qtd_volumes=over.pop("qtd_volumes", 10),
            valor_nf=over.pop("valor_nf", 8000.0),
            peso_kg=over.pop("peso_kg", 300.0),
            observacoes=over.pop("observacoes", None),
        )
        c = calcular(pedido, tarifa_exemplo(**over))
        return mensagens.enviar_cotacao(pedido, c, "Roberto")

    def test_percentual_com_virgula(self):
        texto = self._cotacao()
        self.assertIn("0,55% sobre a NF", texto)
        self.assertNotIn("0.55%", texto)

    def test_valores_no_padrao_br(self):
        texto = self._cotacao()
        self.assertIn("R$ 8.000,00", texto)
        self.assertIn("R$ 252,50", texto)

    def test_sem_linha_em_branco_sobrando(self):
        texto = self._cotacao()
        nl = chr(10)
        self.assertNotIn(nl + '  ' + nl, texto)
        self.assertNotIn(nl + ' ' + nl, texto)

    def test_dia_no_singular(self):
        self.assertIn("1 dia util apos", self._cotacao(prazo_dias=1))
        self.assertIn("3 dias uteis apos", self._cotacao(prazo_dias=3))

    def test_volume_no_singular(self):
        self.assertIn("(1 volume x", self._cotacao(qtd_volumes=1))
        self.assertIn("(10 volumes x", self._cotacao(qtd_volumes=10))

    def test_frete_minimo_aparece_so_quando_acionado(self):
        self.assertIn("Frete minimo", self._cotacao(qtd_volumes=1, valor_nf=900.0))
        self.assertNotIn("Frete minimo", self._cotacao(qtd_volumes=10))

    def test_taxa_dificil_aparece_so_quando_ha(self):
        self.assertIn("Taxa de entrega dificil", self._cotacao(taxa_entrega_dificil=35.0))
        self.assertNotIn("Taxa de entrega dificil", self._cotacao())

    def test_solicitar_dados_lista_o_que_falta(self):
        from cotador.core import mensagens

        p = PedidoCotacao(e_cotacao=True, confianca=0.55, origem="SP/SP", destino="RJ/RJ")
        texto = mensagens.solicitar_dados(p, "Ana")
        self.assertIn("quantidade de volumes", texto)
        self.assertIn("valor da mercadoria", texto)
        self.assertIn("peso total", texto)
        self.assertIn("origem: SP/SP", texto)


class TestPrimeiroNome(unittest.TestCase):
    def _email(self, nome):
        from cotador.core.modelos import Email

        return Email(
            id="1",
            thread_id="1",
            remetente="x@y.com",
            nome_remetente=nome,
            assunto="",
            corpo="",
            message_id_header=None,
            references_header=None,
        )

    def test_usa_apenas_o_primeiro_nome(self):
        self.assertEqual(self._email("Luzandro Candido Tietbohl").primeiro_nome, "Luzandro")

    def test_caixa_alta_vira_capitalizado(self):
        self.assertEqual(self._email("SAC NOVASCHOOL").primeiro_nome, "Sac")

    def test_nome_unico(self):
        self.assertEqual(self._email("webrsilva").primeiro_nome, "webrsilva")

    def test_vazio_cai_para_cliente(self):
        self.assertEqual(self._email("").primeiro_nome, "cliente")

    def test_pontuacao_removida(self):
        self.assertEqual(self._email("Ana, Souza").primeiro_nome, "Ana")


class TestMesclagemDeThread(unittest.TestCase):
    """O cliente responde so o campo que faltava; a thread nao pode esquecer
    o que ele ja informou, senao o agente pede os mesmos dados para sempre."""

    def setUp(self):
        self.primeiro = PedidoCotacao(
            e_cotacao=True,
            confianca=0.90,
            origem="Sao Paulo/SP",
            destino="Campinas/SP",
            qtd_volumes=1,
            peso_kg=10.0,
        )

    def test_resposta_curta_completa_o_pedido(self):
        resposta = PedidoCotacao(e_cotacao=True, confianca=0.50, valor_nf=200.0)
        final = resposta.mesclar(self.primeiro)
        self.assertEqual(final.origem, "Sao Paulo/SP")
        self.assertEqual(final.destino, "Campinas/SP")
        self.assertEqual(final.qtd_volumes, 1)
        self.assertAlmostEqual(final.peso_kg, 10.0)
        self.assertAlmostEqual(final.valor_nf, 200.0)
        self.assertEqual(final.campos_faltantes(), [])

    def test_confianca_e_a_maior_das_duas(self):
        resposta = PedidoCotacao(e_cotacao=True, confianca=0.50, valor_nf=200.0)
        self.assertAlmostEqual(resposta.mesclar(self.primeiro).confianca, 0.90)

    def test_cliente_corrigindo_dado_vence_o_antigo(self):
        correcao = PedidoCotacao(
            e_cotacao=True, confianca=0.8, destino="Santos/SP", valor_nf=200.0
        )
        final = correcao.mesclar(self.primeiro)
        self.assertEqual(final.destino, "Santos/SP")
        self.assertEqual(final.origem, "Sao Paulo/SP")

    def test_ida_e_volta_pelo_banco(self):
        import dataclasses

        salvo = dataclasses.asdict(self.primeiro)
        voltou = PedidoCotacao.de_dict(salvo)
        self.assertEqual(voltou.origem, self.primeiro.origem)
        self.assertEqual(voltou.qtd_volumes, self.primeiro.qtd_volumes)
        self.assertAlmostEqual(voltou.peso_kg, self.primeiro.peso_kg)

    def test_volumes_com_dimensoes_sobrevivem_ao_banco(self):
        import dataclasses

        p = PedidoCotacao(
            e_cotacao=True, confianca=1, volumes=[Volume(2, 40.0, 30.0, 20.0)]
        )
        voltou = PedidoCotacao.de_dict(dataclasses.asdict(p))
        self.assertEqual(voltou.volumes_efetivos, 2)
        self.assertAlmostEqual(voltou.volumes[0].m3_total, 0.048)


class TestEnvioSMTP(unittest.TestCase):
    """Cabecalhos de thread e validacao de config. Nao abre conexao real."""

    def _email(self):
        from cotador.core.modelos import Email

        return Email(
            id="1",
            thread_id="t1",
            remetente="cliente@exemplo.com",
            nome_remetente="Ana Souza",
            assunto="Cotacao de frete",
            corpo="",
            message_id_header="<abc@mail.gmail.com>",
            references_header="<antigo@mail.gmail.com>",
        )

    def _enviador(self, **over):
        from cotador.integracoes.email_smtp import EnviadorSMTP

        base = dict(
            host="smtp.gmail.com",
            porta=465,
            usuario="sac@exemplo.com",
            senha="senhadeapp16car",
            remetente_exibido="Nova School <sac@exemplo.com>",
        )
        base.update(over)
        return EnviadorSMTP(**base)

    def test_config_incompleta_avisa_o_que_falta(self):
        from cotador.integracoes.email_smtp import ConfiguracaoSMTPAusente

        with self.assertRaises(ConfiguracaoSMTPAusente) as ctx:
            self._enviador(senha="")
        self.assertIn("SMTP_SENHA_APP", str(ctx.exception))

    def test_responde_na_mesma_thread(self):
        msg = self._enviador().montar(self._email(), "corpo")
        self.assertEqual(msg["In-Reply-To"], "<abc@mail.gmail.com>")
        self.assertIn("<antigo@mail.gmail.com>", msg["References"])
        self.assertIn("<abc@mail.gmail.com>", msg["References"])

    def test_prefixo_re_sem_duplicar(self):
        env, email = self._enviador(), self._email()
        self.assertEqual(env.montar(email, "x")["Subject"], "Re: Cotacao de frete")
        email.assunto = "Re: Cotacao de frete"
        self.assertEqual(env.montar(email, "x")["Subject"], "Re: Cotacao de frete")

    def test_assunto_vazio_tem_padrao(self):
        email = self._email()
        email.assunto = ""
        self.assertEqual(self._enviador().montar(email, "x")["Subject"], "Re: Cotacao de frete")

    def test_destinatario_e_remetente(self):
        msg = self._enviador().montar(self._email(), "corpo do email")
        self.assertEqual(msg["To"], "cliente@exemplo.com")
        self.assertEqual(msg["From"], "Nova School <sac@exemplo.com>")
        self.assertIn("corpo do email", msg.get_content())

    def test_sem_message_id_nao_quebra(self):
        email = self._email()
        email.message_id_header = None
        msg = self._enviador().montar(email, "x")
        self.assertIsNone(msg["In-Reply-To"])

    def test_modo_enviar_sem_smtp_estoura_claro(self):
        from unittest import mock

        from cotador.agente import Agente

        ag = object.__new__(Agente)
        ag.cfg = mock.Mock(modo_resposta="enviar")
        ag.caixa = mock.Mock()
        ag.enviador = None
        with self.assertRaises(RuntimeError) as ctx:
            ag._responder(self._email(), "texto")
        self.assertIn("SMTP_SENHA_APP", str(ctx.exception))
        ag.caixa.criar_rascunho.assert_not_called()

    def test_modo_rascunho_nao_usa_smtp(self):
        from unittest import mock

        from cotador.agente import Agente

        ag = object.__new__(Agente)
        ag.cfg = mock.Mock(modo_resposta="rascunho")
        ag.caixa = mock.Mock()
        ag.enviador = mock.Mock()
        ag._responder(self._email(), "texto")
        ag.caixa.criar_rascunho.assert_called_once()
        ag.enviador.responder.assert_not_called()

    def test_modo_enviar_usa_smtp_e_nao_a_api(self):
        from unittest import mock

        from cotador.agente import Agente

        ag = object.__new__(Agente)
        ag.cfg = mock.Mock(modo_resposta="enviar")
        ag.caixa = mock.Mock()
        ag.enviador = mock.Mock()
        ag._responder(self._email(), "texto")
        ag.enviador.responder.assert_called_once()
        ag.caixa.criar_rascunho.assert_not_called()


class TestSemOAuth(unittest.TestCase):
    """O OAuth de usuario foi removido: leitura por IMAP, planilha por conta
    de servico. Nada de token.json nem expiracao de 7 dias."""

    def test_modulos_oauth_nao_existem_mais(self):
        import importlib

        for modulo in ("cotador.integracoes.google_auth", "cotador.integracoes.gmail"):
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(modulo)

    def test_planilha_usa_escopo_somente_leitura(self):
        from cotador.integracoes.google_sa import ESCOPOS

        self.assertEqual(ESCOPOS, ["https://www.googleapis.com/auth/spreadsheets.readonly"])

    def test_config_nao_tem_mais_token(self):
        from cotador.config import Config

        self.assertFalse(hasattr(Config, "token_json"))
        self.assertTrue(hasattr(Config, "service_account_json"))


class TestCabecalhoDobrado(unittest.TestCase):
    """Regressao: References longo chega dobrado em varias linhas (RFC 5322) e
    o email.message recusa gravar valor com quebra de linha."""

    def _email(self, references):
        from cotador.core.modelos import Email

        return Email(
            id="1",
            thread_id="t1",
            remetente="cliente@exemplo.com",
            nome_remetente="Ana",
            assunto="Cotacao",
            corpo="",
            message_id_header="<novo@mail.gmail.com>",
            references_header=references,
        )

    def _enviador(self):
        from cotador.integracoes.email_smtp import EnviadorSMTP

        return EnviadorSMTP("smtp.gmail.com", 465, "a@b.com", "senha", "A <a@b.com>")

    def test_desdobrar_junta_linhas(self):
        from cotador.integracoes import mime

        dobrado = "<a@x.com>" + chr(13) + chr(10) + " <b@x.com>" + chr(13) + chr(10) + chr(9) + "<c@x.com>"
        self.assertEqual(mime.desdobrar(dobrado), "<a@x.com> <b@x.com> <c@x.com>")

    def test_desdobrar_preserva_none(self):
        from cotador.integracoes import mime

        self.assertIsNone(mime.desdobrar(None))

    def test_montar_com_references_dobrado_nao_estoura(self):
        dobrado = "<a@x.com>" + chr(13) + chr(10) + " <b@x.com>"
        msg = self._enviador().montar(self._email(dobrado), "corpo")
        self.assertNotIn(chr(10), msg["References"])
        self.assertIn("<a@x.com>", msg["References"])
        self.assertIn("<b@x.com>", msg["References"])
        self.assertIn("<novo@mail.gmail.com>", msg["References"])

    def test_message_id_dobrado_tambem(self):
        email = self._email(None)
        email.message_id_header = " <novo@mail.gmail.com>" + chr(13) + chr(10)
        msg = self._enviador().montar(email, "corpo")
        self.assertEqual(msg["In-Reply-To"], "<novo@mail.gmail.com>")

    def test_serializa_sem_erro(self):
        dobrado = "<a@x.com>" + chr(13) + chr(10) + " <b@x.com>"
        self._enviador().montar(self._email(dobrado), "corpo").as_bytes()


class TestCicloResiliente(unittest.TestCase):
    """Um email problematico nao pode abortar o processamento dos outros."""

    def _agente(self, efeitos):
        from unittest import mock

        from cotador.agente import Agente

        ag = object.__new__(Agente)
        ag.cfg = mock.Mock(gmail_query="is:unread")
        ag.tarifas = mock.Mock()
        ag.caixa = mock.Mock()
        ag.caixa.buscar.return_value = ["1", "2", "3"]
        ag.banco = mock.Mock()
        ag._processar = mock.Mock(side_effect=efeitos)
        return ag

    def test_falha_no_meio_nao_derruba_o_resto(self):
        ag = self._agente(["cotado", ValueError("cabecalho ruim"), "cotado"])
        resumo = ag._ciclo()
        self.assertEqual(resumo, {"cotado": 2, "erro": 1})
        self.assertEqual(ag._processar.call_count, 3)

    def test_credencial_invalida_interrompe_tudo(self):
        from cotador.integracoes.caixa_imap import CredencialInvalida

        ag = self._agente([CredencialInvalida("senha ruim"), "cotado"])
        with self.assertRaises(CredencialInvalida):
            ag._ciclo()
        self.assertEqual(ag._processar.call_count, 1)


if __name__ == "__main__":
    unittest.main()
