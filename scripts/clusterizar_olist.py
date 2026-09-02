from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cotador.ml.clusterizacao import clusterizar


def main() -> int:
    parser = argparse.ArgumentParser(description="Segmenta os embarques Olist e gera o relatório de clusterização")
    parser.add_argument("--dados", required=True, type=Path, help="ZIP Olist, pasta que o contém ou pasta de CSVs")
    parser.add_argument("--saida", type=Path, default=RAIZ / "relatorios" / "clusterizacao")
    parser.add_argument("--k", type=int, default=6, help="número de clusters; 0 escolhe pelo menor Davies-Bouldin")
    parser.add_argument("--k-min", type=int, default=2, help="menor k da varredura diagnóstica")
    parser.add_argument("--k-max", type=int, default=10, help="maior k da varredura diagnóstica")
    parser.add_argument("--replicas", type=int, default=3, help="reajustes em 80% da base para medir estabilidade")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    metadata = clusterizar(
        args.dados, args.saida, k=args.k or None, k_min=args.k_min, k_max=args.k_max,
        replicas=args.replicas, seed=args.seed,
    )
    print(json.dumps(metadata, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
