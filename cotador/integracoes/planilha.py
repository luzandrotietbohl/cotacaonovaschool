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

from cotador.core import curadoria
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
    # Colunas de liberacao humana. Nao precisam existir na planilha: sem elas,
    # nenhuma linha esta revisada e os alertas simplesmente aparecem todos.
    "revisado_por": ["revisado_por", "revisado"],
    "revisado_em": ["revisado_em", "revisado_data"],
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
    # Defaults de classe para que os metodos de consulta funcionem numa
    # instancia criada por __new__ — os testes fazem isso de proposito, para
    # exercitar a decisao sobre as tarifas sem exigir credenciais do Sheets.
    _auditoria_bloqueia: bool = True
    _linhas_brutas: list[list[str]] = []
    _linha_da_tarifa: list[int] = []
    _achados: list[curadoria.Achado] = []
    _quarentena: frozenset[int] = frozenset()

    def __init__(
        self,
        credenciais,
        sheet_id: str,
        aba: str,
        auditoria_bloqueia: bool = True,
    ) -> None:
        self._api = build("sheets", "v4", credentials=credenciais, cache_discovery=False)
        self._sheet_id = sheet_id
        self._aba = aba
        # False deixa a curadoria em modo relatorio: audita e registra, mas
        # nao retira nenhuma linha de circulacao. Serve para a primeira
        # rodada, quando ainda nao se sabe o que a tabela real viola.
        self._auditoria_bloqueia = auditoria_bloqueia
        self._tarifas: list[Tarifa] = []
        self._linhas_brutas: list[list[str]] = []
        self._linha_da_tarifa: list[int] = []
        self._achados: list[curadoria.Achado] = []
        self._quarentena: set[int] = set()

    @property
    def tarifas(self) -> list[Tarifa]:
        return self._tarifas

    @property
    def achados(self) -> list[curadoria.Achado]:
        """Tudo o que a curadoria encontrou na ultima carga."""
        return self._achados

    @property
    def quarentena(self) -> list[Tarifa]:
        """Tarifas que existem na planilha mas estao proibidas de cotar."""
        return [self._tarifas[i] for i in sorted(self._quarentena)]

    @property
    def linhas_brutas(self) -> list[list[str]]:
        return self._linhas_brutas

    @property
    def hash_conteudo(self) -> str:
        return curadoria.hash_conteudo(self._linhas_brutas)

    def impressao(self) -> list[dict]:
        """Snapshot comparavel desta carga (camada 0)."""
        return curadoria.impressao(self._tarifas)

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
        self._linhas_brutas = linhas
        self._tarifas = []
        self._linha_da_tarifa = []
        descartadas = 0
        for n, linha in enumerate(linhas[1:], start=2):
            tarifa = self._linha_para_tarifa(linha, indices)
            if tarifa:
                self._tarifas.append(tarifa)
                self._linha_da_tarifa.append(n)
            else:
                descartadas += 1
                log.debug("Linha %d ignorada (incompleta)", n)

        self._auditar()
        log.info(
            "Tarifas carregadas: %d (descartadas: %d, em quarentena: %d)",
            len(self._tarifas),
            descartadas,
            len(self._quarentena),
        )
        return len(self._tarifas)

    def _auditar(self) -> None:
        """Camada 1 da curadoria, mais a quarentena da camada 5.

        A linha bloqueada sai de `rotas` e portanto nao cota, mas continua
        aparecendo em `trecho_cadastrado`. E o que garante que o cliente nunca
        receba um "nao atendemos" por causa de um erro nosso de cadastro: o
        agente manda a thread para revisao humana.
        """
        self._achados = curadoria.auditar_tabela(self._tarifas, self._linha_da_tarifa)
        self._quarentena = (
            curadoria.indices_bloqueados(self._achados)
            if self._auditoria_bloqueia
            else set()
        )
        for achado in curadoria.bloqueios(self._achados):
            nivel = log.error if self._auditoria_bloqueia else log.warning
            nivel("Curadoria (bloqueio): %s", achado)
        for achado in curadoria.alertas(self._achados):
            log.warning("Curadoria (alerta): %s", achado)

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
            revisado_por=celula("revisado_por"),
            revisado_em=_data(celula("revisado_em")),
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
            for i, t in enumerate(self._tarifas)
            if i not in self._quarentena
            and self._casa_local(t.cidade_origem, t.uf_origem, origem)
            and self._casa_local(t.cidade_destino, t.uf_destino, destino)
            and t.vigente_em(hoje)
        ]

    def motivo_quarentena(self, origem: str, destino: str) -> str | None:
        """Por que este trecho parou de cotar, na linguagem de quem corrige.

        None quando o trecho nao tem linha bloqueada — a indisponibilidade,
        se houver, e vigencia ou status, nao curadoria.
        """
        motivos = [
            str(a)
            for a in curadoria.bloqueios(self._achados)
            if a.indice in self._quarentena
            and self._casa_local(
                self._tarifas[a.indice].cidade_origem,
                self._tarifas[a.indice].uf_origem,
                origem,
            )
            and self._casa_local(
                self._tarifas[a.indice].cidade_destino,
                self._tarifas[a.indice].uf_destino,
                destino,
            )
        ]
        return "; ".join(dict.fromkeys(motivos)) or None

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
