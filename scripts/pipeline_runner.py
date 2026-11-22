"""Queue unread Gmail messages for the running deadline tracker."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def fetch_emails_httpx(creds, max_results=10):
    """Fetch unread Gmail messages using httpx (avoids httplib2 timeout issues)."""
    import httpx
    token = creds.token
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        # List message IDs
        r = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"maxResults": max_results, "q": "is:unread"},
        )
        r.raise_for_status()
        message_ids = [m["id"] for m in r.json().get("messages", [])]

        # Fetch metadata for each
        emails = []
        for mid in message_ids:
            r2 = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                headers=headers,
                params={"format": "full"},
            )
            r2.raise_for_status()
            emails.append(r2.json())
        return emails


async def main():
    from personal_os_agent.agents.observer_agent import ObserverAgent
    from personal_os_agent.mcp_server.gmail_tools import message_payload
    from personal_os_agent.mcp_server.google_auth import get_credentials
    from personal_os_agent.memory.redis_client import RedisClient

    credentials = await asyncio.to_thread(get_credentials)
    messages = await fetch_emails_httpx(credentials, max_results=20)
    redis = RedisClient.get_instance()
    queued = 0
    try:
        for message in messages:
            if await redis.enqueue_unseen_email(ObserverAgent._normalize_email(message_payload(message))):
                queued += 1
    finally:
        await redis.close()
    print(f"Queued {queued} unread Gmail messages for deadline detection.")


if __name__ == "__main__":
    asyncio.run(main())
