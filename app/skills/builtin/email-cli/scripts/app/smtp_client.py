import smtplib
import ssl
from email.message import EmailMessage

from . import config


class SmtpError(RuntimeError):
    pass


class SmtpClient:
    def __init__(self) -> None:
        self.conn: smtplib.SMTP | None = None

    def __enter__(self) -> "SmtpClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        user, pwd = config.require_credentials()
        ctx = ssl.create_default_context()
        self.conn = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        self.conn.ehlo()
        self.conn.starttls(context=ctx)
        self.conn.ehlo()
        try:
            self.conn.login(user, pwd)
        except smtplib.SMTPAuthenticationError as e:
            raise SmtpError(
                f"Gmail SMTP login failed: {e}. Verify USERNAME and that PASSWORD is a "
                "Gmail App Password (https://myaccount.google.com/apppasswords)."
            ) from e

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.quit()
        except Exception:
            try:
                self.conn.close()
            except Exception:
                pass
        finally:
            self.conn = None

    def send(self, msg: EmailMessage) -> None:
        assert self.conn is not None
        self.conn.send_message(msg)
