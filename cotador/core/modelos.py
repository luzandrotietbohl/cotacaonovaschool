"""Estruturas de dados trafegadas entre os modulos.

O modelo de preco segue a aba DICIONARIO da planilha base_rotas_frete:
o frete e cobrado POR VOLUME, nao por peso. Peso serve apenas para validar
PESO_MAXIMO_VOLUME_KG.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


@dataclass
class Volume:
    """Dimensoes informadas pelo cliente. Nao entram no preco desta tabela,
    mas ficam registradas para conferencia na coleta."""

    quantidade: int = 1
    comprimento_cm: float | None = None
    largura_cm: float | None = None
    altura_cm: float | None = None

    @property
    def m3_unitario(self) -> float | None:
        d = (self.comprimento_cm, self.largura_cm, self.altura_cm)
        if any(v is None or v <= 0 for v in d):
            return None
        return (self.comprimento_cm * self.largura_cm * self.altura_cm) / 1_000_000

    @property
    def m3_total(self) -> float | None:
        u = self.m3_unitario
        return None if u is None else u * self.quantidade


@dataclass
class PedidoCotacao:
    """O que o LLM extraiu do corpo do email."""

    e_cotacao: bool
    confianca: float
    origem: str | None = None
    destino: str | None = None
    qtd_volumes: int | None = None
    valor_nf: float | None = None
    peso_kg: float | None = None
    volumes: list[Volume] = field(default_factory=list)
    m3_informado: float | None = None
    modal: str | None = None
    categoria: str | None = None
    observacoes: str | None = None

    @property
    def m3_total(self) -> float | None:
        if self.m3_informado and self.m3_informado > 0:
            return self.m3_informado
        parciais = [v.m3_total for v in self.volumes if v.m3_total]
        return sum(parciais) if parciais else None

    @property
    def volumes_efetivos(self) -> int | None:
        """Quantidade de volumes: o numero explicito ou a soma da lista."""
        if self.qtd_volumes and self.qtd_volumes > 0:
            return self.qtd_volumes
        soma = sum(v.quantidade for v in self.volumes if v.quantidade)
        return soma or None

    @property
    def peso_por_volume(self) -> float | None:
        qtd = self.volumes_efetivos
        if not self.peso_kg or not qtd:
            return None
        return self.peso_kg / qtd

    def mesclar(self, anterior: "PedidoCotacao") -> "PedidoCotacao":
        """Completa os vazios desta extracao com o que a thread ja tinha.

        O cliente responde so o campo que faltava ("valor da nota R$ 200"), e o
        corpo enviado ao LLM nao traz o historico. Sem esta mesclagem o agente
        pediria os mesmos dados para sempre.

        Precedencia: o que o cliente acabou de dizer vence o que estava
        registrado — ele pode estar corrigindo um dado.
        """

        def escolher(novo, velho):
            return novo if novo not in (None, "", 0, []) else velho

        return PedidoCotacao(
            e_cotacao=self.e_cotacao or anterior.e_cotacao,
            # Uma resposta curta sai com confianca baixa por ser curta, nao por
            # ser duvidosa. A evidencia somada da thread e a maior das duas.
            confianca=max(self.confianca, anterior.confianca),
            origem=escolher(self.origem, anterior.origem),
            destino=escolher(self.destino, anterior.destino),
            qtd_volumes=escolher(self.qtd_volumes, anterior.qtd_volumes),
            valor_nf=escolher(self.valor_nf, anterior.valor_nf),
            peso_kg=escolher(self.peso_kg, anterior.peso_kg),
            volumes=escolher(self.volumes, anterior.volumes),
            m3_informado=escolher(self.m3_informado, anterior.m3_informado),
            modal=escolher(self.modal, anterior.modal),
            categoria=escolher(self.categoria, anterior.categoria),
            observacoes=escolher(self.observacoes, anterior.observacoes),
        )

    @classmethod
    def de_dict(cls, d: dict) -> "PedidoCotacao":
        """Reconstroi a extracao salva no banco (dataclasses.asdict)."""
        return cls(
            e_cotacao=bool(d.get("e_cotacao")),
            confianca=float(d.get("confianca") or 0),
            origem=d.get("origem"),
            destino=d.get("destino"),
            qtd_volumes=d.get("qtd_volumes"),
            valor_nf=d.get("valor_nf"),
            peso_kg=d.get("peso_kg"),
            volumes=[Volume(**v) for v in d.get("volumes") or []],
            m3_informado=d.get("m3_informado"),
            modal=d.get("modal"),
            categoria=d.get("categoria"),
            observacoes=d.get("observacoes"),
        )

    def campos_faltantes(self, exigir_peso: bool = True) -> list[str]:
        faltando = []
        if not self.origem:
            faltando.append("origem")
        if not self.destino:
            faltando.append("destino")
        if not self.volumes_efetivos:
            faltando.append("qtd_volumes")
        if self.valor_nf is None or self.valor_nf <= 0:
            faltando.append("valor_nf")
        if exigir_peso and (not self.peso_kg or self.peso_kg <= 0):
            faltando.append("peso")
        return faltando

    def completo(self, exigir_peso: bool = True) -> bool:
        return not self.campos_faltantes(exigir_peso)


@dataclass
class Tarifa:
    """Uma linha normalizada de TABELA_ROTAS."""

    id_rota: str
    cidade_origem: str
    uf_origem: str
    cidade_destino: str
    uf_destino: str
    valor_por_volume: float
    frete_minimo: float
    pedagio_por_volume: float = 0.0
    gris_percentual: float = 0.0
    advalorem_percentual: float = 0.0
    taxa_entrega_dificil: float = 0.0
    peso_maximo_volume_kg: float | None = None
    prazo_dias: int | None = None
    modal: str = "RODOVIARIO"
    regiao_origem: str | None = None
    regiao_destino: str | None = None
    tipo_localidade_destino: str | None = None
    distancia_km: float | None = None
    vigencia_inicio: date | None = None
    vigencia_fim: date | None = None
    status: str = "ATIVO"
    # Liberacao humana de um alerta da curadoria: silencia os alertas desta
    # linha. Nunca silencia bloqueio — valor implausivel se corrige na
    # planilha, nao se aprova por email.
    revisado_por: str | None = None
    revisado_em: date | None = None

    @property
    def revisado(self) -> bool:
        """Alguem assinou esta linha, com nome e data."""
        return bool(self.revisado_por and self.revisado_em)

    @property
    def chave_origem(self) -> str:
        return f"{self.cidade_origem}/{self.uf_origem}"

    @property
    def chave_destino(self) -> str:
        return f"{self.cidade_destino}/{self.uf_destino}"

    def vigente_em(self, dia: date) -> bool:
        if self.status.upper() != "ATIVO":
            return False
        if self.vigencia_inicio and dia < self.vigencia_inicio:
            return False
        if self.vigencia_fim and dia > self.vigencia_fim:
            return False
        return True


@dataclass
class Cotacao:
    """Resultado do calculo, espelhando a aba EXEMPLO_CALCULO."""

    qtd_volumes: int
    valor_nf: float
    frete_volumes: float
    frete_aplicado: float
    gris_advalorem: float
    taxa_entrega_dificil: float
    total: float
    prazo_dias: int | None
    tarifa: Tarifa
    alerta_peso: str | None = None
    fonte: str = "tabela"
    quote_id: str | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    distancia_km: float | None = None
    structural_outlier: bool = False
    structural_score: float | None = None
    model_version: str | None = None

    @property
    def usou_frete_minimo(self) -> bool:
        return self.frete_aplicado > self.frete_volumes


@dataclass
class Email:
    id: str
    thread_id: str
    remetente: str
    nome_remetente: str
    assunto: str
    corpo: str
    message_id_header: str | None
    references_header: str | None
    # Endereco da mensagem na pasta IMAP. Muda entre pastas e sessoes, entao
    # serve para comandos IMAP, nunca como chave de idempotencia — para isso
    # usamos `id`, que e o X-GM-MSGID.
    uid: str | None = None

    @property
    def primeiro_nome(self) -> str:
        """'Luzandro Candido Tietbohl' -> 'Luzandro'. Nome completo no
        cumprimento soa burocratico; usamos so o primeiro."""
        bruto = (self.nome_remetente or "").strip()
        if not bruto:
            return "cliente"
        primeiro = bruto.split()[0].strip(",.;")
        # Remetentes que escrevem o nome em CAIXA ALTA viram Capitalizado.
        return primeiro.capitalize() if primeiro.isupper() else primeiro


Desfecho = Literal["cotado", "incompleto", "sem_rota", "ignorado", "erro"]
