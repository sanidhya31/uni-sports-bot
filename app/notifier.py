import apprise
import logging

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, telegram_token="", telegram_chat_id="", email=""):
        self.app = apprise.Apprise()
        self.enabled = False

        if telegram_token and telegram_chat_id:
            url = f"tgram://{telegram_token}/{telegram_chat_id}"
            self.enabled = self.app.add(url) or self.enabled

        if email:
            self.enabled = self.app.add(email) or self.enabled

    def send(self, title: str, body: str):
        if not self.enabled:
            return

        try:
            self.app.notify(
                title=title,
                body=body,
            )
        except Exception as error:
            logger.error(f"Notification error: {error}")
