"""
channels/groupme.py
GroupMe channel adapter for Project Friday.
Polls approved groups for new messages and returns structured message dicts.
"""

import requests
import logging
from datetime import datetime

logger = logging.getLogger("friday.groupme")

GROUPME_API = "https://api.groupme.com/v3"


class GroupMeChannel:
    def __init__(self, config: dict, memory):
        self.token = config.get("api_token", "")
        self.approved_groups = [g.lower() for g in config.get("approved_groups", [])]
        self.memory = memory
        self._group_cache = {}  # name → group_id

    def _headers(self):
        return {"X-Access-Token": self.token}

    def _get_groups(self) -> list:
        """Fetch all groups the user is a member of."""
        try:
            resp = requests.get(
                f"{GROUPME_API}/groups",
                headers=self._headers(),
                params={"per_page": 100},
                timeout=10
            )
            resp.raise_for_status()
            return resp.json().get("response", [])
        except Exception as e:
            logger.error(f"Failed to fetch GroupMe groups: {e}")
            return []

    def _get_approved_group_ids(self) -> dict:
        """Return a dict of {group_name_lower: group_id} for approved groups."""
        if self._group_cache:
            return self._group_cache

        groups = self._get_groups()
        result = {}
        for group in groups:
            name = group.get("name", "").lower()
            if name in self.approved_groups:
                result[name] = group["id"]
                logger.info(f"Found approved GroupMe group: '{group['name']}' (ID: {group['id']})")

        if not result:
            logger.warning(f"No approved groups found. Looking for: {self.approved_groups}")

        self._group_cache = result
        return result

    def _get_messages(self, group_id: str, group_name: str) -> list:
        """Fetch recent messages from a group, returning only unprocessed ones."""
        memory_key = f"groupme_last_id_{group_id}"
        last_id = self.memory.recall(memory_key)

        params = {"limit": 20}
        if last_id:
            params["after_id"] = last_id

        try:
            resp = requests.get(
                f"{GROUPME_API}/groups/{group_id}/messages",
                headers=self._headers(),
                params=params,
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json().get("response", {})
            messages = data.get("messages", [])
        except Exception as e:
            logger.error(f"Failed to fetch messages for group {group_id}: {e}")
            return []

        if not messages:
            return []

        # Store the most recent message ID so we don't reprocess next poll
        newest_id = messages[0].get("id")
        if newest_id:
            self.memory.remember(memory_key, newest_id)

        # Filter out already-processed messages and system messages
        new_messages = []
        for msg in messages:
            msg_id = msg.get("id")
            sender_type = msg.get("sender_type", "")

            # Skip system messages (GroupMe join/leave notifications etc.)
            if sender_type == "system":
                continue

            if msg_id and not self.memory.is_processed("groupme", msg_id):
                self.memory.mark_processed("groupme", msg_id)
                new_messages.append({
                    "id": msg_id,
                    "group_name": group_name,
                    "group_id": group_id,
                    "sender_name": msg.get("name", "Unknown"),
                    "text": msg.get("text", "") or "",
                    "created_at": datetime.fromtimestamp(
                        msg.get("created_at", 0)
                    ).isoformat(),
                    "source": "groupme"
                })

        if new_messages:
            logger.info(f"GroupMe '{group_name}': {len(new_messages)} new message(s)")

        return new_messages

    def poll(self) -> list:
        """
        Poll all approved GroupMe groups for new messages.
        Returns a list of message dicts ready for the filter pipeline.
        """
        if not self.token:
            logger.error("GroupMe API token not set in friday_config.yaml")
            return []

        group_ids = self._get_approved_group_ids()
        all_messages = []

        for group_name, group_id in group_ids.items():
            messages = self._get_messages(group_id, group_name)
            all_messages.extend(messages)

        return all_messages
