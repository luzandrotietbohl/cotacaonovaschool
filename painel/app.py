"""Fabrica do app Flask do painel. Toda dependencia entra por parametro,
para os testes injetarem fakes (mesmo padrao do Agente)."""
from __future__ import annotations

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from cotador.core import precificacao
from cotador.core.modelos import PedidoCotacao
from painel import consultas


def _reais(valor) -> str:
    if valor is None:
        return "—"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def criar_app(cfg, banco, tarifas, servico, fabrica_caixa, estado) -> Flask:
    app = Flask(__name__)
    # So para flash() de mensagens; o painel e local e sem login.
    app.config["SECRET_KEY"] = "painel-local"
    app.jinja_env.filters["reais"] = _reais

    @app.context_processor
    def _globais():
        return {"servico": servico, "modo": estado["modo"]}

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
        ids = banco.apagar_thread(thread_id)
        devolvidos = 0
        # Conexao IMAP propria da acao: abre, devolve e fecha (mesmo padrao
        # do ciclo do agente — nada de sessao ociosa pendurada).
        with fabrica_caixa() as caixa:
            for id_email in ids:
                if caixa.devolver_para_fila(id_email, [cfg.LABEL_REVISAR]):
                    devolvidos += 1
        flash(
            f"{devolvidos} de {len(ids)} email(s) devolvidos à fila; "
            "o agente reprocessa no próximo ciclo."
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
                tarifas.carregar()
                pedido = PedidoCotacao(
                    e_cotacao=True,
                    confianca=1.0,
                    origem=form["origem"].strip(),
                    destino=form["destino"].strip(),
                    qtd_volumes=int(form["qtd_volumes"]),
                    valor_nf=float(form["valor_nf"].replace(".", "").replace(",", "."))
                    if "," in form["valor_nf"]
                    else float(form["valor_nf"]),
                    peso_kg=float(form["peso_kg"].replace(",", "."))
                    if form.get("peso_kg", "").strip()
                    else None,
                    modal=form.get("modal") or None,
                )
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
            flash("Loop desligado.")
        elif acao == "ciclo":
            try:
                resumo = servico.ciclo_unico()
                flash(f"Ciclo concluído: {resumo or 'nenhum email novo'}")
            except Exception:
                # O detalhe ja esta em servico.ultimo_erro, exibido na pagina.
                flash("Ciclo falhou — veja o último erro abaixo.")
        elif acao == "config":
            servico.intervalo_segundos = max(30, int(request.form["intervalo"]))
            estado["modo"] = (
                "enviar" if request.form.get("modo") == "enviar" else "rascunho"
            )
            flash("Configuração aplicada aos próximos ciclos.")
        return redirect(url_for("agente_pagina"))

    return app
