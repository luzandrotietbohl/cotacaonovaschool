from __future__ import annotations

import json
import math
import secrets
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

from cotador.core.modelos import Cotacao, PedidoCotacao, Tarifa
from cotador.core.precificacao import verificar_peso
from .exceptions import ArtefatosModeloAusentes, CotacaoForaDoDominio, EntradaHistoricaInvalida
from .features import CATEGORICAS, FEATURES, perfil_geografico, preparar_features
from .geografia import LocalNaoResolvido, ResolverGeografico


class PrecificadorHistorico:
    ARQUIVOS = ("model_p25.cbm", "model_p50.cbm", "model_p75.cbm", "structural_outlier.joblib", "geo_map.csv", "metadata.json")

    def __init__(self, pasta_artefatos: Path, bloquear_outlier: bool = True):
        self.pasta = Path(pasta_artefatos)
        ausentes = [nome for nome in self.ARQUIVOS if not (self.pasta / nome).exists()]
        if ausentes:
            raise ArtefatosModeloAusentes("artefatos históricos ausentes: " + ", ".join(ausentes))
        self.modelos = {}
        for nome in ("p25", "p50", "p75"):
            modelo = CatBoostRegressor()
            modelo.load_model(str(self.pasta / f"model_{nome}.cbm"))
            self.modelos[nome] = modelo
        self.outlier = joblib.load(self.pasta / "structural_outlier.joblib")
        self.metadata = json.loads((self.pasta / "metadata.json").read_text(encoding="utf-8"))
        self.ajustes = self.metadata.get("calibration_log_adjustments", {"p25": 0, "p50": 0, "p75": 0})
        self.resolver = ResolverGeografico(self.pasta / "geo_map.csv")
        self.bloquear_outlier = bloquear_outlier

    def cotar(self, pedido: PedidoCotacao, tarifa: Tarifa) -> Cotacao:
        quantidade = pedido.volumes_efetivos
        if not quantidade or quantidade <= 0:
            raise EntradaHistoricaInvalida("quantidade de volumes ausente ou inválida")
        if pedido.peso_kg is None or not math.isfinite(pedido.peso_kg) or pedido.peso_kg <= 0:
            raise EntradaHistoricaInvalida("peso total deve ser informado e maior que zero")
        if pedido.valor_nf is None or not math.isfinite(pedido.valor_nf) or pedido.valor_nf <= 0:
            raise EntradaHistoricaInvalida("valor da mercadoria deve ser maior que zero")

        try:
            origem = self.resolver.resolver(pedido.origem, tarifa.cidade_origem, tarifa.uf_origem)
            destino = self.resolver.resolver(pedido.destino, tarifa.cidade_destino, tarifa.uf_destino)
        except LocalNaoResolvido as exc:
            raise EntradaHistoricaInvalida(str(exc)) from None
        distancia = float(tarifa.distancia_km) if tarifa.distancia_km and tarifa.distancia_km > 0 else self.resolver.distancia(origem, destino)
        features = pd.DataFrame([{
            "distance_km": distancia,
            "weight_total_kg": float(pedido.peso_kg),
            "quantity_items": int(quantidade),
            "declared_value": float(pedido.valor_nf),
            "month": datetime.now().month,
            "origin_state": origem.uf,
            "dest_state": destino.uf,
            "route": f"{origem.uf}>{destino.uf}",
            "same_state": "same_state" if origem.uf == destino.uf else "interstate",
            "geo_profile": perfil_geografico(origem.cidade, destino.cidade),
        }], columns=FEATURES)
        x = preparar_features(features)
        previsoes = {}
        for nome, modelo in self.modelos.items():
            log_pred = float(modelo.predict(x)[0]) + float(self.ajustes.get(nome, 0))
            previsoes[nome] = max(0.0, float(np.expm1(log_pred)))
        ordenadas = sorted(previsoes.values())
        p25, p50, p75 = ordenadas
        estrutural = bool(self.outlier.predict(x)[0] == -1)
        score = float(self.outlier.decision_function(x)[0])
        if estrutural and self.bloquear_outlier:
            raise CotacaoForaDoDominio(f"cotação fora do domínio histórico (score {score:.4f})")
        return Cotacao(
            qtd_volumes=int(quantidade), valor_nf=float(pedido.valor_nf),
            frete_volumes=round(p50, 2), frete_aplicado=round(p50, 2),
            gris_advalorem=0.0, taxa_entrega_dificil=0.0, total=round(p50, 2),
            prazo_dias=tarifa.prazo_dias, tarifa=tarifa,
            alerta_peso=verificar_peso(pedido, tarifa), fonte="historico_olist", quote_id="Q-" + secrets.token_hex(6).upper(),
            p25=round(p25, 2), p50=round(p50, 2), p75=round(p75, 2),
            distancia_km=round(distancia, 3), structural_outlier=estrutural,
            structural_score=score, model_version=str(self.metadata.get("model_version", "desconhecida")),
        )
