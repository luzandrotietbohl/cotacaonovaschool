from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from cotador.ml.visualizacao import gerar_tsne


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gera t-SNE amostral dos embarques Olist")
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--artefatos", type=Path, default=RAIZ / "modelos" / "olist" / "atual")
    parser.add_argument("--saida", type=Path, default=RAIZ / "relatorios")
    parser.add_argument("--limite", type=int, default=5000)
    args = parser.parse_args()
    print(json.dumps(gerar_tsne(args.zip, args.artefatos, args.saida, args.limite), indent=2, ensure_ascii=False))
