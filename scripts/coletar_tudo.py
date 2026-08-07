"""scripts/coletar_tudo.py — orquestrador da Camada 1 (coleta).

Chama o coletar() de cada um dos 6 coletores contra os sites reais. Cada
chamada isolada: falha de uma fonte não impede as outras (mesmo padrão de
falha isolada que já existe dentro de cada coletor individualmente).

Uso:
    python -m scripts.coletar_tudo
"""

import logging
import os
import sys

from dotenv import load_dotenv

import coletores.stj as stj
import coletores.tce_mg as tce_mg
import coletores.tce_pr as tce_pr
import coletores.tce_sp as tce_sp
import coletores.tcu as tcu
import coletores.zenite as zenite
from nucleo.banco import conectar, criar_schema, seed_fontes, transacao
from nucleo.log import configurar_logging

logger = logging.getLogger(__name__)

# (módulo, nome da fonte já semeada por nucleo.banco.seed_fontes).
#
# Achado real (não é bug desta etapa): coletores/tcu.py grava as duas
# publicações do TCU (Informativo LC e Boletim de Jurisprudência) sob um
# único fonte_id, distinguindo por prefixo do título — por isso só a
# primeira linha do TCU entra aqui. A segunda ("TCU — Boletim de
# Jurisprudência") fica sem uso até isso mudar.
_COLETORES = [
    (zenite, "Zênite"),
    (tce_pr, "TCE-PR — Boletim Informativo de Jurisprudência"),
    (tce_mg, "TCE-MG — Informativo de Jurisprudência"),
    (tce_sp, "TCE-SP — Boletim de Jurisprudência + Súmulas"),
    (stj, "STJ — Informativo de Jurisprudência"),
    (tcu, "TCU — Informativo de Licitações e Contratos"),
]


def main() -> None:
    load_dotenv()
    configurar_logging()

    caminho_banco = os.environ.get("DATABASE_PATH")
    if not caminho_banco:
        print("DATABASE_PATH não configurado no .env", file=sys.stderr)
        raise SystemExit(1)

    conexao = conectar(caminho_banco)
    criar_schema(conexao)
    with transacao(conexao):
        seed_fontes(conexao)

    falhas: list[str] = []
    for modulo, nome_fonte in _COLETORES:
        fonte_id = _fonte_id(conexao, nome_fonte)
        logger.info("iniciando coleta", extra={"fonte": nome_fonte})
        try:
            resultado = modulo.coletar(conexao, fonte_id)
        except Exception as erro:
            # rede de segurança do orquestrador — cada coletar() já
            # deveria capturar tudo sozinho, mas falha isolada aqui
            # também, por garantia
            logger.error(
                "coletor quebrou fora do próprio try/except",
                extra={"fonte": nome_fonte, "erro": str(erro)},
            )
            falhas.append(nome_fonte)
            continue

        if resultado.erro:
            falhas.append(nome_fonte)
        logger.info(
            "coleta concluída",
            extra={
                "fonte": nome_fonte, "novos": resultado.itens_novos,
                "repetidos": resultado.itens_repetidos, "erro": resultado.erro,
            },
        )

    conexao.close()

    print()
    print(f"Coleta concluída. {len(_COLETORES) - len(falhas)}/{len(_COLETORES)} fontes ok.")
    if falhas:
        print(f"Falharam: {', '.join(falhas)}")


def _fonte_id(conexao, nome_fonte: str) -> int:
    linha = conexao.execute(
        "SELECT id FROM fontes WHERE nome = ?", (nome_fonte,)
    ).fetchone()
    if linha is None:
        raise RuntimeError(f"fonte não semeada: {nome_fonte!r}")
    return linha["id"]


if __name__ == "__main__":
    main()
