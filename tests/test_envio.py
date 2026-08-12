"""Testes de nucleo/envio.py — smtplib.SMTP mockado, nenhuma conexão real."""

import pytest

from nucleo.envio import enviar_email


class _SMTPFalso:
    """Substitui smtplib.SMTP: registra as chamadas em vez de abrir socket."""

    instancias: list["_SMTPFalso"] = []

    def __init__(self, host, porta):
        self.host = host
        self.porta = porta
        self.chamadas = []
        _SMTPFalso.instancias.append(self)

    def starttls(self):
        self.chamadas.append("starttls")

    def login(self, usuario, senha):
        self.chamadas.append(("login", usuario, senha))

    def send_message(self, mensagem):
        self.chamadas.append(("send_message", mensagem))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _SMTPQueFalha(_SMTPFalso):
    def send_message(self, mensagem):
        raise RuntimeError("SMTP indisponível")


@pytest.fixture(autouse=True)
def _limpar_instancias():
    _SMTPFalso.instancias.clear()
    yield
    _SMTPFalso.instancias.clear()


def test_enviar_email_usa_host_porta_padrao_e_starttls(monkeypatch):
    monkeypatch.setattr("nucleo.envio.smtplib.SMTP", _SMTPFalso)

    enviar_email(
        "dest@exemplo.com", "Assunto", "<p>Corpo</p>",
        usuario="user@gmail.com", senha_app="senha-app",
    )

    assert len(_SMTPFalso.instancias) == 1
    instancia = _SMTPFalso.instancias[0]
    assert instancia.host == "smtp.gmail.com"
    assert instancia.porta == 587
    assert "starttls" in instancia.chamadas
    assert ("login", "user@gmail.com", "senha-app") in instancia.chamadas


def test_enviar_email_monta_mensagem_com_destinatario_assunto_e_html(monkeypatch):
    monkeypatch.setattr("nucleo.envio.smtplib.SMTP", _SMTPFalso)

    enviar_email(
        "dest@exemplo.com", "Boletim de hoje", "<p>Conteúdo</p>",
        usuario="user@gmail.com", senha_app="senha-app",
    )

    instancia = _SMTPFalso.instancias[0]
    envio = next(c for c in instancia.chamadas if isinstance(c, tuple) and c[0] == "send_message")
    mensagem = envio[1]
    assert mensagem["To"] == "dest@exemplo.com"
    assert mensagem["Subject"] == "Boletim de hoje"
    assert mensagem["From"] == "user@gmail.com"
    assert "Conteúdo" in mensagem.get_payload()[0].get_payload(decode=True).decode("utf-8")


def test_enviar_email_respeita_host_e_porta_customizados(monkeypatch):
    monkeypatch.setattr("nucleo.envio.smtplib.SMTP", _SMTPFalso)

    enviar_email(
        "dest@exemplo.com", "Assunto", "<p>Corpo</p>",
        usuario="user@gmail.com", senha_app="senha-app",
        host="smtp.outro.com", porta=465,
    )

    instancia = _SMTPFalso.instancias[0]
    assert instancia.host == "smtp.outro.com"
    assert instancia.porta == 465


def test_enviar_email_propaga_excecao_do_smtp_sem_engolir(monkeypatch):
    # achado de design (plano da Etapa 7): sem retry embutido de propósito
    # — quem chama só marca a decisão como enviada depois desta função
    # retornar sem erro
    monkeypatch.setattr("nucleo.envio.smtplib.SMTP", _SMTPQueFalha)

    with pytest.raises(RuntimeError, match="SMTP indisponível"):
        enviar_email(
            "dest@exemplo.com", "Assunto", "<p>Corpo</p>",
            usuario="user@gmail.com", senha_app="senha-app",
        )
