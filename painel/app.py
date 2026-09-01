"""Fabrica do app Flask do painel. Toda dependencia entra por parametro,
para os testes injetarem fakes (mesmo padrao do Agente)."""
from __future__ import annotations

from flask import Flask, jsonify, render_template

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

    return app
