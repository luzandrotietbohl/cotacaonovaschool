"""Calculo do frete conforme a formula da aba DICIONARIO:

    MAX(QTD_VOLUMES * (VALOR_POR_VOLUME + PEDAGIO_POR_VOLUME), FRETE_MINIMO)
    + VALOR_NF * (GRIS + ADVALOREM) / 100
    + TAXA_ENTREGA_DIFICIL

Nao ha cobranca por peso nem por cubagem nesta tabela. O peso e usado apenas
para verificar PESO_MAXIMO_VOLUME_KG.
"""
from __future__ import annotations

import re
import unicodedata

from cotador.core.modelos import Cotacao, PedidoCotacao, Tarifa


def normalizar_local(valor: str | None) -> str:
    """Deixa cidade/UF comparavel: sem acento, maiuscula, separador unico.

    'Sao Jose dos Campos - sp' e 'SÃO JOSÉ DOS CAMPOS/SP' viram a mesma chave.
    CEP vira apenas digitos.
    """
    if not valor:
        return ""
    texto = valor.strip()

    digitos = re.sub(r"\D", "", texto)
    if len(digitos) == 8 and not re.sub(r"[\d\-\.\s]", "", texto):
        return digitos

    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    limpo = re.sub(r"\s*[-/,]\s*", "/", sem_acento.upper())
    return re.sub(r"\s{2,}", " ", limpo).strip()


def verificar_peso(pedido: PedidoCotacao, tarifa: Tarifa) -> str | None:
    """Alerta quando o peso medio por volume estoura o limite da rota."""
    limite = tarifa.peso_maximo_volume_kg
    medio = pedido.peso_por_volume
    if not limite or medio is None or medio <= limite:
        return None
    return (
        f"peso medio de {medio:.1f} kg por volume excede o limite de "
        f"{limite:.0f} kg da rota {tarifa.id_rota}"
    )


def calcular(pedido: PedidoCotacao, tarifa: Tarifa) -> Cotacao:
    qtd = pedido.volumes_efetivos
    if not qtd:
        raise ValueError("Quantidade de volumes ausente")
    if pedido.valor_nf is None:
        raise ValueError("Valor da nota fiscal ausente")

    frete_volumes = qtd * (tarifa.valor_por_volume + tarifa.pedagio_por_volume)
    frete_aplicado = max(frete_volumes, tarifa.frete_minimo)
    percentual = tarifa.gris_percentual + tarifa.advalorem_percentual
    gris_advalorem = pedido.valor_nf * percentual / 100
    total = frete_aplicado + gris_advalorem + tarifa.taxa_entrega_dificil

    return Cotacao(
        qtd_volumes=qtd,
        valor_nf=pedido.valor_nf,
        frete_volumes=round(frete_volumes, 2),
        frete_aplicado=round(frete_aplicado, 2),
        gris_advalorem=round(gris_advalorem, 2),
        taxa_entrega_dificil=round(tarifa.taxa_entrega_dificil, 2),
        total=round(total, 2),
        prazo_dias=tarifa.prazo_dias,
        tarifa=tarifa,
        alerta_peso=verificar_peso(pedido, tarifa),
    )
