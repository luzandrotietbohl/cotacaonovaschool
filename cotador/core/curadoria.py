"""Curadoria da TABELA_ROTAS: limites duros por campo e comparacao de versoes.

Este modulo nao usa estatistica. Sao intervalos declarados por pessoas,
escritos uma vez, que pegam a classe de erro mais comum e mais destrutiva do
cadastro manual: casa decimal a mais, virgula trocada por ponto, valor
digitado na coluna do vizinho.

Detectar "fora da curva" estatisticamente nao resolveria o caso que mais
custa. `16,65 -> 166,50` qualquer teste pega; `16,65 -> 18,65` nao e outlier
em nenhuma metrica e cota errado para sempre. Esse segundo caso so aparece na
comparacao com a versao anterior da tabela (`comparar`), nunca na distribuicao.

Duas severidades, porque as consequencias sao diferentes:

BLOQUEIO  o valor e implausivel para o negocio. A linha entra em quarentena e
          para de cotar. Nao existe aprovacao para isto: um GRIS de 55% nao e
          uma tarifa a autorizar, e um 0,55 digitado errado. Corrige-se na
          planilha.
ALERTA    o valor e incomum, mas pode ser legitimo. Relata e segue cotando. O
          comercial silencia preenchendo REVISADO_POR e REVISADO_EM na linha.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from cotador.core.modelos import Tarifa

Severidade = Literal["bloqueio", "alerta"]

MODAIS_CONHECIDOS = ("RODOVIARIO", "AEREO")

# Soma de GRIS + ad valorem. O erro mais caro da tabela e digitar 0,30 como 30:
# a parcela sobre a nota fiscal fica 100x maior e o total do frete explode.
LIMITE_SOMA_PERCENTUAL = 3.0


@dataclass(frozen=True)
class Faixa:
    """Intervalo aceitavel para um campo numerico da tarifa."""

    minimo: float
    maximo: float
    severidade: Severidade
    unidade: str = ""
    porque: str = ""


FAIXAS: dict[str, Faixa] = {
    "valor_por_volume": Faixa(
        1.0, 500.0, "bloqueio", "R$",
        "uma casa decimal a mais aqui multiplica o frete inteiro",
    ),
    "frete_minimo": Faixa(
        20.0, 2000.0, "bloqueio", "R$",
        "abaixo de R$ 20 nao cobre custo; acima de R$ 2.000 nao e minimo",
    ),
    "pedagio_por_volume": Faixa(0.0, 200.0, "bloqueio", "R$"),
    "gris_percentual": Faixa(
        0.0, 2.0, "bloqueio", "%",
        "0,30 digitado como 30 multiplica a parcela sobre a NF por 100",
    ),
    "advalorem_percentual": Faixa(0.0, 2.0, "bloqueio", "%"),
    "taxa_entrega_dificil": Faixa(0.0, 1000.0, "bloqueio", "R$"),
    "peso_maximo_volume_kg": Faixa(1.0, 2000.0, "alerta", "kg"),
    "prazo_dias": Faixa(0.0, 30.0, "alerta", "dias"),
    "distancia_km": Faixa(1.0, 6000.0, "alerta", "km"),
}


@dataclass(frozen=True)
class Achado:
    """Uma regra violada por uma linha da tabela."""

    indice: int          # posicao na lista de tarifas carregadas
    id_rota: str
    campo: str
    severidade: Severidade
    mensagem: str
    linha: int | None = None   # linha na planilha, preenchida por quem le

    @property
    def bloqueia(self) -> bool:
        return self.severidade == "bloqueio"

    def __str__(self) -> str:
        onde = f"linha {self.linha}" if self.linha else f"item {self.indice}"
        rota = self.id_rota or "sem id_rota"
        # Sem acento nem pontuacao tipografica: isto sai em console do Windows.
        return f"{onde} {rota} | {self.campo}: {self.mensagem}"


def _n(valor: float) -> str:
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _unid(valor: float, unidade: str) -> str:
    """R$ vem antes, % vem colado, o resto vem depois com espaco."""
    if unidade == "R$":
        return f"R$ {_n(valor)}"
    if unidade == "%":
        return f"{_n(valor)}%"
    return f"{_n(valor)} {unidade}".strip()


# --------------------------------------------------------------------------
# Camada 1 — limites duros
# --------------------------------------------------------------------------


def auditar_linha(tarifa: Tarifa, indice: int = 0) -> list[Achado]:
    """Regras que uma linha viola olhando so para ela mesma."""
    achados: list[Achado] = []

    def anotar(campo: str, severidade: Severidade, mensagem: str) -> None:
        achados.append(
            Achado(
                indice=indice,
                id_rota=tarifa.id_rota,
                campo=campo,
                severidade=severidade,
                mensagem=mensagem,
            )
        )

    # ---- faixas por campo ----
    for campo, faixa in FAIXAS.items():
        valor = getattr(tarifa, campo, None)
        if valor is None:  # campo opcional em branco nao e erro
            continue
        if faixa.minimo <= float(valor) <= faixa.maximo:
            continue
        detalhe = f" - {faixa.porque}" if faixa.porque else ""
        anotar(
            campo,
            faixa.severidade,
            f"{_unid(float(valor), faixa.unidade)} fora da faixa "
            f"{_unid(faixa.minimo, faixa.unidade)} a "
            f"{_unid(faixa.maximo, faixa.unidade)}{detalhe}",
        )

    # ---- regras entre campos da mesma linha ----
    soma = tarifa.gris_percentual + tarifa.advalorem_percentual
    if soma > LIMITE_SOMA_PERCENTUAL:
        anotar(
            "gris_percentual+advalorem_percentual",
            "bloqueio",
            f"somam {_n(soma)}%, acima do limite de {_n(LIMITE_SOMA_PERCENTUAL)}% "
            "sobre o valor da nota fiscal",
        )

    if tarifa.pedagio_por_volume > tarifa.valor_por_volume:
        anotar(
            "pedagio_por_volume",
            "alerta",
            f"R$ {_n(tarifa.pedagio_por_volume)} maior que o valor por volume "
            f"(R$ {_n(tarifa.valor_por_volume)}); conferir se as colunas nao "
            "foram trocadas",
        )

    piso_um_volume = tarifa.valor_por_volume + tarifa.pedagio_por_volume
    if tarifa.frete_minimo and tarifa.frete_minimo < piso_um_volume:
        anotar(
            "frete_minimo",
            "alerta",
            f"R$ {_n(tarifa.frete_minimo)} nunca se aplica: um unico volume ja "
            f"custa R$ {_n(piso_um_volume)}",
        )

    if (
        tarifa.vigencia_inicio
        and tarifa.vigencia_fim
        and tarifa.vigencia_fim < tarifa.vigencia_inicio
    ):
        anotar(
            "vigencia_fim",
            "bloqueio",
            f"{tarifa.vigencia_fim} anterior ao inicio {tarifa.vigencia_inicio}: "
            "a rota fica invisivel sem que ninguem perceba",
        )

    if not tarifa.id_rota:
        anotar("id_rota", "alerta", "vazio: a linha nao e rastreavel na auditoria")

    for campo, uf in (("uf_origem", tarifa.uf_origem), ("uf_destino", tarifa.uf_destino)):
        if uf and len(uf) != 2:
            anotar(campo, "alerta", f"'{uf}' nao parece uma UF de duas letras")

    if tarifa.modal and tarifa.modal not in MODAIS_CONHECIDOS:
        anotar(
            "modal",
            "alerta",
            f"'{tarifa.modal}' desconhecido; o agente so escolhe entre "
            f"{' e '.join(MODAIS_CONHECIDOS)}",
        )

    # Alertas sao silenciaveis por revisao humana; bloqueios nunca.
    if tarifa.revisado:
        achados = [a for a in achados if a.bloqueia]

    return achados


def _trecho(tarifa: Tarifa) -> tuple[str, str, str]:
    return (tarifa.chave_origem, tarifa.chave_destino, tarifa.modal)


def _sobrepoem(a: Tarifa, b: Tarifa) -> bool:
    """Duas vigencias que se cruzam no calendario."""
    inicio_a, fim_a = a.vigencia_inicio, a.vigencia_fim
    inicio_b, fim_b = b.vigencia_inicio, b.vigencia_fim
    if fim_a and inicio_b and fim_a < inicio_b:
        return False
    if fim_b and inicio_a and fim_b < inicio_a:
        return False
    return True


def auditar_tabela(
    tarifas: list[Tarifa], linhas: list[int] | None = None
) -> list[Achado]:
    """Todos os achados da tabela: por linha e entre linhas.

    `linhas[i]` e o numero da linha de `tarifas[i]` na planilha, usado apenas
    para a mensagem chegar ao humano com um endereco que ele reconheca.
    """
    achados: list[Achado] = []
    for i, tarifa in enumerate(tarifas):
        achados.extend(auditar_linha(tarifa, i))

    # id_rota repetido: nao muda o preco, mas quebra a auditoria.
    por_id: dict[str, list[int]] = {}
    for i, t in enumerate(tarifas):
        if t.id_rota:
            por_id.setdefault(t.id_rota, []).append(i)
    for id_rota, indices in por_id.items():
        if len(indices) > 1:
            for i in indices:
                achados.append(
                    Achado(
                        indice=i,
                        id_rota=id_rota,
                        campo="id_rota",
                        severidade="alerta",
                        mensagem=f"repetido em {len(indices)} linhas",
                    )
                )

    # Duas tarifas vigentes para o mesmo trecho e modal: `buscar` devolve a
    # primeira da lista, arbitrariamente. Uma das duas esta errada e o cliente
    # recebe um preco sorteado pela ordem da planilha. Bloqueia as duas.
    por_trecho: dict[tuple[str, str, str], list[int]] = {}
    for i, t in enumerate(tarifas):
        if t.status.upper() == "ATIVO":
            por_trecho.setdefault(_trecho(t), []).append(i)
    for (origem, destino, modal), indices in por_trecho.items():
        if len(indices) < 2:
            continue
        colidem = {
            i
            for a in range(len(indices))
            for b in range(a + 1, len(indices))
            if _sobrepoem(tarifas[indices[a]], tarifas[indices[b]])
            for i in (indices[a], indices[b])
        }
        for i in sorted(colidem):
            achados.append(
                Achado(
                    indice=i,
                    id_rota=tarifas[i].id_rota,
                    campo="vigencia",
                    severidade="bloqueio",
                    mensagem=(
                        f"{origem} -> {destino} [{modal}] tem mais de uma tarifa "
                        "vigente ao mesmo tempo; o preco enviado dependeria da "
                        "ordem das linhas"
                    ),
                )
            )

    if linhas:
        achados = [
            dataclasses.replace(a, linha=linhas[a.indice])
            if a.indice < len(linhas)
            else a
            for a in achados
        ]
    return achados


def bloqueios(achados: list[Achado]) -> list[Achado]:
    return [a for a in achados if a.bloqueia]


def alertas(achados: list[Achado]) -> list[Achado]:
    return [a for a in achados if not a.bloqueia]


def indices_bloqueados(achados: list[Achado]) -> set[int]:
    """Posicoes das tarifas que nao podem cotar."""
    return {a.indice for a in achados if a.bloqueia}


# --------------------------------------------------------------------------
# Camada 0 — snapshot e comparacao entre versoes
# --------------------------------------------------------------------------

# Campos cuja mudanca altera o preco ou a disponibilidade da rota. Mudar
# regiao_destino nao muda cotacao nenhuma; mudar gris_percentual muda todas.
CAMPOS_SENSIVEIS = (
    "valor_por_volume",
    "frete_minimo",
    "pedagio_por_volume",
    "gris_percentual",
    "advalorem_percentual",
    "taxa_entrega_dificil",
    "peso_maximo_volume_kg",
    "prazo_dias",
    "vigencia_inicio",
    "vigencia_fim",
    "status",
)


def impressao(tarifas: list[Tarifa]) -> list[dict]:
    """Snapshot comparavel de uma carga da tabela."""
    return [
        {
            "chave": f"{t.id_rota}|{t.chave_origem}|{t.chave_destino}|{t.modal}",
            **{c: _serial(getattr(t, c)) for c in CAMPOS_SENSIVEIS},
        }
        for t in tarifas
    ]


def _serial(valor):
    return valor.isoformat() if hasattr(valor, "isoformat") else valor


def hash_conteudo(linhas: list[list[str]]) -> str:
    """Hash do conteudo bruto lido da planilha, para versionar a tabela."""
    cru = json.dumps(linhas, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(cru.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Mudanca:
    tipo: Literal["nova", "removida", "alterada"]
    chave: str
    campo: str | None = None
    de: object = None
    para: object = None

    def __str__(self) -> str:
        if self.tipo == "alterada":
            return f"alterada  {self.chave}  {self.campo}: {self.de} -> {self.para}"
        return f"{self.tipo:9} {self.chave}"


def comparar(antes: list[dict], depois: list[dict]) -> list[Mudanca]:
    """O que mudou entre dois snapshots, campo a campo.

    E aqui que aparece o erro plausivel — o que esta dentro de todas as faixas
    e ainda assim errado. Um salto de 12% em valor_por_volume nao viola limite
    nenhum; violado esta o historico daquela rota.
    """
    mapa_antes = {d["chave"]: d for d in antes}
    mapa_depois = {d["chave"]: d for d in depois}

    mudancas: list[Mudanca] = []
    for chave in sorted(mapa_depois.keys() - mapa_antes.keys()):
        mudancas.append(Mudanca("nova", chave))
    for chave in sorted(mapa_antes.keys() - mapa_depois.keys()):
        mudancas.append(Mudanca("removida", chave))
    for chave in sorted(mapa_antes.keys() & mapa_depois.keys()):
        a, d = mapa_antes[chave], mapa_depois[chave]
        for campo in CAMPOS_SENSIVEIS:
            if a.get(campo) != d.get(campo):
                mudancas.append(
                    Mudanca("alterada", chave, campo, a.get(campo), d.get(campo))
                )
    return mudancas
