import logging

from nucleo.log import FormatadorEstruturado, configurar_logging


def test_configurar_logging_nao_gera_erro():
    configurar_logging()
    logging.getLogger("teste").info("ok")


def test_formatador_anexa_campos_extra():
    formatador = FormatadorEstruturado(fmt="%(message)s")
    registro = logging.LogRecord(
        name="teste", level=logging.INFO, pathname=__file__, lineno=1,
        msg="coleta falhou", args=(), exc_info=None,
    )
    registro.fonte = "zenite"
    assert formatador.format(registro) == "coleta falhou | fonte=zenite"


def test_formatador_sem_extras_nao_anexa_separador():
    formatador = FormatadorEstruturado(fmt="%(message)s")
    registro = logging.LogRecord(
        name="teste", level=logging.INFO, pathname=__file__, lineno=1,
        msg="ok", args=(), exc_info=None,
    )
    assert formatador.format(registro) == "ok"
