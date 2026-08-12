"""scripts/rodar_boletim.py — comando de linha da Camada 6+7 (boletim +
envio), pedido pela Etapa 7.

Roda sobre `decisoes` com triagem_status='relevante', analisado_em IS NOT
NULL e enviado_em IS NULL (usa o índice idx_decisoes_envio, existente
desde a Etapa 1). Monta o HTML (nucleo.boletim.montar_boletim) e:

- Sem decisão pendente, ou nenhuma sobrevive à revalidação de âncora:
  encerra com log, não envia nada (Camada 7).
- Com --dry-run: grava o HTML num arquivo em vez de enviar. Não marca
  nada como enviado — pode rodar quantas vezes quiser pra revisar visual.
- Sem --dry-run: envia via Gmail SMTP (nucleo.envio.enviar_email) usando
  GMAIL_USER/GMAIL_APP_PASSWORD/EMAIL_DESTINATARIO do .env. Só marca
  enviado_em nas decisões incluídas depois do envio suceder sem exceção.

`--fontes-com-falha` é manual nesta etapa: coletar_tudo.py já calcula essa
lista (Camada 1) mas não a persiste em lugar nenhum ainda — fechar esse
elo automaticamente é orquestração de dia inteiro, escopo da Etapa 8.

Uso:
    python -m scripts.rodar_boletim [--dry-run] [--saida ARQUIVO]
                                     [--fontes-com-falha "Nome A,Nome B"]
"""

import argparse
import json
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

from nucleo.banco import conectar, marcar_decisao_enviada, transacao
from nucleo.boletim import montar_boletim
from nucleo.envio import enviar_email
from nucleo.log import configurar_logging

logger = logging.getLogger(__name__)


def main() -> None:
    # mesmo achado real de scripts/rodar_triagem.py e rodar_analise.py:
    # print() redirecionado no Windows cai no codepage padrão (cp1252)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv()
    configurar_logging()

    args = _ler_argumentos()

    caminho_banco = os.environ.get("DATABASE_PATH")
    if not caminho_banco:
        print("DATABASE_PATH não configurado no .env", file=sys.stderr)
        raise SystemExit(1)

    conexao = conectar(caminho_banco)
    itens = _decisoes_pendentes(conexao)

    if not itens:
        print("Nenhuma decisão relevante pendente de envio.")
        conexao.close()
        return

    decisoes = [_linha_para_decisao(item) for item in itens]
    data_referencia = date.today()
    html = montar_boletim(
        decisoes, data_referencia=data_referencia,
        fontes_com_falha=args.fontes_com_falha,
    )

    if html is None:
        # todas as pendentes falharam a revalidação de âncora dentro de
        # montar_boletim (não deveria acontecer — já logado lá dentro)
        print("Nenhuma decisão com âncora válida. Nada enviado.")
        conexao.close()
        return

    if args.dry_run:
        caminho_saida = args.saida or f"boletim_{data_referencia.isoformat()}.html"
        with open(caminho_saida, "w", encoding="utf-8") as arquivo:
            arquivo.write(html)
        print(f"--dry-run: HTML salvo em {caminho_saida} ({len(decisoes)} decisões). Nada enviado.")
        conexao.close()
        return

    destinatario, usuario, senha_app = _credenciais_email()
    assunto = f"Boletim Jurídico NexLicit — {data_referencia.strftime('%d/%m/%Y')}"
    enviar_email(
        destinatario, assunto, html, usuario=usuario, senha_app=senha_app,
    )

    with transacao(conexao):
        for item in itens:
            marcar_decisao_enviada(conexao, item["id"])

    conexao.close()
    print(f"E-mail enviado para {destinatario} — {len(decisoes)} decisões.")


def _decisoes_pendentes(conexao):
    sql = (
        "SELECT * FROM decisoes "
        "WHERE triagem_status = 'relevante' AND analisado_em IS NOT NULL "
        "AND enviado_em IS NULL "
        "ORDER BY id"
    )
    return conexao.execute(sql).fetchall()


def _linha_para_decisao(item) -> dict:
    """Converte sqlite3.Row em dict simples pro montar_boletim consumir —
    artigos_lei sai de JSON (TEXT na coluna) pra list[str]/None."""
    dados = dict(item)
    dados["artigos_lei"] = json.loads(item["artigos_lei"]) if item["artigos_lei"] else None
    return dados


def _credenciais_email() -> tuple[str, str, str]:
    destinatario = os.environ.get("EMAIL_DESTINATARIO")
    usuario = os.environ.get("GMAIL_USER")
    senha_app = os.environ.get("GMAIL_APP_PASSWORD")
    faltando = [
        nome for nome, valor in (
            ("EMAIL_DESTINATARIO", destinatario), ("GMAIL_USER", usuario),
            ("GMAIL_APP_PASSWORD", senha_app),
        ) if not valor
    ]
    if faltando:
        print(f"Faltando no .env: {', '.join(faltando)}", file=sys.stderr)
        raise SystemExit(1)
    assert destinatario and usuario and senha_app  # garantido pelo raise acima
    return destinatario, usuario, senha_app


def _lista_fontes_com_falha(valor: str | None) -> list[str]:
    if not valor:
        return []
    return [nome.strip() for nome in valor.split(",") if nome.strip()]


def _ler_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="salva o HTML em arquivo em vez de enviar (não marca nada como enviado)",
    )
    parser.add_argument(
        "--saida", default=None,
        help="caminho do arquivo HTML com --dry-run (padrão: boletim_AAAA-MM-DD.html)",
    )
    parser.add_argument(
        "--fontes-com-falha", default=None,
        help='nomes separados por vírgula, ex.: "TCU,STJ" — vai pro rodapé do e-mail',
    )
    args = parser.parse_args()
    args.fontes_com_falha = _lista_fontes_com_falha(args.fontes_com_falha)
    return args


if __name__ == "__main__":
    main()
