"""Ponto de entrada do agente de cotacao.

Uso:
    python main.py --validar-planilha        # so testa a leitura das tarifas
    python main.py --auditar-planilha        # curadoria: limites, quarentena e mudancas
    python main.py --testar-texto "..."      # so testa a extracao (nao toca no email)
    python main.py --cotar "Sao Paulo/SP" "Campinas/SP" 10 8000
    python main.py --testar-imap             # valida a leitura da caixa
    python main.py --testar-smtp             # valida a senha de app, sem enviar
    python main.py --resumo-revisar          # fila de revisao humana: quantos e ha quanto tempo
    python main.py --reprocessar-erros       # devolve para a fila os que falharam
    python main.py --once                    # um ciclo na caixa de entrada
    python main.py --loop                    # ciclos continuos
    python main.py --painel                  # interface web local de gestao

Codigos de saida: 0 ok | 1 falha de dados | 2 credencial recusada
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cotador.agente import Agente, classificar_erro
from cotador.config import Config
from cotador.core import curadoria, precificacao
from cotador.core.extracao import Extrator
from cotador.core.modelos import PedidoCotacao
from cotador.integracoes import google_sa
from cotador.integracoes.banco import Banco
from cotador.integracoes.caixa_imap import CaixaIMAP, CredencialInvalida
from cotador.integracoes.email_smtp import EnviadorSMTP
from cotador.integracoes.planilha import TabelaTarifas

log = logging.getLogger("cotador")

ARQUIVO_ALERTA = "ALERTA_CREDENCIAL.txt"


def configurar_log(verboso: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%d/%m %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


# ---------------- alerta ----------------
def caminho_alerta(cfg: Config) -> Path:
    return cfg.service_account_json.parent / ARQUIVO_ALERTA


def registrar_alerta(cfg: Config, mensagem: str) -> Path:
    """Grava o alerta em arquivo: o Agendador de Tarefas e qualquer monitor
    conseguem ver que o agente parou sem depender de ler o log."""
    caminho = caminho_alerta(cfg)
    texto = [
        f"[{datetime.now():%d/%m/%Y %H:%M:%S}] AGENTE PARADO - CREDENCIAL RECUSADA",
        "",
        mensagem,
        "",
        "Para voltar a rodar, corrija o .env e valide com:",
        "    python main.py --testar-imap",
        "    python main.py --testar-smtp",
        "",
        "A senha de app do Gmail e gerada em:",
        "    https://myaccount.google.com/apppasswords",
        "Ela deixa de valer se a verificacao em duas etapas for desligada ou se",
        "alguem revogar o acesso nas configuracoes da conta.",
        "",
    ]
    caminho.write_text("\n".join(texto), encoding="utf-8")
    return caminho


def limpar_alerta(cfg: Config) -> None:
    caminho = caminho_alerta(cfg)
    if caminho.exists():
        caminho.unlink()


def gritar(cfg: Config, exc: Exception) -> int:
    caminho = registrar_alerta(cfg, str(exc))
    barra = "!" * 72
    log.error(barra)
    log.error("AGENTE PARADO: credencial recusada")
    log.error("%s", exc)
    log.error("Alerta gravado em %s", caminho)
    log.error(barra)
    return 2


# ---------------- fabricas ----------------
def montar_caixa(cfg: Config) -> CaixaIMAP:
    return CaixaIMAP(
        host=cfg.imap_host,
        porta=cfg.imap_porta,
        usuario=cfg.smtp_usuario,
        senha=cfg.smtp_senha,
    )


def montar_enviador(cfg: Config) -> EnviadorSMTP:
    return EnviadorSMTP(
        host=cfg.smtp_host,
        porta=cfg.smtp_porta,
        usuario=cfg.smtp_usuario,
        senha=cfg.smtp_senha,
        remetente_exibido=cfg.remetente,
    )


def montar_precificador(cfg: Config):
    if cfg.precificador != "historico":
        return None
    from cotador.ml import PrecificadorHistorico
    return PrecificadorHistorico(cfg.modelo_artefatos, cfg.modelo_bloquear_outlier)


def _idade(iso: str) -> str:
    """Quanto tempo desde um carimbo ISO, em horas ou dias."""
    try:
        quando = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "data ilegivel"
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    horas = (datetime.now(timezone.utc) - quando).total_seconds() / 3600
    return f"{horas:.0f} h" if horas < 48 else f"{horas / 24:.0f} dias"


def resumo_revisar(cfg: Config) -> int:
    """Fila de revisao humana agrupada por motivo, do mais antigo ao mais novo.

    O painel (`--painel`) e a tela para trabalhar a fila item por item; este
    comando e a versao para agendador, porque sai com codigo 1 quando ha
    qualquer item. As consultas sao as mesmas do painel, para nao existirem
    duas respostas para a mesma pergunta.
    """
    banco = Banco(cfg.banco)
    itens = banco.por_label(cfg.LABEL_REVISAR)
    if not itens:
        print("Fila de revisao humana vazia.")
        return 0

    # por_label devolve do mais recente; aqui a pergunta e quem espera ha mais
    # tempo, entao invertemos.
    itens.sort(key=lambda item: item["criado_em"])
    print(f"Fila de revisao humana: {len(itens)} email(s) | label {cfg.LABEL_REVISAR}")

    contagem: dict[str, int] = {}
    for item in itens:
        rotulo = classificar_erro(item["erro"])
        contagem[rotulo] = contagem.get(rotulo, 0) + 1

    print()
    print("POR MOTIVO")
    for rotulo, quantos in sorted(contagem.items(), key=lambda par: -par[1]):
        print(f"  {quantos:3d}  {rotulo}")

    antigo = itens[0]
    print()
    print(f"mais antigo: {antigo['criado_em']} ({_idade(antigo['criado_em'])} de espera)")

    print()
    print("A FILA")
    for item in itens[:20]:
        trecho = f"{item['origem'] or '?'} -> {item['destino'] or '?'}"
        print(f"  {item['criado_em'][:16]}  {(item['remetente'] or '-'):30.30}  {trecho}")
        print(f"      {(item['erro'] or '')[:108]}")
    if len(itens) > 20:
        print(f"  ... e mais {len(itens) - 20}")

    print()
    print("Para trabalhar a fila item por item: python main.py --painel")
    print("Corrigida a causa, devolva tudo de uma vez: python main.py --reprocessar-erros")
    return 1


def auditar_planilha(cfg: Config, tabela: TabelaTarifas) -> int:
    """Curadoria da tabela: limites duros, quarentena e o que mudou desde ontem.

    Codigo de saida 1 quando ha bloqueio, para poder virar tarefa agendada:
    quem roda isto num agendador quer o alerta, nao a leitura do relatorio.
    """
    total = tabela.carregar()
    achados = tabela.achados
    travados = curadoria.bloqueios(achados)
    avisos = curadoria.alertas(achados)

    print(f"{total} tarifas carregadas de '{cfg.sheet_aba}'")
    print(f"versao da tabela: {tabela.hash_conteudo[:12]}")
    if not cfg.auditoria_bloqueia:
        print("AUDITORIA_BLOQUEIA=false: nada sai de circulacao, so relata")

    print()
    print(f"BLOQUEIO  {len(travados)}")
    for a in travados:
        print(f"  {a}")
    print()
    print(f"ALERTA    {len(avisos)}")
    for a in avisos:
        print(f"  {a}")

    banco = Banco(cfg.banco)
    banco.registrar_versao_tabela(
        hash_conteudo=tabela.hash_conteudo,
        aba=cfg.sheet_aba,
        linhas=len(tabela.linhas_brutas),
        tarifas=total,
        bloqueios=len(travados),
        impressao=tabela.impressao(),
    )
    anterior = banco.versao_anterior(cfg.sheet_aba, tabela.hash_conteudo)
    if anterior is None:
        print()
        print("Primeira versao registrada: nada com que comparar ainda.")
    else:
        mudancas = curadoria.comparar(anterior["impressao"], tabela.impressao())
        print()
        print(f"MUDANCAS desde {anterior['visto_ate']}  ({len(mudancas)})")
        for m in mudancas:
            print(f"  {m}")

    if travados:
        print()
        print(
            f"{len(tabela.quarentena)} rota(s) em quarentena: nao cotam, e a thread "
            "do cliente vai para revisao humana."
        )
    return 1 if travados else 0


def main() -> int:
    """Qualquer comando pode esbarrar na credencial recusada — inclusive no
    login IMAP, antes do primeiro ciclo. Por isso o alerta e tratado aqui em
    volta de tudo, nao so dentro do loop."""
    try:
        return _executar()
    except CredencialInvalida as exc:
        return gritar(Config.carregar(), exc)


def _executar() -> int:
    p = argparse.ArgumentParser(description="Agente de cotacao de frete por email")
    grupo = p.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--once", action="store_true", help="processa a caixa uma vez")
    grupo.add_argument("--loop", action="store_true", help="processa em intervalos")
    grupo.add_argument("--validar-planilha", action="store_true")
    grupo.add_argument(
        "--auditar-planilha",
        action="store_true",
        help="roda a curadoria da tabela: limites duros, quarentena e o que mudou",
    )
    grupo.add_argument("--testar-texto", metavar="TEXTO")
    grupo.add_argument(
        "--testar-imap",
        action="store_true",
        help="valida a leitura da caixa por IMAP sem processar nada",
    )
    grupo.add_argument(
        "--testar-smtp",
        action="store_true",
        help="valida a conexao e a senha de app do SMTP sem enviar email",
    )
    grupo.add_argument(
        "--resumo-revisar",
        action="store_true",
        help="mostra a fila de revisao humana: quantos, por motivo e ha quanto tempo",
    )
    grupo.add_argument(
        "--reprocessar-erros",
        action="store_true",
        help="limpa do banco os emails com desfecho erro para tentar de novo",
    )
    grupo.add_argument("--confirmar", nargs=2, metavar=("QUOTE_ID", "PRECO"), help="registra aceite de cotação histórica")
    grupo.add_argument("--rejeitar", metavar="QUOTE_ID", help="registra rejeição de cotação histórica")
    grupo.add_argument("--modelo-info", action="store_true", help="mostra metadados do modelo histórico")
    grupo.add_argument(
        "--cotar",
        nargs=4,
        metavar=("ORIGEM", "DESTINO", "QTD_VOLUMES", "VALOR_NF"),
        help="calcula um frete direto da planilha, sem email",
    )
    grupo.add_argument(
        "--painel",
        action="store_true",
        help="sobe a interface web local de gestao (http://localhost:8000)",
    )
    p.add_argument("--porta", type=int, default=8000, help="porta do --painel")
    p.add_argument("--peso", type=float, help="peso total em kg para --cotar histórico")
    p.add_argument("--custo", type=float, help="custo real opcional para --confirmar")
    p.add_argument("--notas", help="observação para aceite ou rejeição")
    p.add_argument("-v", "--verboso", action="store_true")
    args = p.parse_args()

    configurar_log(args.verboso)
    cfg = Config.carregar()

    if args.testar_texto:
        pedido = Extrator(
            cfg.anthropic_api_key, cfg.anthropic_model, cfg.anthropic_workspace_id
        ).analisar("Teste de extracao", args.testar_texto)
        print(pedido)
        print("faltando:", pedido.campos_faltantes(cfg.exigir_peso) or "nada")
        return 0

    if args.testar_imap:
        caixa = montar_caixa(cfg)
        with caixa:
            achados = caixa.buscar(cfg.gmail_query)
            print(f"IMAP OK: {cfg.smtp_usuario}@{cfg.imap_host}:{cfg.imap_porta}")
            print(f"pasta de rascunhos: {caixa.pasta_rascunhos()}")
            print(f"{len(achados)} email(s) batem com: {cfg.gmail_query}")
        limpar_alerta(cfg)
        return 0

    if args.testar_smtp:
        montar_enviador(cfg).testar_conexao()
        print(f"SMTP OK: {cfg.smtp_usuario}@{cfg.smtp_host}:{cfg.smtp_porta}")
        print(f"remetente exibido: {cfg.remetente}")
        limpar_alerta(cfg)
        return 0

    if args.resumo_revisar:
        return resumo_revisar(cfg)

    if args.reprocessar_erros:
        removidos = Banco(cfg.banco).limpar_erros()
        print(f"{removidos} email(s) com erro liberados para reprocessamento")
        # O email de erro fica nao-lido de proposito, entao a busca
        # `is:unread` o encontra de novo sem ninguem tocar no Gmail. Se alguem
        # tiver aberto a thread na mao, ai sim precisa marcar como nao-lida.
        print("Eles voltam no proximo ciclo. Se alguem abriu a thread no Gmail,")
        print("marque-a como nao lida para que a busca a encontre.")
        return 0

    if args.confirmar:
        quote_id, preco = args.confirmar
        resultado = Banco(cfg.banco).confirmar_cotacao(quote_id, float(preco), args.custo, args.notas)
        print(f"{resultado['quote_id']} confirmado por R$ {resultado['contracted_price']:.2f}")
        return 0

    if args.rejeitar:
        resultado = Banco(cfg.banco).rejeitar_cotacao(args.rejeitar, args.notas)
        print(f"{resultado['quote_id']} rejeitado")
        return 0

    if args.modelo_info:
        caminho = cfg.modelo_artefatos / "metadata.json"
        if not caminho.exists():
            print(f"Modelo não encontrado: {caminho}")
            return 1
        print(caminho.read_text(encoding="utf-8"))
        return 0

    cred = google_sa.credenciais(cfg.service_account_json)
    tabela = TabelaTarifas(
        cred, cfg.sheet_id, cfg.sheet_aba, auditoria_bloqueia=cfg.auditoria_bloqueia
    )
    if args.auditar_planilha:
        return auditar_planilha(cfg, tabela)

    if args.validar_planilha:
        total = tabela.carregar()
        print(f"{total} tarifas carregadas de '{cfg.sheet_aba}'")
        for t in tabela.tarifas[:3]:
            print(
                f"  {t.id_rota} {t.chave_origem} -> {t.chave_destino} "
                f"[{t.modal}] vol R$ {t.valor_por_volume:.2f} "
                f"+ pedagio R$ {t.pedagio_por_volume:.2f} | min R$ {t.frete_minimo:.2f} "
                f"| gris {t.gris_percentual:.2f}% + adv {t.advalorem_percentual:.2f}% "
                f"| prazo {t.prazo_dias}d"
            )
        return 0 if total else 1

    precificador_historico = montar_precificador(cfg)

    if args.cotar:
        origem, destino, qtd, valor_nf = args.cotar
        if precificador_historico is not None and (args.peso is None or args.peso <= 0):
            print("O modo histórico exige --peso com valor maior que zero")
            return 1
        tabela.carregar()
        tarifa = tabela.buscar(origem, destino)
        if tarifa is None:
            print(f"Rota nao atendida: {origem} -> {destino}")
            return 1
        pedido = PedidoCotacao(
            e_cotacao=True,
            confianca=1.0,
            origem=origem,
            destino=destino,
            qtd_volumes=int(qtd),
            valor_nf=float(valor_nf),
            peso_kg=args.peso,
        )
        c = (
            precificador_historico.cotar(pedido, tarifa)
            if precificador_historico is not None
            else precificacao.calcular(pedido, tarifa)
        )
        print(f"rota .............. {tarifa.id_rota} [{tarifa.modal}]")
        if c.fonte == "historico_olist":
            print(f"P25 ............... R$ {c.p25:.2f}")
            print(f"P50 (padrão) ...... R$ {c.p50:.2f}")
            print(f"P75 ............... R$ {c.p75:.2f}")
            print(f"distância ......... {c.distancia_km:.1f} km")
            print(f"modelo ............ {c.model_version}")
        else:
            print(f"frete volumes ..... R$ {c.frete_volumes:.2f}")
            print(f"frete aplicado .... R$ {c.frete_aplicado:.2f}")
            print(f"gris + advalorem .. R$ {c.gris_advalorem:.2f}")
            print(f"taxa dificil ...... R$ {c.taxa_entrega_dificil:.2f}")
        print(f"TOTAL ............. R$ {c.total:.2f}")
        print(f"prazo ............. {c.prazo_dias} dia(s) uteis")
        return 0

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
                precificador_historico=precificador_historico,
            )

        servico = ServicoAgente(fabrica_agente, cfg.intervalo_segundos)
        app = criar_app(cfg, banco, tabela, servico, lambda: montar_caixa(cfg), estado, precificador_historico)
        print(f"Painel em http://localhost:{args.porta} (loop desligado, modo rascunho)")
        app.run(host="127.0.0.1", port=args.porta, debug=False)
        servico.desligar()
        return 0

    agente = Agente(
        cfg=cfg,
        caixa=montar_caixa(cfg),
        tarifas=tabela,
        extrator=Extrator(
            cfg.anthropic_api_key, cfg.anthropic_model, cfg.anthropic_workspace_id
        ),
        banco=Banco(cfg.banco),
        # Em modo rascunho nao exigimos SMTP: nada sai pela rede.
        enviador=montar_enviador(cfg) if cfg.modo_resposta == "enviar" else None,
        precificador_historico=precificador_historico,
    )

    if args.once:
        try:
            print(agente.rodar_ciclo())
        except CredencialInvalida as exc:
            return gritar(cfg, exc)
        limpar_alerta(cfg)
        return 0

    log.info("Loop iniciado (intervalo de %ds). Ctrl+C para parar.", cfg.intervalo_segundos)
    while True:
        try:
            resumo = agente.rodar_ciclo()
            if resumo:
                log.info("Ciclo: %s", resumo)
            limpar_alerta(cfg)
        except CredencialInvalida as exc:
            # Insistir nao resolve: precisa de um humano corrigindo a credencial.
            # Melhor sair com codigo de erro do que girar em silencio.
            return gritar(cfg, exc)
        except KeyboardInterrupt:
            log.info("Encerrado pelo usuario")
            return 0
        except Exception:
            log.exception("Ciclo falhou; tentando novamente no proximo intervalo")
        time.sleep(cfg.intervalo_segundos)


if __name__ == "__main__":
    sys.exit(main())
