"""Leitura da aba TABELA_ROTAS no Google Sheets.

Os nomes em COLUNAS sao os cabecalhos reais de base_rotas_frete (aba
TABELA_ROTAS), comparados sem acento/maiuscula. Sinonimos podem ser
acrescentados nas listas sem tocar no resto do codigo.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime

from googleapiclient.discovery import build

from cotador.core.modelos import Tarifa
from cotador.core.precificacao import normalizar_local

log = logging.getLogger(__name__)

COLUNAS: dict[str, list[str]] = {
    "id_rota": ["id_rota"],
    "cidade_origem": ["cidade_origem"],
    "uf_origem": ["uf_origem"],
    "regiao_origem": ["regiao_origem"],
    "cidade_destino": ["cidade_destino"],
    "uf_destino": ["uf_destino"],
    "regiao_destino": ["regiao_destino"],
    "tipo_localidade_destino": ["tipo_localidade_destino"],
    "modal": ["modal"],
    "distancia_km": ["distancia_km"],
    "prazo_dias": ["prazo_entrega_dias_uteis", "prazo_entrega", "prazo"],
    "valor_por_volume": ["valor_por_volume"],
    "frete_minimo": ["frete_minimo"],
    "pedagio_por_volume": ["pedagio_por_volume"],
    "gris_percentual": ["gris_percentual"],
    "advalorem_percentual": ["advalorem_percentual", "ad_valorem_percentual"],
    "taxa_entrega_dificil": ["taxa_entrega_dificil"],
    "peso_maximo_volume_kg": ["peso_maximo_volume_kg"],
    "vigencia_inicio": ["vigencia_inicio"],
    "vigencia_fim": ["vigencia_fim"],
    "status": ["status"],
}

OBRIGATORIAS = {
    "cidade_origem",
    "uf_origem",
    "cidade_destino",
    "uf_destino",
    "valor_por_volume",
    "frete_minimo",
}

_FORMATOS_DATA = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d")


def _chave(texto: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    return re.sub(r"[\s_]+", "_", sem_acento.strip().lower())


def _numero(valor: str | None) -> float | None:
    """Converte 'R$ 1.250,50', '1250.50', '0,30' -> float. Vazio -> None."""
    if valor is None:
        return None
    texto = re.sub(r"[^\d,.\-]", "", str(valor)).strip()
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        log.warning("Valor numerico ilegivel na planilha: %r", valor)
        return None


def _data(valor: str | None) -> date | None:
    if not valor:
        return None
    limpo = str(valor).strip()[:10]
    for formato in _FORMATOS_DATA:
        try:
            return datetime.strptime(limpo, formato).date()
        except ValueError:
            continue
    log.warning("Data ilegivel na planilha: %r", valor)
    return None


class TabelaTarifas:
    def __init__(self, credenciais, sheet_id: str, aba: str) -> None:
        self._api = build("sheets", "v4", credentials=credenciais, cache_discovery=False)
        self._sheet_id = sheet_id
        self._aba = aba
        self._tarifas: list[Tarifa] = []

    @property
    def tarifas(self) -> list[Tarifa]:
        return self._tarifas

    def carregar(self) -> int:
        resp = (
            self._api.spreadsheets()
            .values()
            .get(spreadsheetId=self._sheet_id, range=self._aba)
            .execute()
        )
        linhas = resp.get("values", [])
        if len(linhas) < 2:
            raise RuntimeError(f"Aba '{self._aba}' vazia ou sem cabecalho")

        indices = self._mapear_colunas(linhas[0])
        self._tarifas = []
        descartadas = 0
        for n, linha in enumerate(linhas[1:], start=2):
            tarifa = self._linha_para_tarifa(linha, indices)
            if tarifa:
                self._tarifas.append(tarifa)
            else:
                descartadas += 1
                log.debug("Linha %d ignorada (incompleta)", n)

        log.info("Tarifas carregadas: %d (descartadas: %d)", len(self._tarifas), descartadas)
        return len(self._tarifas)

    def _mapear_colunas(self, cabecalho: list[str]) -> dict[str, int]:
        vistos = {_chave(c): i for i, c in enumerate(cabecalho) if c.strip()}
        indices: dict[str, int] = {}
        for campo, sinonimos in COLUNAS.items():
            for sin in sinonimos:
                if sin in vistos:
                    indices[campo] = vistos[sin]
                    break
        faltando = OBRIGATORIAS - indices.keys()
        if faltando:
            raise RuntimeError(
                f"Colunas obrigatorias nao encontradas: {sorted(faltando)}. "
                f"Cabecalho lido: {cabecalho}. Ajuste COLUNAS em planilha.py."
            )
        return indices

    def _linha_para_tarifa(self, linha: list[str], idx: dict[str, int]) -> Tarifa | None:
        def celula(campo: str) -> str | None:
            i = idx.get(campo)
            if i is None or i >= len(linha):
                return None
            return linha[i].strip() or None

        cidade_origem = celula("cidade_origem")
        cidade_destino = celula("cidade_destino")
        valor_por_volume = _numero(celula("valor_por_volume"))
        if not cidade_origem or not cidade_destino or valor_por_volume is None:
            return None

        prazo = _numero(celula("prazo_dias"))
        return Tarifa(
            id_rota=celula("id_rota") or "",
            cidade_origem=normalizar_local(cidade_origem),
            uf_origem=(celula("uf_origem") or "").strip().upper(),
            cidade_destino=normalizar_local(cidade_destino),
            uf_destino=(celula("uf_destino") or "").strip().upper(),
            valor_por_volume=valor_por_volume,
            frete_minimo=_numero(celula("frete_minimo")) or 0.0,
            pedagio_por_volume=_numero(celula("pedagio_por_volume")) or 0.0,
            gris_percentual=_numero(celula("gris_percentual")) or 0.0,
            advalorem_percentual=_numero(celula("advalorem_percentual")) or 0.0,
            taxa_entrega_dificil=_numero(celula("taxa_entrega_dificil")) or 0.0,
            peso_maximo_volume_kg=_numero(celula("peso_maximo_volume_kg")),
            prazo_dias=int(prazo) if prazo else None,
            modal=(celula("modal") or "RODOVIARIO").strip().upper(),
            regiao_origem=celula("regiao_origem"),
            regiao_destino=celula("regiao_destino"),
            tipo_localidade_destino=celula("tipo_localidade_destino"),
            distancia_km=_numero(celula("distancia_km")),
            vigencia_inicio=_data(celula("vigencia_inicio")),
            vigencia_fim=_data(celula("vigencia_fim")),
            status=(celula("status") or "ATIVO").strip().upper(),
        )

    # ---------------- consulta ----------------
    @staticmethod
    def _casa_local(tarifa_cidade: str, tarifa_uf: str, procurado: str) -> bool:
        """Aceita 'CAMPINAS', 'CAMPINAS/SP' ou 'CAMPINAS SP' como o mesmo destino."""
        alvo = normalizar_local(procurado)
        if not alvo:
            return False
        cidade = tarifa_cidade
        return alvo in (cidade, f"{cidade}/{tarifa_uf}", f"{cidade} {tarifa_uf}")

    def rotas(
        self, origem: str, destino: str, dia: date | None = None
    ) -> list[Tarifa]:
        """Todas as tarifas vigentes do trecho (pode haver mais de um modal)."""
        hoje = dia or date.today()
        return [
            t
            for t in self._tarifas
            if self._casa_local(t.cidade_origem, t.uf_origem, origem)
            and self._casa_local(t.cidade_destino, t.uf_destino, destino)
            and t.vigente_em(hoje)
        ]

    def trecho_cadastrado(self, origem: str, destino: str) -> bool:
        """O trecho existe na planilha, ignorando status e vigencia.

        Serve para separar 'nao atendemos essa rota' (nao cadastrada) de
        'tarifa inativa/vencida' (cadastrada, mas sem preco valido hoje) —
        o segundo caso e erro de cadastro, nao pode virar recusa ao cliente.
        """
        return any(
            self._casa_local(t.cidade_origem, t.uf_origem, origem)
            and self._casa_local(t.cidade_destino, t.uf_destino, destino)
            for t in self._tarifas
        )

    def buscar(
        self,
        origem: str,
        destino: str,
        modal: str | None = None,
        dia: date | None = None,
    ) -> Tarifa | None:
        """Tarifa do trecho. Sem modal informado, prefere RODOVIARIO."""
        candidatas = self.rotas(origem, destino, dia)
        if not candidatas:
            return None
        if modal:
            preferidas = [t for t in candidatas if t.modal == modal.strip().upper()]
            if preferidas:
                return preferidas[0]
            log.info("Modal %s indisponivel em %s->%s; usando o padrao", modal, origem, destino)
        rodoviarias = [t for t in candidatas if t.modal == "RODOVIARIO"]
        return (rodoviarias or candidatas)[0]
