"""scripts/rodar_triagem.py — comando de linha da Camada 3+4 (dedup +
triagem), pedido pela Etapa 5.

Roda sobre tudo que está em itens_brutos com status='coletado': fatia,
calcula a chave de dedup, chama a triagem (Gemini) e grava em `decisoes`.
Imprime uma tabela com título, decisão da triagem e motivo — é essa tabela
que se lê descarte por descarte antes de avançar pra Etapa 6.

Com --reprocessar-erros, também inclui itens com status='erro' — pensado
pra falha transitória (cota do LLM esgotada), não erro de dado. Se um item
cair em erro de novo depois de reprocessado, é indício de bug de verdade,
não de cota.

Uso:
    python -m scripts.rodar_triagem [--limite N] [--reprocessar-erros]
"""

import argparse
import logging
import os
import sys
import textwrap

from dotenv import load_dotenv

from nucleo.banco import conectar, inserir_decisao, transacao
from nucleo.dedup import calcular_chave_dedup
from nucleo.fatiador import fatiar_item
from nucleo.llm import criar_cliente_llm
from nucleo.log import configurar_logging
from nucleo.triagem import extrair_trecho, mesclar_metadados, triar

logger = logging.getLogger(__name__)

LARGURA_TITULO = 48
LARGURA_MOTIVO = 60


def main() -> None:
    # achado real (scripts/rodar_analise.py): print() redirecionado no
    # Windows cai no codepage padrão (cp1252), que não cobre boa parte do
    # que o LLM pode devolver em motivo/tema — UnicodeEncodeError derruba
    # a rodada inteira já processada
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv()
    configurar_logging()

    args = _ler_argumentos()

    caminho_banco = os.environ.get("DATABASE_PATH")
    if not caminho_banco:
        print("DATABASE_PATH não configurado no .env", file=sys.stderr)
        raise SystemExit(1)

    cliente = criar_cliente_llm()
    conexao = conectar(caminho_banco)

    itens = _itens_pendentes(
        conexao, limite=args.limite, reprocessar_erros=args.reprocessar_erros,
    )
    if not itens:
        status_buscado = "'coletado' ou 'erro'" if args.reprocessar_erros else "'coletado'"
        print(f"Nenhum item pendente em itens_brutos (status={status_buscado}).")
        conexao.close()
        return

    if args.reprocessar_erros:
        novos = sum(1 for item in itens if item["status"] == "coletado")
        reprocessados = sum(1 for item in itens if item["status"] == "erro")
        print(f"{len(itens)} pendentes — {novos} novos, {reprocessados} em reprocessamento.")
        print()

    _imprimir_cabecalho()

    total_decisoes = total_relevantes = total_descartadas = 0
    itens_com_erro: list[str] = []

    for item in itens:
        try:
            linhas, relevantes, descartadas = _processar_item(conexao, cliente, item)
        except Exception as erro:
            # falha isolada por item_bruto — um item com bug no fatiador ou
            # com o LLM fora do ar não pode travar os outros já pendentes
            logger.error(
                "falha ao processar item_bruto, marcando como erro",
                extra={"item_bruto_id": item["id"], "erro": str(erro)},
            )
            with transacao(conexao):
                conexao.execute(
                    "UPDATE itens_brutos SET status = 'erro' WHERE id = ?",
                    (item["id"],),
                )
            itens_com_erro.append(f"item_bruto_id={item['id']} ({item['fonte_nome']})")
            continue

        for linha in linhas:
            print(linha)
        total_decisoes += relevantes + descartadas
        total_relevantes += relevantes
        total_descartadas += descartadas

    conexao.close()

    print()
    print(
        f"{total_decisoes} decisões processadas — "
        f"{total_relevantes} relevantes, {total_descartadas} descartadas."
    )
    if itens_com_erro:
        print(f"{len(itens_com_erro)} item(ns) com erro, não processado(s):")
        for descricao in itens_com_erro:
            print(f"  - {descricao}")


def _processar_item(conexao, cliente, item) -> tuple[list[str], int, int]:
    """Fatia um item_bruto, roda a triagem sobre cada decisão resultante e
    grava tudo em `decisoes` numa única transação. Devolve as linhas de
    tabela já formatadas mais as contagens de relevantes/descartadas."""
    decisoes_fatiadas = fatiar_item(
        item["fonte_nome"], item["id"], item["titulo"] or "",
        item["texto_bruto"], item["url_origem"], item["data_publicacao"],
    )
    if not decisoes_fatiadas:
        # lista vazia não é sucesso silencioso: um documento real quase
        # sempre tem pelo menos uma decisão, então 0 é sinal de que o
        # fatiador não reconheceu o formato (achado real: era o caso do
        # TCE-MG antes do fix de segmentação). Propaga como erro — quem
        # chama marca o item_bruto como 'erro' e mostra no resumo, em vez
        # de marcar 'fatiado' com nada dentro.
        raise ValueError(
            f"fatiar_item devolveu 0 decisões pra item_bruto_id={item['id']} "
            f"(fonte={item['fonte_nome']!r}) — formato não reconhecido?"
        )

    processadas = []  # (decisao, resultado, metadados, chave)
    for decisao in decisoes_fatiadas:
        trecho = extrair_trecho(decisao.texto_decisao)
        resultado = triar(cliente, titulo=item["titulo"] or "", trecho=trecho)
        metadados = mesclar_metadados(decisao, resultado)
        chave = calcular_chave_dedup(
            tribunal=metadados["tribunal"],
            numero_acordao=metadados["numero_acordao"],
            numero_processo=decisao.numero_processo,
            data_julgamento=metadados["data_julgamento"],
            titulo_item_bruto=item["titulo"] or "",
            data_publicacao_item_bruto=item["data_publicacao"],
        )
        processadas.append((decisao, resultado, metadados, chave))

    linhas = []
    relevantes = descartadas = 0
    with transacao(conexao):
        for decisao, resultado, metadados, chave in processadas:
            triagem_status = "relevante" if resultado.relevante else "descartado"
            decisao_id = inserir_decisao(
                conexao,
                item_bruto_id=item["id"],
                chave_dedup=chave,
                tribunal=metadados["tribunal"],
                numero_acordao=metadados["numero_acordao"],
                numero_processo=decisao.numero_processo,
                orgao_julgador=decisao.orgao_julgador,
                relator=metadados["relator"],
                data_julgamento=metadados["data_julgamento"],
                url_inteiro_teor=decisao.url_inteiro_teor,
                tema=resultado.tema,
                impacto=resultado.impacto_estimado,
                triagem_status=triagem_status,
                triagem_motivo=resultado.motivo,
            )
            if resultado.relevante:
                relevantes += 1
            else:
                descartadas += 1
            repetida = " (já visto — chave repetida)" if decisao_id is None else ""
            linhas.append(_formatar_linha(item, decisao, metadados, resultado) + repetida)
        conexao.execute(
            "UPDATE itens_brutos SET status = 'fatiado' WHERE id = ?", (item["id"],),
        )

    return linhas, relevantes, descartadas


def _itens_pendentes(conexao, *, limite: int | None, reprocessar_erros: bool = False):
    """Por padrão, só status='coletado'. Com reprocessar_erros=True, some
    junto os que ficaram em 'erro' — aditivo, não substitui os novos."""
    status_alvo = ("coletado", "erro") if reprocessar_erros else ("coletado",)
    placeholders = ", ".join("?" for _ in status_alvo)
    sql = (
        "SELECT itens_brutos.*, fontes.nome AS fonte_nome "
        "FROM itens_brutos JOIN fontes ON fontes.id = itens_brutos.fonte_id "
        f"WHERE itens_brutos.status IN ({placeholders}) "
        "ORDER BY itens_brutos.id"
    )
    parametros: list = list(status_alvo)
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(int(limite))
    return conexao.execute(sql, parametros).fetchall()


def _imprimir_cabecalho() -> None:
    print(f"{'TÍTULO'.ljust(LARGURA_TITULO)}  {'RELEVANTE'.ljust(9)}  MOTIVO")
    print("-" * (LARGURA_TITULO + 9 + LARGURA_MOTIVO + 4))


def _formatar_linha(item, decisao, metadados: dict, resultado) -> str:
    # identificador_exibicao (fatiador) diferencia melhor as decisões de
    # uma mesma publicação do que repetir o título do item_bruto; onde a
    # fonte ainda não popula esse campo, cai pro comportamento antigo
    if decisao.identificador_exibicao:
        base = f"{item['titulo'] or '(sem título)'} — {decisao.identificador_exibicao}"
    else:
        base = f"{item['titulo'] or '(sem título)'} {metadados['numero_acordao'] or ''}".strip()
    titulo_curto = textwrap.shorten(base, width=LARGURA_TITULO, placeholder="…")
    relevante = "sim" if resultado.relevante else "não"
    motivo_curto = textwrap.shorten(resultado.motivo, width=LARGURA_MOTIVO, placeholder="…")
    return f"{titulo_curto.ljust(LARGURA_TITULO)}  {relevante.ljust(9)}  {motivo_curto}"


def _ler_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limite", type=int, default=None,
        help="processa no máximo N itens_brutos pendentes (padrão: todos)",
    )
    parser.add_argument(
        "--reprocessar-erros", action="store_true",
        help=(
            "também processa itens_brutos com status='erro' (reprocessamento "
            "após falha transitória, ex.: cota do LLM esgotada)"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
