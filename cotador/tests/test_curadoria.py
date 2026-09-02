"""Curadoria da tabela: limites duros, quarentena e versionamento.

Sem rede e sem credenciais. A TabelaTarifas e instanciada por
`__new__` para nao tocar na Sheets API: o que se testa aqui e a decisao
sobre os dados, nao a leitura deles.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from cotador.core import curadoria
from cotador.core.modelos import Tarifa
from cotador.integracoes.banco import Banco
from cotador.integracoes.planilha import TabelaTarifas


def tarifa(**over) -> Tarifa:
    """Rota R00001 da planilha: Sao Paulo -> Campinas, valores da aba EXEMPLO_CALCULO."""
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


def tabela_com(tarifas: list[Tarifa], bloqueia: bool = True) -> TabelaTarifas:
    t = TabelaTarifas.__new__(TabelaTarifas)
    t._aba = "TABELA_ROTAS"
    t._auditoria_bloqueia = bloqueia
    t._tarifas = tarifas
    t._linhas_brutas = []
    t._linha_da_tarifa = list(range(2, 2 + len(tarifas)))
    t._achados = []
    t._quarentena = set()
    t._auditar()
    return t


def campos(achados) -> set[str]:
    return {a.campo for a in achados}


class TestTarifaSaudavelNaoGeraAchado(unittest.TestCase):
    def test_linha_boa_passa_limpa(self):
        self.assertEqual(curadoria.auditar_linha(tarifa()), [])

    def test_tabela_boa_passa_limpa(self):
        self.assertEqual(curadoria.auditar_tabela([tarifa()]), [])


class TestLimitesDuros(unittest.TestCase):
    """Camada 1: os erros de digitacao que multiplicam o frete."""

    def test_gris_com_casa_decimal_a_mais_bloqueia(self):
        # 0,30 digitado como 30: a parcela sobre a NF fica 100x maior.
        achados = curadoria.auditar_linha(tarifa(gris_percentual=30.0))
        self.assertTrue(any(a.bloqueia for a in achados))
        self.assertIn("gris_percentual", campos(achados))

    def test_valor_por_volume_com_zero_a_mais_bloqueia(self):
        achados = curadoria.auditar_linha(tarifa(valor_por_volume=1665.0))
        self.assertTrue(any(a.bloqueia and a.campo == "valor_por_volume" for a in achados))

    def test_valor_por_volume_zerado_bloqueia(self):
        achados = curadoria.auditar_linha(tarifa(valor_por_volume=0.0))
        self.assertTrue(any(a.bloqueia and a.campo == "valor_por_volume" for a in achados))

    def test_soma_de_percentuais_acima_do_limite_bloqueia(self):
        # Cada um dentro da faixa, a soma fora dela.
        achados = curadoria.auditar_linha(
            tarifa(gris_percentual=1.9, advalorem_percentual=1.9)
        )
        self.assertTrue(
            any(a.bloqueia and "+" in a.campo for a in achados),
            "a soma gris+advalorem tem de ser checada em separado",
        )

    def test_frete_minimo_absurdo_bloqueia(self):
        achados = curadoria.auditar_linha(tarifa(frete_minimo=5500.0))
        self.assertTrue(any(a.bloqueia and a.campo == "frete_minimo" for a in achados))

    def test_vigencia_invertida_bloqueia(self):
        achados = curadoria.auditar_linha(
            tarifa(vigencia_inicio=date(2026, 6, 1), vigencia_fim=date(2026, 1, 1))
        )
        self.assertTrue(any(a.bloqueia and a.campo == "vigencia_fim" for a in achados))

    def test_taxa_entrega_dificil_absurda_bloqueia(self):
        achados = curadoria.auditar_linha(tarifa(taxa_entrega_dificil=99999.0))
        self.assertTrue(
            any(a.bloqueia and a.campo == "taxa_entrega_dificil" for a in achados)
        )


class TestAlertas(unittest.TestCase):
    """Valores incomuns que podem ser legitimos: relata e segue cotando."""

    def test_pedagio_maior_que_o_frete_e_alerta_nao_bloqueio(self):
        achados = curadoria.auditar_linha(tarifa(pedagio_por_volume=50.0))
        self.assertTrue(achados)
        self.assertFalse(any(a.bloqueia for a in achados))
        self.assertIn("pedagio_por_volume", campos(achados))

    def test_frete_minimo_que_nunca_pega_e_alerta(self):
        achados = curadoria.auditar_linha(tarifa(frete_minimo=20.0))
        self.assertIn("frete_minimo", campos(achados))
        self.assertFalse(any(a.bloqueia for a in achados))

    def test_prazo_absurdo_e_alerta(self):
        achados = curadoria.auditar_linha(tarifa(prazo_dias=90))
        self.assertIn("prazo_dias", campos(achados))
        self.assertFalse(any(a.bloqueia for a in achados))

    def test_uf_com_mais_de_duas_letras_e_alerta(self):
        achados = curadoria.auditar_linha(tarifa(uf_destino="SAO PAULO"))
        self.assertIn("uf_destino", campos(achados))

    def test_modal_desconhecido_e_alerta(self):
        achados = curadoria.auditar_linha(tarifa(modal="MARITIMO"))
        self.assertIn("modal", campos(achados))

    def test_campo_opcional_em_branco_nao_e_erro(self):
        limpa = tarifa(peso_maximo_volume_kg=None, prazo_dias=None, distancia_km=None)
        self.assertEqual(curadoria.auditar_linha(limpa), [])


class TestRevisaoHumana(unittest.TestCase):
    """REVISADO_POR + REVISADO_EM silenciam alerta. Nunca bloqueio."""

    def test_revisao_silencia_alerta(self):
        suspeita = tarifa(pedagio_por_volume=50.0)
        self.assertTrue(curadoria.auditar_linha(suspeita))

        assinada = tarifa(
            pedagio_por_volume=50.0,
            revisado_por="comercial@ouroeprata.com",
            revisado_em=date(2026, 9, 1),
        )
        self.assertEqual(curadoria.auditar_linha(assinada), [])

    def test_revisao_nao_silencia_bloqueio(self):
        # Um GRIS de 30% nao e uma tarifa a autorizar; e um 0,30 digitado
        # errado. Aprovar nao corrige.
        assinada = tarifa(
            gris_percentual=30.0,
            revisado_por="comercial@ouroeprata.com",
            revisado_em=date(2026, 9, 1),
        )
        achados = curadoria.auditar_linha(assinada)
        self.assertTrue(achados)
        self.assertTrue(all(a.bloqueia for a in achados))

    def test_revisado_sem_data_nao_conta(self):
        meia = tarifa(pedagio_por_volume=50.0, revisado_por="alguem")
        self.assertTrue(curadoria.auditar_linha(meia))


class TestAmbiguidadeEntreLinhas(unittest.TestCase):
    def test_id_rota_repetido_e_alerta(self):
        achados = curadoria.auditar_tabela(
            [tarifa(), tarifa(cidade_destino="SANTOS")]
        )
        self.assertIn("id_rota", campos(achados))

    def test_duas_tarifas_vigentes_no_mesmo_trecho_bloqueiam_as_duas(self):
        # `buscar` devolveria a primeira da lista: o preco do cliente
        # dependeria da ordem das linhas na planilha.
        achados = curadoria.auditar_tabela(
            [tarifa(id_rota="R1"), tarifa(id_rota="R2", valor_por_volume=25.0)]
        )
        travados = curadoria.bloqueios(achados)
        self.assertEqual(curadoria.indices_bloqueados(achados), {0, 1})
        self.assertTrue(any("mais de uma tarifa" in a.mensagem for a in travados))

    def test_vigencias_que_nao_se_cruzam_nao_bloqueiam(self):
        antiga = tarifa(
            id_rota="R1",
            vigencia_inicio=date(2026, 1, 1),
            vigencia_fim=date(2026, 6, 30),
        )
        nova = tarifa(
            id_rota="R2",
            vigencia_inicio=date(2026, 7, 1),
            vigencia_fim=date(2026, 12, 31),
        )
        achados = curadoria.auditar_tabela([antiga, nova])
        self.assertEqual(curadoria.indices_bloqueados(achados), set())

    def test_linha_inativa_nao_colide_com_a_vigente(self):
        achados = curadoria.auditar_tabela(
            [tarifa(id_rota="R1"), tarifa(id_rota="R2", status="INATIVO")]
        )
        self.assertEqual(curadoria.indices_bloqueados(achados), set())


class TestQuarentena(unittest.TestCase):
    """Camada 5: a linha bloqueada nao cota, mas o trecho continua nosso."""

    def setUp(self):
        self.tabela = tabela_com([tarifa(gris_percentual=30.0)])

    def test_rota_bloqueada_nao_cota(self):
        self.assertIsNone(self.tabela.buscar("Sao Paulo/SP", "Campinas/SP"))
        self.assertEqual(self.tabela.rotas("Sao Paulo/SP", "Campinas/SP"), [])

    def test_trecho_continua_cadastrado(self):
        # E o que impede o cliente de ouvir "nao atendemos" por erro nosso.
        self.assertTrue(self.tabela.trecho_cadastrado("Sao Paulo/SP", "Campinas/SP"))

    def test_motivo_chega_em_portugues(self):
        motivo = self.tabela.motivo_quarentena("Sao Paulo/SP", "Campinas/SP")
        self.assertIsNotNone(motivo)
        self.assertIn("gris_percentual", motivo)
        self.assertIn("R00001", motivo)

    def test_trecho_sem_bloqueio_nao_tem_motivo(self):
        limpa = tabela_com([tarifa()])
        self.assertIsNone(limpa.motivo_quarentena("Sao Paulo/SP", "Campinas/SP"))

    def test_linha_boa_ao_lado_de_uma_ruim_continua_cotando(self):
        tabela = tabela_com(
            [
                tarifa(gris_percentual=30.0, cidade_destino="SANTOS", id_rota="R2"),
                tarifa(),
            ]
        )
        self.assertIsNotNone(tabela.buscar("Sao Paulo/SP", "Campinas/SP"))
        self.assertIsNone(tabela.buscar("Sao Paulo/SP", "Santos"))

    def test_modo_relatorio_nao_tira_nada_do_ar(self):
        tabela = tabela_com([tarifa(gris_percentual=30.0)], bloqueia=False)
        self.assertTrue(tabela.achados, "o achado continua sendo registrado")
        self.assertEqual(tabela.quarentena, [])
        self.assertIsNotNone(tabela.buscar("Sao Paulo/SP", "Campinas/SP"))

    def test_quarentena_lista_a_tarifa_afetada(self):
        self.assertEqual([t.id_rota for t in self.tabela.quarentena], ["R00001"])

    def test_achado_aponta_a_linha_da_planilha(self):
        achado = curadoria.bloqueios(self.tabela.achados)[0]
        self.assertEqual(achado.linha, 2)
        self.assertIn("linha 2", str(achado))


class TestComparacaoDeVersoes(unittest.TestCase):
    """Camada 0: o erro plausivel, que nenhum limite pega."""

    def test_mudanca_de_12_por_cento_aparece(self):
        antes = curadoria.impressao([tarifa(valor_por_volume=16.65)])
        depois = curadoria.impressao([tarifa(valor_por_volume=18.65)])
        mudancas = curadoria.comparar(antes, depois)
        self.assertEqual(len(mudancas), 1)
        self.assertEqual(mudancas[0].tipo, "alterada")
        self.assertEqual(mudancas[0].campo, "valor_por_volume")
        self.assertEqual((mudancas[0].de, mudancas[0].para), (16.65, 18.65))

    def test_erro_plausivel_nao_viola_nenhum_limite(self):
        # A prova de que a camada 1 nao substitui a camada 0.
        self.assertEqual(curadoria.auditar_linha(tarifa(valor_por_volume=18.65)), [])

    def test_tabela_igual_nao_gera_mudanca(self):
        atual = curadoria.impressao([tarifa()])
        self.assertEqual(curadoria.comparar(atual, atual), [])

    def test_rota_nova_e_rota_removida(self):
        antes = curadoria.impressao([tarifa(id_rota="R1")])
        depois = curadoria.impressao([tarifa(id_rota="R2")])
        tipos = {m.tipo for m in curadoria.comparar(antes, depois)}
        self.assertEqual(tipos, {"nova", "removida"})

    def test_campo_insensivel_nao_gera_mudanca(self):
        antes = curadoria.impressao([tarifa(regiao_destino="SUDESTE")])
        depois = curadoria.impressao([tarifa(regiao_destino="INTERIOR SP")])
        self.assertEqual(curadoria.comparar(antes, depois), [])

    def test_status_e_vigencia_entram_na_comparacao(self):
        antes = curadoria.impressao([tarifa(status="ATIVO")])
        depois = curadoria.impressao([tarifa(status="INATIVO")])
        mudancas = curadoria.comparar(antes, depois)
        self.assertEqual([m.campo for m in mudancas], ["status"])

    def test_hash_muda_com_o_conteudo(self):
        a = curadoria.hash_conteudo([["id_rota", "valor"], ["R1", "16,65"]])
        b = curadoria.hash_conteudo([["id_rota", "valor"], ["R1", "18,65"]])
        self.assertNotEqual(a, b)
        self.assertEqual(a, curadoria.hash_conteudo([["id_rota", "valor"], ["R1", "16,65"]]))


class TestVersionamentoNoBanco(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.banco = Banco(Path(self.dir.name) / "teste.sqlite3")

    def tearDown(self):
        self.dir.cleanup()

    def _registrar(self, hash_conteudo: str, impressao: list[dict]) -> bool:
        return self.banco.registrar_versao_tabela(
            hash_conteudo=hash_conteudo,
            aba="TABELA_ROTAS",
            linhas=len(impressao) + 1,
            tarifas=len(impressao),
            bloqueios=0,
            impressao=impressao,
        )

    def test_primeira_carga_e_nova_a_segunda_igual_nao(self):
        imp = curadoria.impressao([tarifa()])
        self.assertTrue(self._registrar("aaa", imp))
        self.assertFalse(self._registrar("aaa", imp))

    def test_conteudo_diferente_conta_como_nova_versao(self):
        self.assertTrue(self._registrar("aaa", curadoria.impressao([tarifa()])))
        self.assertTrue(
            self._registrar("bbb", curadoria.impressao([tarifa(valor_por_volume=18.65)]))
        )

    def test_versao_anterior_devolve_a_de_antes(self):
        antiga = curadoria.impressao([tarifa(valor_por_volume=16.65)])
        nova = curadoria.impressao([tarifa(valor_por_volume=166.50)])
        self._registrar("aaa", antiga)
        self._registrar("bbb", nova)

        anterior = self.banco.versao_anterior("TABELA_ROTAS", "bbb")
        self.assertIsNotNone(anterior)
        self.assertEqual(anterior["hash"], "aaa")

        mudancas = curadoria.comparar(anterior["impressao"], nova)
        self.assertEqual([m.campo for m in mudancas], ["valor_por_volume"])

    def test_sem_historico_nao_ha_anterior(self):
        self._registrar("aaa", curadoria.impressao([tarifa()]))
        self.assertIsNone(self.banco.versao_anterior("TABELA_ROTAS", "aaa"))

    def test_tarifa_usada_fica_gravada_na_cotacao(self):
        # Amanha o comercial corrige a planilha; esta cotacao tem de continuar
        # reconstruivel a partir do banco.
        import dataclasses
        import json
        import sqlite3

        self.banco.registrar(
            id_email="msg-1",
            thread_id="thr-1",
            remetente="cliente@exemplo.com",
            assunto="cotacao",
            desfecho="cotado",
            valor_frete=252.50,
            tarifa=dataclasses.asdict(tarifa()),
        )
        con = sqlite3.connect(self.banco._caminho)
        try:
            bruto = con.execute(
                "SELECT tarifa_json FROM processados WHERE id_email = 'msg-1'"
            ).fetchone()[0]
        finally:
            con.close()
        gravada = json.loads(bruto)
        self.assertEqual(gravada["id_rota"], "R00001")
        self.assertEqual(gravada["valor_por_volume"], 16.65)
        self.assertEqual(gravada["gris_percentual"], 0.30)


if __name__ == "__main__":
    unittest.main()
