"""SQLite: idempotencia, auditoria das cotacoes e versionamento da tabela.

A tabela `versoes_tabela` e a camada 0 da curadoria. Sem ela nao ha como
responder "que tarifa gerou esta cotacao?" depois de o comercial corrigir a
planilha — e uma cotacao que nao se reconstroi nao se defende.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS processados (
    id_email        TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL,
    remetente       TEXT,
    assunto         TEXT,
    desfecho        TEXT NOT NULL,
    origem          TEXT,
    destino         TEXT,
    id_rota         TEXT,
    qtd_volumes     INTEGER,
    valor_nf        REAL,
    peso_kg         REAL,
    valor_frete     REAL,
    extracao_json   TEXT,
    erro            TEXT,
    criado_em       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processados_thread ON processados(thread_id);
CREATE INDEX IF NOT EXISTS idx_processados_desfecho ON processados(desfecho);

CREATE TABLE IF NOT EXISTS versoes_tabela (
    hash            TEXT PRIMARY KEY,
    aba             TEXT NOT NULL,
    linhas          INTEGER NOT NULL,
    tarifas         INTEGER NOT NULL,
    bloqueios       INTEGER NOT NULL DEFAULT 0,
    impressao_json  TEXT NOT NULL,
    visto_em        TEXT NOT NULL,
    visto_ate       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versoes_visto ON versoes_tabela(aba, visto_ate);
"""

# Colunas acrescentadas depois da primeira versao do banco. ALTER TABLE e
# aplicado so quando falta, para nao exigir que ninguem apague o sqlite.
_COLUNAS_NOVAS = {"tarifa_json": "TEXT"}

_CAMPOS = (
    "id_email",
    "thread_id",
    "remetente",
    "assunto",
    "desfecho",
    "origem",
    "destino",
    "id_rota",
    "qtd_volumes",
    "valor_nf",
    "peso_kg",
    "valor_frete",
    "extracao_json",
    "tarifa_json",
    "erro",
    "criado_em",
)


class Banco:
    def __init__(self, caminho: Path) -> None:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self._caminho = caminho
        with closing(self._conectar()) as con:
            con.executescript(ESQUEMA)
            existentes = {linha[1] for linha in con.execute("PRAGMA table_info(processados)")}
            for coluna, tipo in _COLUNAS_NOVAS.items():
                if coluna not in existentes:
                    con.execute(f"ALTER TABLE processados ADD COLUMN {coluna} {tipo}")
            con.commit()

    def _conectar(self) -> sqlite3.Connection:
        return sqlite3.connect(self._caminho)

    def ja_processado(self, id_email: str) -> bool:
        with closing(self._conectar()) as con:
            cur = con.execute("SELECT 1 FROM processados WHERE id_email = ?", (id_email,))
            return cur.fetchone() is not None

    def ultima_extracao(self, thread_id: str) -> dict | None:
        """Extracao mais recente ja registrada nesta thread.

        Usada para mesclar quando o cliente responde so o dado que faltava.
        """
        with closing(self._conectar()) as con:
            cur = con.execute(
                """SELECT extracao_json FROM processados
                   WHERE thread_id = ? AND extracao_json IS NOT NULL
                   ORDER BY criado_em DESC LIMIT 1""",
                (thread_id,),
            )
            linha = cur.fetchone()
        return json.loads(linha[0]) if linha else None

    def limpar_erros(self) -> int:
        """Esquece os emails com desfecho 'erro' para que voltem para a fila."""
        with closing(self._conectar()) as con:
            cur = con.execute("DELETE FROM processados WHERE desfecho = 'erro'")
            con.commit()
            return cur.rowcount

    def registrar(
        self,
        *,
        id_email: str,
        thread_id: str,
        remetente: str | None,
        assunto: str | None,
        desfecho: str,
        origem: str | None = None,
        destino: str | None = None,
        id_rota: str | None = None,
        qtd_volumes: int | None = None,
        valor_nf: float | None = None,
        peso_kg: float | None = None,
        valor_frete: float | None = None,
        extracao: dict | None = None,
        tarifa: dict | None = None,
        erro: str | None = None,
    ) -> None:
        valores = (
            id_email,
            thread_id,
            remetente,
            assunto,
            desfecho,
            origem,
            destino,
            id_rota,
            qtd_volumes,
            valor_nf,
            peso_kg,
            valor_frete,
            json.dumps(extracao, ensure_ascii=False) if extracao else None,
            json.dumps(tarifa, ensure_ascii=False, default=str) if tarifa else None,
            erro,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        marcadores = ",".join("?" * len(_CAMPOS))
        with closing(self._conectar()) as con:
            con.execute(
                f"INSERT OR REPLACE INTO processados ({','.join(_CAMPOS)}) "
                f"VALUES ({marcadores})",
                valores,
            )
            con.commit()

    # ---------------- camada 0: versoes da tabela de tarifas ----------------
    def registrar_versao_tabela(
        self,
        *,
        hash_conteudo: str,
        aba: str,
        linhas: int,
        tarifas: int,
        bloqueios: int,
        impressao: list[dict],
    ) -> bool:
        """Guarda esta carga da planilha. True se o conteudo e novo.

        Conteudo identico ao da carga anterior so atualiza `visto_ate`: o
        agente recarrega a tabela a cada ciclo e nao interessa guardar a mesma
        planilha 700 vezes por dia.
        """
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._conectar()) as con:
            ja_visto = con.execute(
                "SELECT 1 FROM versoes_tabela WHERE hash = ?", (hash_conteudo,)
            ).fetchone()
            if ja_visto:
                con.execute(
                    "UPDATE versoes_tabela SET visto_ate = ? WHERE hash = ?",
                    (agora, hash_conteudo),
                )
            else:
                con.execute(
                    """INSERT INTO versoes_tabela
                       (hash, aba, linhas, tarifas, bloqueios, impressao_json,
                        visto_em, visto_ate)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        hash_conteudo,
                        aba,
                        linhas,
                        tarifas,
                        bloqueios,
                        json.dumps(impressao, ensure_ascii=False, default=str),
                        agora,
                        agora,
                    ),
                )
            con.commit()
        return not ja_visto

    def versao_anterior(self, aba: str, hash_atual: str) -> dict | None:
        """Ultima versao da tabela com conteudo diferente do atual."""
        with closing(self._conectar()) as con:
            linha = con.execute(
                """SELECT hash, visto_ate, tarifas, bloqueios, impressao_json
                   FROM versoes_tabela
                   WHERE aba = ? AND hash <> ?
                   ORDER BY visto_ate DESC LIMIT 1""",
                (aba, hash_atual),
            ).fetchone()
        if not linha:
            return None
        return {
            "hash": linha[0],
            "visto_ate": linha[1],
            "tarifas": linha[2],
            "bloqueios": linha[3],
            "impressao": json.loads(linha[4]),
        }
