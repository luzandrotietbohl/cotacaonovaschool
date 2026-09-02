from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from cotador.core.precificacao import normalizar_local
from .features import haversine_km, texto_normalizado


@dataclass(frozen=True)
class LocalResolvido:
    latitude: float
    longitude: float
    cidade: str
    uf: str
    fonte: str


class LocalNaoResolvido(ValueError):
    pass


class ResolverGeografico:
    def __init__(self, caminho: Path):
        self._dados = pd.read_csv(caminho, dtype={"zip_prefix": str})
        self._dados["zip_prefix"] = self._dados["zip_prefix"].str.zfill(5)
        self._dados["city_key"] = self._dados["city"].map(texto_normalizado)
        self._dados["state"] = self._dados["state"].astype(str).str.upper()
        self._por_cep = self._dados.drop_duplicates("zip_prefix").set_index("zip_prefix", drop=False)
        cidades = self._dados.groupby(["city_key", "state"], as_index=False).agg(
            lat=("lat", "median"), lng=("lng", "median"), city=("city", "first"), zip_prefix=("zip_prefix", "first")
        )
        self._por_cidade = cidades.set_index(["city_key", "state"], drop=False)

    def resolver(self, valor: str | None, cidade_fallback: str = "", uf_fallback: str = "") -> LocalResolvido:
        bruto = (valor or "").strip()
        digitos = re.sub(r"\D", "", bruto)
        if len(digitos) == 8:
            prefixo = digitos[:5]
            if prefixo in self._por_cep.index:
                return self._linha(self._por_cep.loc[prefixo], "cep")
            if not (cidade_fallback and uf_fallback):
                raise LocalNaoResolvido(f"CEP {bruto} não existe no mapa geográfico do modelo")
            bruto = ""

        cidade, uf = self._cidade_uf(bruto, cidade_fallback, uf_fallback)
        chave = (texto_normalizado(cidade), uf.upper())
        if chave in self._por_cidade.index:
            return self._linha(self._por_cidade.loc[chave], "cidade_uf")
        raise LocalNaoResolvido(f"localidade {cidade}/{uf} não existe no mapa geográfico do modelo")

    @staticmethod
    def _cidade_uf(valor: str, cidade_fallback: str, uf_fallback: str) -> tuple[str, str]:
        normalizado = normalizar_local(valor)
        partes = normalizado.rsplit("/", 1)
        if len(partes) == 2 and len(partes[1]) == 2:
            return partes[0], partes[1]
        if cidade_fallback and uf_fallback:
            return cidade_fallback, uf_fallback
        raise LocalNaoResolvido(f"informe cidade/UF ou CEP, recebido: {valor!r}")

    @staticmethod
    def _linha(linha, fonte: str) -> LocalResolvido:
        if isinstance(linha, pd.DataFrame):
            linha = linha.iloc[0]
        return LocalResolvido(float(linha["lat"]), float(linha["lng"]), str(linha["city"]), str(linha["state"]), fonte)

    def distancia(self, origem: LocalResolvido, destino: LocalResolvido) -> float:
        return float(haversine_km(origem.latitude, origem.longitude, destino.latitude, destino.longitude))
