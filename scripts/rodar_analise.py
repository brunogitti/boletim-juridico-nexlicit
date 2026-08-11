"""scripts/rodar_analise.py — comando de linha da Camada 5 (análise),
pedido pela Etapa 6.

Roda sobre `decisoes` com triagem_status='relevante' e analisado_em IS
NULL. Pra cada uma, checa a âncora (CLAUDE.md: número de acórdão OU
processo E link — sem isso, vira 'sem_ancora' e pula a chamada ao LLM,
já que esse item nunca entraria no e-mail mesmo). Passando na âncora,
recupera o texto completo da decisão (refatiando o item_bruto de origem —
`decisoes` não guarda o texto, só os metadados), chama a análise e grava
artigos_lei/impacto/resumo. Imprime cada item no formato do modelo da
Camada 6 (docs/ARQUITETURA.md) — é o preview do que vira e-mail na Etapa 7.

Uso:
    python -m scripts.rodar_analise [--limite N]
"""

import argparse
import logging
import os
import sys
import textwrap

from dotenv import load_dotenv

from nucleo.analise import analisar, recuperar_ancora_do_texto, tem_ancora
from nucleo.banco import atualizar_analise, conectar
from nucleo.fatiador import DecisaoFatiada, fatiar_item
from nucleo.llm import criar_cliente_llm
from nucleo.log import configurar_logging

logger = logging.getLogger(__name__)

LARGURA_RESUMO = 100


def main() -> None:
    # achado real: no Windows, print() redirecionado pra arquivo (ou pra
    # um console sem UTF-8) cai no codepage padrão (cp1252), que não tem
    # "→" nem boa parte do que o LLM pode devolver em resumo/artigos —
    # UnicodeEncodeError derrubou uma rodada inteira já processada
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

    itens = _decisoes_pendentes(conexao, limite=args.limite)
    if not itens:
        print("Nenhuma decisão relevante pendente de análise.")
        conexao.close()
        return

    total_analisadas = total_sem_ancora = total_com_erro = 0

    for item in itens:
        numero_acordao = item["numero_acordao"]
        numero_processo = item["numero_processo"]
        texto_completo = None
        recuperado_do_rodape = False

        if not (numero_acordao or numero_processo):
            # achado real (2026-08-11): a triagem só vê 1200 caracteres, e
            # em artigos longos da Zênite o número só aparece num rodapé
            # "Serviço" no fim do texto — tenta achar no texto completo
            # (que só a Análise tem) antes de aceitar sem_ancora
            try:
                texto_completo = _recuperar_texto_completo(item)
            except Exception as erro:
                logger.warning(
                    "não consegui recuperar o texto pra tentar achar âncora",
                    extra={"decisao_id": item["id"], "erro": str(erro)},
                )
            if texto_completo is not None:
                numero_acordao, numero_processo = recuperar_ancora_do_texto(texto_completo)
                if numero_acordao or numero_processo:
                    recuperado_do_rodape = True
                    logger.info(
                        "achou número no texto completo, não estava no trecho da triagem",
                        extra={
                            "decisao_id": item["id"], "numero_acordao": numero_acordao,
                            "numero_processo": numero_processo,
                        },
                    )

        if not tem_ancora(
            numero_acordao=numero_acordao, numero_processo=numero_processo,
            url_inteiro_teor=item["url_inteiro_teor"],
        ):
            atualizar_analise(conexao, item["id"], triagem_status="sem_ancora")
            conexao.commit()
            total_sem_ancora += 1
            logger.info(
                "sem âncora — não entra no e-mail",
                extra={"decisao_id": item["id"], "tribunal": item["tribunal"]},
            )
            continue

        try:
            if texto_completo is None:
                texto_completo = _recuperar_texto_completo(item)
            resultado = analisar(
                cliente, titulo=item["titulo_publicacao"] or "", texto_completo=texto_completo,
            )
        except Exception as erro:
            # falha isolada — um erro do LLM ou do refatiamento não pode
            # travar as outras; analisado_em fica NULL, tenta de novo depois
            logger.error(
                "falha ao analisar decisão, tentando na próxima rodada",
                extra={"decisao_id": item["id"], "erro": str(erro)},
            )
            total_com_erro += 1
            continue

        kwargs_ancora = {}
        if recuperado_do_rodape:
            # só grava se achou algo novo — nunca sobrescreve com None
            # (ver o sentinela _NAO_INFORMADO em nucleo.banco.atualizar_analise)
            kwargs_ancora = {"numero_acordao": numero_acordao, "numero_processo": numero_processo}

        atualizar_analise(
            conexao, item["id"],
            artigos_lei=resultado.artigos_lei, impacto=resultado.impacto,
            resumo=resultado.resumo, **kwargs_ancora,
        )
        conexao.commit()
        total_analisadas += 1
        print(_formatar_item(item, resultado, numero_acordao, numero_processo))
        print()

    conexao.close()

    print(
        f"{total_analisadas} decisões analisadas, {total_sem_ancora} sem âncora, "
        f"{total_com_erro} com erro (tentam de novo na próxima rodada)."
    )


def _recuperar_texto_completo(item) -> str:
    """`decisoes` não guarda o texto — só itens_brutos.texto_bruto (a
    publicação inteira). Refatia o item_bruto de origem (determinístico,
    mesmo texto_bruto sempre devolve as mesmas decisões) e casa pela
    combinação (tribunal, numero_acordao, numero_processo) já salva."""
    decisoes_fatiadas = fatiar_item(
        item["fonte_nome"], item["item_bruto_id"], item["titulo_publicacao"] or "",
        item["texto_bruto"], item["url_origem"], item["data_publicacao"],
    )
    if len(decisoes_fatiadas) == 1:
        return decisoes_fatiadas[0].texto_decisao

    match = _casar_decisao(decisoes_fatiadas, item)
    if match is None:
        raise ValueError(
            f"não achei a decisão id={item['id']} entre as "
            f"{len(decisoes_fatiadas)} que o fatiador devolveu pro "
            f"item_bruto_id={item['item_bruto_id']}"
        )
    return match.texto_decisao


def _casar_decisao(decisoes_fatiadas: list[DecisaoFatiada], item) -> DecisaoFatiada | None:
    for d in decisoes_fatiadas:
        if (d.tribunal == item["tribunal"]
                and d.numero_acordao == item["numero_acordao"]
                and d.numero_processo == item["numero_processo"]):
            return d
    return None


def _decisoes_pendentes(conexao, *, limite: int | None):
    sql = (
        # decisoes.* já traz item_bruto_id — não repetir itens_brutos.id
        # com o mesmo alias (sqlite3.Row aceita coluna duplicada e só
        # devolve a primeira, mas é frágil, evitar)
        "SELECT decisoes.*, "
        "itens_brutos.titulo AS titulo_publicacao, "
        "itens_brutos.texto_bruto, itens_brutos.url_origem, itens_brutos.data_publicacao, "
        "fontes.nome AS fonte_nome "
        "FROM decisoes "
        "JOIN itens_brutos ON itens_brutos.id = decisoes.item_bruto_id "
        "JOIN fontes ON fontes.id = itens_brutos.fonte_id "
        "WHERE decisoes.triagem_status = 'relevante' AND decisoes.analisado_em IS NULL "
        "ORDER BY decisoes.id"
    )
    parametros: list = []
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(int(limite))
    return conexao.execute(sql, parametros).fetchall()


def _formatar_item(item, resultado, numero_acordao=None, numero_processo=None) -> str:
    # numero_acordao/numero_processo vêm à parte (não de item[...]) porque
    # podem ter sido recuperados do rodapé "Serviço" do texto completo,
    # não do que a triagem salvou originalmente — ver recuperar_ancora_do_texto
    impacto = (resultado.impacto or "?").upper()
    identificador = numero_acordao or numero_processo or "sem identificador"
    cabecalho = f"[{impacto}] {item['tribunal']} — {identificador}"
    if item["orgao_julgador"]:
        cabecalho += f" — {item['orgao_julgador']}"
    if item["data_julgamento"]:
        cabecalho += f" — {item['data_julgamento']}"

    linhas = [cabecalho]
    if item["tema"]:
        linhas.append(f"Tema: {item['tema']}")
    linhas.append("")
    linhas.append(textwrap.fill(resultado.resumo or "(sem resumo)", width=LARGURA_RESUMO))
    linhas.append("")
    if resultado.artigos_lei:
        linhas.append(f"Artigos: {', '.join(resultado.artigos_lei)}")
    linhas.append(f"→ Inteiro teor: {item['url_inteiro_teor']}")
    return "\n".join(linhas)


def _ler_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limite", type=int, default=None,
        help="processa no máximo N decisões pendentes (padrão: todas)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
