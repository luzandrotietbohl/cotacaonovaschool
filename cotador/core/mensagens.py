"""Templates dos emails de resposta.

>>> AJUSTAR: validade da cotacao, PRAZO_REVISAO_HUMANA e telefone <<<
"""
from __future__ import annotations

from cotador.core.modelos import Cotacao, PedidoCotacao

# Todo email do agente termina dizendo que e automatico e como chegar a uma
# pessoa. E o recurso de quem recebe: sem estas duas linhas o cliente nao
# sabe que foi atendido por um sistema, nem a quem recorrer se discordar.
ASSINATURA = """
Atenciosamente,
Central de Cotacoes
Nova School

Mensagem enviada automaticamente pelo sistema de cotacoes.
Para falar com uma pessoa, responda este email pedindo atendimento humano.
""".rstrip()

# Prazo prometido quando o caso vai para conferencia humana. E uma promessa
# operacional, nao um detalhe de texto: quem muda este valor esta mudando o
# que a empresa se compromete a cumprir. O comando --resumo-revisar existe
# para que alguem consiga cumpri-lo.
PRAZO_REVISAO_HUMANA = "1 dia util"

_ROTULO = {
    "origem": "cidade e estado de origem (coleta)",
    "destino": "cidade e estado de destino (entrega)",
    "qtd_volumes": "quantidade de volumes",
    "valor_nf": "valor da mercadoria / nota fiscal em R$",
    "peso": "peso total da carga em kg",
}


def _reais(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _kg(valor: float) -> str:
    return f"{valor:,.2f} kg".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(valor: float) -> str:
    return f"{valor:.2f}".replace(".", ",") + "%"


def _dias(qtd: int) -> str:
    return "1 dia util" if qtd == 1 else f"{qtd} dias uteis"


def solicitar_dados(pedido: PedidoCotacao, nome: str, exigir_peso: bool = True) -> str:
    faltantes = pedido.campos_faltantes(exigir_peso)
    itens = "\n".join(f"  - {_ROTULO[c]}" for c in faltantes)

    recebidos = []
    if pedido.origem:
        recebidos.append(f"origem: {pedido.origem}")
    if pedido.destino:
        recebidos.append(f"destino: {pedido.destino}")
    if pedido.volumes_efetivos:
        recebidos.append(f"volumes: {pedido.volumes_efetivos}")
    if pedido.valor_nf:
        recebidos.append(f"valor da mercadoria: {_reais(pedido.valor_nf)}")
    if pedido.peso_kg:
        recebidos.append(f"peso: {_kg(pedido.peso_kg)}")
    if pedido.m3_total:
        recebidos.append(f"cubagem: {pedido.m3_total:.3f} m3")

    bloco_recebido = (
        "\nJa temos registrado:\n" + "\n".join(f"  - {r}" for r in recebidos) + "\n"
        if recebidos
        else ""
    )

    return f"""Ola, {nome},

Recebemos sua solicitacao de cotacao de frete. Para calcular o valor,
precisamos das informacoes abaixo:

{itens}
{bloco_recebido}
Pode responder este mesmo email com os dados e retornamos a cotacao em seguida.
{ASSINATURA}"""


def enviar_cotacao(pedido: PedidoCotacao, cotacao: Cotacao, nome: str) -> str:
    if cotacao.fonte == "historico_olist":
        prazo = (
            f"Prazo estimado de entrega: {_dias(cotacao.prazo_dias)} apos a coleta.\n"
            if cotacao.prazo_dias else ""
        )
        obs = f"Observacao registrada: {pedido.observacoes}\n" if pedido.observacoes else ""
        return f"""Ola, {nome},

Segue a cotacao de frete solicitada.

Trecho: {pedido.origem} -> {pedido.destino}
Volumes: {cotacao.qtd_volumes}
Valor da mercadoria: {_reais(cotacao.valor_nf)}
Peso informado: {_kg(pedido.peso_kg)}

VALOR TOTAL DO FRETE: {_reais(cotacao.total)}

{prazo}{obs}
Cotacao valida por 7 dias, sujeita a conferencia dos dados da carga na coleta.
Para seguir com a coleta, basta responder este email confirmando.
{ASSINATURA}"""

    t = cotacao.tarifa
    plural = "volume" if cotacao.qtd_volumes == 1 else "volumes"

    # Montada como lista para nao sobrar linha em branco quando a taxa e zero
    # ou quando o frete minimo nao foi acionado.
    composicao = [
        f"  Frete ({cotacao.qtd_volumes} {plural} x "
        f"{_reais(t.valor_por_volume + t.pedagio_por_volume)}): {_reais(cotacao.frete_volumes)}"
    ]
    if cotacao.usou_frete_minimo:
        composicao.append(
            f"  Frete minimo da rota aplicado: {_reais(cotacao.frete_aplicado)}"
        )
    composicao.append(
        f"  GRIS + ad valorem ({_pct(t.gris_percentual + t.advalorem_percentual)} "
        f"sobre a NF): {_reais(cotacao.gris_advalorem)}"
    )
    if cotacao.taxa_entrega_dificil > 0:
        composicao.append(
            f"  Taxa de entrega dificil: {_reais(cotacao.taxa_entrega_dificil)}"
        )

    cabecalho = [
        f"Trecho: {pedido.origem} -> {pedido.destino}"
        + (f" ({t.modal.lower()})" if t.modal else ""),
        f"Volumes: {cotacao.qtd_volumes}",
        f"Valor da mercadoria: {_reais(cotacao.valor_nf)}",
    ]
    if pedido.peso_kg:
        cabecalho.append(f"Peso informado: {_kg(pedido.peso_kg)}")

    prazo = (
        f"Prazo estimado de entrega: {_dias(cotacao.prazo_dias)} apos a coleta.\n"
        if cotacao.prazo_dias
        else ""
    )
    obs = f"Observacao registrada: {pedido.observacoes}\n" if pedido.observacoes else ""

    return f"""Ola, {nome},

Segue a cotacao de frete solicitada.

{chr(10).join(cabecalho)}

Composicao:
{chr(10).join(composicao)}

VALOR TOTAL DO FRETE: {_reais(cotacao.total)}

{prazo}{obs}
Cotacao valida por 7 dias, sujeita a conferencia de volumes, peso e medidas na coleta.
Para seguir com a coleta, basta responder este email confirmando.
{ASSINATURA}"""


def aguardar_analise(pedido: PedidoCotacao, nome: str) -> str:
    """Pedido valido que o agente nao pode cotar sozinho.

    Nao diz por que. O motivo e interno — limite de peso da rota, tarifa em
    quarentena pela curadoria — e nao ajuda quem le, alem de expor cadastro.
    Diz o que importa ao cliente: recebemos, esta com uma pessoa, e quando
    voltamos. O silencio era a pior das opcoes: ele esperava sem saber que
    estava esperando.
    """
    trecho = (
        f" para o trecho {pedido.origem} -> {pedido.destino}"
        if pedido.origem and pedido.destino
        else ""
    )
    return f"""Ola, {nome},

Recebemos sua solicitacao de cotacao de frete{trecho}.

Este caso precisa da conferencia de uma pessoa da nossa equipe comercial,
por isso o valor nao segue nesta mensagem. Retornamos com a cotacao em ate
{PRAZO_REVISAO_HUMANA}.
{ASSINATURA}"""


def sem_rota(pedido: PedidoCotacao, nome: str) -> str:
    return f"""Ola, {nome},

Agradecemos o contato. Infelizmente nao atendemos o trecho
{pedido.origem} -> {pedido.destino}, portanto nao conseguimos apresentar
cotacao para esta rota.

Caso tenha outra origem ou destino, ficamos a disposicao para cotar.
{ASSINATURA}"""
