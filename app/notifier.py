import apprise
import logging

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, telegram_token="", telegram_chat_id="", email=""):
        self.app = apprise.Apprise()

        if telegram_token and telegram_chat_id:
            url = f"tgram://{telegram_token}/{telegram_chat_id}"
            self.app.add(url)

        if email:
            self.app.add(email)

    def send(self, title: str, body: str):
        try:
            self.app.notify(
                title=title,
                body=body,
            )
        except Exception as error:
            logger.error(f"Notification error: {error}")