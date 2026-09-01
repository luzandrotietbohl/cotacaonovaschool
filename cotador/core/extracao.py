"""Classificacao e extracao dos dados da cotacao via Claude API.

Usa tool_choice forcado: o modelo e obrigado a devolver JSON validado pelo
schema da ferramenta, sem parsing de texto livre.
"""
from __future__ import annotations

import json
import logging

from anthropic import Anthropic

from cotador.core.modelos import PedidoCotacao, Volume

log = logging.getLogger(__name__)

FERRAMENTA = {
    "name": "registrar_cotacao",
    "description": (
        "Registra a analise de um email recebido pela transportadora. "
        "Sempre chame esta ferramenta, mesmo quando o email nao for uma cotacao."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "e_cotacao": {
                "type": "boolean",
                "description": (
                    "true somente se o remetente esta pedindo preco/cotacao de frete "
                    "para transportar mercadoria. Cobranca, reclamacao, rastreio, nota "
                    "fiscal, spam, newsletter e resposta automatica sao false."
                ),
            },
            "confianca": {
                "type": "number",
                "description": "Confianca de 0 a 1 na classificacao e na extracao.",
            },
            "origem": {
                "type": ["string", "null"],
                "description": (
                    "Local de coleta como escrito no email. Prefira 'CIDADE/UF'. "
                    "Se vier CEP, devolva o CEP. null se nao informado."
                ),
            },
            "destino": {
                "type": ["string", "null"],
                "description": "Local de entrega, mesmo formato de origem. null se ausente.",
            },
            "qtd_volumes": {
                "type": ["integer", "null"],
                "description": (
                    "Quantidade total de volumes/caixas/pallets a transportar. "
                    "'3 caixas' = 3. 'uma caixa' = 1. null se nao informado."
                ),
            },
            "valor_nf": {
                "type": ["number", "null"],
                "description": (
                    "Valor da mercadoria / nota fiscal em reais. Aceita 'NF de R$ 8.000', "
                    "'valor da carga 8 mil', 'mercadoria vale 8000'. null se ausente."
                ),
            },
            "peso_kg": {
                "type": ["number", "null"],
                "description": (
                    "Peso TOTAL da carga em quilos. Converta toneladas (t) e gramas. "
                    "null se nao informado."
                ),
            },
            "volumes": {
                "type": "array",
                "description": (
                    "Volumes com dimensoes, quando informadas. Converta metros para "
                    "centimetros. Lista vazia se o email nao trouxer dimensoes."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "quantidade": {"type": "integer"},
                        "comprimento_cm": {"type": "number"},
                        "largura_cm": {"type": "number"},
                        "altura_cm": {"type": "number"},
                    },
                    "required": ["quantidade", "comprimento_cm", "largura_cm", "altura_cm"],
                },
            },
            "m3_informado": {
                "type": ["number", "null"],
                "description": (
                    "Cubagem total em m3 quando o cliente ja informa pronta "
                    "(ex.: 'cubagem 1,2 m3'). null caso contrario."
                ),
            },
            "modal": {
                "type": ["string", "null"],
                "description": (
                    "RODOVIARIO ou AEREO, somente se o cliente pedir explicitamente "
                    "(ex.: 'preciso aereo', 'urgente por aviao'). null caso contrario."
                ),
                "enum": ["RODOVIARIO", "AEREO", None],
            },
            "observacoes": {
                "type": ["string", "null"],
                "description": (
                    "Pedidos especiais relevantes em uma frase: carga perigosa, "
                    "refrigerada, agendamento, coleta em condominio, etc."
                ),
            },
        },
        "required": ["e_cotacao", "confianca", "volumes"],
    },
}

SISTEMA = """Voce analisa emails recebidos por uma transportadora rodoviaria de cargas.
Sua unica saida e a chamada da ferramenta registrar_cotacao.

Regras:
- Extraia SOMENTE o que esta escrito no email. Nunca invente peso, medidas, valores
  ou cidades.
- Numeros em portugues usam virgula decimal e ponto de milhar: "1.250,50" = 1250.5.
- Notacao de medidas: "40x30x20" = comprimento x largura x altura em cm, salvo indicacao
  contraria explicita. "2 cx 40x30x20" = quantidade 2 com essas medidas.
- Some as quantidades para qtd_volumes: "2 caixas e 1 pallet" = 3.
- "12 pallets PBR" sem medidas: preencha qtd_volumes=12 e deixe volumes vazio.
- Nao confunda valor_nf (valor da mercadoria) com o valor do frete que o cliente
  eventualmente cite como referencia de concorrente.
- Se o email for uma resposta do cliente complementando dados, extraia os dados novos.
- Em duvida entre cotacao e outro assunto, marque e_cotacao=false com confianca baixa."""


class Extrator:
    def __init__(self, api_key: str, modelo: str, workspace_id: str = "") -> None:
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY nao definida no .env — necessaria para classificar "
                "e extrair os dados dos emails."
            )
        # Chaves identity-linked exigem o workspace no header, senao a API
        # responde 400 "anthropic-workspace-id is required".
        cabecalhos = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._cliente = Anthropic(api_key=api_key, default_headers=cabecalhos)
        self._modelo = modelo

    def analisar(self, assunto: str, corpo: str) -> PedidoCotacao:
        if not (corpo or "").strip():
            # Sem texto nao ha o que extrair (encaminhamento so com anexo, por
            # exemplo). Evita uma chamada paga de resultado garantidamente vazio.
            log.info("Corpo vazio; classificando como nao-cotacao sem chamar o LLM")
            return PedidoCotacao(e_cotacao=False, confianca=0.0)
        return self._chamar(assunto, corpo)

    def _chamar(self, assunto: str, corpo: str) -> PedidoCotacao:
        resposta = self._cliente.messages.create(
            model=self._modelo,
            max_tokens=2048,
            system=SISTEMA,
            tools=[FERRAMENTA],
            tool_choice={"type": "tool", "name": "registrar_cotacao"},
            messages=[
                {
                    "role": "user",
                    "content": f"Assunto: {assunto}\n\nCorpo do email:\n{corpo}",
                }
            ],
        )

        bruto = next((b.input for b in resposta.content if b.type == "tool_use"), None)
        if bruto is None:
            raise RuntimeError("Modelo nao retornou tool_use em registrar_cotacao")

        log.debug("Extracao: %s", json.dumps(bruto, ensure_ascii=False))
        return self._para_pedido(bruto)

    @staticmethod
    def _para_pedido(d: dict) -> PedidoCotacao:
        volumes = [
            Volume(
                quantidade=int(v.get("quantidade") or 1),
                comprimento_cm=v.get("comprimento_cm"),
                largura_cm=v.get("largura_cm"),
                altura_cm=v.get("altura_cm"),
            )
            for v in d.get("volumes") or []
        ]
        qtd = d.get("qtd_volumes")
        return PedidoCotacao(
            e_cotacao=bool(d.get("e_cotacao")),
            confianca=float(d.get("confianca") or 0),
            origem=(d.get("origem") or None),
            destino=(d.get("destino") or None),
            qtd_volumes=int(qtd) if qtd else None,
            valor_nf=d.get("valor_nf"),
            peso_kg=d.get("peso_kg"),
            volumes=volumes,
            m3_informado=d.get("m3_informado"),
            modal=(d.get("modal") or None),
            observacoes=d.get("observacoes"),
        )
