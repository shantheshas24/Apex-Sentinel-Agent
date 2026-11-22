"""Twilio adapter for critical deadline escalation."""
import asyncio
from dataclasses import dataclass

from twilio.base.exceptions import TwilioRestException

from config.settings import Settings
from utils.logger import get_logger

logger = get_logger("Notifier")

ACTIVE_CALL_STATUSES = frozenset({"queued", "ringing", "in-progress"})
CALL_STATUSES = ACTIVE_CALL_STATUSES | {"completed", "busy", "no-answer", "failed", "canceled"}


@dataclass(frozen=True)
class CallProgress:
    status: str
    sid: str | None = None


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        self._twilio_client = None
        if self._cfg.deadline_phone_available:
            try:
                from twilio.http.http_client import TwilioHttpClient
                from twilio.rest import Client
                self._twilio_client = Client(
                    self._cfg.twilio_account_sid,
                    self._cfg.twilio_auth_token,
                    http_client=TwilioHttpClient(timeout=10),
                )
                logger.info("twilio_initialized")
            except Exception as exc:
                logger.warning("twilio_init_failed", error_type=type(exc).__name__)
                self._twilio_client = None

    async def call_for_deadline(self, task_id: str) -> CallProgress:
        """Attempt a phone alert for a critical task."""
        if not self._twilio_client or not self._cfg.deadline_phone_available:
            return CallProgress("failed")
        twiml = (
            '<Response><Say>Personal O S reminder. A critical deadline '
            'is approaching. Check your deadline dashboard for details.</Say></Response>'
        )
        try:
            call = await asyncio.to_thread(
                self._twilio_client.calls.create,
                twiml=twiml,
                to=self._cfg.twilio_to_number,
                from_=self._cfg.twilio_from_number,
            )
            logger.info("deadline_call_accepted", task_id=task_id)
            return CallProgress(call.status if call.status in CALL_STATUSES else "queued", call.sid)
        except TwilioRestException as exc:
            logger.warning("deadline_call_failed", task_id=task_id, status_code=exc.status)
            return CallProgress("failed" if 400 <= exc.status < 500 else "unconfirmed")
        except Exception as exc:
            logger.warning("deadline_call_failed", task_id=task_id, error_type=type(exc).__name__)
            return CallProgress("unconfirmed")

    async def get_call_status(self, sid: str) -> str | None:
        if not self._twilio_client:
            return None
        try:
            call = await asyncio.to_thread(self._twilio_client.calls(sid).fetch)
            return call.status if call.status in CALL_STATUSES else None
        except Exception as exc:
            logger.warning("deadline_call_status_unavailable", error_type=type(exc).__name__)
            return None
