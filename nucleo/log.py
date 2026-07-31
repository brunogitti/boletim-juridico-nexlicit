"""Configuração de logging do pipeline.

"Estruturado" aqui quer dizer: toda mensagem carrega nível e módulo de
origem, e qualquer contexto extra (qual fonte, qual etapa) passado via
`extra={...}` aparece como chave=valor no fim da linha — sem virar JSON,
pra continuar legível no terminal e no log do GitHub Actions.

Uso:
    from nucleo.log import configurar_logging
    configurar_logging()
    logger = logging.getLogger(__name__)
    logger.warning("coleta falhou", extra={"fonte": "zenite"})
"""

import logging

_CAMPOS_PADRAO_LOG_RECORD = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
)


class FormatadorEstruturado(logging.Formatter):
    """Formata a linha padrão e anexa campo extra como chave=valor."""

    def format(self, record: logging.LogRecord) -> str:
        # Precisa ler os campos extras ANTES de chamar super().format(): o
        # Formatter padrão seta record.message (e record.asctime) como
        # efeito colateral, e esses dois entrariam como "extra" se lidos
        # depois.
        extras = {
            chave: valor
            for chave, valor in record.__dict__.items()
            if chave not in _CAMPOS_PADRAO_LOG_RECORD
        }
        mensagem = super().format(record)
        if not extras:
            return mensagem
        pares = " ".join(f"{chave}={valor}" for chave, valor in extras.items())
        return f"{mensagem} | {pares}"


def configurar_logging(nivel: int = logging.INFO) -> None:
    """Configura o logging raiz. Chamar uma única vez, no ponto de entrada."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        FormatadorEstruturado(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.basicConfig(level=nivel, handlers=[handler], force=True)
