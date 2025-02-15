"""
This module contains all code relevant to interacting with antivirus agents.

Functions:
    poll_antivirus_alerts: Periodically queries Graylog for A/V-related alerts of interest.
    parse_clamav_alert: Extracts fields from a ClamAV "FOUND" alert.
"""

import datetime
import json
import logging
import re
import requests
import time

from celery import current_app
from celery.app import shared_task
from celery.utils.log import get_task_logger
from typing import Any, Dict, List, Union

from aica_django.connectors.SIEM import Graylog

logger = get_task_logger(__name__)


clam_parser = re.compile(
    r"^(\S+)\s+clamav\: ([^-]+) -> ([^:]+): ([^-]+)-([^-]+)-([^(]+)\(([a-f0-9]+):(\d+)\) FOUND"
)


# ClamAV logs are not in json, so we need to format them into something like that
def parse_clamav_alert(line: str) -> Dict[str, Any]:
    """
    Extract fields from ClamAV "FOUND" alert into a dictionary.

    @param line: The log event to parse
    @type line: str
    @return: The extracted fields as a dictionary
    @rtype: dict or bool
    @raise: ValueError: if the FOUND line cannot be parsed
    """
    event_dict: Dict[str, Any] = dict()

    if "FOUND" in line:
        event_dict["event_type"] = "alert"
        matcher = clam_parser.fullmatch(line)
        if matcher is None:
            raise ValueError("Invalid ClamAV line encountered")

        event_dict["hostname"] = matcher.group(1)
        event_dict["date"] = matcher.group(2)
        event_dict["path"] = matcher.group(3)
        event_dict["platform"] = matcher.group(4)
        event_dict["category"] = matcher.group(5)
        event_dict["name"] = matcher.group(6)
        event_dict["signature"] = matcher.group(7)
        event_dict["revision"] = matcher.group(8)

        return event_dict

    else:
        raise ValueError("Invalid ClamAV line encountered")


@shared_task(name="poll-antivirus-alerts")
def poll_clamav_alerts(frequency: int = 30, single: bool = False) -> None:
    """
    Periodically query Graylog for ClamAV "FOUND" alerts, and add to the knowledge graph.

    @param frequency: How often to query Graylog (default, 30 seconds)
    @type frequency: int
    @param single: Run a single poll only, without looping
    @type single: bool
    """

    logger.info(f"Running {__name__}: poll_dbs")

    gl = Graylog("antivirus")

    while True:
        to_time = datetime.datetime.now()
        from_time = to_time - datetime.timedelta(seconds=frequency)

        query_params: Dict[str, Union[str, int, List[str]]] = {
            "query": r"clamav\: AND FOUND",  # Required
            "from": from_time.strftime("%Y-%m-%d %H:%M:%S"),  # Required
            "to": to_time.strftime("%Y-%m-%d %H:%M:%S"),  # Required
            "fields": ["message"],  # Required
            "limit": 150,  # Optional: Default limit is 150 in Graylog
        }

        response = gl.query_graylog(query_params)

        try:
            response.raise_for_status()
            if response.json()["total_results"] > 0:
                for message in response.json()["messages"]:
                    event = message["message"]["message"]
                    alert_dict = parse_clamav_alert(event)
                    if alert_dict:
                        alert_dict = json.loads(json.dumps(alert_dict))
                        current_app.send_task(
                            "ma-knowledge_base-record_antivirus_alert",
                            [alert_dict],
                        )
                        current_app.send_task(
                            "ma-decision_making_engine-handle_antivirus_alert",
                            [alert_dict],
                        )
        except requests.exceptions.HTTPError as e:
            logging.error(f"{e}\n{response.text}")

        if single:
            break

        execution_time = (to_time - datetime.datetime.now()).total_seconds()
        time.sleep(frequency - execution_time)
