"""Monta os dados que as telas exibem, a partir do SQLite do agente."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from cotador.integracoes.banco import Banco

DESFECHOS = ("cotado", "erro", "incompleto", "sem_rota", "ignorado")


def contadores_de_hoje(banco: Banco) -> dict[str, int]:
    # criado_em e gravado em UTC; o dia do filtro segue o mesmo relogio.
    hoje = datetime.now(timezone.utc).date().isoformat()
    bruto = banco.contar_por_desfecho(prefixo_dia=hoje)
    return {desfecho: bruto.get(desfecho, 0) for desfecho in DESFECHOS}


def _quando(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")
    except ValueError:
        return iso


def ultimos_processados(banco: Banco, limite: int = 50) -> list[dict]:
    linhas = banco.ultimos(limite)
    for linha in linhas:
        linha["quando"] = _quando(linha["criado_em"])
        linha["rota"] = (
            f"{linha['origem']} → {linha['destino']}"
            if linha["origem"] and linha["destino"]
            else "—"
        )
    return linhas


def fila_de_revisao(banco: Banco, label_revisar: str) -> list[dict]:
    itens = banco.por_label(label_revisar)
    for item in itens:
        item["quando"] = _quando(item["criado_em"])
        item["extracao"] = (
            json.dumps(
                json.loads(item["extracao_json"]), indent=2, ensure_ascii=False
            )
            if item["extracao_json"]
            else None
        )
    return itens
