"""Fabrica do app Flask do painel. Toda dependencia entra por parametro,
para os testes injetarem fakes (mesmo padrao do Agente)."""
from __future__ import annotations

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

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

    return app
