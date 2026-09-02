"""Fabrica do app Flask do painel. Toda dependencia entra por parametro,
para os testes injetarem fakes (mesmo padrao do Agente)."""
from __future__ import annotations

import logging
import re
import secrets

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from cotador.core import precificacao
from cotador.core.modelos import PedidoCotacao
from painel import consultas

log = logging.getLogger(__name__)

# '1.234.567' — pontos como separador de milhar, sem decimais.
_SO_MILHAR = re.compile(r"-?\d{1,3}(\.\d{3})+")


def _reais(valor) -> str:
    if valor is None:
        return "—"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _para_float_ptbr(texto: str) -> float:
    """Le numero digitado em teclado brasileiro sem perder ordem de grandeza.

    float('8.000') daria 8.0 — a nota de oito mil viraria oito reais e a
    cotacao sairia mil vezes menor. Regras: virgula manda (e o decimal, e os
    pontos sao milhar); so pontos, decide o formato '1.234' vs '8000.50'.
    """
    limpo = texto.strip().replace(" ", "")
    if "," in limpo:
        return float(limpo.replace(".", "").replace(",", "."))
    if _SO_MILHAR.fullmatch(limpo):
        return float(limpo.replace(".", ""))
    return float(limpo)


def criar_app(cfg, banco, tarifas, servico, fabrica_caixa, estado) -> Flask:
    app = Flask(__name__)
    # So para assinar o cookie de flash(); o painel e local e sem login.
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    # Token por processo: o painel nao tem login, entao sem isto qualquer
    # pagina aberta no navegador poderia postar /agente/acao num form oculto.
    token = secrets.token_hex(16)
    app.config["CSRF_TOKEN"] = token
    app.jinja_env.filters["reais"] = _reais

    @app.context_processor
    def _globais():
        return {"servico": servico, "modo": estado["modo"], "csrf_token": token}

    @app.before_request
    def _conferir_token():
        # Bytes dos dois lados: compare_digest recusa str nao-ASCII com
        # TypeError, e um token exotico deve dar 403 como qualquer outro.
        if request.method == "POST" and not secrets.compare_digest(
            request.form.get("_token", "").encode(), token.encode()
        ):
            abort(403)

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
        # So as linhas em revisao. Numa thread mista, devolver as demais
        # deixaria emails ja resolvidos nao lidos e sem idempotencia — o
        # proximo ciclo responderia a cliente de novo.
        ids = banco.ids_da_thread(thread_id, label=cfg.LABEL_REVISAR)
        if not ids:
            # Clique repetido (ou outra aba ja devolveu): nao abre IMAP a toa.
            flash("Nada a devolver: esta thread já saiu da revisão.")
            return redirect(url_for("revisao"))
        try:
            # Conexao IMAP propria da acao: abre, devolve e fecha (mesmo padrao
            # do ciclo do agente — nada de sessao ociosa pendurada).
            with fabrica_caixa() as caixa:
                devolvidos = [
                    id_email
                    for id_email in ids
                    if caixa.devolver_para_fila(id_email, [cfg.LABEL_REVISAR])
                ]
        except Exception as exc:
            # Apagar antes de falar com o Gmail perderia o email de vez: sem
            # registro e sem voltar para 'is:unread', ninguem mais o veria.
            log.exception("Falha ao devolver a thread %s", thread_id)
            flash(f"Nada foi devolvido: falha ao falar com o Gmail ({exc}).")
            return redirect(url_for("revisao"))

        # So sai do banco o que o IMAP confirmou; o resto segue na revisao.
        banco.apagar_emails(devolvidos)
        if len(devolvidos) == len(ids):
            flash(
                f"{len(devolvidos)} email(s) devolvidos à fila; "
                "o agente reprocessa no próximo ciclo."
            )
        else:
            flash(
                f"{len(devolvidos)} de {len(ids)} devolvidos; "
                "os demais permanecem na revisão."
            )
        return redirect(url_for("revisao"))

    # ---------------- cotacao manual ----------------
    @app.route("/cotar", methods=["GET", "POST"])
    def cotar():
        resultado = None
        erro = None
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            try:
                # Cache no processo: a planilha so vai a rede na primeira
                # cotacao, depois a tabela ja esta em memoria.
                if not getattr(tarifas, "tarifas", None):
                    tarifas.carregar()
                pedido = PedidoCotacao(
                    e_cotacao=True,
                    confianca=1.0,
                    origem=form["origem"].strip(),
                    destino=form["destino"].strip(),
                    qtd_volumes=int(form["qtd_volumes"]),
                    valor_nf=_para_float_ptbr(form["valor_nf"]),
                    peso_kg=_para_float_ptbr(form["peso_kg"])
                    if form.get("peso_kg", "").strip()
                    else None,
                    modal=form.get("modal") or None,
                )
                # Numero negativo passa pelo parse, mas nao e cotacao: sem esta
                # barreira sairia um "frete" negativo com cara de valido.
                if pedido.qtd_volumes < 1:
                    raise ValueError("informe ao menos 1 volume")
                if pedido.valor_nf <= 0:
                    raise ValueError("o valor da NF deve ser maior que zero")
                if pedido.peso_kg is not None and pedido.peso_kg < 0:
                    raise ValueError("o peso não pode ser negativo")
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
            # O join pode ter estourado com um ciclo longo em andamento;
            # dizer "desligado" ali seria mentira visivel na propria sidebar.
            if servico.desligando:
                flash("Parada solicitada; o ciclo em andamento termina sozinho.")
            else:
                flash("Loop desligado.")
        elif acao == "ciclo":
            try:
                resumo = servico.ciclo_unico()
                flash(f"Ciclo concluído: {resumo or 'nenhum email novo'}")
            except Exception:
                # O detalhe ja esta em servico.ultimo_erro, exibido na pagina.
                flash("Ciclo falhou — veja o último erro abaixo.")
        elif acao == "config":
            try:
                intervalo = int(request.form["intervalo"])
            except (KeyError, ValueError):
                flash("Intervalo inválido: use um número de segundos.")
                return redirect(url_for("agente_pagina"))
            aplicado = max(30, intervalo)
            servico.intervalo_segundos = aplicado
            estado["modo"] = (
                "enviar" if request.form.get("modo") == "enviar" else "rascunho"
            )
            if intervalo < aplicado:
                # Confirmar "aplicada" escondendo o clamp faria o operador
                # achar que o loop roda a cada 1s.
                flash(f"Intervalo mínimo é 30s; aplicado {aplicado}s.")
            else:
                flash("Configuração aplicada aos próximos ciclos.")
        return redirect(url_for("agente_pagina"))

    return app
