"""nucleo/envio.py — Camada 7: envio do e-mail via Gmail SMTP.

Host/porta confirmados na documentação oficial do Google Workspace
(2026-08-12): smtp.gmail.com, porta 587 com STARTTLS (alternativa: 465
com SSL direto). Autenticação por senha de app, não a senha normal da
conta — https://myaccount.google.com/apppasswords (já documentado em
.env.example).

Só biblioteca padrão (smtplib/email) — nenhuma dependência nova.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

HOST_PADRAO = "smtp.gmail.com"
PORTA_PADRAO = 587


def enviar_email(destinatario: str, assunto: str, corpo_html: str, *,
                  usuario: str, senha_app: str,
                  host: str = HOST_PADRAO, porta: int = PORTA_PADRAO) -> None:
    """Envia `corpo_html` como e-mail HTML. Levanta a exceção do smtplib
    em caso de falha — sem retry embutido de propósito: quem chama só
    marca a decisão como enviada depois desta função retornar sem erro,
    então uma falha aqui faz as mesmas decisões aparecerem de novo na
    próxima rodada (retry natural pelo desenho do schema, sem precisar de
    lógica extra)."""
    mensagem = MIMEMultipart("alternative")
    mensagem["Subject"] = assunto
    mensagem["From"] = usuario
    mensagem["To"] = destinatario
    mensagem.attach(MIMEText(corpo_html, "html", "utf-8"))

    with smtplib.SMTP(host, porta) as servidor:
        servidor.starttls()
        servidor.login(usuario, senha_app)
        servidor.send_message(mensagem)
