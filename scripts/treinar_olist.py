from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cotador.ml.treinamento import treinar


def main() -> int:
    parser = argparse.ArgumentParser(description="Treina o precificador histórico por pedido+vendedor Olist")
    parser.add_argument("--zip", required=True, type=Path, help="ZIP Olist ou pasta que o contém")
    parser.add_argument("--saida", type=Path, default=RAIZ / "modelos" / "olist" / "atual")
    parser.add_argument("--iterations", type=int, default=450)
    args = parser.parse_args()
    print(json.dumps(treinar(args.zip, args.saida, iterations=args.iterations), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
